"""
[검증 전용 — 운영 코드/모델 미사용] 조건 조합 재검증

단독으론 가짜로 판명난 조건(거래량급증/모멘텀상위/52주신고가)이 검증된 필터
(저PER/고배당/저PBR)와 조합하면 단독보다 나은지 확인.

비교 방법: "필터A 단독 만족(필터B 불만족)" vs "필터A+B 모두 만족" 그룹을 비교
(두 그룹은 서로 배타적 부분집합 — A를 만족하는 전체 안에서 B를 추가했을 때의 순수 효과를
보기 위함). t-검정 + IS/OOS 동일방향 확인 + 다중비교 보정(조합 7개 x 2 horizon = 14회 검정,
Bonferroni 0.05/14 ≈ 0.0036).

실행: python combo_validation.py
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
from per_validation import load_per_pit
from pbr_validation import load_bps_pit

pd.set_option("display.width", 160)

N_COMBOS_X_HORIZONS = 14
BONFERRONI_ALPHA = 0.05 / N_COMBOS_X_HORIZONS


def compare_combo(df: pd.DataFrame, cond_a: pd.Series, cond_b: pd.Series, horizon: str, years) -> dict:
    mask_year = df["date"].str[:4].astype(int).isin(years)
    liquid = df["liquid"]
    base = df[mask_year & liquid]
    a = cond_a.reindex(df.index, fill_value=False).loc[base.index]
    b = cond_b.reindex(df.index, fill_value=False).loc[base.index]

    a_only = base[a & ~b]      # A 단독(B 불만족) — 기존 검증된 단독 필터의 효과
    combo = base[a & b]        # A+B 모두 만족 — 조합 효과

    a_mean, a_win, a_p, a_n = _test_group(a_only[horizon])
    c_mean, c_win, c_p, c_n = _test_group(combo[horizon])
    diff_p = None
    if a_n >= 10 and c_n >= 10:
        _, diff_p = stats.ttest_ind(combo[horizon].dropna(), a_only[horizon].dropna(), equal_var=False)
    return {
        "단독(A only)_평균%": a_mean, "단독_승률": a_win, "단독_n": a_n,
        "조합(A+B)_평균%": c_mean, "조합_승률": c_win, "조합_n": c_n,
        "차이_p": round(diff_p, 4) if diff_p is not None else None,
    }


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/2] 데이터 로드 + 조건 계산 (가격조건 + 검증된 필터 3종)"); print("=" * 70)
    prices = load_prices(conn)
    prices = build_price_conditions(prices)
    cond_high_div = load_dividend_pit(conn, prices)
    cond_low_per, _, _ = load_per_pit(conn, prices)
    cond_low_pbr, _, _ = load_bps_pit(conn, prices)
    kospi_regime = load_kospi_regime(conn)
    conn.close()
    print(f"완료 ({time.time()-t0:.0f}s)\n")

    conds = {
        "거래량급증": prices["cond_vol_surge"], "모멘텀상위": prices["cond_momentum_top"],
        "신고가근접": prices["cond_near_high"], "고배당": cond_high_div,
        "저PER": cond_low_per, "저PBR": cond_low_pbr,
    }

    combos = [
        ("저PER", "거래량급증"), ("고배당", "거래량급증"),
        ("저PER", "모멘텀상위"), ("고배당", "모멘텀상위"),
        ("저PER", "신고가근접"),
        ("저PER", "저PBR"), ("고배당", "저PBR"),
    ]

    print("=" * 70); print("[2/2] 조합별 검증 — '단독(A only)' vs '조합(A+B)' 비교"); print("=" * 70)
    rows = []
    for a_name, b_name in combos:
        cond_a, cond_b = conds[a_name], conds[b_name]
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            is_r = compare_combo(prices, cond_a, cond_b, horizon, IN_SAMPLE_YEARS)
            oos_r = compare_combo(prices, cond_a, cond_b, horizon, OOS_YEARS)
            oos_sig = oos_r["차이_p"] is not None and oos_r["차이_p"] < BONFERRONI_ALPHA
            improved = (
                oos_r["조합(A+B)_평균%"] is not None and oos_r["단독(A only)_평균%"] is not None
                and oos_r["조합(A+B)_평균%"] > oos_r["단독(A only)_평균%"]
            )
            is_improved = (
                is_r["조합(A+B)_평균%"] is not None and is_r["단독(A only)_평균%"] is not None
                and is_r["조합(A+B)_평균%"] > is_r["단독(A only)_평균%"]
            )
            verdict = "✅ 조합이 유의하게 나음" if (oos_sig and improved and is_improved) else (
                "⚠️ 방향만 일부 개선(비유의)" if (improved and is_improved) else "❌ 차이없음/악화")
            rows.append({
                "조합": f"{a_name}+{b_name}", "기간": hname,
                "IS_단독%": is_r["단독(A only)_평균%"], "IS_조합%": is_r["조합(A+B)_평균%"],
                "OOS_단독%": oos_r["단독(A only)_평균%"], "OOS_조합%": oos_r["조합(A+B)_평균%"],
                "OOS_조합n": oos_r["조합_n"], "OOS_차이p": oos_r["차이_p"],
                "판정": verdict,
            })
    result_df = pd.DataFrame(rows)
    print(result_df.to_string(index=False))
    print()

    print("--- 시장국면별 (OOS, 5일수익률) — 조합이 유의했던/유망했던 것만 ---")
    prices["regime"] = prices["date"].map(kospi_regime)
    sub_mask = prices["date"].str[:4].astype(int).isin(OOS_YEARS) & prices["liquid"]
    for a_name, b_name in combos:
        cond_a, cond_b = conds[a_name].reindex(prices.index, fill_value=False), conds[b_name].reindex(prices.index, fill_value=False)
        combo_mask = sub_mask & cond_a & cond_b
        sub = prices[combo_mask]
        if sub["symbol"].count() < 30:
            continue
        print(f"  [{a_name}+{b_name}]")
        for regime in ["강세", "횡보", "약세"]:
            grp = sub[sub["regime"] == regime]
            mean, win, p, n = _test_group(grp["ret_fwd_5d"])
            if n >= 10:
                print(f"    {regime}: n={n}, 평균={mean:.2f}%, 승률={win:.1%}")
    print()

    out = Path(__file__).parent / "combo_validation_results.csv"
    result_df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"결과 저장: {out}")
    print(f"총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
