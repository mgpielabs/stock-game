"""
2022년 데이터 확장 학습 + 약세장 백테스트

[1] 튜닝된 하이퍼파라미터로 2022 포함 전체 데이터셋 재학습
[2] 결과 비교: 운영(0.6333) vs 튜닝(0.6373) vs 튜닝+2022
[3] 약세장 OOS 백테스트 (2022-06 ~ 2022-12)
    - 튜닝 모델(2022 미학습)을 사용해 2022 데이터를 OOS로 예측
    - KOSPI 동기간 수익률과 비교
"""

import json
import logging
import pickle
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import catboost as cb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from dataset import FEATURE_COLS, build_dataset
from evaluate import precision_at_topk, run_backtest_5d

MODELS_DIR = Path(__file__).parent.parent / "models"
DB_PATH    = Path(__file__).parent.parent / "data" / "stocks.db"

# ── 기준치 ────────────────────────────────────────────────────────
BASELINE = {
    "label":   "운영 모델",
    "auc":     0.6333,
    "p10":     0.431,
    "p20":     0.417,
    "p30":     0.408,
    "iter":    120,
}
TUNED = {
    "label":   "튜닝 모델",
    "auc":     0.6373,
    "p10":     0.435,
    "p20":     0.419,
    "p30":     0.416,
    "iter":    160,
}
# 튜닝된 하이퍼파라미터
BEST_PARAMS = {
    "iterations":          182,
    "learning_rate":       0.04810394860422767,
    "depth":               7,
    "l2_leaf_reg":         3.9085706990238087,
    "border_count":        140,
    "bagging_temperature": 0.3782444147929104,
    "random_strength":     4.539062925383762,
    "min_data_in_leaf":    11,
    "eval_metric":         "AUC",
    "early_stopping_rounds": 50,
    "task_type":           "CPU",
    "random_seed":         42,
    "verbose":             False,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("train_2022.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── 약세장 OOS 백테스트 ─────────────────────────────────────────

def _load_2022_features(start: str = "20220601", end: str = "20221230") -> Tuple[
    Optional[pd.DataFrame], Optional[pd.DataFrame]
]:
    """
    features 테이블에서 2022 데이터 로드.
    반환: (X_2022, meta_2022)  — meta에는 ret_fwd_5d 포함
    """
    with sqlite3.connect(DB_PATH) as conn:
        features = pd.read_sql_query(
            f"SELECT * FROM features WHERE date >= '{start}' AND date <= '{end}' ORDER BY date, symbol",
            conn,
        )
        if features.empty:
            logger.warning("2022 features 없음 (%s ~ %s)", start, end)
            return None, None

        # prices for ret_fwd_5d, volume_krw (backtest용)
        prices_raw = pd.read_sql_query(
            f"""
            SELECT symbol, date, close, volume, market_cap
            FROM prices
            WHERE date >= '20220101' AND date <= '20230131'
            ORDER BY symbol, date
            """, conn,
        )
        etf_rows = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)

    from dataset import _etf_symbols, _compute_price_features
    etf_syms = set(etf_rows.loc[
        etf_rows["name"].str.contains(
            r'ETF|ETN|레버리지|인버스|선물'
            r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
            na=False, case=False, regex=True,
        ), "symbol"
    ])

    prices = _compute_price_features(prices_raw)
    price_cols = [
        "symbol", "date", "volume_krw", "vol_krw_5d", "vol_krw_20d", "vol_krw_20d_raw",
        "ret_fwd_5d", "vol_fwd_5d_krw", "fwd_max_5d", "fwd_min_5d",
    ]
    df = features.merge(prices[price_cols], on=["symbol", "date"], how="left")
    df = df[~df["symbol"].isin(etf_syms)]
    df = df[df["vol_krw_20d_raw"].fillna(0) >= 1_000_000_000]
    df = df[(df["date"] >= start) & (df["date"] <= end)]

    if df.empty:
        return None, None

    # 피처
    X = df[FEATURE_COLS].astype(float)
    meta = df[["symbol", "date", "ret_fwd_5d", "vol_fwd_5d_krw",
               "fwd_max_5d", "fwd_min_5d", "volume_krw"]].reset_index(drop=True)
    return X, meta


def _load_kospi_2022(start: str = "20220601", end: str = "20221230") -> pd.Series:
    """KOSPI 2022 종가 Series (date 인덱스)"""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT date, close FROM market_index WHERE code='1001' "
            f"AND date >= '{start}' AND date <= '{end}' ORDER BY date"
        ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series({r[0]: r[1] for r in rows})
    return s


def run_bear_market_backtest(
    tuned_model_path: Path,
    start: str = "20220601",
    end: str   = "20221230",
    top_k: int = 10,
    vol_filter_bn: float = 10.0,
    save_dir: Optional[Path] = None,
):
    """
    튜닝 모델(2022 미학습)로 2022 데이터 OOS 예측 → 백테스트.
    """
    logger.info("=" * 60)
    logger.info("약세장 OOS 백테스트: %s ~ %s (튜닝 모델 사용)", start, end)
    logger.info("=" * 60)

    # 모델 로드
    with open(tuned_model_path, "rb") as f:
        model = pickle.load(f)

    X_2022, meta_2022 = _load_2022_features(start, end)
    if X_2022 is None:
        logger.error("2022 features 로드 실패 — 백필 완료 후 재실행 필요")
        return None

    logger.info("  2022 데이터: %d행, %d날짜",
                len(X_2022), meta_2022["date"].nunique())

    # 예측
    proba = model.predict_proba(X_2022.values)[:, 1]

    # 백테스트 (5일 보유)
    from evaluate import COMMISSION, SELL_TAX, SLIPPAGE, MIN_VOLUME_KRW

    vol_filter_krw = vol_filter_bn * 1_000_000_000
    meta = meta_2022.copy()
    meta["y_proba"] = proba

    # 5일 주기 리밸런싱 시뮬레이션
    unique_dates = sorted(meta["date"].unique())
    rebal_dates  = unique_dates[::5]  # 5거래일마다

    portfolio_value = 1.0
    records = []

    for rebal_date in rebal_dates:
        day_df = meta[meta["date"] == rebal_date].copy()
        # 유동성 필터
        day_df = day_df[day_df["volume_krw"].fillna(0) >= vol_filter_krw]
        if day_df.empty:
            continue

        # 상위 top_k
        top = day_df.nlargest(top_k, "y_proba")
        rets = top["ret_fwd_5d"].fillna(0.0).values

        if len(rets) == 0:
            continue

        period_ret_gross = float(rets.mean())
        # 비용 차감
        buy_cost  = SLIPPAGE + COMMISSION
        sell_cost = SLIPPAGE + COMMISSION + SELL_TAX
        period_ret_net = (1 + period_ret_gross) * (1 - buy_cost) * (1 - sell_cost) - 1

        portfolio_value *= (1 + period_ret_net)
        records.append({
            "date":      rebal_date,
            "ret_gross": period_ret_gross * 100,
            "ret_net":   period_ret_net * 100,
            "cum_ret":   (portfolio_value - 1) * 100,
        })

    if not records:
        logger.warning("백테스트 데이터 없음")
        return None

    result_df = pd.DataFrame(records)
    cum_ret = result_df["cum_ret"].iloc[-1]

    # MDD 계산
    vals = 1 + result_df["cum_ret"].values / 100
    peak = vals[0]
    mdd  = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100
        if dd < mdd:
            mdd = dd

    # 승률
    win_rate = (result_df["ret_net"] > 0).mean()

    # KOSPI 수익률
    kospi = _load_kospi_2022(start, end)
    kospi_ret = float((kospi.iloc[-1] / kospi.iloc[0] - 1) * 100) if len(kospi) >= 2 else 0.0

    logger.info("")
    logger.info("약세장 백테스트 결과 (%s ~ %s)", start, end)
    logger.info("-" * 50)
    logger.info("KOSPI 동기간 수익률:  %+.1f%%", kospi_ret)
    logger.info("포트폴리오 누적 수익: %+.1f%%", cum_ret)
    logger.info("최대 낙폭(MDD):       %.2f%%", mdd)
    logger.info("승률:                 %.1f%%", win_rate * 100)
    logger.info("리밸런싱 횟수:        %d회", len(records))
    logger.info("초과 수익:            %+.1f%%p", cum_ret - kospi_ret)
    logger.info("-" * 50)

    # 차트
    if save_dir:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))

        ax1.plot(range(len(result_df)), result_df["cum_ret"].values,
                 label="Portfolio (net)", color="steelblue", linewidth=2)

        # KOSPI 정규화 (같은 기간)
        if not kospi.empty:
            kospi_aligned = kospi[kospi.index >= start]
            kospi_cum = (kospi_aligned / kospi_aligned.iloc[0] - 1) * 100
            ax1_dates = [d for d in kospi_aligned.index if d in set(unique_dates)]
            # 날짜 매핑
            date_to_idx = {d: i for i, d in enumerate(unique_dates)}
            kx = [date_to_idx.get(d, None) for d in kospi_aligned.index]
            ky = list(kospi_cum.values)
            kx_valid = [(x, y) for x, y in zip(kx, ky) if x is not None]
            if kx_valid:
                ax1.plot([x for x, _ in kx_valid], [y for _, y in kx_valid],
                         label=f"KOSPI ({kospi_ret:+.1f}%)",
                         color="orange", linewidth=1.5, linestyle="--")

        ax1.axhline(0, color="black", linewidth=0.8, linestyle=":")
        ax1.set_title(f"약세장 OOS 백테스트 ({start} ~ {end})")
        ax1.set_ylabel("누적 수익률 (%)")
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        colors = ["green" if r > 0 else "red" for r in result_df["ret_net"]]
        ax2.bar(range(len(result_df)), result_df["ret_net"].values, color=colors, alpha=0.7)
        ax2.axhline(0, color="black", linewidth=0.8)
        ax2.set_title("기간별 수익률 (net)")
        ax2.set_ylabel("수익률 (%)")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        chart_path = save_dir / "bear_market_backtest_2022.png"
        fig.savefig(chart_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        logger.info("차트 저장: %s", chart_path)

    return {
        "start":       start,
        "end":         end,
        "kospi_ret":   round(kospi_ret, 2),
        "cum_ret":     round(cum_ret, 2),
        "mdd":         round(mdd, 2),
        "win_rate":    round(float(win_rate) * 100, 1),
        "n_periods":   len(records),
        "alpha":       round(cum_ret - kospi_ret, 2),
    }


# ── 메인 학습 파이프라인 ─────────────────────────────────────────

def main(val_days: int = 252):
    logger.info("=" * 60)
    logger.info("2022 확장 데이터 CatBoost 학습")
    logger.info("=" * 60)

    # ── 1. 데이터셋 로드 ──
    logger.info("[1/4] 데이터셋 로드 (val_days=%d)...", val_days)
    X_train, y_train, X_val, y_val, val_meta, train_meta = build_dataset(
        target_col="target_5d", val_days=val_days
    )
    spw = (y_train == 0).sum() / max((y_train == 1).sum(), 1)

    train_dates = sorted(train_meta["date"].unique())
    val_dates   = sorted(val_meta["date"].unique())
    logger.info("  train: %d행, %d날짜 (%s ~ %s)",
                len(X_train), len(train_dates),
                train_dates[0] if train_dates else "-",
                train_dates[-1] if train_dates else "-")
    logger.info("  val:   %d행, %d날짜 (%s ~ %s)",
                len(X_val), len(val_dates),
                val_dates[0] if val_dates else "-",
                val_dates[-1] if val_dates else "-")
    logger.info("  positive=%.1f%%  scale_pos_weight=%.2f",
                100 * y_train.mean(), spw)

    # ── 2. 학습 ──
    logger.info("[2/4] CatBoost 학습 (튜닝 파라미터, early stopping=50)...")
    t0 = time.time()

    params = {**BEST_PARAMS, "scale_pos_weight": float(spw)}
    # max iterations를 높여 early stopping이 충분히 탐색하도록
    params["iterations"] = max(params["iterations"], 500)

    model = cb.CatBoostClassifier(**params)
    model.fit(X_train, y_train, eval_set=(X_val, y_val))
    elapsed = time.time() - t0

    proba      = model.predict_proba(X_val)[:, 1]
    tuned_auc  = float(roc_auc_score(y_val, proba))
    tuned_topk = precision_at_topk(y_val, proba, val_meta)
    tuned_iter = int(model.best_iteration_)

    # ── 3. 비교표 ──
    logger.info("")
    logger.info("=" * 65)
    logger.info("결과 비교표")
    logger.info("=" * 65)
    logger.info("%-14s  %10s  %10s  %12s", "지표", "운영모델", "튜닝모델", "튜닝+2022")
    logger.info("-" * 65)

    def _d(tuned, base):
        d = tuned - base
        return f"({'+' if d >= 0 else ''}{d*100:.2f}pp)"

    logger.info("%-14s  %10.4f  %10.4f  %10.4f %s",
                "Val AUC", BASELINE["auc"], TUNED["auc"], tuned_auc,
                _d(tuned_auc, TUNED["auc"]))
    logger.info("%-14s  %9.1f%%  %9.1f%%  %9.1f%% %s",
                "Precision@10",
                BASELINE["p10"]*100, TUNED["p10"]*100,
                tuned_topk.get(10, 0)*100,
                _d(tuned_topk.get(10, 0), TUNED["p10"]))
    logger.info("%-14s  %9.1f%%  %9.1f%%  %9.1f%% %s",
                "Precision@20",
                BASELINE["p20"]*100, TUNED["p20"]*100,
                tuned_topk.get(20, 0)*100,
                _d(tuned_topk.get(20, 0), TUNED["p20"]))
    logger.info("%-14s  %9.1f%%  %9.1f%%  %9.1f%% %s",
                "Precision@30",
                BASELINE["p30"]*100, TUNED["p30"]*100,
                tuned_topk.get(30, 0)*100,
                _d(tuned_topk.get(30, 0), TUNED["p30"]))
    logger.info("%-14s  %10d  %10d  %10d",
                "최적 iter", BASELINE["iter"], TUNED["iter"], tuned_iter)
    logger.info("%-14s  %10s  %10d  %10d",
                "학습 샘플", "~530K", len(X_train), len(X_train))
    logger.info("%-14s  %9.1f%%  %9.1f%%  %9.1f%%",
                "양성률", 18.3, 18.3, y_train.mean()*100)
    logger.info("=" * 65)

    auc_vs_tuned = tuned_auc - TUNED["auc"]
    p10_vs_tuned = tuned_topk.get(10, 0) - TUNED["p10"]

    if tuned_auc >= TUNED["auc"] and tuned_topk.get(10, 0) >= TUNED["p10"]:
        verdict = "PASS: 2022 데이터 확장이 AUC와 P@10 모두 개선"
    elif tuned_auc >= TUNED["auc"]:
        verdict = "PARTIAL: AUC 개선, P@10 소폭 감소 — 서버 적용 검토 필요"
    else:
        verdict = f"FAIL: AUC {auc_vs_tuned*100:+.2f}pp — 2022 데이터가 노이즈 추가 가능성"
    logger.info("판정: %s", verdict)

    # ── 4. 저장 ──
    logger.info("")
    logger.info("[3/4] 모델 저장...")
    version  = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_dir = MODELS_DIR / f"target_5d_tuned_2022_{version}"
    save_dir.mkdir(parents=True, exist_ok=True)

    with open(save_dir / "model_cat.pkl", "wb") as f:
        pickle.dump(model, f)

    meta_out = {
        "target_col":       "target_5d",
        "version":          version,
        "model_type":       "CatBoost (Optuna tuned + 2022 expanded)",
        "val_auc":          tuned_auc,
        "precision_at_topk": {str(k): round(v, 4) for k, v in tuned_topk.items()},
        "best_iteration":   tuned_iter,
        "train_size":       len(X_train),
        "val_size":         len(X_val),
        "train_date_range": [train_dates[0] if train_dates else "", train_dates[-1] if train_dates else ""],
        "val_date_range":   [val_dates[0] if val_dates else "", val_dates[-1] if val_dates else ""],
        "positive_rate":    float(y_train.mean()),
        "scale_pos_weight": float(spw),
        "train_time_sec":   round(elapsed, 1),
        "feature_cols":     FEATURE_COLS,
        "hyperparameters":  BEST_PARAMS,
        "verdict":          verdict,
        "comparison": {
            "baseline": BASELINE,
            "tuned":    TUNED,
            "tuned_2022": {
                "auc": tuned_auc,
                "p10": tuned_topk.get(10, 0),
                "p20": tuned_topk.get(20, 0),
                "p30": tuned_topk.get(30, 0),
                "iter": tuned_iter,
            },
        },
    }
    (save_dir / "meta.json").write_text(
        json.dumps(meta_out, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ── 5. 약세장 OOS 백테스트 ──
    logger.info("[4/4] 약세장 OOS 백테스트 (튜닝 모델 기준)...")
    tuned_model_dir = MODELS_DIR / "target_5d_tuned_20260524_185806"
    tuned_model_path = tuned_model_dir / "model_cat.pkl"

    if tuned_model_path.exists():
        bear_result = run_bear_market_backtest(
            tuned_model_path=tuned_model_path,
            start="20220601",
            end="20221230",
            save_dir=save_dir,
        )
        if bear_result:
            meta_out["bear_market_backtest"] = bear_result
            (save_dir / "meta.json").write_text(
                json.dumps(meta_out, indent=2, ensure_ascii=False), encoding="utf-8"
            )
    else:
        logger.warning("튜닝 모델을 찾을 수 없음: %s", tuned_model_path)
        logger.info("  현재 모델로 2022 약세장 백테스트 진행...")
        bear_result = run_bear_market_backtest(
            tuned_model_path=save_dir / "model_cat.pkl",
            start="20220601",
            end="20221230",
            save_dir=save_dir,
        )

    # 백테스트 그래프 (val 기간)
    try:
        run_backtest_5d(proba, val_meta, save_path=save_dir / "backtest_5d.png")
    except Exception as e:
        logger.warning("val 백테스트 그래프 오류: %s", e)

    logger.info("  저장 위치: %s", save_dir)
    logger.info("완료")
    return save_dir


if __name__ == "__main__":
    main()
