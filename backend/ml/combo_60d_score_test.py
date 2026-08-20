"""
60d 모델 스코어를 필터로 결합한 3-필터 조합 검증.
[검증 전용 — SELECT만, 모델/DB 변경 없음]

combo_discovery_v2.py 기존 로직 수정 없이 동일 방법론 적용:
  - IS/OOS 분리, Mann-Whitney p값, Jaccard
  - HORIZON='ret_fwd_20d', PASS_WIN_DELTA=0.03, PASS_OOS_P=0.05

3-필터 조합 평가 방식:
  compare_combo_full(prices, cond_AB, cond_60d, HORIZON, years)
  여기서 cond_AB = 기존 2-필터 교집합, A∩B 열이 3-필터 성과.
"""
import json
import pickle
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 경로 설정 ────────────────────────────────────────────────────────────────
ML_DIR = Path(__file__).parent
BACKEND = ML_DIR.parent
MODELS_ROOT = BACKEND / "models"
DB_PATH = BACKEND / "data" / "stocks.db"

sys.path.insert(0, str(ML_DIR))

from combo_discovery_v2 import (
    IN_SAMPLE_YEARS,
    OOS_YEARS,
    HORIZON,
    PASS_WIN_DELTA,
    PASS_OOS_P,
    PASS_OOS_N_MIN,
    load_prices,
    add_returns_and_liquid,
    build_all_conds,
    compare_combo_full,
    biz_year_pit_vec,
)
from factors_60d import (
    load_factor_data,
    compute_eps_signals,
    compute_roe_signals,
    compute_dps_signals,
    compute_bps_signals,
    join_pit_factors,
)
from ensemble_config import CAT_BLEND_WEIGHT, XGB_BLEND_WEIGHT


# ── 60d 모델 스코어 조건 생성 ────────────────────────────────────────────────

def build_60d_score_conds(prices: pd.DataFrame, conn: sqlite3.Connection) -> dict:
    """
    60d 모델로 전 기간 모든 (symbol, date)를 추론해
    날짜별 top20%/top10% boolean Series 반환.
    """
    t0 = time.time()

    # 1) ACTIVE 60d 모델 로드
    active_dir = (MODELS_ROOT / "ACTIVE_target_60d.txt").read_text(encoding="utf-8").strip()
    model_dir = MODELS_ROOT / active_dir
    meta = json.loads((model_dir / "model_meta.json").read_text(encoding="utf-8"))
    feature_cols = meta.get("features", meta.get("feature_cols", []))
    if not feature_cols:
        raise RuntimeError(f"60d meta에 feature_cols 없음: {model_dir}")

    cat_60d = pickle.load(open(model_dir / "model_cat.pkl", "rb"))
    # 60d 모델은 CatBoost 단독 (XGBoost 없음)
    xgb_path = model_dir / "model_xgb.pkl"
    xgb_60d = pickle.load(open(xgb_path, "rb")) if xgb_path.exists() else None
    print(f"  60d 모델 로드: {active_dir} ({len(feature_cols)}피처, "
          f"blend={'CAT+XGB' if xgb_60d else 'CAT only'})")

    # 2) 기술 피처 로드 (features 테이블, PIT 팩터 제외)
    PIT_FACTOR_COLS = {"eps_growth_yoy", "eps_growth_accel", "roe_level",
                       "dps_growth_yoy", "neg_pbr", "bps_growth_yoy"}
    base_tech_cols = [c for c in feature_cols if c not in PIT_FACTOR_COLS]
    # features 테이블에 pbr이 있어야 neg_pbr 계산 가능
    need_cols = list(dict.fromkeys(base_tech_cols + ["pbr"]))
    # features 테이블에 없는 컬럼 제거
    feat_table_cols_q = conn.execute("PRAGMA table_info(features)").fetchall()
    feat_table_cols = {row[1] for row in feat_table_cols_q}
    load_cols = [c for c in need_cols if c in feat_table_cols]

    sql = (
        f"SELECT symbol, date, {', '.join(load_cols)} FROM features "
        f"WHERE date BETWEEN '20220101' AND '20261231'"
    )
    feat_df = pd.read_sql_query(sql, conn)
    feat_df["symbol"] = feat_df["symbol"].astype(str).str.zfill(6)
    print(f"  features 로드: {len(feat_df):,}행 × {len(feat_df.columns)}컬럼 "
          f"({time.time()-t0:.1f}s)")

    # 3) neg_pbr 계산 (features 테이블의 pbr은 이미 PIT 보정됨)
    if "pbr" in feat_df.columns:
        feat_df["neg_pbr"] = -feat_df["pbr"]
    else:
        feat_df["neg_pbr"] = np.nan

    # 4) biz_year_pit 컬럼 추가 후 PIT 팩터 join
    feat_df["biz_year_pit"] = biz_year_pit_vec(feat_df["date"].astype(int).values)

    eps_df, bps_df, dps_df = load_factor_data(conn)
    eps_sig = compute_eps_signals(eps_df)
    roe_sig = compute_roe_signals(eps_df, bps_df)
    dps_sig = compute_dps_signals(dps_df)
    bps_sig = compute_bps_signals(bps_df)
    feat_df = join_pit_factors(feat_df, eps_sig, roe_sig, dps_sig, bps_sig)
    print(f"  PIT 팩터 join 완료 ({time.time()-t0:.1f}s)")

    # 5) 모델 추론
    avail_cols = [c for c in feature_cols if c in feat_df.columns]
    missing = set(feature_cols) - set(avail_cols)
    if missing:
        print(f"  ⚠ 누락 피처 {len(missing)}개: {sorted(missing)}")
    X = feat_df[avail_cols].values.astype(float)
    cat_prob = cat_60d.predict_proba(X)[:, 1]
    if xgb_60d is not None:
        xgb_prob = xgb_60d.predict_proba(X)[:, 1]
        feat_df["score_60d"] = CAT_BLEND_WEIGHT * cat_prob + XGB_BLEND_WEIGHT * xgb_prob
    else:
        feat_df["score_60d"] = cat_prob
    print(f"  추론 완료: score 범위 [{feat_df['score_60d'].min():.3f}, {feat_df['score_60d'].max():.3f}] "
          f"({time.time()-t0:.1f}s)")

    # 6) prices 인덱스에 맞게 정렬 후 날짜별 percentile rank
    prices_slim = prices[["symbol", "date"]].copy()
    prices_slim["symbol"] = prices_slim["symbol"].astype(str).str.zfill(6)
    merged = prices_slim.merge(
        feat_df[["symbol", "date", "score_60d"]],
        on=["symbol", "date"],
        how="left",
    )
    merged.index = prices.index

    score_s = merged["score_60d"]
    # 날짜별 퍼센타일 rank
    tmp = prices[["date"]].copy()
    tmp["_s"] = score_s.values
    rank_pct = tmp.groupby("date")["_s"].rank(pct=True, na_option="keep")

    cond_top20 = (rank_pct >= 0.80) & score_s.notna()
    cond_top10 = (rank_pct >= 0.90) & score_s.notna()

    n20 = cond_top20.sum()
    n10 = cond_top10.sum()
    print(f"  60d 상위 20%: {n20:,}행, 상위 10%: {n10:,}행 (전체: {len(prices):,})")

    return {"60d상위20%": cond_top20, "60d상위10%": cond_top10}


# ── 단독 필터 성과 (간단 통계) ─────────────────────────────────────────────

def eval_single_filter(prices: pd.DataFrame, cond: pd.Series, horizon: str, years: list) -> dict:
    """단일 조건의 IS/OOS 성과 (기준 필터 없이)."""
    mask_year = prices["date"].astype(str).str[:4].astype(int).isin(years)
    df = prices.loc[mask_year].copy()
    c = cond.loc[mask_year]
    ret = df[horizon]
    n_in = c.sum()
    n_out = (~c).sum()
    if n_in < PASS_OOS_N_MIN:
        return {"n_in": int(n_in), "n_out": int(n_out), "mean_in": None, "win_in": None,
                "mean_out": None, "win_out": None, "p": None}
    r_in  = ret[c]
    r_out = ret[~c]
    mw = stats.mannwhitneyu(r_in, r_out, alternative="greater")
    return {
        "n_in":    int(n_in),
        "n_out":   int(n_out),
        "mean_in": float(r_in.mean() * 100),
        "win_in":  float((r_in > 0).mean() * 100),
        "mean_out": float(r_out.mean() * 100),
        "win_out": float((r_out > 0).mean() * 100),
        "p":       float(mw.pvalue),
    }


# ── Jaccard (기존 4개 조합과 비교) ────────────────────────────────────────

EXISTING_COMBOS = {
    "고배당+저PBR":   ("고배당", "저PBR"),
    "고배당+저PER":   ("고배당", "저PER"),
    "저PBR+배당성장": ("저PBR", "배당성장"),
    "고배당+배당성장": ("고배당", "배당성장"),
}

def jaccard(a: pd.Series, b: pd.Series) -> float:
    inter = (a & b).sum()
    union = (a | b).sum()
    return float(inter / union) if union else 0.0


# ── 결과 출력 ───────────────────────────────────────────────────────────────

def print_single(label: str, is_res: dict, oos_res: dict, is_years, oos_years) -> None:
    print(f"\n{'─'*60}")
    print(f"  {label}  (단독)")
    is_y  = "/".join(map(str, is_years))
    oos_y = "/".join(map(str, oos_years))
    if is_res["mean_in"] is None:
        print("  IS: 표본 부족")
    else:
        print(f"  IS  ({is_y}):  평균={is_res['mean_in']:+.2f}%  승률={is_res['win_in']:.1f}%  "
              f"(n={is_res['n_in']})  p={is_res['p']:.4f}")
    if oos_res["mean_in"] is None:
        print("  OOS: 표본 부족")
    else:
        print(f"  OOS ({oos_y}): 평균={oos_res['mean_in']:+.2f}%  승률={oos_res['win_in']:.1f}%  "
              f"(n={oos_res['n_in']})  p={oos_res['p']:.4f}")
        if oos_res["p"] <= PASS_OOS_P and oos_res["mean_in"] > 0:
            print("  ✅ OOS 유의 (단독)")
        else:
            print("  ❌ OOS 비유의")


def print_combo(label: str, res: dict, is_years, oos_years) -> None:
    """compare_combo_full 결과 출력 (A∩B = 3-필터 성과)."""
    print(f"\n{'─'*60}")
    print(f"  {label}")
    is_y  = "/".join(map(str, is_years))
    oos_y = "/".join(map(str, oos_years))

    def _fmt(r):
        n = r["C_n"]
        if n < PASS_OOS_N_MIN or r["C_win"] is None:
            return f"  표본 부족 (n={n})"
        best_win = r["best_win"] or 0.0
        delta = r["C_win"] - best_win
        p = r["C_vs_best_p"]
        p_str = f"{p:.4f}" if p is not None else "—"
        flag = "✅" if (delta >= PASS_WIN_DELTA and p is not None and p <= PASS_OOS_P) else "❌"
        return (f"  A∩B: 평균={r['C_mean%']:+.2f}%  승률={r['C_win']*100:.1f}%  "
                f"Δ={delta*100:+.1f}%p  p={p_str}  n={n}  {flag}")

    print(f"  IS  ({is_y}): " + _fmt(res["is"]))
    print(f"  OOS ({oos_y}): " + _fmt(res["oos"]))
    # 참고: 단독 2-필터 조합 성과
    r_oos = res["oos"]
    a_w = f"{r_oos['A_win']*100:.1f}%" if r_oos['A_win'] is not None else "—"
    b_w = f"{r_oos['B_win']*100:.1f}%" if r_oos['B_win'] is not None else "—"
    bw  = f"{r_oos['best_win']*100:.1f}%" if r_oos['best_win'] is not None else "—"
    print(f"    (A-only 승률={a_w}  B-only 승률={b_w}  "
          f"best={bw}  n_A={r_oos['A_n']}  n_B={r_oos['B_n']})")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    t_start = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 60)
    print("  60d 모델 스코어 3-필터 조합 검증")
    print(f"  IS={IN_SAMPLE_YEARS}  OOS={OOS_YEARS}  HORIZON={HORIZON}")
    print("=" * 60)

    # ── 가격/수익률 로드 (combo_discovery_v2와 동일)
    print("\n[1] prices 로드...")
    prices = load_prices(conn)
    prices = add_returns_and_liquid(prices)
    print(f"    {len(prices):,}행 로드 완료")

    # ── 기존 조건 빌드
    print("\n[2] 기존 스크리너 조건 빌드...")
    all_conds = build_all_conds(prices, conn)
    print(f"    조건: {list(all_conds.keys())}")

    # ── 60d 스코어 조건 빌드
    print("\n[3] 60d 모델 추론...")
    conds_60d = build_60d_score_conds(prices, conn)

    # ── 기존 4개 조합의 OOS 조건 (Jaccard 비교용)
    existing_cond_sets = {}
    for name, (k_a, k_b) in EXISTING_COMBOS.items():
        cond_ab = all_conds[k_a] & all_conds[k_b]
        oos_mask = prices["date"].astype(str).str[:4].astype(int).isin(OOS_YEARS)
        existing_cond_sets[name] = cond_ab & oos_mask

    # ─────────────────────────────────────────────────────────────────────────
    # 3-필터 조합 테스트
    # 방식: cond_A = 기존 2-필터 교집합, cond_B = 60d 조건
    #        compare_combo_full → A∩B 열이 3-필터 성과
    # ─────────────────────────────────────────────────────────────────────────
    combos_3filter = [
        ("고배당+저PER+60d상위20%",  "고배당",  "저PER",  "60d상위20%"),
        ("고배당+저PER+60d상위10%",  "고배당",  "저PER",  "60d상위10%"),
        ("고배당+저PBR+60d상위20%",  "고배당",  "저PBR",  "60d상위20%"),
        ("고배당+배당성장+60d상위20%", "고배당", "배당성장", "60d상위20%"),
        ("저PBR+배당성장+60d상위20%", "저PBR",  "배당성장", "60d상위20%"),
    ]

    print("\n\n" + "=" * 60)
    print("  [A] 3-필터 조합 결과")
    print("=" * 60)

    combo_results = {}
    for label, k_a, k_b, k_60d in combos_3filter:
        cond_ab = all_conds[k_a] & all_conds[k_b]
        cond_60 = conds_60d[k_60d]
        is_r  = compare_combo_full(prices, cond_ab, cond_60, HORIZON, IN_SAMPLE_YEARS)
        oos_r = compare_combo_full(prices, cond_ab, cond_60, HORIZON, OOS_YEARS)
        combo_results[label] = {"is": is_r, "oos": oos_r}
        print_combo(label, {"is": is_r, "oos": oos_r}, IN_SAMPLE_YEARS, OOS_YEARS)

        # Jaccard vs 기존 4개 조합
        oos_mask = prices["date"].astype(str).str[:4].astype(int).isin(OOS_YEARS)
        cond_abc_oos = cond_ab & cond_60 & oos_mask
        j_scores = {}
        for ex_name, ex_cond in existing_cond_sets.items():
            j_scores[ex_name] = jaccard(cond_abc_oos, ex_cond)
        top_j = max(j_scores.values())
        print(f"    Jaccard vs 기존 4개: {', '.join(f'{k}={v:.2f}' for k,v in j_scores.items())}  "
              f"(max={top_j:.2f})")

    # ─────────────────────────────────────────────────────────────────────────
    # 60d 단독 평가
    # ─────────────────────────────────────────────────────────────────────────
    print("\n\n" + "=" * 60)
    print("  [B] 60d 모델 단독 평가")
    print("=" * 60)

    for k_60d in ("60d상위20%", "60d상위10%"):
        cond = conds_60d[k_60d]
        is_r  = eval_single_filter(prices, cond, HORIZON, IN_SAMPLE_YEARS)
        oos_r = eval_single_filter(prices, cond, HORIZON, OOS_YEARS)
        print_single(k_60d, is_r, oos_r, IN_SAMPLE_YEARS, OOS_YEARS)

        # Jaccard vs 기존 4개 조합
        oos_mask = prices["date"].astype(str).str[:4].astype(int).isin(OOS_YEARS)
        cond_oos = cond & oos_mask
        j_scores = {}
        for ex_name, ex_cond in existing_cond_sets.items():
            j_scores[ex_name] = jaccard(cond_oos, ex_cond)
        top_j = max(j_scores.values())
        print(f"    Jaccard vs 기존 4개: {', '.join(f'{k}={v:.2f}' for k,v in j_scores.items())}  "
              f"(max={top_j:.2f})")

    # ─────────────────────────────────────────────────────────────────────────
    # 요약표
    # ─────────────────────────────────────────────────────────────────────────
    print("\n\n" + "=" * 60)
    print("  [C] 요약 — OOS 성과")
    print("=" * 60)
    print(f"  {'조합':<32}  {'IS승률':>7}  {'OOS승률':>8}  {'OOSΔvs단독':>10}  {'OOS_p':>8}")
    print(f"  {'─'*32}  {'─'*7}  {'─'*8}  {'─'*10}  {'─'*8}")

    for label, k_a, k_b, k_60d in combos_3filter:
        r = combo_results[label]
        is_c  = r["is"]
        oos_c = r["oos"]
        is_w = (f"{is_c['C_win']*100:.1f}%"
                if is_c.get("C_n", 0) >= PASS_OOS_N_MIN and is_c["C_win"] is not None
                else "—")
        if oos_c.get("C_n", 0) >= PASS_OOS_N_MIN and oos_c["C_win"] is not None:
            oos_w   = f"{oos_c['C_win']*100:.1f}%"
            bw = oos_c['best_win'] or 0.0
            oos_dlt = f"{(oos_c['C_win']-bw)*100:+.1f}%p"
            p = oos_c['C_vs_best_p']
            oos_p   = f"{p:.4f}" if p is not None else "—"
        else:
            oos_w = oos_dlt = oos_p = "—"
        print(f"  {label:<32}  {is_w:>7}  {oos_w:>8}  {oos_dlt:>10}  {oos_p:>8}")

    print()
    for k_60d in ("60d상위20%", "60d상위10%"):
        cond = conds_60d[k_60d]
        is_r  = eval_single_filter(prices, cond, HORIZON, IN_SAMPLE_YEARS)
        oos_r = eval_single_filter(prices, cond, HORIZON, OOS_YEARS)
        is_w  = f"{is_r['win_in']:.1f}%" if is_r["win_in"] is not None else "—"
        if oos_r["win_in"] is not None:
            oos_w  = f"{oos_r['win_in']:.1f}%"
            oos_p  = f"{oos_r['p']:.4f}"
        else:
            oos_w = oos_p = "—"
        print(f"  {k_60d+' (단독)':<32}  {is_w:>7}  {oos_w:>8}  {'N/A':>10}  {oos_p:>8}")

    print(f"\n  총 소요: {time.time()-t_start:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
