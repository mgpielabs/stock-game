"""
target_60d 전체 학습 + (a) vs (b') 비교
  라벨: 60일 초과수익(횡단면 중앙값 대비) >= +7%
  (a) 현 운영 기준 피처셋: 45개 기본 + 기존 5개 PIT 팩터 = 50개
  (b') 50개 + bps_growth_yoy = 51개

주지표: top-K 포트폴리오 평균 초과수익률(연속량)
현 기준선 (current running model): K=10: +0.2578 / K=20: +0.2165

promote 조건(사전 고정):
  [S] bps_growth_yoy SHAP > 0.5%
  [K] K=10·K=20 중 최소 하나 (b') > (a), 나머지도 (a)-0.005 이상
  (S AND K 동시 충족)
충족 시 현 ACTIVE → archived_, 새 모델 promote + ACTIVE_target_60d.txt 갱신.
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import argparse, json, pickle, sqlite3, time, logging
import numpy as np


class _NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)
import pandas as pd
from pathlib import Path
from datetime import datetime
from catboost import CatBoostClassifier, Pool
from sklearn.metrics import roc_auc_score

# ── 경로 설정 ──────────────────────────────────────────────────
ROOT      = Path("c:/01coding/stock-game/backend")
DB_PATH   = ROOT / "data" / "stocks.db"
ML_DIR    = ROOT / "ml"
MODEL_DIR = ROOT / "models"
sys.path.insert(0, str(ML_DIR))

from dataset import (
    FEATURE_COLS_REDUCED, _etf_symbols,
    MIN_VOL_KRW_20D, _ETF_PATTERN,
)
from evaluate import precision_at_topk

logging.basicConfig(level=logging.WARNING)   # dataset.py 로그 억제

# ── 하이퍼파라미터 ─────────────────────────────────────────────
LABEL_THRESHOLD = 0.07   # 60d 초과수익 >= +7%
VAL_DAYS        = 252    # 마지막 252 거래일 = val
PURGE_DAYS      = 60     # 라벨 누수 방지용 정화 간격 — train 마지막 60거래일 행은
                         # 라벨 계산 시 val 구간 종가를 참조하므로 제거

EXISTING_PIT = [
    'eps_growth_yoy', 'eps_growth_accel',
    'roe_level', 'dps_growth_yoy', 'neg_pbr',
]
NEW_FACTORS = ['bps_growth_yoy']                           # 이번 추가 후보
FEATURES_A  = FEATURE_COLS_REDUCED + EXISTING_PIT          # 50개 (현 운영 기준선)
FEATURES_B  = FEATURES_A + NEW_FACTORS                     # 51개 (b')

# 현 운영 모델 기준선 (target_60d_20260703_143307, force-promote)
BASELINE_K10 = 0.2578
BASELINE_K20 = 0.2165

CB_PARAMS = {
    'iterations':            2000,
    'depth':                 5,
    'learning_rate':         0.02,   # 낮춰서 더 긴 탐색
    'loss_function':         'Logloss',
    'eval_metric':           'AUC',
    'random_seed':           42,
    'verbose':               200,
    'allow_writing_files':   False,
    'early_stopping_rounds': 100,
    'task_type':             'CPU',
    'thread_count':          -1,
    'l2_leaf_reg':           3.0,
}

parser = argparse.ArgumentParser()
parser.add_argument('--force-promote', action='store_true', dest='force_promote',
                    help='판정 조건 불충족 시에도 (b) 모델 강제 promote')
args = parser.parse_args()

print("=" * 70, flush=True)
print("target_60d 전체 학습 (b') — bps_growth_yoy 추가")
print(f"  라벨 임계값: 60d 초과수익 >= {LABEL_THRESHOLD*100:.0f}%")
print(f"  val_days: {VAL_DAYS}")
print(f"  (a) {len(FEATURES_A)}피처(현 기준선)  |  (b') {len(FEATURES_B)}피처")
print(f"  기준선: K10={BASELINE_K10:+.4f}  K20={BASELINE_K20:+.4f}")
if args.force_promote:
    print("  *** --force-promote 플래그 활성화 ***")
print("=" * 70, flush=True)

# ─────────────────────────────────────────────────────────────
# 1. 데이터 로드
# ─────────────────────────────────────────────────────────────
t0 = time.time()
print("\n[1] 데이터 로드 중...", flush=True)
conn = sqlite3.connect(DB_PATH)

features_df = pd.read_sql_query(
    "SELECT * FROM features ORDER BY date, symbol", conn)
prices_df = pd.read_sql_query(
    "SELECT symbol, date, close, volume FROM prices ORDER BY symbol, date", conn)
eps_df = pd.read_sql_query(
    "SELECT symbol, biz_year, eps FROM dividends WHERE eps IS NOT NULL ORDER BY symbol, biz_year", conn)
bps_df = pd.read_sql_query(
    "SELECT symbol, biz_year, bps FROM financials WHERE bps IS NOT NULL ORDER BY symbol, biz_year", conn)
dps_df = pd.read_sql_query(
    "SELECT symbol, biz_year, dps FROM dividends "
    "WHERE dps IS NOT NULL AND dps>0 AND dps<1000000 ORDER BY symbol, biz_year", conn)
etf_syms = _etf_symbols(conn)
conn.close()

print(f"  features: {len(features_df):,}행 | prices: {len(prices_df):,}행", flush=True)

# ── 타입 통일 ──
for _df in [features_df, prices_df]:
    _df['symbol'] = _df['symbol'].astype(str).str.zfill(6)
    _df['date']   = pd.to_numeric(_df['date'], errors='coerce').astype(int)
for _df in [eps_df, bps_df, dps_df]:
    _df['symbol']   = _df['symbol'].astype(str).str.zfill(6)
    _df['biz_year'] = _df['biz_year'].astype(int)

# ─────────────────────────────────────────────────────────────
# 2. 60d 수익률 + 횡단면 중앙값 벤치마크
# ─────────────────────────────────────────────────────────────
print("\n[2] 60d 수익률 + 벤치마크 계산...", flush=True)

prices_df = prices_df.sort_values(['symbol', 'date']).reset_index(drop=True)
prices_df['close']  = prices_df['close'].astype(float)
prices_df['volume'] = prices_df['volume'].fillna(0).astype(float)
prices_df['vol_krw'] = prices_df['close'] * prices_df['volume']

# 유동성 필터용 rolling 거래대금
prices_df['vol_krw_20d_raw'] = prices_df.groupby('symbol')['vol_krw'].transform(
    lambda x: x.rolling(20, min_periods=10).mean()
)

# 60d 순수익률
prices_df['ret_fwd_60d'] = prices_df.groupby('symbol')['close'].transform(
    lambda x: x.shift(-60) / x - 1
)

# 전체 종목 횡단면 중앙값 벤치마크
valid_60d = prices_df.dropna(subset=['ret_fwd_60d'])
date_med  = valid_60d.groupby('date')['ret_fwd_60d'].median().rename('mkt_ret')
prices_df = prices_df.join(date_med, on='date')
prices_df['excess_60d'] = prices_df['ret_fwd_60d'] - prices_df['mkt_ret']

print(f"  60d 라벨 유효: {prices_df['excess_60d'].notna().sum():,}행", flush=True)

# ─────────────────────────────────────────────────────────────
# 3. join + 필터
# ─────────────────────────────────────────────────────────────
print("\n[3] features × label join + 필터...", flush=True)

price_meta = prices_df[['symbol','date','excess_60d','ret_fwd_60d','vol_krw_20d_raw','vol_krw']].copy()
df = features_df.merge(price_meta, on=['symbol','date'], how='inner')
print(f"  join 후:         {len(df):,}행")

# ETF 제외
before = len(df)
df = df[~df['symbol'].isin(etf_syms)]
print(f"  ETF 제외 후:     {len(df):,}행 (제거 {before-len(df):,})")

# 유동성 필터
before = len(df)
df = df[df['vol_krw_20d_raw'].fillna(0) >= MIN_VOL_KRW_20D]
print(f"  유동성 필터 후:  {len(df):,}행 (제거 {before-len(df):,})")

# 라벨 정의
df['label_60d'] = (df['excess_60d'] >= LABEL_THRESHOLD).astype('Int8')
df = df.dropna(subset=['label_60d', 'excess_60d'])
df = df.reset_index(drop=True)

pos_rate = float(df['label_60d'].mean())
print(f"  최종 행수:       {len(df):,}행 | positive={pos_rate:.1%}", flush=True)

# ─────────────────────────────────────────────────────────────
# 4. PIT join — 새 팩터 5개
# ─────────────────────────────────────────────────────────────
print("\n[4] PIT 신호 join (기존 5개 + bps_growth_yoy)...", flush=True)

from factors_60d import (
    compute_eps_signals, compute_roe_signals,
    compute_dps_signals, compute_bps_signals,
    join_pit_factors,
)

df['year']         = df['date'] // 10000
df['month']        = (df['date'] % 10000) // 100
df['biz_year_pit'] = np.where(df['month'] >= 4, df['year'] - 1, df['year'] - 2)

eps_s = compute_eps_signals(eps_df)
roe_s = compute_roe_signals(eps_df, bps_df)
dps_s = compute_dps_signals(dps_df)
bps_s = compute_bps_signals(bps_df)

df = join_pit_factors(df, eps_s, roe_s, dps_s, bps_s)
df['neg_pbr'] = -df['pbr'].astype(float)

print("  커버리지 (전체 피처셋 기준):")
all_pit = EXISTING_PIT + NEW_FACTORS
for col in all_pit:
    rate = df[col].notna().mean() if col in df.columns else float('nan')
    tag  = " ★ 신규" if col in NEW_FACTORS else ""
    print(f"    {col:20s}: {rate:.1%}{tag}")

# ─────────────────────────────────────────────────────────────
# 5. Train / Val 분리
# ─────────────────────────────────────────────────────────────
unique_dates = sorted(df['date'].unique())
cutoff_idx   = max(0, len(unique_dates) - VAL_DAYS)
cutoff       = unique_dates[cutoff_idx]

# purge: cutoff 직전 PURGE_DAYS(60)거래일은 train에서 제외.
# 60d 라벨 = shift(-60)/x - 1 이므로 이 구간 행들의 라벨이 val 종가를 참조 → look-ahead leak.
purge_idx  = max(0, cutoff_idx - PURGE_DAYS)
purge_date = unique_dates[purge_idx]

df_tr = df[df['date'] < purge_date].reset_index(drop=True)
df_va = df[df['date'] >= cutoff].reset_index(drop=True)

print(f"\n[5] 분리 기준일: {cutoff} (purge 시작: {purge_date}, 정화 구간: {purge_idx}~{cutoff_idx})")
print(f"    train: {len(df_tr):,}행 / {df_tr['date'].nunique()}거래일 (pos={df_tr['label_60d'].mean():.1%})")
print(f"    purge: {cutoff_idx - purge_idx}거래일 제거 (라벨 누수 방지)")
print(f"    val:   {len(df_va):,}행 / {df_va['date'].nunique()}거래일 (pos={df_va['label_60d'].mean():.1%})", flush=True)

val_meta_base = df_va[['symbol','date']].copy()
rand_base     = float(df_va['label_60d'].mean())

# ─────────────────────────────────────────────────────────────
# 6. 학습 함수
# ─────────────────────────────────────────────────────────────
def train_eval(df_tr, df_va, feat_list, tag):
    avail = [f for f in feat_list if f in df.columns]
    y_tr  = df_tr['label_60d'].astype(int).values
    y_va  = df_va['label_60d'].astype(int).values
    X_tr  = df_tr[avail].astype(float).values
    X_va  = df_va[avail].astype(float).values

    ptr = Pool(X_tr, y_tr, feature_names=avail)
    pva = Pool(X_va, y_va, feature_names=avail)

    print(f"\n[학습] {tag} ({len(avail)}피처)", flush=True)
    t = time.time()
    m = CatBoostClassifier(**CB_PARAMS)
    m.fit(ptr, eval_set=pva)
    print(f"  완료: {time.time()-t:.0f}초", flush=True)

    tr_proba = m.predict_proba(X_tr)[:, 1]
    va_proba = m.predict_proba(X_va)[:, 1]
    tr_auc   = roc_auc_score(y_tr, tr_proba)
    va_auc   = roc_auc_score(y_va, va_proba)

    tr_patk = precision_at_topk(
        pd.Series(y_tr), tr_proba, df_tr[['symbol','date']])
    va_patk = precision_at_topk(
        pd.Series(y_va), va_proba, val_meta_base)

    return m, avail, tr_auc, va_auc, tr_patk, va_patk, va_proba

# ─────────────────────────────────────────────────────────────
# 7. (a) 50피처 기준선 학습
# ─────────────────────────────────────────────────────────────
m_a, feats_a, tr_auc_a, va_auc_a, tr_p_a, va_p_a, va_proba_a = \
    train_eval(df_tr, df_va, FEATURES_A, "(a) 50피처 기준선")

# ─────────────────────────────────────────────────────────────
# 8. (b') 51피처 학습
# ─────────────────────────────────────────────────────────────
m_b, feats_b, tr_auc_b, va_auc_b, tr_p_b, va_p_b, va_proba_b = \
    train_eval(df_tr, df_va, FEATURES_B, "(b') 51피처")

# ─────────────────────────────────────────────────────────────
# 9. 결과 비교
# ─────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("[결과 비교] (a) 50피처 기준선 vs (b') 51피처")
print("=" * 70)
print(f"  무작위 기준선 P@10 = {rand_base:.1%}  (positive {rand_base:.1%})\n")

_b_label = "(b') 51피처"
header = f"  {'지표':>22} | {'(a) 50피처':>12} | {_b_label:>12} | {'차이':>8}"
sep    = "  " + "-" * 24 + "+" + "-" * 14 + "+" + "-" * 14 + "+" + "-" * 10
print(header)
print(sep)

def row(label, va, vb):
    return f"  {label:>22} | {va:>12.4f} | {vb:>12.4f} | {vb-va:>+8.4f}"

print(row("train AUC",   tr_auc_a,         tr_auc_b))
print(row("val AUC",     va_auc_a,         va_auc_b))
print(row("AUC 갭(tr-va)", tr_auc_a-va_auc_a, tr_auc_b-va_auc_b))
print(sep)

for k in [10, 20, 30]:
    print(f"  {'train P@'+str(k):>22} | {tr_p_a.get(k,0):>12.1%} | {tr_p_b.get(k,0):>12.1%} | {tr_p_b.get(k,0)-tr_p_a.get(k,0):>+8.2%}")
    print(f"  {'val P@'+str(k):>22} | {va_p_a.get(k,0):>12.1%} | {va_p_b.get(k,0):>12.1%} | {va_p_b.get(k,0)-va_p_a.get(k,0):>+8.2%}")
    print(sep)

# ─────────────────────────────────────────────────────────────
# 10. SHAP — 모델 (b)
# ─────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("[SHAP] 모델 (b') 51피처  ★=신규  ▶=기존PIT")
print("=" * 70)

avail_b  = [f for f in FEATURES_B if f in df.columns]
ptr_b_sh = Pool(df_tr[avail_b].astype(float).values,
               df_tr['label_60d'].astype(int).values,
               feature_names=avail_b)
sv   = m_b.get_feature_importance(ptr_b_sh, type='ShapValues')[:, :-1]
sm   = np.abs(sv).mean(axis=0)
sp   = sm / sm.sum() * 100
shap_df = (pd.DataFrame({'feature': avail_b, 'shap_pct': sp})
             .sort_values('shap_pct', ascending=False)
             .reset_index(drop=True))

print(f"\n  {'#':>3} | {'피처':>24} | {'SHAP%':>7}")
print("  " + "-" * 42)
for rank, row_ in shap_df.iterrows():
    if row_['feature'] in NEW_FACTORS:
        tag = " ★"
    elif row_['feature'] in EXISTING_PIT:
        tag = " ▶"
    else:
        tag = ""
    print(f"  {rank+1:>3} | {row_['feature']:>24} | {row_['shap_pct']:>7.2f}%{tag}")

# ─────────────────────────────────────────────────────────────
# 11. 판정 — 주지표: K=10/K=20 포트폴리오 평균 초과수익률(연속량)
# ─────────────────────────────────────────────────────────────

def _excess_at_k(proba_arr, meta, k):
    m = meta.copy()
    m['proba'] = proba_arr
    vals = []
    for _, grp in m.groupby('date'):
        if len(grp) < k:
            continue
        vals.append(grp.nlargest(k, 'proba')['excess_60d'].mean())
    return round(float(np.nanmean(vals)), 4) if vals else float('nan')

_va_meta = val_meta_base.copy()
_va_meta['excess_60d'] = df_va['excess_60d'].values

val_exc_k10_a = _excess_at_k(va_proba_a, _va_meta, 10)
val_exc_k20_a = _excess_at_k(va_proba_a, _va_meta, 20)
val_exc_k10_b = _excess_at_k(va_proba_b, _va_meta, 10)
val_exc_k20_b = _excess_at_k(va_proba_b, _va_meta, 20)

bps_shap = float(shap_df[shap_df['feature'] == 'bps_growth_yoy']['shap_pct'].values[0]) \
    if 'bps_growth_yoy' in shap_df['feature'].values else 0.0
bps_rank = int(shap_df[shap_df['feature'] == 'bps_growth_yoy'].index[0]) + 1 \
    if 'bps_growth_yoy' in shap_df['feature'].values else 999

EPSILON = 0.005  # 악화 판단 허용 여유

cond_shap    = bps_shap > 0.5
improve_k10  = val_exc_k10_b > val_exc_k10_a + EPSILON
improve_k20  = val_exc_k20_b > val_exc_k20_a + EPSILON
no_worse_k10 = val_exc_k10_b >= val_exc_k10_a - EPSILON
no_worse_k20 = val_exc_k20_b >= val_exc_k20_a - EPSILON
cond_k       = (improve_k10 or improve_k20) and no_worse_k10 and no_worse_k20

print("\n" + "=" * 70)
print("판정 (사전 고정 기준 — 주지표: K=10/K=20 연속 초과수익률)")
print("=" * 70)
print(f"\n  현 기준선(운영 모델): K10={BASELINE_K10:+.4f}  K20={BASELINE_K20:+.4f}")
print(f"\n  (a) 50피처 기준선(신규 학습): K10={val_exc_k10_a:+.4f}  K20={val_exc_k20_a:+.4f}")
print(f"  (b') 51피처:                  K10={val_exc_k10_b:+.4f}  K20={val_exc_k20_b:+.4f}")
print(f"  차이 (b')-(a):               K10={val_exc_k10_b-val_exc_k10_a:+.4f}  K20={val_exc_k20_b-val_exc_k20_a:+.4f}")
print()
print(f"  [S] bps_growth_yoy SHAP>0.5%: {'✅' if cond_shap else '❌'}  ({bps_shap:.2f}%  #{bps_rank}위)")
print(f"  [K] 최소 하나 개선·나머지 악화없음:")
print(f"       K10 개선(+{EPSILON}): {'✅' if improve_k10 else '❌'}  ({val_exc_k10_b:+.4f} vs {val_exc_k10_a:+.4f})")
print(f"       K20 개선(+{EPSILON}): {'✅' if improve_k20 else '❌'}  ({val_exc_k20_b:+.4f} vs {val_exc_k20_a:+.4f})")
print(f"       K10 악화없음:          {'✅' if no_worse_k10 else '❌'}")
print(f"       K20 악화없음:          {'✅' if no_worse_k20 else '❌'}")

promote = (cond_shap and cond_k) or args.force_promote
if args.force_promote and not (cond_shap and cond_k):
    print("\n  (--force-promote로 조건 미충족 상태에서 promote 진행)")

if promote:
    ts        = datetime.now().strftime('%Y%m%d_%H%M%S')
    model_dir = MODEL_DIR / f"target_60d_{ts}"
    model_dir.mkdir(parents=True, exist_ok=True)

    # 현 ACTIVE 모델 archived_ 보존
    active_ptr = MODEL_DIR / "ACTIVE_target_60d.txt"
    if active_ptr.exists():
        old_name = active_ptr.read_text(encoding='utf-8').strip()
        old_dir  = MODEL_DIR / old_name
        if old_dir.exists() and not old_name.startswith('archived_'):
            new_name = f"archived_{old_name}"
            old_dir.rename(MODEL_DIR / new_name)
            print(f"\n  기존 모델 보존: {old_name} → {new_name}")

    with open(model_dir / "model_cat.pkl", "wb") as _f:
        pickle.dump(m_b, _f)
    (model_dir / "feature_cols.txt").write_text('\n'.join(avail_b), encoding='utf-8')

    meta = {
        'target_col':               'target_60d',
        'label_threshold':          LABEL_THRESHOLD,
        'label_def':                'excess_60d >= 0.07 (vs cross-sectional median)',
        'features':                 avail_b,
        'n_features':               len(avail_b),
        'val_auc':                  round(va_auc_b, 4),
        'val_p10':                  round(va_p_b.get(10, float('nan')), 4),
        'val_p20':                  round(va_p_b.get(20, float('nan')), 4),
        'val_p30':                  round(va_p_b.get(30, float('nan')), 4),
        'val_excess_return_k10':    val_exc_k10_b,
        'val_excess_return_k20':    val_exc_k20_b,
        'baseline_k10':             BASELINE_K10,
        'baseline_k20':             BASELINE_K20,
        'bps_growth_shap_pct':      round(bps_shap, 3),
        'bps_growth_rank':          int(bps_rank),
        'existing_pit':             EXISTING_PIT,
        'new_factors':              NEW_FACTORS,
        'force_promoted':           args.force_promote,
        'purge_days':               PURGE_DAYS,
        'purge_date':               purge_date,
        'created_at':               ts,
    }
    (model_dir / "model_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, cls=_NumpyEncoder), encoding='utf-8')

    shap_df.to_csv(model_dir / "shap_importance.csv", index=False, encoding='utf-8-sig')

    active_ptr.write_text(model_dir.name, encoding='utf-8')

    print(f"\n  ★ PROMOTE ★")
    print(f"  모델 저장: {model_dir}")
    print(f"  ACTIVE_target_60d.txt → {model_dir.name}")
    print(f"\n  → PM2 재시작 필요: npx pm2 restart stock-backend --update-env")
else:
    reasons = []
    if not cond_shap: reasons.append(f"bps SHAP {bps_shap:.2f}% ≤ 0.5%")
    if not cond_k:
        if not (improve_k10 or improve_k20):
            reasons.append("K10·K20 둘 다 개선 없음")
        if not no_worse_k10:
            reasons.append(f"K10 악화: {val_exc_k10_b:+.4f} < {val_exc_k10_a-EPSILON:+.4f}")
        if not no_worse_k20:
            reasons.append(f"K20 악화: {val_exc_k20_b:+.4f} < {val_exc_k20_a-EPSILON:+.4f}")
    print(f"\n  ★ NO PROMOTE ★")
    print(f"  사유: {', '.join(reasons)}")
    print(f"\n  현 운영 모델 유지: ACTIVE_target_60d.txt 변경 없음")

print(f"\n총 소요: {time.time()-t0:.0f}초")
print("\n완료!", flush=True)
