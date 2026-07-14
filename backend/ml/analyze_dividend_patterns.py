"""
[분석 전용 — 점수화/추천 로직 없음] 배당 신호 탐색적 분석

dividends 테이블(DART alotMatter 기반, backend/data/dividend_collector.py 수집)을
이용해 배당 관련 5가지 패턴이 실제로 존재하고 통계적으로 유의한지 검증한다.

1. 배당락 전 N일 초과수익률 (배당 따먹기 패턴)
2. 고배당 종목의 배당락 후 회복 패턴
3. 배당 성장(매년 DPS 증가) 종목의 주가 안정성/상승
4. 12월 결산 집중도
5. 위 패턴들의 통계적 유의성(t-test)

실행: python analyze_dividend_patterns.py
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH

pd.set_option("display.width", 140)


def load_data(conn: sqlite3.Connection):
    dividends = pd.read_sql_query(
        "SELECT * FROM dividends WHERE ex_dividend_date IS NOT NULL ORDER BY symbol, biz_year", conn
    )
    kospi = pd.read_sql_query(
        "SELECT date, close FROM market_index WHERE code='1001' ORDER BY date", conn
    )
    kospi["close"] = kospi["close"].astype(float)
    kospi = kospi.set_index("date")["close"]
    return dividends, kospi


def price_series(conn: sqlite3.Connection, symbol: str) -> pd.Series:
    df = pd.read_sql_query(
        "SELECT date, close FROM prices WHERE symbol = ? ORDER BY date", conn, params=(symbol,)
    )
    if df.empty:
        return pd.Series(dtype=float)
    df["close"] = df["close"].astype(float)
    return df.set_index("date")["close"]


def ret_over_offset(series: pd.Series, base_date: str, offset: int) -> float:
    """series의 거래일 인덱스 기준 base_date에서 offset 거래일 이동한 지점까지의 수익률.
    offset<0이면 base_date 이전, >0이면 이후. base_date 자체가 시리즈에 없으면 NaN."""
    dates = series.index
    if base_date not in dates:
        return np.nan
    pos = dates.get_loc(base_date)
    target_pos = pos + offset
    if target_pos < 0 or target_pos >= len(dates):
        return np.nan
    if offset < 0:
        return float(series.iloc[pos] / series.iloc[target_pos] - 1)
    return float(series.iloc[target_pos] / series.iloc[pos] - 1)


def analyze_pre_ex_dividend_runup(conn, dividends: pd.DataFrame, kospi: pd.Series, windows=(5, 10, 20)):
    """1. 배당락 전 N일 초과수익률 (배당 따먹기 패턴)"""
    rows = []
    cache: dict = {}
    for _, d in dividends.iterrows():
        sym, ex_date = d["symbol"], d["ex_dividend_date"]
        if sym not in cache:
            cache[sym] = price_series(conn, sym)
        s = cache[sym]
        if s.empty or ex_date not in s.index:
            continue
        for w in windows:
            stock_ret = ret_over_offset(s, ex_date, -w)
            mkt_ret = ret_over_offset(kospi, ex_date, -w) if ex_date in kospi.index else np.nan
            if not np.isnan(stock_ret):
                rows.append({"window": w, "symbol": sym, "biz_year": d["biz_year"],
                             "stock_ret": stock_ret, "mkt_ret": mkt_ret,
                             "excess_ret": stock_ret - mkt_ret if not np.isnan(mkt_ret) else np.nan})
    df = pd.DataFrame(rows)
    out = []
    for w in windows:
        sub = df[df["window"] == w]
        if sub.empty:
            continue
        t_raw, p_raw = stats.ttest_1samp(sub["stock_ret"].dropna(), 0)
        ex = sub["excess_ret"].dropna()
        t_ex, p_ex = (stats.ttest_1samp(ex, 0) if len(ex) > 1 else (np.nan, np.nan))
        out.append({
            "window_days": w, "n": len(sub),
            "mean_raw_ret_pct": round(sub["stock_ret"].mean() * 100, 2),
            "raw_p_value": round(p_raw, 4),
            "mean_excess_ret_pct": round(ex.mean() * 100, 2) if len(ex) else None,
            "excess_p_value": round(p_ex, 4) if not np.isnan(p_ex) else None,
        })
    return pd.DataFrame(out), df


def analyze_post_ex_dividend_recovery(conn, dividends: pd.DataFrame, kospi: pd.Series, windows=(5, 10, 20)):
    """2. 고배당 종목의 배당락 후 회복 패턴 (고배당 vs 저배당 비교)"""
    div_yield = dividends["dividend_yield"].dropna()
    if div_yield.empty:
        return pd.DataFrame(), pd.DataFrame()
    median_yield = div_yield.median()

    rows = []
    cache: dict = {}
    for _, d in dividends.iterrows():
        sym, ex_date = d["symbol"], d["ex_dividend_date"]
        dy = d["dividend_yield"]
        if pd.isna(dy):
            continue
        group = "high_yield" if dy >= median_yield else "low_yield"
        if sym not in cache:
            cache[sym] = price_series(conn, sym)
        s = cache[sym]
        if s.empty or ex_date not in s.index:
            continue
        for w in windows:
            ret = ret_over_offset(s, ex_date, w)
            mkt_ret = ret_over_offset(kospi, ex_date, w) if ex_date in kospi.index else np.nan
            if not np.isnan(ret):
                rows.append({"window": w, "group": group, "symbol": sym, "biz_year": d["biz_year"],
                             "post_ret": ret, "excess_ret": ret - mkt_ret if not np.isnan(mkt_ret) else np.nan,
                             "div_yield": dy})
    df = pd.DataFrame(rows)
    out = []
    for w in windows:
        sub = df[df["window"] == w]
        hi = sub[sub["group"] == "high_yield"]["post_ret"].dropna()
        lo = sub[sub["group"] == "low_yield"]["post_ret"].dropna()
        if len(hi) < 2 or len(lo) < 2:
            continue
        t, p = stats.ttest_ind(hi, lo, equal_var=False)
        out.append({
            "window_days": w, "n_high": len(hi), "n_low": len(lo),
            "high_yield_mean_ret_pct": round(hi.mean() * 100, 2),
            "low_yield_mean_ret_pct": round(lo.mean() * 100, 2),
            "diff_pct": round((hi.mean() - lo.mean()) * 100, 2),
            "p_value": round(p, 4),
        })
    return pd.DataFrame(out), df


def analyze_dividend_growth(conn, dividends: pd.DataFrame, kospi: pd.Series):
    """3. 매년 DPS를 늘리는 종목(배당성장주)의 주가 성과"""
    piv = dividends.pivot_table(index="symbol", columns="biz_year", values="dps")
    years = sorted(piv.columns)
    if len(years) < 3:
        return pd.DataFrame(), pd.DataFrame(), None

    def is_grower(row):
        vals = row[years].values
        if pd.isna(vals).any():
            return False
        return all(vals[i] < vals[i + 1] for i in range(len(vals) - 1))

    piv["is_grower"] = piv.apply(is_grower, axis=1)
    growers = piv[piv["is_grower"]].index.tolist()
    non_growers = piv[~piv["is_grower"]].index.tolist()

    # 각 종목의 최근 사업연도 ex_dividend_date 기준 1년(거래일 약 250일) 수익률
    rows = []
    cache: dict = {}
    last_year = years[-1]
    last_div = dividends[dividends["biz_year"] == last_year].set_index("symbol")
    for sym in piv.index:
        if sym not in last_div.index:
            continue
        ex_date = last_div.loc[sym, "ex_dividend_date"]
        if pd.isna(ex_date):
            continue
        if sym not in cache:
            cache[sym] = price_series(conn, sym)
        s = cache[sym]
        if s.empty or ex_date not in s.index:
            continue
        ret_1y = ret_over_offset(s, ex_date, -250)  # 배당락일로부터 1년 전 대비 수익률(과거 성과)
        if not np.isnan(ret_1y):
            rows.append({"symbol": sym, "is_grower": sym in growers, "ret_1y_pct": ret_1y * 100})

    df = pd.DataFrame(rows)
    summary = None
    if not df.empty:
        g = df[df["is_grower"]]["ret_1y_pct"].dropna()
        ng = df[~df["is_grower"]]["ret_1y_pct"].dropna()
        if len(g) > 1 and len(ng) > 1:
            t, p = stats.ttest_ind(g, ng, equal_var=False)
            summary = {
                "n_growers": len(g), "n_non_growers": len(ng),
                "grower_mean_1y_ret_pct": round(g.mean(), 2),
                "non_grower_mean_1y_ret_pct": round(ng.mean(), 2),
                "p_value": round(p, 4),
            }
    return pd.DataFrame({"growers": [growers]}), df, summary


def analyze_december_concentration(dividends: pd.DataFrame):
    """4. 12월 결산 집중도"""
    valid = dividends.dropna(subset=["settlement_date"])
    months = valid["settlement_date"].str[4:6]
    dist = months.value_counts().sort_index()
    dec_share = float((months == "12").mean()) if len(months) else None
    return dist, dec_share


def main():
    conn = sqlite3.connect(DB_PATH)
    dividends, kospi = load_data(conn)
    print(f"분석 대상 dividends 행수(ex_dividend_date 확정분): {len(dividends)}")
    print(f"유니크 종목수: {dividends['symbol'].nunique()}, 연도범위: {sorted(dividends['biz_year'].unique())}")
    print()

    print("=" * 70)
    print("1. 배당락 전 N일 초과수익률 (배당 따먹기 패턴)")
    print("=" * 70)
    runup_summary, _ = analyze_pre_ex_dividend_runup(conn, dividends, kospi)
    print(runup_summary.to_string(index=False))
    print()

    print("=" * 70)
    print("2. 배당락 후 회복 패턴 (고배당 vs 저배당)")
    print("=" * 70)
    recovery_summary, _ = analyze_post_ex_dividend_recovery(conn, dividends, kospi)
    print(recovery_summary.to_string(index=False))
    print()

    print("=" * 70)
    print("3. 배당성장주(매년 DPS 증가) vs 비성장주 1년 수익률")
    print("=" * 70)
    grower_list, growth_df, growth_summary = analyze_dividend_growth(conn, dividends, kospi)
    print("배당성장 종목 수:", len(grower_list["growers"].iloc[0]) if not grower_list.empty else 0)
    print("요약:", growth_summary)
    print()

    print("=" * 70)
    print("4. 결산월 집중도")
    print("=" * 70)
    dist, dec_share = analyze_december_concentration(dividends)
    print(dist.to_string())
    print(f"12월 결산 비율: {dec_share*100:.1f}%" if dec_share is not None else "데이터 없음")
    print()

    conn.close()


if __name__ == "__main__":
    main()
