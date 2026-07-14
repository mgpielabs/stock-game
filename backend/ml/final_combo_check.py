"""
[검증 전용] 최종 조합 재확인 (2026-06-23) — 저PER+저PBR, 고배당+저PBR

단독 필터 3개(고배당/저PER/저PBR) 전부 ✅→⚠️ 하향된 뒤, 조합도 현재(전체) 표본 +
고배당은 수정된 dividend_yield_pit(DPS÷실제종가, DART 공시값 불신뢰)로 최종 재확인.

실행: python final_combo_check.py (IDLE 우선순위, 운영 데이터 SELECT만)
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

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from factor_screen_validation import load_prices, build_price_conditions, IN_SAMPLE_YEARS, OOS_YEARS
from per_validation import load_per_pit
from pbr_validation import load_bps_pit
from dividend_yield_pit_validation import load_dividend_yield_pit
from combo_validation import compare_combo

pd.set_option("display.width", 160)


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/2] 데이터 로드"); print("=" * 70)
    prices = load_prices(conn)
    prices = build_price_conditions(prices)
    cond_low_per, _, _ = load_per_pit(conn, prices)
    cond_low_pbr, _, _ = load_bps_pit(conn, prices)
    cond_high_div, _ = load_dividend_yield_pit(conn, prices)  # 수정된 DPS÷종가 기준
    conn.close()
    print(f"저PER {int(cond_low_per.sum())}행, 저PBR {int(cond_low_pbr.sum())}행, "
          f"고배당(재계산) {int(cond_high_div.sum())}행 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/2] 조합 검증 — IS(2022-24) vs OOS(2025-26)"); print("=" * 70)
    combos = [
        ("저PER+저PBR", cond_low_per, cond_low_pbr),
        ("고배당(재계산)+저PBR", cond_high_div, cond_low_pbr),
    ]
    rows = []
    for label, cond_a, cond_b in combos:
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            is_r = compare_combo(prices, cond_a, cond_b, horizon, IN_SAMPLE_YEARS)
            oos_r = compare_combo(prices, cond_a, cond_b, horizon, OOS_YEARS)
            rows.append({
                "조합": label, "기간": hname,
                "IS_단독%": is_r["단독(A only)_평균%"], "IS_조합%": is_r["조합(A+B)_평균%"], "IS_조합n": is_r["조합_n"],
                "OOS_단독%": oos_r["단독(A only)_평균%"], "OOS_조합%": oos_r["조합(A+B)_평균%"],
                "OOS_조합n": oos_r["조합_n"], "OOS_차이p": oos_r["차이_p"],
            })
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))

    # 약세장 승률 재확인 (조합, OOS, 5일)
    from factor_screen_validation import load_kospi_regime
    conn = sqlite3.connect(DB_PATH)
    kospi_regime = load_kospi_regime(conn)
    conn.close()
    prices["regime"] = prices["date"].map(kospi_regime)
    sub = prices[prices["date"].str[:4].astype(int).isin(OOS_YEARS) & prices["liquid"]]

    print("\n--- 조합 약세장 승률 (OOS, 5일) ---")
    for label, cond_a, cond_b in combos:
        grp = sub[(sub["regime"] == "약세") & cond_a.loc[sub.index] & cond_b.loc[sub.index]]
        from factor_screen_validation import _test_group
        mean, win, p, n = _test_group(grp["ret_fwd_5d"])
        print(f"  {label}: n={n}, 평균={mean if mean is not None else float('nan'):.2f}%, "
              f"승률={win if win is not None else float('nan'):.1%}")

    out = Path(__file__).parent / "final_combo_check_results.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n결과 저장: {out}")
    print(f"총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
