"""
CatBoost (target_5d) standalone backtest
"""

import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from dataset import build_dataset
from evaluate import (
    _max_drawdown, _sharpe, precision_at_topk, run_backtest_5d,
    COMMISSION, SELL_TAX, SLIPPAGE, MIN_VOLUME_KRW,
)

MODELS_DIR = Path(__file__).parent.parent / "models"
MODEL_DIR  = MODELS_DIR / "target_5d_20260524_164927"
CAT_PKL    = MODEL_DIR / "model_cat.pkl"
SAVE_JSON  = MODEL_DIR / "backtest_5d_cat_only.json"

# LightGBM reference (user-provided)
LGBM_REF = {
    "gross_total": 546.0,
    "net_total":   368.0,
    "sharpe":      2.6,
    "mdd":        -26.9,
    "win_rate":    60.8,
}

SEP  = "=" * 62
SEP2 = "-" * 62


def _compute_rets(y_proba, val_meta, top_k=10):
    def _net(r):
        return (1 + r) * (1 - SLIPPAGE - COMMISSION) * (1 - SLIPPAGE - COMMISSION - SELL_TAX) - 1

    df = val_meta[["symbol", "date", "ret_fwd_5d"]].copy()
    df["ret_fwd_5d"] = df["ret_fwd_5d"].fillna(0.0)
    df["y_proba"] = y_proba
    if "volume_krw" in val_meta.columns:
        df["volume_krw"] = val_meta["volume_krw"].fillna(0)

    rebal_dates = sorted(df["date"].unique())[::5]
    gross_rets, net_rets, dates = [], [], []
    for date in rebal_dates:
        grp = df[df["date"] == date].copy()
        if "volume_krw" in grp.columns:
            grp = grp[grp["volume_krw"] >= MIN_VOLUME_KRW]
        top = grp.sort_values("y_proba", ascending=False).head(top_k)
        if top.empty:
            continue
        gross_rets.append(float(top["ret_fwd_5d"].mean()))
        net_rets.append(float(top["ret_fwd_5d"].apply(_net).mean()))
        dates.append(str(date))
    return gross_rets, net_rets, dates


def main():
    print()
    print(SEP)
    print("  CatBoost (target_5d) standalone backtest")
    print(SEP)

    # 1. Dataset
    print("\n[1/4] Loading dataset (1-2 min)...")
    X_train, y_train, X_val, y_val, val_meta, train_meta = build_dataset(
        target_col="target_5d", val_days=252
    )
    train_start = train_meta["date"].min()
    train_end   = train_meta["date"].max()
    val_start   = val_meta["date"].min()
    val_end     = val_meta["date"].max()
    print(f"  Train period : {train_start} ~ {train_end}  ({len(X_train):,} rows)")
    print(f"  Val period   : {val_start} ~ {val_end}  ({len(X_val):,} rows)")

    # 2. Look-ahead bias check
    print("\n[2/4] Look-ahead bias check...")
    overlap = set(train_meta["date"]) & set(val_meta["date"])
    if overlap:
        print(f"  [WARNING] Overlap found: {len(overlap)} dates")
        print(f"            {min(overlap)} ~ {max(overlap)}")
        print(f"            -> Excluding overlapping dates from backtest")
        mask = val_meta["date"].isin(overlap)
        val_meta_bt = val_meta[~mask].reset_index(drop=True)
        X_val_bt    = X_val[~mask.values]
        y_val_bt    = y_val[~mask.values]
    else:
        print(f"  [OK] No overlap -- no look-ahead bias")
        val_meta_bt = val_meta
        X_val_bt    = X_val
        y_val_bt    = y_val

    print(f"  Backtest period : {val_meta_bt['date'].min()} ~ {val_meta_bt['date'].max()}")
    print(f"  Trading days    : {val_meta_bt['date'].nunique()}")

    # 3. Model load + predict
    print(f"\n[3/4] Loading CatBoost model: {CAT_PKL}")
    with open(CAT_PKL, "rb") as f:
        model = pickle.load(f)
    print(f"  best_iteration = {model.best_iteration_}")

    cat_proba = model.predict_proba(X_val_bt)[:, 1]
    auc  = roc_auc_score(y_val_bt, cat_proba) if len(np.unique(y_val_bt)) == 2 else float("nan")
    topk = precision_at_topk(y_val_bt, cat_proba, val_meta_bt)
    print(f"  Val AUC       : {auc:.4f}")
    print(f"  Precision@10  : {topk.get(10, 0):.4f}")
    print(f"  Precision@20  : {topk.get(20, 0):.4f}")
    print(f"  Precision@30  : {topk.get(30, 0):.4f}")

    # 4. Backtest
    print("\n[4/4] Running 5-day hold backtest...")
    save_png = MODEL_DIR / "backtest_5d_cat_only.png"
    run_backtest_5d(cat_proba, val_meta_bt, top_k=10, save_path=save_png)

    gross_rets, net_rets, dates = _compute_rets(cat_proba, val_meta_bt)
    gross_ser = pd.Series(gross_rets, index=dates)
    net_ser   = pd.Series(net_rets,   index=dates)
    gross_cum = (1 + gross_ser).cumprod() - 1
    net_cum   = (1 + net_ser).cumprod() - 1

    gross_total = float(gross_cum.iloc[-1]) * 100 if len(gross_cum) else 0.0
    net_total   = float(net_cum.iloc[-1])   * 100 if len(net_cum)   else 0.0
    sharpe_net  = _sharpe(net_ser,   252 / 5)
    sharpe_grs  = _sharpe(gross_ser, 252 / 5)
    mdd_net     = _max_drawdown(net_cum)   * 100
    win_net     = float((net_ser > 0).mean()) * 100
    n_trades    = len(net_ser)

    # Save JSON
    result = {
        "model": "CatBoost only (target_5d_20260524_164927)",
        "backtest_period": f"{val_meta_bt['date'].min()} ~ {val_meta_bt['date'].max()}",
        "look_ahead_overlap_days": len(overlap),
        "gross_total_return_pct": round(gross_total, 2),
        "net_total_return_pct":   round(net_total,   2),
        "sharpe_ratio_net":       round(sharpe_net,  3),
        "sharpe_ratio_gross":     round(sharpe_grs,  3),
        "max_drawdown_pct":       round(mdd_net,     2),
        "win_rate_pct":           round(win_net,     1),
        "n_rebal_periods":        n_trades,
        "avg_holding_days":       5,
    }
    SAVE_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # Comparison table
    print()
    print(SEP)
    print("  Backtest Results Comparison")
    print(SEP)
    print(f"  Period   : {val_meta_bt['date'].min()} ~ {val_meta_bt['date'].max()}")
    print(f"  Strategy : Top-10 5-day equal-weight, rebal every 5 days")
    print(SEP2)
    print(f"  {'Metric':<22} {'LightGBM (prev)':>16} {'CatBoost (cur)':>16}")
    print(SEP2)

    rows = [
        ("Total Return (Gross)", LGBM_REF["gross_total"], gross_total,  True),
        ("Total Return (Net)",   LGBM_REF["net_total"],   net_total,    True),
        ("Sharpe Ratio (Net)",   LGBM_REF["sharpe"],      sharpe_net,   True),
        ("Max Drawdown",         LGBM_REF["mdd"],         mdd_net,      False),
        ("Win Rate",             LGBM_REF["win_rate"],    win_net,      True),
    ]

    for label, ref, cur, higher_better in rows:
        ref_s = f"{ref:>13.1f}%" if ref is not None else f"{'N/A':>14}"
        diff  = cur - ref if ref is not None else 0
        arrow = "^" if diff > 0 else ("v" if diff < 0 else " ")
        good  = (diff > 0) == higher_better
        mark  = "*" if good and abs(diff) > 0.1 else " "
        cur_s = f"{cur:>13.1f}% {arrow}{mark}"
        print(f"  {label:<22} {ref_s:>16} {cur_s:>18}")

    print(SEP2)
    print(f"  {'# Rebal Periods':<22} {'N/A':>16} {n_trades:>15}")
    print(f"  {'Avg Holding Days':<22} {'5':>16} {'5':>16}")
    print(SEP)
    print(f"\n  Saved: {SAVE_JSON}")
    print(f"  Chart: {save_png}")
    print()

    # Korean summary
    print(SEP)
    print("  [KR] 백테스트 결과 요약")
    print(SEP)
    print(f"  백테스트 기간: {val_meta_bt['date'].min()} ~ {val_meta_bt['date'].max()}")
    print(f"  데이터 누수  : {'없음 (학습/백테스트 기간 완전 분리)' if not overlap else str(len(overlap)) + '일 중복 -> 제외 처리'}")
    print(SEP2)
    print(f"  {'지표':<18} {'LightGBM':>14} {'CatBoost':>14}")
    print(SEP2)

    kr_rows = [
        ("총수익(비용 전)", LGBM_REF["gross_total"], gross_total),
        ("총수익(비용 후)", LGBM_REF["net_total"],   net_total),
        ("샤프비율(Net)",  LGBM_REF["sharpe"],      sharpe_net),
        ("MDD",           LGBM_REF["mdd"],         mdd_net),
        ("승률",           LGBM_REF["win_rate"],    win_net),
    ]
    for label, ref, cur in kr_rows:
        is_pct = label != "샤프비율(Net)"
        ref_s = f"{ref:>11.1f}%" if is_pct else f"{ref:>11.2f} "
        cur_s = f"{cur:>11.1f}%" if is_pct else f"{cur:>11.2f} "
        print(f"  {label:<18} {ref_s:>14} {cur_s:>14}")

    print(SEP2)
    print(f"  {'거래 횟수(5일)':<18} {'N/A':>14} {n_trades:>13}회")
    print(f"  {'평균 보유기간':<18} {'5일':>14} {'5일':>14}")
    print(SEP)
    print()


if __name__ == "__main__":
    main()
