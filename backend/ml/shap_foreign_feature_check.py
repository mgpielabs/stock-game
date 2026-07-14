"""
[검증 전용 — 운영 모델/DB 변경 없음] 현재 서빙 중인 CatBoost 모델에서 외국인 수급
피처(foreign_rate/foreign_1d_chg/foreign_5d_chg/foreign_trend)가 실제로 얼마나
기여하는지 SHAP로 점검 (2026-06-27).

배경: investor_trading_kis(새 KIS 수급 데이터)는 30거래일치뿐이라(전체 학습기간
2022~2026의 약 0.03%) 새 피처를 추가해 재학습하는 건 결과가 사실상 정해진 실험임 —
재학습 없이, 이미 같은 문제(커버리지 2.5%, 2025-05-07~)를 가진 기존 foreign_rate
계열 피처가 지금 모델에서 실제로 얼마나 기여하는지만 확인.

운영 모델(models/ACTIVE_target_5d.txt가 가리키는 디렉터리)의 model_cat.pkl을 그대로
로드해서 validation set(build_dataset 마지막 252거래일)에 대해 CatBoost 네이티브
SHAP(get_feature_importance(type=ShapValues))을 계산. 모델 재학습/저장 없음, SELECT만.

실행: python shap_foreign_feature_check.py
"""

import sys
import pickle
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from catboost import Pool

sys.path.insert(0, str(Path(__file__).parent))
from dataset import build_dataset

ROOT = Path(__file__).parent.parent
MODELS_DIR = ROOT / "models"


def load_active_model(target_col: str):
    pointer = MODELS_DIR / f"ACTIVE_{target_col}.txt"
    model_dir = MODELS_DIR / pointer.read_text(encoding="utf-8").strip()
    with open(model_dir / "model_cat.pkl", "rb") as f:
        model = pickle.load(f)
    return model, model_dir


def main():
    target_col = "target_5d"
    model, model_dir = load_active_model(target_col)
    print(f"운영 모델: {model_dir.name}")

    print("검증셋 로드 중 (build_dataset, val_days=252)...")
    X_train, y_train, X_val, y_val, val_meta, train_meta = build_dataset(target_col=target_col, val_days=252)
    print(f"X_val: {X_val.shape}, 기간: {val_meta['date'].min()} ~ {val_meta['date'].max()}")

    feature_cols = list(X_val.columns)
    foreign_cols = ["foreign_rate", "foreign_1d_chg", "foreign_5d_chg", "foreign_trend"]
    print(f"\n외국인 피처 NULL 비율 (검증셋 {len(X_val)}행 기준):")
    for c in foreign_cols:
        if c in X_val.columns:
            null_pct = X_val[c].isna().mean() * 100
            print(f"  {c}: NULL {null_pct:.1f}%")

    print("\nSHAP 계산 중 (CatBoost 네이티브, 검증셋 전체)...")
    pool = Pool(X_val, y_val)
    shap_values = model.get_feature_importance(pool, type="ShapValues")
    # 마지막 컬럼은 base value — 제외
    shap_matrix = shap_values[:, :-1]
    mean_abs_shap = np.abs(shap_matrix).mean(axis=0)

    importance_df = pd.DataFrame({
        "feature": feature_cols,
        "mean_abs_shap": mean_abs_shap,
    }).sort_values("mean_abs_shap", ascending=False).reset_index(drop=True)
    importance_df["rank"] = importance_df.index + 1
    importance_df["pct_of_total"] = importance_df["mean_abs_shap"] / importance_df["mean_abs_shap"].sum() * 100

    print(f"\n=== 전체 {len(feature_cols)}개 피처 중 SHAP 중요도 순위 ===")
    print(importance_df.to_string(index=False))

    print("\n=== 외국인 수급 피처 4종 순위 ===")
    sub = importance_df[importance_df["feature"].isin(foreign_cols)]
    print(sub.to_string(index=False))
    print(f"\n외국인 피처 4종 합산 기여도: {sub['pct_of_total'].sum():.3f}% (전체 SHAP 중)")

    out = Path(__file__).parent / "shap_foreign_feature_check_results.csv"
    importance_df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n결과 저장: {out}")


if __name__ == "__main__":
    main()
