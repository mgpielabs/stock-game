"""
평가 지표 및 백테스트
  - AUC
  - Precision@TopK: 매일 상위 K종목 중 실제 라벨 1 비율
  - 백테스트: 매일 상위 top_k 종목 균등 투자, 현실적 비용 반영
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams['font.family'] = ['Malgun Gothic', 'DejaVu Sans']
matplotlib.rcParams['axes.unicode_minus'] = False
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

# ── 현실적 매매 비용 ──────────────────────────────────────────
COMMISSION      = 0.00015        # 수수료 0.015% (매수·매도 각각)
SELL_TAX        = 0.002          # 증권거래세 0.20% (매도 시)
SLIPPAGE        = 0.002          # 슬리피지 0.20% (매수·매도 각각, 소액 리테일 기준)
MIN_VOLUME_KRW  = 1_000_000_000  # 최소 일 거래대금 10억원 (저유동성 제외)
LIMIT_UP        = 0.29           # 상한가(29% 이상) 종목 매수 불가


def _net_ret(raw: float) -> float:
    """슬리피지 + 수수료 + 매도세 차감 후 순수익률"""
    buy_cost  = SLIPPAGE + COMMISSION             # 0.215%
    sell_cost = SLIPPAGE + COMMISSION + SELL_TAX  # 0.415%
    return (1 + raw) * (1 - buy_cost) * (1 - sell_cost) - 1


def compute_metrics(y_true: pd.Series, y_proba: np.ndarray) -> Dict[str, float]:
    if len(np.unique(y_true)) == 2:
        return {"auc": float(roc_auc_score(y_true, y_proba))}
    return {"auc": float("nan")}


def precision_at_topk(
    y_true: pd.Series,
    y_proba: np.ndarray,
    val_meta: pd.DataFrame,
    ks: List[int] = [10, 20, 30],
) -> Dict[int, float]:
    """매 거래일 예측 확률 상위 K 종목 중 실제 양성 비율의 평균."""
    df = val_meta[["symbol", "date"]].copy()
    df["y_true"]  = y_true.values
    df["y_proba"] = y_proba

    buckets: Dict[int, List[float]] = {k: [] for k in ks}
    for _, grp in df.groupby("date"):
        ranked = grp.sort_values("y_proba", ascending=False)
        for k in ks:
            top = ranked.head(k)
            if len(top) > 0:
                buckets[k].append(float(top["y_true"].mean()))

    return {k: float(np.mean(v)) if v else float("nan") for k, v in buckets.items()}


def run_backtest(
    y_proba: np.ndarray,
    val_meta: pd.DataFrame,
    top_k: int = 10,
    save_path: Optional[Path] = None,
) -> pd.Series:
    """
    매일 예측 확률 상위 top_k 종목 균등 투자 시뮬레이션.
    현실적 비용 (슬리피지·수수료·거래세) 및 필터 (저유동성·상한가) 적용.
    val_meta 필요 컬럼: symbol, date, ret_fwd_1d, (volume_krw 선택)
    반환: 날짜별 비용차감 누적수익률 Series
    """
    df = val_meta[["symbol", "date", "ret_fwd_1d"]].copy()
    df["ret_fwd_1d"] = df["ret_fwd_1d"].fillna(0.0)
    df["y_proba"]    = y_proba
    if "volume_krw" in val_meta.columns:
        df["volume_krw"] = val_meta["volume_krw"].fillna(0)

    gross_rets: List[float] = []
    net_rets:   List[float] = []
    dates:      List[str]   = []
    n_excluded: List[int]   = []

    for date, grp in df.groupby("date"):
        n_before = len(grp)

        # 거래량 필터: 일 거래대금 10억 미만 제외
        if "volume_krw" in grp.columns:
            grp = grp[grp["volume_krw"] >= MIN_VOLUME_KRW]

        # 상한가 종목 제외: 익일 상한가(29%↑)는 매수 불가
        grp = grp[grp["ret_fwd_1d"] < LIMIT_UP]

        n_excluded.append(n_before - len(grp))

        top = grp.sort_values("y_proba", ascending=False).head(top_k)
        if top.empty:
            continue

        gross_rets.append(float(top["ret_fwd_1d"].mean()))
        net_rets.append(float(top["ret_fwd_1d"].apply(_net_ret).mean()))
        dates.append(str(date))

    gross_series = pd.Series(gross_rets, index=dates, name="gross_ret")
    net_series   = pd.Series(net_rets,   index=dates, name="net_ret")
    gross_cum    = (1 + gross_series).cumprod() - 1
    net_cum      = (1 + net_series).cumprod() - 1

    avg_excluded = float(np.mean(n_excluded)) if n_excluded else 0.0

    if save_path is not None:
        _plot_backtest(gross_series, gross_cum, net_series, net_cum, top_k, save_path)
        _save_backtest_json(
            gross_series, gross_cum,
            net_series,   net_cum,
            avg_excluded,
            save_path.parent / "backtest_data.json",
        )

    return net_cum


def run_backtest_5d(
    y_proba: np.ndarray,
    val_meta: pd.DataFrame,
    top_k: int = 10,
    save_path: Optional[Path] = None,
    cooldown_days: int = 0,
    exclude_overheat: bool = False,
    overheat_rsi: float = 80.0,
    overheat_ret_5d: float = 0.30,
    feature_df: Optional[pd.DataFrame] = None,
) -> pd.Series:
    """
    5일 보유 전략 백테스트.
    매 5거래일마다 상위 top_k 종목 교체, ret_fwd_5d 기준.
    val_meta 필요 컬럼: symbol, date, ret_fwd_5d, (volume_krw 선택)
    feature_df: symbol, date, rsi_14, ret_5d 컬럼 포함 시 과열 필터 사용
    반환: 5일 구간별 비용차감 누적수익률 Series
    """
    df = val_meta[["symbol", "date", "ret_fwd_5d"]].copy()
    df["y_proba"] = y_proba
    if "volume_krw" in val_meta.columns:
        df["volume_krw"] = val_meta["volume_krw"].fillna(0)

    # ret_fwd_5d가 NaN인 행(데이터 끝자락이라 5거래일 후 종가를 아직 모르는 경우)은
    # 0%로 채우면 실제로는 알 수 없는 수익률이 "손실 없음"으로 둔갑함 → 후보군에서 제외
    df = df.dropna(subset=["ret_fwd_5d"])

    unique_dates = sorted(df["date"].unique())
    rebal_dates  = unique_dates[::5]
    date_to_idx  = {d: i for i, d in enumerate(unique_dates)}

    last_bought_idx: Dict[str, int] = {}

    gross_rets: List[float] = []
    net_rets:   List[float] = []
    dates:      List[str]   = []
    n_excluded: List[int]   = []

    for date in rebal_dates:
        grp = df[df["date"] == date].copy()
        n_before = len(grp)
        cur_idx  = date_to_idx[date]

        if "volume_krw" in grp.columns:
            grp = grp[grp["volume_krw"] >= MIN_VOLUME_KRW]

        # 쿨다운 필터: 마지막 매수 후 cooldown_days 거래일 이내 재매수 차단
        if cooldown_days > 0 and last_bought_idx:
            cooldown_syms = {
                sym for sym, last_idx in last_bought_idx.items()
                if (cur_idx - last_idx) <= cooldown_days
            }
            if cooldown_syms:
                grp = grp[~grp["symbol"].isin(cooldown_syms)]

        # 과열 필터: rsi_14 또는 ret_5d 기준 초과 종목 제외
        if exclude_overheat and feature_df is not None:
            day_feat = feature_df[feature_df["date"] == date][["symbol", "rsi_14", "ret_5d"]]
            if not day_feat.empty:
                grp = grp.merge(day_feat, on="symbol", how="left")
                oh_mask = pd.Series(False, index=grp.index)
                if "rsi_14" in grp.columns:
                    oh_mask |= grp["rsi_14"].fillna(0) >= overheat_rsi
                if "ret_5d" in grp.columns:
                    oh_mask |= grp["ret_5d"].fillna(0) >= overheat_ret_5d
                grp = grp[~oh_mask]

        n_excluded.append(n_before - len(grp))

        top = grp.sort_values("y_proba", ascending=False).head(top_k)
        if top.empty:
            continue

        # 쿨다운 업데이트: 선택된 종목의 마지막 매수 인덱스 갱신
        if cooldown_days > 0:
            for sym in top["symbol"]:
                last_bought_idx[sym] = cur_idx

        gross_rets.append(float(top["ret_fwd_5d"].mean()))
        net_rets.append(float(top["ret_fwd_5d"].apply(_net_ret).mean()))
        dates.append(str(date))

    gross_series = pd.Series(gross_rets, index=dates, name="gross_ret")
    net_series   = pd.Series(net_rets,   index=dates, name="net_ret")
    gross_cum    = (1 + gross_series).cumprod() - 1
    net_cum      = (1 + net_series).cumprod() - 1

    avg_excluded = float(np.mean(n_excluded)) if n_excluded else 0.0

    if save_path is not None:
        _plot_backtest_5d(gross_series, gross_cum, net_series, net_cum, top_k, save_path)
        _save_backtest_5d_json(
            gross_series, gross_cum,
            net_series, net_cum,
            avg_excluded,
            save_path.parent / "backtest_5d_data.json",
        )

    return net_cum


def _max_drawdown(cumret: pd.Series) -> float:
    peak = (1 + cumret).cummax()
    return float(((1 + cumret) / peak - 1).min())


def _sharpe(ret_series: pd.Series, periods_per_year: float = 252.0) -> float:
    return (
        float(ret_series.mean() / ret_series.std() * np.sqrt(periods_per_year))
        if ret_series.std() > 0 else 0.0
    )


def _save_backtest_5d_json(
    gross_ret: pd.Series,
    gross_cum: pd.Series,
    net_ret:   pd.Series,
    net_cum:   pd.Series,
    avg_excluded: float,
    json_path: Path,
) -> None:
    data = {
        "holding_days": 5,
        "dates":                 list(net_cum.index),
        "cumulative_return_pct": [round(v * 100, 3) for v in net_cum.values],
        "daily_return_pct":      [round(v * 100, 3) for v in net_ret.values],
        "total_return_pct":      round(float(net_cum.iloc[-1]) * 100, 2) if len(net_cum) else 0.0,
        "sharpe_ratio":          round(_sharpe(net_ret, 252 / 5), 3),
        "max_drawdown_pct":      round(_max_drawdown(net_cum) * 100, 2),
        "win_rate":              round(float((net_ret > 0).mean()), 3),
        "gross_total_return_pct": round(float(gross_cum.iloc[-1]) * 100, 2) if len(gross_cum) else 0.0,
        "gross_sharpe_ratio":     round(_sharpe(gross_ret, 252 / 5), 3),
        "gross_win_rate":         round(float((gross_ret > 0).mean()), 3),
        "costs_applied": {
            "commission_pct":  round(COMMISSION * 100, 4),
            "sell_tax_pct":    round(SELL_TAX * 100, 4),
            "slippage_pct":    round(SLIPPAGE * 100, 4),
            "min_volume_bn":   MIN_VOLUME_KRW / 1e9,
            "avg_excluded_per_day": round(avg_excluded, 1),
        },
    }
    json_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _plot_backtest_5d(
    gross_ret: pd.Series,
    gross_cum: pd.Series,
    net_ret:   pd.Series,
    net_cum:   pd.Series,
    top_k: int,
    save_path: Path,
) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

    axes[0].plot(gross_cum.index, gross_cum.values * 100, linewidth=1.0,
                 color="lightblue", alpha=0.7, label="비용 전 (Gross)")
    axes[0].plot(net_cum.index,   net_cum.values   * 100, linewidth=1.5,
                 color="steelblue", label="비용 후 (Net)")
    axes[0].axhline(0, color="gray", linewidth=0.8, linestyle="--")
    axes[0].set_title(
        f"Backtest 5D Hold — Top {top_k} 5-Day Equal-Weight Portfolio\n"
        f"비용: 수수료 {COMMISSION*100:.3f}%×2 + 거래세 {SELL_TAX*100:.2f}% + 슬리피지 {SLIPPAGE*100:.1f}%×2"
    )
    axes[0].set_ylabel("Cumulative Return (%)")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)

    colors = ["#d62728" if r < 0 else "#2ca02c" for r in net_ret.values]
    axes[1].bar(net_ret.index, net_ret.values * 100, color=colors, alpha=0.75, width=2.0)
    mean_ret = net_ret.mean()
    axes[1].axhline(
        mean_ret * 100, color="navy", linewidth=1.2, linestyle="--",
        label=f"평균 {mean_ret * 100:.2f}% (5일 비용 후)",
    )
    axes[1].set_title(f"5-Day Return — Top {top_k} (Net)")
    axes[1].set_ylabel("5-Day Return (%)")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    fig.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def _save_backtest_json(
    gross_ret: pd.Series,
    gross_cum: pd.Series,
    net_ret:   pd.Series,
    net_cum:   pd.Series,
    avg_excluded: float,
    json_path: Path,
) -> None:
    data = {
        # ── 비용 차감 후 (메인 지표) ──
        "dates":                 list(net_cum.index),
        "cumulative_return_pct": [round(v * 100, 3) for v in net_cum.values],
        "daily_return_pct":      [round(v * 100, 3) for v in net_ret.values],
        "total_return_pct":      round(float(net_cum.iloc[-1]) * 100, 2) if len(net_cum) else 0.0,
        "sharpe_ratio":          round(_sharpe(net_ret, 252.0), 3),
        "max_drawdown_pct":      round(_max_drawdown(net_cum) * 100, 2),
        "win_rate":              round(float((net_ret > 0).mean()), 3),
        # ── 비용 차감 전 참고용 ──
        "gross_total_return_pct": round(float(gross_cum.iloc[-1]) * 100, 2) if len(gross_cum) else 0.0,
        "gross_sharpe_ratio":     round(_sharpe(gross_ret, 252.0), 3),
        "gross_win_rate":         round(float((gross_ret > 0).mean()), 3),
        # ── 적용된 비용 정보 ──
        "costs_applied": {
            "commission_pct":  round(COMMISSION * 100, 4),
            "sell_tax_pct":    round(SELL_TAX * 100, 4),
            "slippage_pct":    round(SLIPPAGE * 100, 4),
            "min_volume_bn":   MIN_VOLUME_KRW / 1e9,
            "avg_excluded_per_day": round(avg_excluded, 1),
        },
    }
    json_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _plot_backtest(
    gross_ret: pd.Series,
    gross_cum: pd.Series,
    net_ret:   pd.Series,
    net_cum:   pd.Series,
    top_k: int,
    save_path: Path,
) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

    axes[0].plot(gross_cum.index, gross_cum.values * 100, linewidth=1.0,
                 color="lightblue", alpha=0.7, label="비용 전 (Gross)")
    axes[0].plot(net_cum.index,   net_cum.values   * 100, linewidth=1.5,
                 color="steelblue", label="비용 후 (Net)")
    axes[0].axhline(0, color="gray", linewidth=0.8, linestyle="--")
    axes[0].set_title(
        f"Backtest — Top {top_k} Daily Equal-Weight Portfolio\n"
        f"비용: 수수료 {COMMISSION*100:.3f}%×2 + 거래세 {SELL_TAX*100:.2f}% + 슬리피지 {SLIPPAGE*100:.1f}%×2"
    )
    axes[0].set_ylabel("Cumulative Return (%)")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)

    colors = ["#d62728" if r < 0 else "#2ca02c" for r in net_ret.values]
    axes[1].bar(net_ret.index, net_ret.values * 100, color=colors, alpha=0.75, width=0.8)
    mean_ret = net_ret.mean()
    axes[1].axhline(
        mean_ret * 100, color="navy", linewidth=1.2, linestyle="--",
        label=f"평균 {mean_ret * 100:.2f}% (비용 후)",
    )
    axes[1].set_title(f"Daily Return — Top {top_k} (Net)")
    axes[1].set_ylabel("Daily Return (%)")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    fig.savefig(save_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
