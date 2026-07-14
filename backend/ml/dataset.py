"""
피처 + 라벨 데이터셋 빌드, 시계열 기준 train/val 분리

필터 (실전 적용 가능 유니버스):
  - ETF/레버리지/인버스/파생 상품 제외 (종목명 기준)
  - 20일 평균 거래대금 ≥ 10억원
  - 시가총액 ≥ 500억원

라벨 개선 (target_1d):
  - 종가 +3% 이상 AND 다음날 거래대금 ≥ 5억원 (실제 매도 가능한 상승만 양성)

신규 피처:
  - vol_krw_5d  : 5일 평균 거래대금 (log 스케일)
  - vol_krw_20d : 20일 평균 거래대금 (log 스케일)
  - log_market_cap : 시가총액 로그값
"""

import logging
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from labels import compute_labels

DB_PATH = Path(__file__).parent.parent / "data" / "stocks.db"
logger = logging.getLogger(__name__)

# ── 유니버스 필터 기준 ────────────────────────────────────────
MIN_VOL_KRW_20D = 1_000_000_000   # 20일 평균 거래대금 10억원 이상
MIN_MARKET_CAP  = 500              # 시가총액 500억원 (prices.market_cap 단위: 억원)
MIN_FWD_VOL_KRW = 500_000_000     # 라벨 조건: 다음날 거래대금 5억원 이상 (매도 가능)

# ETF/레버리지/파생상품 제외 패턴 (종목명 기준, 비-캡처 그룹 사용)
_ETF_PATTERN = (
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s'
)

# 주의: market_cap은 현재 DB에 수집되지 않아(모두 NULL) 피처에서 제외
FEATURE_COLS: List[str] = [
    "ret_1d", "ret_5d", "ret_20d", "ret_60d",
    "ma5_dev", "ma20_dev", "ma60_dev", "ma120_dev",
    "rsi_7", "rsi_14",
    "macd", "macd_signal", "macd_hist",
    "bb_pct", "bb_width",
    "vol_ratio_5d", "vol_ratio_20d", "vol_surge",
    "vol_krw_5d", "vol_krw_20d",
    "atr_14", "atr_pct",
    "gap_pct", "body_ratio", "upper_shadow", "lower_shadow",
    "up_streak", "down_streak",
    "rel_market_1d", "rel_market_5d", "rel_market_20d",
    "rel_sector_5d", "rel_sector_20d",
    "foreign_rate", "foreign_1d_chg", "foreign_5d_chg", "foreign_trend",
    "per", "pbr",
    "kospi_ma200_ratio", "kospi_volatility_20d", "kosdaq_kospi_ratio", "market_breadth",
    # ── 시계열 lag 피처 (모멘텀 흐름) ──────────────────────────
    "ret_lag_1", "ret_lag_2", "ret_lag_3", "ret_lag_5", "ret_lag_10",
    "vol_ratio_lag_1", "vol_ratio_lag_3", "vol_ratio_lag_5",
    "up_days_5", "up_days_10",
    "volatility_5", "volatility_20",
    "price_position_20", "price_position_60",
    # ── KIS 수급 신호 피처 (2026-07-02 추가 → 기각됨, 아래 _DEAD_FEATURE_COLS 참고) ──
    "frgn_norm_cum10", "frgn_streak3",
]

# dead features — FEATURE_COLS_REDUCED에서 제외됨:
#   8개: SHAP 0% 확인 (2026-06-27 진단, body_ratio/vol_surge/up_streak/down_streak/
#        ret_lag_3/ret_lag_5/ret_lag_10/up_days_10 — 등가/상수/중복으로 기여 없음)
#   4개: flows.foreign_net 기반 보유비율 파생 피처 — 학습기간(2022~) 대비 actual
#        커버리지 2.7%로 사실상 전부 NULL, SHAP 0.000% 확인됨 (2026-06-27)
#   2개: KIS investor_trading_kis_detail 기반 수급 신호 (2026-07-02 기각됨)
#        D안 재학습 결과 frgn_norm_cum10 SHAP 0.14%, frgn_streak3 SHAP 0.00%.
#        변동성 피처 제거 후에도 SHAP 0% → 순수중복 아님, 독립 예측력 없음.
#        confounding proxy: 외국인은 '이미 좋은 종목'을 사므로 단변량엔 보이나
#        atr_pct(18.45%), ma120_dev(6.27%) 등이 직접 포착해 frgn은 잉여.
#        features 테이블 컬럼과 daily 증분계산은 유지(재계산 비용), 학습 미사용.
_DEAD_FEATURE_COLS = {
    "body_ratio", "vol_surge", "up_streak", "down_streak",
    "ret_lag_3", "ret_lag_5", "ret_lag_10", "up_days_10",
    "foreign_rate", "foreign_1d_chg", "foreign_5d_chg", "foreign_trend",
    "frgn_norm_cum10", "frgn_streak3",  # 기각됨 2026-07-02
}
FEATURE_COLS_REDUCED: List[str] = [c for c in FEATURE_COLS if c not in _DEAD_FEATURE_COLS]


def _etf_symbols(conn: sqlite3.Connection) -> set:
    """ETF/레버리지 상품으로 판단되는 심볼 집합 반환."""
    stocks = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)
    mask = stocks["name"].str.contains(_ETF_PATTERN, na=False, case=False, regex=True)
    excluded = set(stocks.loc[mask, "symbol"])
    logger.info("ETF/파생 제외 종목 수: %d / 전체 %d", len(excluded), len(stocks))
    return excluded


def _compute_price_features(prices: pd.DataFrame) -> pd.DataFrame:
    """
    prices DataFrame에서 신규 피처 + 필터용 컬럼 계산.
    입력 필수 컬럼: symbol, date, close, volume, market_cap
    """
    prices = prices.copy()
    prices["close"]      = prices["close"].astype(float)
    prices["volume"]     = prices["volume"].fillna(0).astype(float)
    prices["market_cap"] = pd.to_numeric(prices["market_cap"], errors="coerce").fillna(0)
    prices["volume_krw"] = prices["close"] * prices["volume"]

    prices = prices.sort_values(["symbol", "date"])

    # 1일 전진 수익률·거래대금 (target_1d 라벨/백테스트)
    prices["ret_fwd_1d"]  = prices.groupby("symbol")["close"].transform(
        lambda c: c.shift(-1) / c - 1
    )
    prices["vol_fwd_krw"] = prices.groupby("symbol")["volume_krw"].shift(-1)

    # 5일 전진 수익률·거래대금·최고·최저 (target_5d 라벨/백테스트)
    prices["ret_fwd_5d"]     = prices.groupby("symbol")["close"].transform(
        lambda c: c.shift(-5) / c - 1
    )
    prices["vol_fwd_5d_krw"] = prices.groupby("symbol")["volume_krw"].shift(-5)

    def _fwd_range(c: pd.Series, n: int, func):
        return pd.concat([c.shift(-i) for i in range(1, n + 1)], axis=1).pipe(func, axis=1) / c - 1

    prices["fwd_max_5d"] = prices.groupby("symbol")["close"].transform(
        lambda c: _fwd_range(c, 5, pd.DataFrame.max)
    )
    prices["fwd_min_5d"] = prices.groupby("symbol")["close"].transform(
        lambda c: _fwd_range(c, 5, pd.DataFrame.min)
    )

    # 신규 피처 (로그 스케일 — 종목 간 거래대금 비교 가능)
    prices["vol_krw_5d"] = prices.groupby("symbol")["volume_krw"].transform(
        lambda x: np.log1p(x.rolling(5, min_periods=1).mean())
    )
    prices["vol_krw_20d"] = prices.groupby("symbol")["volume_krw"].transform(
        lambda x: np.log1p(x.rolling(20, min_periods=5).mean())
    )

    # 필터용 원본값 (피처로 사용 안 함)
    prices["vol_krw_20d_raw"] = prices.groupby("symbol")["volume_krw"].transform(
        lambda x: x.rolling(20, min_periods=10).mean()
    )

    # ── 시계열 lag 피처 ─────────────────────────────────────────

    # 수익률 lag: N일 전 일간 수익률
    for _lag in [1, 2, 3, 5, 10]:
        prices[f"ret_lag_{_lag}"] = prices.groupby("symbol")["close"].transform(
            lambda c, lag=_lag: c.pct_change(1).shift(lag)
        )

    # 거래량 비율 lag: N일 전 vol / 20일 평균
    _vol_ma20 = prices.groupby("symbol")["volume"].transform(
        lambda v: v.rolling(20, min_periods=1).mean()
    )
    prices["_vol_ratio_raw"] = prices["volume"] / _vol_ma20.replace(0, np.nan)
    for _lag in [1, 3, 5]:
        prices[f"vol_ratio_lag_{_lag}"] = prices.groupby("symbol")["_vol_ratio_raw"].transform(
            lambda v, lag=_lag: v.shift(lag)
        )
    prices.drop(columns=["_vol_ratio_raw"], inplace=True)

    # 추세 일관성: 최근 N일 중 상승일 수
    prices["up_days_5"] = prices.groupby("symbol")["close"].transform(
        lambda c: (c > c.shift(1)).astype(float).rolling(5, min_periods=1).sum()
    )
    prices["up_days_10"] = prices.groupby("symbol")["close"].transform(
        lambda c: (c > c.shift(1)).astype(float).rolling(10, min_periods=1).sum()
    )

    # 변동성: 최근 N일 일간 수익률 표준편차
    prices["volatility_5"] = prices.groupby("symbol")["close"].transform(
        lambda c: c.pct_change(1).rolling(5, min_periods=2).std()
    )
    prices["volatility_20"] = prices.groupby("symbol")["close"].transform(
        lambda c: c.pct_change(1).rolling(20, min_periods=5).std()
    )

    # 가격 위치: (현재가 - N일 최저) / (N일 최고 - N일 최저)
    for _n in [20, 60]:
        _lo = prices.groupby("symbol")["close"].transform(
            lambda c, n=_n: c.rolling(n, min_periods=1).min()
        )
        _hi = prices.groupby("symbol")["close"].transform(
            lambda c, n=_n: c.rolling(n, min_periods=1).max()
        )
        prices[f"price_position_{_n}"] = (prices["close"] - _lo) / (_hi - _lo).replace(0, np.nan)

    return prices


def _apply_legacy_per_pbr(df: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
    """[2026-06-27, A/B/C 비교 전용] look-ahead bias가 있던 구버전 per/pbr을 재현.

    db_features.load_fundamentals_latest()와 동일한 버그(date 필터 없이 "가장 최근"
    PER/PBR을 종목당 1개 값으로 모든 과거 날짜에 broadcast)를 fundamentals 테이블에서
    재현해 df["per"]/df["pbr"]을 덮어쓴다. features 테이블 자체는 이미 point-in-time으로
    수정됐으므로(fix_per_pbr_pit.py), 모델 A(현재까지의 운영/오염된 버전)를 재구성하려면
    이 함수로 옛 값을 다시 만들어내야 한다.
    """
    legacy = pd.read_sql_query(
        "SELECT symbol, per, pbr FROM fundamentals "
        "WHERE (symbol, date) IN (SELECT symbol, MAX(date) FROM fundamentals GROUP BY symbol)",
        conn,
    ).rename(columns={"per": "_legacy_per", "pbr": "_legacy_pbr"})
    df = df.merge(legacy, on="symbol", how="left")
    df["per"] = df["_legacy_per"]
    df["pbr"] = df["_legacy_pbr"]
    return df.drop(columns=["_legacy_per", "_legacy_pbr"])


def build_dataset(
    target_col: str = "target_1d",
    val_days: int = 252,
    feature_cols: Optional[List[str]] = None,
    per_pbr_mode: str = "pit",
) -> Tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.Series, pd.DataFrame]:
    """
    features 테이블 + 라벨을 합쳐 시계열 기준 train/val 분리.

    val_days: 마지막 N 거래일을 검증 세트로 (기본 252 ≈ 1년)
    feature_cols: 모델에 쓸 피처 목록 (미지정시 FEATURE_COLS)
    per_pbr_mode: "pit"(기본, features 테이블의 point-in-time 값 그대로 사용) |
                  "legacy_broadcast"(A/B/C 비교용 — look-ahead bias 있던 구버전 재현)

    반환: X_train, y_train, X_val, y_val, val_meta
      val_meta — columns: symbol, date, ret_fwd_1d, volume_krw (백테스트용)
    """
    feature_cols = feature_cols or FEATURE_COLS
    with sqlite3.connect(DB_PATH) as conn:
        features = pd.read_sql_query(
            "SELECT * FROM features ORDER BY date, symbol", conn
        )
        labels_df = compute_labels(conn)
        prices_raw = pd.read_sql_query(
            "SELECT symbol, date, close, volume, market_cap FROM prices ORDER BY symbol, date",
            conn,
        )
        etf_syms = _etf_symbols(conn)
        if per_pbr_mode == "legacy_broadcast":
            features = _apply_legacy_per_pbr(features, conn)

    logger.info("가격 피처 계산 중 (전종목 rolling 연산)...")
    prices = _compute_price_features(prices_raw)

    # ── 병합 ──────────────────────────────────────────────────
    price_cols = [
        "symbol", "date",
        "ret_fwd_1d", "volume_krw", "vol_fwd_krw",
        "vol_krw_5d", "vol_krw_20d",
        "vol_krw_20d_raw",
        "ret_fwd_5d", "vol_fwd_5d_krw", "fwd_max_5d", "fwd_min_5d",
        # lag 피처 — features 테이블에도 동일 컬럼이 있어 중복 방지를 위해 여기서는 제외
        # (pipeline.py가 features 테이블에 이미 채워 넣음)
    ]
    df = (
        features
        .merge(
            labels_df[["symbol", "date", "target_1d", "target_5d"]],
            on=["symbol", "date"],
        )
        .merge(prices[price_cols], on=["symbol", "date"], how="left")
    )

    # ── ETF/파생 제외 ─────────────────────────────────────────
    before_etf = len(df)
    df = df[~df["symbol"].isin(etf_syms)]
    logger.info("ETF 필터 후: %d → %d행", before_etf, len(df))

    # ── 유동성 필터: 20일 평균 거래대금 ≥ 10억원 (시점별 적용) ──
    # 주의: market_cap 데이터 미수집으로 시총 필터 미적용
    before_liq = len(df)
    df = df[df["vol_krw_20d_raw"].fillna(0) >= MIN_VOL_KRW_20D]
    logger.info(
        "유동성 필터 후: %d → %d행 (제거 %d행, %.1f%%)",
        before_liq, len(df), before_liq - len(df),
        100 * (before_liq - len(df)) / max(before_liq, 1),
    )

    # ── 개선된 라벨 ─────────────────────────────────────────────
    if target_col == "target_1d":
        # +3% 상승 AND 다음날 거래대금 ≥ 5억 (실제 매도 가능)
        valid_mask = df["ret_fwd_1d"].notna()
        liquid_label = (
            (df["ret_fwd_1d"] >= 0.03) &
            (df["vol_fwd_krw"].fillna(0) >= MIN_FWD_VOL_KRW)
        ).astype("Int8").where(valid_mask)
        df["target_1d"] = liquid_label

    if target_col == "target_5d":
        # 5일 내 최고 +10% 또는 5일 종가 +5% AND 5일 후 거래대금 ≥ 5억
        valid_mask = df["fwd_max_5d"].notna()
        liquid_label_5d = (
            ((df["fwd_max_5d"] >= 0.10) | (df["ret_fwd_5d"] >= 0.05)) &
            (df["vol_fwd_5d_krw"].fillna(0) >= MIN_FWD_VOL_KRW)
        ).astype("Int8").where(valid_mask)
        df["target_5d"] = liquid_label_5d

    if target_col == "target_5d_close":
        # 5일 후 종가 +5% (실거래 기준 정직한 라벨) AND 5일 후 거래대금 ≥ 5억
        valid_mask = df["ret_fwd_5d"].notna()
        df["target_5d_close"] = (
            (df["ret_fwd_5d"] >= 0.05) &
            (df["vol_fwd_5d_krw"].fillna(0) >= MIN_FWD_VOL_KRW)
        ).astype("Int8").where(valid_mask)

    df = df.dropna(subset=[target_col])

    n_sym  = df["symbol"].nunique()
    n_date = df["date"].nunique()
    pos    = float(df[target_col].mean())
    logger.info(
        "최종 데이터셋: %d종목 × %d날짜 = %d행 | positive=%.1f%%",
        n_sym, n_date, len(df), pos * 100,
    )

    # ── 날짜 기준 분리 (미래 데이터 누수 방지) ────────────────
    unique_dates = sorted(df["date"].unique())
    cutoff_idx = max(0, len(unique_dates) - val_days)
    cutoff = unique_dates[cutoff_idx]

    train_df = df[df["date"] < cutoff]
    val_df   = df[df["date"] >= cutoff]

    X_train  = train_df[feature_cols].astype(float)
    y_train  = train_df[target_col].astype(int)
    X_val    = val_df[feature_cols].astype(float)
    y_val    = val_df[target_col].astype(int)
    val_meta = val_df[
        ["symbol", "date", "ret_fwd_1d", "volume_krw",
         "ret_fwd_5d", "vol_fwd_5d_krw", "fwd_max_5d", "fwd_min_5d"]
    ].reset_index(drop=True)
    train_meta = train_df[["symbol", "date"]].reset_index(drop=True)

    return X_train, y_train, X_val, y_val, val_meta, train_meta
