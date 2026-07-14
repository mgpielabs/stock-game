"""
[분석 전용 — 점수화/추천 로직 없음] 수급 패턴 1단계 탐색

1단계 데이터 소스 확인 결과(스크립트 실행 시점 기준):
  - pykrx 거래주체별 매매 API(get_market_trading_value_by_date 등)는 배당 데이터 때와
    동일하게 KRX 로그인 요구로 막혀 있음 (전부 빈 DataFrame, "KRX_ID/KRX_PW 환경변수
    설정 안 됨" 에러). 외국인/기관 "순매수 금액"은 현재 수집 불가.
  - 단, backend/data/collector.py가 이미 매일 네이버 증권 기반으로 `flows.foreign_net`
    (외국인 보유비율 %)를 수집 중 — 2025-05-07 ~ 현재, 3980종목. 일별 변화량으로
    외국인 순매수 "방향"은 근사 가능 (금액/수량은 아님). 신규 수집 없이 바로 탐색 가능.
  - 기관 순매수(inst_net)는 컬럼만 있고 미수집 — 대안(KRX 정보데이터시스템 등)은
    로그인/크롤링이 필요해 본 1단계 범위 밖.

2단계: prices(거래량)만으로 검증 가능한 패턴 A~D.
부록(E): 1단계에서 확인된 flows.foreign_net으로 외국인 보유비율 변화 패턴 탐색.

전부 read-only, 새 데이터 수집 없음, 기존 운영 테이블 미변경.
실행: python analyze_volume_flow_patterns.py
"""

import sqlite3
import sys
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
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH

pd.set_option("display.width", 140)

ANALYSIS_START = "20220101"
ANALYSIS_END = "20251231"
IN_SAMPLE_YEARS = [2022, 2023, 2024]
OOS_YEARS = [2025]


def load_prices(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT symbol, date, close, volume FROM prices WHERE date BETWEEN ? AND ? ORDER BY symbol, date",
        conn, params=(ANALYSIS_START, ANALYSIS_END),
    )
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["year"] = df["date"].str[:4].astype(int)
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("symbol")
    df["vol_ma20"] = g["volume"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["vol_ratio"] = df["volume"] / df["vol_ma20"].replace(0, np.nan)
    df["value"] = df["volume"] * df["close"]
    df["value_ma20"] = g["value"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["value_ratio"] = df["value"] / df["value_ma20"].replace(0, np.nan)
    df["ret_1d"] = g["close"].transform(lambda s: s.pct_change())
    df["ret_fwd1"] = g["close"].transform(lambda s: s.shift(-1) / s - 1)
    df["ret_fwd5"] = g["close"].transform(lambda s: s.shift(-5) / s - 1)
    # 벤치마크는 KOSPI(시총가중) 대신 "같은 날 전체 종목의 동일가중 평균 수익률"을 사용.
    # KOSPI 대비로 하면 2025년에 평범한 종목(거래량과 무관)도 전부 -0.7%p가 나오는
    # 레짐성 왜곡이 섞여(아래 검증으로 발견) "거래량 신호"처럼 보이는 착시가 생김.
    cross_avg_fwd1 = df.groupby("date")["ret_fwd1"].transform("mean")
    cross_avg_fwd5 = df.groupby("date")["ret_fwd5"].transform("mean")
    df["excess_fwd1"] = df["ret_fwd1"] - cross_avg_fwd1
    df["excess_fwd5"] = df["ret_fwd5"] - cross_avg_fwd5
    # 과거 5일 거래량비율 추세 (점진적 누적 vs 급발생 구분용)
    df["vol_ratio_3d_ago"] = g["vol_ratio"].transform(lambda s: s.shift(3))
    return df


def _test(sub: pd.DataFrame, col: str):
    s = sub[col].dropna()
    if len(s) < 5:
        return None, None
    t, p = stats.ttest_1samp(s, 0)
    return round(s.mean() * 100, 4), round(p, 4)


def _split_consistency(sub: pd.DataFrame, col: str):
    """in-sample/OOS 양쪽에서 같은 방향(+ 또는 -)으로 유의한지 확인"""
    isamp = sub[sub["year"].isin(IN_SAMPLE_YEARS)]
    oos = sub[sub["year"].isin(OOS_YEARS)]
    m_is, p_is = _test(isamp, col)
    m_oos, p_oos = _test(oos, col)
    consistent = (
        m_is is not None and m_oos is not None
        and np.sign(m_is) == np.sign(m_oos)
        and p_is is not None and p_is < 0.05
        and p_oos is not None and p_oos < 0.05
    )
    return {"in_sample_mean_pct": m_is, "in_sample_p": p_is,
            "oos_mean_pct": m_oos, "oos_p": p_oos, "일관됨(양쪽유의+동일방향)": consistent}


def pattern_A_volume_surge(df: pd.DataFrame):
    rows = []
    thresholds = [1.5, 2.0, 3.0, 5.0]
    base = df[(df["vol_ratio"].notna()) & (df["vol_ratio"] < 1.0)]
    for lo, hi in zip([None] + thresholds, thresholds + [None]):
        if lo is None:
            sub = df[df["vol_ratio"] < hi]
            label = f"<{hi}x"
        elif hi is None:
            sub = df[df["vol_ratio"] >= lo]
            label = f">={lo}x"
        else:
            sub = df[(df["vol_ratio"] >= lo) & (df["vol_ratio"] < hi)]
            label = f"{lo}~{hi}x"
        n = len(sub)
        m1, p1 = _test(sub, "excess_fwd1")
        m5, p5 = _test(sub, "excess_fwd5")
        rows.append({"거래량비율": label, "n": n,
                     "익일초과수익_pct": m1, "익일p": p1,
                     "5일초과수익_pct": m5, "5일p": p5})
    out = pd.DataFrame(rows)

    consist_rows = []
    for thr in thresholds:
        sub = df[df["vol_ratio"] >= thr]
        c = _split_consistency(sub, "excess_fwd5")
        c["threshold"] = f">={thr}x"
        consist_rows.append(c)
    consist = pd.DataFrame(consist_rows)
    return out, consist


def pattern_B_surge_direction(df: pd.DataFrame, thr: float = 2.0):
    surge = df[(df["vol_ratio"] >= thr) & df["ret_1d"].notna()]
    up = surge[surge["ret_1d"] > 0]
    down = surge[surge["ret_1d"] < 0]
    rows = []
    for name, sub in [("거래량급증+가격상승(정배열)", up), ("거래량급증+가격하락(이상신호)", down)]:
        m5, p5 = _test(sub, "excess_fwd5")
        rows.append({"조합": name, "n": len(sub), "5일초과수익_pct": m5, "p": p5})
    out = pd.DataFrame(rows)

    u, d = up["excess_fwd5"].dropna(), down["excess_fwd5"].dropna()
    diff_p = stats.ttest_ind(u, d, equal_var=False).pvalue if len(u) > 1 and len(d) > 1 else None

    consist = []
    for name, sub in [("상승+급증", up), ("하락+급증", down)]:
        c = _split_consistency(sub, "excess_fwd5")
        c["조합"] = name
        consist.append(c)
    return out, round(diff_p, 4) if diff_p is not None else None, pd.DataFrame(consist)


def pattern_C_buildup_vs_spike(df: pd.DataFrame):
    valid = df[df["vol_ratio"].notna() & df["vol_ratio_3d_ago"].notna()]
    # 점진적 누적: 3일 전부터 거래량비율이 1.2~2.5 구간을 유지하며 증가
    gradual = valid[
        (valid["vol_ratio_3d_ago"] >= 1.1) & (valid["vol_ratio_3d_ago"] < 2.0)
        & (valid["vol_ratio"] >= 1.2) & (valid["vol_ratio"] < 2.5)
        & (valid["vol_ratio"] > valid["vol_ratio_3d_ago"])
    ]
    # 급발생: 3일 전엔 평범(<1.2)했다가 지금 갑자기 3배 이상
    sudden = valid[(valid["vol_ratio_3d_ago"] < 1.2) & (valid["vol_ratio"] >= 3.0)]

    rows = []
    for name, sub in [("점진적 누적", gradual), ("급발생(스파이크)", sudden)]:
        m5, p5 = _test(sub, "excess_fwd5")
        rows.append({"유형": name, "n": len(sub), "5일초과수익_pct": m5, "p": p5})
    out = pd.DataFrame(rows)

    g_, s_ = gradual["excess_fwd5"].dropna(), sudden["excess_fwd5"].dropna()
    diff_p = stats.ttest_ind(g_, s_, equal_var=False).pvalue if len(g_) > 1 and len(s_) > 1 else None

    consist = []
    for name, sub in [("점진적누적", gradual), ("급발생", sudden)]:
        c = _split_consistency(sub, "excess_fwd5")
        c["유형"] = name
        consist.append(c)
    return out, round(diff_p, 4) if diff_p is not None else None, pd.DataFrame(consist)


def pattern_D_value_vs_volume(df: pd.DataFrame, thr: float = 2.0):
    vol_surge = df[df["vol_ratio"] >= thr]
    val_surge = df[df["value_ratio"] >= thr]
    both = df[(df["vol_ratio"] >= thr) & (df["value_ratio"] >= thr)]
    val_only = df[(df["value_ratio"] >= thr) & (df["vol_ratio"] < thr)]  # 가격 상승으로 거래대금만 튄 경우

    rows = []
    for name, sub in [("거래량비율>=2x", vol_surge), ("거래대금비율>=2x", val_surge),
                       ("둘다 충족", both), ("거래대금만 충족(가격상승 영향)", val_only)]:
        m5, p5 = _test(sub, "excess_fwd5")
        rows.append({"기준": name, "n": len(sub), "5일초과수익_pct": m5, "p": p5})
    return pd.DataFrame(rows)


def overlap_note():
    return (
        "참고: 모델은 이미 vol_ratio_5d, vol_ratio_20d, vol_surge(vr5>2.0), "
        "vol_ratio_lag_{1,3,5}를 피처로 사용 중(features/technical.py). "
        "패턴 A(단순 거래량 급증 임계값)는 이미 모델이 보는 정보와 거의 동일 — "
        "유의하더라도 '새 신호'가 아니라 기존 피처의 재확인일 가능성이 큼. "
        "패턴 B(급증+방향 조합), C(점진적 누적 vs 스파이크 구분), D(거래대금 비율)는 "
        "현재 피처셋에 명시적으로 없는 조합/변형이라 유의하면 신규 정보일 수 있음."
    )


def analyze_foreign_flow(conn: sqlite3.Connection):
    flows = pd.read_sql_query(
        "SELECT symbol, date, foreign_net FROM flows WHERE foreign_net IS NOT NULL ORDER BY symbol, date", conn
    )
    if flows.empty:
        return None, None, "flows 데이터 없음"
    start, end = flows["date"].min(), flows["date"].max()

    prices = pd.read_sql_query(
        "SELECT symbol, date, close FROM prices WHERE date BETWEEN ? AND ? ORDER BY symbol, date",
        conn, params=(start, end),
    )
    prices["close"] = prices["close"].astype(float)
    merged = flows.merge(prices, on=["symbol", "date"], how="inner")
    if merged.empty:
        return None, None, f"flows({start}~{end})와 prices 교차 데이터 없음"

    merged = merged.sort_values(["symbol", "date"])
    merged["date_dt"] = pd.to_datetime(merged["date"], format="%Y%m%d")
    g = merged.groupby("symbol")
    merged["gap_days"] = g["date_dt"].transform(lambda s: s.diff().dt.days)
    # 수집 공백(연속 거래일이 아닌 구간)을 낀 diff는 제외 — 한 종목에 1년짜리 공백이
    # 있는 등 flows 수집이 불연속이라(평균 2일 간격이지만 최대 365일 공백 확인됨),
    # gap<=3일(주말/공휴일 포함 허용)인 행만 "진짜 연속 관찰"로 인정
    merged["foreign_chg"] = g["foreign_net"].transform(lambda s: s.diff())
    merged.loc[merged["gap_days"] > 3, "foreign_chg"] = np.nan
    merged["ret_fwd5"] = g["close"].transform(lambda s: s.shift(-5) / s - 1)
    # 3일 연속 외국인 보유비율 상승 (공백 낀 구간은 streak로 인정 안 함)
    merged["chg_pos"] = merged["foreign_chg"] > 0
    merged["chg_valid"] = merged["foreign_chg"].notna()
    merged["streak3"] = (
        g["chg_pos"].transform(lambda s: s.fillna(False).rolling(3).sum() == 3)
        & g["chg_valid"].transform(lambda s: s.rolling(3).sum() == 3)
    )

    rising = merged[merged["streak3"] == True]
    falling_streak = (
        g["chg_pos"].transform(lambda s: (~s.fillna(True)).rolling(3).sum() == 3)
        & g["chg_valid"].transform(lambda s: s.rolling(3).sum() == 3)
    )
    falling = merged[falling_streak == True]
    print(f"  (공백 제외 후 유효 diff 비율: {merged['chg_valid'].mean():.1%}, "
          f"실질 연속관찰 구간만 사용)")

    rows = []
    for name, sub in [("외국인보유비율 3일연속 상승", rising), ("외국인보유비율 3일연속 하락", falling)]:
        m5, p5 = _test(sub, "ret_fwd5")
        rows.append({"패턴": name, "n": len(sub), "5일수익률_pct": m5, "p": p5})
    out = pd.DataFrame(rows)
    diff_p = None
    r, f = rising["ret_fwd5"].dropna(), falling["ret_fwd5"].dropna()
    if len(r) > 1 and len(f) > 1:
        diff_p = round(stats.ttest_ind(r, f, equal_var=False).pvalue, 4)

    return out, diff_p, f"flows 가용 구간: {start}~{end} ({merged['symbol'].nunique()}종목)"


def main():
    print("=" * 70)
    print("[1단계] 데이터 소스 확인 결과 (스크립트 docstring 참고)")
    print("=" * 70)
    print("- pykrx 거래주체별 매매(외국인/기관 순매수 금액) API: KRX 로그인 요구로 차단됨")
    print("- 대안: flows.foreign_net (외국인 보유비율, 네이버 증권 기반, 이미 일별 수집중)")
    print("- 기관 순매수(inst_net): 컬럼만 존재, 미수집 (1단계 범위 밖)")
    print()

    conn = sqlite3.connect(DB_PATH)
    prices = load_prices(conn)
    print(f"분석 구간: {ANALYSIS_START}~{ANALYSIS_END}, 종목 {prices['symbol'].nunique()}개, 행 {len(prices)}개")
    df = build_features(prices)
    print()

    print("=" * 70); print("[2단계-A] 거래량 급증 임계값별 익일/5일 초과수익률"); print("=" * 70)
    a_out, a_consist = pattern_A_volume_surge(df)
    print(a_out.to_string(index=False)); print()
    print("임계값별 in-sample/OOS 일관성 (5일 초과수익 기준):")
    print(a_consist.to_string(index=False)); print()

    print("=" * 70); print("[2단계-B] 거래량급증 + 가격방향 조합"); print("=" * 70)
    b_out, b_diff_p, b_consist = pattern_B_surge_direction(df)
    print(b_out.to_string(index=False))
    print(f"두 그룹 차이 p-value: {b_diff_p}")
    print(b_consist.to_string(index=False)); print()

    print("=" * 70); print("[2단계-C] 점진적 누적 vs 급발생(스파이크)"); print("=" * 70)
    c_out, c_diff_p, c_consist = pattern_C_buildup_vs_spike(df)
    print(c_out.to_string(index=False))
    print(f"두 그룹 차이 p-value: {c_diff_p}")
    print(c_consist.to_string(index=False)); print()

    print("=" * 70); print("[2단계-D] 거래량 비율 vs 거래대금 비율"); print("=" * 70)
    d_out = pattern_D_value_vs_volume(df)
    print(d_out.to_string(index=False)); print()

    print(overlap_note()); print()

    print("=" * 70); print("[부록-E] 외국인 보유비율 변화 패턴 (flows.foreign_net, 신규수집 없음)"); print("=" * 70)
    e_out, e_diff_p, e_note = analyze_foreign_flow(conn)
    print(e_note)
    if e_out is not None:
        print(e_out.to_string(index=False))
        print(f"두 그룹 차이 p-value: {e_diff_p}")
    print()

    conn.close()


if __name__ == "__main__":
    main()
