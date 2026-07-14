"""
[진단 전용 — 모델/코드 수정 없음] 운영 모델(target_5d) 약점 진단 1단계

현재 서빙 중인 모델(models/target_5d_*/, find_latest_model_dir과 동일 로직으로 탐색)을
그대로 불러와 다음을 측정한다. 모델 파일을 새로 저장하지 않고, train.py/predictor.py
등 운영 코드도 건드리지 않는다.

  1. 피처 중요도(CatBoost) + 피처 간 상관관계(다중공선성)
  2. train AUC vs val AUC 격차 (과적합 징후), 모델별/앙상블
  3. Calibration: 예측확률 10분위별 실제 적중률
  4. 오분류 패턴: 변동성/시장국면(KOSPI MA200 비율) 구간별 top10 정밀도
  5. (참고로 이미 알려진 사실은 재계산 안 하고 텍스트로만 인용:
     - tune_catboost.py: CatBoost Optuna 50trials, val AUC 0.6333→0.6373 (+0.40pp),
       단 운영 train.py에는 미반영(CatBoost는 여전히 기본 파라미터)
     - train_2022.py: 2022년 데이터 포함 시 val AUC -0.51pp (노이즈로 판정, FAIL)
     - meta.json: 현재 모델 precision@10 — CatBoost단독 43.5% > 앙상블 42.7% > XGB 41.7% > LGBM 41.0%
       (동일가중 평균이 최고 단일모델보다 못함 — 가중치 재검토 후보)
     )

실행: python diagnose_model.py
"""

import pickle
import sys
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000040)
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
from dataset import FEATURE_COLS, build_dataset
from evaluate import precision_at_topk

MODELS_DIR = Path(__file__).parent.parent / "models"
pd.set_option("display.width", 140)


def find_latest_model_dir(target_col: str = "target_5d") -> Path:
    candidates = sorted(
        MODELS_DIR.glob(f"{target_col}_*/model_cat.pkl"),
        key=lambda p: p.parent.stat().st_mtime,
    )
    return candidates[-1].parent


def load_models(model_dir: Path):
    models = {}
    for name, fname in [("cat", "model_cat.pkl"), ("xgb", "model_xgb.pkl"), ("lgbm", "model_lgbm.pkl")]:
        with open(model_dir / fname, "rb") as f:
            models[name] = pickle.load(f)
    return models


def section_feature_importance(models, X_train: pd.DataFrame):
    print("=" * 70); print("[1] 피처 중요도 (CatBoost) + 다중공선성"); print("=" * 70)
    cat = models["cat"]
    imp = pd.Series(cat.get_feature_importance(), index=FEATURE_COLS).sort_values(ascending=False)
    print(f"피처 수: {len(FEATURE_COLS)}개\n")
    print("상위 15개:")
    print(imp.head(15).to_string())
    print("\n하위 10개 (제거 후보 — 거의 기여 없음):")
    print(imp.tail(10).to_string())
    near_zero = imp[imp < imp.sum() * 0.001]
    print(f"\n중요도 0.1% 미만 피처 수: {len(near_zero)}개 — {list(near_zero.index)}")

    print("\n다중공선성 (|상관계수| >= 0.9인 피처쌍):")
    corr = X_train[FEATURE_COLS].corr()
    pairs = []
    cols = corr.columns
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            c = corr.iloc[i, j]
            if abs(c) >= 0.9:
                pairs.append((cols[i], cols[j], round(float(c), 3)))
    pairs.sort(key=lambda x: -abs(x[2]))
    for a, b, c in pairs:
        print(f"  {a:22s} <-> {b:22s}  r={c}")
    print(f"  총 {len(pairs)}쌍")
    print()
    return imp, pairs


def section_overfit_gap(models, X_train, y_train, X_val, y_val):
    print("=" * 70); print("[2] Train vs Val AUC 격차 (과적합 진단)"); print("=" * 70)
    rows = []
    train_probas = {}
    val_probas = {}
    for name, mdl in models.items():
        tr_p = mdl.predict_proba(X_train)[:, 1]
        va_p = mdl.predict_proba(X_val)[:, 1]
        train_probas[name], val_probas[name] = tr_p, va_p
        tr_auc = roc_auc_score(y_train, tr_p)
        va_auc = roc_auc_score(y_val, va_p)
        rows.append({"모델": name, "train_AUC": round(tr_auc, 4), "val_AUC": round(va_auc, 4),
                     "격차(train-val)": round(tr_auc - va_auc, 4)})
    ens_tr = sum(train_probas.values()) / 3
    ens_va = sum(val_probas.values()) / 3
    rows.append({"모델": "ensemble(동일가중)", "train_AUC": round(roc_auc_score(y_train, ens_tr), 4),
                 "val_AUC": round(roc_auc_score(y_val, ens_va), 4),
                 "격차(train-val)": round(roc_auc_score(y_train, ens_tr) - roc_auc_score(y_val, ens_va), 4)})
    print(pd.DataFrame(rows).to_string(index=False))
    print()
    return val_probas


def section_calibration(val_probas, y_val, val_meta):
    print("=" * 70); print("[3] Calibration — 예측확률 10분위별 실제 적중률 (앙상블)"); print("=" * 70)
    ens = sum(val_probas.values()) / 3
    df = pd.DataFrame({"proba": ens, "y": y_val.values})
    df["decile"] = pd.qcut(df["proba"], 10, labels=False, duplicates="drop")
    g = df.groupby("decile").agg(
        n=("y", "size"), 평균예측확률=("proba", "mean"), 실제적중률=("y", "mean")
    ).reset_index()
    g["calibration_gap(예측-실제)"] = (g["평균예측확률"] - g["실제적중률"]).round(4)
    print(g.round(4).to_string(index=False))
    print()
    return ens


def section_misclassification(ens_proba, y_val, val_meta, conn_features: pd.DataFrame):
    print("=" * 70); print("[4] 오분류 패턴 — 시장국면/변동성 구간별 Top10 정밀도"); print("=" * 70)
    df = val_meta[["symbol", "date"]].copy()
    df["y_proba"] = ens_proba
    df["y_true"] = y_val.values
    df = df.merge(
        conn_features[["symbol", "date", "volatility_20", "kospi_ma200_ratio", "rsi_14"]],
        on=["symbol", "date"], how="left",
    )

    # 시장국면: kospi_ma200_ratio > 1 = 강세(지수가 200일선 위), <=1 = 약세
    df["regime"] = np.where(df["kospi_ma200_ratio"] > 1.0, "강세(지수>MA200)", "약세(지수<=MA200)")
    rows = []
    for regime, grp in df.groupby("regime"):
        p10 = []
        for _, day in grp.groupby("date"):
            top = day.sort_values("y_proba", ascending=False).head(10)
            if len(top):
                p10.append(top["y_true"].mean())
        rows.append({"구간": regime, "일수": grp["date"].nunique(), "전체n": len(grp),
                     "positive_rate": round(grp["y_true"].mean(), 4),
                     "top10_precision": round(float(np.mean(p10)), 4) if p10 else None})
    print(pd.DataFrame(rows).to_string(index=False))
    print()

    # 변동성 구간 (volatility_20 4분위)
    df["vol_q"] = pd.qcut(df["volatility_20"].fillna(df["volatility_20"].median()), 4,
                           labels=["Q1(낮음)", "Q2", "Q3", "Q4(높음)"])
    rows2 = []
    for vq, grp in df.groupby("vol_q"):
        p10 = []
        for _, day in grp.groupby("date"):
            top = day.sort_values("y_proba", ascending=False).head(10)
            if len(top):
                p10.append(top["y_true"].mean())
        rows2.append({"변동성구간": str(vq), "전체n": len(grp),
                      "positive_rate": round(grp["y_true"].mean(), 4),
                      "top10_precision": round(float(np.mean(p10)), 4) if p10 else None})
    print(pd.DataFrame(rows2).to_string(index=False))
    print()

    # FP/FN 균형: top10으로 뽑힌 것 중 실패(FP) 비율, 그리고 실제 양성인데 top10에 못 든 비율(놓침)
    selected = df.copy()
    selected["selected_top10"] = False
    for date, grp in selected.groupby("date"):
        idx = grp.sort_values("y_proba", ascending=False).head(10).index
        selected.loc[idx, "selected_top10"] = True
    tp = ((selected["selected_top10"]) & (selected["y_true"] == 1)).sum()
    fp = ((selected["selected_top10"]) & (selected["y_true"] == 0)).sum()
    total_pos = (selected["y_true"] == 1).sum()
    fn_missed = total_pos - tp
    print(f"Top10 선택 기준: TP={tp}, FP={fp} (선택했지만 실패), "
          f"전체 양성 중 미선택(FN)={fn_missed}/{total_pos} ({fn_missed/total_pos:.1%})")
    print(f"-> Top10 안에서는 FP가 압도적으로 많음(선택 자체가 K=10으로 극히 제한되니 당연) — "
          f"진짜 문제는 '놓친 기회'가 아니라 '선택한 것의 적중률'이므로 calibration/중요도 쪽이 더 중요한 레버")
    print()


def main():
    model_dir = find_latest_model_dir()
    print(f"진단 대상 모델: {model_dir.name}\n")

    print("데이터셋 빌드 중 (target_5d, val_days=252)...")
    X_train, y_train, X_val, y_val, val_meta, train_meta = build_dataset(
        target_col="target_5d", val_days=252
    )
    print(f"train={len(X_train)}행 / val={len(X_val)}행 / "
          f"positive_rate(train)={y_train.mean():.3%} / positive_rate(val)={y_val.mean():.3%}\n")

    models = load_models(model_dir)

    section_feature_importance(models, X_train)
    val_probas = section_overfit_gap(models, X_train, y_train, X_val, y_val)
    ens_proba = section_calibration(val_probas, y_val, val_meta)

    import sqlite3
    from dataset import DB_PATH
    with sqlite3.connect(DB_PATH) as conn:
        feat_ctx = pd.read_sql_query(
            "SELECT symbol, date, volatility_20, kospi_ma200_ratio, rsi_14 FROM features", conn
        )
    section_misclassification(ens_proba, y_val, val_meta, feat_ctx)

    print("=" * 70); print("[참고] 이미 다른 실험 스크립트에서 확인된 사실 (재계산 안 함)"); print("=" * 70)
    print("- tune_catboost.py: CatBoost Optuna 50trials -> val AUC 0.6333→0.6373(+0.40pp), "
          "P@10 43.1%→43.5%. 운영 train.py의 CatBoost는 여전히 기본 파라미터(미반영).")
    print("- train_2022.py: 2022년 데이터를 학습에 포함하면 val AUC -0.51pp (FAIL, 노이즈 추가로 판정).")
    print("- train_regime.py 최신 결과: bear 전용 모델 AUC 0.656(통합모델 0.633보다 높음)이지만 "
          "top10 정밀도는 38.2%로 통합모델(43.1%)보다 낮음 — AUC 개선이 실제 선택 정확도로 안 이어짐.")


if __name__ == "__main__":
    main()
