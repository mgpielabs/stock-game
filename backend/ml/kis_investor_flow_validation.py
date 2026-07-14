"""
[검증 전용 — 운영 코드/모델 미사용, DB는 SELECT만] KIS 투자자매매동향(외국인/기관/개인
순매수) 연속 신호 → forward 수익률 가설 검증 (2026-06-27).

⚠️ 표본 제약: investor_trading_kis는 KIS API 특성상 최근 ~30거래일 롤링 윈도우만 있음
(2026-05-14~06-26, 정확히 30거래일). multi-year IS/OOS 워크포워드 불가 — 이 검증은
"방향성 참고용" 단일 기간 체크. N일 연속 스트릭을 요구하면 그만큼 시작 구간이 깎이고,
forward 10일 수익률을 보려면 끝 구간도 깎여서 실제 신호 발생 가능 날짜는 30일보다
훨씬 적음 — 표본 크기를 반드시 같이 확인할 것.

가설 6종 (N=3,5):
  1. 외국인 N일 연속 순매수 → 5/10일 수익률
  2. 외국인 N일 연속 순매도 → 5/10일 수익률
  3. 기관 N일 연속 순매수 → 5/10일 수익률
  4. 기관 N일 연속 순매도 → 5/10일 수익률
  5. 외국인+기관 동시 순매수 & 개인 순매도(당일) → 5/10일 수익률
  6. 외국인+기관 동시 순매도 & 개인 순매수(당일) → 5/10일 수익률

방법: 신호 만족 종목의 forward 수익률 vs "같은 날짜 전체(유동성 통과) 종목 평균"
(diff, 승률, 중앙값). 통계검정(t-test)도 참고로 계산하지만 표본이 작아 방향성 위주로
해석.

실행: python kis_investor_flow_validation.py
출력: kis_investor_flow_validation_results.csv
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

import sqlite3
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from factor_screen_validation import ETF_PATTERN

pd.set_option("display.width", 160)

MIN_VALUE_KRW = 1_000_000_000  # 최소 일 거래대금 10억원 (저유동성 제외, evaluate.py와 동일 기준)


def load_data(conn: sqlite3.Connection) -> pd.DataFrame:
    names = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)
    etf_syms = set(names.loc[names["name"].fillna("").str.contains(ETF_PATTERN), "symbol"])

    inv = pd.read_sql_query(
        "SELECT symbol, date, foreign_net_qty, inst_net_qty, indiv_net_qty "
        "FROM investor_trading_kis WHERE date >= '20260514'",
        conn,
    )
    inv = inv[~inv["symbol"].isin(etf_syms)]

    prices = pd.read_sql_query(
        "SELECT symbol, date, close, volume FROM prices WHERE date >= '20260414'",
        conn,
    )
    prices = prices[~prices["symbol"].isin(etf_syms)]
    prices["close"] = prices["close"].astype(float)
    prices["volume"] = prices["volume"].astype(float)
    prices["value"] = prices["close"] * prices["volume"]

    df = prices.merge(inv, on=["symbol", "date"], how="left").sort_values(["symbol", "date"])
    g = df.groupby("symbol", group_keys=False)
    df["value_ma20"] = g["value"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    df["ret_fwd_5d"] = g["close"].transform(lambda s: s.shift(-5) / s - 1)
    df["ret_fwd_10d"] = g["close"].transform(lambda s: s.shift(-10) / s - 1)
    df["liquid"] = df["value_ma20"] >= MIN_VALUE_KRW

    # N일 연속 스트릭 (날짜 연속성 깨지면 무효화 — 가격 데이터는 매일 있지만 안전장치로 유지)
    df["date_dt"] = pd.to_datetime(df["date"], format="%Y%m%d")
    gap = g["date_dt"].diff().dt.days
    df["gap_ok"] = (gap <= 5) | gap.isna()

    return df


def streak(df: pd.DataFrame, col: str, positive: bool, n: int) -> pd.Series:
    cond = (df[col] > 0) if positive else (df[col] < 0)
    valid = df[col].notna() & df["gap_ok"]
    flag = cond & valid
    flag_sum = flag.groupby(df["symbol"]).transform(lambda s: s.rolling(n).sum())
    valid_sum = valid.groupby(df["symbol"]).transform(lambda s: s.rolling(n).sum())
    return (flag_sum == n) & (valid_sum == n)


def summarize(df: pd.DataFrame, cond: pd.Series, horizon: str) -> dict:
    base = df[df["liquid"]]
    cond = cond.reindex(df.index, fill_value=False)
    sat = base[cond.loc[base.index]][horizon].dropna()
    all_same = base[horizon].dropna()  # 전체(유동성 통과) 종목 평균 — 같은 기간 전체 분포
    n = len(sat)
    if n < 3:
        return {"n": n, "mean": None, "median": None, "win": None, "baseline_mean": None,
                "excess": None, "p": None}
    mean = sat.mean() * 100
    median = sat.median() * 100
    win = (sat > 0).mean()
    baseline_mean = all_same.mean() * 100
    excess = mean - baseline_mean
    p = None
    if n >= 5:
        _, p = stats.ttest_1samp(sat, 0)
    return {"n": n, "mean": round(mean, 3), "median": round(median, 3), "win": round(win, 3),
            "baseline_mean": round(baseline_mean, 3), "excess": round(excess, 3),
            "p": round(p, 4) if p is not None else None}


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    print("=" * 70); print("[1/2] 데이터 로드"); print("=" * 70)
    df = load_data(conn)
    conn.close()
    print(f"행 수: {len(df)}, 종목 수: {df['symbol'].nunique()}")
    print(f"유동성 통과(거래대금 10억+) 행 수: {df['liquid'].sum()}")

    print("\n" + "=" * 70); print("[2/2] 가설별 검증"); print("=" * 70)

    rows = []
    for n in (3, 5):
        f_buy = streak(df, "foreign_net_qty", True, n)
        f_sell = streak(df, "foreign_net_qty", False, n)
        i_buy = streak(df, "inst_net_qty", True, n)
        i_sell = streak(df, "inst_net_qty", False, n)

        conditions = [
            (f"외국인 {n}일연속 순매수", f_buy),
            (f"외국인 {n}일연속 순매도", f_sell),
            (f"기관 {n}일연속 순매수", i_buy),
            (f"기관 {n}일연속 순매도", i_sell),
        ]
        for label, cond in conditions:
            for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_10d", "10일")]:
                r = summarize(df, cond, horizon)
                rows.append({"가설": label, "기간": hname, **r})

    # 5,6: 외국인+기관 동시(당일, 스트릭 아님) & 개인 반대방향
    both_buy = (df["foreign_net_qty"] > 0) & (df["inst_net_qty"] > 0) & (df["indiv_net_qty"] < 0)
    both_sell = (df["foreign_net_qty"] < 0) & (df["inst_net_qty"] < 0) & (df["indiv_net_qty"] > 0)
    for label, cond in [
        ("외국인+기관 동시순매수(개인순매도)", both_buy),
        ("외국인+기관 동시순매도(개인순매수)", both_sell),
    ]:
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_10d", "10일")]:
            r = summarize(df, cond, horizon)
            rows.append({"가설": label, "기간": hname, **r})

    result_df = pd.DataFrame(rows)
    print(result_df.to_string(index=False))

    out = Path(__file__).parent / "kis_investor_flow_validation_results.csv"
    result_df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n결과 저장: {out}")
    print(f"총 소요시간: {time.time()-t0:.0f}s")
    print("\n⚠️ 30거래일 단일기간, IS/OOS 불가 — 방향성 참고용. 표본 n이 작은 행은 신뢰 낮음.")


if __name__ == "__main__":
    main()
