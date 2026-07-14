"""
[검증 전용 — 운영 코드/모델 미사용] ROE(근사)/배당성향 point-in-time 검증 (2026-06-23)

DART 추가수집 없이 이미 있는 데이터로만 검증:
  ROE(근사) = dividends.eps / financials.bps (같은 symbol+biz_year)
  배당성향 = dividends.payout_ratio (그대로)
point-in-time: per_validation.py/pbr_validation.py와 동일하게 biz_year+1년 4/1부터
"공개된 사실"로 취급(정기보고서 제출기한 근사).

검증:
  1. 고ROE(상위20%) 단독
  2. 배당성향 저/중/고 3분위
  3. 조합: ROE+저PER, ROE+저PBR, 고배당+고ROE, (저PER+저PBR)+ROE 추가효과

방법은 factor_screen_validation.py의 표준 프레임(IS 2022-24/OOS 2025-26, Bonferroni+FDR,
국면별 분해)을 그대로 따름 — 작은 표본 과대추정 경계(저PBR 11% 교훈) 적용.

실행: python roe_payout_validation.py (IDLE 우선순위, 운영 데이터 SELECT만)
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

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from factor_screen_validation import (
    load_prices, build_price_conditions, load_kospi_regime,
    _test_group, evaluate_condition, IN_SAMPLE_YEARS, OOS_YEARS,
)
from per_validation import load_per_pit
from pbr_validation import load_bps_pit
from combo_validation import compare_combo
from variant_screen_validation import bh_fdr

pd.set_option("display.width", 180)
pd.set_option("display.max_rows", 200)

MIN_EFFECT_PCT = 0.3
MIN_N = 100  # 표본 이 이하면 결론보류(저PBR 11% 교훈)


def _map_pit(conn: sqlite3.Connection, prices: pd.DataFrame, sql: str, value_col: str) -> pd.Series:
    """공통 point-in-time 매핑 — biz_year+1년 4/1부터 공개된 것으로 취급."""
    df = pd.read_sql_query(sql, conn)
    df["known_from"] = (df["biz_year"] + 1).astype(str) + "0401"
    out = pd.Series(np.nan, index=prices.index)
    dates = prices["date"].values
    symbols = prices["symbol"].values
    df_sorted = df.sort_values(["symbol", "known_from"])
    for sym, grp in df_sorted.groupby("symbol"):
        mask = symbols == sym
        if not mask.any():
            continue
        idx = np.where(mask)[0]
        d_for_sym = dates[idx]
        known_from = grp["known_from"].values
        vals = grp[value_col].values
        pos = np.searchsorted(known_from, d_for_sym, side="right") - 1
        valid = pos >= 0
        out.iloc[idx[valid]] = vals[pos[valid]]
    return out


def load_roe_pit(conn: sqlite3.Connection, prices: pd.DataFrame) -> pd.Series:
    sql = """
        SELECT d.symbol, d.biz_year, d.eps / f.bps AS roe
        FROM dividends d JOIN financials f
          ON f.symbol = d.symbol AND f.biz_year = d.biz_year
        WHERE d.stock_type = '보통주' AND d.eps IS NOT NULL AND f.bps IS NOT NULL AND f.bps > 0
    """
    return _map_pit(conn, prices, sql, "roe")


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/5] 데이터 로드 + 가격조건/전진수익률"); print("=" * 70)
    prices = load_prices(conn)
    prices = build_price_conditions(prices)
    print(f"유니버스: {prices['symbol'].nunique()}종목, {len(prices)}행 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/5] ROE(근사)/배당성향 point-in-time 역산"); print("=" * 70)
    roe_pit = load_roe_pit(conn, prices)
    prices["_roe_pit"] = roe_pit
    roe_rank = prices.groupby("date")["_roe_pit"].rank(pct=True)
    cond_high_roe = (roe_rank >= 0.8) & prices["_roe_pit"].notna()
    n_roe = prices["_roe_pit"].notna().sum()
    print(f"ROE 계산 가능 행: {n_roe} ({prices.loc[prices['_roe_pit'].notna(),'symbol'].nunique()}종목), "
          f"고ROE(상위20%) 신호: {int(cond_high_roe.sum())}")

    payout_pit = _map_pit(
        conn, prices,
        "SELECT symbol, biz_year, payout_ratio FROM dividends WHERE payout_ratio IS NOT NULL",
        "payout_ratio",
    )
    prices["_payout_pit"] = payout_pit
    n_payout = prices["_payout_pit"].notna().sum()
    print(f"배당성향 계산 가능 행: {n_payout} ({prices.loc[prices['_payout_pit'].notna(),'symbol'].nunique()}종목)\n")

    # 저PER/저PBR 재사용 (기존 검증 스크립트 그대로)
    cond_low_per, per_pit, _ = load_per_pit(conn, prices)
    cond_low_pbr, pbr_pit, _ = load_bps_pit(conn, prices)
    cond_high_div = prices.get("cond_high_div")
    if cond_high_div is None:
        from factor_screen_validation import load_dividend_pit
        cond_high_div = load_dividend_pit(conn, prices)

    kospi_regime = load_kospi_regime(conn)
    conn.close()

    print("=" * 70); print("[3/5] 단독 검증 — IS(2022-24) vs OOS(2025-26)"); print("=" * 70)
    rows = []
    for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
        is_r = evaluate_condition(prices, cond_high_roe, horizon, IN_SAMPLE_YEARS)
        oos_r = evaluate_condition(prices, cond_high_roe, horizon, OOS_YEARS)
        rows.append({"조건": "고ROE(근사, 상위20%)", "기간": hname,
                     "IS_평균%": is_r["mean_sat_pct"], "IS_승률": is_r["win_sat"], "IS_n": is_r["n_sat"], "IS_p": is_r["sat_vs_zero_p"],
                     "OOS_평균%": oos_r["mean_sat_pct"], "OOS_승률": oos_r["win_sat"], "OOS_n": oos_r["n_sat"], "OOS_p": oos_r["sat_vs_zero_p"]})

    # 배당성향 3분위 (날짜별 cross-section tercile)
    payout_rank = prices.groupby("date")["_payout_pit"].rank(pct=True)
    bucket_conds = {
        "저배당성향(하위33%)": (payout_rank <= 0.33) & prices["_payout_pit"].notna(),
        "중배당성향(중간33%)": (payout_rank > 0.33) & (payout_rank <= 0.67) & prices["_payout_pit"].notna(),
        "고배당성향(상위33%)": (payout_rank > 0.67) & prices["_payout_pit"].notna(),
    }
    for label, cond in bucket_conds.items():
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            is_r = evaluate_condition(prices, cond, horizon, IN_SAMPLE_YEARS)
            oos_r = evaluate_condition(prices, cond, horizon, OOS_YEARS)
            rows.append({"조건": label, "기간": hname,
                         "IS_평균%": is_r["mean_sat_pct"], "IS_승률": is_r["win_sat"], "IS_n": is_r["n_sat"], "IS_p": is_r["sat_vs_zero_p"],
                         "OOS_평균%": oos_r["mean_sat_pct"], "OOS_승률": oos_r["win_sat"], "OOS_n": oos_r["n_sat"], "OOS_p": oos_r["sat_vs_zero_p"]})

    main_df = pd.DataFrame(rows)
    n_tests = len(main_df)
    bonf_alpha = 0.05 / n_tests
    main_df["IS_FDR"] = bh_fdr(list(main_df["IS_p"]), q=0.05)
    main_df["OOS_FDR"] = bh_fdr(list(main_df["OOS_p"]), q=0.05)
    main_df["부호일치"] = [
        (a is not None and b is not None and np.sign(a) == np.sign(b))
        for a, b in zip(main_df["IS_평균%"], main_df["OOS_평균%"])
    ]
    main_df["IS_Bonf"] = [p is not None and p < bonf_alpha for p in main_df["IS_p"]]
    main_df["OOS_Bonf"] = [p is not None and p < bonf_alpha for p in main_df["OOS_p"]]

    def _verdict(r):
        if (r["IS_n"] or 0) < MIN_N or (r["OOS_n"] or 0) < MIN_N:
            return "⏸ 결론보류(표본부족)"
        if not (r["IS_FDR"] and r["OOS_FDR"] and r["부호일치"]):
            return "❌ 무의미"
        bonf = r["IS_Bonf"] and r["OOS_Bonf"]
        weak = r["OOS_평균%"] is None or abs(r["OOS_평균%"]) < MIN_EFFECT_PCT
        tag = "✅ 진짜(Bonferroni)" if bonf else "✅ 진짜(FDR만)"
        return tag + (" — 효과작음" if weak else "")

    main_df["판정"] = main_df.apply(_verdict, axis=1)
    print(f"검정 {n_tests}개 (Bonferroni 임계값 {bonf_alpha:.6f})\n")
    print(main_df[["조건", "기간", "IS_평균%", "IS_승률", "IS_n", "OOS_평균%", "OOS_승률", "OOS_n", "판정"]].to_string(index=False))
    print()
    main_df.to_csv(Path(__file__).parent / "roe_payout_results.csv", index=False, encoding="utf-8-sig")

    print("=" * 70); print("[4/5] 조합 검증"); print("=" * 70)
    combos = [
        ("ROE+저PER", cond_low_per, cond_high_roe),
        ("ROE+저PBR", cond_low_pbr, cond_high_roe),
        ("고배당+고ROE", cond_high_div, cond_high_roe),
        ("(저PER+저PBR)+ROE추가", cond_low_per & cond_low_pbr, cond_high_roe),
    ]
    combo_rows = []
    for label, cond_a, cond_b in combos:
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            is_r = compare_combo(prices, cond_a, cond_b, horizon, IN_SAMPLE_YEARS)
            oos_r = compare_combo(prices, cond_a, cond_b, horizon, OOS_YEARS)
            combo_rows.append({
                "조합": label, "기간": hname,
                "IS_단독%": is_r["단독(A only)_평균%"], "IS_조합%": is_r["조합(A+B)_평균%"],
                "OOS_단독%": oos_r["단독(A only)_평균%"], "OOS_조합%": oos_r["조합(A+B)_평균%"],
                "OOS_조합n": oos_r["조합_n"], "OOS_차이p": oos_r["차이_p"],
            })
    combo_df = pd.DataFrame(combo_rows)

    def _combo_verdict(r):
        if (r["OOS_조합n"] or 0) < MIN_N:
            return "⏸ 결론보류(표본부족)"
        if r["OOS_차이p"] is None or r["OOS_차이p"] >= 0.05:
            return "❌ 차이없음/악화" if (r["OOS_조합%"] or 0) <= (r["OOS_단독%"] or 0) else "⚠️ 방향만 개선(비유의)"
        better = (r["OOS_조합%"] or 0) > (r["OOS_단독%"] or 0)
        return "✅ 조합이 유의하게 나음" if better else "❌ 조합이 유의하게 나쁨"

    combo_df["판정"] = combo_df.apply(_combo_verdict, axis=1)
    print(combo_df.to_string(index=False))
    combo_df.to_csv(Path(__file__).parent / "roe_payout_combo_results.csv", index=False, encoding="utf-8-sig")
    print()

    print("=" * 70); print("[5/5] 시장 국면별 분해 — 본검정 통과 조건만"); print("=" * 70)
    prices["regime"] = prices["date"].map(kospi_regime)
    sub_oos = prices[prices["date"].str[:4].astype(int).isin(OOS_YEARS) & prices["liquid"]]

    name_to_cond = {"고ROE(근사, 상위20%)": cond_high_roe, **bucket_conds}
    passed = main_df.loc[main_df["기간"] == "5일", :]
    passed_names = passed.loc[passed["판정"].str.startswith("✅"), "조건"].tolist()
    if not passed_names:
        print("(본검정 통과 조건 없음 — 국면 분해 생략)")
    else:
        for name in passed_names:
            cond = name_to_cond[name]
            print(f"--- {name} (5일 수익률, OOS) ---")
            for regime in ["강세", "횡보", "약세"]:
                grp = sub_oos[(sub_oos["regime"] == regime) & cond.loc[sub_oos.index]]
                mean, win, p, n = _test_group(grp["ret_fwd_5d"])
                print(f"  {regime}: n={n}, 평균={mean if mean is not None else float('nan'):.2f}%, "
                      f"승률={win if win is not None else float('nan'):.1%}, p={p}")
    print(f"\n총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
