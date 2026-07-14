"""
[검증 전용 — 운영 모델/데이터 미변경] 전체 기간 워크포워드 시뮬레이션

목적: 2026-06-21에 적용한 개선들(앙상블 가중치, PER NULL 버그 수정, 약세장 가드)의
실제 효과를 과거 전체 기간 재현 시뮬레이션으로 한 번에 검증 + "백테스트 정밀도 vs
실거래 승률 31.9%" 미스터리를 rank1 종목 대량 표본으로 재확인.

설계:
  - cutoff마다 "그 시점 이전 데이터로만 학습"(라벨 해소를 위해 5거래일 퍼지 적용,
    미래 누수 차단) → cutoff 당일 스냅샷에 대해 예측 → 실제 5일 후 결과로 평가.
  - 한 cutoff당 학습은 1회만 수행(CatBoost+XGBoost+LightGBM, 고정 파라미터 — Optuna
    튜닝 없음). 매 cutoff마다 튜닝하면 수 시간이 걸리고, 이미 진단(diagnose_model.py)에서
    튜닝이 효과가 작거나 역효과였음을 확인했으므로 본 비교(스코어링/필터 로직 차이)의
    본질에는 영향 없음.
  - 앙상블가중치/PER필터/약세장가드는 전부 "추론 후처리" 로직이라 같은 cutoff의 동일한
    3개 모델 예측값에 서로 다른 조합 규칙만 적용해 재사용 — 재학습 없이 ablation 5종 도출
    (cutoff당 학습 1회로 압축, 5종 x 50 cutoff = 250회 학습을 피함).
  - 운영 모델 파일(models/), ACTIVE 포인터, paper_trades 등 전혀 안 건드림.
    결과는 backend/ml/walkforward_results.csv 로만 저장.

실행: python walkforward_simulation.py
"""

import gc
import pickle
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000040)
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import catboost as cb
import lightgbm as lgb
import numpy as np
import pandas as pd
import xgboost as xgb
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import FEATURE_COLS, build_dataset, DB_PATH
from evaluate import COMMISSION, SELL_TAX, SLIPPAGE, MIN_VOLUME_KRW, LIMIT_UP, _net_ret

pd.set_option("display.width", 160)

# ── 워크포워드 파라미터 ──────────────────────────────────────────
SIM_START      = "20240601"   # cutoff 시작 (요청: "2024 중반")
PURGE_DAYS     = 5            # target_5d 해소에 필요한 거래일 — 누수 방지 퍼지
MIN_TRAIN_DAYS = 250          # cutoff 이전 최소 학습 거래일(약 1년)
STEP_DAYS      = 10           # cutoff 간격(거래일) ≈ 2주
TOP_N          = 10

# 약세장 가드 임계값 (predictor.py get_kospi_bear_signal과 동일)
BEAR_QUARTER_THRESH = -0.05
BEAR_HALF_THRESH    = -0.03


def _fixed_models(spw: float):
    cat = cb.CatBoostClassifier(
        iterations=200, depth=6, learning_rate=0.07, scale_pos_weight=spw,
        random_seed=42, verbose=False,
    )
    xgbm = xgb.XGBClassifier(
        n_estimators=200, max_depth=6, learning_rate=0.07, subsample=0.8,
        colsample_bytree=0.8, scale_pos_weight=spw, eval_metric="logloss",
        random_state=42, verbosity=0, device="cpu",
    )
    lgbm = lgb.LGBMClassifier(
        n_estimators=200, num_leaves=63, max_depth=6, learning_rate=0.07,
        scale_pos_weight=spw, random_state=42, verbosity=-1,
    )
    return cat, xgbm, lgbm


def load_kospi_series() -> pd.Series:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, close FROM market_index WHERE code='1001' ORDER BY date"
        ).fetchall()
    s = pd.Series({r[0]: float(r[1]) for r in rows}).sort_index()
    return s


def kospi_5d_ret_asof(kospi: pd.Series, date_pos: Dict[str, int], dates: List[str], cutoff: str) -> float:
    """cutoff 당일까지의 데이터만 사용한 KOSPI 5거래일 수익률 (미래 누수 없음)."""
    idx = kospi.index
    pos = idx.searchsorted(cutoff, side="right") - 1  # cutoff 이하 최신 위치
    if pos < 5:
        return 0.0
    return float(kospi.iloc[pos] / kospi.iloc[pos - 5] - 1)


def select_topn(day_df: pd.DataFrame, score_col: str, n: int) -> pd.DataFrame:
    """상한가(매수불가) 제외 후 score_col 기준 상위 n개."""
    cand = day_df[day_df["ret_fwd_5d"] < LIMIT_UP]
    return cand.sort_values(score_col, ascending=False).head(n)


def main():
    t_start = time.time()
    print("=" * 70)
    print("[1/3] 전체 기간 데이터 로드 (target_5d, 전 구간 — val_days로 강제 전체 추출)")
    print("=" * 70)
    # val_days를 매우 크게 주면 train_df가 비고 val_df가 전체 기간이 됨 (build_dataset 내부 로직)
    _, _, X_full, y_full, meta_full, _ = build_dataset(target_col="target_5d", val_days=10_000_000)
    meta_full = meta_full.reset_index(drop=True)
    X_full = X_full.reset_index(drop=True)
    y_full = y_full.reset_index(drop=True)
    meta_full["per"] = X_full["per"].values
    meta_full["volume_krw"] = meta_full["volume_krw"].fillna(0)

    print(f"전체 행: {len(meta_full)}, 기간: {meta_full['date'].min()} ~ {meta_full['date'].max()}")

    unique_dates = sorted(meta_full["date"].unique())
    date_pos = {d: i for i, d in enumerate(unique_dates)}
    meta_full["_pos"] = meta_full["date"].map(date_pos)

    kospi = load_kospi_series()

    # ── cutoff 목록 산출 ──
    cutoffs = []
    for i, d in enumerate(unique_dates):
        if d < SIM_START:
            continue
        if i - PURGE_DAYS < MIN_TRAIN_DAYS:
            continue
        cutoffs.append((i, d))
    cutoffs = cutoffs[::STEP_DAYS]
    print(f"cutoff 개수: {len(cutoffs)} (간격 {STEP_DAYS}거래일, {SIM_START}~{unique_dates[-1]})")
    print()

    print("=" * 70)
    print(f"[2/3] 워크포워드 학습/평가 시작 (cutoff당 1회 학습, {len(cutoffs)}회)")
    print("=" * 70)

    records: List[Dict] = []
    out_csv = Path(__file__).parent / "walkforward_results.csv"
    if out_csv.exists():
        out_csv.unlink()  # 새 실행 — 누적 방지

    for n_done, (idx_c, cutoff) in enumerate(cutoffs, 1):
        train_pos_max = idx_c - PURGE_DAYS
        train_mask = meta_full["_pos"] <= train_pos_max
        test_mask = meta_full["_pos"] == idx_c

        X_train = X_full.loc[train_mask].astype(float)
        y_train = y_full.loc[train_mask].astype(int)
        meta_test = meta_full.loc[test_mask]
        X_test = X_full.loc[test_mask].astype(float)

        if len(X_train) < 50_000 or len(meta_test) < 10:
            continue

        spw = (y_train == 0).sum() / max((y_train == 1).sum(), 1)
        cat, xgbm, lgbm = _fixed_models(float(spw))

        t0 = time.time()
        cat.fit(X_train, y_train)
        xgbm.fit(X_train, y_train)
        lgbm.fit(X_train, y_train)
        elapsed = time.time() - t0

        cat_p = cat.predict_proba(X_test)[:, 1]
        xgb_p = xgbm.predict_proba(X_test)[:, 1]
        lgb_p = lgbm.predict_proba(X_test)[:, 1]

        day = meta_test.copy()
        day["y_true"] = y_full.loc[test_mask].values
        day["cat_p"], day["xgb_p"], day["lgb_p"] = cat_p, xgb_p, lgb_p

        # 유동성 필터 (run_backtest_5d와 동일 기준)
        day = day[day["volume_krw"] >= MIN_VOLUME_KRW]
        if len(day) < TOP_N:
            continue

        # ── 약세장 가드 신호 (cutoff 시점까지 데이터만 사용) ──
        kospi_5d = kospi_5d_ret_asof(kospi, date_pos, unique_dates, cutoff)
        reduce_quarter = kospi_5d < BEAR_QUARTER_THRESH
        reduce_half = (not reduce_quarter) and kospi_5d < BEAR_HALF_THRESH
        is_bear_day = bool(reduce_quarter or reduce_half)
        guard_n = TOP_N // 4 if reduce_quarter else (TOP_N // 2 if reduce_half else TOP_N)

        # ── PER 필터 (구버전 버그 vs 신버전) ──
        old_per_pass = day["per"].fillna(0) > 0          # NULL→0 취급 (버그, NULL 제외)
        new_per_pass = day["per"].isna() | (day["per"] > 0)  # NULL 통과 (수정본)

        # ── 앙상블 스코어 (구: 동일가중 3모델 / 신: cat 0.8 + xgb 0.2) ──
        day["score_old_ens"] = (day["cat_p"] + day["xgb_p"] + day["lgb_p"]) / 3.0
        day["score_new_ens"] = 0.8 * day["cat_p"] + 0.2 * day["xgb_p"]

        def _eval(score_col: str, per_mask: pd.Series, n: int, label: str) -> Dict:
            cand = day[per_mask]
            top10 = select_topn(cand, score_col, TOP_N)   # 정밀도 비교용 — 항상 10개 고정
            topn = select_topn(cand, score_col, n)         # 실제 채택 수(약세장 가드 반영)
            if len(topn) == 0:
                return None
            net_rets = topn["ret_fwd_5d"].apply(_net_ret)
            rank1 = topn.iloc[0]
            return {
                "config": label, "cutoff": cutoff, "is_bear_day": is_bear_day,
                "kospi_5d_ret": round(kospi_5d * 100, 2),
                "n_candidates": len(cand), "n_selected": len(topn),
                "precision_at_10": float(top10["y_true"].mean()) if len(top10) else None,
                "mean_net_return": float(net_rets.mean()),
                "win_rate": float((net_rets > 0).mean()),
                "rank1_symbol": rank1["symbol"], "rank1_hit": int(rank1["y_true"]),
                "rank1_net_return": float(_net_ret(rank1["ret_fwd_5d"])),
            }

        configs = [
            # (score_col, per_mask, n, label)
            ("score_old_ens", old_per_pass, TOP_N, "A_old(전체개선전)"),
            ("score_new_ens", new_per_pass, guard_n, "B_new(전체개선후)"),
            ("score_new_ens", old_per_pass, TOP_N, "ablation_weight_only"),
            ("score_old_ens", new_per_pass, TOP_N, "ablation_per_only"),
            ("score_old_ens", old_per_pass, guard_n, "ablation_bearguard_only"),
        ]
        new_rows = []
        for score_col, per_mask, n, label in configs:
            rec = _eval(score_col, per_mask, n, label)
            if rec:
                new_rows.append(rec)
                records.append(rec)

        # 매 cutoff마다 즉시 디스크에 추가 — 중간에 죽어도 그동안 결과는 보존됨
        if new_rows:
            pd.DataFrame(new_rows).to_csv(
                out_csv, mode="a", header=not out_csv.exists() or out_csv.stat().st_size == 0,
                index=False, encoding="utf-8-sig",
            )

        train_n = len(X_train)
        # 학습에 쓴 거대 객체들을 명시적으로 해제 — 장시간 루프에서 메모리 누적 방지
        del cat, xgbm, lgbm, X_train, y_train, X_test, meta_test, day
        gc.collect()

        print(f"  [{n_done}/{len(cutoffs)}] cutoff={cutoff} train={train_n}행 "
              f"학습{elapsed:.1f}s bear={is_bear_day} kospi5d={kospi_5d*100:.1f}%", flush=True)

    print()
    print(f"학습/평가 완료 — 총 {time.time()-t_start:.0f}초")
    print()

    df = pd.DataFrame(records)
    print(f"원본 결과 저장됨(누적 기록): {out_csv} ({len(df)}행)")
    print()

    print("=" * 70)
    print("[3/3] 결과 집계")
    print("=" * 70)
    _report(df)


def _paired_ttest(df: pd.DataFrame, col: str, label_a: str, label_b: str):
    a = df[df["config"] == label_a].set_index("cutoff")[col]
    b = df[df["config"] == label_b].set_index("cutoff")[col]
    common = a.index.intersection(b.index)
    if len(common) < 5:
        return None, None, len(common)
    t, p = stats.ttest_rel(a.loc[common], b.loc[common])
    return float(b.loc[common].mean() - a.loc[common].mean()), float(p), len(common)


def _report(df: pd.DataFrame):
    n_cutoffs = df["cutoff"].nunique()
    print(f"총 cutoff 수: {n_cutoffs}, 약세장 cutoff: {df[df['is_bear_day']]['cutoff'].nunique()}")
    print()

    print("--- [A] 개선 전 vs 후 + ablation 종합 (cutoff 평균) ---")
    summary = df.groupby("config").agg(
        n_cutoffs=("cutoff", "nunique"),
        precision_at_10=("precision_at_10", "mean"),
        mean_net_return_pct=("mean_net_return", lambda s: s.mean() * 100),
        win_rate=("win_rate", "mean"),
        avg_n_selected=("n_selected", "mean"),
    ).round(4)
    order = ["A_old(전체개선전)", "B_new(전체개선후)", "ablation_weight_only",
             "ablation_per_only", "ablation_bearguard_only"]
    summary = summary.reindex([o for o in order if o in summary.index])
    print(summary.to_string())
    print()

    print("--- [B] 통계적 유의성 (A_old 대비, paired t-test on per-cutoff mean_net_return) ---")
    for label in ["B_new(전체개선후)", "ablation_weight_only", "ablation_per_only", "ablation_bearguard_only"]:
        diff, p, n = _paired_ttest(df, "mean_net_return", "A_old(전체개선전)", label)
        if diff is None:
            print(f"  {label:28s} 표본 부족(n={n})")
        else:
            sig = "유의(p<0.05)" if p < 0.05 else "비유의"
            print(f"  {label:28s} A_old 대비 차이={diff*100:+.3f}%p  p={p:.4f}  n={n}  -> {sig}")
    print()

    print("--- [C] 약세장 cutoff만 — 가드 효과 (A_old vs ablation_bearguard_only) ---")
    bear_df = df[df["is_bear_day"]]
    if bear_df["cutoff"].nunique() >= 3:
        bsum = bear_df.groupby("config").agg(
            n_cutoffs=("cutoff", "nunique"),
            mean_net_return_pct=("mean_net_return", lambda s: s.mean() * 100),
            win_rate=("win_rate", "mean"),
        ).round(4)
        bsum = bsum.reindex([o for o in order if o in bsum.index])
        print(bsum.to_string())
        diff, p, n = _paired_ttest(bear_df, "mean_net_return", "A_old(전체개선전)", "ablation_bearguard_only")
        if diff is not None:
            print(f"  가드효과(약세장만) 차이={diff*100:+.3f}%p  p={p:.4f}  n={n}")
    else:
        print(f"  약세장 cutoff 표본 부족 (n={bear_df['cutoff'].nunique()}) — 결론 보류")
    print()

    print("--- [D] Rank1 종목 미스터리 재확인 (B_new 기준, 대량 표본) ---")
    for label in ["A_old(전체개선전)", "B_new(전체개선후)"]:
        sub = df[df["config"] == label]
        n = len(sub)
        hit_rate = sub["rank1_hit"].mean()
        mean_ret = sub["rank1_net_return"].mean() * 100
        win_rate = (sub["rank1_net_return"] > 0).mean()
        binom_p = stats.binomtest(int(sub["rank1_hit"].sum()), n, p=0.5, alternative="greater").pvalue if n else None
        print(f"  {label:22s} n={n:3d}  rank1 적중률={hit_rate:.1%}  평균수익={mean_ret:+.2f}%  "
              f"승률={win_rate:.1%}  vs50%이항p={binom_p:.4f}" if binom_p is not None else "표본없음")
    print()

    print("--- [E] 노이즈 판정 가이드 ---")
    print("  paired t-test p<0.05 이면서 효과 방향이 일관되면 '효과 있음', 그 외는 '노이즈 가능성' 으로 해석.")


if __name__ == "__main__":
    main()
