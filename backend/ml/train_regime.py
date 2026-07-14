"""
시장 국면별 CatBoost 분리 학습

KOSPI 60d MA > 120d MA → bull 국면
KOSPI 60d MA ≤ 120d MA → bear 국면

각 국면에 맞는 데이터만 선택해 CatBoost를 개별 학습한 뒤,
통합 모델(AUC 0.6333)과 성능을 비교합니다.
저장: backend/models/target_5d_regime_YYYYMMDD_HHMMSS/
"""

import json
import logging
import pickle
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple

import catboost as cb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "features"))

from dataset import FEATURE_COLS, FEATURE_COLS_REDUCED, DB_PATH, build_dataset
from evaluate import precision_at_topk
from market_regime import load_kospi_regime_labels

MODELS_DIR = Path(__file__).parent.parent / "models"
UNIFIED_AUC   = 0.6333   # 통합 CatBoost 기준선 (비교용)
UNIFIED_P10   = 0.431
UNIFIED_P20   = 0.417
UNIFIED_P30   = 0.408
WARN_MIN_ROWS = 50_000

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("train_regime.log", encoding="utf-8"),
    ],
)


def _train_catboost(
    X_train: pd.DataFrame, y_train: pd.Series,
    X_val: pd.DataFrame,   y_val: pd.Series,
    spw: float, label: str,
) -> Tuple[cb.CatBoostClassifier, np.ndarray, int, float]:
    t0 = time.time()
    logger.info("  [%s] train=%d  val=%d  spw=%.2f", label, len(X_train), len(X_val), spw)
    model = cb.CatBoostClassifier(
        iterations=2000,
        learning_rate=0.05,
        depth=6,
        scale_pos_weight=float(spw),
        eval_metric="AUC",
        early_stopping_rounds=50,
        random_seed=42,
        verbose=False,
    )
    model.fit(X_train, y_train, eval_set=(X_val, y_val))
    proba    = model.predict_proba(X_val)[:, 1]
    best_iter = int(model.best_iteration_)
    return model, proba, best_iter, time.time() - t0


def _regime_split(
    X_all: pd.DataFrame,
    y_all: pd.Series,
    meta_all: pd.DataFrame,
    regime: pd.Series,
    regime_name: str,
    val_ratio: float = 0.20,
):
    """
    특정 국면(regime_name)에 속하는 데이터만 추려서
    날짜 순 앞 80% → train, 뒤 20% → val 로 분리.
    """
    mask = meta_all["date"].map(regime).fillna("bull") == regime_name
    X_r    = X_all[mask.values].reset_index(drop=True)
    y_r    = y_all[mask.values].reset_index(drop=True)
    meta_r = meta_all[mask.values].reset_index(drop=True)

    dates  = sorted(meta_r["date"].unique())
    cut    = max(1, int(len(dates) * (1 - val_ratio)))
    cutoff = dates[cut]

    tr_m = meta_r["date"] < cutoff
    va_m = meta_r["date"] >= cutoff

    return (
        X_r[tr_m.values], y_r[tr_m.values],
        X_r[va_m.values], y_r[va_m.values],
        meta_r[va_m.values].reset_index(drop=True),
        len(dates),
    )


def train_regime(val_days: int = 252) -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("국면별 CatBoost 분리 학습 | target=target_5d")
    logger.info("  val 분리 방식: 각 국면 내 마지막 20%% 날짜")
    logger.info("=" * 60)

    # ── 1. 전체 데이터셋 로드 (train+val 합산) ──────────────────
    logger.info("[1/4] 데이터셋 로드...")
    X_train, y_train, X_val, y_val, val_meta, train_meta = build_dataset(
        target_col="target_5d", val_days=val_days,
        feature_cols=FEATURE_COLS_REDUCED,
    )

    # train+val 합산 (국면 내부에서 재분리하기 위해)
    X_all   = pd.concat([X_train, X_val],    ignore_index=True)
    y_all   = pd.concat([y_train, y_val],    ignore_index=True)
    meta_all = pd.concat([train_meta[["symbol","date"]],
                          val_meta[["symbol","date"]]], ignore_index=True)
    logger.info("  전체 데이터: %d행", len(X_all))

    # ── 2. 국면 라벨링 ───────────────────────────────────────────
    logger.info("[2/4] KOSPI 60d/120d MA 국면 계산...")
    regime = load_kospi_regime_labels()

    all_regime_col = meta_all["date"].map(regime).fillna("bull")
    bull_total = (all_regime_col == "bull").sum()
    bear_total = (all_regime_col == "bear").sum()
    logger.info(
        "  전체 bull=%d (%.1f%%)  bear=%d (%.1f%%)",
        bull_total, 100 * bull_total / len(X_all),
        bear_total, 100 * bear_total / len(X_all),
    )

    for name, n in [("bull", bull_total), ("bear", bear_total)]:
        if n < WARN_MIN_ROWS:
            logger.warning(
                "⚠️  [%s] 샘플 %d행 — %d 미만, 학습이 불안정할 수 있습니다.",
                name, n, WARN_MIN_ROWS,
            )

    # ── 3. 국면 내부 분리 후 학습 ────────────────────────────────
    logger.info("[3/4] Bull 국면 CatBoost 학습...")
    X_tr_bull, y_tr_bull, X_va_bull, y_va_bull, bull_val_meta, bull_days = _regime_split(
        X_all, y_all, meta_all, regime, "bull"
    )
    spw_bull = (y_tr_bull == 0).sum() / max((y_tr_bull == 1).sum(), 1)
    bull_model, bull_proba, bull_iter, bull_time = _train_catboost(
        X_tr_bull, y_tr_bull, X_va_bull, y_va_bull, spw_bull, "bull"
    )
    bull_auc  = roc_auc_score(y_va_bull, bull_proba) if len(np.unique(y_va_bull)) == 2 else float("nan")
    bull_topk = precision_at_topk(y_va_bull, bull_proba, bull_val_meta)

    logger.info("[3/4] Bear 국면 CatBoost 학습...")
    X_tr_bear, y_tr_bear, X_va_bear, y_va_bear, bear_val_meta, bear_days = _regime_split(
        X_all, y_all, meta_all, regime, "bear"
    )
    spw_bear = (y_tr_bear == 0).sum() / max((y_tr_bear == 1).sum(), 1)
    bear_model, bear_proba, bear_iter, bear_time = _train_catboost(
        X_tr_bear, y_tr_bear, X_va_bear, y_va_bear, spw_bear, "bear"
    )
    bear_auc  = roc_auc_score(y_va_bear, bear_proba) if len(np.unique(y_va_bear)) == 2 else float("nan")
    bear_topk = precision_at_topk(y_va_bear, bear_proba, bear_val_meta)

    # ── 결과 비교표 ──────────────────────────────────────────────
    logger.info("")
    logger.info("┌─────────────┬──────────┬──────────┬──────────────┐")
    logger.info("│ 지표        │   Bull   │   Bear   │ 통합(기준선) │")
    logger.info("├─────────────┼──────────┼──────────┼──────────────┤")
    logger.info("│ Val AUC     │  %.4f  │  %.4f  │    %.4f    │",
                bull_auc, bear_auc, UNIFIED_AUC)
    logger.info("│ Precision@10│  %.4f  │  %.4f  │    %.4f    │",
                bull_topk.get(10, 0), bear_topk.get(10, 0), UNIFIED_P10)
    logger.info("│ Precision@20│  %.4f  │  %.4f  │    %.4f    │",
                bull_topk.get(20, 0), bear_topk.get(20, 0), UNIFIED_P20)
    logger.info("│ Precision@30│  %.4f  │  %.4f  │    %.4f    │",
                bull_topk.get(30, 0), bear_topk.get(30, 0), UNIFIED_P30)
    logger.info("│ 학습시간(s) │  %6.1f  │  %6.1f  │      -       │",
                bull_time, bear_time)
    logger.info("│ 최적 iter   │  %6d  │  %6d  │      -       │",
                bull_iter, bear_iter)
    logger.info("│ train 샘플  │  %6d  │  %6d  │      -       │",
                len(X_tr_bull), len(X_tr_bear))
    logger.info("│ val 샘플    │  %6d  │  %6d  │      -       │",
                len(X_va_bull), len(X_va_bear))
    logger.info("│ 국면 일수   │  %6d  │  %6d  │      -       │",
                bull_days, bear_days)
    logger.info("└─────────────┴──────────┴──────────┴──────────────┘")

    # ── 4. 저장 ─────────────────────────────────────────────────
    logger.info("[4/4] 모델 저장...")
    version  = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = MODELS_DIR / f"target_5d_regime_{version}"
    save_dir.mkdir(parents=True)

    with open(save_dir / "model_cat_bull.pkl", "wb") as f:
        pickle.dump(bull_model, f)
    with open(save_dir / "model_cat_bear.pkl", "wb") as f:
        pickle.dump(bear_model, f)

    meta: Dict = {
        "version":     version,
        "target_col":  "target_5d",
        "regime_rule": "bull: kospi_ma60 > kospi_ma120 | bear: ≤",
        "val_split":   "regime 내부 마지막 20% 날짜",
        "val_auc": {
            "bull":    bull_auc,
            "bear":    bear_auc,
            "unified": UNIFIED_AUC,
        },
        "precision_at_topk": {
            "bull":    {str(k): v for k, v in bull_topk.items()},
            "bear":    {str(k): v for k, v in bear_topk.items()},
            "unified": {"10": UNIFIED_P10, "20": UNIFIED_P20, "30": UNIFIED_P30},
        },
        "train_size":  {"bull": len(X_tr_bull), "bear": len(X_tr_bear)},
        "val_size":    {"bull": len(X_va_bull),  "bear": len(X_va_bear)},
        "regime_days": {"bull": bull_days,        "bear": bear_days},
        "best_iter":   {"bull": bull_iter,        "bear": bear_iter},
        "train_time_sec": {"bull": bull_time,     "bear": bear_time},
        "scale_pos_weight": {"bull": float(spw_bull), "bear": float(spw_bear)},
        "feature_cols": FEATURE_COLS_REDUCED,
    }
    (save_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    logger.info("  model_cat_bull.pkl → %s", save_dir / "model_cat_bull.pkl")
    logger.info("  model_cat_bear.pkl → %s", save_dir / "model_cat_bear.pkl")
    logger.info("  meta.json          → %s", save_dir / "meta.json")
    logger.info("=" * 60)
    logger.info("완료 | 저장: %s", save_dir)
    logger.info("=" * 60)
    return save_dir


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="국면별(bull/bear) CatBoost 분리 학습")
    parser.add_argument("--val-days", type=int, default=252, help="검증 기간 거래일 수")
    args = parser.parse_args()
    train_regime(val_days=args.val_days)
