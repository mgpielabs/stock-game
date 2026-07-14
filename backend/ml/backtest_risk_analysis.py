"""
MDD 원인 분석 + 리스크 필터 백테스트
  1. 낙폭 구간 추출 & 원인 분석 (KOSPI, 변동성, 추천 종목)
  2. 필터 A/B/C 단독·조합 백테스트
  3. 결과 비교표 출력
"""

import json
import pickle
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from dataset import build_dataset, DB_PATH
from evaluate import COMMISSION, SELL_TAX, SLIPPAGE, MIN_VOLUME_KRW, _max_drawdown, _sharpe

MODELS_DIR = Path(__file__).parent.parent / "models"
MODEL_DIR  = MODELS_DIR / "target_5d_20260524_164927"
CAT_PKL    = MODEL_DIR / "model_cat.pkl"
SEP  = "=" * 66
SEP2 = "-" * 66

BASELINE = dict(net_total=396.3, mdd=-33.35, sharpe=2.551, win=64.7)


# ── 비용 차감 ─────────────────────────────────────────────────

def _net(r: float) -> float:
    if pd.isna(r):
        return 0.0
    return (1 + r) * (1 - SLIPPAGE - COMMISSION) * (1 - SLIPPAGE - COMMISSION - SELL_TAX) - 1


def _stats(net_ser: pd.Series, gross_ser: pd.Series) -> Dict:
    net_ser   = net_ser.fillna(0.0)
    gross_ser = gross_ser.fillna(0.0)
    if net_ser.empty:
        return dict(gross_total=0, net_total=0, sharpe=0, mdd=0, win=0, n=0)
    gc = (1 + gross_ser).cumprod() - 1
    nc = (1 + net_ser).cumprod()   - 1
    return dict(
        gross_total = float(gc.iloc[-1]) * 100,
        net_total   = float(nc.iloc[-1]) * 100,
        sharpe      = _sharpe(net_ser, 252 / 5),
        mdd         = _max_drawdown(nc) * 100,
        win         = float((net_ser > 0).mean()) * 100,
        n           = len(net_ser),
    )


# ── 낙폭 구간 탐지 (peak-to-trough 정확 추적) ────────────────

def find_drawdown_periods(dates: List[str], cum_rets: List[float], threshold: float = -10.0):
    vals = np.array([1 + r / 100 for r in cum_rets])
    n    = len(vals)
    periods = []

    peak_val   = vals[0];  peak_idx   = 0
    trough_val = vals[0];  trough_idx = 0
    in_dd = False

    for i in range(1, n):
        # 현재 값이 이전 최고치보다 낮으면 trough 업데이트
        if vals[i] < trough_val:
            trough_val = vals[i]
            trough_idx = i

        cur_dd = (trough_val / peak_val - 1) * 100

        # 낙폭이 threshold 이하로 진입
        if cur_dd <= threshold and not in_dd:
            in_dd = True

        # 현재 값이 직전 peak 이상 → 회복
        if vals[i] >= peak_val:
            if in_dd:
                actual_dd = (trough_val / peak_val - 1) * 100
                if actual_dd <= threshold:
                    periods.append(dict(
                        start         = dates[peak_idx + 1] if peak_idx + 1 < n else dates[peak_idx],
                        end           = dates[trough_idx],
                        peak_date     = dates[peak_idx],
                        recovery_date = dates[i],
                        drawdown_pct  = round(actual_dd, 2),
                        peak_val      = round(float(peak_val), 4),
                        trough_val    = round(float(trough_val), 4),
                    ))
                in_dd = False
            # 새로운 peak 업데이트
            peak_val = vals[i];   peak_idx   = i
            trough_val = vals[i]; trough_idx = i

    # 시리즈 끝까지 회복 못한 낙폭
    if in_dd:
        actual_dd = (trough_val / peak_val - 1) * 100
        if actual_dd <= threshold:
            periods.append(dict(
                start         = dates[peak_idx + 1] if peak_idx + 1 < n else dates[peak_idx],
                end           = dates[trough_idx],
                peak_date     = dates[peak_idx],
                recovery_date = None,
                drawdown_pct  = round(actual_dd, 2),
                peak_val      = round(float(peak_val), 4),
                trough_val    = round(float(trough_val), 4),
            ))

    return sorted(periods, key=lambda x: x["drawdown_pct"])


# ── KOSPI 시계열 ──────────────────────────────────────────────

def load_kospi_series() -> pd.Series:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            "SELECT date, close FROM market_index WHERE code='1001' ORDER BY date", conn
        )
    return df.set_index("date")["close"]


# ── 필터 백테스트 시뮬레이션 ──────────────────────────────────

def run_filtered(
    df: pd.DataFrame,
    kospi_5d: pd.Series,
    top_k: int = 10,
    filter_A: bool = False,
    filter_B: bool = False,
    filter_C: bool = False,
) -> Tuple[pd.Series, pd.Series, List[Dict]]:
    """
    df 필요 컬럼: date, symbol, ret_fwd_5d, volume_krw, y_proba, volatility_20, mkt
    """
    unique_dates = sorted(df["date"].unique())
    rebal_dates  = unique_dates[::5]

    gross_rets, net_rets, dates = [], [], []
    picks_log: List[Dict] = []

    for date in rebal_dates:
        grp = df[df["date"] == date].copy()
        grp = grp[grp["volume_krw"].fillna(0) >= MIN_VOLUME_KRW]
        if grp.empty:
            continue

        # Filter A: volatility_20 상위 10% 제외
        if filter_A and "volatility_20" in grp.columns:
            thr = grp["volatility_20"].quantile(0.90)
            grp = grp[grp["volatility_20"] <= thr]

        # Filter B: KOSPI 5일 수익률 < -3% → top_k 절반
        cur_k = top_k
        if filter_B:
            k5 = kospi_5d.get(date, np.nan)
            if pd.notna(k5) and k5 < -0.03:
                cur_k = max(top_k // 2, 1)

        ranked = grp.sort_values("y_proba", ascending=False)

        # Filter C: market(KOSPI/KOSDAQ)별 최대 3종목
        if filter_C and "mkt" in ranked.columns:
            selected, mkt_cnt = [], {}
            for _, row in ranked.iterrows():
                mkt = row.get("mkt", "UNK")
                if mkt_cnt.get(mkt, 0) < 3:
                    selected.append(row)
                    mkt_cnt[mkt] = mkt_cnt.get(mkt, 0) + 1
                if len(selected) >= cur_k:
                    break
            top = pd.DataFrame(selected)
        else:
            top = ranked.head(cur_k)

        if top.empty:
            continue

        picks_log.append(dict(date=date, symbols=list(top["symbol"])))

        ret5d = top["ret_fwd_5d"].fillna(0.0)
        gross_rets.append(float(ret5d.mean()))
        net_rets.append(float(ret5d.apply(_net).mean()))
        dates.append(str(date))

    return (
        pd.Series(gross_rets, index=dates),
        pd.Series(net_rets,   index=dates),
        picks_log,
    )


# ── 메인 ──────────────────────────────────────────────────────

def main():
    print()
    print(SEP)
    print("  MDD Analysis + Risk Filter Backtest")
    print(SEP)

    # 데이터셋 & 모델
    print("\n[1/5] Loading dataset (1-2 min)...")
    X_train, y_train, X_val, y_val, val_meta, train_meta = build_dataset(
        target_col="target_5d", val_days=252
    )
    print(f"  Train: {train_meta['date'].min()} ~ {train_meta['date'].max()} ({len(X_train):,})")
    print(f"  Val  : {val_meta['date'].min()} ~ {val_meta['date'].max()} ({len(X_val):,})")

    print("\n[2/5] Loading CatBoost model & predicting...")
    with open(CAT_PKL, "rb") as f:
        model = pickle.load(f)
    cat_proba = model.predict_proba(X_val)[:, 1]

    with sqlite3.connect(DB_PATH) as conn:
        stocks_df = pd.read_sql_query("SELECT symbol, market FROM stocks", conn)
    sym_to_mkt = stocks_df.set_index("symbol")["market"].to_dict()

    # 분석용 통합 DataFrame
    feat_r = X_val.reset_index(drop=True)
    meta_r = val_meta.reset_index(drop=True)

    df = meta_r[["symbol", "date", "ret_fwd_5d", "volume_krw"]].copy()
    df["ret_fwd_5d"]  = df["ret_fwd_5d"].fillna(0.0)
    df["y_proba"]      = cat_proba
    df["volatility_20"] = feat_r["volatility_20"].values
    df["mkt"]          = df["symbol"].map(sym_to_mkt).fillna("UNK")

    print("\n[3/5] Loading KOSPI series...")
    kospi = load_kospi_series()
    kospi.index = kospi.index.astype(str)
    kospi_5d = kospi.pct_change(5)
    kospi_5d.index = kospi_5d.index.astype(str)

    # ──────────────────────────────────────────────────────────
    # [1] 낙폭 구간 분석
    # ──────────────────────────────────────────────────────────
    print("\n" + SEP)
    print("  [1] Drawdown Period Analysis (>= -10%)")
    print(SEP)

    with open(MODEL_DIR / "backtest_5d_data.json") as f:
        bt_data = json.load(f)

    dd_periods = find_drawdown_periods(
        bt_data["dates"], bt_data["cumulative_return_pct"], threshold=-10.0
    )

    if not dd_periods:
        print("  No drawdown >= -10% detected.")
    else:
        for i, p in enumerate(dd_periods):
            print(f"\n  Drawdown #{i+1}  [{p['start']} ~ {p['end']}]")
            print(f"    Drawdown     : {p['drawdown_pct']:.2f}%")
            print(f"    Peak date    : {p['peak_date']}")
            print(f"    Recovery     : {p['recovery_date'] or 'Not recovered yet'}")

            # KOSPI 변동
            k_sub = kospi[(kospi.index >= p['peak_date']) & (kospi.index <= p['end'])]
            if len(k_sub) >= 2:
                k_chg = (k_sub.iloc[-1] / k_sub.iloc[0] - 1) * 100
                print(f"    KOSPI change : {k_chg:+.2f}% (peak~trough)")
            else:
                print(f"    KOSPI change : N/A")

            # 구간 내 추천 후보 변동성 평균
            p_mask = (df["date"] >= p["start"]) & (df["date"] <= p["end"])
            p_df   = df[p_mask & (df["volume_krw"] >= MIN_VOLUME_KRW)]
            if not p_df.empty:
                avg_vol = p_df["volatility_20"].mean()
                print(f"    Avg vol_20 (universe) : {avg_vol*100:.2f}% annualized")

        # 최악 낙폭 구간 Top picks 분석
        worst = dd_periods[0]
        print(f"\n  === Worst Drawdown [{worst['start']} ~ {worst['end']}] -- Top Picks ===")

        w_unique = sorted(df["date"].unique())
        w_rebal  = [d for d in w_unique[::5]
                    if worst["start"] <= d <= worst["end"]]

        all_picks = []
        for date in w_rebal:
            grp = df[df["date"] == date].copy()
            grp = grp[grp["volume_krw"].fillna(0) >= MIN_VOLUME_KRW]
            top = grp.sort_values("y_proba", ascending=False).head(10)
            top = top.copy()
            top["rebal_date"] = date
            all_picks.append(top)

        if all_picks:
            picks_df = pd.concat(all_picks, ignore_index=True)
            sym_stats = (
                picks_df.groupby("symbol")
                .agg(count=("rebal_date","count"),
                     avg_ret5d=("ret_fwd_5d","mean"),
                     avg_vol20=("volatility_20","mean"),
                     mkt=("mkt","first"))
                .sort_values("count", ascending=False)
                .head(10)
            )
            overall_90 = df[df["volume_krw"].fillna(0) >= MIN_VOLUME_KRW]["volatility_20"].quantile(0.90)
            worst_avg  = picks_df["volatility_20"].mean()

            print(f"\n  {'Symbol':<12} {'Times':>6} {'AvgRet5d':>10} {'AvgVol20':>10} {'Market':>8}")
            print(f"  {'-'*52}")
            for sym, row in sym_stats.iterrows():
                flag = " *" if row["avg_vol20"] > overall_90 else ""
                print(f"  {sym:<12} {int(row['count']):>6} "
                      f"{row['avg_ret5d']*100:>9.2f}% "
                      f"{row['avg_vol20']*100:>9.2f}%{flag} "
                      f"{row['mkt']:>8}")

            print(f"\n  (* = above 90th pct vol_20 threshold: {overall_90*100:.2f}%)")
            print(f"  Worst-DD picks avg vol_20 : {worst_avg*100:.2f}%  vs  90th: {overall_90*100:.2f}%")
            arrow = "MORE" if worst_avg > overall_90 else "WITHIN"
            print(f"  -> Worst-period picks were {arrow} volatile than 90th percentile")

            mkt_dist = picks_df["mkt"].value_counts()
            print(f"\n  Market dist. in worst-DD picks:")
            for mkt, cnt in mkt_dist.items():
                print(f"    {mkt:<12}: {cnt:>4} picks ({cnt/len(picks_df)*100:.1f}%)")

            # KOSPI 5d 상황
            dd_dates = sorted(set(picks_df["rebal_date"]))
            print(f"\n  KOSPI 5d returns on each rebalance date in worst-DD:")
            for d in dd_dates:
                k5 = kospi_5d.get(d, np.nan)
                flag = " <-- FILTER-B would trigger" if pd.notna(k5) and k5 < -0.03 else ""
                print(f"    {d}: {k5*100:+.2f}%{flag}" if pd.notna(k5) else f"    {d}: N/A")

    # ──────────────────────────────────────────────────────────
    # [2] 필터 백테스트
    # ──────────────────────────────────────────────────────────
    print("\n" + SEP)
    print("  [2] Risk Filter Backtest (5-day hold, top-10)")
    print(SEP)

    configs = [
        ("No Filter (baseline)",  False, False, False),
        ("A: High-vol exclude",   True,  False, False),
        ("B: Market-risk halve",  False, True,  False),
        ("C: Sector diversify",   False, False, True),
        ("A+B combined",          True,  True,  False),
        ("A+B+C combined",        True,  True,  True),
    ]

    results = []
    for label, fa, fb, fc in configs:
        gross_s, net_s, picks = run_filtered(df, kospi_5d, top_k=10,
                                             filter_A=fa, filter_B=fb, filter_C=fc)
        s = _stats(net_s, gross_s)
        s["label"] = label
        results.append(s)
        print(f"  {label:<30}  net={s['net_total']:+.1f}%  MDD={s['mdd']:.1f}%  "
              f"sharpe={s['sharpe']:.2f}  win={s['win']:.1f}%")

    # ──────────────────────────────────────────────────────────
    # [3] 비교표
    # ──────────────────────────────────────────────────────────
    print()
    print(SEP)
    print("  [3] Filter Comparison Table")
    print(SEP)
    print(f"  {'Filter':<28} {'Net(%)'  :>9} {'MDD(%)'  :>8} "
          f"{'Sharpe':>8} {'Win(%)':>8} {'N':>4}  Goal")
    print(SEP2)
    for r in results:
        mdd_ok  = abs(r["mdd"]) <= 28.0
        goal    = "<= -28% MDD OK" if mdd_ok else ""
        print(
            f"  {r['label']:<28} "
            f"{r['net_total']:>+8.1f}% "
            f"{r['mdd']:>7.1f}% "
            f"{r['sharpe']:>8.2f} "
            f"{r['win']:>7.1f}% "
            f"{r['n']:>4}  {goal}"
        )
    print(SEP)

    # ──────────────────────────────────────────────────────────
    # [4] 권장 조합
    # ──────────────────────────────────────────────────────────
    print("\n" + SEP)
    print("  [4] Recommendation (MDD <= -28%, max Net Return)")
    print(SEP)

    candidates = [r for r in results if r["mdd"] >= -28.0]
    if candidates:
        best = max(candidates, key=lambda x: x["net_total"])
    else:
        best = min(results[1:], key=lambda x: abs(x["mdd"]))  # fallback: lowest MDD (excl baseline)

    print(f"\n  Recommended: [{best['label']}]")
    print(f"    Net Total : {best['net_total']:+.1f}%  (baseline: +{BASELINE['net_total']:.1f}%)")
    print(f"    MDD       : {best['mdd']:.1f}%  (baseline: {BASELINE['mdd']:.1f}%)")
    print(f"    Sharpe    : {best['sharpe']:.2f}  (baseline: {BASELINE['sharpe']:.2f})")
    print(f"    Win Rate  : {best['win']:.1f}%  (baseline: {BASELINE['win']:.1f}%)")

    print(f"\n  Filter effect vs no-filter:")
    base = results[0]
    for r in results[1:]:
        mdd_d = r["mdd"] - base["mdd"]
        ret_d = r["net_total"] - base["net_total"]
        print(f"    {r['label']:<28}  MDD {mdd_d:+.1f}%p  Net {ret_d:+.1f}%p  Sharpe {r['sharpe']-base['sharpe']:+.2f}")

    print()
    print("  NOTE: Server application NOT performed.")
    print("        Verify here before applying to predictor.py / main.py")
    print(SEP)
    print()

    # JSON 저장
    out = {
        "drawdown_periods": dd_periods,
        "filter_results": results,
        "recommendation": best,
    }
    out_path = MODEL_DIR / "risk_analysis.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved: {out_path}")


if __name__ == "__main__":
    main()
