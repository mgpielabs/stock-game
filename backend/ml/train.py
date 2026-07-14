"""
LightGBM + XGBoost + CatBoost 앙상블 학습

흐름:
  1. build_dataset() → X_train / X_val / val_meta
  2. Optuna: LightGBM 하이퍼파라미터 튜닝 (날짜 기준 CV)
  3. 세 모델 모두 동일 train/val로 학습
       - LightGBM: Optuna 최적 파라미터 + early stopping
       - XGBoost:  기본값 + early stopping
       - CatBoost: 기본값 + early stopping
  4. Soft voting 앙상블: final_prob = (lgbm + xgb + cat) / 3
  5. 모델·메타·그래프를 models/{target}_{timestamp}/ 에 저장
"""

import argparse
import json
import logging
import pickle
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

import catboost as cb
import lightgbm as lgb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import optuna
import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score

from calibration_utils import fit_calibrator_with_holdout_check
from dataset import FEATURE_COLS, FEATURE_COLS_REDUCED, build_dataset
from ensemble_config import CAT_BLEND_WEIGHT, XGB_BLEND_WEIGHT
from evaluate import compute_metrics, precision_at_topk, run_backtest, run_backtest_5d

MODELS_DIR = Path(__file__).parent.parent / "models"

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("train.log", encoding="utf-8"),
    ],
)


# ── 날짜 기준 CV (같은 날짜는 항상 같은 파티션) ────────────────

def _date_cv_splits(
    X: pd.DataFrame,
    n_splits: int = 5,
) -> Iterator[Tuple[np.ndarray, np.ndarray]]:
    """
    X의 정렬 순서(date 기준)를 이용한 확장 윈도우 CV.
    같은 날짜의 모든 행이 train 또는 val 한 쪽에만 속함.
    """
    n = len(X)
    fold_size = n // (2 * n_splits)
    base      = n // 2

    for i in range(1, n_splits + 1):
        tr_end = base + fold_size * (i - 1)
        va_end = base + fold_size * i
        if va_end > n:
            va_end = n
        tr_idx = np.arange(0, tr_end)
        va_idx = np.arange(tr_end, va_end)
        if len(tr_idx) > 200 and len(va_idx) > 200:
            yield tr_idx, va_idx


# ── Optuna 목적함수 (LightGBM 튜닝) ───────────────────────────

def _lgbm_objective(
    trial: optuna.Trial,
    X_train: pd.DataFrame,
    y_train: pd.Series,
) -> float:
    params = {
        "objective":         "binary",
        "metric":            "auc",
        "verbosity":         -1,
        "n_estimators":      100,  # Optuna 탐색용 (최종 모델은 2000 유지)
        "num_leaves":        trial.suggest_int("num_leaves", 20, 200),
        "max_depth":         trial.suggest_int("max_depth", 3, 12),
        "learning_rate":     trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
        "min_child_samples": trial.suggest_int("min_child_samples", 5, 100),
        "subsample":         trial.suggest_float("subsample", 0.5, 1.0),
        "colsample_bytree":  trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "reg_alpha":         trial.suggest_float("reg_alpha", 1e-8, 10.0, log=True),
        "reg_lambda":        trial.suggest_float("reg_lambda", 1e-8, 10.0, log=True),
    }

    aucs = []
    for fold_i, (tr_idx, va_idx) in enumerate(_date_cv_splits(X_train, n_splits=1)):
        X_t, X_v = X_train.iloc[tr_idx], X_train.iloc[va_idx]
        y_t, y_v = y_train.iloc[tr_idx], y_train.iloc[va_idx]

        spw = (y_t == 0).sum() / max((y_t == 1).sum(), 1)
        mdl = lgb.LGBMClassifier(**params, scale_pos_weight=float(spw), random_state=42)
        mdl.fit(
            X_t, y_t,
            eval_set=[(X_v, y_v)],
            callbacks=[lgb.early_stopping(20, verbose=False), lgb.log_evaluation(-1)],
        )
        proba = mdl.predict_proba(X_v)[:, 1]
        if len(np.unique(y_v)) == 2:
            aucs.append(roc_auc_score(y_v, proba))
        trial.report(float(np.mean(aucs)), step=fold_i)
        if trial.should_prune():
            raise optuna.TrialPruned()

    return float(np.mean(aucs)) if aucs else 0.0


# ── 개별 모델 학습 함수 ─────────────────────────────────────────

def _train_lgbm(
    X_train: pd.DataFrame, y_train: pd.Series,
    X_val: pd.DataFrame,   y_val: pd.Series,
    best_params: Dict, spw: float,
) -> Tuple[lgb.LGBMClassifier, np.ndarray, int, float]:
    t0 = time.time()
    final_params = {
        "objective":        "binary",
        "metric":           "auc",
        "verbosity":        -1,
        "n_estimators":     2000,
        "scale_pos_weight": float(spw),
        "random_state":     42,
        **best_params,
    }
    model = lgb.LGBMClassifier(**final_params)
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(80, verbose=False), lgb.log_evaluation(-1)],
    )
    proba = model.predict_proba(X_val)[:, 1]
    best_iter = model.best_iteration_ or final_params["n_estimators"]
    return model, proba, int(best_iter), time.time() - t0


def _train_xgboost(
    X_train: pd.DataFrame, y_train: pd.Series,
    X_val: pd.DataFrame,   y_val: pd.Series,
    spw: float,
) -> Tuple[xgb.XGBClassifier, np.ndarray, int, float]:
    t0 = time.time()
    model = xgb.XGBClassifier(
        n_estimators=2000,
        learning_rate=0.05,
        max_depth=6,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=float(spw),
        eval_metric="auc",
        early_stopping_rounds=50,
        random_state=42,
        verbosity=0,
        device="cpu",
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        verbose=False,
    )
    proba = model.predict_proba(X_val)[:, 1]
    best_iter = int(model.best_iteration) if model.best_iteration is not None else 2000
    return model, proba, best_iter, time.time() - t0


# 참고: backend/ml/tune_catboost.py의 Optuna 튜닝 파라미터를 운영에 반영 시도했으나
# (2026-06-21) val AUC는 개선(0.640→0.645)됐지만 실제 운영 지표인 precision@10이
# 오히려 악화(43.5%→41.7%)되어 롤백함. AUC 최적화가 top-K 선택 정확도와 다른 방향일 수
# 있다는 사례 — CLAUDE.md "신호 연구/모델 진단" 참고. 기본 파라미터 유지.
def _train_catboost(
    X_train: pd.DataFrame, y_train: pd.Series,
    X_val: pd.DataFrame,   y_val: pd.Series,
    spw: float,
) -> Tuple[cb.CatBoostClassifier, np.ndarray, int, float]:
    t0 = time.time()
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
    model.fit(
        X_train, y_train,
        eval_set=(X_val, y_val),
    )
    proba = model.predict_proba(X_val)[:, 1]
    best_iter = int(model.best_iteration_)
    return model, proba, best_iter, time.time() - t0


# ── 피처 중요도 그래프 ─────────────────────────────────────────

def _plot_importance(model: lgb.LGBMClassifier, feature_cols: List[str], save_path: Path) -> None:
    imp = (
        pd.DataFrame({"feature": feature_cols, "importance": model.feature_importances_})
        .sort_values("importance", ascending=True)
        .tail(30)
    )
    fig, ax = plt.subplots(figsize=(10, 10))
    ax.barh(imp["feature"], imp["importance"], color="steelblue")
    ax.set_xlabel("Feature Importance (split)")
    ax.set_title("Top 30 Feature Importances (LightGBM)")
    plt.tight_layout()
    fig.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ── 메인 학습 파이프라인 ───────────────────────────────────────

def train(
    target_col: str = "target_1d",
    n_trials: int = 50,
    val_days: int = 252,
    promote: bool = True,
) -> Path:
    """
    LightGBM + XGBoost + CatBoost 앙상블 학습 파이프라인.
    반환: 저장된 모델 디렉토리 경로.
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("앙상블 학습 시작 | target=%s | trials=%d | val_days=%d",
                target_col, n_trials, val_days)
    logger.info("=" * 60)

    # ── 1. 데이터셋 ──
    logger.info("[1/5] 데이터셋 로드...")
    X_train, y_train, X_val, y_val, val_meta, _train_meta = build_dataset(
        target_col=target_col, val_days=val_days,
        feature_cols=FEATURE_COLS_REDUCED,
    )
    spw = (y_train == 0).sum() / max((y_train == 1).sum(), 1)
    logger.info("  train=%d  val=%d  positive=%.1f%%  scale_pos_weight=%.2f",
                len(X_train), len(X_val), 100 * y_train.mean(), spw)

    # ── 2. LightGBM: Optuna 튜닝 ──
    logger.info("[2/5] LightGBM Optuna 튜닝 (%d trials)...", n_trials)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(
        direction="maximize",
        pruner=optuna.pruners.MedianPruner(n_startup_trials=10, n_warmup_steps=0),
    )
    study.optimize(
        lambda trial: _lgbm_objective(trial, X_train, y_train),
        n_trials=n_trials,
        show_progress_bar=True,
    )
    best_lgbm_params = study.best_params
    logger.info("  best_auc_cv=%.4f | params=%s", study.best_value, best_lgbm_params)

    # ── 3. 세 모델 학습 ──
    logger.info("[3/5] LightGBM 최종 학습...")
    lgbm_model, lgbm_proba, lgbm_iter, lgbm_time = _train_lgbm(
        X_train, y_train, X_val, y_val, best_lgbm_params, spw
    )
    lgbm_auc  = roc_auc_score(y_val, lgbm_proba)
    lgbm_topk = precision_at_topk(y_val, lgbm_proba, val_meta)
    logger.info("  LightGBM | AUC=%.4f | P@10=%.4f | P@30=%.4f | iter=%d | %.1fs",
                lgbm_auc, lgbm_topk.get(10, float("nan")),
                lgbm_topk.get(30, float("nan")), lgbm_iter, lgbm_time)

    logger.info("[3/5] XGBoost 학습...")
    xgb_model, xgb_proba, xgb_iter, xgb_time = _train_xgboost(
        X_train, y_train, X_val, y_val, spw
    )
    xgb_auc  = roc_auc_score(y_val, xgb_proba)
    xgb_topk = precision_at_topk(y_val, xgb_proba, val_meta)
    logger.info("  XGBoost  | AUC=%.4f | P@10=%.4f | P@30=%.4f | iter=%d | %.1fs",
                xgb_auc, xgb_topk.get(10, float("nan")),
                xgb_topk.get(30, float("nan")), xgb_iter, xgb_time)

    logger.info("[3/5] CatBoost 학습...")
    cat_model, cat_proba, cat_iter, cat_time = _train_catboost(
        X_train, y_train, X_val, y_val, spw
    )
    cat_auc  = roc_auc_score(y_val, cat_proba)
    cat_topk = precision_at_topk(y_val, cat_proba, val_meta)
    logger.info("  CatBoost | AUC=%.4f | P@10=%.4f | P@30=%.4f | iter=%d | %.1fs",
                cat_auc, cat_topk.get(10, float("nan")),
                cat_topk.get(30, float("nan")), cat_iter, cat_time)

    # ── 4. Soft voting 앙상블 ──
    logger.info("[4/5] Soft voting 앙상블 (평균 확률)...")
    ens_proba = (lgbm_proba + xgb_proba + cat_proba) / 3.0
    ens_auc   = roc_auc_score(y_val, ens_proba)
    ens_topk  = precision_at_topk(y_val, ens_proba, val_meta)
    logger.info("  Ensemble | AUC=%.4f | P@10=%.4f | P@30=%.4f",
                ens_auc, ens_topk.get(10, float("nan")),
                ens_topk.get(30, float("nan")))

    # ── 결과 비교표 ──
    logger.info("")
    logger.info("┌─────────────┬──────────┬──────────┬──────────┬──────────┐")
    logger.info("│ 지표        │ LightGBM │  XGBoost │ CatBoost │  앙상블  │")
    logger.info("├─────────────┼──────────┼──────────┼──────────┼──────────┤")
    logger.info("│ Val AUC     │  %.4f  │  %.4f  │  %.4f  │  %.4f  │",
                lgbm_auc, xgb_auc, cat_auc, ens_auc)
    logger.info("│ Precision@10│  %.4f  │  %.4f  │  %.4f  │  %.4f  │",
                lgbm_topk.get(10, 0), xgb_topk.get(10, 0),
                cat_topk.get(10, 0),  ens_topk.get(10, 0))
    logger.info("│ Precision@20│  %.4f  │  %.4f  │  %.4f  │  %.4f  │",
                lgbm_topk.get(20, 0), xgb_topk.get(20, 0),
                cat_topk.get(20, 0),  ens_topk.get(20, 0))
    logger.info("│ Precision@30│  %.4f  │  %.4f  │  %.4f  │  %.4f  │",
                lgbm_topk.get(30, 0), xgb_topk.get(30, 0),
                cat_topk.get(30, 0),  ens_topk.get(30, 0))
    logger.info("│ 학습시간(s) │  %6.1f  │  %6.1f  │  %6.1f  │     -    │",
                lgbm_time, xgb_time, cat_time)
    logger.info("│ 최적 iter   │  %6d  │  %6d  │  %6d  │     -    │",
                lgbm_iter, xgb_iter, cat_iter)
    logger.info("└─────────────┴──────────┴──────────┴──────────┴──────────┘")

    # ── 4-b. 확률 calibration ──
    # 서빙 시 실제 쓰는 블렌드(CatBoost*0.8+XGBoost*0.2, equal-weight 3모델 앙상블이 아님 —
    # predictor.py의 _CatXgbBlend와 동일 가중치)에 맞춰 보정 함수를 학습. val을 시간순
    # 70:30 재분리해 뒤 30%(완전 held-out)로 과적합 여부 확인 후, 최종본은 val 전체로
    # 다시 학습(calibration_analysis.py 진단 결과: gap 0.27→0.03, 2026-06-22, 진단#7 후속).
    logger.info("[4-b/5] 확률 calibration 학습 (서빙 블렌드 기준)...")
    serving_blend_proba = CAT_BLEND_WEIGHT * cat_proba + XGB_BLEND_WEIGHT * xgb_proba
    calibrator, calib_gap_before, calib_gap_after = fit_calibrator_with_holdout_check(
        serving_blend_proba, y_val.values, val_meta["date"]
    )
    logger.info(
        "  calibration gap(held-out, 보정 전→후): %.4f -> %.4f (%.1f%% 개선)",
        calib_gap_before, calib_gap_after,
        100 * (calib_gap_before - calib_gap_after) / max(calib_gap_before, 1e-9),
    )

    # ── 5. 저장 ──
    logger.info("[5/5] 모델 저장...")
    version  = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = MODELS_DIR / f"{target_col}_{version}"
    save_dir.mkdir(parents=True)

    # LightGBM: .lgb 형식 (기존 서버 호환) + .pkl
    lgbm_model.booster_.save_model(str(save_dir / "model.lgb"))
    with open(save_dir / "model_lgbm.pkl", "wb") as f:
        pickle.dump(lgbm_model, f)

    # XGBoost: .pkl
    with open(save_dir / "model_xgb.pkl", "wb") as f:
        pickle.dump(xgb_model, f)

    # CatBoost: .pkl (내부 cbm 포함)
    with open(save_dir / "model_cat.pkl", "wb") as f:
        pickle.dump(cat_model, f)

    # 확률 calibration 함수 (predictor.py가 화면 표시용 probability_calibrated 계산에 사용,
    # 순위/추천 선정에는 영향 없음 — raw 확률로 그대로 랭킹)
    with open(save_dir / "calibrator.pkl", "wb") as f:
        pickle.dump(calibrator, f)

    # 메타
    meta = {
        "target_col":   target_col,
        "version":      version,
        "ensemble":     "soft_voting_equal_weight",
        "models":       ["lgbm", "xgb", "cat"],
        "val_auc": {
            "lgbm": lgbm_auc, "xgb": xgb_auc,
            "cat": cat_auc,   "ensemble": ens_auc,
        },
        "precision_at_topk": {
            "lgbm":     {str(k): v for k, v in lgbm_topk.items()},
            "xgb":      {str(k): v for k, v in xgb_topk.items()},
            "cat":      {str(k): v for k, v in cat_topk.items()},
            "ensemble": {str(k): v for k, v in ens_topk.items()},
        },
        "val_auc":           ens_auc,          # 서버 호환 (단일값)
        "lgbm_best_params":  best_lgbm_params,
        "lgbm_best_iter":    lgbm_iter,
        "xgb_best_iter":     xgb_iter,
        "cat_best_iter":     cat_iter,
        "train_time_sec":    {"lgbm": lgbm_time, "xgb": xgb_time, "cat": cat_time},
        "feature_cols":      FEATURE_COLS_REDUCED,
        "train_size":        len(X_train),
        "val_size":          len(X_val),
        "positive_rate":     float(y_train.mean()),
        "scale_pos_weight":  float(spw),
        "calibration_gap_before": calib_gap_before,
        "calibration_gap_after":  calib_gap_after,
    }
    # precision_at_topk 키 중복 제거 (ensemble을 최상위에 노출)
    meta["precision_at_topk_ensemble"] = meta["precision_at_topk"]["ensemble"]

    # ── target_5d: 5일 종가 기준 실거래 수치 계산 ──
    # P@K는 라벨 기준(5일내 최고+10% OR 종가+5%)이라 실거래(5일 보유 후 종가 청산)와
    # 수치가 다름. 아래 두 필드를 UI에서 병기해 과장 없이 표시.
    if target_col == "target_5d":
        _vm = val_meta.copy()
        _vm["_prob"] = ens_proba
        _win_list, _hit5_list = [], []
        for _, _grp in _vm.groupby("date"):
            _top = _grp.nlargest(10, "_prob")
            _rets = _top["ret_fwd_5d"].dropna().values
            if len(_rets) == 0:
                continue
            _win_list.append(float((_rets > 0).mean()))
            _hit5_list.append(float((_rets >= 0.05).mean()))
        meta["label_basis"] = "5일내최고+10% 또는 종가+5% (거래대금>=5억)"
        meta["close_win_rate_pct"] = round(float(np.mean(_win_list)) * 100, 1) if _win_list else None
        meta["close_hit5_rate_pct"] = round(float(np.mean(_hit5_list)) * 100, 1) if _hit5_list else None
        logger.info("  실거래 기준: 5d 종가 승률=%.1f%%, +5%% 도달률=%.1f%%",
                    meta["close_win_rate_pct"] or 0, meta["close_hit5_rate_pct"] or 0)

    (save_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    _plot_importance(lgbm_model, FEATURE_COLS_REDUCED, save_dir / "feature_importance.png")
    if target_col == "target_5d":
        run_backtest_5d(ens_proba, val_meta, save_path=save_dir / "backtest_5d.png")
    else:
        run_backtest(ens_proba, val_meta, save_path=save_dir / "backtest.png")

    logger.info("  model.lgb      → %s", save_dir / "model.lgb")
    logger.info("  model_lgbm.pkl → %s", save_dir / "model_lgbm.pkl")
    logger.info("  model_xgb.pkl  → %s", save_dir / "model_xgb.pkl")
    logger.info("  model_cat.pkl  → %s", save_dir / "model_cat.pkl")
    logger.info("  meta.json      → %s", save_dir / "meta.json")

    # ── 운영 모델 포인터 갱신 (server/predictor.py의 find_latest_model_dir 참고) ──
    # mtime이 아니라 이 파일이 가리키는 디렉터리만 서빙되므로, 검증 안 된 실험용
    # 학습(예: 진단/튜닝 비교)은 promote=False(--no-promote)로 호출해 운영에 영향 안 주게 할 것.
    # (2026-06-21: 실험 모델이 mtime 최신이라 잠깐 운영에 잘못 올라간 사고 이후 추가)
    if promote:
        active_pointer = MODELS_DIR / f"ACTIVE_{target_col}.txt"
        active_pointer.write_text(save_dir.name, encoding="utf-8")
        logger.info("  운영 모델 포인터  → %s (%s)", active_pointer.name, save_dir.name)
    else:
        logger.info("  --no-promote 지정 — 운영 모델 포인터 변경 안 함 (디렉터리만 저장됨)")

    logger.info("=" * 60)
    logger.info("완료 | 저장: %s", save_dir)
    logger.info("=" * 60)
    return save_dir


# ── CLI ────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="LightGBM+XGBoost+CatBoost 앙상블 학습")
    parser.add_argument(
        "--target", choices=["target_1d", "target_5d"], default="target_1d",
        help="학습할 라벨 컬럼",
    )
    parser.add_argument("--trials",   type=int, default=50,  help="Optuna 시도 횟수 (LightGBM)")
    parser.add_argument("--val-days", type=int, default=252, help="검증 기간 거래일 수")
    parser.add_argument("--no-promote", action="store_true",
                        help="결과를 운영 모델로 승격 안 함 (ACTIVE_{target}.txt 미변경) — 실험/진단용 학습 시 사용")
    args = parser.parse_args()
    train(target_col=args.target, n_trials=args.trials, val_days=args.val_days,
          promote=not args.no_promote)


if __name__ == "__main__":
    main()
