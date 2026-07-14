"""
[검증 전용 — 운영 코드/모델 미사용] 스크리너 후보 조건 단독 검증

목적: 스크리너를 만들기 전에 "어떤 개별 조건이 실제로 다음 5일/20일 수익률에
edge가 있는지"를 워크포워드로 검증. ML 예측/추세추종은 이미 실패했으므로,
이번엔 복잡한 모델 없이 단순 조건 13종을 하나씩 검증한다.

데이터 가용성 — 조건별로 신뢰도가 다름(중요):
  [Tier 1: 2022~2026 전체 기간, point-in-time 안전] prices 테이블만 사용
    → 거래량급증/거래대금/신고가근접/모멘텀/역추세/RSI/볼린저/사이즈프록시/변동성
  [Tier 2: 배당] dividends 테이블(DART 연간 공시) — 2021~2025년 사업연도, 공시
    지연(약 3개월) 가정한 point-in-time 처리로 2022~2025 검증 가능
  [Tier 3: ⚠️ 데이터 빈약, 결론 보류 권장] PER/PBR/외국인 순매수
    조사 중 발견: features.per/pbr는 load_fundamentals_latest()가 "가장 최근
    값"을 모든 과거 행에 동일하게 채워서 만든 것이라 실제로는 날짜별로 변하지
    않는 정적 속성에 가까움(point-in-time 아님 — 별도 이슈로 기록 필요).
    원본 fundamentals/flows 테이블도 2025-05-07~2026-05-07 약 1년 공백이 있어
    실제 연속 일별 데이터는 2026-05-12~06-19 약 6주뿐. 이 두 조건은 raw
    테이블로 그 6주만 테스트하고 강한 결론은 내리지 않음.

검증 방법:
  - in-sample(2022~2024) / out-of-sample(2025~2026) 분리, 양쪽에서 같은 방향
    +유의해야 "진짜 edge"로 인정 (배당/계절성 신호 연구와 동일한 기준)
  - 조건 만족군 vs 불만족군 vs 전체평균, 승률+평균수익+t검정
  - 다중비교 보정: 조건 13개 x 2개 horizon = 26회 검정 → Bonferroni 기준
    0.05/26 ≈ 0.0019 적용
  - 시장 상황(강세/횡보/약세, KOSPI 20일 수익률 기준)별 분할

실행: python factor_screen_validation.py
"""

import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from evaluate import MIN_VOLUME_KRW

pd.set_option("display.width", 160)

DATA_START = "20211001"
DATA_END   = "20260619"
IN_SAMPLE_YEARS = [2022, 2023, 2024]
OOS_YEARS       = [2025, 2026]
N_CONDITIONS_X_HORIZONS = 26
BONFERRONI_ALPHA = 0.05 / N_CONDITIONS_X_HORIZONS

import re
ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)


# ── 데이터 로드 ───────────────────────────────────────────────

def load_prices(conn: sqlite3.Connection) -> pd.DataFrame:
    names = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)
    etf_syms = set(names.loc[names["name"].fillna("").str.contains(ETF_PATTERN), "symbol"])

    df = pd.read_sql_query(
        "SELECT symbol, date, open, high, low, close, volume FROM prices "
        "WHERE date BETWEEN ? AND ? ORDER BY symbol, date",
        conn, params=(DATA_START, DATA_END),
    )
    df = df[~df["symbol"].isin(etf_syms)].reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["value"] = df["close"] * df["volume"]
    counts = df.groupby("symbol").size()
    keep = counts[counts >= 280].index
    return df[df["symbol"].isin(keep)].reset_index(drop=True)


def build_price_conditions(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("symbol", group_keys=False)

    # 전진(forward) 수익률 — 라벨, 미래값이지만 "결과 측정용"이라 누수 아님(조건 자체엔 미사용)
    df["ret_fwd_5d"]  = g["close"].transform(lambda s: s.shift(-5) / s - 1)
    df["ret_fwd_20d"] = g["close"].transform(lambda s: s.shift(-20) / s - 1)

    # 유동성 (오늘까지의 과거 데이터만 사용)
    df["value_ma20"] = g["value"].transform(lambda s: s.rolling(20).mean())
    df["liquid"] = df["value_ma20"] >= MIN_VOLUME_KRW

    # 거래량 급증 (60일 평균 대비 — 이전 분석과 다른 기준)
    df["vol_ma60"] = g["volume"].transform(lambda s: s.rolling(60).mean())
    df["vol_ratio_60"] = df["volume"] / df["vol_ma60"].replace(0, np.nan)
    df["cond_vol_surge"] = df["vol_ratio_60"] >= 3.0

    # 거래대금 상위 (당일, 횡단면 상위 20% — 날짜별로 계산)
    df["cond_value_top"] = df.groupby("date")["value"].rank(pct=True) >= 0.8

    # 신고가 근접 (252일 최고가의 95% 이상)
    df["high_252"] = g["close"].transform(lambda s: s.rolling(252, min_periods=200).max())
    df["cond_near_high"] = df["close"] >= 0.95 * df["high_252"]

    # 모멘텀 상위/하위 (20일 수익률 횡단면 분위)
    df["ret_20d"] = g["close"].transform(lambda s: s.pct_change(20))
    rank20 = df.groupby("date")["ret_20d"].rank(pct=True)
    df["cond_momentum_top"] = rank20 >= 0.8
    df["cond_momentum_bottom"] = rank20 <= 0.2

    # RSI(14) 과매도
    delta = g["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.groupby(df["symbol"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    avg_loss = loss.groupby(df["symbol"]).transform(lambda s: s.ewm(alpha=1 / 14, adjust=False).mean())
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi_14"] = 100 - 100 / (1 + rs)
    df["cond_rsi_oversold"] = df["rsi_14"] < 30

    # 볼린저 하단 터치
    ma20 = g["close"].transform(lambda s: s.rolling(20).mean())
    std20 = g["close"].transform(lambda s: s.rolling(20).std())
    upper, lower = ma20 + 2 * std20, ma20 - 2 * std20
    df["bb_pct"] = (df["close"] - lower) / (upper - lower).replace(0, np.nan)
    df["cond_bb_lower"] = df["bb_pct"] <= 0.05

    # 사이즈 프록시(시가총액 NULL이라 60일 평균 거래대금으로 대체 — 시총과는 다름, 유동성 규모 프록시)
    size_rank = df.groupby("date")["value_ma20"].rank(pct=True)
    df["cond_small_proxy"] = size_rank <= 0.2
    df["cond_large_proxy"] = size_rank >= 0.8

    # 변동성 구간 (20일 일간수익률 표준편차)
    daily_ret = g["close"].transform(lambda s: s.pct_change())
    vol20 = daily_ret.groupby(df["symbol"]).transform(lambda s: s.rolling(20).std())
    df["volatility_20"] = vol20
    vol_rank = df.groupby("date")["volatility_20"].rank(pct=True)
    df["cond_low_vol"] = vol_rank <= 0.25
    df["cond_high_vol"] = vol_rank >= 0.75

    return df


def load_dividend_pit(conn: sqlite3.Connection, prices: pd.DataFrame) -> pd.Series:
    """배당수익률을 point-in-time으로: biz_year Y 공시는 Y+1년 4/1부터 '안 사실'로 취급
    (실제 정기보고서 제출기한 근사). 반환: prices 인덱스에 맞춘 cond_high_div 불리언."""
    div = pd.read_sql_query(
        "SELECT symbol, biz_year, dividend_yield FROM dividends WHERE dividend_yield IS NOT NULL", conn
    )
    div["known_from"] = (div["biz_year"] + 1).astype(str) + "0401"

    out = pd.Series(False, index=prices.index)
    div_yield_col = pd.Series(np.nan, index=prices.index)
    dates = prices["date"].values
    symbols = prices["symbol"].values

    # 종목별로 "그 날짜 기준 가장 최근에 공시된" 배당수익률 매핑
    div_sorted = div.sort_values(["symbol", "known_from"])
    for sym, grp in div_sorted.groupby("symbol"):
        mask = symbols == sym
        if not mask.any():
            continue
        idx = np.where(mask)[0]
        d_for_sym = dates[idx]
        known_from = grp["known_from"].values
        yields = grp["dividend_yield"].values
        pos = np.searchsorted(known_from, d_for_sym, side="right") - 1
        valid = pos >= 0
        div_yield_col.iloc[idx[valid]] = yields[pos[valid]]

    prices = prices.copy()
    prices["_div_yield_pit"] = div_yield_col
    rank = prices.groupby("date")["_div_yield_pit"].rank(pct=True)
    return (rank >= 0.8) & prices["_div_yield_pit"].notna()


def load_tier3(conn: sqlite3.Connection, prices: pd.DataFrame) -> Dict[str, pd.Series]:
    """PER/PBR/외국인 순매수 — raw 테이블, 실제 연속구간(2026-05-12~06-19)만 유효."""
    fund = pd.read_sql_query("SELECT symbol, date, per, pbr FROM fundamentals WHERE per IS NOT NULL OR pbr IS NOT NULL", conn)
    flows = pd.read_sql_query("SELECT symbol, date, foreign_net FROM flows WHERE foreign_net IS NOT NULL", conn)

    merged = prices[["symbol", "date"]].merge(fund, on=["symbol", "date"], how="left")
    merged = merged.merge(flows, on=["symbol", "date"], how="left")

    per_rank = merged.groupby("date")["per"].rank(pct=True)
    pbr_rank = merged.groupby("date")["pbr"].rank(pct=True)
    cond_low_per = (per_rank <= 0.2) & (merged["per"] > 0)  # 적자(<=0) 제외
    cond_low_pbr = pbr_rank <= 0.2

    merged["foreign_chg"] = merged.groupby("symbol")["foreign_net"].diff()
    merged["date_dt"] = pd.to_datetime(merged["date"], format="%Y%m%d")
    gap = merged.groupby("symbol")["date_dt"].diff().dt.days
    merged.loc[gap > 3, "foreign_chg"] = np.nan
    chg_pos = merged["foreign_chg"] > 0
    streak3 = (
        merged.groupby("symbol")["foreign_chg"].transform(lambda s: (s > 0).rolling(3).sum() == 3)
        & merged.groupby("symbol")["foreign_chg"].transform(lambda s: s.notna().rolling(3).sum() == 3)
    )

    valid_window = merged["date"].between("20260512", "20260619")
    return {
        "저PER(20%)": cond_low_per.reindex(prices.index, fill_value=False) & valid_window.reindex(prices.index, fill_value=False),
        "저PBR(20%)": cond_low_pbr.reindex(prices.index, fill_value=False) & valid_window.reindex(prices.index, fill_value=False),
        "외국인순매수(3일연속)": streak3.reindex(prices.index, fill_value=False) & valid_window.reindex(prices.index, fill_value=False),
    }


def load_kospi_regime(conn: sqlite3.Connection) -> pd.Series:
    df = pd.read_sql_query(
        "SELECT date, close FROM market_index WHERE code='1001' AND date BETWEEN ? AND ? ORDER BY date",
        conn, params=(DATA_START, DATA_END),
    )
    df["close"] = df["close"].astype(float)
    df["ret_20d"] = df["close"].pct_change(20)
    regime = np.where(df["ret_20d"] > 0.03, "강세", np.where(df["ret_20d"] < -0.03, "약세", "횡보"))
    return pd.Series(regime, index=df["date"])


# ── 통계 ──────────────────────────────────────────────────────

def _test_group(returns: pd.Series) -> Tuple[Optional[float], Optional[float], Optional[float], int]:
    r = returns.dropna()
    n = len(r)
    if n < 10:
        return None, None, None, n
    mean = float(r.mean() * 100)
    win = float((r > 0).mean())
    _, p = stats.ttest_1samp(r, 0)
    return mean, win, float(p), n


def evaluate_condition(df: pd.DataFrame, cond_col, horizon: str, years: List[int]) -> Dict:
    mask_year = df["date"].str[:4].astype(int).isin(years)
    liquid = df["liquid"] if "liquid" in df.columns else pd.Series(True, index=df.index)
    base = df[mask_year & liquid]
    cond = cond_col.reindex(df.index, fill_value=False)
    sat = base[cond.loc[base.index]]
    unsat = base[~cond.loc[base.index]]

    sat_mean, sat_win, sat_p, sat_n = _test_group(sat[horizon])
    unsat_mean, unsat_win, unsat_p, unsat_n = _test_group(unsat[horizon])
    diff_p = None
    if sat_n >= 10 and unsat_n >= 10:
        _, diff_p = stats.ttest_ind(sat[horizon].dropna(), unsat[horizon].dropna(), equal_var=False)
    return {
        "n_sat": sat_n, "mean_sat_pct": round(sat_mean, 3) if sat_mean is not None else None,
        "win_sat": round(sat_win, 3) if sat_win is not None else None,
        "n_unsat": unsat_n, "mean_unsat_pct": round(unsat_mean, 3) if unsat_mean is not None else None,
        "win_unsat": round(unsat_win, 3) if unsat_win is not None else None,
        "diff_p": round(diff_p, 4) if diff_p is not None else None,
        "sat_vs_zero_p": round(sat_p, 4) if sat_p is not None else None,
    }


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/4] 데이터 로드 + Tier1 조건 계산 (prices 기반)"); print("=" * 70)
    prices = load_prices(conn)
    print(f"유니버스: {prices['symbol'].nunique()}종목, {len(prices)}행")
    prices = build_price_conditions(prices)
    print(f"완료 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/4] Tier2(배당) / Tier3(PER·PBR·외국인) 조건 계산"); print("=" * 70)
    prices["cond_high_div"] = load_dividend_pit(conn, prices)
    tier3 = load_tier3(conn, prices)
    kospi_regime = load_kospi_regime(conn)
    conn.close()
    print(f"완료 ({time.time()-t0:.0f}s)\n")

    conditions_tier1 = [
        ("거래량급증(60일대비3x)", "cond_vol_surge"),
        ("거래대금 상위20%", "cond_value_top"),
        ("52주신고가 근접(95%+)", "cond_near_high"),
        ("모멘텀 상위20%(20일수익률)", "cond_momentum_top"),
        ("역추세 후보 하위20%(20일수익률)", "cond_momentum_bottom"),
        ("RSI14 과매도(<30)", "cond_rsi_oversold"),
        ("볼린저 하단터치(bb_pct<=0.05)", "cond_bb_lower"),
        ("소형(저유동성 프록시)", "cond_small_proxy"),
        ("대형(고유동성 프록시)", "cond_large_proxy"),
        ("저변동성 구간(Q1)", "cond_low_vol"),
        ("고변동성 구간(Q4)", "cond_high_vol"),
        ("고배당(상위20%, point-in-time)", "cond_high_div"),
    ]

    print("=" * 70); print("[3/4] 조건별 검증 — in-sample(2022-24) vs out-of-sample(2025-26)"); print("=" * 70)
    rows = []
    for label, col in conditions_tier1:
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            is_r = evaluate_condition(prices, prices[col], horizon, IN_SAMPLE_YEARS)
            oos_r = evaluate_condition(prices, prices[col], horizon, OOS_YEARS)
            is_sig = is_r["sat_vs_zero_p"] is not None and is_r["sat_vs_zero_p"] < BONFERRONI_ALPHA
            oos_sig = oos_r["sat_vs_zero_p"] is not None and oos_r["sat_vs_zero_p"] < BONFERRONI_ALPHA
            same_sign = (
                is_r["mean_sat_pct"] is not None and oos_r["mean_sat_pct"] is not None
                and np.sign(is_r["mean_sat_pct"]) == np.sign(oos_r["mean_sat_pct"])
            )
            verdict = "✅ 진짜 edge" if (is_sig and oos_sig and same_sign) else (
                "⚠️ 약함/불일치" if (is_r["diff_p"] and is_r["diff_p"] < 0.05) else "❌ 무의미")
            rows.append({
                "조건": label, "기간": hname,
                "IS_평균%": is_r["mean_sat_pct"], "IS_승률": is_r["win_sat"], "IS_n": is_r["n_sat"],
                "OOS_평균%": oos_r["mean_sat_pct"], "OOS_승률": oos_r["win_sat"], "OOS_n": oos_r["n_sat"],
                "불만족군평균%(OOS)": oos_r["mean_unsat_pct"],
                "판정": verdict,
            })
    result_df = pd.DataFrame(rows)
    print(result_df.to_string(index=False))
    print()

    print("=" * 70); print("[Tier3 — ⚠️ 데이터 빈약(6주만), 참고용 단일구간만]"); print("=" * 70)
    rows3 = []
    for label, cond in tier3.items():
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            r = evaluate_condition(prices, cond, horizon, [2026])
            rows3.append({"조건": label, "기간": hname, "평균%": r["mean_sat_pct"], "승률": r["win_sat"],
                          "n": r["n_sat"], "p(vs0)": r["sat_vs_zero_p"]})
    print(pd.DataFrame(rows3).to_string(index=False))
    print("⚠️ 표본기간이 6주뿐이라 in-sample/OOS 분리 불가 — 결론 보류 권장\n")

    print("=" * 70); print("[4/4] 시장상황별(강세/횡보/약세) — OOS 기준 상위 후보만"); print("=" * 70)
    prices["regime"] = prices["date"].map(kospi_regime)
    candidates = ["cond_momentum_top", "cond_momentum_bottom", "cond_rsi_oversold", "cond_bb_lower", "cond_near_high"]
    for col in candidates:
        label = next(l for l, c in conditions_tier1 if c == col)
        print(f"--- {label} (5일 수익률, OOS 2025-26) ---")
        sub = prices[prices["date"].str[:4].astype(int).isin(OOS_YEARS) & prices["liquid"]]
        for regime in ["강세", "횡보", "약세"]:
            grp = sub[(sub["regime"] == regime) & sub[col]]
            mean, win, p, n = _test_group(grp["ret_fwd_5d"])
            print(f"  {regime}: n={n}, 평균={mean if mean is not None else float('nan'):.2f}%, "
                  f"승률={win if win is not None else float('nan'):.1%}, p={p}")
    print()

    out = Path(__file__).parent / "factor_screen_validation_results.csv"
    result_df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"결과 저장: {out}")
    print(f"총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
