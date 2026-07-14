"""
피처 엔지니어링 파이프라인

흐름:
  1. DB에서 전종목 가격 로드 (date × symbol 피벗)
  2. 시장/섹터 평균 수익률 사전 계산
  3. 종목별 루프:
       a. 기술적 지표 (technical.py)
       b. 시장 상대 강도 (market_context.py)
       c. 수급 피처 (flows.py)
       d. 펀더멘털 병합
       e. 결측치 처리
       f. features 테이블에 upsert
  4. 진행률 tqdm 표시 + 에러 로깅
"""

import logging
import argparse
import sys
from datetime import date, timedelta
from typing import Optional, List, Dict

# daily_pipeline.py가 자식 프로세스 stdout을 UTF-8로 디코딩하므로(run() 참고) 맞춰줌 —
# 콘솔이 cp949인 환경(수동 실행 .bat 등)에서 안 맞추면 UnicodeEncodeError 스팸 발생(2026-06-23)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from tqdm import tqdm

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
    load_per_pbr_pit,
    load_all_symbols_with_market,
    load_symbol_sector_map,
    get_last_feature_date,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),  # 기본값(stderr)은 위 reconfigure 효과를 못 받음
        logging.FileHandler("features.log", encoding="utf-8"),
    ],
)

# 피처 계산에 필요한 최소 데이터 수 (MA120 때문에 최소 130개 필요)
MIN_ROWS = 130
# NaN 허용 비율: 전체 컬럼의 이 비율 이상 NaN이면 해당 행 DROP
MAX_NAN_RATIO = 0.5


# ── 결측치 처리 ────────────────────────────────────────────────

def _clean_row(row: Dict) -> Dict:
    """float NaN / numpy nan / pandas NA → None (SQLite NULL); numpy scalar → Python type"""
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


def _df_to_records(
    symbol: str,
    tech_df: pd.DataFrame,
    rel_df: pd.DataFrame,
    flow_df: pd.DataFrame,
    fund_pit: pd.DataFrame,
    start_date: Optional[str],
    regime_df: Optional[pd.DataFrame] = None,
    frgn_feat_df: Optional[pd.DataFrame] = None,
) -> List[Dict]:
    """
    모든 피처 DataFrame을 합치고 DB 레코드 형태로 변환
    start_date 이후 데이터만 반환 (증분 업데이트)
    """
    # 피처 합치기
    combined = pd.concat([tech_df, rel_df, flow_df], axis=1)

    # 시장 국면 피처 join (날짜 기준 left join)
    if regime_df is not None and not regime_df.empty:
        combined = combined.join(regime_df, how="left")

    # rel_sector_1d는 DB 스키마에 없으므로 제거
    combined = combined.drop(columns=["rel_sector_1d"], errors="ignore")

    # 펀더멘털 추가 (point-in-time PER/PBR — 날짜별 join, look-ahead bias 없음)
    combined = combined.join(fund_pit, how="left")

    # frgn_norm_cum10/frgn_streak3: 기각됨(2026-07-02), 계산 미전달(2026-07-05).
    # 기존 저장분은 DB에 남아 있음 — frgn_feat_df=None이면 join 건너뜀.
    if frgn_feat_df is not None and not frgn_feat_df.empty:
        combined = combined.join(frgn_feat_df, how="left")

    # NaN이 너무 많은 초기 행 제거 (워밍업 기간)
    feature_cols = [c for c in combined.columns if c != "target"]
    nan_ratio = combined[feature_cols].isna().mean(axis=1)
    combined = combined[nan_ratio < MAX_NAN_RATIO]

    # 증분: 마지막 저장일 이후만
    if start_date:
        combined = combined[combined.index > start_date]

    if combined.empty:
        return []

    records = []
    for date_str, row in combined.iterrows():
        rec = {"symbol": symbol, "date": date_str}
        rec.update(row.to_dict())
        records.append(_clean_row(rec))

    return records


# ── 단일 종목 처리 ─────────────────────────────────────────────

def process_symbol(
    sym_info: Dict,
    market_rets: Dict[str, pd.Series],
    sector_ret_map: Dict[str, pd.Series],
    data_start: str,
    regime_df: Optional[pd.DataFrame] = None,
    force_rebuild: bool = False,
) -> int:
    """
    단일 종목의 전체 피처를 계산해 DB에 저장
    반환: 저장된 레코드 수
    """
    symbol = sym_info["symbol"]
    market = sym_info["market"]

    # ── 가격 데이터 로드 ──
    prices_df = load_prices_for_symbol(symbol, start=data_start)
    if prices_df.empty or len(prices_df) < MIN_ROWS:
        logger.debug("데이터 부족 스킵 [%s]: %d행", symbol, len(prices_df))
        return 0

    # ── 증분: 이미 저장된 마지막 날짜 확인 ──
    last_saved = None if force_rebuild else get_last_feature_date(symbol)

    # sort once; compute_all_technical also sorts internally but uses same result
    prices_sorted = prices_df.sort_values("date").set_index("date")

    # ── 기술적 지표 ──
    tech_df = compute_all_technical(prices_df)
    if tech_df.empty:
        return 0

    # ── 시장 상대 강도 ──
    close = prices_sorted["close"].astype(float)
    market_ret_series = market_rets.get(market, pd.Series(dtype=float))
    sector_ret_series = sector_ret_map.get(symbol, pd.Series(dtype=float))

    rel_df = calc_relative_strength(close, market_ret_series, sector_ret_series)

    # ── 수급 피처 (flows — 외국인 보유비율 기반) ──
    flows_df = load_flows(symbol, start=data_start)
    flow_df  = calc_flow_features(tech_df.index, flows_df)

    # ── 펀더멘털 (point-in-time PER/PBR — look-ahead bias 제거, 2026-06-27) ──
    fund_pit = load_per_pbr_pit(symbol, tech_df.index, close.reindex(tech_df.index))

    # ── 합치기 & 저장 ──
    # frgn_norm_cum10/frgn_streak3: 기각됨(SHAP 0%, 2026-07-02). 계산 제거(L1 수정 2026-07-05).
    # 기존 저장분은 DB에 남아 있음. 재활성화 시 load_frgn_features() + frgn_feat_df 인자 복구.
    records = _df_to_records(
        symbol, tech_df, rel_df, flow_df, fund_pit, last_saved, regime_df,
    )
    if records:
        upsert_features(records)

    return len(records)


# ── 메인 파이프라인 ────────────────────────────────────────────

def run_pipeline(
    start: Optional[str] = None,
    end: Optional[str] = None,
    symbols: Optional[List[str]] = None,
    force_rebuild: bool = False,
):
    """
    전종목 피처 계산 파이프라인

    start         : 가격 데이터 로드 시작일 (미지정시 3년 전)
    end           : 사용 안 함 (prices 테이블에 있는 전체 기간 사용)
    symbols       : 특정 종목만 처리 (미지정시 전체)
    force_rebuild : True면 마지막 저장일 무시하고 전체 재계산
    """
    init_features_table()

    data_start = start or (
        date.today() - timedelta(days=3 * 365 + 30)  # 워밍업 여유 포함
    ).strftime("%Y%m%d")

    logger.info("=" * 60)
    logger.info("피처 파이프라인 시작 | 가격 데이터 기준일: %s~", data_start)
    logger.info("=" * 60)

    # ── 전종목 종가 피벗 로드 (시장 평균 계산용) ──
    logger.info("[1/3] 전종목 종가 피벗 로드 중...")
    all_closes = load_all_closes(start=data_start)
    sym_market_list = load_all_symbols_with_market()
    symbol_market_map = {s["symbol"]: s["market"] for s in sym_market_list}

    logger.info("[2/3] 시장/섹터 평균 수익률 + 시장 국면 피처 계산 중...")
    symbol_sector_map = load_symbol_sector_map()
    n_with_sector = sum(1 for v in symbol_sector_map.values() if v)
    market_rets    = build_market_returns(all_closes, symbol_market_map)
    sector_ret_map = build_sector_returns(
        all_closes, symbol_market_map, market_rets=market_rets,
        symbol_sector_map=symbol_sector_map,
    )
    logger.info("  KOSPI 기준일 수: %d, KOSDAQ: %d | 업종코드 수집된 종목: %d/%d (나머지는 시장 평균 폴백)",
                len(market_rets.get("KOSPI", [])),
                len(market_rets.get("KOSDAQ", [])),
                n_with_sector, len(symbol_sector_map))

    regime_df = build_market_regime_features(all_closes, start=data_start)
    if regime_df.empty:
        logger.warning("시장 국면 피처 비어있음 — index_collector.py --initial 실행 필요")

    # ── 처리 대상 종목 결정 ──
    if symbols:
        target_list = [s for s in sym_market_list if s["symbol"] in symbols]
    else:
        target_list = sym_market_list

    logger.info("[3/3] 종목별 피처 계산 시작: %d개", len(target_list))

    total_saved = 0
    error_count = 0

    for sym_info in tqdm(target_list, desc="피처 계산", unit="종목"):
        try:
            n = process_symbol(sym_info, market_rets, sector_ret_map, data_start,
                               regime_df=regime_df, force_rebuild=force_rebuild)
            total_saved += n
        except Exception as exc:
            error_count += 1
            logger.error("피처 오류 [%s]: %s", sym_info["symbol"], exc, exc_info=False)

    logger.info("=" * 60)
    logger.info("파이프라인 완료 | 저장: %d건 | 오류: %d건", total_saved, error_count)
    logger.info("=" * 60)


# ── CLI ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="피처 엔지니어링 파이프라인")
    parser.add_argument("--start",   type=str, help="가격 데이터 시작일 YYYYMMDD")
    parser.add_argument("--symbols", type=str, help="특정 종목만 처리 (쉼표 구분, 예: 005930,000660)")
    parser.add_argument("--init-only", action="store_true", help="features 테이블만 생성")
    parser.add_argument("--rebuild", action="store_true",
                        help="마지막 저장일 무시하고 전체 피처 재계산")
    args = parser.parse_args()

    if args.init_only:
        init_features_table()
        print("features 테이블 초기화 완료")
        return

    symbol_list = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    run_pipeline(start=args.start, symbols=symbol_list, force_rebuild=args.rebuild)


if __name__ == "__main__":
    main()
