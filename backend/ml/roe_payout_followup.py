"""
[검증 전용] "고배당+고ROE" 조합 — 보류 상태 마무리 검증 (2026-06-23)

이전 1차 검증에서 OOS 20일만 확인하고 보류 처리했던 부분을 마저 확인:
  1. IS 자체의 유의성 (compare_combo의 IS 차이_p)
  2. 5일/20일 horizon 일치 여부
  3. 시장 국면별(강세/횡보/약세) 분해 — 같은 날짜의 단독군 vs 조합군 비교라
     market-drift 문제 없음(저PBR 국면분해 때와 달리 cross-sectional 비교라 안전)
  4. 다중비교 보정(이번 4개 검정 — IS/OOS x 5일/20일)
  5. 효과크기 vs 거래비용(evaluate.py 가정 — 왕복 0.63%)

실행: python roe_payout_followup.py (IDLE 우선순위, 운영 데이터 SELECT만)
"""

import sqlite3
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

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from factor_screen_validation import (
    load_prices, build_price_conditions, load_dividend_pit, load_kospi_regime,
    _test_group, IN_SAMPLE_YEARS, OOS_YEARS,
)
from combo_validation import compare_combo
from roe_payout_validation import load_roe_pit
from variant_screen_validation import bh_fdr

pd.set_option("display.width", 160)


def compare_combo_regime(df: pd.DataFrame, cond_a: pd.Series, cond_b: pd.Series,
                          horizon: str, regime_series: pd.Series, regime: str) -> dict:
    """compare_combo와 동일하나 OOS 특정 국면(같은 날짜)으로만 제한 — 단독군/조합군이
    같은 날짜 집합에서 나오므로 시장 드리프트가 자연 통제됨(국면 자체가 그날의 일."""
    mask_year = df["date"].str[:4].astype(int).isin(OOS_YEARS)
    on_regime = df["date"].map(regime_series) == regime
    liquid = df["liquid"]
    base = df[mask_year & liquid & on_regime]
    a = cond_a.reindex(df.index, fill_value=False).loc[base.index]
    b = cond_b.reindex(df.index, fill_value=False).loc[base.index]
    a_only = base[a & ~b]
    combo = base[a & b]
    a_mean, a_win, a_p, a_n = _test_group(a_only[horizon])
    c_mean, c_win, c_p, c_n = _test_group(combo[horizon])
    diff_p = None
    if a_n >= 10 and c_n >= 10:
        _, diff_p = stats.ttest_ind(combo[horizon].dropna(), a_only[horizon].dropna(), equal_var=False)
    return {"단독%": a_mean, "단독n": a_n, "조합%": c_mean, "조합n": c_n, "차이p": diff_p}


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/4] 데이터 로드"); print("=" * 70)
    prices = load_prices(conn)
    prices = build_price_conditions(prices)
    cond_high_div = load_dividend_pit(conn, prices)
    roe_pit = load_roe_pit(conn, prices)
    prices["_roe_pit"] = roe_pit
    roe_rank = prices.groupby("date")["_roe_pit"].rank(pct=True)
    cond_high_roe = (roe_rank >= 0.8) & prices["_roe_pit"].notna()
    kospi_regime = load_kospi_regime(conn)
    conn.close()
    print(f"고배당 신호: {int(cond_high_div.sum())}행, 고ROE 신호: {int(cond_high_roe.sum())}행 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/4] IS/OOS x 5일/20일 — 4개 검정 + 다중비교 보정"); print("=" * 70)
    rows = []
    for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
        is_r = compare_combo(prices, cond_high_div, cond_high_roe, horizon, IN_SAMPLE_YEARS)
        oos_r = compare_combo(prices, cond_high_div, cond_high_roe, horizon, OOS_YEARS)
        rows.append({
            "기간": hname,
            "IS_단독%": is_r["단독(A only)_평균%"], "IS_조합%": is_r["조합(A+B)_평균%"],
            "IS_조합n": is_r["조합_n"], "IS_p": is_r["차이_p"],
            "OOS_단독%": oos_r["단독(A only)_평균%"], "OOS_조합%": oos_r["조합(A+B)_평균%"],
            "OOS_조합n": oos_r["조합_n"], "OOS_p": oos_r["차이_p"],
        })
    df = pd.DataFrame(rows)

    n_tests = 4  # IS/OOS x 5일/20일
    bonf_alpha = 0.05 / n_tests
    is_p = list(df["IS_p"])
    oos_p = list(df["OOS_p"])
    df["IS_FDR"] = bh_fdr(is_p, q=0.05)
    df["OOS_FDR"] = bh_fdr(oos_p, q=0.05)
    df["IS_Bonf"] = [p is not None and p < bonf_alpha for p in is_p]
    df["OOS_Bonf"] = [p is not None and p < bonf_alpha for p in oos_p]
    df["IS_개선방향"] = (df["IS_조합%"] - df["IS_단독%"]) > 0
    df["OOS_개선방향"] = (df["OOS_조합%"] - df["OOS_단독%"]) > 0
    df["방향일치"] = df["IS_개선방향"] == df["OOS_개선방향"]

    print(f"Bonferroni 임계값: {bonf_alpha:.4f}\n")
    print(df.to_string(index=False))
    print()

    both_horizons_pass = (
        df["IS_FDR"].all() and df["OOS_FDR"].all() and df["방향일치"].all()
        and (df["IS_개선방향"]).all()
    )
    print(f"5일+20일 모두(IS유의 + OOS유의 + 방향일치 + 개선방향): {'예' if both_horizons_pass else '아니오'}\n")

    print("=" * 70); print("[3/4] 시장 국면별 분해 (OOS, 5일/20일)"); print("=" * 70)
    regime_rows = []
    for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
        for regime in ["강세", "횡보", "약세"]:
            r = compare_combo_regime(prices, cond_high_div, cond_high_roe, horizon, kospi_regime, regime)
            regime_rows.append({"기간": hname, "국면": regime, **r})
    regime_df = pd.DataFrame(regime_rows)
    regime_df["FDR"] = bh_fdr(list(regime_df["차이p"]), q=0.05)
    print(regime_df.to_string(index=False))
    print()

    print("=" * 70); print("[4/4] 효과크기 vs 거래비용"); print("=" * 70)
    for _, r in df.iterrows():
        delta = r["OOS_조합%"] - r["OOS_단독%"]
        # 왕복 거래비용 자체가 아니라, "단독 전략 대비 조합 전략으로 바꿨을 때의 증분 효과"가
        # 의미 있으려면 증분(delta)이 비용보다 커야 함 — 추가 매매비용은 없음(필터만 바꾸는 것),
        # 절대수익 자체가 비용을 넘는지도 같이 표시.
        from dividend_event_validation import BUY_COST, SELL_COST
        roundtrip_cost_pct = (BUY_COST + SELL_COST) * 100
        print(f"{r['기간']}: 조합 증분효과 {delta:+.3f}%p (단독 {r['OOS_단독%']:.3f}% -> 조합 {r['OOS_조합%']:.3f}%), "
              f"왕복비용 {roundtrip_cost_pct:.2f}% 기준 {'증분효과가 비용보다 작음(거래비용 감내 못함)' if abs(delta) < roundtrip_cost_pct else '증분효과가 비용보다 큼'} | "
              f"절대수익(조합)이 비용 넘는지: {'예' if r['OOS_조합%'] > roundtrip_cost_pct else '아니오'}")

    print(f"\n총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
