"""
[검증 전용 — 운영 코드/모델 미사용] 배당락 전후 패턴 이벤트 스터디 (2026-06-23)

dividends 테이블 재수집(213→2,699종목, 2026-06-23) 후 제대로 된 표본으로 검증.
방법: 시장조정 초과수익(excess return) = 종목 수익률 - 같은 기간 KOSPI 수익률.
국면별로 쪼개도 시장 드리프트에 안 오염되도록(variant_screen_validation.py에서
"0 대비 검정" 함정을 발견한 교훈) 처음부터 전부 시장조정 수익률 기준으로 검정한다.

검증 1: 배당락 N일 전 매수 → 배당락일 매도 (가격만, 배당 미수령)
검증 2: 배당락일 매수 → N일 후 매도 (가격만, 배당 미수령 — 매수가 기준일 이후라 무자격)
검증 3: 배당락 X일 전 매수 → Y일 후 매도 (보유기간 동안 배당기준일을 지나므로 배당 수령
        — 가격수익만/가격+배당(세후) 두 버전)

다중비교: 본검정(검증1+2+3, 가격only+배당포함) IS/OOS 합쳐 한 묶음으로 FDR(q=0.05) +
Bonferroni 둘 다 표시. 국면/배당수익률 구간 분해는 별도 묶음으로 FDR 보정(탐색적 표시).

실행: python dividend_event_validation.py (IDLE 우선순위, 운영 데이터 SELECT만)
"""

import sqlite3
import sys
import time
from bisect import bisect_left
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
from factor_screen_validation import load_prices, ETF_PATTERN  # noqa: E402
from variant_screen_validation import bh_fdr  # noqa: E402

pd.set_option("display.width", 180)
pd.set_option("display.max_rows", 200)

IS_BIZYEARS = [2022, 2023]
OOS_BIZYEARS = [2024, 2025]
MIN_EFFECT_PCT = 0.3
DIV_INCOME_TAX = 0.154  # 배당소득세 15.4%

# evaluate.py와 동일한 거래비용 가정(왕복) — 일관성 유지
COMMISSION, SELL_TAX, SLIPPAGE = 0.00015, 0.002, 0.002
BUY_COST = SLIPPAGE + COMMISSION
SELL_COST = SLIPPAGE + COMMISSION + SELL_TAX


def apply_costs(raw: float) -> float:
    return (1 + raw) * (1 - BUY_COST) * (1 - SELL_COST) - 1


def load_dividend_events(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT symbol, biz_year, ex_dividend_date, record_date, dps, dividend_yield
        FROM dividends
        WHERE stock_type = '보통주' AND ex_dividend_date IS NOT NULL
          AND biz_year BETWEEN 2022 AND 2025
        """,
        conn,
    )
    return df


def load_kospi_series(conn: sqlite3.Connection) -> pd.Series:
    df = pd.read_sql_query(
        "SELECT date, close FROM market_index WHERE code='1001' ORDER BY date", conn
    )
    return pd.Series(df["close"].astype(float).values, index=df["date"].values)


def build_symbol_index(prices: pd.DataFrame) -> Dict[str, Tuple[List[str], np.ndarray]]:
    """symbol -> (정렬된 날짜 리스트, close 배열). bisect로 빠른 위치 탐색용."""
    out = {}
    for sym, grp in prices.groupby("symbol"):
        grp = grp.sort_values("date")
        out[sym] = (list(grp["date"].values), grp["close"].astype(float).values)
    return out


def find_idx(dates: List[str], target: str, tolerance_days: int = 3) -> Optional[int]:
    """target 날짜의 위치. 거래정지 등으로 정확히 없으면 허용범위(기본 3일) 내
    가장 가까운 거래일로 대체, 그 이상 벌어지면 매칭 실패(None)."""
    pos = bisect_left(dates, target)
    if pos < len(dates) and dates[pos] == target:
        return pos
    candidates = [p for p in (pos - 1, pos) if 0 <= p < len(dates)]
    if not candidates:
        return None
    best = min(candidates, key=lambda p: abs(_date_diff(dates[p], target)))
    return best if abs(_date_diff(dates[best], target)) <= tolerance_days else None


def _date_diff(a: str, b: str) -> int:
    from datetime import datetime
    return (datetime.strptime(a, "%Y%m%d") - datetime.strptime(b, "%Y%m%d")).days


def market_ret(kospi: pd.Series, d0: str, d1: str) -> Optional[float]:
    if d0 not in kospi.index or d1 not in kospi.index:
        return None
    p0, p1 = kospi[d0], kospi[d1]
    return (p1 / p0 - 1) if p0 else None


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/5] 데이터 로드"); print("=" * 70)
    prices = load_prices(conn)
    print(f"가격 유니버스: {prices['symbol'].nunique()}종목, {len(prices)}행")
    events = load_dividend_events(conn)
    print(f"배당락 이벤트(보통주, 2022-2025): {len(events)}건, {events['symbol'].nunique()}종목")
    kospi = load_kospi_series(conn)
    conn.close()

    sym_index = build_symbol_index(prices)
    print(f"종목별 가격 인덱스 구성 완료 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/5] 이벤트별 윈도우 수익률 계산"); print("=" * 70)
    PRE_N = [1, 3, 5, 10, 15, 20]
    POST_N = [5, 10, 20, 60]
    COMBO_X = [5, 10, 15, 20]
    COMBO_Y = [5, 10, 20]

    rows = []
    skipped = 0
    for ev in events.itertuples(index=False):
        sym, biz_year, ex_date = ev.symbol, ev.biz_year, ev.ex_dividend_date
        if sym not in sym_index:
            skipped += 1
            continue
        dates, closes = sym_index[sym]
        idx = find_idx(dates, ex_date)
        if idx is None:
            skipped += 1
            continue

        rec = {"symbol": sym, "biz_year": biz_year, "ex_date": ex_date,
               "dps": ev.dps, "dividend_yield": ev.dividend_yield}
        ex_close = closes[idx]

        for n in PRE_N:
            j = idx - n
            if j < 0:
                rec[f"pre_{n}"] = None
                continue
            buy = closes[j]
            raw = (ex_close / buy - 1) if buy else None
            mret = market_ret(kospi, dates[j], dates[idx])
            rec[f"pre_{n}"] = (raw - mret) if (raw is not None and mret is not None) else None

        for n in POST_N:
            j = idx + n
            if j >= len(dates):
                rec[f"post_{n}"] = None
                continue
            sell = closes[j]
            raw = (sell / ex_close - 1) if ex_close else None
            mret = market_ret(kospi, dates[idx], dates[j])
            rec[f"post_{n}"] = (raw - mret) if (raw is not None and mret is not None) else None

        for x in COMBO_X:
            for y in COMBO_Y:
                jx, jy = idx - x, idx + y
                if jx < 0 or jy >= len(dates):
                    rec[f"combo_{x}_{y}"] = None
                    rec[f"combo_{x}_{y}_div"] = None
                    continue
                buy, sell = closes[jx], closes[jy]
                raw = (sell / buy - 1) if buy else None
                mret = market_ret(kospi, dates[jx], dates[jy])
                excess = (raw - mret) if (raw is not None and mret is not None) else None
                rec[f"combo_{x}_{y}"] = excess
                # dps/종가 > 50%면 alotMatter 파싱 이상치(실제 회사가 주가의 절반 넘는
                # 배당을 줄 리 없음 — 진단 결과 4201건 중 7건이 이런 이상치였고 dps가
                # 수억원대로 찍혀 평균을 10000%대로 왜곡시킴, 2026-06-23) — 제외.
                dps_ratio = (ev.dps / ex_close) if (ev.dps is not None and ex_close) else None
                if excess is not None and ev.dps is not None and buy and dps_ratio is not None and dps_ratio <= 0.5:
                    net_dps = ev.dps * (1 - DIV_INCOME_TAX)
                    raw_div = (sell + net_dps) / buy - 1
                    rec[f"combo_{x}_{y}_div"] = raw_div - mret
                else:
                    rec[f"combo_{x}_{y}_div"] = None

        rows.append(rec)

    ev_df = pd.DataFrame(rows)
    print(f"이벤트 {len(ev_df)}건 처리 완료 (스킵 {skipped}건) ({time.time()-t0:.0f}s)\n")

    metric_cols = (
        [f"pre_{n}" for n in PRE_N] + [f"post_{n}" for n in POST_N]
        + [f"combo_{x}_{y}" for x in COMBO_X for y in COMBO_Y]
        + [f"combo_{x}_{y}_div" for x in COMBO_X for y in COMBO_Y]
    )

    def _test(series: pd.Series) -> Tuple[Optional[float], Optional[float], Optional[float], int]:
        s = series.dropna()
        n = len(s)
        if n < 30:
            return None, None, None, n
        mean = float(s.mean() * 100)
        win = float((s > 0).mean())
        _, p = stats.ttest_1samp(s, 0)
        return mean, win, float(p), n

    print("=" * 70); print(f"[3/5] 본검정 — IS({IS_BIZYEARS}) vs OOS({OOS_BIZYEARS}), 시장조정 초과수익"); print("=" * 70)
    is_mask = ev_df["biz_year"].isin(IS_BIZYEARS)
    oos_mask = ev_df["biz_year"].isin(OOS_BIZYEARS)

    main_rows = []
    for col in metric_cols:
        is_mean, is_win, is_p, is_n = _test(ev_df.loc[is_mask, col])
        oos_mean, oos_win, oos_p, oos_n = _test(ev_df.loc[oos_mask, col])
        main_rows.append({
            "지표": col, "IS_평균%": is_mean, "IS_승률": is_win, "IS_n": is_n, "IS_p": is_p,
            "OOS_평균%": oos_mean, "OOS_승률": oos_win, "OOS_n": oos_n, "OOS_p": oos_p,
        })
    main_df = pd.DataFrame(main_rows)

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
        if not (r["IS_FDR"] and r["OOS_FDR"] and r["부호일치"]):
            return "❌ 무의미"
        bonf = r["IS_Bonf"] and r["OOS_Bonf"]
        weak = r["OOS_평균%"] is None or abs(r["OOS_평균%"]) < MIN_EFFECT_PCT
        tag = "✅ 진짜(Bonferroni)" if bonf else "✅ 진짜(FDR만)"
        return tag + (" — 효과작음" if weak else "")

    main_df["판정"] = main_df.apply(_verdict, axis=1)
    n_pass = ((main_df["IS_FDR"]) & (main_df["OOS_FDR"]) & (main_df["부호일치"])).sum()
    print(f"검정 {n_tests}개 중 통과 {n_pass}건 (Bonferroni 임계값 {bonf_alpha:.6f})\n")
    print(main_df[["지표", "IS_평균%", "IS_승률", "IS_n", "OOS_평균%", "OOS_승률", "OOS_n", "판정"]].to_string(index=False))
    print()

    out1 = Path(__file__).parent / "dividend_event_results.csv"
    main_df.to_csv(out1, index=False, encoding="utf-8-sig")

    print("=" * 70); print("[4/5] 국면별 분해 — 통과한 지표만, 시장조정 초과수익이라 드리프트 안전"); print("=" * 70)
    with sqlite3.connect(DB_PATH) as conn2:
        from factor_screen_validation import load_kospi_regime
        regime = load_kospi_regime(conn2)
    ev_df["regime"] = ev_df["ex_date"].map(regime)

    passed_cols = main_df.loc[
        main_df["IS_FDR"] & main_df["OOS_FDR"] & main_df["부호일치"], "지표"
    ].tolist()
    regime_rows = []
    for col in passed_cols:
        for rg in ["강세", "횡보", "약세"]:
            sub = ev_df.loc[ev_df["regime"] == rg, col]
            mean, win, p, n = _test(sub)
            regime_rows.append({"지표": col, "국면": rg, "평균%": mean, "승률": win, "n": n, "p": p})
    if regime_rows:
        regime_df = pd.DataFrame(regime_rows)
        regime_df["FDR"] = bh_fdr(list(regime_df["p"]), q=0.05)
        print(regime_df.to_string(index=False))
        regime_df.to_csv(Path(__file__).parent / "dividend_event_regime_results.csv", index=False, encoding="utf-8-sig")
    else:
        print("(본검정 통과 지표 없음 — 국면 분해 생략)")
    print()

    print("=" * 70); print("[5/5] 배당수익률 구간별(저/중/고, 통과한 지표만)"); print("=" * 70)
    yv = ev_df["dividend_yield"]
    valid = yv.notna()
    ev_df["yield_bucket"] = pd.NA
    if valid.sum() > 30:
        ev_df.loc[valid, "yield_bucket"] = pd.qcut(yv[valid], 3, labels=["저배당", "중배당", "고배당"])

    bucket_rows = []
    for col in passed_cols:
        for b in ["저배당", "중배당", "고배당"]:
            sub = ev_df.loc[ev_df["yield_bucket"] == b, col]
            mean, win, p, n = _test(sub)
            bucket_rows.append({"지표": col, "구간": b, "평균%": mean, "승률": win, "n": n, "p": p})
    if bucket_rows:
        bucket_df = pd.DataFrame(bucket_rows)
        bucket_df["FDR"] = bh_fdr(list(bucket_df["p"]), q=0.05)
        print(bucket_df.to_string(index=False))
        bucket_df.to_csv(Path(__file__).parent / "dividend_event_yieldbucket_results.csv", index=False, encoding="utf-8-sig")
    else:
        print("(본검정 통과 지표 없음 — 구간 분해 생략)")
    print()

    print("=" * 70); print("[참고] 거래비용/세금 적용 후 실질 수익 (본검정 통과 지표만)"); print("=" * 70)
    for _, r in main_df[main_df["IS_FDR"] & main_df["OOS_FDR"] & main_df["부호일치"]].iterrows():
        oos_raw_pct = r["OOS_평균%"]
        if oos_raw_pct is None:
            continue
        after_cost = apply_costs(oos_raw_pct / 100) * 100
        print(f"{r['지표']}: OOS 평균 {oos_raw_pct:+.2f}% → 비용 적용 후 {after_cost:+.2f}%p "
              f"({'비용 차감해도 양수' if after_cost > 0 else '비용 차감하면 음수 — 실거래 무의미'})")

    print(f"\n총 소요시간: {time.time()-t0:.0f}s")
    print(f"결과 저장: {out1}")


if __name__ == "__main__":
    main()
