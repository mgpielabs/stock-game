"""
[검증 전용 — 기존 검증 파일·DB·모델 변경 없음, SELECT/계산만] 배당 분할비율 보정 전후 비교 (2026-06-26).

dividend_split_correction.py가 만든 dividend_split_correction_factors.csv(보정값)를 적용한
"보정된" 고배당 point-in-time 조건과, 기존 dividend_yield_pit_validation.py의 "미보정" 조건을
나란히 계산해서:
  1. 고배당 상위20% 표본에 새로 들어오고/빠지는 종목 수
  2. 보정 후 워크포워드 재검증(IS/OOS, 시장국면별, 5일/20일, Bonferroni) — 기존과 동일 방식
  3. 기존 결론(⚠️ 약세장 한정, OOS 전체평균 무의미)이 유지되는지

dividend_yield_pit_validation.py, factor_screen_validation.py, DB, 모델 전부 미변경.
결과는 dividend_split_correction_validation_results.csv로 별도 저장(기존 CSV 덮어쓰지 않음).

실행: python dividend_split_correction_validate.py
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

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from factor_screen_validation import (
    load_prices, build_price_conditions, load_kospi_regime,
    _test_group, evaluate_condition, IN_SAMPLE_YEARS, OOS_YEARS, BONFERRONI_ALPHA,
)
from dividend_yield_pit_validation import load_dividend_yield_pit_legacy
from dividend_split_correction import load_dividend_yield_pit_corrected, FACTORS_CSV

import sqlite3

pd.set_option("display.width", 160)


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/5] 데이터 로드"); print("=" * 70)
    prices = load_prices(conn)
    prices = build_price_conditions(prices)
    print(f"유니버스: {prices['symbol'].nunique()}종목, {len(prices)}행")

    print("\n" + "=" * 70); print("[2/5] 미보정 vs 보정 고배당 조건 계산"); print("=" * 70)
    cond_old, yield_old = load_dividend_yield_pit_legacy(conn, prices)
    cond_new, yield_new = load_dividend_yield_pit_corrected(conn, prices, FACTORS_CSV)
    kospi_regime = load_kospi_regime(conn)
    conn.close()

    print(f"미보정 고배당 표본: {int(cond_old.sum())}행 / {prices.loc[cond_old,'symbol'].nunique()}종목")
    print(f"보정후 고배당 표본: {int(cond_new.sum())}행 / {prices.loc[cond_new,'symbol'].nunique()}종목")

    print("\n" + "=" * 70); print("[3/5] 신규 진입/이탈 종목"); print("=" * 70)
    syms_old = set(prices.loc[cond_old, "symbol"].unique())
    syms_new = set(prices.loc[cond_new, "symbol"].unique())
    entered = syms_new - syms_old
    exited = syms_old - syms_new
    print(f"보정 후 새로 진입: {len(entered)}종목")
    print(f"보정 후 빠짐: {len(exited)}종목")
    print(f"공통(변화 없음): {len(syms_old & syms_new)}종목")

    # 행 단위 변화(연도별/기간 전체 사실상 어느 시점에든 한 번이라도 플래그가 바뀐 비중)
    changed_rows = (cond_old != cond_new).sum()
    print(f"행 단위 플래그 변화: {changed_rows} / {len(prices)} ({changed_rows/len(prices)*100:.3f}%)")

    print("\n" + "=" * 70); print("[4/5] 워크포워드 재검증 — 보정 후 (IS 2022-24 / OOS 2025-26)"); print("=" * 70)
    rows = []
    for label, cond in [("고배당(미보정, 기존)", cond_old), ("고배당(분할비율 보정)", cond_new)]:
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            is_r = evaluate_condition(prices, cond, horizon, IN_SAMPLE_YEARS)
            oos_r = evaluate_condition(prices, cond, horizon, OOS_YEARS)
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
                "OOS_p(vs0)": oos_r["sat_vs_zero_p"],
                "판정": verdict,
            })
    result_df = pd.DataFrame(rows)
    print(result_df.to_string(index=False))

    out = Path(__file__).parent / "dividend_split_correction_validation_results.csv"
    result_df.to_csv(out, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 70); print("[5/5] 시장국면별 (5일 수익률, OOS) — 보정 전후 비교"); print("=" * 70)
    prices["regime"] = prices["date"].map(kospi_regime)
    sub = prices[prices["date"].str[:4].astype(int).isin(OOS_YEARS) & prices["liquid"]]
    regime_rows = []
    for label, cond in [("미보정", cond_old), ("보정후", cond_new)]:
        for regime in ["강세", "횡보", "약세"]:
            grp = sub[(sub["regime"] == regime) & cond.loc[sub.index]]
            mean, win, p, n = _test_group(grp["ret_fwd_5d"])
            regime_rows.append({"조건": label, "국면": regime, "n": n, "평균%": mean, "승률": win, "p": p})
            print(f"  [{label}] {regime}: n={n}, 평균={mean if mean is not None else float('nan'):.2f}%, "
                  f"승률={win if win is not None else float('nan'):.1%}, p={p}")
    pd.DataFrame(regime_rows).to_csv(
        Path(__file__).parent / "dividend_split_correction_regime_results.csv",
        index=False, encoding="utf-8-sig",
    )

    print(f"\n총 소요시간: {time.time()-t0:.0f}s")
    print(f"결과 저장: {out}")


if __name__ == "__main__":
    main()
