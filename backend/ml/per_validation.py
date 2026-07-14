"""
[검증 전용 — 운영 코드/모델 미사용] 저PER 조건 point-in-time 재구성 + 워크포워드 검증

배경: factor_screen_validation.py의 Tier3 PER/PBR 검증은 fundamentals 테이블의
1년 수집 공백(원인 조사 결과: collect_fundamentals_and_flows()가 항상 "오늘 날짜"만
기록하는 구조 + StockGame-DailyUpdate 작업 자체가 2026-05-11에야 처음 등록돼 그 전엔
자동수집이 존재하지 않았음 — "죽은 수집기"가 아니라 "그때부터 비로소 존재"한 것)
때문에 연속 데이터가 6주뿐이라 결론을 못 냈음.

이번엔 신규 수집 없이 이미 있는 `dividends.eps`(DART alotMatter, 2021~2025년 사업연도,
배당 신호 연구 때 수집)로 PER을 point-in-time으로 역산해서 제대로 검증한다.
  PER(t) = 종가(t) / 시점 t까지 공시된 가장 최근 EPS (공시지연 약 3개월 가정,
           dividend point-in-time 로직과 동일 — load_dividend_pit() 참고)

한계: EPS는 배당을 준 연도만 dividends 테이블에 있어(배당 안 주는 적자/성장주 제외)
표본이 ~200종목/년으로 전체 유니버스보다 작고 배당주 쪽으로 약간 편향됨 — 결과 해석 시 감안.

실행: python per_validation.py
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
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from factor_screen_validation import (
    load_prices, build_price_conditions, load_kospi_regime,
    _test_group, evaluate_condition, IN_SAMPLE_YEARS, OOS_YEARS, BONFERRONI_ALPHA,
)

pd.set_option("display.width", 160)


def load_per_pit(conn: sqlite3.Connection, prices: pd.DataFrame) -> pd.Series:
    """EPS를 point-in-time으로 역산해 PER 계산. biz_year Y의 EPS는 Y+1년 4/1부터
    '안 사실'로 취급(정기보고서 제출기한 근사 — load_dividend_pit과 동일 가정)."""
    eps_df = pd.read_sql_query(
        "SELECT symbol, biz_year, eps FROM dividends WHERE eps IS NOT NULL", conn
    )
    eps_df["known_from"] = (eps_df["biz_year"] + 1).astype(str) + "0401"

    eps_pit = pd.Series(np.nan, index=prices.index)
    dates = prices["date"].values
    symbols = prices["symbol"].values

    eps_sorted = eps_df.sort_values(["symbol", "known_from"])
    for sym, grp in eps_sorted.groupby("symbol"):
        mask = symbols == sym
        if not mask.any():
            continue
        idx = np.where(mask)[0]
        d_for_sym = dates[idx]
        known_from = grp["known_from"].values
        eps_vals = grp["eps"].values
        pos = np.searchsorted(known_from, d_for_sym, side="right") - 1
        valid = pos >= 0
        eps_pit.iloc[idx[valid]] = eps_vals[pos[valid]]

    prices = prices.copy()
    prices["_eps_pit"] = eps_pit
    prices["_per_pit"] = prices["close"] / prices["_eps_pit"].where(prices["_eps_pit"] > 0)

    rank = prices.groupby("date")["_per_pit"].rank(pct=True)
    cond_low_per = (rank <= 0.2) & prices["_per_pit"].notna()
    return cond_low_per, prices["_per_pit"], prices["_eps_pit"]


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/3] 데이터 로드 + 가격 기반 조건/전진수익률 계산"); print("=" * 70)
    prices = load_prices(conn)
    prices = build_price_conditions(prices)
    print(f"유니버스: {prices['symbol'].nunique()}종목, {len(prices)}행 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/3] dividends.eps로 point-in-time PER 역산"); print("=" * 70)
    cond_low_per, per_pit, eps_pit = load_per_pit(conn, prices)
    n_with_per = per_pit.notna().sum()
    n_symbols_with_per = prices.loc[per_pit.notna(), "symbol"].nunique()
    print(f"PER 계산 가능한 행: {n_with_per} ({n_symbols_with_per}종목) — EPS 공시연도 2021~2025 기준")
    print(f"저PER(하위20%) 신호 발생 행: {int(cond_low_per.sum())}\n")

    kospi_regime = load_kospi_regime(conn)
    conn.close()

    print("=" * 70); print("[3/3] 워크포워드 검증 — in-sample(2022-24) vs out-of-sample(2025-26)"); print("=" * 70)
    rows = []
    for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
        is_r = evaluate_condition(prices, cond_low_per, horizon, IN_SAMPLE_YEARS)
        oos_r = evaluate_condition(prices, cond_low_per, horizon, OOS_YEARS)
        is_sig = is_r["sat_vs_zero_p"] is not None and is_r["sat_vs_zero_p"] < BONFERRONI_ALPHA
        oos_sig = oos_r["sat_vs_zero_p"] is not None and oos_r["sat_vs_zero_p"] < BONFERRONI_ALPHA
        same_sign = (
            is_r["mean_sat_pct"] is not None and oos_r["mean_sat_pct"] is not None
            and np.sign(is_r["mean_sat_pct"]) == np.sign(oos_r["mean_sat_pct"])
        )
        verdict = "✅ 진짜 edge" if (is_sig and oos_sig and same_sign) else (
            "⚠️ 약함/불일치" if (is_r["diff_p"] and is_r["diff_p"] < 0.05) else "❌ 무의미")
        rows.append({
            "조건": "저PER(EPS 역산, 하위20%)", "기간": hname,
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
        grp = sub[(sub["regime"] == regime) & cond_low_per.loc[sub.index]]
        mean, win, p, n = _test_group(grp["ret_fwd_5d"])
        print(f"  {regime}: n={n}, 평균={mean if mean is not None else float('nan'):.2f}%, "
              f"승률={win if win is not None else float('nan'):.1%}, p={p}")
    print()

    out = Path(__file__).parent / "per_validation_results.csv"
    result_df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"결과 저장: {out}")
    print(f"총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
