"""
[검증 전용 — SELECT만, 모델/DB 변경 없음]
combo_discovery v2 후속:
  [1] PASS 5개 조합의 절대 20d 수익률 비교 (단독 최고 대비 개선 여부)
  [2] 최근 기준일 조합별 교집합 종목의 Jaccard 유사도 행렬
"""
import sqlite3
import sys
import time
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

OUT_LOG = Path(r"C:\Users\kkkhe\AppData\Local\Temp\combo_followup_out.txt")


def biz_year_pit_vec(date_int_arr: np.ndarray) -> np.ndarray:
    year  = date_int_arr // 10000
    month = (date_int_arr % 10000) // 100
    return np.where(month >= 4, year - 1, year - 2)


def load_prices(conn):
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
    g = df.groupby("symbol", group_keys=False)
    df[HORIZON]      = g["close"].transform(lambda s: s.shift(-20) / s - 1)
    df["value_ma20"] = g["value"].transform(lambda s: s.rolling(20).mean())
    df["liquid"]     = df["value_ma20"] >= MIN_VOLUME_KRW
    return df


def pit_merge(prices, annual_df, val_col):
    annual = annual_df[["symbol", "biz_year", val_col]].copy()
    annual["symbol"] = annual["symbol"].astype(str).str.zfill(6)
    annual = annual.rename(columns={"biz_year": "biz_year_pit"})
    merged = prices[["symbol", "biz_year_pit"]].merge(
        annual, on=["symbol", "biz_year_pit"], how="left"
    )
    return pd.Series(merged[val_col].values, index=prices.index)


def build_all_conds(prices, conn):
    eps_df, bps_df, dps_df = load_factor_data(conn)

    # PER
    eps_pit = pit_merge(prices, eps_df, "eps")
    per_pit = prices["close"] / eps_pit.where(eps_pit > 0)
    rank_per = prices.assign(_v=per_pit).groupby("date")["_v"].rank(pct=True)
    cond_low_per = (rank_per <= 0.2) & per_pit.notna()

    # PBR
    bps_pit = pit_merge(prices, bps_df, "bps")
    pbr_pit = prices["close"] / bps_pit.where(bps_pit > 0)
    rank_pbr = prices.assign(_v=pbr_pit).groupby("date")["_v"].rank(pct=True)
    cond_low_pbr = (rank_pbr <= 0.2) & pbr_pit.notna()

    # 고배당
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
    rank_div  = prices.assign(_v=div_yield).groupby("date")["_v"].rank(pct=True)
    cond_high_div = (rank_div >= 0.8) & div_yield.notna() & (div_yield < 0.5)

    # 60d 팩터
    eps_sig = compute_eps_signals(eps_df)
    roe_sig = compute_roe_signals(eps_df, bps_df)
    dps_sig = compute_dps_signals(dps_df)
    bps_sig = compute_bps_signals(bps_df)

    def pit_sig(sig_df, val_col):
        return pit_merge(prices, sig_df[["symbol", "biz_year", val_col]], val_col)

    dps_growth = pit_sig(dps_sig, "dps_growth_yoy")
    eps_accel  = pit_sig(eps_sig, "eps_growth_accel")

    cond_div_growth = (dps_growth > 0) & dps_growth.notna()
    cond_eps_accel  = (eps_accel  > 0) & eps_accel.notna()

    return {
        "고배당":   cond_high_div,
        "저PER":    cond_low_per,
        "저PBR":    cond_low_pbr,
        "배당성장": cond_div_growth,
        "EPS가속":  cond_eps_accel,
    }


def combo_abs_stats(prices, cond_a, cond_b, years):
    """절대 20d 수익률 기준: combo(A&B) vs. best_single(A or B)"""
    mask_year = prices["date"].str[:4].astype(int).isin(years)
    base = prices[mask_year & prices["liquid"]].copy()
    ca = cond_a.reindex(prices.index, fill_value=False).loc[base.index]
    cb = cond_b.reindex(prices.index, fill_value=False).loc[base.index]

    def stats_of(mask):
        s = base.loc[mask, HORIZON].dropna()
        if len(s) < 5:
            return None, None, None, len(s)
        return s.mean() * 100, (s > 0).mean(), s, len(s)

    a_mean, a_win, a_s, a_n = stats_of(ca & ~cb)
    b_mean, b_win, b_s, b_n = stats_of(~ca & cb)
    c_mean, c_win, c_s, c_n = stats_of(ca & cb)

    if a_mean is not None and b_mean is not None:
        best_mean = max(a_mean, b_mean)
        best_s    = a_s if a_mean >= b_mean else b_s
    elif a_mean is not None:
        best_mean, best_s = a_mean, a_s
    elif b_mean is not None:
        best_mean, best_s = b_mean, b_s
    else:
        best_mean, best_s = None, None

    diff_p = None
    dir_ok = (c_mean is not None and best_mean is not None and c_mean > best_mean)
    if c_s is not None and best_s is not None and len(c_s) >= 5 and len(best_s) >= 5:
        _, diff_p = stats.mannwhitneyu(c_s, best_s, alternative="greater")

    return {
        "C_n": c_n, "C_mean%": c_mean, "best_mean%": best_mean,
        "dir_ok": dir_ok, "p": round(diff_p, 4) if diff_p is not None else None,
    }


def jaccard_at_latest(prices, named_conds: dict):
    """최근 기준일(liquid 조건 충족 날짜 기준)의 각 조합 종목 집합으로 Jaccard 계산."""
    latest_date = prices.loc[prices["liquid"], "date"].max()
    snap = prices[prices["date"] == latest_date].reset_index(drop=True)
    snap_idx = snap.index  # prices 전체 인덱스가 아니라 snap 내 인덱스

    sets = {}
    for name, cond in named_conds.items():
        c = cond.reindex(prices.index, fill_value=False).iloc[snap.index.map(
            dict(zip(snap.index, range(len(snap))))
        ).values] if False else None
        # 더 안전하게: snap의 prices 내 위치를 직접 찾음
        pos_in_prices = prices[prices["date"] == latest_date].index
        c_vals = cond.reindex(prices.index, fill_value=False).loc[pos_in_prices]
        syms = set(prices.loc[pos_in_prices[c_vals.values], "symbol"])
        sets[name] = syms

    names = list(sets.keys())
    n = len(names)
    matrix = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            a, b = sets[names[i]], sets[names[j]]
            union = len(a | b)
            matrix[i, j] = len(a & b) / union if union > 0 else 0.0

    return names, matrix, sets, latest_date


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    def log(msg): print(msg, flush=True)

    log("=" * 65)
    log("[데이터 로드]")
    log("=" * 65)
    prices = load_prices(conn)
    log(f"  prices: {len(prices):,}행  ({time.time()-t0:.0f}s)")

    log("\n[조건 계산]")
    conds = build_all_conds(prices, conn)
    conn.close()
    log(f"  완료  ({time.time()-t0:.0f}s)")

    # ── 검증 대상 5개 조합 ──
    TARGET_COMBOS = [
        ("고배당", "저PER"),
        ("저PBR",  "배당성장"),
        ("고배당", "EPS가속"),
        ("고배당", "배당성장"),
        ("고배당", "저PBR"),
    ]

    log("\n" + "=" * 65)
    log("[1] 절대 20d 수익률 — 조합 vs 단독 최고")
    log("=" * 65)
    log(f"{'조합':<18} {'IS_dir':>7} {'IS_mean%':>9} {'OOS_dir':>8} "
        f"{'OOS_mean%':>10} {'best_mean%':>11} {'OOS_p':>8}  판정")
    log("-" * 80)

    abs_results = {}
    for a_name, b_name in TARGET_COMBOS:
        ca, cb = conds[a_name], conds[b_name]
        is_r   = combo_abs_stats(prices, ca, cb, IN_SAMPLE_YEARS)
        oos_r  = combo_abs_stats(prices, ca, cb, OOS_YEARS)

        both_dir   = is_r["dir_ok"] and oos_r["dir_ok"]
        oos_p_ok   = (oos_r["p"] is not None and oos_r["p"] < 0.05)
        verdict = "✅ 절대수익도 통과" if (both_dir and oos_p_ok) else "⚠️ 초과승률만 통과"

        def f(v): return f"{v:+.2f}%" if v is not None else "  N/A"
        def fdir(b): return "✓" if b else "✗"

        p_str = f"{oos_r['p']:.4f}" if oos_r['p'] is not None else "N/A"
        log(f"{a_name}+{b_name:<8} {fdir(is_r['dir_ok']):>7} {f(is_r['C_mean%']):>9} "
            f"{fdir(oos_r['dir_ok']):>8} {f(oos_r['C_mean%']):>10} "
            f"{f(oos_r['best_mean%']):>11} {p_str:>8}  {verdict}")

        abs_results[f"{a_name}+{b_name}"] = {"oos_dir": oos_r["dir_ok"], "oos_p_ok": oos_p_ok, "verdict": verdict}

    # ── [2] Jaccard 행렬 ──
    log("\n" + "=" * 65)
    log("[2] 최근 기준일 Jaccard 유사도 행렬")
    log("=" * 65)

    combo_conds = {}
    for a_name, b_name in TARGET_COMBOS:
        key = f"{a_name}+{b_name}"
        combo_conds[key] = conds[a_name] & conds[b_name]

    names, matrix, sets, latest_date = jaccard_at_latest(prices, combo_conds)
    log(f"  기준일: {latest_date}")
    log(f"  각 조합 종목 수: " + ", ".join(f"{n}={len(sets[n])}" for n in names))
    log("")

    # 헤더
    short = [n.replace("고배당+", "HD+").replace("저PBR+", "LPB+")
              .replace("저PER", "LPE").replace("배당성장", "DG")
              .replace("EPS가속", "EA").replace("저PBR", "LPB")
              for n in names]

    col_w = 10
    log(" " * 20 + "".join(f"{s:>{col_w}}" for s in short))
    log("-" * (20 + col_w * len(names)))
    for i, row_name in enumerate(names):
        row_short = short[i]
        row_str = f"{row_short:<20}"
        for j in range(len(names)):
            v = matrix[i, j]
            marker = " ⚠️" if (i != j and v > 0.7) else ""
            row_str += f"{v:>{col_w}.3f}{'' if not marker else ''}"
            if marker:
                row_str = row_str.rstrip() + marker
        log(row_str)

    log("\n  (⚠️ = Jaccard > 0.7, 중복 과다)")

    # 최종 요약
    log("\n" + "=" * 65)
    log("[최종 판정 요약]")
    log("=" * 65)
    log(f"{'조합':<20} {'초과승률':>8} {'절대수익':>8} {'최종'}")
    log("-" * 50)
    for a_name, b_name in TARGET_COMBOS:
        key = f"{a_name}+{b_name}"
        r = abs_results[key]
        # 초과승률은 v2에서 모두 PASS
        abs_v = "✅" if (r["oos_dir"] and r["oos_p_ok"]) else "⚠️"
        final = "✅ 프리셋 확정" if abs_v == "✅" else "⚠️ 보류"
        if key == "고배당+저PBR":
            final = "✅ 기존 운영"
        log(f"{key:<20} {'✅':>8} {abs_v:>8}  {final}")

    log(f"\n총 소요: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    with open(OUT_LOG, "w", encoding="utf-8") as _f:
        class _Tee:
            def write(self, s):
                sys.__stdout__.write(s); _f.write(s); _f.flush()
            def flush(self):
                sys.__stdout__.flush(); _f.flush()
        sys.stdout = _Tee()
        try:
            main()
        finally:
            sys.stdout = sys.__stdout__
