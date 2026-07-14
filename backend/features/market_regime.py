"""
시장 국면 피처 계산
- kospi_ma200_ratio    : KOSPI 종가 / 200일 이동평균
- kospi_volatility_20d : KOSPI 20일 수익률 표준편차
- kosdaq_kospi_ratio   : KOSDAQ 5일 수익률 - KOSPI 5일 수익률
- market_breadth       : 전일 대비 상승 종목 비율

모든 피처는 날짜(date) 기준으로 계산되므로 종목 루프 밖에서 한 번만 계산한다.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from db_features import load_index_closes

logger = logging.getLogger(__name__)


def load_kospi_regime_labels(start: str = "2000-01-01") -> pd.Series:
    """
    KOSPI 60일/120일 이동평균 교차로 시장 국면을 bull/bear로 분류.
    ma60 > ma120 → bull, 그 외 → bear. (train_regime.py의 국면별 분리 학습에서 사용)

    반환: index=date(str, YYYYMMDD), value='bull'|'bear'
    """
    idx_closes = load_index_closes(start=start)
    kospi = idx_closes.get("1001", pd.Series(dtype=float))
    if isinstance(kospi, pd.DataFrame):
        kospi = kospi.squeeze()
    if kospi.empty:
        raise RuntimeError("market_index에 KOSPI(1001) 데이터 없음 — 국면 판별 불가")

    kospi = kospi.astype(float).sort_index()
    ma60  = kospi.rolling(60,  min_periods=30).mean()
    ma120 = kospi.rolling(120, min_periods=60).mean()
    regime = (ma60 > ma120).map({True: "bull", False: "bear"}).dropna()
    regime.name = "regime"

    logger.info(
        "KOSPI 국면(60d/120d MA 교차): bull=%d일, bear=%d일 (전체 %d일)",
        (regime == "bull").sum(), (regime == "bear").sum(), len(regime),
    )
    return regime


def build_market_regime_features(
    all_closes: pd.DataFrame,
    start: Optional[str] = None,
) -> pd.DataFrame:
    """
    시장 국면 피처를 날짜별로 계산해 반환.

    매개변수:
        all_closes : load_all_closes() 반환값 (date × symbol 피벗)
                     market_breadth 계산에 사용
        start      : market_index 로드 시작일 (None이면 전체)

    반환: DataFrame  index=date  columns=[kospi_ma200_ratio, ...]
    """
    # ── 1. 지수 종가 로드 ─────────────────────────────────────
    idx_closes = load_index_closes(start=start)

    kospi  = idx_closes.get("1001", pd.Series(dtype=float))
    kosdaq = idx_closes.get("2001", pd.Series(dtype=float))

    if isinstance(kospi, pd.DataFrame):
        kospi = kospi.squeeze()
    if isinstance(kosdaq, pd.DataFrame):
        kosdaq = kosdaq.squeeze()

    if kospi.empty:
        logger.warning("market_index에 KOSPI(1001) 데이터 없음 — 시장 국면 피처 모두 NaN")
        return pd.DataFrame()

    # ── 2. kospi_ma200_ratio ──────────────────────────────────
    kospi_float = kospi.astype(float)
    ma200 = kospi_float.rolling(200, min_periods=150).mean()
    kospi_ma200_ratio = kospi_float / ma200

    # ── 3. kospi_volatility_20d ───────────────────────────────
    kospi_ret = kospi_float.pct_change()
    kospi_volatility_20d = kospi_ret.rolling(20, min_periods=10).std()

    # ── 4. kosdaq_kospi_ratio ─────────────────────────────────
    if not kosdaq.empty:
        kosdaq_float = kosdaq.astype(float)
        kospi_ret_5d  = kospi_float.pct_change(5)
        kosdaq_ret_5d = kosdaq_float.pct_change(5)
        # 날짜 정렬 맞추기
        common = kospi_ret_5d.index.intersection(kosdaq_ret_5d.index)
        kosdaq_kospi_ratio = (kosdaq_ret_5d.reindex(common) -
                              kospi_ret_5d.reindex(common))
    else:
        logger.warning("market_index에 KOSDAQ(2001) 데이터 없음")
        kosdaq_kospi_ratio = pd.Series(dtype=float, name="kosdaq_kospi_ratio")

    # ── 5. market_breadth ─────────────────────────────────────
    if not all_closes.empty:
        daily_ret = all_closes.astype(float).pct_change(1)
        up = (daily_ret > 0).sum(axis=1)
        total = daily_ret.notna().sum(axis=1)
        market_breadth = (up / total.replace(0, np.nan)).rename("market_breadth")
    else:
        logger.warning("all_closes 없음 — market_breadth NaN")
        market_breadth = pd.Series(dtype=float, name="market_breadth")

    # ── 6. 합치기 ─────────────────────────────────────────────
    regime = pd.DataFrame({
        "kospi_ma200_ratio":    kospi_ma200_ratio,
        "kospi_volatility_20d": kospi_volatility_20d,
        "kosdaq_kospi_ratio":   kosdaq_kospi_ratio.reindex(kospi_ma200_ratio.index),
        "market_breadth":       market_breadth.reindex(kospi_ma200_ratio.index),
    })

    logger.info(
        "시장 국면 피처 계산 완료: %d날짜 | NaN비율 kospi_ma200=%.1f%% vol=%.1f%% "
        "kq_kp=%.1f%% breadth=%.1f%%",
        len(regime),
        regime["kospi_ma200_ratio"].isna().mean() * 100,
        regime["kospi_volatility_20d"].isna().mean() * 100,
        regime["kosdaq_kospi_ratio"].isna().mean() * 100,
        regime["market_breadth"].isna().mean() * 100,
    )
    return regime
