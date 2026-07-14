"""
[검증 전용 — 운영 코드/모델 미사용] 저PBR 조건 point-in-time 재구성 + 워크포워드 검증

per_validation.py(저PER, dividends.eps 역산)와 동일한 방식을 PBR에 적용.
backend/data/bps_collector.py로 DART 재무제표(자본총계)+주식총수를 신규 수집해
BPS = 자본총계/발행주식총수를 계산, financials 테이블에 저장한 결과를 사용.

⚠️ 부분 수집: DART 일일 호출 한도에 걸려 13,075개 작업 중 1,387개(294종목, 그중 217종목은
2021~2025 5개년 전체)만 완료됨. 전체 유니버스(2,705종목) 대비 ~11%로 작음 — 결과는
참고용으로 보되, n(표본수)이 충분한지 확인 후 판단할 것.

  PBR(t) = 종가(t) / 시점 t까지 공시된 가장 최근 BPS (공시지연 약 3개월 가정, dividend/PER
           point-in-time 로직과 동일)

실행: python pbr_validation.py
"""

import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict

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

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from factor_screen_validation import (
    load_prices, build_price_conditions, load_kospi_regime,
    _test_group, evaluate_condition, IN_SAMPLE_YEARS, OOS_YEARS, BONFERRONI_ALPHA,
)

pd.set_option("display.width", 160)


def load_bps_pit(conn: sqlite3.Connection, prices: pd.DataFrame) -> pd.Series:
    """BPS를 point-in-time으로: biz_year Y의 BPS는 Y+1년 4/1부터 '안 사실'로 취급."""
    bps_df = pd.read_sql_query(
        "SELECT symbol, biz_year, bps FROM financials WHERE bps IS NOT NULL", conn
    )
    bps_df["known_from"] = (bps_df["biz_year"] + 1).astype(str) + "0401"

    bps_pit = pd.Series(np.nan, index=prices.index)
    dates = prices["date"].values
    symbols = prices["symbol"].values

    bps_sorted = bps_df.sort_values(["symbol", "known_from"])
    for sym, grp in bps_sorted.groupby("symbol"):
        mask = symbols == sym
        if not mask.any():
            continue
        idx = np.where(mask)[0]
        d_for_sym = dates[idx]
        known_from = grp["known_from"].values
        bps_vals = grp["bps"].values
        pos = np.searchsorted(known_from, d_for_sym, side="right") - 1
        valid = pos >= 0
        bps_pit.iloc[idx[valid]] = bps_vals[pos[valid]]

    prices = prices.copy()
    prices["_bps_pit"] = bps_pit
    prices["_pbr_pit"] = prices["close"] / prices["_bps_pit"].where(prices["_bps_pit"] > 0)

    rank = prices.groupby("date")["_pbr_pit"].rank(pct=True)
    cond_low_pbr = (rank <= 0.2) & prices["_pbr_pit"].notna()
    return cond_low_pbr, prices["_pbr_pit"], prices["_bps_pit"]


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/3] 데이터 로드 + 가격 기반 조건/전진수익률 계산"); print("=" * 70)
    prices = load_prices(conn)
    prices = build_price_conditions(prices)
    print(f"유니버스: {prices['symbol'].nunique()}종목, {len(prices)}행 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/3] financials.bps로 point-in-time PBR 역산"); print("=" * 70)
    cond_low_pbr, pbr_pit, bps_pit = load_bps_pit(conn, prices)
    n_with_pbr = pbr_pit.notna().sum()
    n_symbols_with_pbr = prices.loc[pbr_pit.notna(), "symbol"].nunique()
    print(f"PBR 계산 가능한 행: {n_with_pbr} ({n_symbols_with_pbr}종목) — ⚠️ DART 한도로 부분 수집(전체의 ~11%)")
    print(f"저PBR(하위20%) 신호 발생 행: {int(cond_low_pbr.sum())}\n")

    kospi_regime = load_kospi_regime(conn)
    conn.close()

    print("=" * 70); print("[3/3] 워크포워드 검증 — in-sample(2022-24) vs out-of-sample(2025-26)"); print("=" * 70)
    rows = []
    for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
        is_r = evaluate_condition(prices, cond_low_pbr, horizon, IN_SAMPLE_YEARS)
        oos_r = evaluate_condition(prices, cond_low_pbr, horizon, OOS_YEARS)
        is_sig = is_r["sat_vs_zero_p"] is not None and is_r["sat_vs_zero_p"] < BONFERRONI_ALPHA
        oos_sig = oos_r["sat_vs_zero_p"] is not None and oos_r["sat_vs_zero_p"] < BONFERRONI_ALPHA
        same_sign = (
            is_r["mean_sat_pct"] is not None and oos_r["mean_sat_pct"] is not None
            and np.sign(is_r["mean_sat_pct"]) == np.sign(oos_r["mean_sat_pct"])
        )
        verdict = "✅ 진짜 edge" if (is_sig and oos_sig and same_sign) else (
            "⚠️ 약함/불일치" if (is_r["diff_p"] and is_r["diff_p"] < 0.05) else "❌ 무의미")
        rows.append({
            "조건": "저PBR(BPS 역산, 하위20%)", "기간": hname,
            "IS_평균%": is_r["mean_sat_pct"], "IS_승률": is_r["win_sat"], "IS_n": is_r["n_sat"],
            "OOS_평균%": oos_r["mean_sat_pct"], "OOS_승률": oos_r["win_sat"], "OOS_n": oos_r["n_sat"],
            "불만족군평균%(OOS)": oos_r["mean_unsat_pct"],
            "판정": verdict,
        })
    result_df = pd.DataFrame(rows)
    print(result_df.to_string(index=False))
    print()

    print("--- 시장국면별 (5일 수익률, OOS 2025-26) ---")
    prices["regime"] = prices["date"].map(kospi_regime)
    sub = prices[prices["date"].str[:4].astype(int).isin(OOS_YEARS) & prices["liquid"]]
    for regime in ["강세", "횡보", "약세"]:
        grp = sub[(sub["regime"] == regime) & cond_low_pbr.loc[sub.index]]
        mean, win, p, n = _test_group(grp["ret_fwd_5d"])
        print(f"  {regime}: n={n}, 평균={mean if mean is not None else float('nan'):.2f}%, "
              f"승률={win if win is not None else float('nan'):.1%}, p={p}")
    print()

    out = Path(__file__).parent / "pbr_validation_results.csv"
    result_df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"결과 저장: {out}")
    print(f"총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
