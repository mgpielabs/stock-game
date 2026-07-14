"""
[검증 전용 — 운영 모델/DB 변경 없음]
신용잔고비율(대주잔고/상장주식수) 소표본 4관문 검증 스크립트.
KIS FHPST04760000(daily_credit_balance, fid_cond_scr_div_code=20476) 수집 후 실행.

4관문:
  관문1: 변동성 피처군과 선형/비선형 중복성 (Pearson r + MI)
  관문2: 공매도 체결비율(ssts_vol_rlim)과 상관 — 높으면 이미 기각된 신호와 중복
  관문3: 상위20% vs 하위20% 5d/20d 초과수익 diff-in-means + Mann-Whitney p
  관문4: 시총 3구간(대/중/소) 분리 반복 — 구간별 IS/OOS 방향 일치 여부

선행 관문(직교성 이론 논증):
  신용잔고는 flow(체결비율)가 아닌 stock(포지션 잔량) 데이터.
  대차 시스템이 기관 대차와 독립된 소액 브로커 경로(대주)라는 것이 직교 근거.
  그러나 ssts_vol_rlim(체결비율)도 동일한 "공매도 압력" 가설이었고 SHAP 0%로 기각됐으므로
  관문2(체결비율과 상관)를 통과해야 "다른 신호"임이 확인됨.

실행: uv run --project backend python backend/analysis/credit_balance_validation.py

⚠️ 실행 전 확인사항:
  1. KIS FHPST04760000 수집 완료 (5종목 테스트 → 전종목 백필)
  2. 아래 TODO 항목 실제 테이블명/컬럼명으로 교체
  3. features 테이블에 ssts_vol_rlim 컬럼 존재 여부 확인
     (존재 안 하면 관문2 건너뜀)
"""

import sys
import sqlite3
import pickle
import warnings
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from scipy import stats
from catboost import CatBoostClassifier, Pool

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT      = Path(__file__).parent.parent
DB_PATH   = ROOT / "data" / "stocks.db"
ML_DIR    = ROOT / "ml"
MODELS_DIR = ROOT / "models"
sys.path.insert(0, str(ML_DIR))

from dataset import build_dataset, FEATURE_COLS_REDUCED

# KIS FHPST04760000 실제 테이블/컬럼명 (2026-07-06 확정)
# whol_stln_rmnd_rate = 대주잔고율(%) — API 직접 제공(상장주식수 대비 %)
CREDIT_TABLE  = "credit_balance_kis"
CREDIT_COL    = "stln_rmnd_rate"
# 참고 필드: stln_rmnd_stcn(대주잔고 주수), stln_new_stcn(신규), stln_rdmp_stcn(상환)
#            loan_rmnd_rate(신용융자 잔고율 — 별개)

# 변동성 중복성 관문에서 체크할 기존 피처
VOL_FEATURES = ["atr_pct", "volatility_5", "volatility_20", "vol_ratio_20d", "ma120_dev"]
# 공매도 체결비율 (이미 SHAP 0%로 기각된 신호)
SSTS_COL     = "ssts_vol_rlim"

IS_END  = "20231231"   # IS: 2022~2023
OOS_START = "20240101" # OOS: 2024~2026

# prices 기반 거래대금 proxy: 소형 / 중형 / 대형 (단위: 원)
# 거래대금 10억 미만 / 10억~100억 / 100억 이상 (최근 30일 평균)
MKTCAP_BINS = [0, 1e9, 10e9, float("inf")]
MKTCAP_LABELS = ["소형(<10억)", "중형(10억~100억)", "대형(>100억)"]


def load_active_model():
    pointer = MODELS_DIR / "ACTIVE_target_5d.txt"
    model_dir = MODELS_DIR / pointer.read_text(encoding="utf-8").strip()
    with open(model_dir / "model_cat.pkl", "rb") as f:
        model = pickle.load(f)
    print(f"운영 모델: {model_dir.name}")
    return model, model_dir


def load_credit_data() -> pd.DataFrame:
    """신용잔고 데이터 로드.
    반환: symbol(str,zfill6) / date(int YYYYMMDD) / CREDIT_COL(float) 컬럼.
    TODO: 실제 스키마에 맞게 수정."""
    with sqlite3.connect(DB_PATH) as conn:
        # 테이블 존재 여부 확인
        tables = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table'", conn)["name"].tolist()
        if CREDIT_TABLE not in tables:
            raise RuntimeError(
                f"'{CREDIT_TABLE}' 테이블이 DB에 없음. KIS 수집 완료 후 실행하세요.")
        df = pd.read_sql_query(
            f"SELECT symbol, date, {CREDIT_COL} FROM {CREDIT_TABLE} "
            f"WHERE {CREDIT_COL} IS NOT NULL ORDER BY symbol, date",
            conn)
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["date"]   = pd.to_numeric(df["date"], errors="coerce").astype(int)
    print(f"신용잔고 데이터: {len(df):,}행, {df['symbol'].nunique():,}종목, "
          f"{df['date'].min()}~{df['date'].max()}")
    return df


def load_market_caps() -> pd.DataFrame:
    """시총 추정 로드. stocks에 mktcap 없으므로 prices 최근 30일 평균 거래대금으로 대리."""
    with sqlite3.connect(DB_PATH) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(stocks)")}
        if "mktcap" in cols:
            mc = pd.read_sql_query("SELECT symbol, mktcap FROM stocks", conn)
        else:
            # prices 기반: 최근 30일 평균(volume × close)을 시총 proxy로 사용
            mc = pd.read_sql_query("""
                SELECT symbol, AVG(CAST(volume AS REAL) * close) AS mktcap
                FROM prices
                WHERE date >= (SELECT DATE(MAX(date), '-30 days') FROM prices)
                GROUP BY symbol
                HAVING COUNT(*) >= 5
            """, conn)
    mc["symbol"] = mc["symbol"].astype(str).str.zfill(6)
    return mc


def mi_score(x: np.ndarray, y: np.ndarray) -> float:
    """상호정보량 (이산화, sklearn 없이 직접)."""
    try:
        from sklearn.feature_selection import mutual_info_regression
        mask = ~(np.isnan(x) | np.isnan(y))
        if mask.sum() < 100:
            return float("nan")
        return float(mutual_info_regression(
            x[mask].reshape(-1, 1), y[mask], random_state=42)[0])
    except ImportError:
        return float("nan")


def diff_in_means(high: np.ndarray, low: np.ndarray):
    """Mann-Whitney U + 평균 차이."""
    mask_h = ~np.isnan(high)
    mask_l = ~np.isnan(low)
    if mask_h.sum() < 10 or mask_l.sum() < 10:
        return float("nan"), float("nan"), float("nan"), float("nan")
    h, l = high[mask_h], low[mask_l]
    diff = h.mean() - l.mean()
    _, p = stats.mannwhitneyu(h, l, alternative="two-sided")
    return h.mean(), l.mean(), diff, p


def check_ssts_col(conn: sqlite3.Connection) -> bool:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(features)")}
    return SSTS_COL in cols


# ── 메인 ─────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("신용잔고비율 소표본 4관문 검증 (100종목 직접 쿼리)")
    print("=" * 65)

    credit_df = load_credit_data()
    credit_syms = tuple(credit_df["symbol"].unique())
    sym_sql = "','".join(credit_syms)

    # ── 관문1+2용: vol 피처 + ssts (100종목만) ───────────────────
    print("\n피처 로드 중 (100종목, 필요 컬럼만)...")
    with sqlite3.connect(DB_PATH) as conn:
        has_ssts = check_ssts_col(conn)
        extra = ([SSTS_COL] if has_ssts else [])
        load_cols = VOL_FEATURES + extra
        feat_small = pd.read_sql_query(
            f"SELECT symbol, date, {','.join(load_cols)} FROM features "
            f"WHERE symbol IN ('{sym_sql}')",
            conn)
    feat_small["symbol"] = feat_small["symbol"].astype(str).str.zfill(6)
    feat_small["date"]   = pd.to_numeric(feat_small["date"], errors="coerce").astype(int)

    merged = feat_small.merge(
        credit_df[["symbol", "date", CREDIT_COL]],
        on=["symbol", "date"], how="inner")
    print(f"  피처×신용 join: {len(merged):,}행, {merged['symbol'].nunique()}종목")

    # ── 초과수익 계산 (prices는 전종목 — 횡단면 중앙값 필요) ───
    print("  가격 로드 중 (전종목 close)...")
    with sqlite3.connect(DB_PATH) as conn:
        prices_all = pd.read_sql_query(
            "SELECT symbol, date, close FROM prices", conn)
    prices_all["symbol"] = prices_all["symbol"].astype(str).str.zfill(6)
    prices_all["date"]   = pd.to_numeric(prices_all["date"], errors="coerce").astype(int)
    prices_all.sort_values(["symbol", "date"], inplace=True)

    for rc, shift in [("ret_5d", -5), ("ret_20d", -20)]:
        prices_all[rc] = prices_all.groupby("symbol")["close"].transform(
            lambda x, s=shift: x.shift(s) / x - 1)
        med = prices_all.groupby("date")[rc].median().rename(f"med_{rc}")
        prices_all = prices_all.join(med, on="date")
        prices_all[f"exc_{rc}"] = prices_all[rc] - prices_all[f"med_{rc}"]

    # 100종목 행만 추출해서 join
    prices_credit = prices_all[prices_all["symbol"].isin(set(credit_syms))][
        ["symbol", "date", "exc_ret_5d", "exc_ret_20d"]]
    del prices_all  # 메모리 해제

    merged = merged.merge(prices_credit, on=["symbol", "date"], how="left")
    merged_is  = merged[merged["date"] <= int(IS_END)]
    merged_oos = merged[merged["date"] >= int(OOS_START)]
    print(f"  통합 데이터: {len(merged):,}행 (IS {len(merged_is):,} / OOS {len(merged_oos):,})")

    # ── 관문1: 변동성 중복성 ──────────────────────────────────────
    print("\n[관문1] 변동성 피처와 Pearson r + MI")
    cred = merged[CREDIT_COL].values
    passed_gate1 = True
    for vf in VOL_FEATURES:
        if vf not in merged.columns:
            print(f"  {vf}: 컬럼 없음 건너뜀")
            continue
        vv = merged[vf].values
        mask = ~(np.isnan(cred) | np.isnan(vv))
        r, _ = stats.pearsonr(cred[mask], vv[mask]) if mask.sum() > 30 else (float("nan"), None)
        mi = mi_score(cred, vv)
        tag = " ⚠ 높음" if abs(r) > 0.3 else ""
        print(f"  vs {vf:<20}: r={r:+.3f}  MI={mi:.4f}{tag}")
        if abs(r) > 0.5:
            passed_gate1 = False
    print(f"  관문1 판정: {'통과' if passed_gate1 else '탈락 — 변동성 피처와 중복'}")

    # ── 관문2: 체결비율 상관 ──────────────────────────────────────
    print("\n[관문2] 공매도 체결비율(ssts_vol_rlim)과 상관")
    if SSTS_COL in merged.columns:
        sv = merged[SSTS_COL].values
        mask = ~(np.isnan(cred) | np.isnan(sv))
        r_ssts, _ = stats.pearsonr(cred[mask], sv[mask]) if mask.sum() > 30 else (float("nan"), None)
        mi_ssts = mi_score(cred, sv)
        print(f"  r={r_ssts:+.3f}, MI={mi_ssts:.4f}")
        if abs(r_ssts) > 0.5:
            print("  관문2 판정: 탈락 — 이미 기각된 체결비율과 높은 상관 → 중복 신호")
        else:
            print("  관문2 판정: 통과")
    else:
        print(f"  {SSTS_COL} 컬럼 없음 — 건너뜀")

    # ── 관문3: diff-in-means ──────────────────────────────────────
    print("\n[관문3] 상위20% vs 하위20% 수익률 diff-in-means")
    for period_label, df_sub, ret_col in [
        ("IS  5d",  merged_is,  "exc_ret_5d"),
        ("IS  20d", merged_is,  "exc_ret_20d"),
        ("OOS 5d",  merged_oos, "exc_ret_5d"),
        ("OOS 20d", merged_oos, "exc_ret_20d"),
    ]:
        cred_sub = df_sub[CREDIT_COL].values
        ret_sub  = df_sub[ret_col].values
        mask = ~(np.isnan(cred_sub) | np.isnan(ret_sub))
        if mask.sum() < 50:
            print(f"  {period_label}: 표본 부족 ({mask.sum()}행)")
            continue
        q20 = np.nanpercentile(cred_sub, 20)
        q80 = np.nanpercentile(cred_sub, 80)
        high_idx = cred_sub >= q80
        low_idx  = cred_sub <= q20
        h_mean, l_mean, diff, p = diff_in_means(
            ret_sub[high_idx & ~np.isnan(ret_sub)],
            ret_sub[low_idx  & ~np.isnan(ret_sub)])
        sig = " **" if p < 0.05 else (" *" if p < 0.1 else "")
        print(f"  {period_label}: high={h_mean:+.4f} low={l_mean:+.4f} "
              f"diff={diff:+.4f} p={p:.4f}{sig} (n_h={high_idx.sum()}, n_l={low_idx.sum()})")

    # ── 관문4: SHAP (조건부, vol 피처 제거 후, 100종목만) ────────
    print("\n[관문4] 조건부 SHAP (vol 제거 후 미니 CatBoost, 100종목 직접 로드)")
    from dataset import FEATURE_COLS_REDUCED

    with sqlite3.connect(DB_PATH) as conn:
        feat_cols_in_db = {r[1] for r in conn.execute("PRAGMA table_info(features)")}

    # 실제 DB에 있는 피처만 (target_5d는 features 테이블에 없음 — prices에서 계산)
    use_cols = [c for c in FEATURE_COLS_REDUCED if c in feat_cols_in_db]
    no_vol_cols = [c for c in use_cols
                   if not any(v in c for v in ["atr", "volatility", "vol_ratio", "vol_krw"])]

    with sqlite3.connect(DB_PATH) as conn:
        feat_shap = pd.read_sql_query(
            f"SELECT {','.join(no_vol_cols + ['symbol', 'date'])} FROM features "
            f"WHERE symbol IN ('{sym_sql}')",
            conn)
    feat_shap["symbol"] = feat_shap["symbol"].astype(str).str.zfill(6)
    feat_shap["date"]   = pd.to_numeric(feat_shap["date"], errors="coerce").astype(int)

    # credit join
    feat_shap = feat_shap.merge(
        credit_df[["symbol", "date", CREDIT_COL]],
        on=["symbol", "date"], how="left")

    # 라벨: exc_ret_5d > 0 (prices_credit에서 이미 계산됨)
    feat_shap = feat_shap.merge(
        prices_credit[["symbol", "date", "exc_ret_5d"]],
        on=["symbol", "date"], how="left")
    feat_shap["label"] = (feat_shap["exc_ret_5d"] > 0).astype(int)
    feat_shap = feat_shap[feat_shap["exc_ret_5d"].notna()]

    credit_avail = feat_shap[CREDIT_COL].notna().mean() * 100
    print(f"  커버리지: {credit_avail:.1f}%  라벨: exc_ret_5d>0 (binary proxy)")

    # IS/OOS 분리
    tr_mask = feat_shap["date"] <= int(IS_END)
    va_mask = feat_shap["date"] >= int(OOS_START)
    feat_tr = feat_shap[tr_mask].copy()
    feat_va = feat_shap[va_mask].copy()

    feat_cols_shap = no_vol_cols + [CREDIT_COL]
    X_tr_s = feat_tr[feat_cols_shap]
    y_tr_s = feat_tr["label"]
    X_va_s = feat_va[feat_cols_shap]
    y_va_s = feat_va["label"]

    print(f"  train: {len(X_tr_s):,}행, val: {len(X_va_s):,}행, 피처: {len(feat_cols_shap)}개")

    mini = CatBoostClassifier(
        iterations=300, depth=4, learning_rate=0.05,
        loss_function="Logloss", random_seed=42,
        verbose=0, allow_writing_files=False, thread_count=-1)
    mini.fit(X_tr_s, y_tr_s, eval_set=(X_va_s, y_va_s),
             early_stopping_rounds=30, verbose=0)
    imp = dict(zip(mini.feature_names_, mini.get_feature_importance()))
    cred_shap = imp.get(CREDIT_COL, 0.0)
    print(f"  {CREDIT_COL} SHAP(vol 제거 후): {cred_shap:.3f}%")
    # 상위 10 피처 출력
    top10 = sorted(imp.items(), key=lambda x: -x[1])[:10]
    for rank, (f, v) in enumerate(top10, 1):
        mark = " ★" if f == CREDIT_COL else ""
        print(f"    {rank:2d}. {f:<30}: {v:.3f}%{mark}")
    if cred_shap < 0.5:
        print("  관문4 판정: 탈락 — vol 제거 후에도 SHAP < 0.5%")
    else:
        print("  관문4 판정: 통과 (독립 예측력 있음)")

    # ── 관문4b: 시총(거래대금) 3구간 분리 ────────────────────────
    print("\n[관문4b] 거래대금 3구간별 관문3 반복 (OOS 5d)")
    mc_df = load_market_caps()
    if not mc_df.empty:
        merged_mc = merged.merge(mc_df, on="symbol", how="left")
        merged_mc["mktcap_bin"] = pd.cut(
            merged_mc["mktcap"], bins=MKTCAP_BINS, labels=MKTCAP_LABELS, right=False)
        for cap_label in MKTCAP_LABELS:
            oos_sub = merged_mc[
                (merged_mc["mktcap_bin"] == cap_label) &
                (merged_mc["date"] >= int(OOS_START))]
            cred_s = oos_sub[CREDIT_COL].values
            ret_s  = oos_sub["exc_ret_5d"].values
            mask = ~(np.isnan(cred_s) | np.isnan(ret_s))
            if mask.sum() < 20:
                print(f"  {cap_label}: 표본 부족 ({mask.sum()}행)")
                continue
            q80, q20 = np.nanpercentile(cred_s, 80), np.nanpercentile(cred_s, 20)
            h_m, l_m, diff, p = diff_in_means(
                ret_s[cred_s >= q80], ret_s[cred_s <= q20])
            print(f"  {cap_label}: diff={diff:+.4f} p={p:.4f} (n={mask.sum()})")
    else:
        print("  시총 데이터 없음 — 건너뜀")

    print("\n" + "=" * 65)
    print("검증 완료. SHAP 통과 + 관문1~4 모두 통과 시에만 전종목 백필 착수.")
    print("=" * 65)


if __name__ == "__main__":
    main()
