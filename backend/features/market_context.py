"""
시장 컨텍스트 피처 계산
- KOSPI / KOSDAQ 일별 평균 수익률 (market return)
- 종목별 상대 강도 = 종목 수익률 - 시장 평균 수익률
- 섹터(시장 구분) 대비 상대 강도
"""

import logging
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def build_market_returns(all_closes: pd.DataFrame, symbol_market_map: Dict[str, str]) -> Dict[str, pd.Series]:
    """
    전종목 종가 피벗(date × symbol)으로부터
    KOSPI / KOSDAQ 시장별 일별 평균 수익률 계산

    반환: {"KOSPI": Series(date→ret), "KOSDAQ": Series(date→ret)}
    """
    if all_closes.empty:
        return {"KOSPI": pd.Series(dtype=float), "KOSDAQ": pd.Series(dtype=float)}

    # 일별 수익률 (date × symbol)
    daily_ret = all_closes.pct_change(1)

    market_returns: Dict[str, pd.Series] = {}
    for market_name in ("KOSPI", "KOSDAQ"):
        symbols_in_market = [
            s for s, m in symbol_market_map.items()
            if m == market_name and s in daily_ret.columns
        ]
        if not symbols_in_market:
            market_returns[market_name] = pd.Series(dtype=float)
            continue

        # 각 날짜별 중앙값 사용 (이상치 종목에 덜 민감)
        market_ret = daily_ret[symbols_in_market].median(axis=1)
        market_returns[market_name] = market_ret

    return market_returns


MIN_SECTOR_MEMBERS = 3  # 이 수 미만으로 가격 데이터가 있는 업종은 시장 평균으로 폴백


def build_sector_returns(
    all_closes: pd.DataFrame,
    symbol_market_map: Dict[str, str],
    market_rets: Optional[Dict[str, pd.Series]] = None,
    symbol_sector_map: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, pd.Series]:
    """
    업종(섹터) 코드(stocks.sector, DART induty_code 기반)별 일별 평균 수익률 계산.

    symbol_sector_map이 없거나 해당 종목의 sector가 NULL/소속 종목수<MIN_SECTOR_MEMBERS면
    시장(KOSPI/KOSDAQ) 평균으로 폴백 (이전 placeholder 동작 — sector_collector.py로 아직
    업종코드를 못 가져온 종목/세션 한도로 일부만 수집된 경우 안전하게 동작).

    반환: {symbol: sector_return_series}
    """
    if market_rets is None:
        market_rets = build_market_returns(all_closes, symbol_market_map)

    if not symbol_sector_map:
        # 업종코드 자체가 없으면 전부 시장 평균으로 폴백 (이전 동작과 동일)
        return {sym: market_rets.get(market, pd.Series(dtype=float))
                for sym, market in symbol_market_map.items()}

    daily_ret = all_closes.pct_change(1)

    # 업종코드별 그룹 (가격 데이터가 있는 종목만)
    sector_groups: Dict[str, list] = {}
    for sym, sector in symbol_sector_map.items():
        if sector and sym in daily_ret.columns:
            sector_groups.setdefault(sector, []).append(sym)

    sector_rets: Dict[str, pd.Series] = {}
    for sector, syms in sector_groups.items():
        if len(syms) < MIN_SECTOR_MEMBERS:
            continue
        sector_rets[sector] = daily_ret[syms].median(axis=1)

    sector_ret_map: Dict[str, pd.Series] = {}
    for sym, market in symbol_market_map.items():
        sector = symbol_sector_map.get(sym)
        if sector and sector in sector_rets:
            sector_ret_map[sym] = sector_rets[sector]
        else:
            # 업종 미수집/소수 업종 종목 → 시장 평균 폴백
            sector_ret_map[sym] = market_rets.get(market, pd.Series(dtype=float))

    return sector_ret_map


def calc_relative_strength(
    close: pd.Series,
    market_ret: pd.Series,
    sector_ret: pd.Series,
    windows: tuple = (1, 5, 20),
) -> pd.DataFrame:
    """
    종목 수익률 - 시장/섹터 평균 수익률

    입력:
        close      : 단일 종목 종가 Series (index=date)
        market_ret : 시장 일별 수익률 Series (index=date)
        sector_ret : 섹터 일별 수익률 Series (index=date)
        windows    : 계산할 윈도우 (일 단위)

    반환: DataFrame with rel_market_Nd, rel_sector_Nd 컬럼
    """
    stock_ret_1d = close.pct_change(1)

    # 날짜 정렬 맞추기
    market_aligned = market_ret.reindex(close.index)
    sector_aligned = sector_ret.reindex(close.index)

    def _cumret(ret: pd.Series, w: int) -> pd.Series:
        return np.expm1(np.log1p(ret).rolling(w).sum())

    result: Dict[str, pd.Series] = {}

    # 1일 상대 강도
    result["rel_market_1d"] = stock_ret_1d - market_aligned
    result["rel_sector_1d"] = stock_ret_1d - sector_aligned

    # 5일 / 20일 롤링 누적 상대 강도
    for w in (5, 20):
        stock_cumret  = close.pct_change(w)
        market_cumret = _cumret(market_aligned, w)
        sector_cumret = _cumret(sector_aligned, w)

        result[f"rel_market_{w}d"] = stock_cumret - market_cumret
        result[f"rel_sector_{w}d"] = stock_cumret - sector_cumret

    return pd.DataFrame(result, index=close.index)
