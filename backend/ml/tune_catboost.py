"""
CatBoost 하이퍼파라미터 Optuna 튜닝

흐름:
  1. build_dataset() → 현재 운영 모델과 동일한 train/val split
  2. Optuna 50 trials → Val AUC 최대화
  3. 베스트 파라미터로 최종 모델 학습
  4. 현재 운영 모델(0.6333)과 AUC / P@10 비교
  5. backend/models/target_5d_tuned_YYYYMMDD_HHMMSS/ 저장
"""

import json
import logging
import pickle
import time
from datetime import datetime
from pathlib import Path

import catboost as cb
import numpy as np
import optuna
from sklearn.metrics import roc_auc_score

from dataset import FEATURE_COLS, build_dataset
from evaluate import precision_at_topk, run_backtest_5d

MODELS_DIR = Path(__file__).parent.parent / "models"

# 현재 운영 모델 기준치
BASELINE_AUC   = 0.6333
BASELINE_P10   = 0.431
BASELINE_P20   = 0.417
BASELINE_P30   = 0.408
BASELINE_ITER  = 120

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("tune_catboost.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _catboost_objective(
    trial: optuna.Trial,
    X_train, y_train,
    X_val, y_val,
    spw: float,
) -> float:
    params = {
        "iterations":         trial.suggest_int("iterations", 100, 500),
        "learning_rate":      trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
        "depth":              trial.suggest_int("depth", 4, 10),
        "l2_leaf_reg":        trial.suggest_float("l2_leaf_reg", 1.0, 10.0),
        "border_count":       trial.suggest_int("border_count", 32, 255),
        "bagging_temperature": trial.suggest_float("bagging_temperature", 0.0, 1.0),
        "random_strength":    trial.suggest_float("random_strength", 0.0, 10.0),
        "min_data_in_leaf":   trial.suggest_int("min_data_in_leaf", 1, 100),
        # 고정값
        "eval_metric":        "AUC",
        "early_stopping_rounds": 50,
        "scale_pos_weight":   float(spw),
        "random_seed":        42,
        "verbose":            False,
        "task_type":          "CPU",
    }

    model = cb.CatBoostClassifier(**params)
    model.fit(X_train, y_train, eval_set=(X_val, y_val))
    proba = model.predict_proba(X_val)[:, 1]
    auc = float(roc_auc_score(y_val, proba))

    trial.set_user_attr("best_iteration", int(model.best_iteration_))
    return auc


def main(n_trials: int = 50, val_days: int = 252) -> None:
    logger.info("=" * 60)
    logger.info("CatBoost Optuna 튜닝 시작 | n_trials=%d | val_days=%d", n_trials, val_days)
    logger.info("optuna %s  catboost %s", optuna.__version__, cb.__version__)
    logger.info("=" * 60)

    # ── 1. 데이터 로드 ──
    logger.info("[1/4] 데이터셋 로드 (target_5d, val_days=%d)...", val_days)
    X_train, y_train, X_val, y_val, val_meta, _train_meta = build_dataset(
        target_col="target_5d", val_days=val_days
    )
    spw = (y_train == 0).sum() / max((y_train == 1).sum(), 1)
    logger.info(
        "  train=%d  val=%d  positive=%.1f%%  scale_pos_weight=%.2f",
        len(X_train), len(X_val), 100 * y_train.mean(), spw,
    )

    # ── 2. Optuna 튜닝 ──
    logger.info("[2/4] Optuna CatBoost 튜닝 (%d trials)...", n_trials)
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    completed = [0]

    def _objective_with_log(trial):
        auc = _catboost_objective(trial, X_train, y_train, X_val, y_val, spw)
        completed[0] += 1
        best_so_far = max(
            (t.value for t in trial.study.trials if t.value is not None),
            default=auc,
        )
        logger.info(
            "Trial %3d/%d | AUC=%.4f | best=%.4f | params=%s",
            completed[0], n_trials, auc, best_so_far,
            {k: round(v, 4) if isinstance(v, float) else v
             for k, v in trial.params.items()},
        )
        return auc

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(_objective_with_log, n_trials=n_trials, show_progress_bar=False)

    best_params = study.best_params
    best_auc_cv = study.best_value
    logger.info("  Optuna 완료 | best_trial_auc=%.4f", best_auc_cv)
    logger.info("  best_params: %s", json.dumps(best_params, indent=2))

    # ── 3. 베스트 파라미터로 최종 모델 학습 ──
    logger.info("[3/4] 최종 CatBoost 모델 학습 (베스트 파라미터)...")
    t0 = time.time()

    final_params = {
        **best_params,
        "eval_metric":           "AUC",
        "early_stopping_rounds": 50,
        "scale_pos_weight":      float(spw),
        "random_seed":           42,
        "verbose":               False,
        "task_type":             "CPU",
    }
    # iterations는 탐색 공간 상한보다 더 허용 (early stopping이 잡아줌)
    final_params["iterations"] = max(best_params.get("iterations", 200), 500)

    model = cb.CatBoostClassifier(**final_params)
    model.fit(X_train, y_train, eval_set=(X_val, y_val))
    elapsed = time.time() - t0

    proba = model.predict_proba(X_val)[:, 1]
    tuned_auc  = float(roc_auc_score(y_val, proba))
    tuned_topk = precision_at_topk(y_val, proba, val_meta)
    tuned_iter = int(model.best_iteration_)

    # ── 4. 비교표 출력 ──
    logger.info("")
    logger.info("=" * 60)
    logger.info("결과 비교표")
    logger.info("=" * 60)
    logger.info("%-14s  %10s  %10s  %s", "지표", "현재 운영", "튜닝 모델", "개선")

    def _diff(tuned, base, pct=True):
        d = tuned - base
        sign = "+" if d >= 0 else ""
        return f"{sign}{d*100:.2f}pp" if pct else f"{sign}{d:.0f}"

    logger.info("%-14s  %10.4f  %10.4f  %s", "Val AUC",
                BASELINE_AUC, tuned_auc, _diff(tuned_auc, BASELINE_AUC))
    logger.info("%-14s  %9.1f%%  %9.1f%%  %s", "Precision@10",
                BASELINE_P10 * 100, tuned_topk.get(10, 0) * 100,
                _diff(tuned_topk.get(10, 0), BASELINE_P10))
    logger.info("%-14s  %9.1f%%  %9.1f%%  %s", "Precision@20",
                BASELINE_P20 * 100, tuned_topk.get(20, 0) * 100,
                _diff(tuned_topk.get(20, 0), BASELINE_P20))
    logger.info("%-14s  %9.1f%%  %9.1f%%  %s", "Precision@30",
                BASELINE_P30 * 100, tuned_topk.get(30, 0) * 100,
                _diff(tuned_topk.get(30, 0), BASELINE_P30))
    logger.info("%-14s  %10d  %10d", "최적 iter", BASELINE_ITER, tuned_iter)
    logger.info("%-14s  %10s  %9.1fs", "학습시간", "-", elapsed)
    logger.info("=" * 60)

    # ── 판정 ──
    auc_improved  = tuned_auc > BASELINE_AUC
    p10_improved  = tuned_topk.get(10, 0) > BASELINE_P10

    if auc_improved and p10_improved:
        verdict = "PASS: AUC와 P@10 모두 개선 -> 저장 진행"
    elif auc_improved and not p10_improved:
        verdict = "PARTIAL: AUC 개선됐지만 P@10 감소 -> 저장하되 운영 적용 주의"
    else:
        verdict = "FAIL: Val AUC가 기준치(%g)보다 낮거나 같음 -> 튜닝 실패" % BASELINE_AUC

    logger.info("판정: %s", verdict)
    logger.info("")

    # ── 5. 저장 ──
    logger.info("[4/4] 모델 저장...")
    version  = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = MODELS_DIR / f"target_5d_tuned_{version}"
    save_dir.mkdir(parents=True, exist_ok=True)

    with open(save_dir / "model_cat.pkl", "wb") as f:
        pickle.dump(model, f)

    best_params_full = {k: v for k, v in final_params.items()}
    best_params_full["best_iteration_actual"] = tuned_iter
    (save_dir / "best_params.json").write_text(
        json.dumps(best_params_full, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    meta = {
        "target_col":      "target_5d",
        "version":         version,
        "model_type":      "CatBoost (Optuna tuned)",
        "n_trials":        n_trials,
        "val_days":        val_days,
        "best_trial_auc":  best_auc_cv,
        "val_auc":         tuned_auc,
        "precision_at_topk": {
            str(k): round(v, 4) for k, v in tuned_topk.items()
        },
        "best_iteration":  tuned_iter,
        "train_time_sec":  round(elapsed, 1),
        "train_size":      len(X_train),
        "val_size":        len(X_val),
        "positive_rate":   float(y_train.mean()),
        "scale_pos_weight": float(spw),
        "feature_cols":    FEATURE_COLS,
        "baseline": {
            "model":       "target_5d_20260524_164927",
            "val_auc":     BASELINE_AUC,
            "p10":         BASELINE_P10,
            "p20":         BASELINE_P20,
            "p30":         BASELINE_P30,
            "best_iter":   BASELINE_ITER,
        },
        "verdict": verdict,
        "optuna_best_params": best_params,
    }
    (save_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 백테스트 그래프
    try:
        run_backtest_5d(proba, val_meta, save_path=save_dir / "backtest_5d.png")
    except Exception as e:
        logger.warning("백테스트 그래프 저장 실패: %s", e)

    logger.info("  model_cat.pkl  -> %s", save_dir / "model_cat.pkl")
    logger.info("  best_params.json -> %s", save_dir / "best_params.json")
    logger.info("  meta.json      -> %s", save_dir / "meta.json")
    logger.info("")
    logger.info("베스트 파라미터 (JSON):")
    logger.info("%s", json.dumps(best_params, indent=2))
    logger.info("")
    logger.info("완료 | 저장 위치: %s", save_dir)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CatBoost Optuna 하이퍼파라미터 튜닝")
    parser.add_argument("--trials",   type=int, default=50,  help="Optuna trial 수 (기본 50)")
    parser.add_argument("--val-days", type=int, default=252, help="검증 기간 거래일 수")
    args = parser.parse_args()
    main(n_trials=args.trials, val_days=args.val_days)
