"""
[검증 전용 — 운영 코드/모델 미사용] 배당수익률 point-in-time 재계산 + 고배당 필터 재검증 (2026-06-23)

배경: 스크리너의 고배당 필터가 `dividends.dividend_yield`(DART 공시 자체값)를 그대로
써왔는데, 한국내화(010040) 등 일부 종목은 DART 공시 자체가 시가 대신 액면가를 분모로
써서 배당수익률을 부풀려 공시했음(예: 010040 2024년 dps=100원인데 공시 yield=20.0% —
실제로는 100÷액면가500=20.0%, 진짜 시가 기준은 100÷종가2170=4.6%). 49건 중 18건이
이 패턴과 정확히 일치, 우리 파싱 버그가 아니라 DART 원본 공시 오류로 확인됨.

또한 별개로 와이엔텍(067900) 등 일부는 dps 자체가 억대로 파싱되는 이상치(이전 배당락
검증에서도 발견했던 것과 동일 버그, 이번엔 근본 원인 미수정인 채로 1,000,000원 캡으로
방어) — 두 문제 모두 본 스크립트에서 dps < 1,000,000 필터로 제외.

수정: dividend_yield_pit(t) = 시점 t까지 공시된 가장 최근 DPS ÷ 종가(t).
per_validation.py(PER)와 완전히 동일한 point-in-time 패턴(공시지연 약 3개월 가정).

검증: 기존 "고배당 ✅ 무조건 유효(OOS 56~62%)" 결론이 버그 섞인 DART yield로 나온
결과인지, 재계산해도 유지되는지 확인.

[2026-06-26 정식 반영] DPS÷종가 재계산에도 별개의 문제가 있었음 — 배당 공시 *이후*
주식분할/병합/무상증자/감자를 한 종목은 분자(미조정 과거 DPS)와 분모(분할조정된 현재
pykrx 수정종가)의 스케일이 어긋나 수익률이 왜곡됨(미원화학 10배, INVENI 5배 등 —
CLAUDE.md "고배당 배당수익률 버그 수정 + 재검증"/"point-in-time 분할비율 보정" 절 참고).
`dividend_split_correction.py`의 `load_dividend_yield_pit_corrected()`(분할비율 보정판)를
이 파일의 `load_dividend_yield_pit()` 정식 경로로 승격. 보정 전후 비교 검증
(dividend_split_correction_validate.py)에서 결론(⚠️ 약세장 한정, OOS 전체평균 무의미)이
바뀌지 않음을 확인한 뒤 반영 — 기존 미보정 로직은 `load_dividend_yield_pit_legacy()`로
이름만 바꿔 그대로 보존(비교/롤백용, import해서 그대로 호출 가능).

실행: python dividend_yield_pit_validation.py (IDLE 우선순위, 운영 데이터 SELECT만)
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
    _test_group, evaluate_condition, IN_SAMPLE_YEARS, OOS_YEARS, BONFERRONI_ALPHA,
)
from dividend_split_correction import load_dividend_yield_pit_corrected

pd.set_option("display.width", 160)

DPS_SANITY_CAP = 1_000_000  # 이 이상은 파싱 이상치(예: 067900 18억원) — 위 docstring 참고


def load_dividend_yield_pit_legacy(conn: sqlite3.Connection, prices: pd.DataFrame):
    """[보존/롤백용 — 2026-06-26부터 정식 경로 아님] DPS를 point-in-time으로 역산해 배당수익률
    계산하되 분할/병합 보정은 하지 않음(원래의 dividend_yield_pit_validation.py 로직 그대로).
    DART의 자체 dividend_yield 공시값은 액면가 오분모 오류가 있어 신뢰하지 않고 항상
    DPS/실제종가로 직접 계산하지만, 배당 공시 이후 분할/병합이 있었던 종목은 여기서도
    여전히 왜곡됨(미원화학/INVENI 등) — 정식 경로는 아래 load_dividend_yield_pit() 참고."""
    dps_df = pd.read_sql_query(
        f"SELECT symbol, biz_year, dps FROM dividends WHERE dps IS NOT NULL AND dps < {DPS_SANITY_CAP}",
        conn,
    )
    dps_df["known_from"] = (dps_df["biz_year"] + 1).astype(str) + "0401"

    dps_pit = pd.Series(np.nan, index=prices.index)
    dates = prices["date"].values
    symbols = prices["symbol"].values

    dps_sorted = dps_df.sort_values(["symbol", "known_from"])
    for sym, grp in dps_sorted.groupby("symbol"):
        mask = symbols == sym
        if not mask.any():
            continue
        idx = np.where(mask)[0]
        d_for_sym = dates[idx]
        known_from = grp["known_from"].values
        dps_vals = grp["dps"].values
        pos = np.searchsorted(known_from, d_for_sym, side="right") - 1
        valid = pos >= 0
        dps_pit.iloc[idx[valid]] = dps_vals[pos[valid]]

    prices = prices.copy()
    prices["_dps_pit"] = dps_pit
    prices["_div_yield_pit"] = (prices["_dps_pit"] / prices["close"] * 100).where(prices["close"] > 0)

    rank = prices.groupby("date")["_div_yield_pit"].rank(pct=True)
    cond_high_div = (rank >= 0.8) & prices["_div_yield_pit"].notna()
    return cond_high_div, prices["_div_yield_pit"]


def load_dividend_yield_pit(conn: sqlite3.Connection, prices: pd.DataFrame):
    """[정식 경로, 2026-06-26부터] dividend_split_correction.py의 분할/병합 비율 보정판을
    그대로 호출. 기존 호출부(이 함수명을 import하던 곳: final_combo_check.py 등)는
    코드 변경 없이 자동으로 보정판을 쓰게 됨. 미보정 버전이 필요하면
    load_dividend_yield_pit_legacy()를 명시적으로 사용."""
    return load_dividend_yield_pit_corrected(conn, prices)


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/4] 데이터 로드 + 가격조건/전진수익률"); print("=" * 70)
    prices = load_prices(conn)
    prices = build_price_conditions(prices)
    print(f"유니버스: {prices['symbol'].nunique()}종목, {len(prices)}행 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/4] DPS÷종가로 point-in-time 배당수익률 역산 (분할/병합 비율 보정 적용)"); print("=" * 70)
    cond_high_div_new, div_yield_pit = load_dividend_yield_pit(conn, prices)
    n_with_yield = div_yield_pit.notna().sum()
    n_syms = prices.loc[div_yield_pit.notna(), "symbol"].nunique()
    print(f"배당수익률 계산 가능 행: {n_with_yield} ({n_syms}종목)")
    print(f"고배당(상위20%) 신호(재계산): {int(cond_high_div_new.sum())}")

    # 비교용: 기존 DART 공시값 기반 (load_dividend_pit, factor_screen_validation.py)
    from factor_screen_validation import load_dividend_pit
    cond_high_div_old = load_dividend_pit(conn, prices)
    print(f"고배당(상위20%) 신호(기존 DART 공시값): {int(cond_high_div_old.sum())}")

    overlap = (cond_high_div_new & cond_high_div_old).sum()
    only_new = (cond_high_div_new & ~cond_high_div_old).sum()
    only_old = (cond_high_div_old & ~cond_high_div_new).sum()
    print(f"두 조건 중첩: {overlap}행 / 재계산에서만 고배당: {only_new}행 / 기존공시값에서만 고배당: {only_old}행\n")

    kospi_regime = load_kospi_regime(conn)
    conn.close()

    print("=" * 70); print("[3/4] 워크포워드 검증 — in-sample(2022-24) vs out-of-sample(2025-26)"); print("=" * 70)
    rows = []
    for label, cond in [("고배당(재계산, DPS÷종가, 분할보정)", cond_high_div_new), ("고배당(기존, DART공시값)", cond_high_div_old)]:
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
                "판정": verdict,
            })
    result_df = pd.DataFrame(rows)
    print(result_df.to_string(index=False))
    print()

    out = Path(__file__).parent / "dividend_yield_pit_results.csv"
    result_df.to_csv(out, index=False, encoding="utf-8-sig")

    print("=" * 70); print("[4/4] 시장국면별 (5일 수익률, OOS) — 재계산 기준"); print("=" * 70)
    prices["regime"] = prices["date"].map(kospi_regime)
    sub = prices[prices["date"].str[:4].astype(int).isin(OOS_YEARS) & prices["liquid"]]
    for regime in ["강세", "횡보", "약세"]:
        grp = sub[(sub["regime"] == regime) & cond_high_div_new.loc[sub.index]]
        mean, win, p, n = _test_group(grp["ret_fwd_5d"])
        print(f"  {regime}: n={n}, 평균={mean if mean is not None else float('nan'):.2f}%, "
              f"승률={win if win is not None else float('nan'):.1%}, p={p}")
    print(f"\n총 소요시간: {time.time()-t0:.0f}s")
    print(f"결과 저장: {out}")


if __name__ == "__main__":
    main()
