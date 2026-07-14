"""
integrated_backtest.py — 운용 규칙 v1.1 통합 백테스트 (2026-07-09)

규칙 v1.1: 5d(30%) G2게이트 적용 / 60d(70%) 게이트 없음 (무조건 60거래일 주기 진입)
  - v1.0 수정 근거: G2 검증은 5d 데이터로만 수행됐고 60d 적용은 미검증 외삽이었음
    (통합 백테스트로 판명 — 60d에 G2 적용 시 타이밍 어긋나 KOSPI 대비 추가 손실)
  - 60d 하락장 리스크는 게이트가 아닌 "장기보유+저회전+MDD" 특성으로 관리

4-way 비교: v1.0(G2전체) / v1.1(5d-G2) / G0(게이트없음) / KOSPI
기간: 2022~2026
비용: 익일시가 진입, 왕복 0.63%

주의: 2022~2024는 현 운영 모델의 학습 구간 포함(IS). 절대 수치 과신 금물.
목적: 라이브 페이퍼트레이딩이 그릴 "기대 형태" 파악. 규칙 변경 근거로 쓰지 말 것.
"""

import json
import logging
import pickle
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 경로 설정 ─────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
ML_DIR     = ROOT / "ml"
SERVER_DIR = ROOT / "server"
DB_PATH    = ROOT / "data" / "stocks.db"
MODELS_DIR = ROOT.parent / "models"

for _d in [str(ML_DIR), str(SERVER_DIR)]:
    if _d not in sys.path:
        sys.path.insert(0, _d)

from evaluate import COMMISSION, SELL_TAX, SLIPPAGE, MIN_VOLUME_KRW, LIMIT_UP, _net_ret
from dataset import FEATURE_COLS_REDUCED, _ETF_PATTERN
from ensemble_config import CAT_BLEND_WEIGHT, XGB_BLEND_WEIGHT
from factors_60d import (
    load_factor_data, compute_eps_signals, compute_roe_signals,
    compute_dps_signals, compute_bps_signals, biz_year_pit,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("int_bt")

# ── 파라미터 ──────────────────────────────────────────────────────
INITIAL_CAPITAL = 100_000_000  # 1억원
W5  = 0.30  # 5d 슬리브 비중
W60 = 0.70  # 60d 슬리브 비중
TOP_K = 10
START = "20220101"
END   = "20261231"


# ════════════════════════════════════════════════════════════════
# 1. G2 게이트 타임라인
# ════════════════════════════════════════════════════════════════

def build_gate_timeline(conn: sqlite3.Connection) -> pd.DataFrame:
    """날짜별 G2 게이트 상태 (predictor.get_regime_gate_g2 동일 알고리즘)."""
    rows = conn.execute(
        "SELECT date, close FROM market_index WHERE code='1001' ORDER BY date"
    ).fetchall()
    dates  = [r[0] for r in rows]
    closes = np.array([float(r[1]) for r in rows])

    # 이동평균 (누적합 방식)
    def _rm(a: np.ndarray, n: int) -> np.ndarray:
        out = np.full(len(a), np.nan)
        cs = np.cumsum(a)
        # cs[n-1:] (길이 N-n+1)에서 [0, cs[0..N-n-1]] (길이 N-n+1)를 빼면 창 합
        out[n-1:] = (cs[n-1:] - np.concatenate([[0], cs[:len(a)-n]])) / n
        return out

    ma60  = _rm(closes, 60)
    ma120 = _rm(closes, 120)

    blocked = False
    bear_streak = bull_streak = 0
    recs = []

    for i, dt in enumerate(dates):
        if np.isnan(ma60[i]) or np.isnan(ma120[i]):
            recs.append({"date": dt, "regime": "bull", "gate_blocked": False,
                         "ma60": np.nan, "ma120": np.nan})
            continue

        is_bear = ma60[i] <= ma120[i]
        if is_bear:
            bear_streak += 1; bull_streak = 0
        else:
            bull_streak += 1; bear_streak = 0

        if bear_streak >= 2:
            blocked = True
        if blocked and bull_streak >= 2:
            blocked = False

        recs.append({
            "date":         dt,
            "regime":       "bear" if is_bear else "bull",
            "gate_blocked": blocked,
            "ma60":         round(ma60[i], 2),
            "ma120":        round(ma120[i], 2),
        })

    df = pd.DataFrame(recs)
    n_blocked = df["gate_blocked"].sum()
    log.info("G2 게이트 타임라인: %d일 / 차단 %d일 (%.1f%%)",
             len(df), n_blocked, 100*n_blocked/len(df))
    return df


def identify_bear_blocks(gate_df: pd.DataFrame) -> List[dict]:
    """연속 gate_blocked=True 구간을 A/B/C/D/E... 로 레이블링."""
    blocks = []
    in_block = False
    start = None

    for _, row in gate_df.iterrows():
        if row["gate_blocked"] and not in_block:
            in_block = True
            start = row["date"]
        elif not row["gate_blocked"] and in_block:
            in_block = False
            blocks.append({"label": chr(65 + len(blocks)), "start": start, "end": row["date"]})

    if in_block:
        blocks.append({"label": chr(65 + len(blocks)), "start": start,
                       "end": gate_df["date"].iloc[-1]})

    for b in blocks:
        log.info("  차단구간 %s: %s ~ %s", b["label"], b["start"], b["end"])
    return blocks


# ════════════════════════════════════════════════════════════════
# 2. 모델 로드
# ════════════════════════════════════════════════════════════════

def load_5d_model():
    from predictor import find_latest_model_dir
    model_dir = find_latest_model_dir("target_5d")
    if not model_dir:
        raise FileNotFoundError("5d 모델 없음")
    meta = json.loads((model_dir / "meta.json").read_text("utf-8")) \
           if (model_dir / "meta.json").exists() else {}
    fcols = meta.get("feature_cols", FEATURE_COLS_REDUCED)

    with open(model_dir / "model_cat.pkl", "rb") as f:
        cat = pickle.load(f)
    with open(model_dir / "model_xgb.pkl", "rb") as f:
        xgb = pickle.load(f)

    log.info("5d 모델 로드: %s (%d 피처)", model_dir.name, len(fcols))
    return (cat, xgb), fcols


def load_60d_model():
    from predictor import find_latest_model_dir
    model_dir = find_latest_model_dir("target_60d")
    if not model_dir:
        raise FileNotFoundError("60d 모델 없음")

    # 피처 목록 우선순위: feature_cols.txt → meta.json/feature_cols → model_meta.json/features
    txt_path = model_dir / "feature_cols.txt"
    if txt_path.exists():
        fcols = [c.strip() for c in txt_path.read_text("utf-8").strip().splitlines() if c.strip()]
    else:
        for fname in ["meta.json", "model_meta.json"]:
            p = model_dir / fname
            if p.exists():
                m = json.loads(p.read_text("utf-8"))
                fcols = m.get("feature_cols") or m.get("features") or []
                if fcols:
                    break
        else:
            fcols = []

    with open(model_dir / "model_cat.pkl", "rb") as f:
        cat = pickle.load(f)

    log.info("60d 모델 로드: %s (%d 피처)", model_dir.name, len(fcols))
    return cat, fcols


# ════════════════════════════════════════════════════════════════
# 3. 데이터 로드 & 전처리
# ════════════════════════════════════════════════════════════════

def load_data(conn: sqlite3.Connection, fcols_5d: list, fcols_60d: list) -> pd.DataFrame:
    """features + prices 로드, 익일시가 기준 forward return 계산."""
    # ETF 제외 심볼
    stocks = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)
    stocks["symbol"] = stocks["symbol"].astype(str).str.zfill(6)
    etf_syms = set(
        stocks[stocks["name"].str.contains(_ETF_PATTERN, na=False, case=False, regex=True)]["symbol"]
    )
    log.info("ETF 제외: %d종목", len(etf_syms))

    # features 테이블 컬럼 확인
    avail = {r[1] for r in conn.execute("PRAGMA table_info(features)").fetchall()}
    need  = list((set(fcols_5d) | set(fcols_60d)) & avail)
    sel   = ", ".join(["f.symbol", "f.date"] + [f"f.{c}" for c in need])

    log.info("features 로드 중 (%d컬럼)...", len(need))
    feat = pd.read_sql_query(
        f"SELECT {sel} FROM features f WHERE f.date >= ? AND f.date <= ?",
        conn, params=(START, END),
    )
    feat["symbol"] = feat["symbol"].astype(str).str.zfill(6)
    feat = feat[~feat["symbol"].isin(etf_syms)].reset_index(drop=True)
    log.info("features 로드 완료: %d행 / %d종목 / %d날짜",
             len(feat), feat["symbol"].nunique(), feat["date"].nunique())

    # 가격 로드 (open, close, volume) — 선도 수익률 계산을 위해 더 넓은 기간
    log.info("prices 로드 중...")
    # 60d forward return을 위해 END보다 60거래일 더 필요 — 근사로 90일 추가
    end_ext = pd.Timestamp(END) + pd.Timedelta(days=120)
    price = pd.read_sql_query(
        """SELECT symbol, date, open, close, volume
           FROM prices WHERE date >= ? AND date <= ?""",
        conn, params=(START, end_ext.strftime("%Y%m%d")),
    )
    price["symbol"] = price["symbol"].astype(str).str.zfill(6)
    price = price[~price["symbol"].isin(etf_syms)]
    price = price.sort_values(["symbol", "date"]).reset_index(drop=True)
    log.info("prices 로드 완료: %d행", len(price))

    # 이동 컬럼 계산
    g = price.groupby("symbol", sort=False)
    price["open_next"]  = g["open"].shift(-1)    # 익일 시가
    price["close_5d"]   = g["close"].shift(-5)   # 5거래일 후 종가
    price["close_60d"]  = g["close"].shift(-60)  # 60거래일 후 종가
    price["close_prev"] = g["close"].shift(1)     # 전일 종가
    price["volume_krw"] = price["close"] * price["volume"]

    # 상한가: 당일 close / 전일 close - 1 >= 0.29
    price["is_limit_up"] = (
        price["close"] / price["close_prev"].replace(0, np.nan) - 1
    ).fillna(0) >= LIMIT_UP

    # 익일시가 기준 선도 수익률
    price["ret_adj_5d"]  = price["close_5d"]  / price["open_next"] - 1
    price["ret_adj_60d"] = price["close_60d"] / price["open_next"] - 1

    # feat에 가격 정보 merge
    price_sub = price[["symbol", "date", "volume_krw", "is_limit_up",
                        "ret_adj_5d", "ret_adj_60d"]].copy()
    feat = feat.merge(price_sub, on=["symbol", "date"], how="left")

    return feat


def add_60d_factors(feat: pd.DataFrame, conn: sqlite3.Connection, fcols_60d: list) -> pd.DataFrame:
    """feat에 PIT 60d 팩터 컬럼 추가."""
    eps_df, bps_df, dps_df = load_factor_data(conn)
    eps_s = compute_eps_signals(eps_df)
    roe_s = compute_roe_signals(eps_df, bps_df)
    dps_s = compute_dps_signals(dps_df)
    bps_s = compute_bps_signals(bps_df)

    factors = (
        eps_s[["symbol", "biz_year", "eps_growth_yoy", "eps_growth_accel"]]
        .merge(roe_s[["symbol", "biz_year", "roe_level"]], on=["symbol", "biz_year"], how="outer")
        .merge(dps_s[["symbol", "biz_year", "dps_growth_yoy"]], on=["symbol", "biz_year"], how="outer")
        .merge(bps_s[["symbol", "biz_year", "bps_growth_yoy"]], on=["symbol", "biz_year"], how="outer")
    )

    feat["biz_year_pit"] = feat["date"].astype(int).apply(biz_year_pit)

    factor_cols = ["eps_growth_yoy", "eps_growth_accel", "roe_level", "dps_growth_yoy", "bps_growth_yoy"]
    needed = [c for c in factor_cols if c in fcols_60d]
    if needed:
        f_sub = factors[["symbol", "biz_year"] + needed].rename(columns={"biz_year": "biz_year_pit"})
        feat = feat.merge(f_sub, on=["symbol", "biz_year_pit"], how="left")

    # neg_pbr = -pbr (PIT 수정된 pbr이 이미 features에 있음)
    if "neg_pbr" in fcols_60d and "pbr" in feat.columns:
        feat["neg_pbr"] = -feat["pbr"].fillna(np.nan)

    return feat


# ════════════════════════════════════════════════════════════════
# 4. 예측 점수 생성
# ════════════════════════════════════════════════════════════════

def compute_scores(feat: pd.DataFrame, model_5d, fcols_5d, model_60d, fcols_60d) -> pd.DataFrame:
    # 모델이 기대하는 모든 피처 컬럼을 순서대로 포함 (없는 컬럼은 NaN → CatBoost/XGB 결측 처리)
    log.info("5d 점수 계산 중 (%d 피처)...", len(fcols_5d))
    cat, xgb = model_5d
    X5_df = feat.reindex(columns=fcols_5d)  # CatBoost: DataFrame으로 컬럼명 매칭
    missing_5d = [c for c in fcols_5d if c not in feat.columns]
    if missing_5d:
        log.warning("5d 피처 %d개 누락(NaN 처리): %s", len(missing_5d), missing_5d[:5])
    feat["score_5d"] = (
        CAT_BLEND_WEIGHT * cat.predict_proba(X5_df)[:, 1]
        + XGB_BLEND_WEIGHT * xgb.predict_proba(X5_df.fillna(0.0).values)[:, 1]
    )

    log.info("60d 점수 계산 중 (%d 피처)...", len(fcols_60d))
    X60_df = feat.reindex(columns=fcols_60d)
    missing_60d = [c for c in fcols_60d if c not in feat.columns]
    if missing_60d:
        log.warning("60d 피처 %d개 누락(NaN 처리): %s", len(missing_60d), missing_60d[:5])
    feat["score_60d"] = model_60d.predict_proba(X60_df)[:, 1]

    return feat


# ════════════════════════════════════════════════════════════════
# 5. 슬리브 시뮬레이션
# ════════════════════════════════════════════════════════════════

def _period_return(day_df: pd.DataFrame, score_col: str, ret_col: str) -> Optional[float]:
    """하루치 데이터에서 top-K 순수익률 평균. 유효 데이터 없으면 None."""
    valid = day_df.copy()
    # 유동성 필터
    if "volume_krw" in valid.columns:
        valid = valid[valid["volume_krw"].fillna(0) >= MIN_VOLUME_KRW]
    # 상한가 필터
    if "is_limit_up" in valid.columns:
        valid = valid[~valid["is_limit_up"].fillna(False)]
    # 선도 수익률 없는 행 제거 (기간 끝자락, inf 포함)
    valid = valid.replace([np.inf, -np.inf], np.nan).dropna(subset=[ret_col])
    if len(valid) == 0:
        return None

    top = valid.nlargest(TOP_K, score_col).head(TOP_K)
    if top.empty:
        return None
    return _net_ret(float(top[ret_col].mean()))


def simulate_sleeve(
    feat: pd.DataFrame,
    gate_map: dict,
    score_col: str,
    ret_col: str,
    rebal_days: int,
    initial_cap: float,
    use_gate: bool = True,
) -> Tuple[pd.Series, List[dict]]:
    """
    슬리브 시뮬레이션. 반환: (날짜별 자본 Series, 기간 목록)

    날짜별 자본 = 마지막 청산 후 자본 (스텝 함수, 청산일에 업데이트).
    G2 gate 차단 중 신규진입 없음 — 기존 포지션은 만기대로 청산.
    """
    trading_dates = sorted(feat["date"].unique())
    n = len(trading_dates)

    cap = initial_cap
    last_rebal_idx = -rebal_days  # 첫 날 즉시 진입 허용
    periods: List[dict] = []

    # exit_date → new_capital (청산일에 자본 업데이트)
    exit_cap: dict = {}

    for i, date in enumerate(trading_dates):
        days_since = i - last_rebal_idx
        if days_since < rebal_days:
            continue

        # G2 게이트 체크
        if use_gate and gate_map.get(date, False):
            # 차단 중 — 주기가 됐어도 진입 안 함
            # last_rebal_idx는 갱신하지 않아 다음 날도 계속 체크됨
            continue

        # 진입
        day_df = feat[feat["date"] == date]
        r = _period_return(day_df, score_col, ret_col)
        if r is None:
            # 유효 종목 없음 — 진입 스킵, 다음 주기에 재시도 (last_rebal_idx 갱신 안 함)
            continue

        exit_idx  = min(i + rebal_days, n - 1)
        exit_date = trading_dates[exit_idx]
        old_cap   = cap
        cap       = cap * (1 + r)

        periods.append({
            "entry_date": date,
            "exit_date":  exit_date,
            "return_pct": round(r * 100, 4),
            "capital_after": round(cap, 0),
        })
        exit_cap[exit_date] = cap
        last_rebal_idx = i

    # 날짜별 자본 Series (스텝 함수)
    cap_series = pd.Series(initial_cap, index=trading_dates, dtype=float)
    running = initial_cap
    for dt in trading_dates:
        if dt in exit_cap:
            running = exit_cap[dt]
        cap_series[dt] = running

    log.info("  %s 슬리브: %d기간, 최종자본 %.0f원 (%.1f%%)",
             ret_col, len(periods), running, (running/initial_cap - 1)*100)
    return cap_series, periods


# ════════════════════════════════════════════════════════════════
# 6. 성과 지표 계산
# ════════════════════════════════════════════════════════════════

def mdd(cap_series: pd.Series) -> float:
    peak = cap_series.cummax()
    return float((cap_series / peak - 1).min())


def annual_returns(cap_series: pd.Series, initial_cap: float) -> dict:
    """연도별 수익률 (연초 → 연말 자본 비교)."""
    result = {}
    cap_series = cap_series.copy()
    cap_series.index = pd.to_datetime(cap_series.index, format="%Y%m%d")

    for year in cap_series.index.year.unique():
        yr_data = cap_series[cap_series.index.year == year]
        if yr_data.empty:
            continue
        start_val = cap_series[cap_series.index < yr_data.index[0]].iloc[-1] \
            if len(cap_series[cap_series.index < yr_data.index[0]]) > 0 else initial_cap
        end_val = yr_data.iloc[-1]
        result[int(year)] = round((end_val / start_val - 1) * 100, 2)
    return result


def monthly_capitals(cap_series: pd.Series) -> pd.Series:
    """월말 자본 Series."""
    s = cap_series.copy()
    s.index = pd.to_datetime(s.index, format="%Y%m%d")
    return s.resample("ME").last()


def bear_block_performance(
    cap_series: pd.Series,
    periods: List[dict],
    bear_blocks: List[dict],
) -> List[dict]:
    """차단 구간별 성적 (차단 중 열린 포지션 기준)."""
    results = []
    for blk in bear_blocks:
        # 차단 구간과 겹치는 기간들 (진입일이 차단 기간에 있는 것)
        blk_periods = [
            p for p in periods
            if blk["start"] <= p["entry_date"] <= blk["end"]
        ]
        n = len(blk_periods)
        avg_ret = float(np.mean([p["return_pct"] for p in blk_periods])) if n > 0 else None

        # 구간 전후 자본 비교 (cap_series에서 직접)
        s = cap_series.copy()
        s.index = pd.to_datetime(s.index, format="%Y%m%d")
        blk_s = pd.to_datetime(blk["start"], format="%Y%m%d")
        blk_e = pd.to_datetime(blk["end"], format="%Y%m%d")
        before = s[s.index <= blk_s].iloc[-1] if not s[s.index <= blk_s].empty else None
        after  = s[s.index >= blk_e].iloc[0]  if not s[s.index >= blk_e].empty else None
        block_ret = (after / before - 1) * 100 if (before and after) else None

        results.append({
            "label":      blk["label"],
            "start":      blk["start"],
            "end":        blk["end"],
            "n_periods":  n,
            "avg_return": round(avg_ret, 2) if avg_ret is not None else None,
            "block_total_return_pct": round(block_ret, 2) if block_ret is not None else None,
        })
    return results


def kospi_returns(gate_df: pd.DataFrame, conn: sqlite3.Connection) -> pd.Series:
    """KOSPI 누적 수익률 Series (trading_dates 기준)."""
    rows = conn.execute(
        "SELECT date, close FROM market_index WHERE code='1001' ORDER BY date"
    ).fetchall()
    kdf = pd.DataFrame(rows, columns=["date", "close"])
    kdf = kdf[(kdf["date"] >= START) & (kdf["date"] <= END)]
    kdf["close"] = kdf["close"].astype(float)
    kdf["ret"] = kdf["close"].pct_change().fillna(0)
    kdf["cum"] = (1 + kdf["ret"]).cumprod() - 1
    kdf = kdf.set_index("date")["cum"]
    return kdf


# ════════════════════════════════════════════════════════════════
# 7. 출력
# ════════════════════════════════════════════════════════════════

def print_results(
    portfolio_g2:   pd.Series,  # v1.0: 5d-G2 + 60d-G2
    portfolio_v11:  pd.Series,  # v1.1: 5d-G2 + 60d-G0  (운용 규칙)
    portfolio_g0:   pd.Series,  # G0: 게이트 없음
    kospi:          pd.Series,
    periods_5d_g2:  List[dict],
    periods_60d_v11:List[dict],
    bear_blocks:    List[dict],
    initial_cap:    float,
):
    print("\n" + "=" * 75)
    print("  운용 규칙 v1.1 통합 백테스트 결과 (2022~2026)")
    print("  v1.1: 5d-G2게이트 / 60d-게이트없음 (무조건 60거래일 주기)")
    print("  [주의] 2022~2024: IS 구간 포함 -- 절대 수치 과신 금지")
    print("=" * 75)

    # ── 전체 요약 ──
    total_g2  = (portfolio_g2.iloc[-1]  / initial_cap - 1) * 100
    total_v11 = (portfolio_v11.iloc[-1] / initial_cap - 1) * 100
    total_g0  = (portfolio_g0.iloc[-1]  / initial_cap - 1) * 100
    total_kp  = kospi.iloc[-1] * 100 if len(kospi) > 0 else float("nan")
    mdd_g2    = mdd(portfolio_g2)  * 100
    mdd_v11   = mdd(portfolio_v11) * 100
    mdd_g0    = mdd(portfolio_g0)  * 100

    print(f"\n[ 전체 성과 ({START}~{END}) ]")
    print(f"  v1.0 G2전체 (5d-G2+60d-G2): {total_g2:+.1f}%  (MDD {mdd_g2:.1f}%)")
    print(f"  v1.1 5d-G2  (5d-G2+60d-G0): {total_v11:+.1f}%  (MDD {mdd_v11:.1f}%)  ← 운용 규칙")
    print(f"  G0 게이트없음  (5d-G0+60d-G0): {total_g0:+.1f}%  (MDD {mdd_g0:.1f}%)")
    print(f"  KOSPI 벤치마크:              {total_kp:+.1f}%")
    print(f"  v1.1 vs KOSPI: {total_v11 - total_kp:+.1f}%p")
    print(f"  v1.1 vs v1.0:  {total_v11 - total_g2:+.1f}%p")
    print(f"  v1.1 vs G0:    {total_v11 - total_g0:+.1f}%p")

    # ── 연도별 ──
    print(f"\n[ 연도별 수익률 ]")
    ann_g2  = annual_returns(portfolio_g2,  initial_cap)
    ann_v11 = annual_returns(portfolio_v11, initial_cap)
    ann_g0  = annual_returns(portfolio_g0,  initial_cap)

    # KOSPI 연도별
    kdf = kospi.copy()
    kdf.index = pd.to_datetime(kdf.index, format="%Y%m%d")
    kdf_mult = kdf + 1

    ann_kp = {}
    for yr in sorted(ann_g2):
        yr_data = kdf_mult[kdf_mult.index.year == yr]
        if yr_data.empty:
            ann_kp[yr] = float("nan")
            continue
        prev = kdf_mult[kdf_mult.index.year < yr]
        start_mult = float(prev.iloc[-1]) if not prev.empty else 1.0
        end_mult   = float(yr_data.iloc[-1])
        ann_kp[yr] = round((end_mult / start_mult - 1) * 100, 2)

    print(f"  {'연도':>6}  {'v1.0 G2전체':>12}  {'v1.1 5d-G2':>12}  {'G0':>10}  {'KOSPI':>10}  {'v1.1-KOSPI':>12}")
    print(f"  {'-'*75}")
    for yr in sorted(ann_g2):
        g2r  = ann_g2.get(yr,  float("nan"))
        v11r = ann_v11.get(yr, float("nan"))
        g0r  = ann_g0.get(yr,  float("nan"))
        kpr  = ann_kp.get(yr,  float("nan"))
        vs_kp = v11r - kpr if not np.isnan(v11r) and not np.isnan(kpr) else float("nan")
        print(f"  {yr:>6}  {g2r:>+11.1f}%  {v11r:>+11.1f}%  {g0r:>+9.1f}%  {kpr:>+9.1f}%  {vs_kp:>+11.1f}%p")

    # ── 슬리브별 기간 수 (v1.1 기준) ──
    print(f"\n[ 슬리브별 통계 (v1.1 운용 규칙) ]")
    r5  = [p["return_pct"] for p in periods_5d_g2]
    r60 = [p["return_pct"] for p in periods_60d_v11]
    print(f"  5d 슬리브 (G2게이트): {len(r5):3d}기간, 평균 {np.mean(r5):+.2f}%, "
          f"승률 {sum(r>0 for r in r5)/max(1,len(r5))*100:.0f}%")
    print(f"  60d 슬리브 (게이트없음): {len(r60):3d}기간, 평균 {np.mean(r60):+.2f}%, "
          f"승률 {sum(r>0 for r in r60)/max(1,len(r60))*100:.0f}%")

    # ── G2 차단 구간 ──
    if bear_blocks:
        print(f"\n[ G2 차단 구간 성적 (차단 기간 중 누적 수익률) ]")
        print(f"  {'구간':>4}  {'시작':>10}  {'종료':>10}  {'전체수익':>10}  설명")
        print(f"  {'-'*60}")
        for b in bear_blocks[:8]:  # 최대 8개
            blk_ret = b.get("block_total_return_pct")
            blk_str = f"{blk_ret:+.1f}%" if blk_ret is not None else "  N/A"
            print(f"  {b['label']:>4}  {b['start']:>10}  {b['end']:>10}  {blk_str:>10}")

    # ── 월별 자본 (마지막 12개월) ──
    print(f"\n[ 월별 자본 (최근 24개월) ]")
    mc = monthly_capitals(portfolio_g2)
    mc_g0 = monthly_capitals(portfolio_g0)
    mc_k = kospi.copy()
    mc_k.index = pd.to_datetime(mc_k.index, format="%Y%m%d")
    mc_k_m = mc_k.resample("ME").last()

    print(f"  {'월':>8}  {'G2자본(만원)':>14}  {'초과수익':>10}  {'vs G0':>10}")
    for dt, val in mc.tail(24).items():
        dt_str = dt.strftime("%Y-%m")
        g0_val = mc_g0.get(dt, float("nan"))
        excess = (val / initial_cap - 1 - mc_k_m.get(dt, 0)) * 100
        vs_g0  = (val / g0_val - 1) * 100 if not np.isnan(g0_val) else float("nan")
        print(f"  {dt_str:>8}  {val/10000:>13.0f}  {excess:>+9.1f}%  {vs_g0:>+9.1f}%")

    print("\n" + "=" * 70)


def save_results(
    portfolio_g2:   pd.Series,  # v1.0
    portfolio_v11:  pd.Series,  # v1.1 (운용 규칙)
    portfolio_g0:   pd.Series,
    kospi:          pd.Series,
    periods_5d:     List[dict],
    periods_60d:    List[dict],  # v1.1 60d (게이트 없음)
    bear_blocks:    List[dict],
    initial_cap:    float,
    out_path:       Path,
):
    """결과를 JSON으로 저장."""
    mc     = monthly_capitals(portfolio_v11)
    mc_g2  = monthly_capitals(portfolio_g2)
    mc_g0  = monthly_capitals(portfolio_g0)

    out = {
        "meta": {
            "period": f"{START}~{END}",
            "initial_capital": initial_cap,
            "weight_5d": W5,
            "weight_60d": W60,
            "rule_version": "v1.1",
            "rule_5d": "G2 게이트 적용",
            "rule_60d": "게이트 없음 (무조건 60거래일 주기)",
            "warning": "2022~2024 IS 구간 포함 — 절대 수치 과신 금지",
        },
        "summary": {
            "total_return_v11_pct":   round((portfolio_v11.iloc[-1]/initial_cap - 1)*100, 2),
            "total_return_v10_g2_pct":round((portfolio_g2.iloc[-1] /initial_cap - 1)*100, 2),
            "total_return_g0_pct":    round((portfolio_g0.iloc[-1] /initial_cap - 1)*100, 2),
            "total_return_kospi_pct": round(float(kospi.iloc[-1])*100, 2) if len(kospi) > 0 else None,
            "mdd_v11_pct":  round(mdd(portfolio_v11)*100, 2),
            "mdd_v10_g2_pct":round(mdd(portfolio_g2)*100, 2),
            "mdd_g0_pct":   round(mdd(portfolio_g0)*100, 2),
        },
        "annual_returns_v11":  annual_returns(portfolio_v11,  initial_cap),
        "annual_returns_v10_g2": annual_returns(portfolio_g2, initial_cap),
        "annual_returns_g0":   annual_returns(portfolio_g0,   initial_cap),
        "monthly_capital_v11": {
            dt.strftime("%Y-%m"): round(float(v), 0)
            for dt, v in mc.items()
        },
        "monthly_capital_v10_g2": {
            dt.strftime("%Y-%m"): round(float(v), 0)
            for dt, v in mc_g2.items()
        },
        "monthly_capital_g0": {
            dt.strftime("%Y-%m"): round(float(v), 0)
            for dt, v in mc_g0.items()
        },
        "bear_blocks": bear_blocks,
        "periods_5d_count":  len(periods_5d),
        "periods_60d_count": len(periods_60d),
        "periods_5d_avg_return_pct":  round(float(np.mean([p["return_pct"] for p in periods_5d])), 4)
            if periods_5d else None,
        "periods_60d_avg_return_pct": round(float(np.mean([p["return_pct"] for p in periods_60d])), 4)
            if periods_60d else None,
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("결과 저장: %s", out_path)


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

def main():
    with sqlite3.connect(DB_PATH, timeout=60) as conn:
        # 1. G2 게이트 타임라인
        log.info("=== 1. G2 게이트 타임라인 ===")
        gate_df = build_gate_timeline(conn)
        bear_blocks = identify_bear_blocks(
            gate_df[(gate_df["date"] >= START) & (gate_df["date"] <= END)]
        )
        gate_map = dict(zip(gate_df["date"], gate_df["gate_blocked"]))

        # KOSPI 수익률
        kospi = kospi_returns(gate_df, conn)

        # 2. 모델 로드
        log.info("=== 2. 모델 로드 ===")
        model_5d, fcols_5d = load_5d_model()
        model_60d, fcols_60d = load_60d_model()

        # 3. 데이터 로드
        log.info("=== 3. 데이터 로드 ===")
        feat = load_data(conn, fcols_5d, fcols_60d)

        # 60d 팩터 추가
        log.info("60d PIT 팩터 계산 중...")
        feat = add_60d_factors(feat, conn, fcols_60d)

        # 4. 예측 점수
        log.info("=== 4. 예측 점수 계산 ===")
        feat = compute_scores(feat, model_5d, fcols_5d, model_60d, fcols_60d)

    # 5. 슬리브 시뮬레이션
    log.info("=== 5. 슬리브 시뮬레이션 ===")

    # v1.0 + v1.1 공유: 5d-G2 슬리브
    log.info("  5d 슬리브 (G2게이트)...")
    cap_5d_g2, periods_5d_g2 = simulate_sleeve(
        feat, gate_map, "score_5d", "ret_adj_5d", 5,
        INITIAL_CAPITAL * W5, use_gate=True,
    )

    # v1.0 전용: 60d-G2 슬리브
    log.info("  60d 슬리브 (G2게이트, v1.0용)...")
    cap_60d_g2, periods_60d_g2 = simulate_sleeve(
        feat, gate_map, "score_60d", "ret_adj_60d", 60,
        INITIAL_CAPITAL * W60, use_gate=True,
    )

    # G0 (게이트 없음): 5d + 60d
    log.info("  5d 슬리브 (G0 비교)...")
    cap_5d_g0, periods_5d_g0 = simulate_sleeve(
        feat, gate_map, "score_5d", "ret_adj_5d", 5,
        INITIAL_CAPITAL * W5, use_gate=False,
    )
    log.info("  60d 슬리브 (게이트없음, v1.1+G0 공용)...")
    cap_60d_g0, periods_60d_g0 = simulate_sleeve(
        feat, gate_map, "score_60d", "ret_adj_60d", 60,
        INITIAL_CAPITAL * W60, use_gate=False,
    )

    # 6. 포트폴리오 합산
    log.info("=== 6. 포트폴리오 합산 ===")
    portfolio_g2  = cap_5d_g2.add(cap_60d_g2, fill_value=0)   # v1.0: 5d-G2 + 60d-G2
    portfolio_v11 = cap_5d_g2.add(cap_60d_g0, fill_value=0)   # v1.1: 5d-G2 + 60d-G0
    portfolio_g0  = cap_5d_g0.add(cap_60d_g0, fill_value=0)   # G0:   5d-G0 + 60d-G0

    log.info("  v1.0 포트폴리오 최종: %.1f%%", (portfolio_g2.iloc[-1]/INITIAL_CAPITAL - 1)*100)
    log.info("  v1.1 포트폴리오 최종: %.1f%%", (portfolio_v11.iloc[-1]/INITIAL_CAPITAL - 1)*100)
    log.info("  G0  포트폴리오 최종: %.1f%%", (portfolio_g0.iloc[-1]/INITIAL_CAPITAL - 1)*100)

    # 7. 차단 구간 성과 (v1.0 기준, 역사적 참고용)
    bear_perf_g2 = bear_block_performance(portfolio_g2, periods_5d_g2 + periods_60d_g2, bear_blocks)

    # 8. 출력
    log.info("=== 7. 결과 출력 ===")
    print_results(
        portfolio_g2, portfolio_v11, portfolio_g0, kospi,
        periods_5d_g2, periods_60d_g0,
        bear_perf_g2,
        INITIAL_CAPITAL,
    )

    # 9. JSON 저장
    out_path = ROOT / "ml" / "integrated_backtest_results.json"
    save_results(
        portfolio_g2, portfolio_v11, portfolio_g0, kospi,
        periods_5d_g2, periods_60d_g0,
        bear_perf_g2,
        INITIAL_CAPITAL, out_path,
    )
    log.info("완료!")


if __name__ == "__main__":
    main()
