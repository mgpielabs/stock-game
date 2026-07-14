"""
약세장 대응 메커니즘 백테스트 비교

방법 A : KOSPI MA20 < MA60 → 전량 현금 (Cash mode)
방법 B : KOSPI 5d_ret < -3% → top_k 절반, < -5% → top_k 1/4
방법 C : ret_5d > 10% 제외 + volatility_20 상위 30% 제외
방법 D : A + C 조합

기준선 : 섹터 분산(KOSPI/KOSDAQ max 3) 적용 but 약세장 가드 없음

검증 기간:
  - 약세장: 2022-06-01 ~ 2022-12-30 (OOS, 튜닝 모델 미학습)
  - 강세장: 2025-05-12 ~ 2026-05-21 (val 기간)
"""

import json
import logging
import pickle
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "data"))

from dataset import FEATURE_COLS, build_dataset, _compute_price_features
from evaluate import COMMISSION, SELL_TAX, SLIPPAGE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bear_guard_analysis.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

DB_PATH    = Path(__file__).parent.parent / "data" / "stocks.db"
MODELS_DIR = Path(__file__).parent.parent / "models"

MIN_VOL_KRW = 1_000_000_000   # 유동성 필터 10억
TOP_K_BASE  = 10               # 기본 top_k
MAX_PER_MKT = 3                # 섹터 분산 (KOSPI/KOSDAQ max)

ETF_PATTERN = (
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s'
)


# ── KOSPI 데이터 로드 ─────────────────────────────────────────

def _load_kospi(start: str = "20211001", end: str = "20261231") -> pd.Series:
    """market_index에서 KOSPI 종가 시계열 (date 인덱스)"""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, close FROM market_index "
            "WHERE code='1001' AND date >= ? AND date <= ? ORDER BY date",
            (start, end),
        ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series({r[0]: float(r[1]) for r in rows})


def _kospi_signals(kospi: pd.Series, date: str) -> Dict:
    """특정 날짜의 KOSPI 신호 계산"""
    if date not in kospi.index:
        return {"ma_ok": True, "ret_5d": 0.0, "ma20": None, "ma60": None}

    idx = kospi.index.get_loc(date)
    ma20 = float(kospi.iloc[max(0, idx - 19): idx + 1].mean())
    ma60 = float(kospi.iloc[max(0, idx - 59): idx + 1].mean())
    ret_5d = float(kospi.iloc[idx] / kospi.iloc[max(0, idx - 5)] - 1) if idx >= 5 else 0.0

    return {
        "ma20":   ma20,
        "ma60":   ma60,
        "ma_ok":  ma20 >= ma60,   # True = bull/neutral
        "ret_5d": ret_5d,
    }


def _load_stock_markets() -> Dict[str, str]:
    """symbol → market (KOSPI/KOSDAQ) 매핑"""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT symbol, market FROM stocks").fetchall()
    return {r[0]: r[1] for r in rows}


# ── 예측 데이터 로드 ──────────────────────────────────────────

def _load_period_data(model, start: str, end: str) -> Tuple[
    Optional[np.ndarray],    # proba
    Optional[pd.DataFrame],  # meta (symbol, date, ret_fwd_5d, vol_krw_20d_raw, volume_krw)
    Optional[pd.DataFrame],  # X (feature matrix with ret_5d, volatility_20)
]:
    """
    지정 기간의 피처 + 예측 확률 + 메타 반환
    vol_krw_20d_raw, ret_5d, volatility_20는 prices 또는 features에서 조달
    """
    # prices 로드 (vol 계산용 — 넉넉한 기간)
    p_start = f"{int(start[:4])-1}{start[4:]}"  # 1년 앞 (rolling용)
    with sqlite3.connect(DB_PATH) as conn:
        features = pd.read_sql_query(
            f"SELECT * FROM features WHERE date >= '{start}' AND date <= '{end}' ORDER BY date, symbol",
            conn,
        )
        prices_raw = pd.read_sql_query(
            f"SELECT symbol, date, close, volume, market_cap "
            f"FROM prices WHERE date >= '{p_start}' AND date <= '{end}' ORDER BY symbol, date",
            conn,
        )
        etf_df = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)

    if features.empty:
        logger.warning("features 없음: %s ~ %s", start, end)
        return None, None, None

    etf_syms = set(etf_df.loc[
        etf_df["name"].str.contains(ETF_PATTERN, na=False, case=False, regex=True),
        "symbol",
    ])

    prices = _compute_price_features(prices_raw)
    price_cols = [
        "symbol", "date", "volume_krw", "vol_krw_5d", "vol_krw_20d",
        "vol_krw_20d_raw", "ret_fwd_5d", "vol_fwd_5d_krw", "fwd_max_5d", "fwd_min_5d",
    ]
    df = features.merge(prices[price_cols], on=["symbol", "date"], how="left")
    df = df[~df["symbol"].isin(etf_syms)]
    df = df[df["vol_krw_20d_raw"].fillna(0) >= MIN_VOL_KRW]
    df = df[(df["date"] >= start) & (df["date"] <= end)]

    if df.empty:
        return None, None, None

    X = df[FEATURE_COLS].astype(float)
    proba = model.predict_proba(X.values)[:, 1]

    meta = df[["symbol", "date", "volume_krw", "vol_krw_20d_raw",
               "ret_fwd_5d", "vol_fwd_5d_krw"]].copy().reset_index(drop=True)
    # anti-momentum 필터용 피처 추가
    meta["ret_5d"]       = df["ret_5d"].values if "ret_5d" in df.columns else 0.0
    meta["volatility_20"] = df["volatility_20"].values if "volatility_20" in df.columns else 0.0
    meta["proba"] = proba

    return proba, meta, X


# ── 핵심 백테스트 엔진 ─────────────────────────────────────────

def _run_backtest(
    meta: pd.DataFrame,
    kospi: pd.Series,
    stock_markets: Dict[str, str],
    method: str,           # "baseline", "A", "B", "C", "D"
    top_k: int = TOP_K_BASE,
    max_per_mkt: int = MAX_PER_MKT,
) -> Dict:
    """
    5일 리밸런싱 백테스트.
    meta 필수 컬럼: symbol, date, proba, ret_fwd_5d, volume_krw, ret_5d, volatility_20
    """
    unique_dates = sorted(meta["date"].unique())
    rebal_dates  = unique_dates[::5]

    portfolio_value = 1.0
    records = []
    cash_periods = 0

    for rd in rebal_dates:
        day = meta[meta["date"] == rd].copy()
        day = day[day["volume_krw"].fillna(0) >= MIN_VOL_KRW]
        if day.empty:
            continue

        sig = _kospi_signals(kospi, rd)
        effective_top_k = top_k

        # ── Method A: Cash mode ──
        if method in ("A", "D") and not sig["ma_ok"]:
            cash_periods += 1
            records.append({"date": rd, "ret_net": 0.0, "cum_ret": (portfolio_value - 1) * 100,
                            "cash_mode": True})
            continue

        # ── Method B: 포지션 축소 ──
        if method == "B":
            if sig["ret_5d"] < -0.05:
                effective_top_k = max(1, top_k // 4)
            elif sig["ret_5d"] < -0.03:
                effective_top_k = max(1, top_k // 2)

        # ── Method C / D: 역모멘텀 필터 ──
        if method in ("C", "D"):
            before_c = len(day)
            # ret_5d > 10% 제외
            day = day[day["ret_5d"].fillna(0) <= 0.10]
            # volatility_20 상위 30% 제외
            if not day.empty:
                vol20_cutoff = day["volatility_20"].quantile(0.70)
                day = day[day["volatility_20"].fillna(0) <= vol20_cutoff]

        if day.empty:
            records.append({"date": rd, "ret_net": 0.0, "cum_ret": (portfolio_value - 1) * 100,
                            "cash_mode": False})
            continue

        # ── 섹터 분산 (KOSPI/KOSDAQ max_per_mkt) ──
        day = day.sort_values("proba", ascending=False)
        selected, mkt_cnt = [], {}
        for _, row in day.iterrows():
            mkt = stock_markets.get(row["symbol"], "UNKNOWN")
            if mkt_cnt.get(mkt, 0) < max_per_mkt:
                selected.append(row)
                mkt_cnt[mkt] = mkt_cnt.get(mkt, 0) + 1
            if len(selected) >= effective_top_k:
                break

        if not selected:
            records.append({"date": rd, "ret_net": 0.0, "cum_ret": (portfolio_value - 1) * 100,
                            "cash_mode": False})
            continue

        rets = np.array([r["ret_fwd_5d"] if pd.notna(r["ret_fwd_5d"]) else 0.0 for r in selected])
        gross = float(rets.mean())
        net   = (1 + gross) * (1 - SLIPPAGE - COMMISSION) * (1 - SLIPPAGE - COMMISSION - SELL_TAX) - 1
        portfolio_value *= (1 + net)

        records.append({
            "date":      rd,
            "ret_gross": gross * 100,
            "ret_net":   net * 100,
            "cum_ret":   (portfolio_value - 1) * 100,
            "cash_mode": False,
        })

    if not records:
        return {}

    df_r = pd.DataFrame(records)
    cum_ret  = df_r["cum_ret"].iloc[-1]
    wins     = (df_r["ret_net"] > 0).sum()
    n_active = (df_r["cash_mode"] == False).sum() if "cash_mode" in df_r.columns else len(df_r)
    win_rate = float(wins / max(n_active, 1)) * 100

    # MDD
    vals = (1 + df_r["cum_ret"] / 100).values
    peak, mdd = vals[0], 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100
        if dd < mdd:
            mdd = dd

    cash_pct = cash_periods / max(len(rebal_dates), 1) * 100

    return {
        "method":      method,
        "cum_ret":     round(cum_ret, 1),
        "mdd":         round(mdd, 2),
        "win_rate":    round(win_rate, 1),
        "cash_pct":    round(cash_pct, 1),
        "n_periods":   len(records),
        "records":     df_r,
    }


# ── 차트 ──────────────────────────────────────────────────────

def _plot_comparison(results_dict: Dict, kospi_ret: float, title: str, save_path: Path):
    fig, ax = plt.subplots(figsize=(14, 7))
    colors = {
        "baseline": "gray",
        "A":        "steelblue",
        "B":        "orange",
        "C":        "green",
        "D":        "red",
    }
    labels = {
        "baseline": "Baseline (섹터분산만)",
        "A":        "A: Cash mode (MA20<MA60)",
        "B":        "B: 포지션 축소",
        "C":        "C: 역모멘텀 필터",
        "D":        "D: A+C 조합",
    }
    for method, res in results_dict.items():
        if not res or "records" not in res:
            continue
        df_r = res["records"]
        ax.plot(range(len(df_r)), df_r["cum_ret"].values,
                label=f"{labels.get(method, method)} ({res['cum_ret']:+.1f}%)",
                color=colors.get(method, "black"), linewidth=2)

    ax.axhline(0, color="black", linewidth=0.8, linestyle=":")
    ax.axhline(kospi_ret, color="purple", linewidth=1.5, linestyle="--",
               label=f"KOSPI ({kospi_ret:+.1f}%)")
    ax.set_title(title)
    ax.set_ylabel("누적 수익률 (%)")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    fig.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ── 메인 ──────────────────────────────────────────────────────

def main():
    logger.info("=" * 70)
    logger.info("약세장 대응 메커니즘 백테스트 분석")
    logger.info("=" * 70)

    # 튜닝 모델 로드
    tuned_dir  = MODELS_DIR / "target_5d_tuned_20260524_185806"
    model_path = tuned_dir / "model_cat.pkl"
    if not model_path.exists():
        logger.error("튜닝 모델 없음: %s", model_path)
        return

    with open(model_path, "rb") as f:
        model = pickle.load(f)
    logger.info("튜닝 모델 로드 완료: %s", tuned_dir.name)

    # KOSPI 전체 로드 (두 기간 모두 커버)
    kospi_full    = _load_kospi("20211001", "20261231")
    stock_markets = _load_stock_markets()

    methods = ["baseline", "A", "B", "C", "D"]

    # ── 1. 약세장 (2022-06 ~ 2022-12, OOS) ──
    logger.info("\n[1/2] 약세장 OOS 백테스트: 20220601 ~ 20221230")
    _, meta_bear, _ = _load_period_data(model, "20220601", "20221230")
    if meta_bear is None:
        logger.error("2022 데이터 없음 — backfill_2022.py 실행 필요")
        return

    kospi_bear_ret = float(
        (kospi_full.get("20221229", kospi_full.iloc[-1]) /
         kospi_full.get("20220601", kospi_full.iloc[0]) - 1) * 100
    ) if not kospi_full.empty else -15.9

    results_bear = {}
    for m in methods:
        res = _run_backtest(meta_bear, kospi_full, stock_markets, method=m)
        results_bear[m] = res
        logger.info("  [%s] 누적=%+.1f%% MDD=%.1f%% 승률=%.1f%% 현금=%s%%",
                    m,
                    res.get("cum_ret", 0),
                    res.get("mdd", 0),
                    res.get("win_rate", 0),
                    res.get("cash_pct", 0) if m in ("A", "D") else "-")

    # ── 2. 강세장 (2025-05 ~ 2026-05, val 기간) ──
    logger.info("\n[2/2] 강세장 val 백테스트: 20250512 ~ 20260521")
    _, meta_bull, _ = _load_period_data(model, "20250512", "20260521")
    if meta_bull is None:
        logger.error("2025-2026 val 데이터 없음")
        return

    kospi_bull_ret = float(
        (kospi_full.get("20260521", kospi_full.iloc[-1]) /
         kospi_full.get("20250512", kospi_full.iloc[0]) - 1) * 100
    ) if not kospi_full.empty else 0.0

    results_bull = {}
    for m in methods:
        res = _run_backtest(meta_bull, kospi_full, stock_markets, method=m)
        results_bull[m] = res
        logger.info("  [%s] 누적=%+.1f%% MDD=%.1f%% 승률=%.1f%% 현금=%s%%",
                    m,
                    res.get("cum_ret", 0),
                    res.get("mdd", 0),
                    res.get("win_rate", 0),
                    res.get("cash_pct", 0) if m in ("A", "D") else "-")

    # ── 비교표 ──
    logger.info("")
    logger.info("=" * 75)
    logger.info("약세장 백테스트 결과 비교 (20220601 ~ 20221230, KOSPI: %+.1f%%)", kospi_bear_ret)
    logger.info("=" * 75)
    header = f"{'방법':<10}  {'누적수익':>9}  {'MDD':>8}  {'승률':>7}  {'알파':>8}  {'현금비중':>8}"
    logger.info(header)
    logger.info("-" * 75)
    for m in methods:
        r = results_bear[m]
        cum  = r.get("cum_ret", 0)
        mdd  = r.get("mdd", 0)
        wr   = r.get("win_rate", 0)
        alp  = cum - kospi_bear_ret
        cp   = r.get("cash_pct", 0)
        name = {"baseline": "현재(분산만)", "A": "A: Cash", "B": "B: 축소",
                "C": "C: 역모멘텀", "D": "D: A+C"}[m]
        logger.info("%-12s  %+8.1f%%  %7.1f%%  %6.1f%%  %+7.1f%%p  %7.1f%%",
                    name, cum, mdd, wr, alp, cp if m in ("A", "D") else 0)

    logger.info("")
    logger.info("=" * 75)
    logger.info("강세장 백테스트 결과 비교 (20250512 ~ 20260521, KOSPI: %+.1f%%)", kospi_bull_ret)
    logger.info("=" * 75)
    logger.info(header)
    logger.info("-" * 75)
    for m in methods:
        r = results_bull[m]
        cum  = r.get("cum_ret", 0)
        mdd  = r.get("mdd", 0)
        wr   = r.get("win_rate", 0)
        alp  = cum - kospi_bull_ret
        cp   = r.get("cash_pct", 0)
        name = {"baseline": "현재(분산만)", "A": "A: Cash", "B": "B: 축소",
                "C": "C: 역모멘텀", "D": "D: A+C"}[m]
        logger.info("%-12s  %+8.1f%%  %7.1f%%  %6.1f%%  %+7.1f%%p  %7.1f%%",
                    name, cum, mdd, wr, alp, cp if m in ("A", "D") else 0)

    # ── 권장 방법 선택 ──
    logger.info("")
    logger.info("=" * 75)
    logger.info("권장 방법 선택 기준:")
    logger.info("  - 약세장 알파 > -10%%p  (현재: %+.1f%%p)", results_bear["baseline"].get("cum_ret", 0) - kospi_bear_ret)
    logger.info("  - 강세장 수익 > 500%%   (현재: %.1f%%)", results_bull["baseline"].get("cum_ret", 0))
    logger.info("  - 현금 보유 비중 < 50%%")
    logger.info("")

    best_method = None
    for m in ["D", "A", "C", "B"]:  # 우선순위: D > A > C > B
        bear_alpha = results_bear[m].get("cum_ret", 0) - kospi_bear_ret
        bull_cum   = results_bull[m].get("cum_ret", 0)
        cash_pct   = results_bear[m].get("cash_pct", 0) if m in ("A", "D") else 0
        if bear_alpha > -10 and bull_cum > 500 and cash_pct < 50:
            best_method = m
            logger.info("*** 권장: 방법 %s (약세장 알파=%+.1f%%p, 강세장=%+.1f%%, 현금=%.1f%%)",
                        m, bear_alpha, bull_cum, cash_pct)
            break

    if best_method is None:
        logger.info("기준 충족 방법 없음. 약세장 개선 효과가 가장 큰 방법 선택:")
        best_method = max(["A", "B", "C", "D"],
                          key=lambda m: results_bear[m].get("cum_ret", -999) - kospi_bear_ret)
        logger.info("  선택: 방법 %s", best_method)

    # ── 차트 저장 ──
    save_dir = MODELS_DIR / "target_5d_tuned_20260524_185806"
    _plot_comparison(
        results_bear, kospi_bear_ret,
        "약세장 OOS 백테스트 (2022-06 ~ 2022-12)",
        save_dir / "bear_market_method_comparison.png",
    )
    _plot_comparison(
        results_bull, kospi_bull_ret,
        "강세장 val 백테스트 (2025-05 ~ 2026-05)",
        save_dir / "bull_market_method_comparison.png",
    )

    # ── 결과 저장 ──
    output = {
        "bear_market": {
            m: {k: v for k, v in r.items() if k != "records"}
            for m, r in results_bear.items() if r
        },
        "bull_market": {
            m: {k: v for k, v in r.items() if k != "records"}
            for m, r in results_bull.items() if r
        },
        "kospi_bear_ret": round(kospi_bear_ret, 2),
        "kospi_bull_ret": round(kospi_bull_ret, 2),
        "recommended_method": best_method,
    }
    (save_dir / "bear_guard_analysis.json").write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("\n결과 저장: %s", save_dir / "bear_guard_analysis.json")
    logger.info("차트 저장: bear_market_method_comparison.png / bull_market_method_comparison.png")
    logger.info("=" * 70)
    logger.info("권장 방법: %s", best_method)

    return best_method, output


if __name__ == "__main__":
    main()
