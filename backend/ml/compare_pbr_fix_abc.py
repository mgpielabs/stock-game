"""
[실험 전용 — 운영 모델/포인터 변경 없음] pbr look-ahead bias 수정 영향 측정 (2026-06-27)

배경: load_fundamentals_latest()가 date 필터 없이 "가장 최근 PER/PBR"을 모든 과거
날짜에 broadcast하던 버그를 확인(per는 SHAP 0%로 무해, pbr은 #3/9.7%로 모델 평가
전체를 오염시킬 수 있었음). features 테이블의 per/pbr은 이미 point-in-time으로
수정됐고(fix_per_pbr_pit.py), pipeline.py도 동일 로직(load_per_pbr_pit)을 쓰도록
바뀌었음. 이 스크립트는 그 수정의 영향을 3개 변형으로 측정한다.

  A = 기존(오염) per/pbr(룩어헤드) + dead feature 8개 포함  ── 현재까지 봤던 모든 숫자
  B = A에서 per/pbr만 point-in-time으로 교체 (dead feature는 그대로 포함)
  C = B + dead feature 8개 제거 (최종 제안 피처셋)

A→B 델타 = look-ahead bias가 부풀렸던 거품의 크기
B→C 델타 = dead feature 제거가 무해한지 확인

target_5d만 비교(유일하게 서빙 중인 모델, ACTIVE_target_5d.txt). LightGBM은 서빙
안 되는 모델이라(CLAUDE.md "앙상블 가중치 조정" 참고 — 모든 조합에서 최저성능) 제외하고
실제 서빙 블렌드(CatBoost*0.8+XGBoost*0.2)만 비교해 학습 비용을 줄임. KIS 백필(PID 1083)과
동시 실행되므로 IDLE 우선순위로 낮춰서 백필에 영향을 최소화함.

실행: python compare_pbr_fix_abc.py
결과: compare_pbr_fix_abc_results.csv + 콘솔 출력
"""

import sys
import time
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
from catboost import CatBoostClassifier, Pool
from xgboost import XGBClassifier
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
from dataset import build_dataset, FEATURE_COLS, FEATURE_COLS_REDUCED
from evaluate import precision_at_topk
from ensemble_config import CAT_BLEND_WEIGHT, XGB_BLEND_WEIGHT

TARGET_COL = "target_5d"
VAL_DAYS = 252

VARIANTS = {
    "A_baseline_legacy":      dict(feature_cols=FEATURE_COLS,         per_pbr_mode="legacy_broadcast"),
    "B_pit_fix_only":         dict(feature_cols=FEATURE_COLS,         per_pbr_mode="pit"),
    "C_pit_fix_plus_dead_rm": dict(feature_cols=FEATURE_COLS_REDUCED, per_pbr_mode="pit"),
}


def train_cat_xgb(X_train, y_train, X_val, y_val):
    spw = (y_train == 0).sum() / max((y_train == 1).sum(), 1)

    cat = CatBoostClassifier(
        iterations=2000, learning_rate=0.05, depth=6,
        scale_pos_weight=float(spw), eval_metric="AUC",
        early_stopping_rounds=50, random_seed=42, verbose=False,
    )
    cat.fit(X_train, y_train, eval_set=(X_val, y_val))
    cat_proba = cat.predict_proba(X_val)[:, 1]

    xgb = XGBClassifier(
        n_estimators=2000, learning_rate=0.05, max_depth=6,
        subsample=0.8, colsample_bytree=0.8, scale_pos_weight=float(spw),
        eval_metric="auc", early_stopping_rounds=50, random_state=42,
        verbosity=0, device="cpu",
    )
    xgb.fit(X_train, y_train, eval_set=[(X_val, y_val)], verbose=False)
    xgb_proba = xgb.predict_proba(X_val)[:, 1]

    return cat, cat_proba, xgb, xgb_proba


def main():
    results = []
    pbr_shap_rows = []

    for name, cfg in VARIANTS.items():
        print("=" * 70)
        print(f"[{name}] feature_cols={len(cfg['feature_cols'])}개, per_pbr_mode={cfg['per_pbr_mode']}")
        print("=" * 70)
        t0 = time.time()

        X_train, y_train, X_val, y_val, val_meta, _ = build_dataset(
            target_col=TARGET_COL, val_days=VAL_DAYS,
            feature_cols=cfg["feature_cols"], per_pbr_mode=cfg["per_pbr_mode"],
        )
        print(f"  train={len(X_train)} val={len(X_val)} positive={100*y_train.mean():.1f}%")

        cat, cat_proba, xgb, xgb_proba = train_cat_xgb(X_train, y_train, X_val, y_val)

        cat_auc = roc_auc_score(y_val, cat_proba)
        xgb_auc = roc_auc_score(y_val, xgb_proba)
        blend_proba = CAT_BLEND_WEIGHT * cat_proba + XGB_BLEND_WEIGHT * xgb_proba
        blend_auc = roc_auc_score(y_val, blend_proba)

        cat_topk = precision_at_topk(y_val, cat_proba, val_meta)
        blend_topk = precision_at_topk(y_val, blend_proba, val_meta)

        elapsed = time.time() - t0
        print(f"  CatBoost AUC={cat_auc:.4f} | Blend AUC={blend_auc:.4f}")
        print(f"  Blend P@10={blend_topk.get(10, float('nan')):.4f} "
              f"P@20={blend_topk.get(20, float('nan')):.4f} "
              f"P@30={blend_topk.get(30, float('nan')):.4f}  ({elapsed:.1f}s)")

        # pbr/per SHAP 순위 (CatBoost 네이티브, variant마다 feature_cols가 다름)
        feature_cols = list(X_val.columns)
        pool = Pool(X_val, y_val)
        shap_values = cat.get_feature_importance(pool, type="ShapValues")
        mean_abs_shap = np.abs(shap_values[:, :-1]).mean(axis=0)
        imp_df = pd.DataFrame({"feature": feature_cols, "mean_abs_shap": mean_abs_shap})
        imp_df["pct"] = imp_df["mean_abs_shap"] / imp_df["mean_abs_shap"].sum() * 100
        imp_df = imp_df.sort_values("mean_abs_shap", ascending=False).reset_index(drop=True)
        imp_df["rank"] = imp_df.index + 1

        for feat in ("pbr", "per"):
            row = imp_df[imp_df["feature"] == feat]
            if not row.empty:
                pbr_shap_rows.append({
                    "variant": name, "feature": feat,
                    "rank": int(row["rank"].iloc[0]),
                    "pct_of_total_shap": float(row["pct"].iloc[0]),
                })
                print(f"  {feat} SHAP 순위: #{int(row['rank'].iloc[0])} ({row['pct'].iloc[0]:.2f}%)")

        results.append({
            "variant": name,
            "n_features": len(cfg["feature_cols"]),
            "per_pbr_mode": cfg["per_pbr_mode"],
            "train_rows": len(X_train), "val_rows": len(X_val),
            "positive_rate": float(y_train.mean()),
            "cat_auc": cat_auc, "xgb_auc": xgb_auc, "blend_auc": blend_auc,
            "blend_p10": blend_topk.get(10, float("nan")),
            "blend_p20": blend_topk.get(20, float("nan")),
            "blend_p30": blend_topk.get(30, float("nan")),
            "elapsed_sec": elapsed,
        })

    res_df = pd.DataFrame(results)
    shap_df = pd.DataFrame(pbr_shap_rows)

    print("\n" + "=" * 70)
    print("=== 최종 비교 ===")
    print("=" * 70)
    print(res_df.to_string(index=False))
    print()
    print(shap_df.to_string(index=False))

    a = res_df[res_df["variant"] == "A_baseline_legacy"].iloc[0]
    b = res_df[res_df["variant"] == "B_pit_fix_only"].iloc[0]
    c = res_df[res_df["variant"] == "C_pit_fix_plus_dead_rm"].iloc[0]

    print()
    print(f"A→B (look-ahead bias 거품 크기): P@10 {a['blend_p10']:.4f} → {b['blend_p10']:.4f} "
          f"(델타 {b['blend_p10']-a['blend_p10']:+.4f}, {100*(b['blend_p10']-a['blend_p10'])/a['blend_p10']:+.1f}%)")
    print(f"B→C (dead feature 제거 영향):   P@10 {b['blend_p10']:.4f} → {c['blend_p10']:.4f} "
          f"(델타 {c['blend_p10']-b['blend_p10']:+.4f}, {100*(c['blend_p10']-b['blend_p10'])/b['blend_p10']:+.1f}%)")

    out_dir = Path(__file__).parent
    res_df.to_csv(out_dir / "compare_pbr_fix_abc_results.csv", index=False)
    shap_df.to_csv(out_dir / "compare_pbr_fix_abc_shap.csv", index=False)
    print(f"\n저장: compare_pbr_fix_abc_results.csv, compare_pbr_fix_abc_shap.csv")


if __name__ == "__main__":
    main()
