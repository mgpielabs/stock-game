"""
2022년 features 백필 스크립트

- prices: 2021-05-25부터 있음 (MA120 워밍업용)
- 계산 대상: 2022-01-01 ~ 2022-12-31
- flows/fundamentals: 2022 데이터 없음 → NaN (CatBoost가 처리)
- market_index: 2022 전체 확보 완료
"""

import logging
import sys
from pathlib import Path
from typing import Optional, List, Dict

import numpy as np
import pandas as pd
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "data"))

from technical import compute_all_technical
from market_context import build_market_returns, build_sector_returns, calc_relative_strength
from flows import calc_flow_features
from market_regime import build_market_regime_features
from db_features import (
    init_features_table,
    upsert_features,
    load_prices_for_symbol,
    load_all_closes,
    load_flows,
    load_fundamentals_latest,
    load_all_symbols_with_market,
    load_index_closes,
)

# ⚠️ 재실행 금지 — look-ahead bias 버그(load_fundamentals_latest)가 포함됨.
# 재실행하면 fix_per_pbr_pit.py로 수정된 features.per/pbr PIT 값을 덮어씁니다.
# 수정이 필요하면 load_per_pbr_pit()으로 교체한 뒤 이 가드를 제거하세요.
# --force 플래그를 추가하면 우회 가능합니다.
if "--force" not in sys.argv:
    print(
        "[backfill_2022] 이 스크립트는 look-ahead bias(load_fundamentals_latest)를 포함합니다.\n"
        "  재실행 시 features.per/pbr PIT 수정(2026-06-27)을 파괴합니다.\n"
        "  재실행하려면 --force 플래그를 추가하세요.",
        file=sys.stderr,
    )
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("backfill_2022.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

PRICE_START      = "20210525"   # 워밍업 포함 (MA120 = 120거래일 필요)
TARGET_START     = "20220101"   # 백필 대상 시작
TARGET_END       = "20221231"   # 백필 대상 종료
MIN_ROWS         = 130
MAX_NAN_RATIO    = 0.5


def _clean_row(row: Dict) -> Dict:
    cleaned = {}
    for k, v in row.items():
        if hasattr(v, "item"):
            try:
                v = v.item()
            except (ValueError, OverflowError):
                cleaned[k] = None
                continue
        try:
            cleaned[k] = None if pd.isna(v) else v
        except TypeError:
            cleaned[k] = v
    return cleaned


def process_symbol_2022(
    sym_info: Dict,
    market_rets: Dict,
    sector_ret_map: Dict,
    regime_df: Optional[pd.DataFrame],
) -> int:
    """단일 종목의 2022년 피처를 계산해 upsert. 반환: 저장 행 수"""
    symbol = sym_info["symbol"]
    market = sym_info["market"]

    prices_df = load_prices_for_symbol(symbol, start=PRICE_START)
    if prices_df.empty or len(prices_df) < MIN_ROWS:
        return 0

    # 기술적 지표 계산 (전체 기간 필요 — MA120 워밍업)
    tech_df = compute_all_technical(prices_df)
    if tech_df.empty:
        return 0

    # 2022 이후만 필터링
    tech_2022 = tech_df[
        (tech_df.index >= TARGET_START) & (tech_df.index <= TARGET_END)
    ]
    if tech_2022.empty:
        return 0

    prices_sorted = prices_df.sort_values("date").set_index("date")
    close = prices_sorted["close"].astype(float)

    market_ret_series = market_rets.get(market, pd.Series(dtype=float))
    sector_ret_series = sector_ret_map.get(symbol, pd.Series(dtype=float))
    rel_df = calc_relative_strength(close, market_ret_series, sector_ret_series)

    # flows는 2022 데이터 없음 → NaN 허용
    flows_df = load_flows(symbol, start=PRICE_START)
    flow_df  = calc_flow_features(tech_df.index, flows_df)

    fund = load_fundamentals_latest(symbol)

    # 합치기
    combined = pd.concat([tech_df, rel_df, flow_df], axis=1)

    if regime_df is not None and not regime_df.empty:
        combined = combined.join(regime_df, how="left")

    combined = combined.drop(columns=["rel_sector_1d"], errors="ignore")
    combined["per"] = fund.get("per")
    combined["pbr"] = fund.get("pbr")

    # NaN 과다 행 제거
    feature_cols = [c for c in combined.columns if c != "target"]
    nan_ratio = combined[feature_cols].isna().mean(axis=1)
    combined = combined[nan_ratio < MAX_NAN_RATIO]

    # 2022년만 추출
    combined_2022 = combined[
        (combined.index >= TARGET_START) & (combined.index <= TARGET_END)
    ]
    if combined_2022.empty:
        return 0

    records = []
    for date_str, row in combined_2022.iterrows():
        rec = {"symbol": symbol, "date": date_str}
        rec.update(row.to_dict())
        records.append(_clean_row(rec))

    if records:
        upsert_features(records)

    return len(records)


def run_backfill(symbols: Optional[List[str]] = None):
    init_features_table()

    logger.info("=" * 60)
    logger.info("2022 features 백필 | 가격 시작: %s | 대상: %s ~ %s",
                PRICE_START, TARGET_START, TARGET_END)
    logger.info("=" * 60)

    logger.info("[1/3] 전종목 종가 피벗 로드 (시작: %s)...", PRICE_START)
    all_closes = load_all_closes(start=PRICE_START)
    sym_market_list = load_all_symbols_with_market()
    symbol_market_map = {s["symbol"]: s["market"] for s in sym_market_list}

    logger.info("[2/3] 시장/섹터 수익률 + 국면 피처 계산...")
    market_rets    = build_market_returns(all_closes, symbol_market_map)
    sector_ret_map = build_sector_returns(all_closes, symbol_market_map, market_rets=market_rets)
    regime_df      = build_market_regime_features(all_closes, start=PRICE_START)

    if regime_df.empty:
        logger.warning("시장 국면 피처 비어있음 — market_index 확인 필요")
    else:
        r2022 = regime_df[(regime_df.index >= TARGET_START) & (regime_df.index <= TARGET_END)]
        logger.info("  2022 regime 날짜 수: %d개 (첫날: %s, 마지막: %s)",
                    len(r2022), r2022.index.min() if not r2022.empty else '-',
                    r2022.index.max() if not r2022.empty else '-')

    target_list = ([s for s in sym_market_list if s["symbol"] in symbols]
                   if symbols else sym_market_list)

    logger.info("[3/3] 종목별 2022 피처 계산: %d개", len(target_list))

    total_saved = 0
    error_count = 0

    for sym_info in tqdm(target_list, desc="2022 backfill", unit="종목"):
        try:
            n = process_symbol_2022(sym_info, market_rets, sector_ret_map, regime_df)
            total_saved += n
        except Exception as exc:
            error_count += 1
            logger.error("오류 [%s]: %s", sym_info["symbol"], exc, exc_info=False)

    logger.info("=" * 60)
    logger.info("백필 완료 | upsert: %d건 | 오류: %d건", total_saved, error_count)
    logger.info("=" * 60)
    return total_saved


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="2022년 features 백필")
    parser.add_argument("--symbols", type=str, help="특정 종목만 (쉼표구분, 예: 005930,000660)")
    args = parser.parse_args()
    syms = args.symbols.split(",") if args.symbols else None
    run_backfill(symbols=syms)
