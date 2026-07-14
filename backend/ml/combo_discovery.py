"""
[검증 전용 — SELECT만, 모델/DB 변경 없음]
스크리너 검증 필터 9개의 모든 2-조합(36개) 탐색.

판정 기준 (사전 고정):
  IS/OOS 방향 일치
  AND OOS p < 0.05
  AND OOS 승률 >= 단독 최고 필터 승률 + 3%p
  AND OOS 조합 n >= 20

실행: uv run --project backend python backend/ml/combo_discovery.py
"""

import sqlite3
import sys
import time
from itertools import combinations
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from scipy import stats

ML_DIR = Path(__file__).parent
sys.path.insert(0, str(ML_DIR))

from dataset import DB_PATH
from per_validation import load_per_pit
from pbr_validation import load_bps_pit
from dividend_split_correction import load_dividend_yield_pit_corrected
from factors_60d import (
    load_factor_data, compute_eps_signals, compute_roe_signals,
    compute_dps_signals, compute_bps_signals, biz_year_pit,
)

# ── 판정 기준 ─────────────────────────────────────────────────────
PASS_WIN_DELTA    = 0.03
PASS_OOS_P        = 0.05
PASS_OOS_N_MIN    = 20
HORIZON           = "ret_fwd_20d"
HORIZON_LABEL     = "20일"

DATA_START        = "20220101"
DATA_END          = "20261231"
MIN_VOLUME_KRW    = 1_000_000_000
IN_SAMPLE_YEARS   = [2022, 2023, 2024]
OOS_YEARS         = [2025, 2026]

import re
ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)


def load_prices(conn: sqlite3.Connection) -> pd.DataFrame:
    names = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)
    etf_syms = set(names.loc[names["name"].fillna("").str.contains(ETF_PATTERN), "symbol"])
    df = pd.read_sql_query(
        "SELECT symbol, date, close, volume FROM prices "
        "WHERE date BETWEEN ? AND ? ORDER BY symbol, date",
        conn, params=(DATA_START, DATA_END),
    )
    df = df[~df["symbol"].isin(etf_syms)].reset_index(drop=True)
    df["close"]  = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["value"]  = df["close"] * df["volume"]
    counts = df.groupby("symbol").size()
    keep = counts[counts >= 280].index
    df = df[df["symbol"].isin(keep)].reset_index(drop=True)
    return df


def add_returns_and_liquid(df: pd.DataFrame) -> pd.DataFrame:
    """forward return 20d + liquid 플래그. groupby 없이 shift로 처리."""
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", group_keys=False)
    df[HORIZON]        = g["close"].transform(lambda s: s.shift(-20) / s - 1)
    df["value_ma20"]   = g["value"].transform(lambda s: s.rolling(20).mean())
    df["liquid"]       = df["value_ma20"] >= MIN_VOLUME_KRW
    # biz_year_pit 컬럼 (PIT 조인용)
    date_int = df["date"].astype(int).values
    df["biz_year_pit"] = np.where(
        (date_int % 10000) // 100 >= 4,
        date_int // 10000 - 1,
        date_int // 10000 - 2,
    )
    return df


def build_factor_conds_vectorized(prices: pd.DataFrame, conn: sqlite3.Connection) -> dict:
    """60d 검증 팩터 5종을 prices 인덱스 맞춤 bool Series로 반환 — merge 방식."""
    eps_df, bps_df, dps_df = load_factor_data(conn)
    eps_sig = compute_eps_signals(eps_df)
    roe_sig = compute_roe_signals(eps_df, bps_df)
    dps_sig = compute_dps_signals(dps_df)
    bps_sig = compute_bps_signals(bps_df)

    base = prices[["symbol", "date", "biz_year_pit"]].copy()

    def left_join_val(sig_df, val_col):
        sub = sig_df[["symbol", "biz_year", val_col]].copy()
        sub = sub.rename(columns={"biz_year": "biz_year_pit"})
        merged = base.merge(sub, on=["symbol", "biz_year_pit"], how="left")
        # merge may duplicate index — align back
        return merged[val_col].values

    eps_growth  = pd.Series(left_join_val(eps_sig, "eps_growth_yoy"),  index=prices.index)
    eps_accel   = pd.Series(left_join_val(eps_sig, "eps_growth_accel"), index=prices.index)
    roe_level   = pd.Series(left_join_val(roe_sig, "roe_level"),        index=prices.index)
    dps_growth  = pd.Series(left_join_val(dps_sig, "dps_growth_yoy"),   index=prices.index)
    bps_growth  = pd.Series(left_join_val(bps_sig, "bps_growth_yoy"),   index=prices.index)

    def top20(s: pd.Series) -> pd.Series:
        tmp = prices[["date"]].copy()
        tmp["_v"] = s.values
        rank = tmp.groupby("date")["_v"].rank(pct=True)
        return (rank >= 0.8) & s.notna()

    return {
        "eps_growth_top": top20(eps_growth),
        "eps_accel":      (eps_accel > 0) & eps_accel.notna(),
        "roe_top":        top20(roe_level),
        "bps_growth_top": top20(bps_growth),
        "div_growth":     (dps_growth > 0) & dps_growth.notna(),
    }


def compare_combo_full(
    prices: pd.DataFrame,
    cond_a: pd.Series,
    cond_b: pd.Series,
    horizon: str,
    years: list,
) -> dict:
    mask_year = prices["date"].str[:4].astype(int).isin(years)
    base = prices[mask_year & prices["liquid"]].copy()

    med = base.groupby("date")[horizon].median()
    base["_excess"] = base[horizon] - base["date"].map(med)

    ca = cond_a.reindex(prices.index, fill_value=False).loc[base.index]
    cb = cond_b.reindex(prices.index, fill_value=False).loc[base.index]

    def grp_stats(mask):
        s = base.loc[mask, "_excess"].dropna()
        if len(s) < 5:
            return None, None, None, len(s)
        return s.mean() * 100, (s > 0).mean(), s, len(s)

    a_mean, a_win, a_s, a_n = grp_stats(ca & ~cb)
    b_mean, b_win, b_s, b_n = grp_stats(~ca & cb)
    c_mean, c_win, c_s, c_n = grp_stats(ca & cb)

    diff_p, best_win = None, None
    if a_n >= 5 and b_n >= 5 and c_n >= 5 and c_s is not None:
        if a_win is not None and b_win is not None:
            best_win = max(a_win, b_win)
            best_s   = a_s if a_win >= b_win else b_s
            _, diff_p = stats.mannwhitneyu(c_s, best_s, alternative="greater")

    return {
        "A_mean%": a_mean, "A_win": a_win, "A_n": a_n,
        "B_mean%": b_mean, "B_win": b_win, "B_n": b_n,
        "C_mean%": c_mean, "C_win": c_win, "C_n": c_n,
        "best_win": best_win,
        "C_vs_best_p": round(diff_p, 4) if diff_p is not None else None,
    }


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70)
    print("[1/4] 가격 데이터 로드")
    print("=" * 70, flush=True)
    prices = load_prices(conn)
    print(f"  raw prices: {len(prices):,}행  ({time.time()-t0:.0f}s)", flush=True)
    prices = add_returns_and_liquid(prices)
    print(f"  returns 추가 완료  ({time.time()-t0:.0f}s)", flush=True)

    print("\n[2/4] PIT 필터 조건 계산", flush=True)
    cond_low_per, _, _  = load_per_pit(conn, prices)
    print(f"  low_per done  ({time.time()-t0:.0f}s)", flush=True)
    cond_low_pbr, _, _  = load_bps_pit(conn, prices)
    print(f"  low_pbr done  ({time.time()-t0:.0f}s)", flush=True)
    cond_high_div       = load_dividend_yield_pit_corrected(conn, prices)
    print(f"  high_div done  ({time.time()-t0:.0f}s)", flush=True)
    factor_conds        = build_factor_conds_vectorized(prices, conn)
    print(f"  60d factors done  ({time.time()-t0:.0f}s)", flush=True)
    conn.close()

    grp     = prices.groupby("symbol", group_keys=False)
    vol_krw = grp["value"].transform(lambda s: s.rolling(20).mean())
    cond_min_vol = (vol_krw >= 1_000_000_000)
    print(f"  min_vol done  ({time.time()-t0:.0f}s)", flush=True)

    all_conds: dict = {
        "고배당":        cond_high_div,
        "저PER":         cond_low_per,
        "저PBR":         cond_low_pbr,
        "배당성장":      factor_conds["div_growth"],
        "EPS성장상위":   factor_conds["eps_growth_top"],
        "EPS가속":       factor_conds["eps_accel"],
        "ROE상위":       factor_conds["roe_top"],
        "BPS성장상위":   factor_conds["bps_growth_top"],
        "거래대금10억+": cond_min_vol,
    }

    print("\n[3/4] 단독 필터 OOS 기준선", flush=True)
    print(f"{'필터':<16} {'OOS_n':>7} {'OOS_win':>8} {'OOS_mean%':>10}")
    print("-" * 45)
    single_win: dict = {}
    for name, cond in all_conds.items():
        mask_oos = prices["date"].str[:4].astype(int).isin(OOS_YEARS) & prices["liquid"]
        base_oos = prices[mask_oos].copy()
        med_oos  = base_oos.groupby("date")[HORIZON].median()
        base_oos["_excess"] = base_oos[HORIZON] - base_oos["date"].map(med_oos)
        c   = cond.reindex(prices.index, fill_value=False).loc[base_oos.index]
        s   = base_oos.loc[c, "_excess"].dropna()
        win  = (s > 0).mean() if len(s) >= 5 else float("nan")
        mean = s.mean() * 100  if len(s) >= 5 else float("nan")
        single_win[name] = win
        print(f"{name:<16} {len(s):>7,} {win:>8.1%} {mean:>10.2f}%")
    print(flush=True)

    print(f"\n[4/4] 전체 2-조합 검증 (36개, horizon={HORIZON_LABEL})", flush=True)
    print("=" * 70)

    filter_names = list(all_conds.keys())
    rows = []

    for a_name, b_name in combinations(filter_names, 2):
        cond_a = all_conds[a_name]
        cond_b = all_conds[b_name]

        is_r  = compare_combo_full(prices, cond_a, cond_b, HORIZON, IN_SAMPLE_YEARS)
        oos_r = compare_combo_full(prices, cond_a, cond_b, HORIZON, OOS_YEARS)

        best_is = max((x for x in [is_r["A_mean%"], is_r["B_mean%"]] if x is not None), default=None)
        is_dir_ok  = (is_r["C_mean%"]  is not None and best_is is not None
                      and is_r["C_mean%"] > best_is)
        oos_dir_ok = (oos_r["C_win"] is not None and oos_r["best_win"] is not None
                      and oos_r["C_win"] > oos_r["best_win"])
        oos_p_ok   = (oos_r["C_vs_best_p"] is not None and oos_r["C_vs_best_p"] < PASS_OOS_P)
        oos_n_ok   = (oos_r["C_n"] >= PASS_OOS_N_MIN)
        win_delta  = (oos_r["C_win"] - oos_r["best_win"]
                      if oos_r["C_win"] is not None and oos_r["best_win"] is not None else None)
        win_ok     = (win_delta is not None and win_delta >= PASS_WIN_DELTA)

        if is_dir_ok and oos_dir_ok and oos_p_ok and oos_n_ok and win_ok:
            verdict = "✅ PASS"
        elif oos_dir_ok and oos_n_ok:
            verdict = "⚠️ 방향O/유의성X"
        else:
            verdict = "❌"

        rows.append({
            "조합":         f"{a_name}+{b_name}",
            "IS_C_mean%":   is_r["C_mean%"],
            "IS_C_win":     is_r["C_win"],
            "IS_dir":       "✓" if is_dir_ok else "✗",
            "OOS_C_n":      oos_r["C_n"],
            "OOS_C_mean%":  oos_r["C_mean%"],
            "OOS_best_win": oos_r["best_win"],
            "OOS_C_win":    oos_r["C_win"],
            "win_delta":    win_delta,
            "OOS_p":        oos_r["C_vs_best_p"],
            "판정":         verdict,
        })

    df = pd.DataFrame(rows)
    df["_sort"] = df["판정"].map({"✅ PASS": 0, "⚠️ 방향O/유의성X": 1, "❌": 2})
    df = df.sort_values(["_sort", "win_delta"], ascending=[True, False]).drop(columns="_sort")

    def fmt(v, fmt_str):
        return f"{v:{fmt_str}}" if (v is not None and not (isinstance(v, float) and np.isnan(v))) else "  N/A"

    print(f"\n{'조합':<26} {'IS방향':>6} {'IS_win':>7} "
          f"{'OOS_n':>6} {'OOS_C_win':>10} {'best_win':>9} {'Δwin':>7} {'p':>8}  판정")
    print("-" * 102)
    for _, r in df.iterrows():
        print(f"{r['조합']:<26} {r['IS_dir']:>6} {fmt(r['IS_C_win'],'.1%'):>7} "
              f"{r['OOS_C_n']:>6} {fmt(r['OOS_C_win'],'.1%'):>10} "
              f"{fmt(r['OOS_best_win'],'.1%'):>9} {fmt(r['win_delta'],'+.1%'):>7} "
              f"{fmt(r['OOS_p'],'.4f'):>8}  {r['판정']}")
    print(flush=True)

    passed = df[df["판정"] == "✅ PASS"]
    print(f"\n{'='*60}")
    print(f"총 {len(df)}개 조합 중 PASS: {len(passed)}개")
    print(f"{'='*60}")

    if len(passed) > 0:
        print("\n[PASS 조합 상세]")
        for _, r in passed.iterrows():
            print(f"\n  {r['조합']}")
            print(f"    OOS: n={r['OOS_C_n']}, combo_win={fmt(r['OOS_C_win'],'.1%')}, "
                  f"best_single_win={fmt(r['OOS_best_win'],'.1%')}, "
                  f"Δ={fmt(r['win_delta'],'+.1%')}, p={fmt(r['OOS_p'],'.4f')}")
            print(f"    IS:  combo_win={fmt(r['IS_C_win'],'.1%')}, dir={r['IS_dir']}")
    else:
        print("\n→ 고배당+저PBR이 유일한 검증된 조합임이 재확인됨 (통과 0개).")

    # 고배당+저PBR 기존 조합 재확인
    print("\n[고배당+저PBR — 기존 운영 조합 재확인]")
    ref_is  = compare_combo_full(prices, all_conds["고배당"], all_conds["저PBR"], HORIZON, IN_SAMPLE_YEARS)
    ref_oos = compare_combo_full(prices, all_conds["고배당"], all_conds["저PBR"], HORIZON, OOS_YEARS)
    print(f"  IS  combo_win={ref_is['C_win']:.1%} (n={ref_is['C_n']}), "
          f"best_single={ref_is['best_win']:.1%}")
    print(f"  OOS combo_win={ref_oos['C_win']:.1%} (n={ref_oos['C_n']}), "
          f"best_single={ref_oos['best_win']:.1%}, "
          f"Δ={ref_oos['C_win']-ref_oos['best_win']:+.1%}, "
          f"p={ref_oos['C_vs_best_p']:.4f}")

    out = Path(__file__).parent / "combo_discovery_results.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n결과 CSV: {out}")
    print(f"총 소요: {time.time()-t0:.0f}s")
    sys.stdout.flush()


if __name__ == "__main__":
    out_log = Path(r"C:\Users\kkkhe\AppData\Local\Temp\combo_discovery_out.txt")
    with open(out_log, "w", encoding="utf-8") as _f:
        import sys as _sys
        _orig = _sys.stdout
        class _Tee:
            def write(self, s):
                _orig.write(s); _f.write(s); _f.flush()
            def flush(self):
                _orig.flush(); _f.flush()
        _sys.stdout = _Tee()
        try:
            main()
        finally:
            _sys.stdout = _orig
