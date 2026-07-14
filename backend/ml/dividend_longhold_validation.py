"""
[검증 전용 — 운영 코드/모델 미사용] 배당 역방향 장기보유 전략 검증 (2026-06-23)

"배당락 직후(저점) 매수 → 다음 배당락 직전(배당 기대로 오를 때) 매도" — 약 11~12개월 보유.
dividend_event_validation.py의 유틸(가격 인덱스, KOSPI 시계열, FDR, 거래비용)을 재사용.

⚠️ 구조적 한계(끝까지 정직하게 보고): 이 전략은 "배당락일 이후 매수, 다음 배당락일 이전
매도"이므로 두 배당 모두 배당기준일(record date)을 못 채워 배당을 받지 못함(배당은
record_date 이전부터 보유해야 받음). 사용자가 요청한 "받은 배당 포함 총수익"은 이 전략
구조상 0원이라 가격수익=총수익. 이 사실을 숨기지 않고 그대로 보고한다.

사이클(Y→Y+1)이 2022→2023, 2023→2024, 2024→2025 세 개뿐 — IS=Y∈{2022,2023}(2개 코호트),
OOS=Y=2024(1개 코호트)뿐이라 종목수가 많아도 "연도 다양성"은 1개뿐임을 명시하고,
표본이 작으면 ✅ 강행하지 않고 "결론 보류"로 처리한다(저PBR 11% 교훈).

실행: python dividend_longhold_validation.py (IDLE 우선순위, 운영 데이터 SELECT만)
"""

import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
from factor_screen_validation import load_prices  # noqa: E402
from variant_screen_validation import bh_fdr  # noqa: E402
from dividend_event_validation import (  # noqa: E402
    build_symbol_index, find_idx, load_kospi_series, market_ret, apply_costs,
)

pd.set_option("display.width", 180)
pd.set_option("display.max_rows", 200)

MIN_EFFECT_PCT = 0.5      # 1년 보유 전략이라 단기보다 기준치 약간 높임
MIN_N_FOR_VERDICT = 100   # 이보다 표본 적으면 통계적으로 유의해도 "결론 보류"
BUY_OFFSETS = [0, 5, 10, 20]     # 배당락일(0) 또는 N일 후 매수
SELL_OFFSETS = [5, 10, 20, 30]  # 다음 배당락 M일 전 매도


def load_dividend_pairs(conn: sqlite3.Connection) -> pd.DataFrame:
    """종목별 연속된 사업연도(Y, Y+1) 배당 이벤트 쌍을 만든다."""
    df = pd.read_sql_query(
        """
        SELECT symbol, biz_year, ex_dividend_date, dividend_yield
        FROM dividends
        WHERE stock_type = '보통주' AND ex_dividend_date IS NOT NULL
          AND biz_year BETWEEN 2022 AND 2025
        ORDER BY symbol, biz_year
        """,
        conn,
    )
    pairs = []
    for sym, grp in df.groupby("symbol"):
        grp = grp.set_index("biz_year")
        years = sorted(grp.index)
        for y in years:
            if (y + 1) in grp.index:
                pairs.append({
                    "symbol": sym, "buy_year": y,
                    "ex_date_y": grp.loc[y, "ex_dividend_date"],
                    "ex_date_y1": grp.loc[y + 1, "ex_dividend_date"],
                    "dividend_yield": grp.loc[y, "dividend_yield"],
                })
    return pd.DataFrame(pairs)


def _test(series: pd.Series) -> Tuple[Optional[float], Optional[float], Optional[float], int]:
    s = series.dropna()
    n = len(s)
    if n < 10:
        return None, None, None, n
    mean = float(s.mean() * 100)
    win = float((s > 0).mean())
    _, p = stats.ttest_1samp(s, 0)
    return mean, win, float(p), n


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/4] 데이터 로드 + 사업연도 연속 쌍 구성"); print("=" * 70)
    prices = load_prices(conn)
    pairs = load_dividend_pairs(conn)
    kospi = load_kospi_series(conn)
    conn.close()
    print(f"가격 유니버스: {prices['symbol'].nunique()}종목")
    print(f"연속 배당 사이클(Y→Y+1): {len(pairs)}건, {pairs['symbol'].nunique()}종목")
    print("코호트(매수년도)별 건수:")
    print(pairs["buy_year"].value_counts().sort_index().to_string())
    print()

    sym_index = build_symbol_index(prices)
    print(f"가격 인덱스 구성 완료 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/4] 사이클별 윈도우 수익률 계산 (시장조정, 가격수익=총수익 — 위 docstring 참고)"); print("=" * 70)
    rows = []
    skipped = 0
    for ev in pairs.itertuples(index=False):
        sym = ev.symbol
        if sym not in sym_index:
            skipped += 1
            continue
        dates, closes = sym_index[sym]
        idx_y = find_idx(dates, ev.ex_date_y)
        idx_y1 = find_idx(dates, ev.ex_date_y1)
        if idx_y is None or idx_y1 is None:
            skipped += 1
            continue

        rec = {"symbol": sym, "buy_year": ev.buy_year, "dividend_yield": ev.dividend_yield}

        # 벤치마크: 배당락일(offset 0)→다음 배당락일(offset 0) 그냥 들고 있기
        if idx_y < idx_y1:
            fh_raw = closes[idx_y1] / closes[idx_y] - 1 if closes[idx_y] else None
            fh_mkt = market_ret(kospi, dates[idx_y], dates[idx_y1])
            rec["fullhold"] = (fh_raw - fh_mkt) if (fh_raw is not None and fh_mkt is not None) else None
        else:
            rec["fullhold"] = None

        for bo in BUY_OFFSETS:
            for so in SELL_OFFSETS:
                buy_idx, sell_idx = idx_y + bo, idx_y1 - so
                col = f"buy{bo}_sell{so}"
                if buy_idx >= sell_idx or buy_idx < 0 or sell_idx >= len(dates):
                    rec[col] = None
                    continue
                buy_p, sell_p = closes[buy_idx], closes[sell_idx]
                raw = (sell_p / buy_p - 1) if buy_p else None
                mret = market_ret(kospi, dates[buy_idx], dates[sell_idx])
                rec[col] = (raw - mret) if (raw is not None and mret is not None) else None
        rows.append(rec)

    cyc_df = pd.DataFrame(rows)
    print(f"사이클 {len(cyc_df)}건 처리 (스킵 {skipped}건) ({time.time()-t0:.0f}s)\n")

    combo_cols = [f"buy{bo}_sell{so}" for bo in BUY_OFFSETS for so in SELL_OFFSETS]

    print("=" * 70)
    print(f"[3/4] 본검정 — IS(매수년도 2022~2023, {(cyc_df['buy_year']<=2023).sum()}건) "
          f"vs OOS(매수년도 2024, {(cyc_df['buy_year']==2024).sum()}건)")
    print("=" * 70)
    is_mask = cyc_df["buy_year"].isin([2022, 2023])
    oos_mask = cyc_df["buy_year"] == 2024

    main_rows = []
    for col in combo_cols + ["fullhold"]:
        is_mean, is_win, is_p, is_n = _test(cyc_df.loc[is_mask, col])
        oos_mean, oos_win, oos_p, oos_n = _test(cyc_df.loc[oos_mask, col])
        main_rows.append({
            "전략": col, "IS_평균%": is_mean, "IS_승률": is_win, "IS_n": is_n, "IS_p": is_p,
            "OOS_평균%": oos_mean, "OOS_승률": oos_win, "OOS_n": oos_n, "OOS_p": oos_p,
        })
    main_df = pd.DataFrame(main_rows)

    n_tests = len(main_df) - 1  # fullhold는 벤치마크라 다중비교 패밀리에서 제외
    bonf_alpha = 0.05 / max(n_tests, 1)
    is_p_list = list(main_df.loc[main_df["전략"] != "fullhold", "IS_p"])
    oos_p_list = list(main_df.loc[main_df["전략"] != "fullhold", "OOS_p"])
    is_fdr = bh_fdr(is_p_list, q=0.05)
    oos_fdr = bh_fdr(oos_p_list, q=0.05)
    main_df["IS_FDR"] = [None] * len(main_df)
    main_df["OOS_FDR"] = [None] * len(main_df)
    main_df.loc[main_df["전략"] != "fullhold", "IS_FDR"] = is_fdr
    main_df.loc[main_df["전략"] != "fullhold", "OOS_FDR"] = oos_fdr

    main_df["부호일치"] = [
        (a is not None and b is not None and np.sign(a) == np.sign(b))
        for a, b in zip(main_df["IS_평균%"], main_df["OOS_평균%"])
    ]
    main_df["IS_Bonf"] = [p is not None and p < bonf_alpha for p in main_df["IS_p"]]
    main_df["OOS_Bonf"] = [p is not None and p < bonf_alpha for p in main_df["OOS_p"]]

    def _verdict(r):
        if r["전략"] == "fullhold":
            return "(벤치마크)"
        n_small = (r["IS_n"] or 0) < MIN_N_FOR_VERDICT or (r["OOS_n"] or 0) < MIN_N_FOR_VERDICT
        if n_small:
            return "⏸ 결론보류(표본부족)"
        if not (r["IS_FDR"] and r["OOS_FDR"] and r["부호일치"]):
            return "❌ 무의미"
        bonf = r["IS_Bonf"] and r["OOS_Bonf"]
        weak = r["OOS_평균%"] is None or abs(r["OOS_평균%"]) < MIN_EFFECT_PCT
        tag = "✅ 진짜(Bonferroni)" if bonf else "✅ 진짜(FDR만)"
        return tag + (" — 효과작음" if weak else "")

    main_df["판정"] = main_df.apply(_verdict, axis=1)
    print(f"OOS 코호트 1개뿐 — 연도 다양성 부족, 통계적 유의성과 별개로 일반화 가능성에 한계 있음\n")
    print(main_df[["전략", "IS_평균%", "IS_승률", "IS_n", "OOS_평균%", "OOS_승률", "OOS_n", "판정"]].to_string(index=False))
    print()

    out1 = Path(__file__).parent / "dividend_longhold_results.csv"
    main_df.to_csv(out1, index=False, encoding="utf-8-sig")

    print("=" * 70); print("[4/4] 배당수익률 구간별(코호트 연도 내 저/중/고 tercile)"); print("=" * 70)
    yv = cyc_df["dividend_yield"]
    cyc_df["yield_bucket"] = pd.NA
    for yr, grp in cyc_df.groupby("buy_year"):
        valid = grp["dividend_yield"].notna()
        if valid.sum() < 30:
            continue
        buckets = pd.qcut(grp.loc[valid, "dividend_yield"], 3, labels=["저배당", "중배당", "고배당"])
        cyc_df.loc[grp.loc[valid].index, "yield_bucket"] = buckets

    # 가장 대표적인 조합 1개만(검증된 게 있으면 그것, 없으면 buy0_sell20)만 구간별로 펼쳐 보고
    passed = main_df.loc[main_df["판정"].str.startswith("✅"), "전략"].tolist()
    rep_col = passed[0] if passed else "buy0_sell20"
    bucket_rows = []
    for b in ["저배당", "중배당", "고배당"]:
        for label, mask in [("IS", is_mask), ("OOS", oos_mask)]:
            sub = cyc_df.loc[mask & (cyc_df["yield_bucket"] == b), rep_col]
            mean, win, p, n = _test(sub)
            bucket_rows.append({"구간": b, "기간": label, "평균%": mean, "승률": win, "n": n, "p": p})
    bucket_df = pd.DataFrame(bucket_rows)
    print(f"대표 전략: {rep_col} ({'본검정 통과' if passed else '본검정 미통과, 참고용'})")
    print(bucket_df.to_string(index=False))
    bucket_df.to_csv(Path(__file__).parent / "dividend_longhold_yieldbucket_results.csv", index=False, encoding="utf-8-sig")
    print()

    print("=" * 70); print("[참고] 거래비용 적용 후 실질 수익 (✅ 판정 받은 전략만)"); print("=" * 70)
    any_pass = False
    for _, r in main_df[main_df["판정"].str.startswith("✅")].iterrows():
        any_pass = True
        oos_raw_pct = r["OOS_평균%"]
        after_cost = apply_costs(oos_raw_pct / 100) * 100
        print(f"{r['전략']}: OOS 평균 {oos_raw_pct:+.2f}% → 비용 적용 후 {after_cost:+.2f}%p")
    if not any_pass:
        print("✅ 판정된 전략 없음 — 비용 분석 대상 없음")
    print("\n참고: 본 전략은 구조상(배당락 직후 매수~다음 배당락 직전 매도) 배당기준일을 채우지 "
          "못해 배당소득세 영향이 없음(가격수익=총수익).")

    print(f"\n총 소요시간: {time.time()-t0:.0f}s")
    print(f"결과 저장: {out1}")


if __name__ == "__main__":
    main()
