#!/usr/bin/env python3
"""
investor_flow_signal_scan.py
수급(외국인/연기금/기관) 연속 순매수 신호 탐색 스캔

IS: 2021-01 ~ 2024-12  (발견셋)
OOS: 2025-01 ~ 2026-06 (검증셋)

투자자 × 연속조건 × 수익시점 전수 스캔
초과수익률 = 해당 날 전종목 평균 대비 차감 (시장 드리프트 제거)
다중검정 보정: Bonferroni
"""
import sys
import sqlite3
import numpy as np
import pandas as pd
from scipy import stats as sp_stats
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB   = Path(__file__).resolve().parent.parent / "data" / "stocks.db"
IS   = ("20210101", "20241231")
OOS  = ("20250101", "20260630")
HOLD = [5, 20, 40]
ALPHA  = 0.05
MIN_N  = 100   # 조합당 최소 샘플


# ─── 로드 ────────────────────────────────────────────────────────────────────

def load_all():
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")

    print("1/4  investor_trading_kis_detail 로딩...", flush=True)
    flow = pd.read_sql_query("""
        SELECT symbol, date,
               COALESCE(foreign_value,    0) AS frgn,
               COALESCE(pension_value,    0) AS pens,
               COALESCE(inst_total_value, 0) AS inst,
               close                         AS kclose
        FROM investor_trading_kis_detail
        WHERE date BETWEEN '20210101' AND '20260630'
          AND symbol NOT LIKE '9%'
        ORDER BY symbol, date
    """, conn)
    print(f"     {len(flow):,} 행, {flow['symbol'].nunique():,} 종목", flush=True)

    print("2/4  prices 로딩...", flush=True)
    prices = pd.read_sql_query("""
        SELECT symbol, date, close
        FROM prices
        WHERE date BETWEEN '20210101' AND '20260630'
          AND symbol NOT LIKE '9%'
        ORDER BY symbol, date
    """, conn)
    print(f"     {len(prices):,} 행", flush=True)

    print("3/4  shares 로딩 (시가총액 정규화용)...", flush=True)
    shares = pd.read_sql_query("""
        SELECT symbol, MAX(shares_total) AS sh
        FROM financials
        WHERE shares_total > 0
        GROUP BY symbol
    """, conn)
    conn.close()

    sh_map = dict(zip(shares["symbol"], shares["sh"]))
    return flow, prices, sh_map


# ─── 매트릭스 빌드 ────────────────────────────────────────────────────────────

def build_mats(flow, prices, sh_map):
    print("4/4  매트릭스 피벗 중...", flush=True)

    # 공통 종목
    syms = sorted(set(flow["symbol"]) & set(prices["symbol"]))
    fl = flow[flow["symbol"].isin(syms)].copy()
    pr = prices[prices["symbol"].isin(syms)].copy()

    def piv(df, val):
        return df.pivot_table(index="date", columns="symbol", values=val, aggfunc="first")

    F  = piv(fl, "frgn")
    P  = piv(fl, "pens")
    I_ = piv(fl, "inst")
    KC = piv(fl, "kclose")
    PR = piv(pr, "close")

    dates = sorted(set(F.index) & set(PR.index))
    for m in [F, P, I_, KC, PR]:
        m.reindex(dates)

    F  = F.reindex(dates).reindex(columns=syms)
    P  = P.reindex(dates).reindex(columns=syms)
    I_ = I_.reindex(dates).reindex(columns=syms)
    KC = KC.reindex(dates).reindex(columns=syms)
    PR = PR.reindex(dates).reindex(columns=syms)

    print(f"     {len(dates)} 거래일 × {len(syms)} 종목", flush=True)

    # 시가총액 = KIS close × 발행주식 (최신 연간치 — 정규화 목적, 완벽한 PIT 불필요)
    sh = np.array([sh_map.get(s, np.nan) for s in syms], dtype=float)
    mktcap_vals = KC.values * sh[None, :]
    mktcap_vals[mktcap_vals == 0] = np.nan
    mktcap = pd.DataFrame(mktcap_vals, index=dates, columns=syms)

    return {
        "F":   F,  "P": P,  "I": I_,
        "F_n": F / mktcap,
        "P_n": P / mktcap,
        "I_n": I_ / mktcap,
        "PR":  PR,
        "dates": dates, "syms": syms,
    }


# ─── 신호 계산 ───────────────────────────────────────────────────────────────

def compute_signals(flow_mat: pd.DataFrame) -> dict:
    """
    반환: condition_name → float DataFrame (1.0=조건충족, 0.0=미충족, NaN=데이터부족)
    """
    pos = (flow_mat > 0).astype(float)
    pos = pos.where(flow_mat.notna())          # NaN flow → NaN signal

    s3   = pos.rolling(3,  min_periods=3).min()   # 1.0 iff all 3 positive
    s5   = pos.rolling(5,  min_periods=5).min()
    r5   = flow_mat.rolling(5,  min_periods=3).sum().rank(axis=1, pct=True, na_option="keep")
    r10  = flow_mat.rolling(10, min_periods=5).sum().rank(axis=1, pct=True, na_option="keep")

    to_signal = lambda r, thr: (r >= thr).astype(float).where(r.notna())

    return {
        "streak3":    s3,
        "streak5":    s5,
        "cum5_top20": to_signal(r5,  0.8),
        "cum10_top20":to_signal(r10, 0.8),
    }


# ─── 초과수익률 ──────────────────────────────────────────────────────────────

def compute_excess(price_mat: pd.DataFrame) -> dict:
    """hold → date×sym 초과수익률 (전종목 평균 차감)"""
    out = {}
    for n in HOLD:
        fwd = price_mat.shift(-n) / price_mat - 1
        mkt = fwd.mean(axis=1)
        out[n] = fwd.subtract(mkt, axis=0)
    return out


# ─── 통계 ────────────────────────────────────────────────────────────────────

def stats_block(sig_df: pd.DataFrame, exc_df: pd.DataFrame) -> dict:
    """
    sig_df: 1.0/0.0/NaN
    exc_df: 초과수익률 (float / NaN)
    """
    common = sig_df.index.intersection(exc_df.index)
    sig = sig_df.loc[common].values.ravel().astype(float)
    ret = exc_df.loc[common].values.ravel().astype(float)

    valid = np.isfinite(ret) & np.isfinite(sig)
    sv, rv = sig[valid], ret[valid]

    c  = rv[sv == 1.0]
    nc = rv[sv == 0.0]
    n  = len(c)

    if n < MIN_N:
        return dict(n=n, mean=np.nan, med=np.nan, wr=np.nan, diff=np.nan, pval=np.nan)

    mean = float(np.mean(c))
    med  = float(np.median(c))
    wr   = float(np.mean(c > 0))
    diff = float(np.mean(c) - np.mean(nc)) if len(nc) >= MIN_N else np.nan

    if len(nc) >= MIN_N:
        pval = float(sp_stats.ttest_ind(c, nc, equal_var=False).pvalue)
    else:
        pval = np.nan

    return dict(n=n, mean=mean, med=med, wr=wr, diff=diff, pval=pval)


# ─── 메인 ────────────────────────────────────────────────────────────────────

def main():
    flow, prices, sh_map = load_all()
    mats = build_mats(flow, prices, sh_map)

    dates    = mats["dates"]
    is_dates = set(d for d in dates if IS[0]  <= d <= IS[1])
    oo_dates = set(d for d in dates if OOS[0] <= d <= OOS[1])
    print(f"\nIS: {len(is_dates)}거래일  OOS: {len(oo_dates)}거래일")

    exc = compute_excess(mats["PR"])

    # ── 투자자별 flow 행렬 ────────────────────────────────────────────────────
    inv_flows = {
        "frgn_abs":  mats["F"],
        "frgn_norm": mats["F_n"],
        "pens_abs":  mats["P"],
        "pens_norm": mats["P_n"],
        "inst_abs":  mats["I"],
        "inst_norm": mats["I_n"],
    }

    # 동시 조합: streak은 AND, cum은 합산
    FP = mats["F"] + mats["P"]
    sig_f = compute_signals(mats["F"])
    sig_p = compute_signals(mats["P"])
    sig_fp = compute_signals(FP)

    def and_signal(a, b):
        """양쪽 모두 1.0이면 1.0, 어느 쪽이든 0.0이면 0.0, 어느 쪽이든 NaN이면 NaN"""
        both_valid = a.notna() & b.notna()
        result = ((a == 1.0) & (b == 1.0)).astype(float)
        return result.where(both_valid)

    sims = {
        "streak3":     and_signal(sig_f["streak3"],    sig_p["streak3"]),
        "streak5":     and_signal(sig_f["streak5"],    sig_p["streak5"]),
        "cum5_top20":  sig_fp["cum5_top20"],    # 합산 기준 상위20%
        "cum10_top20": sig_fp["cum10_top20"],
    }

    # ── 전수 스캔 ─────────────────────────────────────────────────────────────
    N_INVESTOR = len(inv_flows) + 1  # frgn_pens_sim 포함
    total_combos = N_INVESTOR * 4 * len(HOLD)
    bonf = ALPHA / total_combos
    print(f"총 조합: {total_combos}개  Bonferroni α = {bonf:.6f}\n")

    rows = []
    done = 0

    def record(inv, cond, hold, sig_df):
        nonlocal done
        done += 1

        s_is  = sig_df.loc[sig_df.index.isin(is_dates)]
        s_oos = sig_df.loc[sig_df.index.isin(oo_dates)]
        e_is  = exc[hold].loc[exc[hold].index.isin(is_dates)]
        e_oos = exc[hold].loc[exc[hold].index.isin(oo_dates)]

        is_s  = stats_block(s_is, e_is)
        oos_s = stats_block(s_oos, e_oos)

        is_bonf = (not np.isnan(is_s["pval"])) and (is_s["pval"] < bonf)

        rows.append({
            "investor": inv, "condition": cond, "hold_td": hold,
            "IS_n":    is_s["n"],    "IS_mean": is_s["mean"],  "IS_med":  is_s["med"],
            "IS_wr":   is_s["wr"],   "IS_diff": is_s["diff"],  "IS_pval": is_s["pval"],
            "IS_bonf": is_bonf,
            "OOS_n":   oos_s["n"],   "OOS_mean": oos_s["mean"], "OOS_med": oos_s["med"],
            "OOS_wr":  oos_s["wr"],  "OOS_diff": oos_s["diff"], "OOS_pval": oos_s["pval"],
        })

        if done % 20 == 0:
            print(f"  [{done}/{total_combos}] 진행중...", flush=True)

    # 단순 투자자
    for inv, fmat in inv_flows.items():
        print(f"  처리: {inv}", flush=True)
        sigs = compute_signals(fmat)
        for cond, sdf in sigs.items():
            for hold in HOLD:
                record(inv, cond, hold, sdf)

    # 동시 조합
    print("  처리: frgn_pens_sim", flush=True)
    for cond, sdf in sims.items():
        for hold in HOLD:
            record("frgn_pens_sim", cond, hold, sdf)

    # ── 결과 출력 ─────────────────────────────────────────────────────────────
    df = pd.DataFrame(rows)

    out = Path(__file__).resolve().parent / "investor_flow_signal_results.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n결과 저장: {out}")

    cols_disp = [
        "investor", "condition", "hold_td",
        "IS_n", "IS_mean", "IS_med", "IS_wr", "IS_pval", "IS_bonf",
        "OOS_n", "OOS_mean", "OOS_med", "OOS_wr", "OOS_pval",
    ]

    # 전체 테이블 (IS_mean 내림차순)
    print("\n" + "=" * 140)
    print("전체 결과 (IS 초과수익 내림차순, 단위: %로 표시)")
    print("=" * 140)
    disp = df[cols_disp].copy()
    for col in ["IS_mean", "IS_med", "IS_wr", "IS_diff", "OOS_mean", "OOS_med", "OOS_wr", "OOS_diff"]:
        if col in disp.columns:
            disp[col] = disp[col] * 100  # → %
    disp = disp.sort_values("IS_mean", ascending=False)
    print(disp.to_string(index=False, float_format=lambda x: f"{x:.3f}" if abs(x) < 1e6 else f"{x:.2e}"))

    # Bonferroni 보정 후 유의한 조합
    sig = df[df["IS_bonf"] == True].copy()
    print(f"\n{'='*140}")
    print(f"IS Bonferroni 보정 후 유의: {len(sig)}개 / {total_combos}개  (α={bonf:.6f})")

    if len(sig) == 0:
        print("  → 없음. 어떤 조합도 다중검정 보정 후 유의성을 통과하지 못함.")
        print("  → 수급 신호의 IS 효과 자체가 약하거나 존재하지 않음을 시사.")
    else:
        sig = sig.copy()
        # 생존 판정: IS와 OOS 초과수익 방향이 같고, OOS 데이터 존재
        sig["OOS_dir_ok"] = (
            np.sign(sig["IS_mean"].fillna(0)) == np.sign(sig["OOS_mean"].fillna(0))
        ) & sig["OOS_mean"].notna()

        survived = sig[sig["OOS_dir_ok"]]
        failed   = sig[~sig["OOS_dir_ok"]]

        print(f"\n★ IS+OOS 생존 (진짜 후보): {len(survived)}개")
        if len(survived) > 0:
            s_disp = survived[cols_disp].copy()
            for col in ["IS_mean", "IS_med", "IS_wr", "OOS_mean", "OOS_med", "OOS_wr"]:
                s_disp[col] = s_disp[col] * 100
            print(s_disp.sort_values("IS_mean", ascending=False).to_string(
                index=False, float_format=lambda x: f"{x:.3f}"))

        print(f"\n✗ OOS에서 무너짐 (방향 반전 or OOS 샘플 부족): {len(failed)}개")
        if len(failed) > 0:
            f_disp = failed[cols_disp].copy()
            for col in ["IS_mean", "IS_med", "IS_wr", "OOS_mean", "OOS_med", "OOS_wr"]:
                f_disp[col] = f_disp[col] * 100
            print(f_disp.sort_values("IS_mean", ascending=False).to_string(
                index=False, float_format=lambda x: f"{x:.3f}"))

    # ── pension 커버리지 진단 ─────────────────────────────────────────────────
    print("\n[연기금 데이터 커버리지 진단]")
    pens_pos = (mats["P"] > 0).sum(axis=1).mean()
    pens_nonzero = (mats["P"] != 0).sum(axis=1).mean()
    pens_pct = (mats["P"] > 0).values.mean() * 100
    print(f"  하루 평균 연기금 순매수(>0) 종목수: {pens_pos:.0f} / {len(mats['syms'])} 종목")
    print(f"  하루 평균 연기금 거래(≠0) 종목수:  {pens_nonzero:.0f}")
    print(f"  전체 셀 중 연기금 순매수(>0) 비율: {pens_pct:.2f}%")

    frgn_pct = (mats["F"] > 0).values.mean() * 100
    inst_pct = (mats["I"] > 0).values.mean() * 100
    print(f"  외국인 순매수 비율: {frgn_pct:.2f}%")
    print(f"  기관 순매수 비율:   {inst_pct:.2f}%")

    print("\n완료.")


if __name__ == "__main__":
    main()
