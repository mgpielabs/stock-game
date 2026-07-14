"""
확률 calibration 검증 + 운영 모델 calibrator 생성 (진단#7 후속, 2026-06-22)

진단(diagnose_model.py)에서 확인된 calibration gap(예측확률이 실제 적중률보다
18~24%p 과대평가)을 Isotonic Regression으로 보정했을 때:
  1. 보정 후 calibration gap이 얼마나 줄어드는지 (held-out 분리로 과적합 여부 확인)
  2. top10 순위가 보정 전/후 동일한지 (단조변환이라 이론상 항상 동일해야 함 — 실측으로 재확인)

운영 서빙(predictor.py)과 동일한 CatBoost(0.8)+XGBoost(0.2) 블렌드를 그대로 사용.
val set(최근 252거래일)을 시간순으로 다시 7:3 분리해 보정 함수는 앞 70%로만 학습하고
뒤 30%(완전히 보지 않은 기간)로 검증 — 그래야 "보정 함수가 val set에 과적합되지 않았는지"
확인 가능. 검증 후 최종 calibrator는 val 전체로 다시 학습해 현재 운영 모델 디렉터리에
저장(이후 재학습부터는 train.py가 자동 갱신 — 이 스크립트는 train.py 도입 전 학습된
기존 운영 모델에 1회성으로 적용하기 위한 것).

실행: python calibration_analysis.py
출력: backend/ml/calibration_report.md (분석 결과물)
      models/{운영모델}/calibrator.pkl (predictor.py가 실제로 로드하는 운영 파일)
"""

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

ML_DIR = Path(__file__).parent
SERVER_DIR = ML_DIR.parent / "server"
sys.path.insert(0, str(ML_DIR))
sys.path.insert(0, str(SERVER_DIR))

from calibration_utils import fit_calibrator_with_holdout_check  # noqa: E402
from dataset import FEATURE_COLS, build_dataset  # noqa: E402
from predictor import load_model  # noqa: E402 (production과 동일 블렌드 가중치 재사용)

OUT_REPORT = ML_DIR / "calibration_report.md"


def decile_table(proba: np.ndarray, y: np.ndarray) -> pd.DataFrame:
    df = pd.DataFrame({"proba": proba, "y": y})
    df["decile"] = pd.qcut(df["proba"], 10, labels=False, duplicates="drop")
    g = df.groupby("decile").agg(
        n=("y", "size"), 평균예측확률=("proba", "mean"), 실제적중률=("y", "mean")
    ).reset_index()
    g["gap(예측-실제)"] = (g["평균예측확률"] - g["실제적중률"]).round(4)
    return g.round(4)


def topk_overlap(dates: pd.Series, raw: np.ndarray, calibrated: np.ndarray, k: int = 10) -> float:
    """날짜별 top-k 종목 집합이 보정 전/후 동일한 비율 (이론상 1.0이어야 함 — 동률 처리 차이만 예외)."""
    df = pd.DataFrame({"date": dates.values, "raw": raw, "cal": calibrated})
    same, total = 0, 0
    for _, grp in df.groupby("date"):
        top_raw = set(grp.sort_values("raw", ascending=False).head(k).index)
        top_cal = set(grp.sort_values("cal", ascending=False).head(k).index)
        same += int(top_raw == top_cal)
        total += 1
    return same / total if total else 1.0


def main():
    print("운영 모델 로드 (target_5d, CatBoost 0.8 + XGBoost 0.2 블렌드)...")
    model, model_dir = load_model("target_5d")
    print(f"모델: {model_dir.name}\n")

    print("데이터셋 빌드 중 (target_5d, val_days=252)...")
    X_train, y_train, X_val, y_val, val_meta, train_meta = build_dataset(
        target_col="target_5d", val_days=252
    )
    print(f"val={len(X_val)}행, positive_rate={y_val.mean():.3%}\n")

    proba_val = model.predict_proba(X_val.values)[:, 1]

    # val을 시간순으로 70:30 분리 — 보정함수는 앞쪽만 학습, 뒤쪽은 완전 held-out
    dates = val_meta["date"]
    unique_dates = sorted(dates.unique())
    cutoff = unique_dates[int(len(unique_dates) * 0.7)]
    fit_mask = (dates < cutoff).values
    test_mask = ~fit_mask
    print(f"calib_fit: {fit_mask.sum()}행 (~{unique_dates[0]}~{cutoff} 이전) / "
          f"calib_test(held-out): {test_mask.sum()}행 ({cutoff}~{unique_dates[-1]})\n")

    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(proba_val[fit_mask], y_val.values[fit_mask])

    raw_test = proba_val[test_mask]
    cal_test = iso.transform(raw_test)
    y_test = y_val.values[test_mask]
    dates_test = dates[test_mask]

    print("=" * 70)
    print("[보정 전] held-out 구간 calibration (10분위)")
    print("=" * 70)
    raw_table = decile_table(raw_test, y_test)
    print(raw_table.to_string(index=False))
    raw_gap_mean = raw_table["gap(예측-실제)"].abs().mean()
    print(f"\n평균 |gap|: {raw_gap_mean:.4f}\n")

    print("=" * 70)
    print("[보정 후] held-out 구간 calibration (10분위)")
    print("=" * 70)
    cal_table = decile_table(cal_test, y_test)
    print(cal_table.to_string(index=False))
    cal_gap_mean = cal_table["gap(예측-실제)"].abs().mean()
    print(f"\n평균 |gap|: {cal_gap_mean:.4f}\n")

    print(f"개선: {raw_gap_mean:.4f} -> {cal_gap_mean:.4f} "
          f"({'개선' if cal_gap_mean < raw_gap_mean else '악화'}, "
          f"{(raw_gap_mean - cal_gap_mean) / raw_gap_mean * 100:+.1f}%)\n")

    print("=" * 70)
    print("[순위 보존 검증] held-out 구간 날짜별 top10 종목 집합 일치율")
    print("=" * 70)
    overlap = topk_overlap(dates_test, raw_test, cal_test, k=10)
    print(f"top10 완전 일치 비율: {overlap:.4f} (1.0이면 단조변환대로 순위 100% 보존)\n")

    # 가장 확신하는 구간(최상위 decile) 전/후 비교 — 사용자가 언급한 "56.8% vs 38.8%" 재현
    top_decile_raw = raw_table.iloc[-1]
    top_decile_cal = cal_table.iloc[-1]
    print("=" * 70)
    print("[최상위 확신 구간 비교]")
    print("=" * 70)
    print(f"보정 전: 예측 {top_decile_raw['평균예측확률']:.3f} vs 실제 {top_decile_raw['실제적중률']:.3f} "
          f"(gap {top_decile_raw['gap(예측-실제)']:+.3f})")
    print(f"보정 후: 예측 {top_decile_cal['평균예측확률']:.3f} vs 실제 {top_decile_cal['실제적중률']:.3f} "
          f"(gap {top_decile_cal['gap(예측-실제)']:+.3f})")

    # 최종 calibrator는 val 전체로 재학습(데이터 최대 활용) — held-out 검증은 위에서 이미 끝냈으므로
    # fit_calibrator_with_holdout_check를 그대로 재사용(동일 분리 로직, train.py와 일치).
    final_calibrator, _, _ = fit_calibrator_with_holdout_check(proba_val, y_val.values, dates)
    calibrator_path = model_dir / "calibrator.pkl"
    import pickle
    with open(calibrator_path, "wb") as f:
        pickle.dump(final_calibrator, f)
    print(f"\n운영 calibrator 저장: {calibrator_path} (predictor.py가 다음 재시작부터 로드)")

    report_lines = [
        f"# 확률 calibration 분석 결과 (모델: {model_dir.name})",
        "",
        f"- val set 252거래일을 시간순 70:30 분리 — calib_fit {fit_mask.sum()}행 / "
        f"calib_test(held-out) {test_mask.sum()}행",
        f"- 보정 전 평균 |gap|: {raw_gap_mean:.4f}",
        f"- 보정 후 평균 |gap|: {cal_gap_mean:.4f} "
        f"({(raw_gap_mean - cal_gap_mean) / raw_gap_mean * 100:+.1f}%)",
        f"- top10 순위 완전 일치율(보정 전/후): {overlap:.4f}",
        "",
        "## 보정 전 (held-out)",
        raw_table.to_string(index=False),
        "",
        "## 보정 후 (held-out)",
        cal_table.to_string(index=False),
    ]
    OUT_REPORT.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"저장: {OUT_REPORT}")


if __name__ == "__main__":
    main()
