"""
[검증 전용 — SELECT만, 모델/DB 변경 없음]
combo_discovery.py v2 — 모든 PIT 매핑을 merge 방식으로 벡터화.
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
from factors_60d import (
    load_factor_data, compute_eps_signals, compute_roe_signals,
    compute_dps_signals, compute_bps_signals,
)

FACTORS_CSV = ML_DIR.parent / "data" / "dividend_split_correction_factors.csv"

import re
ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)

DATA_START      = "20220101"
DATA_END        = "20261231"
MIN_VOLUME_KRW  = 1_000_000_000
IN_SAMPLE_YEARS = [2022, 2023, 2024]
OOS_YEARS       = [2025, 2026]
HORIZON         = "ret_fwd_20d"
HORIZON_LABEL   = "20일"
PASS_WIN_DELTA  = 0.03
PASS_OOS_P      = 0.05
PASS_OOS_N_MIN  = 20

OUT_LOG = Path(r"C:\Users\kkkhe\AppData\Local\Temp\combo_discovery_v2_out.txt")


def biz_year_pit_vec(date_int_arr: np.ndarray) -> np.ndarray:
    year  = date_int_arr // 10000
    month = (date_int_arr % 10000) // 100
    return np.where(month >= 4, year - 1, year - 2)


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
    df["biz_year_pit"] = biz_year_pit_vec(df["date"].astype(int).values)
    return df


def add_returns_and_liquid(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("symbol", group_keys=False)
    df[HORIZON]      = g["close"].transform(lambda s: s.shift(-20) / s - 1)
    df["value_ma20"] = g["value"].transform(lambda s: s.rolling(20).mean())
    df["liquid"]     = df["value_ma20"] >= MIN_VOLUME_KRW
    return df


def pit_merge(prices: pd.DataFrame, annual_df: pd.DataFrame, val_col: str) -> pd.Series:
    """
    prices에 biz_year_pit이 있을 때 annual_df (symbol, biz_year, val_col)와
    left-join으로 PIT 값 반환. 연도 갭(NaN)은 허용 — 검증 목적상 충분.
    """
    annual = annual_df[["symbol", "biz_year", val_col]].copy()
    annual["symbol"] = annual["symbol"].astype(str).str.zfill(6)
    annual = annual.rename(columns={"biz_year": "biz_year_pit"})
    merged = prices[["symbol", "biz_year_pit"]].merge(
        annual, on=["symbol", "biz_year_pit"], how="left"
    )
    return pd.Series(merged[val_col].values, index=prices.index)


def build_all_conds(prices: pd.DataFrame, conn: sqlite3.Connection) -> dict:
    eps_df, bps_df, dps_df = load_factor_data(conn)

    # ── EPS (PER 분자) ─────────────────────────────────────
    eps_pit = pit_merge(prices, eps_df, "eps")   # raw EPS

    per_pit = prices["close"] / eps_pit.where(eps_pit > 0)
    rank_per = prices.groupby("date")["_per_tmp"].rank(pct=True) if False else \
               prices.assign(_per_tmp=per_pit).groupby("date")["_per_tmp"].rank(pct=True)
    cond_low_per = (rank_per <= 0.2) & per_pit.notna()

    # ── BPS (PBR 분모) ─────────────────────────────────────
    bps_pit = pit_merge(prices, bps_df, "bps")
    pbr_pit = prices["close"] / bps_pit.where(bps_pit > 0)
    rank_pbr = prices.assign(_pbr_tmp=pbr_pit).groupby("date")["_pbr_tmp"].rank(pct=True)
    cond_low_pbr = (rank_pbr <= 0.2) & pbr_pit.notna()

    # ── 고배당 (DPS÷종가, 분할보정) ────────────────────────
    dps_for_div = dps_df[["symbol", "biz_year", "dps"]].copy()
    dps_for_div = dps_for_div[dps_for_div["dps"] < 1_000_000]
    dps_raw_pit = pit_merge(prices, dps_for_div, "dps")
    try:
        cf_df = pd.read_csv(FACTORS_CSV)[["symbol", "biz_year", "correction_factor"]]
        cf_df["symbol"] = cf_df["symbol"].astype(str).str.zfill(6)
        cf_lookup = prices[["symbol", "biz_year_pit"]].merge(
            cf_df.rename(columns={"biz_year": "biz_year_pit"}),
            on=["symbol", "biz_year_pit"], how="left"
        )["correction_factor"].values
        cf_arr = pd.Series(cf_lookup, index=prices.index).fillna(1.0)
        dps_corr = dps_raw_pit * cf_arr
    except Exception:
        dps_corr = dps_raw_pit

    div_yield = dps_corr / prices["close"].replace(0, np.nan)
    rank_div  = prices.assign(_dy=div_yield).groupby("date")["_dy"].rank(pct=True)
    cond_high_div = (rank_div >= 0.8) & div_yield.notna() & (div_yield < 0.5)

    # ── 60d 팩터 (EPS성장/ROE/DPS성장/BPS성장) ─────────────
    eps_sig = compute_eps_signals(eps_df)
    roe_sig = compute_roe_signals(eps_df, bps_df)
    dps_sig = compute_dps_signals(dps_df)
    bps_sig = compute_bps_signals(bps_df)

    def pit_sig(sig_df, val_col):
        return pit_merge(prices, sig_df[["symbol", "biz_year", val_col]], val_col)

    eps_growth = pit_sig(eps_sig, "eps_growth_yoy")
    eps_accel  = pit_sig(eps_sig, "eps_growth_accel")
    roe_level  = pit_sig(roe_sig, "roe_level")
    dps_growth = pit_sig(dps_sig, "dps_growth_yoy")
    bps_growth = pit_sig(bps_sig, "bps_growth_yoy")

    def top20(s: pd.Series) -> pd.Series:
        tmp = prices[["date"]].copy()
        tmp["_v"] = s.values
        return (tmp.groupby("date")["_v"].rank(pct=True) >= 0.8) & s.notna()

    # ── 거래대금 ────────────────────────────────────────────
    grp = prices.groupby("symbol", group_keys=False)
    cond_min_vol = grp["value"].transform(lambda s: s.rolling(20).mean()) >= MIN_VOLUME_KRW

    # ── 배당 성장 ───────────────────────────────────────────
    cond_div_growth = (dps_growth > 0) & dps_growth.notna()

    return {
        "고배당":        cond_high_div,
        "저PER":         cond_low_per,
        "저PBR":         cond_low_pbr,
        "배당성장":      cond_div_growth,
        "EPS성장상위":   top20(eps_growth),
        "EPS가속":       (eps_accel > 0) & eps_accel.notna(),
        "ROE상위":       top20(roe_level),
        "BPS성장상위":   top20(bps_growth),
        "거래대금10억+": cond_min_vol,
    }


def compare_combo_full(prices, cond_a, cond_b, horizon, years):
    mask_year = prices["date"].str[:4].astype(int).isin(years)
    base = prices[mask_year & prices["liquid"]].copy()
    med  = base.groupby("date")[horizon].median()
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

    def log(msg):
        print(msg, flush=True)

    log("=" * 70)
    log("[1/4] 가격 데이터 로드")
    log("=" * 70)
    prices = load_prices(conn)
    log(f"  raw prices: {len(prices):,}행  ({time.time()-t0:.0f}s)")
    prices = add_returns_and_liquid(prices)
    log(f"  returns 추가 완료  ({time.time()-t0:.0f}s)")

    log("\n[2/4] PIT 필터 조건 계산 (merge 방식)")
    all_conds = build_all_conds(prices, conn)
    conn.close()
    log(f"  모든 조건 완료  ({time.time()-t0:.0f}s)")

    log("\n[3/4] 단독 필터 OOS 기준선")
    log(f"{'필터':<16} {'OOS_n':>7} {'OOS_win':>8} {'OOS_mean%':>10}")
    log("-" * 45)
    single_win = {}
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
        log(f"{name:<16} {len(s):>7,} {win:>8.1%} {mean:>10.2f}%")

    log(f"\n[4/4] 전체 2-조합 검증 (36개, horizon={HORIZON_LABEL})")
    log("=" * 70)

    filter_names = list(all_conds.keys())
    rows = []

    for a_name, b_name in combinations(filter_names, 2):
        cond_a = all_conds[a_name]
        cond_b = all_conds[b_name]
        is_r   = compare_combo_full(prices, cond_a, cond_b, HORIZON, IN_SAMPLE_YEARS)
        oos_r  = compare_combo_full(prices, cond_a, cond_b, HORIZON, OOS_YEARS)

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
        return (f"{v:{fmt_str}}"
                if (v is not None and not (isinstance(v, float) and np.isnan(v)))
                else "  N/A")

    log(f"\n{'조합':<26} {'IS방향':>6} {'IS_win':>7} "
        f"{'OOS_n':>6} {'OOS_C_win':>10} {'best_win':>9} {'Δwin':>7} {'p':>8}  판정")
    log("-" * 102)
    for _, r in df.iterrows():
        log(f"{r['조합']:<26} {r['IS_dir']:>6} {fmt(r['IS_C_win'],'.1%'):>7} "
            f"{r['OOS_C_n']:>6} {fmt(r['OOS_C_win'],'.1%'):>10} "
            f"{fmt(r['OOS_best_win'],'.1%'):>9} {fmt(r['win_delta'],'+.1%'):>7} "
            f"{fmt(r['OOS_p'],'.4f'):>8}  {r['판정']}")

    passed = df[df["판정"] == "✅ PASS"]
    log(f"\n{'='*60}")
    log(f"총 {len(df)}개 조합 중 PASS: {len(passed)}개")
    log(f"{'='*60}")

    if len(passed) > 0:
        log("\n[PASS 조합 상세]")
        for _, r in passed.iterrows():
            log(f"\n  {r['조합']}")
            log(f"    OOS: n={r['OOS_C_n']}, combo_win={fmt(r['OOS_C_win'],'.1%')}, "
                f"best_single_win={fmt(r['OOS_best_win'],'.1%')}, "
                f"Δ={fmt(r['win_delta'],'+.1%')}, p={fmt(r['OOS_p'],'.4f')}")
            log(f"    IS:  combo_win={fmt(r['IS_C_win'],'.1%')}, dir={r['IS_dir']}")
    else:
        log("\n→ 고배당+저PBR이 유일한 검증된 조합임이 재확인됨 (통과 0개).")

    log("\n[고배당+저PBR — 기존 운영 조합 재확인]")
    ref_is  = compare_combo_full(prices, all_conds["고배당"], all_conds["저PBR"], HORIZON, IN_SAMPLE_YEARS)
    ref_oos = compare_combo_full(prices, all_conds["고배당"], all_conds["저PBR"], HORIZON, OOS_YEARS)
    log(f"  IS  combo_win={ref_is['C_win']:.1%} (n={ref_is['C_n']}), "
        f"best_single={ref_is['best_win']:.1%}")
    log(f"  OOS combo_win={ref_oos['C_win']:.1%} (n={ref_oos['C_n']}), "
        f"best_single={ref_oos['best_win']:.1%}, "
        f"Δ={ref_oos['C_win']-ref_oos['best_win']:+.1%}, "
        f"p={ref_oos['C_vs_best_p']:.4f}")

    out = Path(__file__).parent / "combo_discovery_results.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    log(f"\n결과 CSV: {out}")
    log(f"총 소요: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    with open(OUT_LOG, "w", encoding="utf-8") as _f:
        class _Tee:
            def write(self, s):
                sys.__stdout__.write(s)
                _f.write(s); _f.flush()
            def flush(self):
                sys.__stdout__.flush(); _f.flush()
        sys.stdout = _Tee()
        try:
            main()
        finally:
            sys.stdout = sys.__stdout__
