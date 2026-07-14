"""revenue_growth_yoy 중복성 + 단변량 60d 검증.
관문1: vs 기존 5팩터 |r| + MI  (사전 고정 판정: |r|>0.6 기각 검토)
관문1b: 발산 케이스 비중 (매출↑&EPS↓, 매출↓&EPS↑)
관문2: 상위/하위 quintile 60d 초과수익 diff-in-means IS/OOS (purge gap 60일)
관문3: 조건부 SHAP (기존 60d 피처셋 + revenue_growth 소표본 재학습)
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
import sqlite3
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats
from sklearn.metrics import mutual_info_score
from sklearn.preprocessing import KBinsDiscretizer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB = Path("backend/data/stocks.db")
IS_END   = "2023-12-31"   # IS: 2021~2023
OOS_START = "2024-01-01"  # OOS: 2024~2026
PURGE_GAP = 60            # 거래일
MIN_P     = 0.05          # Bonferroni 보정 후 유의 기준
CORR_REJECT = 0.60        # 사전 고정: |r| > 이 값이면 기각 검토
CORR_PASS   = 0.40        # |r| < 이 값이면 진행


def load_pit_factors(conn) -> pd.DataFrame:
    """financials/dividends에서 PIT 팩터 계산 (factors_60d.py와 동일 로직)."""
    # EPS growth (dividends.eps 기반)
    eps = pd.read_sql_query("""
        SELECT symbol, biz_year, eps
        FROM dividends
        WHERE eps IS NOT NULL
        ORDER BY symbol, biz_year
    """, conn)
    eps["eps_prev"] = eps.groupby("symbol")["eps"].shift(1)
    eps["eps_growth_yoy"] = (eps["eps"] - eps["eps_prev"]) / eps["eps_prev"].abs().replace(0, np.nan)
    eps["known_from"] = (eps["biz_year"] + 1).astype(str) + "-04-01"
    eps_pit = eps[["symbol", "biz_year", "known_from", "eps_growth_yoy"]].dropna()

    # Revenue growth (financials.revenue 기반 — 신규)
    rev = pd.read_sql_query("""
        SELECT symbol, biz_year, revenue
        FROM financials
        WHERE revenue IS NOT NULL
        ORDER BY symbol, biz_year
    """, conn)
    rev["rev_prev"] = rev.groupby("symbol")["revenue"].shift(1)
    rev["revenue_growth_yoy"] = (rev["revenue"] - rev["rev_prev"]) / rev["rev_prev"].abs().replace(0, np.nan)
    rev["known_from"] = (rev["biz_year"] + 1).astype(str) + "-04-01"
    rev_pit = rev[["symbol", "biz_year", "known_from", "revenue_growth_yoy"]].dropna()

    # Operating income growth
    op = pd.read_sql_query("""
        SELECT symbol, biz_year, operating_income
        FROM financials
        WHERE operating_income IS NOT NULL AND operating_income != 0
        ORDER BY symbol, biz_year
    """, conn)
    op["op_prev"] = op.groupby("symbol")["operating_income"].shift(1)
    op["op_growth_yoy"] = (op["operating_income"] - op["op_prev"]) / op["op_prev"].abs().replace(0, np.nan)
    op["known_from"] = (op["biz_year"] + 1).astype(str) + "-04-01"
    op_pit = op[["symbol", "biz_year", "known_from", "op_growth_yoy"]].dropna()

    # ROE (eps/bps 근사)
    fin = pd.read_sql_query("""
        SELECT d.symbol, d.biz_year, d.eps, f.bps,
               (CAST(d.biz_year AS TEXT) || '-04-01') AS known_from
        FROM dividends d JOIN financials f ON d.symbol=f.symbol AND d.biz_year=f.biz_year
        WHERE d.eps IS NOT NULL AND f.bps IS NOT NULL AND f.bps > 0
    """, conn)
    fin["roe_level"] = fin["eps"] / fin["bps"]
    roe_pit = fin[["symbol", "biz_year", "known_from", "roe_level"]].dropna()

    # DPS growth
    dps = pd.read_sql_query("""
        SELECT symbol, biz_year, dps
        FROM dividends
        WHERE dps IS NOT NULL AND dps > 0
        ORDER BY symbol, biz_year
    """, conn)
    dps["dps_prev"] = dps.groupby("symbol")["dps"].shift(1)
    dps["dps_growth_yoy"] = (dps["dps"] - dps["dps_prev"]) / dps["dps_prev"].replace(0, np.nan)
    dps["known_from"] = (dps["biz_year"] + 1).astype(str) + "-04-01"
    dps_pit = dps[["symbol", "biz_year", "known_from", "dps_growth_yoy"]].dropna()

    # BPS growth
    bps = pd.read_sql_query("""
        SELECT symbol, biz_year, bps
        FROM financials
        WHERE bps IS NOT NULL AND bps > 0
        ORDER BY symbol, biz_year
    """, conn)
    bps["bps_prev"] = bps.groupby("symbol")["bps"].shift(1)
    bps["bps_growth_yoy"] = (bps["bps"] - bps["bps_prev"]) / bps["bps_prev"].replace(0, np.nan)
    bps["known_from"] = (bps["biz_year"] + 1).astype(str) + "-04-01"
    bps_pit = bps[["symbol", "biz_year", "known_from", "bps_growth_yoy"]].dropna()

    # PBR (neg_pbr = -pbr)
    pbr_q = pd.read_sql_query("""
        SELECT symbol, biz_year, -bps AS neg_pbr,
               (CAST(biz_year AS TEXT) || '-04-01') AS known_from
        FROM financials WHERE bps IS NOT NULL AND bps > 0
    """, conn)

    merged = eps_pit.merge(rev_pit, on=["symbol","biz_year","known_from"], how="outer")
    merged = merged.merge(op_pit,  on=["symbol","biz_year","known_from"], how="outer")
    merged = merged.merge(roe_pit, on=["symbol","biz_year","known_from"], how="outer")
    merged = merged.merge(dps_pit, on=["symbol","biz_year","known_from"], how="outer")
    merged = merged.merge(bps_pit, on=["symbol","biz_year","known_from"], how="outer")
    return merged


def mi_score(x: np.ndarray, y: np.ndarray, n_bins: int = 10) -> float:
    kbd = KBinsDiscretizer(n_bins=n_bins, encode="ordinal", strategy="quantile")
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 50:
        return np.nan
    xb = kbd.fit_transform(x[mask].reshape(-1, 1)).ravel().astype(int)
    yb = kbd.fit_transform(y[mask].reshape(-1, 1)).ravel().astype(int)
    return mutual_info_score(xb, yb)


def compute_excess_return(conn, horizon: int = 60) -> pd.DataFrame:
    """날짜별 횡단면 중앙값 초과수익 계산."""
    prices = pd.read_sql_query(
        "SELECT symbol, date, close FROM prices ORDER BY symbol, date", conn
    )
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices.sort_values(["symbol", "date"])
    prices["fwd"] = prices.groupby("symbol")["close"].shift(-horizon)
    prices["ret"] = (prices["fwd"] - prices["close"]) / prices["close"]
    prices = prices.dropna(subset=["ret"])
    med = prices.groupby("date")["ret"].median().rename("market_ret")
    prices = prices.join(med, on="date")
    prices["exc_ret"] = prices["ret"] - prices["market_ret"]
    return prices[["symbol", "date", "exc_ret"]].copy()


def gate3_diff_in_means(df: pd.DataFrame, factor_col: str, outcome_col: str,
                        label: str = "") -> dict:
    """상위/하위 quintile diff-in-means."""
    df = df.dropna(subset=[factor_col, outcome_col])
    q20 = df[factor_col].quantile(0.20)
    q80 = df[factor_col].quantile(0.80)
    high = df[df[factor_col] >= q80][outcome_col]
    low  = df[df[factor_col] <= q20][outcome_col]
    stat, p = stats.mannwhitneyu(high, low, alternative="greater")
    diff = high.mean() - low.mean()
    print(f"  {label:12s}: high={high.mean():+.4f}({len(high)}) low={low.mean():+.4f}({len(low)}) "
          f"diff={diff:+.4f} p={p:.4f}")
    return {"diff": diff, "p": p, "n_h": len(high), "n_l": len(low)}


def main():
    print("=" * 65)
    print("revenue_growth_yoy 검증 (소표본)")
    print("=" * 65)

    conn = sqlite3.connect(DB)

    # 데이터 상태 확인
    r = conn.execute(
        "SELECT COUNT(*), COUNT(revenue), COUNT(operating_income), COUNT(net_income) "
        "FROM financials"
    ).fetchone()
    print(f"financials 전체: {r[0]}행, revenue={r[1]}, op_income={r[2]}, net_income={r[3]}")

    factors = load_pit_factors(conn)
    rev_ok = factors.dropna(subset=["revenue_growth_yoy"])
    print(f"revenue_growth_yoy 유효 행: {len(rev_ok)}")
    if len(rev_ok) < 30:
        print("⚠ 샘플 부족 — 수집 먼저 완료 필요")
        conn.close()
        return

    # ────────────────────────────────────────────────────────
    # [관문1] 중복성: revenue_growth vs 기존 5팩터
    # ────────────────────────────────────────────────────────
    print()
    print("[관문1] 중복성 — revenue_growth_yoy vs 기존 팩터")
    EXISTING_FACTORS = ["eps_growth_yoy", "roe_level", "dps_growth_yoy", "bps_growth_yoy"]
    all_pass = True
    for f2 in EXISTING_FACTORS:
        sub = factors.dropna(subset=["revenue_growth_yoy", f2])
        if len(sub) < 10:
            print(f"  vs {f2:25s}: 교집합 {len(sub)}건 부족 — 스킵")
            continue
        rv = sub["revenue_growth_yoy"].clip(
            sub["revenue_growth_yoy"].quantile(0.01),
            sub["revenue_growth_yoy"].quantile(0.99)
        )
        f2v = sub[f2].clip(sub[f2].quantile(0.01), sub[f2].quantile(0.99))
        r_val, _ = stats.pearsonr(rv, f2v)
        mi = mi_score(rv.values, f2v.values)
        flag = "❌ 기각 검토" if abs(r_val) > CORR_REJECT else ("✅ 통과" if abs(r_val) < CORR_PASS else "⚠ 발산")
        print(f"  vs {f2:25s}: r={r_val:+.3f}  MI={mi:.4f}  n={len(sub)}  {flag}")
        if abs(r_val) > CORR_REJECT:
            all_pass = False

    # op_growth도 확인
    sub = factors.dropna(subset=["revenue_growth_yoy", "op_growth_yoy"])
    if len(sub) >= 10:
        rv = sub["revenue_growth_yoy"].clip(sub["revenue_growth_yoy"].quantile(0.01), sub["revenue_growth_yoy"].quantile(0.99))
        opv = sub["op_growth_yoy"].clip(sub["op_growth_yoy"].quantile(0.01), sub["op_growth_yoy"].quantile(0.99))
        r_op, _ = stats.pearsonr(rv, opv)
        mi_op = mi_score(rv.values, opv.values)
        flag = "❌" if abs(r_op) > CORR_REJECT else "✅"
        print(f"  vs {'op_growth_yoy':25s}: r={r_op:+.3f}  MI={mi_op:.4f}  n={len(sub)}  {flag}")
    print(f"\n관문1 판정: {'✅ 통과' if all_pass else '❌ 기각 검토'}")

    # ────────────────────────────────────────────────────────
    # [관문1b] 발산 케이스 비중
    # ────────────────────────────────────────────────────────
    print()
    print("[관문1b] 발산 케이스 (매출↑&EPS↓ 또는 매출↓&EPS↑)")
    sub = factors.dropna(subset=["revenue_growth_yoy", "eps_growth_yoy"])
    n_total = len(sub)
    div_up_dn = ((sub["revenue_growth_yoy"] > 0.05) & (sub["eps_growth_yoy"] < -0.05)).sum()
    div_dn_up = ((sub["revenue_growth_yoy"] < -0.05) & (sub["eps_growth_yoy"] > 0.05)).sum()
    print(f"  전체: {n_total}건")
    print(f"  매출↑ & EPS↓: {div_up_dn}건 ({100*div_up_dn/n_total:.1f}%)")
    print(f"  매출↓ & EPS↑: {div_dn_up}건 ({100*div_dn_up/n_total:.1f}%)")
    print(f"  합계: {div_up_dn+div_dn_up}건 ({100*(div_up_dn+div_dn_up)/n_total:.1f}%) — 독립 정보 후보")

    if not all_pass:
        print("\n관문1 실패 — 관문2 스킵")
        conn.close()
        return

    # ────────────────────────────────────────────────────────
    # [관문2] 단변량 60d 신호 IS/OOS
    # ────────────────────────────────────────────────────────
    print()
    print("[관문2] 단변량 60d 초과수익 (purge gap 60거래일)")
    exc = compute_excess_return(conn, horizon=60)
    exc["date"] = pd.to_datetime(exc["date"])

    # 소표본 종목 필터
    sample_syms = set(factors.dropna(subset=["revenue_growth_yoy"])["symbol"].unique())
    exc = exc[exc["symbol"].isin(sample_syms)]

    # PIT join: known_from 날짜 기준으로 join (known_from 이후 첫 거래일)
    pit = factors.dropna(subset=["revenue_growth_yoy"])[
        ["symbol", "biz_year", "known_from", "revenue_growth_yoy", "op_growth_yoy"]
    ].copy()
    pit["known_from"] = pd.to_datetime(pit["known_from"])

    merged = exc.merge(pit, on="symbol", how="inner")
    merged = merged[merged["date"] >= merged["known_from"]]
    # 각 (symbol, biz_year)별 known_from 이후 첫 날짜만
    merged = merged.sort_values("date").groupby(["symbol","biz_year"]).first().reset_index()

    IS_END_DT  = pd.Timestamp(IS_END)
    OOS_ST_DT  = pd.Timestamp(OOS_START)
    is_df  = merged[merged["date"] <= IS_END_DT]
    oos_df = merged[merged["date"] >= OOS_ST_DT]
    print(f"  IS: {len(is_df)}행 / OOS: {len(oos_df)}행")

    print("\n  revenue_growth_yoy:")
    r_is  = gate3_diff_in_means(is_df,  "revenue_growth_yoy", "exc_ret", "IS")
    r_oos = gate3_diff_in_means(oos_df, "revenue_growth_yoy", "exc_ret", "OOS")

    if len(factors.dropna(subset=["op_growth_yoy"])) > 30:
        print("\n  op_growth_yoy:")
        gate3_diff_in_means(is_df,  "op_growth_yoy", "exc_ret", "IS")
        gate3_diff_in_means(oos_df, "op_growth_yoy", "exc_ret", "OOS")

    is_ok  = r_is["p"]  < MIN_P
    oos_ok = r_oos["p"] < MIN_P
    dir_ok = (r_is["diff"] > 0) == (r_oos["diff"] > 0)
    g2_pass = is_ok and oos_ok and dir_ok
    print(f"\n관문2 판정: {'✅ 통과' if g2_pass else '❌ 탈락'}"
          f" (IS p={r_is['p']:.4f}, OOS p={r_oos['p']:.4f}, 방향 {'일치' if dir_ok else '불일치'})")

    if not g2_pass:
        print("관문2 실패 — 관문3 스킵")
        conn.close()
        return

    # ────────────────────────────────────────────────────────
    # [관문3] 조건부 SHAP
    # ────────────────────────────────────────────────────────
    print()
    print("[관문3] 조건부 SHAP — 기존 60d 피처 + revenue_growth")
    try:
        import catboost as cb
        import shap
    except ImportError:
        print("  catboost/shap 없음 — 스킵")
        conn.close()
        return

    # 기존 60d 팩터 + revenue_growth 합친 소표본 피처셋
    FACTORS = ["eps_growth_yoy", "roe_level", "dps_growth_yoy", "bps_growth_yoy", "revenue_growth_yoy"]
    feat_df = factors[["symbol","biz_year","known_from"] + FACTORS].dropna()
    feat_df = feat_df.merge(exc[["symbol","date","exc_ret"]], on="symbol", how="inner")
    feat_df["known_from"] = pd.to_datetime(feat_df["known_from"])
    feat_df = feat_df[feat_df["date"] >= feat_df["known_from"]]
    feat_df = feat_df.sort_values("date").groupby(["symbol","biz_year"]).first().reset_index()
    feat_df["label"] = (feat_df["exc_ret"] > 0).astype(int)

    cutoff = pd.Timestamp("2025-01-01")
    tr = feat_df[feat_df["date"] < cutoff]
    va = feat_df[feat_df["date"] >= cutoff]
    print(f"  train: {len(tr)}행, val: {len(va)}행")

    if len(tr) < 30 or len(va) < 10:
        print("  표본 부족 — SHAP 스킵")
        conn.close()
        return

    model = cb.CatBoostClassifier(
        iterations=200, depth=4, learning_rate=0.05,
        verbose=0, random_seed=42, task_type="CPU",
        eval_metric="AUC",
    )
    model.fit(tr[FACTORS], tr["label"], eval_set=(va[FACTORS], va["label"]))
    exp = shap.TreeExplainer(model)
    sv = exp.shap_values(va[FACTORS])
    shap_imp = np.abs(sv).mean(axis=0)
    total = shap_imp.sum() or 1.0
    print("  SHAP 기여도:")
    for col, imp in sorted(zip(FACTORS, shap_imp), key=lambda x: -x[1]):
        print(f"    {col:30s}: {100*imp/total:.3f}%")

    rev_shap = shap_imp[FACTORS.index("revenue_growth_yoy")] / total * 100
    g3_pass = rev_shap >= 0.5
    print(f"\n관문3 판정: {'✅ 통과' if g3_pass else '❌ 탈락'} (revenue_growth SHAP={rev_shap:.3f}%)")

    print()
    print("=" * 65)
    print(f"최종: 관문1={'✅' if all_pass else '❌'} 관문2={'✅' if g2_pass else '❌'} 관문3={'✅' if g3_pass else '❌'}")
    print("=" * 65)

    conn.close()


if __name__ == "__main__":
    main()
