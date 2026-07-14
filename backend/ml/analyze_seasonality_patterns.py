"""
[분석 전용 — 점수화/추천 로직 없음] 계절성/캘린더 패턴 탐색

이미 있는 market_index(KOSPI/KOSDAQ 지수) + prices(개별 종목 시세)만 사용,
새 데이터 수집 없음.

패턴:
  A. 월별 효과 (1월효과/산타랠리/5월매도 등)
  B. 월중 패턴 (월초/월말 윈도우드레싱)
  C. 분기말 효과 (3/6/9/12월말 기관 수급)
  D. 명절(연휴) 전후 패턴 — market_index 거래일 캘린더의 4일+ 공백으로 탐지
  E. 요일 효과
  F. 개별 종목 계절성 — in-sample(2022~2024)에서 "3년 연속 양(+)" 패턴을 탐지한 뒤
     out-of-sample(2025)에서 실제로 맞는 비율을 무작위(50%) 기준과 이항검정으로 비교
     → 과적합(우연히 맞은 패턴) 여부를 가려내는 핵심 장치.

실행: python analyze_seasonality_patterns.py
"""

import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH

pd.set_option("display.width", 140)

ANALYSIS_START = "20220103"
ANALYSIS_END = "20260619"
IN_SAMPLE_YEARS = [2022, 2023, 2024]
OOS_YEARS = [2025]


def load_index_returns(conn: sqlite3.Connection, code: str = "1001") -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT date, close FROM market_index WHERE code=? AND date BETWEEN ? AND ? ORDER BY date",
        conn, params=(code, ANALYSIS_START, ANALYSIS_END),
    )
    df["close"] = df["close"].astype(float)
    df["ret"] = df["close"].pct_change()
    dt = pd.to_datetime(df["date"], format="%Y%m%d")
    df["year"] = dt.dt.year
    df["month"] = dt.dt.month
    df["dow"] = dt.dt.dayofweek  # 0=월
    return df.dropna(subset=["ret"]).reset_index(drop=True)


def pattern_A_monthly(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for m in range(1, 13):
        sub = df[df["month"] == m]
        if len(sub) < 5:
            continue
        _, p = stats.ttest_1samp(sub["ret"], 0)
        by_year = sub.groupby("year")["ret"].mean()
        rows.append({
            "month": m, "n_days": len(sub), "n_years": len(by_year),
            "mean_daily_ret_pct": round(sub["ret"].mean() * 100, 4),
            "p_value": round(p, 4),
            "positive_years": f"{(by_year > 0).sum()}/{len(by_year)}",
        })
    return pd.DataFrame(rows)


def pattern_B_intramonth(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["tdom"] = df.groupby(["year", "month"]).cumcount() + 1
    df["days_in_month"] = df.groupby(["year", "month"])["tdom"].transform("max")
    df["from_end"] = df["days_in_month"] - df["tdom"]

    first3 = df[df["tdom"] <= 3]["ret"]
    last3 = df[df["from_end"] <= 2]["ret"]
    mid = df[(df["tdom"] > 3) & (df["from_end"] > 2)]["ret"]

    rows = []
    for name, s in [("월초(1~3거래일)", first3), ("월말(마지막3거래일)", last3), ("월중(나머지)", mid)]:
        if len(s) > 1:
            _, p = stats.ttest_1samp(s, 0)
        else:
            p = np.nan
        rows.append({
            "구간": name, "n": len(s),
            "mean_daily_ret_pct": round(s.mean() * 100, 4),
            "p_value": round(p, 4) if not np.isnan(p) else None,
        })
    return pd.DataFrame(rows)


def pattern_C_quarter_end(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["tdom"] = df.groupby(["year", "month"]).cumcount() + 1
    df["days_in_month"] = df.groupby(["year", "month"])["tdom"].transform("max")
    df["from_end"] = df["days_in_month"] - df["tdom"]
    is_qend_month = df["month"].isin([3, 6, 9, 12])
    q_end = df[is_qend_month & (df["from_end"] <= 2)]["ret"]
    others = df[~(is_qend_month & (df["from_end"] <= 2))]["ret"]
    _, p = stats.ttest_ind(q_end, others, equal_var=False)
    return pd.DataFrame([{
        "n_quarter_end_days": len(q_end),
        "mean_qend_ret_pct": round(q_end.mean() * 100, 4),
        "mean_other_ret_pct": round(others.mean() * 100, 4),
        "diff_pct": round((q_end.mean() - others.mean()) * 100, 4),
        "p_value": round(p, 4),
    }])


def detect_holiday_gaps(df: pd.DataFrame, min_gap_days: int = 4):
    dates = pd.to_datetime(df["date"], format="%Y%m%d").sort_values().reset_index(drop=True)
    gaps = []
    for i in range(1, len(dates)):
        delta = (dates[i] - dates[i - 1]).days
        if delta >= min_gap_days:
            gaps.append((dates[i - 1].strftime("%Y%m%d"), dates[i].strftime("%Y%m%d"), delta))
    return gaps


def pattern_D_holiday(df: pd.DataFrame, gaps, window: int = 3):
    dates = df["date"].tolist()
    rets = df.set_index("date")["ret"]
    rows = []
    for before, after, gap_days in gaps:
        if before not in dates or after not in dates:
            continue
        ib, ia = dates.index(before), dates.index(after)
        pre = rets.iloc[max(0, ib - window + 1): ib + 1]
        post = rets.iloc[ia: ia + window]
        rows.append({
            "gap_start(연휴전 마지막거래일)": before, "gap_end(연휴후 첫거래일)": after,
            "calendar_days": gap_days,
            f"pre_{window}d_ret_pct": round(float((1 + pre).prod() - 1) * 100, 2) if len(pre) else None,
            f"post_{window}d_ret_pct": round(float((1 + post).prod() - 1) * 100, 2) if len(post) else None,
        })
    gap_df = pd.DataFrame(rows)
    summary = {}
    if not gap_df.empty:
        pre_col, post_col = f"pre_{window}d_ret_pct", f"post_{window}d_ret_pct"
        pre_vals, post_vals = gap_df[pre_col].dropna(), gap_df[post_col].dropna()
        summary["n_holidays"] = len(gap_df)
        if len(pre_vals) > 1:
            _, p = stats.ttest_1samp(pre_vals, 0)
            summary["pre_mean_pct"], summary["pre_p"] = round(pre_vals.mean(), 2), round(p, 4)
        if len(post_vals) > 1:
            _, p = stats.ttest_1samp(post_vals, 0)
            summary["post_mean_pct"], summary["post_p"] = round(post_vals.mean(), 2), round(p, 4)
    return gap_df, summary


def pattern_E_dow(df: pd.DataFrame) -> pd.DataFrame:
    names = ["월", "화", "수", "목", "금"]
    rows = []
    for d in range(5):
        sub = df[df["dow"] == d]["ret"]
        if len(sub) < 5:
            continue
        _, p = stats.ttest_1samp(sub, 0)
        rows.append({"요일": names[d], "n": len(sub), "mean_daily_ret_pct": round(sub.mean() * 100, 4), "p_value": round(p, 4)})
    return pd.DataFrame(rows)


def pattern_F_individual_stock(conn: sqlite3.Connection):
    prices = pd.read_sql_query(
        "SELECT symbol, date, close FROM prices WHERE date BETWEEN '20220101' AND '20251231' ORDER BY symbol, date",
        conn,
    )
    prices["close"] = prices["close"].astype(float)
    dt = pd.to_datetime(prices["date"], format="%Y%m%d")
    prices["year"], prices["month"] = dt.dt.year, dt.dt.month

    monthly = prices.groupby(["symbol", "year", "month"])["close"].last().reset_index()
    monthly = monthly.sort_values(["symbol", "year", "month"])
    monthly["ret"] = monthly.groupby("symbol")["close"].pct_change()
    monthly = monthly.dropna(subset=["ret"])

    in_sample = monthly[monthly["year"].isin(IN_SAMPLE_YEARS)]
    oos = monthly[monthly["year"].isin(OOS_YEARS)]

    detected = []
    for symbol, grp in in_sample.groupby("symbol"):
        piv = grp.pivot_table(index="year", columns="month", values="ret")
        if piv.shape[0] < len(IN_SAMPLE_YEARS):
            continue
        for m in range(1, 13):
            if m not in piv.columns:
                continue
            vals = piv[m].dropna()
            if len(vals) < len(IN_SAMPLE_YEARS):
                continue
            if (vals > 0).all():  # in-sample 3년 전부 양(+)인 패턴만 "탐지"로 인정
                detected.append({"symbol": symbol, "month": m, "in_sample_mean_pct": round(vals.mean() * 100, 2)})

    detected_df = pd.DataFrame(detected)
    if detected_df.empty:
        return detected_df, None

    oos_piv = oos.pivot_table(index="symbol", columns="month", values="ret")
    hits = []
    for _, row in detected_df.iterrows():
        sym, m = row["symbol"], row["month"]
        if sym in oos_piv.index and m in oos_piv.columns and pd.notna(oos_piv.loc[sym, m]):
            hits.append(bool(oos_piv.loc[sym, m] > 0))

    summary = None
    if hits:
        n_success, n_tested = sum(hits), len(hits)
        binom_p = stats.binomtest(n_success, n_tested, p=0.5, alternative="greater").pvalue
        summary = {
            "n_detected_patterns(in-sample)": len(detected_df),
            "n_testable_oos": n_tested,
            "oos_hit_rate": round(n_success / n_tested, 3),
            "random_baseline": 0.5,
            "vs_random_p_value": round(binom_p, 4),
        }
    return detected_df, summary


def main():
    conn = sqlite3.connect(DB_PATH)
    kospi = load_index_returns(conn, "1001")
    kosdaq = load_index_returns(conn, "2001")
    print(f"분석 구간: {ANALYSIS_START} ~ {ANALYSIS_END} (KOSPI {len(kospi)}거래일)")
    print()

    print("=" * 70); print("A. 월별 효과 (KOSPI)"); print("=" * 70)
    print(pattern_A_monthly(kospi).to_string(index=False)); print()

    print("=" * 70); print("B. 월중 패턴 — 월초/월말/월중 (KOSPI)"); print("=" * 70)
    print(pattern_B_intramonth(kospi).to_string(index=False)); print()

    print("=" * 70); print("C. 분기말 효과 (KOSPI)"); print("=" * 70)
    print(pattern_C_quarter_end(kospi).to_string(index=False)); print()

    print("=" * 70); print("D. 명절(연휴) 전후 패턴 (KOSPI, ±3거래일)"); print("=" * 70)
    gaps = detect_holiday_gaps(kospi)
    gap_df, d_summary = pattern_D_holiday(kospi, gaps, window=3)
    print(gap_df.to_string(index=False))
    print("요약:", d_summary); print()

    print("=" * 70); print("E. 요일 효과 (KOSPI)"); print("=" * 70)
    print(pattern_E_dow(kospi).to_string(index=False)); print()

    print("=" * 70); print("F. 개별 종목 계절성 (in-sample 2022-2024 → out-of-sample 2025 검증)"); print("=" * 70)
    detected_df, f_summary = pattern_F_individual_stock(conn)
    print(f"in-sample에서 '3년 연속 양(+)' 탐지된 (종목,월) 조합: {len(detected_df)}개")
    print("out-of-sample 검증 요약:", f_summary)
    print()

    conn.close()


if __name__ == "__main__":
    main()
