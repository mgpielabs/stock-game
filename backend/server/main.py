"""
FastAPI 주식 예측 서버

실행:
  cd backend/server
  uvicorn main:app --port 8001
"""

import asyncio
import bisect
import csv
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# .env 로드 (python-dotenv 없이 직접 파싱)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.db import init_db, get_connection as _db_get_connection
from concentration_filter import RecommendationLog
from disclosure_analysis import analyze_risks
from disclosure_filter import filter_risky_predictions
from history_trader import sync_trades as _sync_trades, get_trades as _get_trades
from paper_trader import (
    record_recommendations,
    close_expired_trades,
    get_active_trades_enriched,
    get_performance_summary as paper_performance_summary,
    get_performance_timeline as paper_performance_timeline,
    get_performance_by_model as paper_performance_by_model,
    should_enter_5d,
    should_enter_60d,
)
from predictor import (
    find_latest_model_dir,
    get_kospi_bear_signal,
    get_latest_feature_date,
    get_market_trend,
    get_regime_gate_g2,
    get_volatility_regime,
    invalidate_60d_model_cache,
    invalidate_etf_cache,
    load_backtest_data,

    load_calibrator,
    load_model,
    load_model_info,
    predict_60d,
    predict_ticker,
    predict_today,
    set_serving_feature_cols,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ── 앱 상태 ───────────────────────────────────────────────────

BACKEND_ROOT = Path(__file__).parent.parent
DB_PATH = BACKEND_ROOT / "data" / "stocks.db"

# KSIC 업종코드 → 한국어 섹터명 캐시 (최초 접근 시 1회 로드)
_ksic_names_cache: Optional[Dict[str, str]] = None

def _get_ksic_names() -> Dict[str, str]:
    global _ksic_names_cache
    if _ksic_names_cache is None:
        try:
            ksic_path = BACKEND_ROOT / "data" / "ksic_sector_names.json"
            import json as _json_mod
            _ksic_names_cache = _json_mod.loads(ksic_path.read_text(encoding="utf-8"))
        except Exception:
            _ksic_names_cache = {}
    return _ksic_names_cache

def _sector_name(sector_code: Optional[str]) -> Optional[str]:
    """DART induty_code(5~6자리) → KSIC 3자리 키로 한국어 섹터명 반환."""
    if not sector_code:
        return None
    key = str(sector_code)[:3]
    return _get_ksic_names().get(key)

class _State:
    booster: Optional[Any] = None
    model_dir: Optional[Path] = None
    # 확률 calibration(진단#7 후속, 2026-06-22) — 화면 표시용 probability_calibrated 계산에만
    # 쓰임, 랭킹/추천 선정에는 영향 없음(raw 확률로 이미 정렬 완료된 결과에 덧붙이는 값)
    calibrator: Optional[Any] = None
    latest_date: Optional[str] = None
    predictions_cache: Dict[str, Any] = {}
    excluded_cache: Dict[str, Any] = {}
    # 약세장 가드 활성화 시 전환할 bear 전용 모델 (train_regime.py 산출물, 없으면 None — 통합 모델만 사용)
    bear_booster: Optional[Any] = None
    bear_model_dir: Optional[Path] = None
    bear_predictions_cache: Dict[str, Any] = {}
    # 시장 추세 캐시 (날짜별 1회 계산)
    market_trend_cache: Optional[Dict[str, Any]] = None
    market_trend_date: Optional[str] = None
    # 변동성 국면 캐시 (날짜별 1회 계산 — get_volatility_regime 자체는 가벼워졌지만
    # 매 요청 DB 왕복도 피하도록 동일 패턴 적용)
    volatility_regime_cache: Optional[Dict[str, Any]] = None
    volatility_regime_date: Optional[str] = None
    # 스크리너: 52주 신고가 근접 맵 (날짜별 1회 계산 — 252일 롤링이라 가벼운 연산 아님)
    near_high_cache: Optional[Dict[str, bool]] = None
    near_high_date: Optional[str] = None
    # 스크리너: 배당수익률 맵 (dividends 테이블, 종목당 최신 공시 1건이라 가벼움 — 날짜별 캐시)
    dps_cache: Optional[Dict[str, float]] = None
    dps_date: Optional[str] = None
    # 스크리너: EPS 맵 (dividends.eps — PER을 fundamentals.per 대신 이걸로 역산, 검증됨)
    eps_cache: Optional[Dict[str, float]] = None
    eps_date: Optional[str] = None
    # 스크리너: BPS 맵 (financials.bps — PBR 역산, 검증됨, ⚠️ 전체의 ~11%만 수집)
    bps_cache: Optional[Dict[str, float]] = None
    bps_date: Optional[str] = None
    # 수급 경고 플래그 (investor_trading_kis, 외국인+기관 동시순매도&개인순매수 — 2026-06-27
    # kis_investor_flow_validation.py에서 5일/10일 둘 다 일관된 초과수익 음수로 확인된
    # 유일한 신호. 모델 피처화는 커버리지 0.03%라 기각, 룰 기반 경고 표시로만 사용)
    investor_warning_cache: Optional[Dict[str, bool]] = None
    investor_warning_date: Optional[str] = None
    # 전일 고변동성 경고 플래그 (prices 테이블, 전일 intraday range 상위5% — 2026-07-02
    # gap_streak_volatility_validation.py IS/OOS p=0.0000 일관 확인, atr_pct와 r=0.51로
    # 기존 피처와 높은 상관+SHAP 0%라 모델 피처 기각, 표시 전용 배지로만 채택)
    high_vol_cache: Optional[Dict[str, bool]] = None
    high_vol_date: Optional[str] = None
    # 배당 성장 상위 플래그 (dividends.dps YoY 성장률 상위 25% — 2026-07-03
    # timescale_profiling.py: OOS 5d/20d/60d 전구간 유의(p=0.003/0.000/0.000).
    # SHAP 0% + NaN 70%라 모델 피처 기각, 스크리너 필터/표시 전용으로 채택)
    dps_growth_cache: Optional[Dict[str, bool]] = None
    dps_growth_date: Optional[str] = None
    # trailing 20d 평균 갭 맵 (C모드 갭 필터용 — 2026-07-04
    # gap_alternatives.py 검증: trail_gap>2% 제외 시 실전 평균 -0.14% → +0.35% 개선.
    # 강세장(val 2025-26) 기준. 하락장 미검증. 모델 점수/순위 불변, 후처리 필터만)
    trail_gap_cache: Optional[Dict[str, float]] = None
    trail_gap_date: Optional[str] = None
    # G2 레짐 게이트 캐시 (regime_gate_cumulative.py 2026-07-08 검증 — IS+OOS 누적 +155.8%p,
    # OOS 독립 구간 1개라 통계 보류, 페이퍼 전용 채택)
    regime_gate_cache: Optional[Dict[str, Any]] = None
    regime_gate_date: Optional[str] = None
    # 스크리너: 60d PIT 팩터 맵 (EPS성장/ROE/BPS성장, 날짜별 캐시)
    factors_60d_cache: Optional[Dict[str, Dict]] = None
    factors_60d_date: Optional[str] = None
    # 스크리너: 60d 모델 전체 종목 점수 맵 (score_60d_top 필터 활성 시만 계산)
    scores_60d_cache: Optional[Dict[str, float]] = None
    scores_60d_date: Optional[str] = None
    # 스크리너: 20일 평균 거래대금 (prices.close*volume)
    vol20d_cache: Optional[Dict[str, float]] = None
    vol20d_date: Optional[str] = None
    # 스크리너: 특별배당 포함 여부 플래그 (payout_ratio > 100% → True)
    special_div_cache: Optional[Dict[str, bool]] = None
    special_div_date: Optional[str] = None
    # 스크리너: 주식병합/분할 CF 보정 적용 여부 플래그 (CF ≠ 1.0 → True)
    split_adj_cache: Optional[Dict[str, bool]] = None
    split_adj_date: Optional[str] = None
    # 스크리너: 배당중단의심 플래그 (FY2024 사용 중 + FY2025 dps=NULL 행 존재 → True)
    div_suspended_cache: Optional[Dict[str, bool]] = None
    div_suspended_date: Optional[str] = None
    # 스크리너: 실질 배당수익률 = 보정DPS ÷ 배당기준일 주가 (prices 테이블 조회)
    actual_yield_cache: Optional[Dict[str, float]] = None
    actual_yield_date: Optional[str] = None
    # 재학습 프로세스 추적
    retrain_proc: Optional[subprocess.Popen] = None
    retrain_started_at: Optional[float] = None
    retrain_log: List[str] = []
    # 데이터 업데이트(인앱 버튼, daily_pipeline.py --no-server-restart) 프로세스 추적
    update_proc: Optional[subprocess.Popen] = None
    update_started_at: Optional[float] = None
    update_log: List[str] = []
    update_finished_status: Optional[str] = None  # 마지막 완료 결과: "done" | "error"
    # 스크리너: 섹터 자금흐름 집계 캐시
    sector_flow_cache: Optional[Dict] = None
    sector_flow_date: Optional[str] = None
    # 스크리너: 외국인 보유비율 맵 (flows.foreign_net 최신값, 표시 전용)
    foreign_rate_cache: Optional[Dict[str, float]] = None
    foreign_rate_date: Optional[str] = None

_state = _State()

# 추천 로그 싱글톤 (서버 재시작 후에도 파일 유지)
_rec_log = RecommendationLog()
# 이미 로그에 기록된 날짜 추적 (동일 날짜 중복 쓰기 방지)
_logged_dates: set[str] = set()


def _reload_model_and_cache() -> None:
    """모델을 (재)로드하고 최신 날짜 예측을 캐시. 서버 시작 시 + 재학습 완료 시 호출."""
    try:
        _state.booster, _state.model_dir = load_model("target_5d")
        _state.calibrator = load_calibrator(_state.model_dir)
        _state.latest_date = get_latest_feature_date()
        # meta.json의 feature_cols로 서빙 피처 목록 갱신 (train-serving 일관성 보장)
        meta = load_model_info(_state.model_dir)
        if "feature_cols" in meta and meta["feature_cols"]:
            set_serving_feature_cols(meta["feature_cols"])
        logger.info(
            "모델 로드 완료 | dir=%s | 최신 피처 날짜=%s",
            _state.model_dir.name, _state.latest_date,
        )
        _state.predictions_cache.clear()
        _state.excluded_cache.clear()
        _state.bear_predictions_cache.clear()
        _state.market_trend_cache = None
        _state.market_trend_date = None
        _state.volatility_regime_cache = None
        _state.volatility_regime_date = None
        _state.near_high_cache = None
        _state.near_high_date = None
        _state.dps_cache = None
        _state.dps_date = None
        _state.eps_cache = None
        _state.eps_date = None
        _state.bps_cache = None
        _state.bps_date = None
        _state.investor_warning_cache = None
        _state.investor_warning_date = None
        _state.high_vol_cache = None
        _state.high_vol_date = None
        _state.dps_growth_cache = None
        _state.dps_growth_date = None
        _state.trail_gap_cache = None
        _state.trail_gap_date = None
        _state.regime_gate_cache = None
        _state.regime_gate_date = None
        _state.factors_60d_cache = None
        _state.factors_60d_date = None
        _state.scores_60d_cache = None
        _state.scores_60d_date = None
        _state.vol20d_cache = None
        _state.vol20d_date = None
        _state.special_div_cache = None
        _state.special_div_date = None
        _state.split_adj_cache = None
        _state.split_adj_date = None
        _state.div_suspended_cache = None
        _state.div_suspended_date = None
        _state.actual_yield_cache = None
        _state.actual_yield_date = None
        _state.sector_flow_cache = None
        _state.sector_flow_date = None
        _state.foreign_rate_cache = None
        _state.foreign_rate_date = None
        invalidate_60d_model_cache()
        invalidate_etf_cache()
        try:
            preds = predict_today(_state.booster, _state.latest_date, top_n=100, calibrator=_state.calibrator)
            filtered, excluded = filter_risky_predictions(preds, days=14)
            _state.predictions_cache[_state.latest_date] = filtered
            _state.excluded_cache[_state.latest_date] = excluded
            logger.info("공시 필터 적용: 통과 %d / 제외 %d", len(filtered), len(excluded))
            logger.info("예측 캐시 완료: %d종목", len(_state.predictions_cache[_state.latest_date]))
        except Exception as e:
            logger.warning("예측 캐시 실패: %s", e)

        # regime 모델 스위칭 제거 (2026-07-14): bear_guard UI 제거 시 스위칭 로직도 같이 제거했어야 하나
        # main.py에 잔존했음. 항상 통합 모델(ACTIVE 포인터)만 사용.
        _state.bear_booster, _state.bear_model_dir = None, None
    except Exception as exc:
        logger.warning("모델 로드 실패 (예측 엔드포인트 사용 불가): %s", exc)


def _is_bear_routing_active() -> bool:
    """약세장 가드가 켜져 있고 bear 전용 모델이 로드돼 있으면 True."""
    enabled = os.environ.get("BEAR_MARKET_GUARD", "true").lower() != "false"
    if not enabled or _state.bear_booster is None:
        return False
    return bool(get_kospi_bear_signal().get("guard_active", False))


def _get_predictions_for_date(target_date: str, shap_k: int = 6) -> Tuple[list, list, bool]:
    """target_date의 공시필터 적용된 전체 예측 + 제외목록 + bear모델 사용 여부.
    약세장 가드 활성 시 bear 전용 캐시/모델을, 아니면 기존 통합 캐시/모델을 사용."""
    use_bear = _is_bear_routing_active()
    cache = _state.bear_predictions_cache if use_bear else _state.predictions_cache
    cached = cache.get(target_date)
    if cached is not None:
        return cached, _state.excluded_cache.get(target_date, []), use_bear

    booster = _state.bear_booster if use_bear else _state.booster
    # bear 전용 모델은 별도 calibrator가 없음 — 약세장 가드 활성 시엔 보정 없이 raw 확률 표시
    calibrator = None if use_bear else _state.calibrator
    raw = predict_today(booster, target_date, top_n=100, shap_top_k=shap_k, calibrator=calibrator)
    all_predictions, excluded = filter_risky_predictions(raw, days=14)
    cache[target_date] = all_predictions
    _state.excluded_cache[target_date] = excluded
    return all_predictions, excluded, use_bear


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("서버 시작 — 모델 로드 중...")
    _reload_model_and_cache()

    # 모의투자: market_index 갱신 (거래일 캘린더 — 청산에 필수)
    _run_index_collector()

    # 모의투자: 경과 거래 자동 청산
    try:
        from datetime import date as _date
        _today = _date.today().strftime("%Y%m%d")
        result = close_expired_trades(_today)
        logger.info("모의투자 자동 청산: closed=%d expired=%d", result["closed"], result["expired"])
    except Exception as exc:
        logger.warning("모의투자 자동 청산 실패: %s", exc)

    # 모의투자: 오늘 추천 자동 기록 (캐시 있을 때만, UNIQUE로 중복 스킵)
    if _state.latest_date and _state.predictions_cache.get(_state.latest_date):
        try:
            _top_source, _, _used_bear = _get_predictions_for_date(_state.latest_date)
            top10 = _top_source[:10]
            _mkt = _get_market_trend_cached(_state.latest_date)
            _mode, _, _, _ = _compute_market_mode(_mkt)
            _guard_enabled = os.environ.get("BEAR_MARKET_GUARD", "true").lower() != "false"
            _guard_active = get_kospi_bear_signal().get("guard_active", False) if _guard_enabled else False
            _model_ver = (_state.bear_model_dir if _used_bear else _state.model_dir)
            _gate = _get_regime_gate_cached(_state.latest_date)
            _gate_blocked = _gate.get("gate_blocked", False)

            # ── 5d 슬리브: v1.2 — 5d 운용 중단, shadow 기록만 유지 ──
            # 근거: allocation_backtest.py (2026-07-12) — 5d cum -52.9%, 60d 단독이 +71.2%p 우위
            from prediction_logger import log_shadow_predictions
            log_shadow_predictions(_state.latest_date, "5d", top10, horizon=5)
            logger.info("v1.2: 5d 운용 중단 — shadow 기록만 (%s)", _state.latest_date)
            # (G2 게이트 로직 불필요 — 진입 자체가 없음)

            # ── 60d 슬리브: v1.1 게이트 없음 — 항상 60거래일 주기 진입 ──
            if should_enter_60d(_state.latest_date):
                try:
                    preds_60d = predict_60d(_state.latest_date, top_n=10)
                    if preds_60d:
                        _dir_60d = find_latest_model_dir("target_60d")
                        inserted_60 = record_recommendations(
                            _state.latest_date, preds_60d,
                            market_mode=_mode,
                            bear_guard_active=bool(_guard_active),
                            model_version=(_dir_60d.name if _dir_60d else None),
                            regime_gate_blocked=False,
                            horizon=60,
                        )
                        logger.info(
                            "모의투자 60d 기록: %s -> %d건 삽입 (gate_blocked=%s)",
                            _state.latest_date, inserted_60, _gate_blocked,
                        )
                        # shadow 60d: 게이트有 가상기록 — 게이트 차단 중 발생한 60d 진입을
                        # 별도 추적해 "gate-ON 60d였으면 어땠을지" 향후 성과 비교 가능
                        if _gate_blocked:
                            from prediction_logger import log_shadow_predictions
                            log_shadow_predictions(_state.latest_date, "60d", preds_60d, horizon=60)
                            logger.info("60d shadow 기록 (게이트有 반사실 추적): %s", _state.latest_date)
                except Exception as exc_60:
                    logger.warning("60d 모의투자 기록 실패: %s", exc_60)
            else:
                logger.info("60d 리밸런싱 주기 미도달 — 스킵 (%s)", _state.latest_date)

        except Exception as exc:
            logger.warning("모의투자 자동 기록 실패: %s", exc)

    # 매일 자정 market_index 갱신 + 모의투자 청산 백그라운드 태스크 시작
    task = asyncio.create_task(_daily_maintenance_loop())

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _daily_maintenance_loop() -> None:
    """매일 자정 00:05에 market_index 갱신 + 모의투자 자동 청산."""
    while True:
        now = datetime.now()
        next_run = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        wait_sec = (next_run - now).total_seconds()
        logger.info("일일 유지보수 다음 실행: %s (%.0f초 후)", next_run.strftime("%Y-%m-%d %H:%M"), wait_sec)
        await asyncio.sleep(wait_sec)

        logger.info("=== 일일 유지보수 시작 ===")
        _run_index_collector()
        try:
            today_str = datetime.now().strftime("%Y%m%d")
            result = close_expired_trades(today_str)
            logger.info("일일 모의투자 청산: closed=%d expired=%d", result["closed"], result["expired"])
        except Exception as exc:
            logger.warning("일일 모의투자 청산 실패: %s", exc)

        # prices 스테일 감지 — 파이프라인 미실행 경고
        try:
            pst = _get_prices_staleness()
            if pst["prices_stale"]:
                logger.warning(
                    "⚠ prices 테이블 %d거래일 지연 (최신: %s, 거래일: %s) — 데이터_수동업데이트.bat 실행 필요",
                    pst["prices_stale_trading_days"],
                    pst["prices_latest_date"],
                    pst["market_latest_date"],
                )
            else:
                logger.info(
                    "prices 최신일: %s (거래일 기준 정상)", pst["prices_latest_date"]
                )
        except Exception as exc:
            logger.warning("prices staleness 체크 실패: %s", exc)

        # 재학습 표시등 — 모델 학습일 90일 경과 여부
        try:
            rts = _get_retrain_status()
            if rts["retrain_due"]:
                logger.warning(
                    "⚠ 재학습 권고: %s 모델이 90일 이상 경과 (경과일: %s) — "
                    "주간모델_수동재학습.bat 실행 검토",
                    ", ".join(rts["retrain_due_models"]),
                    ", ".join(f"{k}={v}일" for k, v in rts["model_ages_days"].items()),
                )
        except Exception as exc:
            logger.warning("재학습 상태 체크 실패: %s", exc)


def _run_index_collector() -> None:
    """index_collector.py 실행 (market_index 증분 갱신)."""
    index_script = BACKEND_ROOT / "data" / "index_collector.py"
    if not index_script.exists():
        logger.warning("index_collector.py 없음: %s", index_script)
        return
    try:
        result = subprocess.run(
            [sys.executable, str(index_script)],
            capture_output=True, timeout=60,
        )
        if result.returncode == 0:
            logger.info("market_index 갱신 완료")
        else:
            logger.warning("market_index 갱신 실패 (returncode=%d)", result.returncode)
    except Exception as exc:
        logger.warning("market_index 갱신 예외: %s", exc)


app = FastAPI(
    title="Stock Prediction API",
    description="LightGBM 기반 한국 주식 상승 확률 예측",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── 헬퍼 ─────────────────────────────────────────────────────

def _require_model() -> None:
    if _state.booster is None:
        raise HTTPException(status_code=503, detail="모델이 로드되지 않았습니다. 먼저 train.py를 실행하세요.")


def _get_date(date: Optional[str]) -> str:
    """?date= 파라미터가 없으면 최신 피처 날짜 사용."""
    if date:
        return date
    if _state.latest_date:
        return _state.latest_date
    return get_latest_feature_date()


def _get_market_trend_cached(target_date: str) -> Dict[str, Any]:
    """날짜가 바뀔 때만 재계산, 이외에는 캐시 반환."""
    if _state.market_trend_cache is None or _state.market_trend_date != target_date:
        _state.market_trend_cache = get_market_trend(days=30)
        _state.market_trend_date = target_date
    return _state.market_trend_cache


def _get_volatility_regime_cached(target_date: str) -> Dict[str, Any]:
    """날짜가 바뀔 때만 재계산, 이외에는 캐시 반환 (market_trend과 동일 패턴)."""
    if _state.volatility_regime_cache is None or _state.volatility_regime_date != target_date:
        _state.volatility_regime_cache = get_volatility_regime()
        _state.volatility_regime_date = target_date
    return _state.volatility_regime_cache


def _compute_market_mode(market: Dict[str, Any]) -> tuple:
    """trend → (mode, message, threshold, max_count). max_count=0은 제한 없음."""
    trend = market.get("trend", "unknown")
    if trend == "bear":
        return (
            "defensive",
            "약세장 감지 - 방어 모드 활성화 (확신도 높은 종목 최대 15개)",
            0.55,
            15,
        )
    if trend == "sideways":
        return (
            "cautious",
            "횡보장 진행 중 - 강한 신호 종목만 표시 (최대 15개)",
            0.6,
            15,
        )
    return ("aggressive", "", 0.0, 0)


# ── 엔드포인트 ────────────────────────────────────────────────

@app.get(
    "/api/predictions/today",
    summary="오늘 기준 상위 종목 예측",
    response_description="상위 N종목 목록 (확률 + SHAP 주요 피처)",
)
async def get_today_predictions(
    top_n: int = Query(default=30, ge=1, le=100, description="반환할 종목 수"),
    shap_k: int = Query(default=6, ge=1, le=20, description="종목별 SHAP 피처 수"),
    date: Optional[str] = Query(default=None, description="기준일 YYYYMMDD (미지정=최신)"),
) -> Dict[str, Any]:
    _require_model()
    target_date = _get_date(date)

    # 1단계: 공시 필터 후 전체 리스트 캐시 (top_n 미적용)
    # 약세장 가드 활성 + bear 전용 모델이 있으면 그 모델의 예측을 사용
    all_predictions, _, _used_bear_model = _get_predictions_for_date(target_date, shap_k)

    predictions = all_predictions

    # 2.5단계: 적자 기업 제외 (PER<=0인 종목만 — PER 데이터 없음(NULL)은 "정보 없음"으로
    # 통과시킴. 예전엔 (per or 0) > 0라서 NULL이 0으로 취급돼 적자로 오인 제외됐었음 —
    # 분석 결과 전체 종목의 57%가 PER NULL이라 정상 종목 다수가 잘못 걸러지고 있었음.
    before_profit = len(predictions)
    predictions = [p for p in predictions if p.get("per") is None or p.get("per") > 0]
    if len(predictions) < before_profit:
        logger.info("적자 기업(PER<=0) 제외: %d → %d종목", before_profit, len(predictions))

    # 3단계: 가격 급등 주의 표시 (제외 대신 플래그)
    RET_5D_WARN = 0.30   # 5일 30% 이상
    RET_1D_WARN = 0.15   # 당일 15% 이상
    for p in predictions:
        p["price_surge_warning"] = (
            (p.get("ret_5d") or 0) >= RET_5D_WARN
            or (p.get("ret_1d") or 0) >= RET_1D_WARN
        )

    # 3.5단계: 수급 경고 표시 (외국인+기관 동시순매도&개인순매수 — 룰 기반, 모델 점수와
    # 무관. 제외 대신 플래그만 — investor_trading_kis 30거래일 한정이라 단정적 필터링은
    # 과함, CLAUDE.md "수급 데이터 ML 피처화 검토" 참고)
    investor_warning_map = _get_investor_warning_cached(target_date)
    for p in predictions:
        p["investor_sell_warning"] = investor_warning_map.get(p["symbol"], False)

    # 3.6단계: 전일 고변동성 경고 표시 (전일 intraday range 횡단면 상위5% — 룰 기반,
    # 모델 점수/추천 순위에 영향 없음. validate_signals.py 검증: IS/OOS p=0.0000 일관
    # (-0.727%p/-1.103%p), atr_pct r=0.51 중복+SHAP 0%라 모델피처 기각, 배지만 표시)
    high_vol_map = _get_high_vol_cached(target_date)
    for p in predictions:
        p["high_vol_warning"] = high_vol_map.get(p["symbol"], False)

    # 3.7단계: 배당 성장 상위 표시 (dividends.dps YoY 성장률 상위 25% — 룰 기반,
    # 모델 점수/순위에 영향 없음. timescale_profiling.py 검증: OOS 5d/20d/60d 전구간
    # 유의(+0.35%p/+1.07%p/+2.64%p), SHAP 0%+NaN 70%로 모델 피처는 기각, 표시만)
    dps_growth_map = _get_dps_growth_cached(target_date)
    for p in predictions:
        p["dps_growth_flag"] = dps_growth_map.get(p["symbol"], False)

    # 3.8단계: C모드 갭 필터 (trailing 20d 평균갭 표시 + 고갭 종목 후순위)
    # trail_gap > 2%인 종목은 모델 확률 순위를 유지한 채로 저갭 종목보다 뒤로 밀림.
    # 모델 점수/probability 자체 불변. 표시 정보로도 활용(trail_gap20d_pct 필드).
    # gap_alternatives.py(2026-07-04) 강세장val 검증: 적용 시 실전평균 -0.14%→+0.35%.
    GAP_C_THRESHOLD = 0.02  # 2%
    _trail_gap_map = _get_trail_gap_cached(target_date)
    for p in predictions:
        tg = _trail_gap_map.get(p["symbol"])
        p["trail_gap20d_pct"] = round(tg * 100, 2) if tg is not None else None
        p["gap_high"] = bool(tg is not None and tg > GAP_C_THRESHOLD)
    # 저갭 우선 stable sort (같은 그룹 내 원래 확률 순서 보존)
    _n_gap_high = sum(1 for p in predictions if p.get("gap_high"))
    if _n_gap_high > 0:
        predictions = (
            [p for p in predictions if not p.get("gap_high")]
            + [p for p in predictions if p.get("gap_high")]
        )
        logger.info("C모드 갭 필터: 고갭(>2%%) %d종목 후순위로", _n_gap_high)

    # 4단계: 시장 국면 표시용 정보 (필터 미적용 — 기각된 설계, 2026-07-12 제거)
    market = _get_market_trend_cached(target_date)
    mode, message, threshold, max_count = _compute_market_mode(market)
    # threshold / max_count 는 API 응답 메타데이터용으로만 유지, 실제 필터 미사용

    # 4.5단계: 약세장 가드 신호 수집 (표시용만 — top_n 축소 제거, 2026-07-12)
    _bear_guard_enabled = os.environ.get("BEAR_MARKET_GUARD", "true").lower() != "false"
    _bear_signal = get_kospi_bear_signal() if _bear_guard_enabled else {
        "ret_5d": 0.0, "reduce_half": False, "reduce_quarter": False,
        "guard_active": False, "guard_reason": "BEAR_MARKET_GUARD=false",
    }
    _guard_top_n_cap = None  # 축소 미적용 (메타데이터로만 유지)

    # 5단계: 최종 종목 선택 (섹터 분산 필터 + top_n)
    # SECTOR_DIVERSIFICATION=false 환경변수로 비활성화 가능
    effective_n = top_n
    _sector_div_enabled = os.environ.get("SECTOR_DIVERSIFICATION", "true").lower() != "false"
    _max_per_market     = int(os.environ.get("SECTOR_MAX_PER_MARKET", "5"))

    if _sector_div_enabled:
        _selected, _mkt_cnt, _excl_div = [], {}, 0
        for _p in predictions:
            _mkt = _p.get("market", "UNKNOWN")
            if _mkt_cnt.get(_mkt, 0) < _max_per_market:
                _selected.append(_p)
                _mkt_cnt[_mkt] = _mkt_cnt.get(_mkt, 0) + 1
            else:
                _excl_div += 1
            if len(_selected) >= effective_n:
                break
        logger.info(
            "섹터 분산 필터(market max=%d): 후보=%d → 선택=%d 제외=%d",
            _max_per_market, len(_selected) + _excl_div, len(_selected), _excl_div,
        )
        predictions = _selected
        _filters_applied = ["sector_diversification"]
    else:
        predictions = predictions[:effective_n]
        _mkt_cnt, _excl_div = {}, 0
        _filters_applied = []
        for _p in predictions:
            _mkt = _p.get("market", "UNKNOWN")
            _mkt_cnt[_mkt] = _mkt_cnt.get(_mkt, 0) + 1

    # 거래량 급등 종목 별도 분리 (20일 평균 대비 1.5배 이상)
    VOL_SURGE_MIN = 1.5
    vol_surge = [p for p in predictions if (p.get("vol_ratio_20d") or 0) >= VOL_SURGE_MIN]
    predictions = [p for p in predictions if (p.get("vol_ratio_20d") or 0) < VOL_SURGE_MIN]
    logger.info("거래량 급등 분리: 일반=%d, 급등=%d", len(predictions), len(vol_surge))

    # 최신 날짜 추천만 로그에 기록 (역사적 날짜 쿼리는 제외, vol_surge 포함)
    if target_date == _state.latest_date and target_date not in _logged_dates:
        all_symbols = [p["symbol"] for p in predictions] + [p["symbol"] for p in vol_surge]
        _rec_log.append(target_date, all_symbols)
        _logged_dates.add(target_date)

    excluded_with_analysis = [
        {**ex, "analysis": analyze_risks(ex.get("risks", []))}
        for ex in _state.excluded_cache.get(target_date, [])
    ]

    # 변동성 국면 (저변동성 구간 신뢰도 경고 — 추천 개수는 조절 안 함, 백테스트로
    # 손실 확대 근거를 찾지 못해 경고 표시만 적용. 약세장 가드와는 독립적인 신호.)
    _volatility_regime = _get_volatility_regime_cached(target_date)

    # 신뢰도 / 위험도 분포 집계
    _conf_dist: Dict[str, int] = {}
    _risk_dist: Dict[str, int] = {}
    for _p in predictions:
        _cl = _p.get("confidence_level", "LOW")
        _rl = _p.get("risk_level", "LOW")
        _conf_dist[_cl] = _conf_dist.get(_cl, 0) + 1
        _risk_dist[_rl] = _risk_dist.get(_rl, 0) + 1

    return {
        "date":               target_date,
        "total_stocks":       len(predictions),
        "predictions":        predictions,
        "vol_surge":          vol_surge,
        "excluded":           excluded_with_analysis,
        "market_mode":        mode,
        "market_message":     message,
        "threshold_applied":  threshold,
        # 섹터 분산 필터 메타데이터
        "filter_applied":                  _filters_applied,
        "market_distribution":             dict(_mkt_cnt),
        "excluded_by_diversification":     _excl_div,
        # 약세장 가드 메타데이터
        "bear_market_guard_active":        bool(_bear_signal.get("guard_active", False)),
        "bear_market_guard_reason":        _bear_signal.get("guard_reason", ""),
        "kospi_5d_ret_pct":                _bear_signal.get("ret_5d", 0.0),
        # 이번 예측에 실제로 사용된 모델 ("bear"=약세장 전용 모델로 전환됨, "unified"=기존 통합 모델)
        "model_regime":                    "bear" if _used_bear_model else "unified",
        # 변동성 국면 (저변동성 구간 신뢰도 경고)
        "volatility_regime":               _volatility_regime,
        # C모드 갭 필터 메타데이터 (gap_alternatives.py 2026-07-04 검증)
        "gap_filter_mode":                 "C_trailing20d",
        "gap_filter_threshold_pct":        2.0,
        "gap_filter_high_count":           _n_gap_high,
        "gap_filter_expected_return_pct":  0.35,  # 강세장 val 기준
        "gap_filter_note":                 "강세장(2025-26 val) 기준. 하락장 성과 미검증.",
        # G2 레짐 게이트 (페이퍼 전용, 2026-07-08 채택)
        "regime_gate":                     _get_regime_gate_cached(target_date),
        # 추천 설명 메타데이터
        "explanation_version":             "v1",
        "confidence_distribution":         _conf_dist,
        "risk_distribution":               _risk_dist,
    }


@app.get(
    "/api/predictions/60d",
    summary="60일 중기 예측 상위 종목",
    response_description="60d 초과수익 모델 기준 상위 N종목",
)
async def get_60d_predictions(
    top_n: int = Query(default=30, ge=1, le=100, description="반환할 종목 수"),
    date: Optional[str] = Query(default=None, description="기준일 YYYYMMDD (미지정=최신)"),
) -> Dict[str, Any]:
    target_date = _get_date(date)
    try:
        preds = predict_60d(target_date, top_n=top_n)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"60d 모델 없음: {exc}")
    except Exception as exc:
        logger.exception("predict_60d 오류")
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "date":        target_date,
        "predictions": preds,
        "count":       len(preds),
        "model":       "target_60d",
        "note":        "60일 초과수익(횡단면 중앙값 대비 +7% 이상) 예측 모델. 5d 모델과 독립 운용.",
    }


@app.get(
    "/api/predictions/{ticker}",
    summary="특정 종목 상세 분석",
    response_description="확률, 전체 SHAP, 가격 히스토리, 최근 피처값",
)
async def get_ticker_prediction(
    ticker: str,
    date: Optional[str] = Query(default=None, description="기준일 YYYYMMDD (미지정=최신)"),
    price_days: int = Query(default=60, ge=10, le=1095, description="가격 히스토리 일수"),
) -> Dict[str, Any]:
    _require_model()
    target_date = _get_date(date)
    result = predict_ticker(_state.booster, ticker, target_date, price_days=price_days, calibrator=_state.calibrator)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"종목 {ticker}의 피처 없음 (date={target_date}). 피처 파이프라인을 먼저 실행하세요.",
        )
    return result


@app.get(
    "/api/backtest/performance",
    summary="백테스트 성과",
    response_description="누적 수익률, 일별 수익률, Sharpe, MDD, Win Rate",
)
async def get_backtest_performance() -> Dict[str, Any]:
    _require_model()
    meta      = load_model_info(_state.model_dir)
    backtest  = load_backtest_data(_state.model_dir, meta.get("target_col", "target_1d"))
    return {
        "model_version":       meta.get("version"),
        "target_col":          meta.get("target_col"),
        "trained_at":          meta.get("trained_at"),
        "val_auc":             meta.get("val_auc"),
        "precision_at_topk":   meta.get("precision_at_topk"),
        "label_basis":         meta.get("label_basis"),
        "close_win_rate_pct":  meta.get("close_win_rate_pct"),
        "close_hit5_rate_pct": meta.get("close_hit5_rate_pct"),
        "backtest":            backtest,  # None이면 train 후 재실행 필요
    }


@app.get(
    "/api/model/info",
    summary="모델 정보",
    response_description="버전, 학습일, AUC, 피처 목록 등 메타 정보",
)
async def get_model_info() -> Dict[str, Any]:
    _require_model()
    return load_model_info(_state.model_dir)


@app.get("/api/market/trend", summary="시장 추세")
async def market_trend_endpoint() -> Dict[str, Any]:
    """최근 20거래일 유동성 종목 중앙값 수익률 기반 시장 추세 (강세/횡보/약세)."""
    target_date = _get_date(None)
    result = dict(_get_market_trend_cached(target_date))
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT close FROM market_index WHERE code='1001' ORDER BY date DESC LIMIT 20"
            ).fetchall()
        result["kospi_20d"] = [r[0] for r in reversed(rows)]
    except Exception:
        result["kospi_20d"] = []
    return result


@app.get("/api/daily/summary", summary="일일 아침 요약")
async def daily_summary() -> Dict[str, Any]:
    """시장 상황 + 오늘의 상위 3종목 + 신뢰도 한줄 요약."""
    _require_model()
    target_date = _get_date(None)
    cached = _state.predictions_cache.get(target_date, [])
    top3 = cached[:3]
    market = _get_market_trend_cached(target_date)

    if market["trend"] == "bear":
        caution = "약세장 진행 중 — 추천 신호 신뢰도 낮음. 포지션 최소화 권장."
        model_conf = "낮음"
    elif market["trend"] == "bull":
        caution = "강세장 진행 중 — 추천 신호 활용도 양호."
        model_conf = "높음"
    else:
        caution = "횡보장 — 종목 선별 신중, 손절 기준 미리 설정 권장."
        model_conf = "보통"

    return {
        "date":             target_date,
        "market":           market,
        "top3":             top3,
        "model_confidence": model_conf,
        "caution":          caution,
    }


@app.get("/api/naver-search/ac", summary="네이버 주식 종목명 자동완성")
async def naver_search_ac(q: str = "", target: str = "stock,index", lang: str = "ko"):
    url = f"https://ac.stock.naver.com/ac?q={urllib.parse.quote(q)}&target={urllib.parse.quote(target)}&lang={lang}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://m.stock.naver.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            import json as _json
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception:
        data = {"items": []}
    return data


@app.get("/api/candles/{symbol}", summary="종목 일봉 캔들 데이터")
async def get_candles(
    symbol: str,
    days: int = Query(default=365, ge=30, le=1825, description="조회 일수"),
) -> List[Dict]:
    """prices 테이블에서 OHLCV 일봉을 반환. time은 UTC 자정 Unix 초."""
    code = symbol.split(".")[0]
    if not DB_PATH.exists():
        raise HTTPException(status_code=503, detail="DB 파일 없음")
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM prices "
            "WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (code, days),
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"종목 {code} 데이터 없음")
    result = []
    for date_str, open_, high, low, close, volume in reversed(rows):
        y, m, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])
        unix_sec = int(datetime(y, m, d, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        result.append({
            "time": unix_sec,
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": int(volume) if volume is not None else 0,
        })
    return result


@app.get("/health", summary="헬스 체크")
async def health() -> Dict[str, Any]:
    pst = _get_prices_staleness()
    rts = _get_retrain_status()
    return {
        "status":                    "ok" if _state.booster is not None else "no_model",
        "latest_date":               _state.latest_date or "unknown",
        "model_dir":                 _state.model_dir.name if _state.model_dir else "none",
        "prices_latest_date":        pst["prices_latest_date"],
        "prices_stale_trading_days": pst["prices_stale_trading_days"],
        "prices_stale":              pst["prices_stale"],
        "retrain_due":               rts["retrain_due"],
        "retrain_due_models":        rts["retrain_due_models"],
        "model_ages_days":           rts["model_ages_days"],
    }


@app.get("/status", summary="운영 상태 (사람이 읽는 HTML)", response_class=HTMLResponse)
async def status_page() -> HTMLResponse:
    """브라우저에서 바로 읽을 수 있는 운영 현황 요약 페이지."""
    pst = _get_prices_staleness()
    rts = _get_retrain_status()
    gate = _get_regime_gate_cached(_state.latest_date or "00000000")

    # 마지막 paper_trades 진입일
    try:
        with sqlite3.connect(DB_PATH) as _c:
            _last_paper = _c.execute(
                "SELECT MAX(recommended_date) FROM paper_trades"
            ).fetchone()[0] or "기록 없음"
    except Exception:
        _last_paper = "조회 실패"

    # 파이프라인 마지막 실행 (pipeline_daily.log 마지막 완료 행)
    try:
        log_path = Path(__file__).parent.parent / "pipeline_daily.log"
        _pipe_last = "로그 없음"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for ln in reversed(lines):
                if "일일 파이프라인 완료" in ln or "daily pipeline complete" in ln.lower():
                    _pipe_last = ln.strip()
                    break
            else:
                _pipe_last = f"완료 행 없음 (총 {len(lines)}줄)"
    except Exception as e:
        _pipe_last = f"로그 읽기 실패: {e}"

    def _ok(v: bool, yes: str = "✅", no: str = "🔴") -> str:
        return yes if v else no

    stale_days = pst.get("prices_stale_trading_days")
    stale_ok = not pst.get("prices_stale", False)
    model_ok = _state.booster is not None
    retrain_ok = not rts.get("retrain_due", False)
    gate_blocked = gate.get("gate_blocked", False)

    ages = rts.get("model_ages_days", {})

    try:
        dir_60d: Optional[Path] = find_latest_model_dir("target_60d")
    except Exception:
        dir_60d = None

    def _row(label: str, value: str, icon: str = "") -> str:
        return f"<tr><td>{label}</td><td>{icon} {value}</td></tr>"

    rows_data = "".join([
        _row("prices 최신일",
             pst.get("prices_latest_date") or "unknown",
             _ok(stale_ok, "✅", "⚠️")),
        _row("prices 지연",
             f"{stale_days}거래일" if stale_days is not None else "알 수 없음",
             _ok(stale_ok, "✅", "🔴")),
        _row("5d 모델",
             f"{_state.model_dir.name if _state.model_dir else '없음'} ({ages.get('5d', '?')}일 경과)",
             _ok(model_ok)),
        _row("60d 모델",
             f"{dir_60d.name} ({ages.get('60d', '?')}일 경과)" if dir_60d else "없음",
             _ok(dir_60d is not None)),
        _row("재학습 필요",
             f"{'예 — ' + ', '.join(rts.get('retrain_due_models', [])) if rts.get('retrain_due') else '아니요 (90일 미경과)'}",
             _ok(retrain_ok, "✅", "⚠️")),
        _row("G2 레짐 게이트",
             gate.get("reason", "?"),
             "🔴" if gate_blocked else "✅"),
        _row("마지막 paper_trades",
             _last_paper, "📊"),
        _row("파이프라인 마지막 완료",
             _pipe_last, "⚙️"),
    ])

    overall_ok = stale_ok and model_ok
    banner_color = "#1a3a1a" if overall_ok else "#3a1a1a"
    banner_text = "✅ 정상 운영 중" if overall_ok else "⚠️ 주의 필요"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>운영 상태 — Stock Game</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#0f172a; color:#e2e8f0; margin:0; padding:1.5rem; }}
  h1 {{ font-size:1.25rem; margin:0 0 0.25rem; }}
  .banner {{ background:{banner_color}; border:1px solid #334155; border-radius:8px;
             padding:0.75rem 1rem; margin-bottom:1.25rem; font-size:1.1rem; font-weight:600; }}
  .ts {{ color:#64748b; font-size:0.8rem; margin-bottom:1.5rem; }}
  table {{ border-collapse:collapse; width:100%; max-width:680px; }}
  th,td {{ padding:0.55rem 0.9rem; border-bottom:1px solid #1e293b; text-align:left; font-size:0.9rem; }}
  th {{ color:#94a3b8; font-weight:500; background:#0f172a; }}
  td:first-child {{ color:#94a3b8; width:200px; }}
  td:last-child {{ color:#e2e8f0; font-family:monospace; }}
  .section {{ margin-top:1.5rem; }}
  h2 {{ font-size:0.85rem; text-transform:uppercase; letter-spacing:.05em;
        color:#64748b; margin:0 0 0.5rem; }}
  .hint {{ background:#1e293b; border-radius:6px; padding:0.75rem 1rem;
           font-size:0.8rem; color:#94a3b8; margin-top:1.5rem; max-width:680px; }}
  .hint code {{ background:#0f172a; padding:0.1em 0.4em; border-radius:3px; color:#7dd3fc; }}
  a {{ color:#38bdf8; }}
</style>
</head>
<body>
<h1>Stock Game — 운영 상태</h1>
<div class="ts">조회 시각: {now_str} (60초 자동 새로고침)</div>
<div class="banner">{banner_text}</div>

<div class="section">
<h2>현황</h2>
<table>
<thead><tr><th>항목</th><th>상태</th></tr></thead>
<tbody>{rows_data}</tbody>
</table>
</div>

<div class="hint">
<b>이상 시 대응</b><br>
⚠️ prices 지연 → <code>데이터_수동업데이트.bat</code> 더블클릭<br>
🔴 모델 없음 → <code>npx pm2 restart stock-backend --update-env</code><br>
⚠️ 재학습 필요 → <code>주간모델_수동재학습.bat</code> 더블클릭<br>
자세한 절차 → <a href="/docs">API 문서</a> · 프로젝트 루트 <b>운영.md</b>
</div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


# ── 재학습 ────────────────────────────────────────────────────

def _drain_retrain_stdout(proc: subprocess.Popen) -> None:
    """Background thread — continuously drains subprocess pipe so it never deadlocks."""
    try:
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                _state.retrain_log.append(stripped)
                if len(_state.retrain_log) > 200:
                    _state.retrain_log = _state.retrain_log[-200:]
    except Exception:
        logger.exception("재학습 stdout 스트리밍 중 예외 — 로그 수집 중단")
    finally:
        _RETRAIN_PID_FILE.unlink(missing_ok=True)
        rc = proc.poll()
        if rc == 0:
            logger.info("재학습 완료 → 신규 모델 핫 리로드")
            try:
                _reload_model_and_cache()
            except Exception as exc:
                logger.warning("재학습 후 모델 리로드 실패: %s", exc)
        else:
            logger.warning("재학습 실패 (exit=%s) — 기존 모델 유지", rc)


def _retrain_status() -> Dict[str, Any]:
    proc = _state.retrain_proc
    if proc is None:
        # 이 서버가 시작한 프로세스가 없어도 PID 파일로 외부(자동) 재학습 감지
        if _check_retrain_already_running():
            try:
                elapsed = round(time.time() - _RETRAIN_PID_FILE.stat().st_mtime)
            except Exception:
                elapsed = None
            return {
                "status": "running",
                "elapsed_sec": elapsed,
                "log": ["[자동] 백그라운드 재학습 파이프라인 실행 중..."],
            }
        return {"status": "idle", "elapsed_sec": None, "log": []}

    elapsed = round(time.time() - (_state.retrain_started_at or 0))
    rc = proc.poll()
    status = "running" if rc is None else ("done" if rc == 0 else "error")

    return {
        "status": status,
        "elapsed_sec": elapsed,
        "return_code": rc,
        "log": _state.retrain_log[-50:],
    }


_RETRAIN_PID_FILE = BACKEND_ROOT / "retrain.pid"


def _check_retrain_already_running() -> bool:
    """PID 파일로 다른 서버 인스턴스에서 시작한 파이프라인도 감지"""
    if _RETRAIN_PID_FILE.exists():
        try:
            pid = int(_RETRAIN_PID_FILE.read_text().strip())
            import subprocess as _sp
            result = _sp.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True,
            )
            if str(pid) in result.stdout:
                return True
        except Exception:
            pass
        _RETRAIN_PID_FILE.unlink(missing_ok=True)
    return False


@app.post("/api/admin/retrain", summary="모델 재학습 시작")
async def start_retrain(
    quick: bool = Query(default=False, description="빠른 재학습: 데이터 수집 스킵 + 15 trials"),
) -> Dict[str, Any]:
    proc = _state.retrain_proc
    if proc is not None and proc.poll() is None:
        raise HTTPException(status_code=409, detail="이미 재학습이 실행 중입니다.")
    if _check_retrain_already_running():
        raise HTTPException(status_code=409, detail="다른 서버 인스턴스에서 재학습이 실행 중입니다.")

    pipeline_script = BACKEND_ROOT / "run_full_pipeline.py"
    if not pipeline_script.exists():
        raise HTTPException(status_code=500, detail=f"파이프라인 스크립트를 찾을 수 없음: {pipeline_script}")

    cmd = [sys.executable, str(pipeline_script)]
    if quick:
        cmd.append("--quick")

    mode_label = "빠른 재학습" if quick else "전체 재학습"
    _state.retrain_log = [f"[시작] {mode_label} 파이프라인 실행 중..."]
    _state.retrain_started_at = time.time()
    _state.retrain_proc = subprocess.Popen(
        cmd,
        cwd=str(BACKEND_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    _RETRAIN_PID_FILE.write_text(str(_state.retrain_proc.pid))
    threading.Thread(
        target=_drain_retrain_stdout,
        args=(_state.retrain_proc,),
        daemon=True,
    ).start()
    logger.info("%s 시작 (PID=%d)", mode_label, _state.retrain_proc.pid)
    return {"status": "started", "pid": _state.retrain_proc.pid, "quick": quick}


@app.get("/api/admin/retrain/status", summary="재학습 진행 상태")
async def retrain_status() -> Dict[str, Any]:
    return _retrain_status()


# ── 데이터 수동 업데이트 (인앱 버튼, 2026-06-23) ────────────────
# daily_pipeline.py를 --no-server-restart로 실행 — 이 호출을 처리하는 서버 프로세스 자신을
# 죽이면 안 되므로(버튼을 누른 화면이 끊김), 완료 후 _reload_model_and_cache()로 인프로세스
# 핫리로드만 함. 자동 스케줄(평일 16:30)은 비활성화됐고 .bat(비상용)과 이 버튼이 수동 실행의
# 두 경로 — daily_pipeline.py 자체 PID 락파일을 공유해서 어느 쪽으로 실행 중이든 중복 방지.

_UPDATE_LOCK_FILE     = BACKEND_ROOT / "daily_pipeline.pid"   # daily_pipeline.py가 직접 쓰는 락파일
_PIPELINE_STATUS_FILE = BACKEND_ROOT / "pipeline_status.json" # 단계/하트비트 기록 (daily_pipeline.py가 씀)


def _read_pipeline_status() -> Dict[str, Any]:
    """daily_pipeline.py가 기록하는 JSON 상태 파일 — 진행률/하트비트/started_at/reason."""
    try:
        if _PIPELINE_STATUS_FILE.exists():
            data = json.loads(_PIPELINE_STATUS_FILE.read_text(encoding="utf-8"))
            return {
                "current_stage":      data.get("current_stage"),
                "current_stage_name": data.get("current_stage_name"),
                "total_stages":       data.get("total_stages", 11),
                "started_at":         data.get("started_at"),
                "last_heartbeat":     data.get("last_heartbeat"),
                "pipeline_status":    data.get("status"),
                "reason":             data.get("reason"),
            }
    except Exception:
        pass
    return {}


def _read_last_log_lines(n: int = 5) -> List[str]:
    """pipeline_daily.log 마지막 N줄 — 외부 실행 중 로그 미리보기."""
    log_path = BACKEND_ROOT / "pipeline_daily.log"
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [line.rstrip() for line in lines[-n:] if line.strip()]
    except Exception:
        return ["[로그 읽기 실패]"]


def _check_update_already_running() -> bool:
    """daily_pipeline.py의 락파일로 .bat 등 다른 경로의 실행도 감지."""
    if _UPDATE_LOCK_FILE.exists():
        try:
            pid = int(_UPDATE_LOCK_FILE.read_text().strip())
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
            )
            if str(pid) in result.stdout:
                return True
        except Exception:
            pass
    return False


def _get_prices_staleness() -> Dict[str, Any]:
    """prices 테이블 최신일 vs market_index 최신 거래일 비교 — 파이프라인 미실행 감지.

    market_index는 서버 nightly loop이 매일 갱신하므로 "최근 거래일" 기준으로 쓸 수 있음.
    prices는 daily_pipeline 실행 시에만 갱신 — 2거래일 이상 지연 시 stale로 판정.
    """
    with sqlite3.connect(DB_PATH) as conn:
        prices_date = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
        market_date = conn.execute(
            "SELECT MAX(date) FROM market_index WHERE code='1001'"
        ).fetchone()[0]
    stale_trading_days = None
    if prices_date and market_date and prices_date < market_date:
        # market_index 날짜들 중 prices_date 이후 개수 = 지연 거래일 수
        with sqlite3.connect(DB_PATH) as conn:
            stale_trading_days = conn.execute(
                "SELECT COUNT(*) FROM market_index WHERE code='1001' AND date > ?",
                (prices_date,),
            ).fetchone()[0]
    elif prices_date and market_date:
        stale_trading_days = 0
    return {
        "prices_latest_date": prices_date,
        "market_latest_date": market_date,
        "prices_stale_trading_days": stale_trading_days,
        "prices_stale": stale_trading_days is not None and stale_trading_days >= 2,
    }


def _get_retrain_status() -> Dict[str, Any]:
    """모델 디렉토리명에서 학습일 파싱 → 90일 경과 여부 확인 (재학습 표시등).

    디렉토리명 형식: target_5d_YYYYMMDD_HHMMSS / target_60d_YYYYMMDD_HHMMSS
    ACTIVE_{target}.txt 포인터로 서빙 중인 모델만 대상.
    """
    RETRAIN_DAYS = 90
    due_models: List[str] = []
    model_ages: Dict[str, int] = {}

    for horizon in ("5d", "60d"):
        try:
            model_dir = find_latest_model_dir(f"target_{horizon}")
            m = re.search(r"_(\d{8})_\d{6}$", model_dir.name)
            if not m:
                continue
            train_date = datetime.strptime(m.group(1), "%Y%m%d")
            age_days = (datetime.now() - train_date).days
            model_ages[horizon] = age_days
            if age_days >= RETRAIN_DAYS:
                due_models.append(horizon)
        except Exception:
            pass

    return {
        "retrain_due": len(due_models) > 0,
        "retrain_due_models": due_models,
        "model_ages_days": model_ages,
    }


def _get_data_freshness() -> Dict[str, Any]:
    """화면에 항상 표시할 '데이터 기준일' + 외국인비율(flows.foreign_net) staleness.

    [2026-06-30 갱신] PER/PBR(features.per/pbr)은 더 이상 이 경고 대상이 아님 — 룩어헤드
    bias 수정(load_per_pbr_pit, dividends.eps/financials.bps 기반 point-in-time) 이후
    daily_pipeline을 건너뛴 날짜도 다음 피처 업데이트 때 소급 계산되므로 "영구 공백"이
    아니게 됨(연 단위 공시일 lookup이라 "오늘 수집 여부"와 무관). 반면 외국인비율
    (flows.foreign_net)은 여전히 collect_fundamentals_and_flows가 "오늘"만 기록하고
    백필이 없어(CLAUDE.md 이슈#9) 건너뛴 날짜가 그대로 영구 공백으로 남음 — 이 함수는
    이제 외국인비율 단독 기준으로만 경고."""
    with sqlite3.connect(DB_PATH) as conn:
        latest_feature = conn.execute("SELECT MAX(date) FROM features").fetchone()[0]
        latest_flow = conn.execute("SELECT MAX(date) FROM flows").fetchone()[0]
    stale_days = None
    if latest_flow:
        try:
            stale_days = (datetime.now() - datetime.strptime(latest_flow, "%Y%m%d")).days
        except ValueError:
            stale_days = None
    return {
        "latest_feature_date": latest_feature,
        "latest_foreign_rate_date": latest_flow,
        "foreign_rate_stale_days": stale_days,
        "foreign_rate_warning": stale_days is not None and stale_days >= 3,
    }


def _dart_partial_from_log(log_lines: List[str]) -> bool:
    return any(("한도로 중단됨" in line or "한도 도달" in line) for line in log_lines)


def _drain_update_stdout(proc: subprocess.Popen) -> None:
    """Background thread — daily_pipeline.py(--no-server-restart) stdout을 계속 비움."""
    try:
        for line in proc.stdout:
            stripped = line.rstrip()
            if stripped:
                _state.update_log.append(stripped)
                if len(_state.update_log) > 300:
                    _state.update_log = _state.update_log[-300:]
    except Exception:
        logger.exception("데이터 업데이트 stdout 스트리밍 중 예외 — 로그 수집 중단")
    finally:
        rc = proc.poll()
        if rc == 0:
            _state.update_finished_status = "done"
            logger.info("데이터 업데이트 완료 → 모델/캐시 인프로세스 핫 리로드")
            try:
                _reload_model_and_cache()
            except Exception as exc:
                logger.warning("데이터 업데이트 후 핫 리로드 실패(서버는 정상 동작 유지): %s", exc)
        else:
            _state.update_finished_status = "error"
            logger.warning("데이터 업데이트 실패 (exit=%s) — 서버는 영향 없이 정상 동작 유지", rc)


_NULL_STAGE_FIELDS: Dict[str, Any] = {
    "current_stage":      None,
    "current_stage_name": None,
    "total_stages":       None,
    "started_at":         None,
    "last_heartbeat":     None,
}


def _update_status() -> Dict[str, Any]:
    freshness = _get_data_freshness()
    proc = _state.update_proc

    if proc is None:
        # 이 서버 인스턴스가 시작한 건 없어도 락파일로 .bat 등 외부 실행 감지
        if _check_update_already_running():
            ps = _read_pipeline_status()
            started_at = ps.get("started_at")
            # started_at이 없으면(락파일 생성 직후 상태파일 미기록 경합) 락파일 mtime으로 폴백
            if not started_at:
                try:
                    started_at = _UPDATE_LOCK_FILE.stat().st_mtime
                except Exception:
                    pass
            elapsed: int = 0
            if started_at:
                try:
                    elapsed = max(0, round(time.time() - started_at))
                except Exception:
                    pass
            return {
                "status":             "running",
                "elapsed_sec":        elapsed,
                "log":                _read_last_log_lines(5),
                "dart_partial":       False,
                "current_stage":      ps.get("current_stage"),
                "current_stage_name": ps.get("current_stage_name"),
                "total_stages":       ps.get("total_stages", 11),
                "started_at":         started_at,
                "last_heartbeat":     ps.get("last_heartbeat"),
                **freshness,
            }
        # 스케줄러가 자동 스킵한 경우 24시간 내에 배너로 노출
        if _state.update_finished_status is None:
            ps = _read_pipeline_status()
            if ps.get("pipeline_status") == "skipped":
                try:
                    age = time.time() - (ps.get("last_heartbeat") or 0)
                    if age < 86400:
                        return {
                            "status":      "skipped",
                            "reason":      ps.get("reason"),
                            "elapsed_sec": None,
                            "log":         [],
                            "dart_partial": False,
                            **_NULL_STAGE_FIELDS,
                            **freshness,
                        }
                except Exception:
                    pass
        return {
            "status":      _state.update_finished_status or "idle",
            "elapsed_sec": None,
            "log":         _state.update_log[-50:],
            "dart_partial": _dart_partial_from_log(_state.update_log),
            **_NULL_STAGE_FIELDS,
            **freshness,
        }

    rc = proc.poll()
    if rc is None:
        ps = _read_pipeline_status()
        elapsed = round(time.time() - (_state.update_started_at or 0))
        return {
            "status":             "running",
            "elapsed_sec":        elapsed,
            "log":                _state.update_log[-50:],
            "dart_partial":       _dart_partial_from_log(_state.update_log),
            "current_stage":      ps.get("current_stage"),
            "current_stage_name": ps.get("current_stage_name"),
            "total_stages":       ps.get("total_stages", 11),
            "started_at":         _state.update_started_at,
            "last_heartbeat":     ps.get("last_heartbeat"),
            **freshness,
        }

    return {
        "status":      "done" if rc == 0 else "error",
        "elapsed_sec": round(time.time() - (_state.update_started_at or 0)),
        "log":         _state.update_log[-50:],
        "dart_partial": _dart_partial_from_log(_state.update_log),
        **_NULL_STAGE_FIELDS,
        **freshness,
    }


@app.post("/api/update", summary="데이터 수동 업데이트 시작 (인앱 버튼)")
async def start_update() -> Dict[str, Any]:
    proc = _state.update_proc
    if proc is not None and proc.poll() is None:
        raise HTTPException(status_code=409, detail="이미 데이터 업데이트가 실행 중입니다.")
    if _check_update_already_running():
        raise HTTPException(status_code=409, detail="다른 경로(.bat 등)로 데이터 업데이트가 이미 실행 중입니다.")

    pipeline_script = BACKEND_ROOT / "daily_pipeline.py"
    if not pipeline_script.exists():
        raise HTTPException(status_code=500, detail=f"파이프라인 스크립트를 찾을 수 없음: {pipeline_script}")

    cmd = [sys.executable, str(pipeline_script), "--no-server-restart", "--force"]

    _state.update_log = ["[시작] 데이터 업데이트 파이프라인 실행 중..."]
    _state.update_started_at = time.time()
    _state.update_finished_status = None
    _state.update_proc = subprocess.Popen(
        cmd,
        cwd=str(BACKEND_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    threading.Thread(target=_drain_update_stdout, args=(_state.update_proc,), daemon=True).start()
    logger.info("데이터 업데이트 시작 (PID=%d)", _state.update_proc.pid)
    return {"status": "started", "pid": _state.update_proc.pid}


@app.get("/api/update/status", summary="데이터 업데이트 진행 상태 + 최신 데이터 기준일")
async def update_status() -> Dict[str, Any]:
    return _update_status()


@app.get("/api/admin/reanalysis-status", summary="모의투자 재분석 리포트 준비 상태")
async def reanalysis_status() -> Dict[str, Any]:
    """check_reanalysis_ready.py가 생성하는 backend/reanalysis_report.md 존재 여부."""
    report_path = BACKEND_ROOT / "reanalysis_report.md"
    if not report_path.exists():
        return {"available": False}
    return {
        "available": True,
        "generated_at": datetime.fromtimestamp(report_path.stat().st_mtime).isoformat(),
    }


# ── 역사 시뮬레이션 거래 이력 ─────────────────────────────────

class _HistorySyncBody(BaseModel):
    session_id: str
    trades: List[Dict[str, Any]]


@app.post("/api/history/sync", summary="역사 시뮬레이션 거래 이력 동기화")
async def history_sync(body: _HistorySyncBody) -> Dict[str, int]:
    inserted = _sync_trades(body.session_id, body.trades)
    return {"inserted": inserted, "total": len(body.trades)}


@app.get("/api/history/trades", summary="역사 시뮬레이션 거래 이력 조회")
async def history_trades(session_id: str = Query(..., description="브라우저 세션 UUID")) -> List[Dict[str, Any]]:
    return _get_trades(session_id)


# ── 세력 분석 (Gemini) ────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-1.5-flash"
GEMINI_URL     = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"


class _AnalyzeBody(BaseModel):
    symbol: str
    name: str
    trades: List[Dict[str, Any]]  # {type, price, quantity, timestamp, note}


def _build_prompt(name: str, plays: List[Dict]) -> str:
    lines = []
    for i, p in enumerate(plays, 1):
        pnl = p["pnl_pct"]
        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"플레이{i}: {p['buy_date']} {p['buy_price']:,}원 매수 → "
            f"{p['sell_date']} {p['sell_price']:,}원 매도 "
            f"({sign}{pnl:.1f}%, {p['holding_days']}일 보유)"
            + (f" / 매도 다음날: {p['next_day_chg']:+.1f}%" if p.get("next_day_chg") is not None else "")
            + (f" / 메모: {p['note']}" if p.get("note") else "")
        )

    plays_text = "\n".join(lines)
    total_pnl  = sum(p["pnl_pct"] for p in plays)
    wins       = sum(1 for p in plays if p["pnl_pct"] > 0)

    return f"""너는 주식 작전 세력 텔레그램방 멤버야.
팀장, 부팀장, 감시자 3명이 개미 투자자의 매매를 실시간으로 보면서 대화하는 형식으로 써줘.
말투는 짧고 비밀스럽게. 각 플레이마다 반응해줘.
개미가 잘 한 플레이엔 세력이 당황하거나 감탄하고,
못 한 플레이엔 세력이 기뻐하거나 비웃어.

종목: {name}
전체 {len(plays)}번 거래, {wins}승 {len(plays)-wins}패, 합산 수익률 {total_pnl:+.1f}%

거래 기록:
{plays_text}

각 플레이를 날짜 순서대로 대화로 써줘.
형식: [날짜]\n팀장: ...\n감시자: ...\n부팀장: ...
너무 길지 않게, 각 플레이당 3~4줄로."""


@app.post("/api/history/analyze", summary="세력 텔레그램방 AI 생성")
async def analyze_history(body: _AnalyzeBody) -> Dict[str, Any]:
    import urllib.request, json as _json
    from datetime import datetime, timezone

    trades = sorted(body.trades, key=lambda t: t["timestamp"])
    buys, plays = [], []

    for t in trades:
        if t["type"] == "buy":
            buys.append(t)
        elif buys:
            b = buys.pop(0)
            buy_dt  = datetime.fromtimestamp(b["timestamp"] / 1000, tz=timezone.utc)
            sell_dt = datetime.fromtimestamp(t["timestamp"]  / 1000, tz=timezone.utc)
            holding = (t["timestamp"] - b["timestamp"]) // (1000 * 60 * 60 * 24)
            pnl_pct = (t["price"] - b["price"]) / b["price"] * 100
            plays.append({
                "buy_date":  buy_dt.strftime("%m/%d"),
                "sell_date": sell_dt.strftime("%m/%d"),
                "buy_price":  b["price"],
                "sell_price": t["price"],
                "holding_days": holding,
                "pnl_pct": pnl_pct,
                "note": t.get("note") or b.get("note"),
            })

    if not plays:
        return {"text": "거래 기록이 없습니다."}

    prompt = _build_prompt(body.name, plays)
    payload = _json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.95, "maxOutputTokens": 1200},
    }).encode()

    req = urllib.request.Request(
        GEMINI_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = _json.loads(res.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return {"text": text}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini 오류: {exc}")


# ── ElevenLabs TTS ────────────────────────────────────────────

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

# 캐릭터별 목소리
ELEVENLABS_VOICES = {
    "boss":   "pNInz6obpgDQGcFmaJgB",  # Adam   - Dominant, Firm
    "deputy": "N2lVS1w4EtoT3dr4eOWO",  # Callum - Husky Trickster
    "spy":    "SAz9YHcvj6GT2YYXdXww",  # River  - Relaxed, Neutral
}


_ALLOWED_SPEAKERS = {"boss", "deputy", "spy"}

class _TtsBody(BaseModel):
    text: str
    speaker: str  # boss | deputy | spy


@app.post("/api/tts", summary="ElevenLabs TTS")
async def tts(body: _TtsBody) -> Response:
    import urllib.request, json as _json

    if len(body.text) > 500:
        raise HTTPException(status_code=400, detail="text too long (max 500 chars)")
    if body.speaker not in _ALLOWED_SPEAKERS:
        raise HTTPException(status_code=400, detail=f"invalid speaker: {body.speaker}")

    voice_id = ELEVENLABS_VOICES.get(body.speaker, ELEVENLABS_VOICES["spy"])
    payload  = _json.dumps({
        "text": body.text.replace("\n", " "),
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.80, "style": 0.15},
    }).encode()

    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=payload,
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            audio = res.read()
        return Response(content=audio, media_type="audio/mpeg")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ElevenLabs 오류: {exc}")


# ── 모의투자 ──────────────────────────────────────────────────

@app.post("/api/paper/record", summary="오늘 추천을 모의투자 테이블에 기록")
async def paper_record(
    date: Optional[str] = Query(default=None, description="기준일 YYYYMMDD (미지정=최신)"),
    top_n: int = Query(default=10, ge=1, le=100, description="기록할 종목 수"),
) -> Dict[str, Any]:
    _require_model()
    target_date = _get_date(date)

    cached, _, _used_bear = _get_predictions_for_date(target_date)
    top = cached[:top_n]
    _mkt = _get_market_trend_cached(target_date)
    _mode, _, _, _ = _compute_market_mode(_mkt)
    _guard_enabled = os.environ.get("BEAR_MARKET_GUARD", "true").lower() != "false"
    _guard_active = get_kospi_bear_signal().get("guard_active", False) if _guard_enabled else False
    _model_ver = (_state.bear_model_dir if _used_bear else _state.model_dir)
    _gate = _get_regime_gate_cached(target_date)
    _gate_blocked = _gate.get("gate_blocked", False)
    if _gate_blocked:
        from prediction_logger import log_shadow_predictions
        log_shadow_predictions(target_date, "5d", top, horizon=5)
        return {
            "date":            target_date,
            "inserted":        0,
            "skipped":         len(top),
            "regime_gate":     "blocked",
            "regime_gate_reason": _gate.get("reason", ""),
        }
    inserted = record_recommendations(
        target_date, top,
        market_mode=_mode,
        bear_guard_active=bool(_guard_active),
        model_version=_model_ver.name if _model_ver else None,
        regime_gate_blocked=False,
    )
    return {
        "date":        target_date,
        "inserted":    inserted,
        "skipped":     len(top) - inserted,
        "regime_gate": "open",
    }


@app.post("/api/paper/close-expired", summary="5거래일 경과 모의투자 자동 청산")
async def paper_close_expired() -> Dict[str, int]:
    from datetime import date as _date
    today = _state.latest_date or _date.today().strftime("%Y%m%d")
    return close_expired_trades(today)


@app.get("/api/paper/active", summary="현재 보유 중인 모의투자 종목")
async def paper_active() -> List[Dict[str, Any]]:
    from datetime import date as _date
    today = _state.latest_date or _date.today().strftime("%Y%m%d")
    return get_active_trades_enriched(today)


@app.get("/api/paper/performance", summary="모의투자 누적 성과 요약")
async def paper_performance() -> Dict[str, Any]:
    return paper_performance_summary()


@app.get("/api/paper/performance-timeline", summary="추천일별 성과 추이")
async def paper_performance_timeline_endpoint(
    days: int = Query(default=30, ge=1, le=365, description="집계 기간 (일)"),
    since: Optional[str] = Query(default=None, description="집계 시작일 YYYYMMDD (days보다 우선)"),
) -> Dict[str, Any]:
    return paper_performance_timeline(days=days, since=since)


@app.get("/api/paper/performance-by-model", summary="모델 버전별 성과 분리 집계")
async def paper_performance_by_model_endpoint() -> Dict[str, Any]:
    """model_version별로 거래를 그룹화해 승률/평균수익/누적수익을 따로 계산.
    현재 운영 모델(ACTIVE 포인터)·과거 모델·폐기 모델(rejected_)·추적 불가(NULL)를 구분."""
    return paper_performance_by_model()


@app.get("/api/paper/open-summary", summary="현재 열린 60d 포지션 현황 요약")
async def paper_open_summary() -> Dict[str, Any]:
    """열린 포지션의 수, 평균 미실현 손익, 만기까지 남은 거래일, 최고/최저 종목 반환."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            positions = conn.execute(
                "SELECT symbol, recommended_date, recommended_price, horizon "
                "FROM paper_trades WHERE status='open' ORDER BY recommended_date"
            ).fetchall()

            if not positions:
                return {"count": 0, "positions": [], "avg_pnl_pct": None, "best": None, "worst": None}

            symbols = [p["symbol"] for p in positions]
            placeholders = ",".join("?" * len(symbols))
            latest_prices = {
                row[0]: row[1]
                for row in conn.execute(
                    f"SELECT p.symbol, p.close FROM prices p "
                    f"INNER JOIN (SELECT symbol, MAX(date) AS md FROM prices WHERE symbol IN ({placeholders}) GROUP BY symbol) m "
                    f"ON p.symbol=m.symbol AND p.date=m.md",
                    symbols,
                ).fetchall()
            }

            # 남은 거래일: 추천일 이후 market_index 거래일 수 계산
            trading_dates = [
                r[0] for r in conn.execute(
                    "SELECT date FROM market_index WHERE code='1001' ORDER BY date"
                ).fetchall()
            ]
            trading_set = set(trading_dates)

            today_str = trading_dates[-1] if trading_dates else ""

            def days_elapsed(from_date: str) -> int:
                try:
                    idx_from = trading_dates.index(from_date) if from_date in trading_set else None
                    idx_to = trading_dates.index(today_str) if today_str in trading_set else None
                    if idx_from is None or idx_to is None:
                        return 0
                    return max(0, idx_to - idx_from)
                except Exception:
                    return 0

            result_positions = []
            for p in positions:
                sym = p["symbol"]
                rec_price = p["recommended_price"] or 0
                cur_price = latest_prices.get(sym)
                pnl_pct = round((cur_price - rec_price) / rec_price * 100, 2) if cur_price and rec_price else None
                elapsed = days_elapsed(p["recommended_date"])
                remaining = max(0, (p["horizon"] or 60) - elapsed)

                name_row = conn.execute("SELECT name FROM stocks WHERE symbol=?", (sym,)).fetchone()
                name = name_row["name"] if name_row else sym

                result_positions.append({
                    "symbol": sym,
                    "name": name,
                    "recommended_date": p["recommended_date"],
                    "horizon": p["horizon"],
                    "elapsed_days": elapsed,
                    "remaining_days": remaining,
                    "recommended_price": rec_price,
                    "current_price": cur_price,
                    "pnl_pct": pnl_pct,
                })

            pnl_values = [pos["pnl_pct"] for pos in result_positions if pos["pnl_pct"] is not None]
            avg_pnl = round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else None

            sorted_by_pnl = sorted(
                [p for p in result_positions if p["pnl_pct"] is not None],
                key=lambda x: x["pnl_pct"],
            )
            best = sorted_by_pnl[-1] if sorted_by_pnl else None
            worst = sorted_by_pnl[0] if sorted_by_pnl else None

            return {
                "count": len(result_positions),
                "avg_pnl_pct": avg_pnl,
                "best": best,
                "worst": worst,
                "positions": result_positions,
            }
    except Exception as exc:
        logger.exception("open-summary 오류: %s", exc)
        return {"count": 0, "positions": [], "avg_pnl_pct": None, "best": None, "worst": None}


@app.get("/api/paper/shadow-vs-live", summary="G2 게이트 on(실제) vs off(shadow) 성과 비교")
async def shadow_vs_live_performance() -> Dict[str, Any]:
    """shadow_prediction_log vs prediction_log 성과 비교.

    G2 게이트가 없었다면 차단일에도 진입했을 때의 수익과,
    실제 기록된 진입 수익을 나란히 비교한다.
    n<10이면 {"status": "collecting"} 반환.
    """
    try:
        import sys as _sys
        _parent = Path(__file__).parent
        if str(_parent) not in _sys.path:
            _sys.path.insert(0, str(_parent))
        from prediction_logger import get_shadow_vs_live_performance
        return get_shadow_vs_live_performance()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/live-performance", summary="prediction_log 기반 라이브 실전 성과")
async def live_performance() -> Dict[str, Any]:
    """prediction_log + prediction_outcomes에서 집계한 실전 추적 성과.

    5d: top10 hit rate (종가+5% 달성률 P@10)
    60d: top10 평균 초과수익률 (KOSPI 대비)
    n<30이면 {"status": "collecting"} 반환.
    """
    try:
        import sys as _sys
        _parent = Path(__file__).parent
        if str(_parent) not in _sys.path:
            _sys.path.insert(0, str(_parent))
        from prediction_logger import get_live_performance
        return get_live_performance()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"라이브 성과 조회 실패: {exc}")


@app.get("/api/volume-anomaly", summary="거래량 이상 급등 스캐너 (가격 소폭, 거래량 폭등)")
async def get_volume_anomaly(
    vol_min: float = Query(default=5.0, ge=2.0, description="20일 평균 대비 최소 거래량 배율"),
    price_max: float = Query(default=0.05, ge=0.0, description="1일 최대 가격 변동률"),
    ret5d_max: float = Query(default=0.25, ge=0.0, description="5일 최대 수익률 (이미 많이 오른 종목 제외)"),
) -> Dict[str, Any]:
    """
    거래량은 폭등했지만 가격은 아직 크게 안 오른 종목을 스캔.
    '현대오토에버형' 패턴: 스마트머니 축적 신호로 해석 가능.
    ETN/채권ETF 제외, 순수 주식만 반환.
    """
    db_path = Path(__file__).parent.parent / "data" / "stocks.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ETN은 종목코드가 5~8로 시작하는 경향, 채권ETF 제외
    # 이름에 ETF/ETN/채권 관련 키워드가 없는 순수 주식만
    etf_keywords = [
        'RISE ', 'PLUS ', 'TIGER ', 'KODEX ', 'KBSTAR ', 'HANARO ', 'ACE ',
        'KOSEF ', 'ARIRANG ', 'SOL ', 'FOCUS ', 'TIMEFOLIO ', 'TREX ',
        'WON ', 'VITA ', 'HK ', 'N2 ', 'KB ', ' ETF', 'ETN(', 'ETN ',
        '채권', '국채', '머니마켓', '레버리지', '인버스', '선물 ', '선물(',
        '헤지', '특수채', '액티브', '베트남VN', '일본 국채',
    ]
    name_filter = ' AND '.join([f"s.name NOT LIKE '%{kw}%'" for kw in etf_keywords])
    # ETN 코드 범위 제외 (5, 6, 7, 8로 시작하는 6자리)
    code_filter = "AND (CAST(f.symbol AS INTEGER) < 500000)"

    query = f"""
        SELECT f.symbol, s.name, s.market, s.sector,
               f.vol_ratio_20d, f.vol_ratio_5d,
               f.vol_ratio_lag_1, f.vol_ratio_lag_3, f.vol_ratio_lag_5,
               f.ret_1d, f.ret_5d, f.ret_20d, f.ret_60d,
               f.per
        FROM features f
        JOIN stocks s ON f.symbol = s.symbol
        WHERE f.date = (SELECT MAX(date) FROM features)
          AND f.vol_ratio_20d >= ?
          AND ABS(f.ret_1d) <= ?
          AND f.ret_5d < ?
          AND f.per > 0
          AND {name_filter}
          {code_filter}
        ORDER BY f.vol_ratio_20d DESC
        LIMIT 50
    """
    cur.execute(query, (vol_min, price_max, ret5d_max))
    rows = cur.fetchall()
    conn.close()

    result = []
    for r in rows:
        freshness = "fresh" if (r["vol_ratio_lag_1"] or 0) >= 2.0 else "normal"
        per = r["per"]
        profit = "흑자" if (per is not None and per > 0) else "적자"
        result.append({
            "symbol":          r["symbol"],
            "name":            r["name"],
            "market":          r["market"],
            "sector":          r["sector"],
            "vol_ratio_20d":   round(r["vol_ratio_20d"], 1) if r["vol_ratio_20d"] else None,
            "vol_ratio_5d":    round(r["vol_ratio_5d"], 1) if r["vol_ratio_5d"] else None,
            "vol_ratio_lag_1": round(r["vol_ratio_lag_1"], 1) if r["vol_ratio_lag_1"] else None,
            "vol_ratio_lag_3": round(r["vol_ratio_lag_3"], 1) if r["vol_ratio_lag_3"] else None,
            "ret_1d":          round(r["ret_1d"], 4) if r["ret_1d"] is not None else None,
            "ret_5d":          round(r["ret_5d"], 4) if r["ret_5d"] is not None else None,
            "ret_20d":         round(r["ret_20d"], 4) if r["ret_20d"] is not None else None,
            "signal":          freshness,
        })

    return {
        "date":   _state.latest_date,
        "count":  len(result),
        "stocks": result,
        "params": {"vol_min": vol_min, "price_max": price_max, "ret5d_max": ret5d_max},
    }


@app.get("/api/alltime-volume-surge", summary="역대 거래량 상위 N위 이내 + 소폭 상승 종목 (최근 N일 범위)")
async def get_alltime_volume_surge(
    rank_max:  int   = Query(default=10, ge=1, le=50,  description="역대 거래량 순위 상한"),
    ret_min:   float = Query(default=0.0,               description="급등일 최소 등락률"),
    ret_max:   float = Query(default=0.15,              description="급등일 최대 등락률"),
    min_days:  int   = Query(default=60,                description="최소 상장 거래일 수"),
    lookback:  int   = Query(default=5, ge=1, le=30,   description="탐색 범위 (최근 N 거래일)"),
) -> Dict[str, Any]:
    """
    최근 lookback 거래일 안에 역대 거래량 순위가 rank_max 이내였던 종목 스캔.
    오늘뿐 아니라 최근 며칠 내 역사적 거래량 급등 + 소폭 상승 패턴 탐지.
    """
    db_path = Path(__file__).parent.parent / "data" / "stocks.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    latest = _state.latest_date or ""

    etf_conditions = " AND ".join([
        "s.name NOT LIKE '%RISE %'", "s.name NOT LIKE '%PLUS %'",
        "s.name NOT LIKE '%TIGER %'", "s.name NOT LIKE '%KODEX %'",
        "s.name NOT LIKE '%HANARO%'", "s.name NOT LIKE '%ACE %'",
        "s.name NOT LIKE '%KOSEF%'", "s.name NOT LIKE '%ARIRANG%'",
        "s.name NOT LIKE '%SOL %'", "s.name NOT LIKE '%VITA %'",
        "s.name NOT LIKE '%HK %'", "s.name NOT LIKE '%KB %'",
        "s.name NOT LIKE '% ETF%'", "s.name NOT LIKE '%ETN(%'",
        "s.name NOT LIKE '%채권%'", "s.name NOT LIKE '%국채%'",
        "s.name NOT LIKE '%레버리지%'", "s.name NOT LIKE '%인버스%'",
        "s.name NOT LIKE '%액티브%'", "s.name NOT LIKE '%머니%'",
        "s.name NOT LIKE '%선물%'", "s.name NOT LIKE '%특수채%'",
        "CAST(c.symbol AS INTEGER) < 500000",
    ])

    # 기존엔 RANK() OVER (PARTITION BY symbol)을 전체 prices(약 4백만 행)에 돌려서
    # 17초 이상 걸렸음. 실제로 필요한 건 "최근 N일" 후보 행(symbol 수 × lookback,
    # 약 2만 행)의 역대 순위뿐이므로, 이 후보 행들에 대해서만 "거래량이 더 큰 날이
    # 몇 개인지"를 인덱스 기반 correlated subquery로 rank_max까지만 캡핑해서 계산 —
    # 결과는 RANK() 윈도우 함수와 동일(직접 대조 검증함), 0.5초 이내로 단축됨.
    query = f"""
        WITH recent_dates AS (
            SELECT DISTINCT date FROM prices
            WHERE date <= ?
            ORDER BY date DESC
            LIMIT ?
        ),
        recent_rows AS (
            SELECT p.symbol, p.date, p.volume, p.open, p.close
            FROM prices p
            JOIN recent_dates rd ON p.date = rd.date
        ),
        -- 최근 N일 각 행의 역대 순위 (rank_max보다 크면 캡핑되어 정확한 값이 필요 없음)
        ranked AS (
            SELECT r.symbol, r.date, r.volume, r.open, r.close,
                   1 + (
                       SELECT COUNT(*) FROM (
                           SELECT 1 FROM prices p2
                           WHERE p2.symbol = r.symbol AND p2.volume > r.volume
                           LIMIT ?
                       )
                   ) AS vol_rank
            FROM recent_rows r
        ),
        totals AS (
            SELECT symbol, COUNT(*) AS total_days FROM prices GROUP BY symbol
        ),
        -- 최근 N일 안에 역대 상위 N위 이내였던 종목 (종목당 최고 순위 기준)
        candidates AS (
            SELECT rk.symbol,
                   MIN(rk.vol_rank) AS best_rank,
                   MAX(t.total_days) AS total_days
            FROM ranked rk
            JOIN totals t ON t.symbol = rk.symbol
            WHERE rk.vol_rank <= ?
              AND (rk.close - rk.open) * 1.0 / NULLIF(rk.open, 0) > ?
              AND (rk.close - rk.open) * 1.0 / NULLIF(rk.open, 0) < ?
              AND t.total_days >= ?
            GROUP BY rk.symbol
        ),
        -- best_rank를 달성한 날짜 (동점이면 가장 최근)
        surge_dates AS (
            SELECT rk.symbol, MAX(rk.date) AS surge_date
            FROM ranked rk
            JOIN candidates c ON rk.symbol = c.symbol
            WHERE rk.vol_rank = c.best_rank
            GROUP BY rk.symbol
        )
        SELECT c.symbol, s.name, s.market, s.sector,
               c.best_rank, c.total_days, sd.surge_date AS surge_date_last,
               surge.volume AS surge_volume,
               surge.close  AS surge_close,
               (surge.close - surge.open) * 1.0 / NULLIF(surge.open, 0) AS surge_ret,
               latest_p.close AS latest_close,
               (latest_p.close - surge.close) * 1.0 / NULLIF(surge.close, 0) AS ret_since_surge,
               feat.per AS per
        FROM candidates c
        JOIN stocks s ON c.symbol = s.symbol
        JOIN surge_dates sd ON sd.symbol = c.symbol
        JOIN ranked surge ON surge.symbol = c.symbol
            AND surge.date = sd.surge_date
            AND surge.vol_rank = c.best_rank
        LEFT JOIN prices latest_p ON latest_p.symbol = c.symbol AND latest_p.date = ?
        LEFT JOIN features feat ON feat.symbol = c.symbol AND feat.date = ?
        WHERE {etf_conditions}
        ORDER BY c.best_rank ASC,
                 (surge.close - surge.open) * 1.0 / NULLIF(surge.open, 0) ASC
        LIMIT 50
    """
    cur.execute(query, (latest, lookback, rank_max, rank_max, ret_min, ret_max, min_days, latest, latest))
    rows = cur.fetchall()
    conn.close()

    result = []
    for r in rows:
        surge_ret = r["surge_ret"] or 0
        days_ago = 0
        # surge_date_last와 latest의 거래일 차이 계산 (근사)
        try:
            from datetime import datetime
            d1 = datetime.strptime(r["surge_date_last"], "%Y%m%d")
            d2 = datetime.strptime(latest, "%Y%m%d")
            days_ago = (d2 - d1).days
        except Exception:
            pass

        result.append({
            "symbol":         r["symbol"],
            "name":           r["name"],
            "market":         r["market"],
            "sector":         r["sector"],
            "vol_rank":       r["best_rank"],
            "total_days":     r["total_days"],
            "surge_date":     r["surge_date_last"],
            "days_ago":       days_ago,
            "surge_volume":   r["surge_volume"],
            "surge_close":    r["surge_close"],
            "surge_ret":      round(surge_ret, 4),
            "latest_close":   r["latest_close"],
            "ret_since_surge": round(r["ret_since_surge"] or 0, 4),
            "signal_strength": "극강" if r["best_rank"] <= 3 else "강" if r["best_rank"] <= 6 else "유의",
            "per":             r["per"],
        })

    return {
        "date":     latest,
        "lookback": lookback,
        "count":    len(result),
        "stocks":   result,
        "params":   {"rank_max": rank_max, "ret_min": ret_min, "ret_max": ret_max, "lookback": lookback},
    }


# ── 스크리너 (2026-06-21) ───────────────────────────────────────
# backend/ml/factor_screen_validation.py의 워크포워드 검증 결과를 그대로 반영.
# ✅ 검증됨: 고배당 상위 (IS/OOS 모두 양(+), 다중비교 보정 통과)
# ⚠️ 시장국면 조건부: RSI 과매도/볼린저 하단 (약세·횡보장에서만 유의, 강세장에서는 무효)
# ❓ 미검증/가짜로 판명: 거래량급증/모멘텀상위/52주신고가(IS·OOS 부호 반전), PER/PBR(데이터 6주뿐)
_SCREENER_ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO|PLUS|FOCUS|WON|VITA)\s',
    re.IGNORECASE,
)


def _compute_near_high_map(target_date: str) -> Dict[str, bool]:
    """종목별 종가가 252거래일 최고가의 95% 이상인지. 매일 1회만 계산(252일 롤링이라 가벼운 연산 아님)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT symbol, date, close FROM prices WHERE date <= ? ORDER BY symbol, date",
            (target_date,),
        ).fetchall()
    by_symbol: Dict[str, List[Tuple[str, float]]] = {}
    for sym, date, close in rows:
        by_symbol.setdefault(sym, []).append((date, close))

    result: Dict[str, bool] = {}
    for sym, series in by_symbol.items():
        window = series[-252:]
        if len(window) < 200 or window[-1][0] != target_date:
            continue
        closes = [c for _, c in window]
        high_252 = max(closes)
        if high_252 > 0:
            result[sym] = closes[-1] >= 0.95 * high_252
    return result


def _get_near_high_cached(target_date: str) -> Dict[str, bool]:
    if _state.near_high_cache is None or _state.near_high_date != target_date:
        _state.near_high_cache = _compute_near_high_map(target_date)
        _state.near_high_date = target_date
    return _state.near_high_cache


def _compute_60d_factors_map(target_date: str) -> Dict[str, Dict]:
    """60d 모델 PIT 팩터(EPS성장/ROE/BPS성장) 전종목 계산. 스크리너 필터/표시용."""
    from factors_60d import compute_factors_for_date  # predictor import 시 ml/ 경로 등록됨
    import math
    with sqlite3.connect(DB_PATH) as conn:
        df = compute_factors_for_date(target_date, conn)
    result: Dict[str, Dict] = {}
    for _, row in df.iterrows():
        def _safe(v):
            try:
                f = float(v)
                return None if math.isnan(f) else round(f, 4)
            except (TypeError, ValueError):
                return None
        result[str(row["symbol"]).zfill(6)] = {
            "eps_growth_yoy":   _safe(row.get("eps_growth_yoy")),
            "eps_growth_accel": _safe(row.get("eps_growth_accel")),
            "roe_level":        _safe(row.get("roe_level")),
            "bps_growth_yoy":   _safe(row.get("bps_growth_yoy")),
        }
    return result


def _get_60d_factors_cached(target_date: str) -> Dict[str, Dict]:
    if _state.factors_60d_cache is None or _state.factors_60d_date != target_date:
        _state.factors_60d_cache = _compute_60d_factors_map(target_date)
        _state.factors_60d_date = target_date
    return _state.factors_60d_cache


def _compute_scores_60d_map(target_date: str) -> Dict[str, float]:
    """60d 모델 전체 종목 추론 (~2,800종목). 스크리너 상위 N% 필터 활성 시만 호출 (초기 ~3초)."""
    preds = predict_60d(target_date, top_n=3000)
    return {p["symbol"]: float(p["probability"]) for p in preds}


def _get_scores_60d_cached(target_date: str) -> Dict[str, float]:
    if _state.scores_60d_cache is None or _state.scores_60d_date != target_date:
        _state.scores_60d_cache = _compute_scores_60d_map(target_date)
        _state.scores_60d_date = target_date
    return _state.scores_60d_cache


def _compute_vol20d_map(target_date: str) -> Dict[str, float]:
    """종목별 20일 평균 거래대금(close×volume). 유동성 필터/표시용."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT p.symbol, AVG(CAST(p.close AS REAL) * p.volume) AS avg_vol_krw
            FROM prices p
            WHERE p.date IN (
                SELECT date FROM market_index WHERE date <= ? ORDER BY date DESC LIMIT 20
            )
            GROUP BY p.symbol
            HAVING COUNT(*) >= 10
            """,
            (target_date,),
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def _get_vol20d_cached(target_date: str) -> Dict[str, float]:
    if _state.vol20d_cache is None or _state.vol20d_date != target_date:
        _state.vol20d_cache = _compute_vol20d_map(target_date)
        _state.vol20d_date = target_date
    return _state.vol20d_cache


def _load_split_correction_factors() -> Dict[Tuple[str, int], float]:
    """배당 분할/병합 비율 보정값 로드 (backend/ml/dividend_split_correction_factors.csv —
    backend/ml/dividend_split_correction.py가 생성, 검증 경로 load_dividend_yield_pit_corrected()와
    동일 출처). 종목코드 앞자리 0 손실 버그(pandas read_csv가 정수로 잘못 추론하는 것과
    같은 계열, 2026-06-26 발견)를 피하려고 zfill(6)을 명시 적용. 파일 없으면 빈 dict
    (보정 없음 = 기존 동작과 동일하게 안전 폴백)."""
    path = BACKEND_ROOT / "ml" / "dividend_split_correction_factors.csv"
    factors: Dict[Tuple[str, int], float] = {}
    if not path.exists():
        return factors
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    sym = row["symbol"].strip().zfill(6)
                    biz_year = int(row["biz_year"])
                    factor = float(row["correction_factor"])
                except (KeyError, ValueError):
                    continue
                factors[(sym, biz_year)] = factor
    except Exception:
        logger.warning("배당 분할보정 계수 로드 실패 — 보정 없이 진행", exc_info=True)
        return {}
    return factors


def _compute_dps_map() -> Dict[str, float]:
    """종목별 가장 최근 공시된 DPS(주당배당금, dividends.dps). 자주 안 바뀌므로 날짜별 캐시.
    배당수익률은 더 이상 dividends.dividend_yield(DART 공시 자체값)를 안 믿고 이 DPS를
    실제 시장가로 나눠 직접 계산함(2026-06-23) — 한국내화(010040) 등 일부 종목의 DART
    공시 "현금배당수익률"이 시장가 대신 액면가를 분모로 써서 20%처럼 부풀려진 사례 발견
    (실제 DPS÷종가는 4.6%인데 공시값은 DPS÷액면가=20.0%). 49건 중 18건이 이 패턴과
    정확히 일치 — 우리 파싱 버그가 아니라 DART 원본 공시 자체의 오류로 확인됨.

    [2026-06-26] 분할/병합 비율 보정 추가 — 배당 공시 *이후* 주식분할·병합·무상증자·감자를
    한 종목은 (미조정)DPS와 (분할조정된)현재 종가의 스케일이 어긋나 수익률이 부풀려짐
    (미원화학 10배, INVENI 5배 등). 검증 경로(backend/ml/dividend_split_correction.py,
    dividend_yield_pit_validation.py의 load_dividend_yield_pit())와 동일한 보정계수를
    적용해 화면에 보이는 스크리너 고배당 필터의 정확도를 검증 결과와 일치시킴.

    [2026-07-30] 스테일 DPS 필터 추가 — MAX(biz_year)에 recency 제한 없으면 배당을 수년 전
    중단한 종목(엑세바이오 2022년, 씨앤투스 2021년 등)이 현재가 급락 후에도 옛 DPS로
    40%+ 허수 수익률을 표시함. biz_year >= current_year-2 조건으로 구 DPS 자동 제외.

    [2026-07-30] 서브쿼리 dps NULL 제외 — 서브쿼리 MAX(biz_year) 계산 시 dps=NULL인 행
    (오리온홀딩스 2025년 등, 공시 지연으로 biz_year 행은 있으나 DPS 미집계)이 포함되면
    그 biz_year가 최신으로 선택되고 → 메인 WHERE의 dps IS NOT NULL에서 제외돼 전체 누락.
    직전 유효 biz_year(2024)의 dps도 같이 제외되는 이중 버그. 서브쿼리에도 동일 조건 추가."""
    min_biz_year = datetime.now().year - 2  # 2026 기준 2024 이상만 허용
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT symbol, biz_year, dps FROM dividends d1
            WHERE dps IS NOT NULL AND dps < 1000000
              AND biz_year >= :min_biz_year
              AND biz_year = (SELECT MAX(biz_year) FROM dividends d2
                               WHERE d2.symbol = d1.symbol AND d2.biz_year >= :min_biz_year
                                 AND d2.dps IS NOT NULL AND d2.dps < 1000000)
            """,
            {"min_biz_year": min_biz_year}
        ).fetchall()
    factors = _load_split_correction_factors()
    return {
        str(sym).zfill(6): dps * factors.get((str(sym).zfill(6), biz_year), 1.0)
        for sym, biz_year, dps in rows
    }


def _get_dps_cached(target_date: str) -> Dict[str, float]:
    if _state.dps_cache is None or _state.dps_date != target_date:
        _state.dps_cache = _compute_dps_map()
        _state.dps_date = target_date
    return _state.dps_cache


def _compute_special_div_map() -> Dict[str, bool]:
    """payout_ratio > 100%인 종목 플래그 — 특별배당 포함 가능성 표시용. 모델/필터 미개입.
    _compute_dps_map()과 동일한 biz_year 선택 로직 사용 (서브쿼리 dps NULL 제외 포함)."""
    min_biz_year = datetime.now().year - 2
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT symbol, payout_ratio FROM dividends d1
            WHERE dps IS NOT NULL AND dps < 1000000
              AND biz_year >= :min_biz_year
              AND biz_year = (SELECT MAX(biz_year) FROM dividends d2
                               WHERE d2.symbol = d1.symbol AND d2.biz_year >= :min_biz_year
                                 AND d2.dps IS NOT NULL AND d2.dps < 1000000)
            """,
            {"min_biz_year": min_biz_year}
        ).fetchall()
    return {
        str(sym).zfill(6): bool(pr is not None and pr > 100)
        for sym, pr in rows
    }


def _get_special_div_cached(target_date: str) -> Dict[str, bool]:
    if _state.special_div_cache is None or _state.special_div_date != target_date:
        _state.special_div_cache = _compute_special_div_map()
        _state.special_div_date = target_date
    return _state.special_div_cache


def _compute_split_adjusted_map() -> Dict[str, bool]:
    """CF ≠ 1.0 종목 플래그 — 주식병합/분할 보정이 적용된 배당수익률 표시용. 모델/필터 미개입.
    payout_ratio=None(EPS음수)인 경우에도 병합환산 상황을 감지 (웅진씽크빅 사례).
    has_special_dividend(payout>100%)와 독립적으로 병기됨.
    _compute_dps_map()과 동일한 biz_year 선택 로직 사용 (서브쿼리 dps NULL 제외 포함)."""
    min_biz_year = datetime.now().year - 2
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT symbol, biz_year FROM dividends d1
            WHERE dps IS NOT NULL AND dps < 1000000
              AND biz_year >= :min_biz_year
              AND biz_year = (SELECT MAX(biz_year) FROM dividends d2
                               WHERE d2.symbol = d1.symbol AND d2.biz_year >= :min_biz_year
                                 AND d2.dps IS NOT NULL AND d2.dps < 1000000)
            """,
            {"min_biz_year": min_biz_year}
        ).fetchall()
    factors = _load_split_correction_factors()
    result: Dict[str, bool] = {}
    for sym, biz_year in rows:
        sym_z = str(sym).zfill(6)
        cf = factors.get((sym_z, biz_year), 1.0)
        result[sym_z] = (cf != 1.0)
    return result


def _get_split_adj_cached(target_date: str) -> Dict[str, bool]:
    if _state.split_adj_cache is None or _state.split_adj_date != target_date:
        _state.split_adj_cache = _compute_split_adjusted_map()
        _state.split_adj_date = target_date
    return _state.split_adj_cache


def _compute_div_suspended_map() -> Dict[str, bool]:
    """배당중단의심 플래그: 현재 사용 중인 biz_year가 2024년인데(FY2025 데이터 없음)
    FY2025 행이 dps=NULL로 존재하는 종목 — 배당을 중단했을 가능성이 높음.
    모델/필터 미개입, 표시 배지 전용."""
    cur_year = datetime.now().year   # 2026
    prev_year = cur_year - 1        # 2025
    min_biz_year = cur_year - 2     # 2024
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT d1.symbol FROM dividends d1
            WHERE d1.dps IS NOT NULL AND d1.dps < 1000000
              AND d1.biz_year = :min_biz_year
              AND d1.biz_year = (
                SELECT MAX(biz_year) FROM dividends d2
                WHERE d2.symbol = d1.symbol
                  AND d2.biz_year >= :min_biz_year
                  AND d2.dps IS NOT NULL AND d2.dps < 1000000
              )
              AND EXISTS (
                SELECT 1 FROM dividends d3
                WHERE d3.symbol = d1.symbol
                  AND d3.biz_year = :prev_year
                  AND d3.dps IS NULL
              )
            """,
            {"min_biz_year": min_biz_year, "prev_year": prev_year},
        ).fetchall()
    return {str(sym).zfill(6): True for (sym,) in rows}


def _get_div_suspended_cached(target_date: str) -> Dict[str, bool]:
    if _state.div_suspended_cache is None or _state.div_suspended_date != target_date:
        _state.div_suspended_cache = _compute_div_suspended_map()
        _state.div_suspended_date = target_date
    return _state.div_suspended_cache


def _compute_actual_yield_map() -> Dict[str, float]:
    """실질 배당수익률 = 보정DPS ÷ 배당기준일(record_date) 주가.
    투자자가 배당기준일에 주식을 보유했을 때 실제로 받은 수익률 기준.

    prices 테이블은 pykrx adjusted=True(전기간 분할조정 가격)이고 DPS도 동일한 스케일로
    correction_factor 보정됨 → adjusted_dps / adjusted_record_price = 실제 DPS / 실제 주가.

    조회 우선순위:
      1. prices.date = record_date (정확 매칭, 99.5%)
      2. prices.date <= record_date ORDER BY date DESC (가장 가까운 이전 거래일, 0.5% 폴백)
      record_date NULL인 경우 biz_year||'1231' 사용 (2021 이전 — min_biz_year=2024라 실질 미발생)
    """
    min_biz_year = datetime.now().year - 2  # 2026 기준 2024 이상
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT
                d1.symbol, d1.biz_year, d1.dps,
                (SELECT p.close FROM prices p
                 WHERE p.symbol = d1.symbol
                   AND p.date <= COALESCE(d1.record_date, CAST(d1.biz_year AS TEXT) || '1231')
                 ORDER BY p.date DESC LIMIT 1) AS record_close
            FROM dividends d1
            WHERE d1.dps IS NOT NULL AND d1.dps < 1000000
              AND d1.biz_year >= :min_biz_year
              AND d1.biz_year = (SELECT MAX(biz_year) FROM dividends d2
                                  WHERE d2.symbol = d1.symbol AND d2.biz_year >= :min_biz_year
                                    AND d2.dps IS NOT NULL AND d2.dps < 1000000)
            """,
            {"min_biz_year": min_biz_year},
        ).fetchall()
    factors = _load_split_correction_factors()
    result: Dict[str, float] = {}
    for sym, biz_year, dps, record_close in rows:
        if not record_close:
            continue
        sym_z = str(sym).zfill(6)
        cf = factors.get((sym_z, biz_year), 1.0)
        result[sym_z] = dps * cf / record_close * 100
    return result


def _get_actual_yield_cached(target_date: str) -> Dict[str, float]:
    if _state.actual_yield_cache is None or _state.actual_yield_date != target_date:
        _state.actual_yield_cache = _compute_actual_yield_map()
        _state.actual_yield_date = target_date
    return _state.actual_yield_cache


def _compute_foreign_rate_map() -> Dict[str, float]:
    """종목별 최신 외국인 보유비율(%) — flows.foreign_net 표시 전용."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT symbol, foreign_net
            FROM (
                SELECT symbol, foreign_net,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM flows
                WHERE foreign_net IS NOT NULL
            )
            WHERE rn = 1
        """).fetchall()
    return {str(sym).zfill(6): rate for sym, rate in rows}


def _get_foreign_rate_cached(target_date: str) -> Dict[str, float]:
    if _state.foreign_rate_cache is None or _state.foreign_rate_date != target_date:
        _state.foreign_rate_cache = _compute_foreign_rate_map()
        _state.foreign_rate_date = target_date
    return _state.foreign_rate_cache


def _load_verified_yield_whitelist() -> set:
    """DART로 검증 완료된 고배당 종목 심볼 집합 반환.
    .dividend_yield_verified.json 파일 기반 — 수동 관리."""
    import json as _json
    path = BACKEND_ROOT / "data" / ".dividend_yield_verified.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("verified", {}).keys())
    except FileNotFoundError:
        return set()
    except Exception as e:
        logger.warning("배당수익률 화이트리스트 로드 실패: %s", e)
        return set()


def _compute_eps_map() -> Dict[str, float]:
    """종목별 가장 최근 공시된 EPS(dividends.eps, DART alotMatter 기반).
    PER을 fundamentals.per(1년 공백+신선도 의심) 대신 이걸로 역산 — backend/ml/per_validation.py
    워크포워드 검증 결과 저PER(이 방식)이 ✅ 진짜 edge로 확인돼 운영에 반영(2026-06-21).
    ⚠️ 2026-06-23: dividends 표본이 213→2,699종목으로 늘어난 뒤 재검증 결과 전체평균은
    무의미(OOS 49~51%)로 판명, ⚠️ 국면조건부로 하향(약세장 58.2%/강세장 46.8% 역효과) —
    PER 표시값/역산 방식 자체는 그대로 유지, 필터의 검증 등급만 하향됨."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT symbol, eps FROM dividends d1
            WHERE eps IS NOT NULL
              AND biz_year = (SELECT MAX(biz_year) FROM dividends d2
                               WHERE d2.symbol = d1.symbol AND d2.eps IS NOT NULL)
            """
        ).fetchall()
    return {sym: e for sym, e in rows}


def _get_eps_cached(target_date: str) -> Dict[str, float]:
    if _state.eps_cache is None or _state.eps_date != target_date:
        _state.eps_cache = _compute_eps_map()
        _state.eps_date = target_date
    return _state.eps_cache


def _compute_bps_map() -> Dict[str, float]:
    """종목별 가장 최근 공시된 BPS(financials.bps, DART 재무제표+주식총수 역산).
    BPS 표본이 294→2,277종목(~83%)으로 늘어난 뒤 backend/ml/pbr_validation.py 재검증 결과,
    저PBR 단독은 전체 평균으로는 무의미(OOS 승률 49~51%, 11% 표본 때의 53~56%는 작은
    표본의 과대추정이었음)로 등급 하향됨 — 다만 시장국면별로는 약세장 60.7%(p<.001)/
    횡보장 51.1%/강세장 47.0%(역효과)로 RSI/볼린저와 같은 "국면조건부" 패턴은 유지됨
    (2026-06-22). 저PER+저PBR/고배당+저PBR 조합은 재검증에서도 여전히 유의하게 나음
    (오히려 약세장 승률 79.7%→85.6%, 74.1%→78.9%로 더 강해짐) — combo_validation.py 참고."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT symbol, bps FROM financials f1
            WHERE bps IS NOT NULL
              AND biz_year = (SELECT MAX(biz_year) FROM financials f2
                               WHERE f2.symbol = f1.symbol AND f2.bps IS NOT NULL)
            """
        ).fetchall()
    return {sym: b for sym, b in rows}


def _get_bps_cached(target_date: str) -> Dict[str, float]:
    if _state.bps_cache is None or _state.bps_date != target_date:
        _state.bps_cache = _compute_bps_map()
        _state.bps_date = target_date
    return _state.bps_cache


def _compute_investor_warning_map(target_date: str) -> Dict[str, bool]:
    """외국인+기관 동시 순매도 & 개인 순매수(같은 날) 종목 맵 — investor_trading_kis의
    가장 최근 날짜(target_date 이전 또는 같은 날) 기준 단일 일자 신호.
    kis_investor_flow_validation.py(2026-06-27) 검증 결과: 6개 가설 중 이 조합만
    5일·10일 수익률 둘 다 일관되게 음(-1.55%/-1.42% 초과수익) — 30거래일 단일기간이라
    방향성 참고용이지만 가장 또렷했던 신호라 경고 표시용으로 채택. 모델 점수와는 무관."""
    with sqlite3.connect(DB_PATH) as conn:
        latest = conn.execute(
            "SELECT MAX(date) FROM investor_trading_kis WHERE date <= ?", (target_date,)
        ).fetchone()[0]
        if not latest:
            return {}
        rows = conn.execute(
            """
            SELECT symbol FROM investor_trading_kis
            WHERE date = ? AND foreign_net_qty < 0 AND inst_net_qty < 0 AND indiv_net_qty > 0
            """,
            (latest,),
        ).fetchall()
    return {sym: True for (sym,) in rows}


def _get_investor_warning_cached(target_date: str) -> Dict[str, bool]:
    if _state.investor_warning_cache is None or _state.investor_warning_date != target_date:
        _state.investor_warning_cache = _compute_investor_warning_map(target_date)
        _state.investor_warning_date = target_date
    return _state.investor_warning_cache


def _compute_high_vol_map(target_date: str) -> Dict[str, bool]:
    """전일 intraday range (high-low)/close 횡단면 상위5% 종목 맵.
    validate_signals.py(2026-07-02) 검증: IS 2021~2024 diff=-0.727%p p=0.0000,
    OOS 2025~2026 diff=-1.103%p p=0.0000 — 방향 일치, 통계적으로 강력.
    모델 피처화는 atr_pct r=0.51 중복+SHAP 0%라 기각, 배지 표시(표시전용)로만 채택.
    추천 순위/모델 점수에는 일절 영향 없음."""
    with sqlite3.connect(DB_PATH) as conn:
        prev_date = conn.execute(
            "SELECT MAX(date) FROM prices WHERE date < ?", (target_date,)
        ).fetchone()[0]
        if not prev_date:
            return {}
        rows = conn.execute(
            """
            SELECT symbol,
                   CAST(high - low AS REAL) / NULLIF(CAST(close AS REAL), 0) AS intraday_range
            FROM prices
            WHERE date = ? AND close > 0 AND high > 0 AND low >= 0
              AND close * volume >= 1000000000
            """,
            (prev_date,),
        ).fetchall()
    if not rows:
        return {}
    ranges = [r for _, r in rows if r is not None]
    if len(ranges) < 20:
        return {}
    threshold = sorted(ranges)[int(len(ranges) * 0.95)]
    return {sym: True for sym, r in rows if r is not None and r >= threshold}


def _get_high_vol_cached(target_date: str) -> Dict[str, bool]:
    if _state.high_vol_cache is None or _state.high_vol_date != target_date:
        _state.high_vol_cache = _compute_high_vol_map(target_date)
        _state.high_vol_date = target_date
    return _state.high_vol_cache


def _compute_dps_growth_map() -> Dict[str, bool]:
    """종목별 YoY DPS(주당배당금) 성장률을 계산해 상위 25% 종목을 마킹.
    timescale_profiling.py(2026-07-03): OOS 5d +0.35%(p=0.003)/20d +1.07%(p=0.000)/
    60d +2.64%(p=0.000) — 전구간 유의. atr_pct 등과 상관 r<0.12로 완전 독립.
    단 NaN 70%(배당 없는 종목 제외)라 SHAP 0% — 모델 피처 기각, 표시/필터 전용.
    DPS는 연간 공시 기준. 분할/병합 보정계수도 동일하게 적용."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT symbol, biz_year, dps FROM dividends
            WHERE dps IS NOT NULL AND dps > 0 AND dps < 1000000
            ORDER BY symbol, biz_year
            """
        ).fetchall()
    from collections import defaultdict
    sym_years: Dict[str, list] = defaultdict(list)
    for sym, biz_year, dps in rows:
        sym_years[str(sym).zfill(6)].append((biz_year, dps))

    factors = _load_split_correction_factors()
    min_biz_year = datetime.now().year - 2  # 2026 기준 2024 이상: 배당 중단 종목 제외
    growths: Dict[str, float] = {}
    for sym, yearly in sym_years.items():
        yearly.sort(key=lambda x: x[0])
        if len(yearly) < 2:
            continue
        prev_biz_year, prev_dps = yearly[-2]
        curr_biz_year, curr_dps = yearly[-1]
        if curr_biz_year < min_biz_year:
            continue  # 가장 최근 배당 자체가 오래됨 — 현재 배당 종목 아님
        prev_adj = prev_dps * factors.get((sym, prev_biz_year), 1.0)
        curr_adj = curr_dps * factors.get((sym, curr_biz_year), 1.0)
        if prev_adj <= 0:
            continue
        g = (curr_adj - prev_adj) / prev_adj
        # clip 극단치 (5배 초과 성장·역전은 이상치로 처리)
        growths[sym] = max(-5.0, min(5.0, g))

    if not growths:
        return {}
    vals = sorted(growths.values())
    cutoff = vals[int(len(vals) * 0.75)]
    return {sym: True for sym, g in growths.items() if g >= cutoff}


def _get_dps_growth_cached(target_date: str) -> Dict[str, bool]:
    if _state.dps_growth_cache is None or _state.dps_growth_date != target_date:
        _state.dps_growth_cache = _compute_dps_growth_map()
        _state.dps_growth_date = target_date
    return _state.dps_growth_cache


def _compute_trail_gap_map(target_date: str) -> Dict[str, float]:
    """종목별 trailing 20d 평균 갭(open/prev_close-1) 계산.
    C모드 갭 필터(사전 필터)용: trail_gap>2%인 종목은 고갭 이력으로 추천 후순위.
    gap_alternatives.py(2026-07-04) 검증(강세장val): C모드 적용 시 실전 평균 -0.14%→+0.35%.
    ⚠️ 강세장(2025-26) 기준. 하락장 성과는 별도 검증 필요."""
    # 45 calendar days ≈ 30 trading days (20d rolling + 워밍업 충분)
    import pandas as pd
    import numpy as np
    from datetime import datetime, timedelta
    try:
        dt = datetime.strptime(target_date, "%Y%m%d")
    except ValueError:
        return {}
    date_from = (dt - timedelta(days=45)).strftime("%Y%m%d")
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT symbol, date, open, close
            FROM prices
            WHERE date >= ? AND date <= ? AND close > 0 AND open > 0
            ORDER BY symbol, date
            """,
            (date_from, target_date),
        ).fetchall()
    if not rows:
        return {}
    p = pd.DataFrame(rows, columns=["symbol", "date", "open", "close"])
    p = p.sort_values(["symbol", "date"])
    p["prev_close"] = p.groupby("symbol")["close"].shift(1)
    p["gap"] = np.where(p["prev_close"] > 0, p["open"] / p["prev_close"] - 1, np.nan)
    # shift(1): 당일 갭은 내일 필터에 쓰임 (look-ahead 방지)
    p["trail_gap20"] = p.groupby("symbol")["gap"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=5).mean()
    )
    latest = p.groupby("symbol")["trail_gap20"].last()
    return latest.dropna().to_dict()


def _get_trail_gap_cached(target_date: str) -> Dict[str, float]:
    if _state.trail_gap_cache is None or _state.trail_gap_date != target_date:
        _state.trail_gap_cache = _compute_trail_gap_map(target_date)
        _state.trail_gap_date = target_date
    return _state.trail_gap_cache


def _get_regime_gate_cached(target_date: str) -> Dict[str, Any]:
    """G2 레짐 게이트 상태 (날짜별 캐시 — 하루에 한 번만 DB 계산)."""
    if _state.regime_gate_cache is None or _state.regime_gate_date != target_date:
        _state.regime_gate_cache = get_regime_gate_g2()
        _state.regime_gate_date = target_date
    return _state.regime_gate_cache


_REGIME_LABEL = {"bull": "강세장", "sideways": "횡보장", "bear": "약세장", "unknown": "판단불가"}


@app.get("/api/screener", summary="조건 조합 종목 스크리너 (워크포워드 검증된 필터만)")
async def screener(
    high_dividend:    bool = Query(default=False, description="⚠️ 시장국면 조건부(2026-06-23 하향) — 배당수익률 상위 20%, DPS÷실제시장가 직접 계산. 약세장 65.8%/강세장 48.7%(역효과)"),
    rsi_oversold:     bool = Query(default=False, description="⚠️ 시장국면 조건부 — RSI14 < 30"),
    bb_lower:         bool = Query(default=False, description="⚠️ 시장국면 조건부 — 볼린저밴드 하단 터치"),
    low_per:          bool = Query(default=False, description="⚠️ 시장국면 조건부(2026-06-23 하향) — PER 하위 20% (EPS 역산 PIT). 약세장 58.2%/강세장 46.8%(역효과)"),
    low_pbr:          bool = Query(default=False, description="⚠️ 시장국면 조건부 — PBR 하위 20% (BPS 역산). 약세장 60.7%/강세장 47.0%(역효과)"),
    div_growth:       bool = Query(default=False, description="📈 배당 성장 상위 25% — YoY DPS 성장률 상위 25%. OOS 5d/20d/60d 전구간 유의. 커버리지 ~40%"),
    eps_growth_top:   bool = Query(default=False, description="✅ 60d 검증 팩터 — EPS 성장률 상위 20% (OOS 60d +1.47%p, p=0.000, PIT)"),
    eps_accel:        bool = Query(default=False, description="✅ 60d 검증 팩터 — EPS 성장 가속(전년 대비 가속) (OOS 60d +2.16%p, p=0.000)"),
    roe_top:          bool = Query(default=False, description="✅ 60d 검증 팩터 — ROE 상위 20% (OOS 60d +2.52%p, p=0.000)"),
    bps_growth_top:   bool = Query(default=False, description="✅ 60d 모델 포함 — BPS 성장률 상위 20% (SHAP 1.55%)"),
    score_60d_top20:  bool = Query(default=False, description="60d 모델 추천 상위 20% (전체 종목 기준, 첫 호출 시 추론 수 초 소요)"),
    score_60d_top10:  bool = Query(default=False, description="60d 모델 추천 상위 10% (전체 종목 기준, 첫 호출 시 추론 수 초 소요)"),
    min_vol20d:       bool = Query(default=False, description="🔧 유동성 필터 — 20일 평균 거래대금 10억 이상 (운용규칙과 동일 기준)"),
    exclude_high_atr: bool = Query(default=False, description="🔧 품질 필터 — ATR 상위 10% 종목 제외 (⚡ 변동성↑ 배지 동일 기준, IS/OOS p=0.0000)"),
    sort_by:  str = Query(default="symbol", description="symbol/per/pbr/rsi_14/dividend_yield/vol_ratio_20d/ret_20d"),
    sort_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    limit: int = Query(default=100, ge=1, le=300),
    symbol: Optional[str] = Query(default=None, description="단일 종목 프로파일 조회 — 지정 시 해당 종목 1건 + 필터 통과 여부 반환"),
) -> Dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        target_date = conn.execute("SELECT MAX(date) FROM features").fetchone()[0]
        if target_date is None:
            raise HTTPException(status_code=503, detail="features 데이터 없음")

        rows = conn.execute(
            """
            SELECT f.symbol, s.name, s.market, s.sector, p.close,
                   f.per, f.pbr, f.rsi_14, f.bb_pct, f.vol_ratio_20d, f.ret_20d, f.atr_pct
            FROM features f
            JOIN stocks s ON f.symbol = s.symbol
            JOIN prices p ON p.symbol = f.symbol AND p.date = f.date
            WHERE f.date = ?
            """,
            (target_date,),
        ).fetchall()

    stocks = [dict(r) for r in rows if not _SCREENER_ETF_PATTERN.search(r["name"] or "")]

    # 섹터명 (KSIC 3자리 매핑)
    for s in stocks:
        s["sector_name"] = _sector_name(s.get("sector"))

    # PIT PER/PBR (dividends.eps, financials.bps 역산 — look-ahead 없음)
    eps_map = _get_eps_cached(target_date)
    bps_map = _get_bps_cached(target_date)
    for s in stocks:
        eps = eps_map.get(s["symbol"])
        s["per_pit"] = (s["close"] / eps) if eps and eps > 0 else None
        bps = bps_map.get(s["symbol"])
        s["pbr_pit"] = (s["close"] / bps) if bps and bps > 0 else None

    # 횡단면 percentile rank (당일 전종목 기준)
    per_pit_vals = sorted(s["per_pit"] for s in stocks if s["per_pit"] is not None and s["per_pit"] > 0)
    pbr_pit_vals = sorted(s["pbr_pit"] for s in stocks if s["pbr_pit"] is not None and s["pbr_pit"] > 0)
    atr_vals     = sorted(s["atr_pct"] for s in stocks if s["atr_pct"] is not None)

    def _pct_rank(sorted_vals: List[float], v: Optional[float]) -> Optional[float]:
        if v is None or not sorted_vals:
            return None
        return bisect.bisect_right(sorted_vals, v) / len(sorted_vals)

    # 배당 관련 맵
    dps_map = _get_dps_cached(target_date)
    dps_growth_map = _get_dps_growth_cached(target_date)
    special_div_map = _get_special_div_cached(target_date)
    split_adj_map = _get_split_adj_cached(target_date)
    div_suspended_map = _get_div_suspended_cached(target_date)
    actual_yield_map = _get_actual_yield_cached(target_date)
    verified_yield_symbols = _load_verified_yield_whitelist()
    for s in stocks:
        dps = dps_map.get(s["symbol"])
        # dividend_yield = (DPS × CF) ÷ 현재종가 — trailing yield.
        #   필터 판정("고배당 상위 20%")과 백테스트(combo_discovery_v2.py)가 이 값을 씀.
        #   백테스트: `dps_corr / prices["close"]` — 동일 공식. train-serving skew 없음.
        # actual_yield   = (DPS × CF) ÷ 배당기준일종가 — 역사적 고정값.
        #   표시(배당 컬럼 숫자)와 정렬(sort_key_map["dividend_yield"])에만 사용.
        #   투자자가 배당락일 시점의 실제 수익률을 보도록 하기 위함.
        s["dividend_yield"] = (dps / s["close"] * 100) if dps and s["close"] else None
        s["actual_yield"] = actual_yield_map.get(s["symbol"])
        s["dps_growth"] = dps_growth_map.get(s["symbol"], False)
        s["has_special_dividend"] = special_div_map.get(s["symbol"], False)
        s["has_split_adjusted"] = split_adj_map.get(s["symbol"], False)
        s["has_div_suspended"] = div_suspended_map.get(s["symbol"], False)
        ay = s.get("actual_yield")
        # DART 검증 완료 종목(화이트리스트)은 미검증 뱃지 제외
        is_verified = s["symbol"] in verified_yield_symbols
        s["has_unverified_yield"] = (
            ay is not None
            and ay > 15
            and not is_verified
            and not s["has_special_dividend"]
            and not s["has_split_adjusted"]
            and not s["has_div_suspended"]
        )
    # 필터 판정용 횡단면 순위 기반. dividend_yield(trailing) 기준 — 백테스트와 동일.
    div_vals = sorted(s["dividend_yield"] for s in stocks if s["dividend_yield"] is not None)

    # 60d PIT 팩터 (EPS성장/ROE/BPS성장 — 모든 요청에서 계산, 캐시됨)
    factors_60d_map = _get_60d_factors_cached(target_date)
    for s in stocks:
        fac = factors_60d_map.get(s["symbol"], {})
        s["eps_growth_yoy"]   = fac.get("eps_growth_yoy")
        s["eps_growth_accel"] = fac.get("eps_growth_accel")
        s["roe_level"]        = fac.get("roe_level")
        s["bps_growth_yoy"]   = fac.get("bps_growth_yoy")

    # 60d 팩터 percentile 기준값
    eps_growth_vals  = sorted(s["eps_growth_yoy"] for s in stocks if s["eps_growth_yoy"] is not None)
    roe_vals         = sorted(s["roe_level"]       for s in stocks if s["roe_level"] is not None)
    bps_growth_vals  = sorted(s["bps_growth_yoy"]  for s in stocks if s["bps_growth_yoy"] is not None)

    # 20일 평균 거래대금 (캐시됨)
    vol20d_map = _get_vol20d_cached(target_date)
    for s in stocks:
        s["vol_krw_20d"] = vol20d_map.get(s["symbol"])

    # 60d 모델 점수 (score_60d_top 필터 활성 시만 계산 — 초기 ~3초)
    need_60d_scores = score_60d_top10 or score_60d_top20 or symbol is not None
    scores_60d_map = _get_scores_60d_cached(target_date) if need_60d_scores else {}
    score_vals = sorted(scores_60d_map.values()) if scores_60d_map else []
    for s in stocks:
        s["score_60d"] = scores_60d_map.get(s["symbol"])

    # 외국인 보유비율 (flows.foreign_net 최신값 — 표시 전용)
    foreign_rate_map = _get_foreign_rate_cached(target_date)
    for s in stocks:
        s["foreign_rate"] = foreign_rate_map.get(s["symbol"])

    # 시장 국면 (필터 모드/프로파일 모드 공통)
    market = _get_market_trend_cached(target_date)
    trend = market.get("trend", "unknown")
    mean_reversion_valid = trend in ("bear", "sideways")
    regime = {
        "trend": trend,
        "label": _REGIME_LABEL.get(trend, "판단불가"),
        "ret_20d_pct": market.get("ret_20d_pct"),
        "mean_reversion_valid": mean_reversion_valid,
        "mean_reversion_reason": (
            f"{_REGIME_LABEL.get(trend)}에서는 RSI/볼린저 평균회귀 필터가 워크포워드 검증상 유효(승률 60~80%)"
            if mean_reversion_valid else
            f"{_REGIME_LABEL.get(trend)}에서는 평균회귀 필터의 과거 승률이 37~44%로 낮음 — 사용 비추천"
        ),
    }

    # 단일 종목 프로파일 모드
    if symbol is not None:
        sym_key = symbol.strip().upper().zfill(6)
        target_stock = next((s for s in stocks if s["symbol"] == sym_key), None)
        if target_stock is None:
            raise HTTPException(status_code=404, detail=f"종목을 찾을 수 없습니다: {symbol}")
        s = target_stock
        score_vals_full = sorted(scores_60d_map.values()) if scores_60d_map else []
        flags: Dict[str, bool] = {
            "high_dividend":  s["dividend_yield"] is not None and (_pct_rank(div_vals, s["dividend_yield"]) or 0) >= 0.8,
            "rsi_oversold":   s["rsi_14"] is not None and s["rsi_14"] < 30,
            "bb_lower":       s["bb_pct"] is not None and s["bb_pct"] <= 0.05,
            "low_per":        s["per_pit"] is not None and s["per_pit"] > 0 and (_pct_rank(per_pit_vals, s["per_pit"]) or 1) <= 0.2,
            "low_pbr":        s["pbr_pit"] is not None and (_pct_rank(pbr_pit_vals, s["pbr_pit"]) or 1) <= 0.2,
            "div_growth":     bool(s["dps_growth"]),
            "eps_growth_top": s["eps_growth_yoy"] is not None and (_pct_rank(eps_growth_vals, s["eps_growth_yoy"]) or 0) >= 0.8,
            "eps_accel":      s["eps_growth_accel"] is not None and s["eps_growth_accel"] > 0,
            "roe_top":        s["roe_level"] is not None and (_pct_rank(roe_vals, s["roe_level"]) or 0) >= 0.8,
            "bps_growth_top": s["bps_growth_yoy"] is not None and (_pct_rank(bps_growth_vals, s["bps_growth_yoy"]) or 0) >= 0.8,
            "score_60d_top20": s["score_60d"] is not None and bool(score_vals_full) and (_pct_rank(score_vals_full, s["score_60d"]) or 0) >= 0.8,
            "score_60d_top10": s["score_60d"] is not None and bool(score_vals_full) and (_pct_rank(score_vals_full, s["score_60d"]) or 0) >= 0.9,
            "min_vol20d":     s["vol_krw_20d"] is not None and s["vol_krw_20d"] >= 1_000_000_000,
            "exclude_high_atr": s["atr_pct"] is None or (_pct_rank(atr_vals, s["atr_pct"]) or 0) < 0.9,
        }
        return {"date": target_date, "market_regime": regime, "stock": s, "filter_flags": flags}

    filtered = []
    for s in stocks:
        if high_dividend and (s["dividend_yield"] is None or _pct_rank(div_vals, s["dividend_yield"]) < 0.8):
            continue
        if rsi_oversold and (s["rsi_14"] is None or s["rsi_14"] >= 30):
            continue
        if bb_lower and (s["bb_pct"] is None or s["bb_pct"] > 0.05):
            continue
        if low_per and (s["per_pit"] is None or s["per_pit"] <= 0 or _pct_rank(per_pit_vals, s["per_pit"]) > 0.2):
            continue
        if low_pbr and (s["pbr_pit"] is None or _pct_rank(pbr_pit_vals, s["pbr_pit"]) > 0.2):
            continue
        if div_growth and not s["dps_growth"]:
            continue
        if eps_growth_top and (s["eps_growth_yoy"] is None or _pct_rank(eps_growth_vals, s["eps_growth_yoy"]) < 0.8):
            continue
        if eps_accel and (s["eps_growth_accel"] is None or s["eps_growth_accel"] <= 0):
            continue
        if roe_top and (s["roe_level"] is None or _pct_rank(roe_vals, s["roe_level"]) < 0.8):
            continue
        if bps_growth_top and (s["bps_growth_yoy"] is None or _pct_rank(bps_growth_vals, s["bps_growth_yoy"]) < 0.8):
            continue
        if min_vol20d and (s["vol_krw_20d"] is None or s["vol_krw_20d"] < 1_000_000_000):
            continue
        if exclude_high_atr:
            atr_rank = _pct_rank(atr_vals, s["atr_pct"])
            if atr_rank is not None and atr_rank >= 0.9:
                continue
        if score_60d_top10 and (s["score_60d"] is None or _pct_rank(score_vals, s["score_60d"]) < 0.9):
            continue
        if score_60d_top20 and (s["score_60d"] is None or _pct_rank(score_vals, s["score_60d"]) < 0.8):
            continue
        filtered.append(s)

    sort_key_map = {
        "symbol": lambda s: s["symbol"],
        "per": lambda s: s["per_pit"] if s["per_pit"] is not None else float("inf"),
        "pbr": lambda s: s["pbr_pit"] if s["pbr_pit"] is not None else float("inf"),
        "rsi_14": lambda s: s["rsi_14"] if s["rsi_14"] is not None else float("inf"),
        "dividend_yield": lambda s: (
            s.get("actual_yield") if s.get("actual_yield") is not None
            else (s["dividend_yield"] if s["dividend_yield"] is not None else -1)
        ),
        "vol_ratio_20d": lambda s: s["vol_ratio_20d"] if s["vol_ratio_20d"] is not None else -1,
        "ret_20d": lambda s: s["ret_20d"] if s["ret_20d"] is not None else -1,
        "score_60d": lambda s: s["score_60d"] if s["score_60d"] is not None else -1,
    }
    key_fn = sort_key_map.get(sort_by, sort_key_map["symbol"])
    filtered.sort(key=key_fn, reverse=(sort_dir == "desc"))
    filtered = filtered[:limit]

    return {"date": target_date, "market_regime": regime, "total": len(filtered), "stocks": filtered}


@app.get("/api/screener/cross-analysis", summary="검증된 6개 조합 교차 분석 — 2개 이상 조합에 동시 포함된 종목")
async def screener_cross_analysis() -> Dict[str, Any]:
    """검증된 COMBO_PRESETS 6개를 동시에 실행해 2개 이상에 해당하는 종목을 반환."""
    CROSS_PRESETS = [
        {"label": "고배당+저PBR+60d상위20%", "keys": ["high_dividend", "low_pbr", "score_60d_top20"], "oos_rate": "76.1%"},
        {"label": "고배당+저PER+60d상위10%", "keys": ["high_dividend", "low_per", "score_60d_top10"], "oos_rate": "75.7%"},
        {"label": "고배당+저PER",            "keys": ["high_dividend", "low_per"],                    "oos_rate": "71.9%"},
        {"label": "고배당+저PBR",            "keys": ["high_dividend", "low_pbr"],                    "oos_rate": "68.8%"},
        {"label": "고배당+배당성장",          "keys": ["high_dividend", "div_growth"],                 "oos_rate": "67.5%"},
        {"label": "저PBR+배당성장",           "keys": ["low_pbr",       "div_growth"],                 "oos_rate": "63.9%"},
    ]

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        target_date = conn.execute("SELECT MAX(date) FROM features").fetchone()[0]
        if target_date is None:
            raise HTTPException(status_code=503, detail="features 데이터 없음")
        rows = conn.execute(
            """
            SELECT f.symbol, s.name, s.market, s.sector, p.close,
                   f.per, f.pbr, f.rsi_14, f.bb_pct, f.vol_ratio_20d, f.ret_20d, f.atr_pct
            FROM features f
            JOIN stocks s ON f.symbol = s.symbol
            JOIN prices p ON p.symbol = f.symbol AND p.date = f.date
            WHERE f.date = ?
            """,
            (target_date,),
        ).fetchall()

    stocks = [dict(r) for r in rows if not _SCREENER_ETF_PATTERN.search(r["name"] or "")]

    # 섹터명
    for s in stocks:
        s["sector_name"] = _sector_name(s.get("sector"))

    # PIT PER/PBR
    eps_map = _get_eps_cached(target_date)
    bps_map = _get_bps_cached(target_date)
    for s in stocks:
        eps = eps_map.get(s["symbol"])
        s["per_pit"] = (s["close"] / eps) if eps and eps > 0 else None
        bps = bps_map.get(s["symbol"])
        s["pbr_pit"] = (s["close"] / bps) if bps and bps > 0 else None

    per_pit_vals = sorted(s["per_pit"] for s in stocks if s["per_pit"] is not None and s["per_pit"] > 0)
    pbr_pit_vals = sorted(s["pbr_pit"] for s in stocks if s["pbr_pit"] is not None and s["pbr_pit"] > 0)

    def _pct_rank_local(sorted_vals: List[float], v: Optional[float]) -> Optional[float]:
        if v is None or not sorted_vals:
            return None
        return bisect.bisect_right(sorted_vals, v) / len(sorted_vals)

    # 배당 관련
    dps_map        = _get_dps_cached(target_date)
    dps_growth_map = _get_dps_growth_cached(target_date)
    actual_yield_map = _get_actual_yield_cached(target_date)
    for s in stocks:
        dps = dps_map.get(s["symbol"])
        s["dividend_yield"] = (dps / s["close"] * 100) if dps and s["close"] else None
        s["actual_yield"]   = actual_yield_map.get(s["symbol"])
        s["dps_growth"]     = dps_growth_map.get(s["symbol"], False)

    div_vals = sorted(s["dividend_yield"] for s in stocks if s["dividend_yield"] is not None)

    # 60d 모델 점수 (cross-analysis 프리셋 2개가 score_60d 필터를 사용)
    scores_60d_map = _get_scores_60d_cached(target_date)
    score_vals     = sorted(scores_60d_map.values()) if scores_60d_map else []
    for s in stocks:
        s["score_60d"] = scores_60d_map.get(s["symbol"])

    def _passes(s: Dict[str, Any], key: str) -> bool:
        if key == "high_dividend":
            return s["dividend_yield"] is not None and (_pct_rank_local(div_vals, s["dividend_yield"]) or 0) >= 0.8
        if key == "low_per":
            return s["per_pit"] is not None and s["per_pit"] > 0 and (_pct_rank_local(per_pit_vals, s["per_pit"]) or 1) <= 0.2
        if key == "low_pbr":
            return s["pbr_pit"] is not None and (_pct_rank_local(pbr_pit_vals, s["pbr_pit"]) or 1) <= 0.2
        if key == "div_growth":
            return bool(s["dps_growth"])
        if key == "score_60d_top20":
            return s["score_60d"] is not None and bool(score_vals) and (_pct_rank_local(score_vals, s["score_60d"]) or 0) >= 0.8
        if key == "score_60d_top10":
            return s["score_60d"] is not None and bool(score_vals) and (_pct_rank_local(score_vals, s["score_60d"]) or 0) >= 0.9
        return False

    result: List[Dict[str, Any]] = []
    for s in stocks:
        combo_flags = [all(_passes(s, k) for k in p["keys"]) for p in CROSS_PRESETS]
        combo_count = sum(combo_flags)
        if combo_count >= 2:
            result.append({
                "symbol":         s["symbol"],
                "name":           s["name"],
                "market":         s["market"],
                "close":          s["close"],
                "dividend_yield": s["dividend_yield"],
                "actual_yield":   s.get("actual_yield"),
                "score_60d":      s["score_60d"],
                "per":            s["per_pit"],
                "pbr":            s["pbr_pit"],
                "combo_count":    combo_count,
                "combo_flags":    combo_flags,
            })

    result.sort(key=lambda x: (-x["combo_count"], -(x["dividend_yield"] or 0)))

    return {
        "date":    target_date,
        "presets": CROSS_PRESETS,
        "total":   len(result),
        "stocks":  result,
    }


@app.get("/api/screener/sector-flow", summary="KSIC 섹터별 외국인·기관·연기금 자금흐름 집계")
async def screener_sector_flow() -> Dict[str, Any]:
    """investor_trading_kis 기반 5d/20d/60d 섹터 자금흐름. 표시 전용."""
    import json as _json

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        target_date = conn.execute("SELECT MAX(date) FROM features").fetchone()[0]
        if target_date is None:
            raise HTTPException(status_code=503, detail="features 데이터 없음")

    # 일별 캐시
    if _state.sector_flow_cache and _state.sector_flow_date == target_date:
        return _state.sector_flow_cache

    # KSIC 3자리 → 한국어 이름 맵
    ksic_path = Path(__file__).parent.parent / "data" / "ksic_sector_names.json"
    try:
        ksic_names: Dict[str, str] = _json.loads(ksic_path.read_text(encoding="utf-8"))
    except Exception:
        ksic_names = {}

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # 거래일 컷오프: 5d / prior5d / 20d / 60d / 120d / 250d
        # DISTINCT + WHERE code='1001' 필수 — market_index는 KOSPI/KOSDAQ 2행/일이라
        # DISTINCT 없으면 260행 = 130 고유 날짜 → 120d/250d가 실제로 60d/125d가 되는 버그
        all_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM market_index WHERE code='1001' ORDER BY date DESC LIMIT 260"
        ).fetchall()]
        if len(all_dates) < 10:
            raise HTTPException(status_code=503, detail="market_index 날짜 부족")

        cutoff_5d   = all_dates[5]   if len(all_dates) > 5   else all_dates[-1]
        cutoff_10d  = all_dates[10]  if len(all_dates) > 10  else all_dates[-1]
        cutoff_20d  = all_dates[20]  if len(all_dates) > 20  else all_dates[-1]
        cutoff_60d  = all_dates[60]  if len(all_dates) > 60  else all_dates[-1]
        cutoff_120d = all_dates[120] if len(all_dates) > 120 else all_dates[-1]
        cutoff_250d = all_dates[250] if len(all_dates) > 250 else all_dates[-1]

        logger.info(
            "sector-flow cutoffs — 5d:%s 20d:%s 60d:%s 120d:%s 250d:%s",
            cutoff_5d, cutoff_20d, cutoff_60d, cutoff_120d, cutoff_250d,
        )

        # 종목 → 섹터 3자리 코드 맵 (stocks.sector = DART induty_code)
        sym_sector: Dict[str, str] = {}
        sym_name: Dict[str, str] = {}
        for r in conn.execute("SELECT symbol, name, sector FROM stocks WHERE sector IS NOT NULL").fetchall():
            code3 = str(r["sector"] or "")[:3]
            if code3.isdigit():
                sym_sector[r["symbol"]] = code3
                sym_name[r["symbol"]] = r["name"] or r["symbol"]

        # 섹터별 종목 수 집계 → 10개 미만 제외
        from collections import defaultdict as _defaultdict
        sector_symbols: Dict[str, list] = _defaultdict(list)
        for sym, code3 in sym_sector.items():
            sector_symbols[code3].append(sym)
        valid_sectors = {c for c, syms in sector_symbols.items() if len(syms) >= 10}

        # 외국인+기관 순매수 (5d ~ 250d)
        # investor_trading_kis_detail: 2020-12-21 ~ (full history, foreign_value + inst_total_value)
        # investor_trading_kis: 2026-03-25 ~ (rolling 30d collection, foreign_net_value + inst_net_value)
        # detail.foreign_value = kis.foreign_net_value (검증됨)
        # 250d(약 1년) 범위를 올바르게 덮으려면 detail을 메인으로, detail 종료 이후는 kis로 보완
        # value 컬럼 단위: 백만원. 억원으로 변환 = ÷100
        _det_max_date = (conn.execute(
            "SELECT MAX(date) FROM investor_trading_kis_detail"
        ).fetchone()[0] or "20200101")

        kis_rows = conn.execute(
            """
            SELECT symbol, date,
                   foreign_value    AS fv,
                   inst_total_value AS iv
            FROM investor_trading_kis_detail
            WHERE date >= ?
            UNION ALL
            SELECT symbol, date,
                   foreign_net_value AS fv,
                   inst_net_value    AS iv
            FROM investor_trading_kis
            WHERE date > ? AND date >= ?
            """,
            (cutoff_250d, _det_max_date, cutoff_250d),
        ).fetchall()

        # investor_trading_kis_detail: pension (best-effort, stale)
        pension_rows = conn.execute(
            """
            SELECT k.symbol, k.date,
                   k.pension_value AS pv
            FROM investor_trading_kis_detail k
            WHERE k.date >= ? AND k.pension_value IS NOT NULL
            """,
            (cutoff_250d,),
        ).fetchall()

        # 연기금 최신 데이터 날짜 (매일 수집 안 됨 — 표시용)
        pension_latest_row = conn.execute(
            "SELECT MAX(date) AS d FROM investor_trading_kis_detail"
            " WHERE pension_value IS NOT NULL AND pension_value != 0"
        ).fetchone()
        pension_latest_date: Optional[str] = pension_latest_row["d"] if pension_latest_row else None

    # ── 섹터별 집계 ──────────────────────────────────────────────────────
    from collections import defaultdict as _dd2

    # kis_rows → {symbol: {date: (fv, iv)}}
    sym_kis: Dict[str, Dict[str, tuple]] = _dd2(dict)
    for r in kis_rows:
        sym_kis[r["symbol"]][r["date"]] = (r["fv"] or 0.0, r["iv"] or 0.0)

    sym_pension: Dict[str, Dict[str, float]] = _dd2(dict)
    for r in pension_rows:
        sym_pension[r["symbol"]][r["date"]] = r["pv"] or 0.0

    def _sum_range(sym: str, since: str) -> tuple:
        """(foreign_억, inst_억, pension_억)"""
        f_tot = i_tot = p_tot = 0.0
        for dt, (fv, iv) in sym_kis.get(sym, {}).items():
            if dt >= since:
                f_tot += fv
                i_tot += iv
        for dt, pv in sym_pension.get(sym, {}).items():
            if dt >= since:
                p_tot += pv
        return f_tot / 100, i_tot / 100, p_tot / 100

    # 섹터별 집계
    sector_agg: Dict[str, Dict[str, float]] = {}
    pension_has_data: Dict[str, bool] = {}
    sym_5d_values:   Dict[str, tuple] = {}  # {symbol: (foreign_5d,   inst_5d)}
    sym_20d_values:  Dict[str, tuple] = {}  # {symbol: (foreign_20d,  inst_20d)}
    sym_60d_values:  Dict[str, tuple] = {}  # {symbol: (foreign_60d,  inst_60d)}
    sym_120d_values: Dict[str, tuple] = {}  # {symbol: (foreign_120d, inst_120d)}
    sym_250d_values: Dict[str, tuple] = {}  # {symbol: (foreign_250d, inst_250d)}

    for code3 in valid_sectors:
        syms = sector_symbols[code3]
        f5 = i5 = p5 = f10 = i10 = f20 = i20 = p20 = f60 = i60 = p60 = 0.0
        f120 = i120 = p120 = f250 = i250 = p250 = 0.0
        has_p_5d = has_p_20d = has_p_60d = has_p_120d = has_p_250d = False
        for s in syms:
            a, b, c = _sum_range(s, cutoff_5d)
            f5 += a; i5 += b; p5 += c
            sym_5d_values[s] = (a, b)
            a10, b10, _ = _sum_range(s, cutoff_10d)
            f10 += a10; i10 += b10
            a20, b20, c20 = _sum_range(s, cutoff_20d)
            f20 += a20; i20 += b20; p20 += c20
            sym_20d_values[s] = (a20, b20)
            a60, b60, c60 = _sum_range(s, cutoff_60d)
            f60 += a60; i60 += b60; p60 += c60
            sym_60d_values[s] = (a60, b60)
            a120, b120, c120 = _sum_range(s, cutoff_120d)
            f120 += a120; i120 += b120; p120 += c120
            sym_120d_values[s] = (a120, b120)
            a250, b250, c250 = _sum_range(s, cutoff_250d)
            f250 += a250; i250 += b250; p250 += c250
            sym_250d_values[s] = (a250, b250)
            # has_p 기간별 체크 (sym_pension은 이미 cutoff_250d 이후만 포함)
            for dt, pv in sym_pension.get(s, {}).items():
                if pv:
                    has_p_250d = True
                    if dt >= cutoff_120d:
                        has_p_120d = True
                    if dt >= cutoff_60d:
                        has_p_60d = True
                    if dt >= cutoff_20d:
                        has_p_20d = True
                    if dt >= cutoff_5d:
                        has_p_5d = True
        prior5_f = f10 - f5
        prior5_i = i10 - i5
        prior5_comb = prior5_f + prior5_i
        comb5 = f5 + i5
        momentum = (comb5 / prior5_comb) if abs(prior5_comb) > 0.01 else None
        sector_agg[code3] = {
            "foreign_5d":    round(f5,   1),
            "inst_5d":       round(i5,   1),
            "pension_5d":    round(p5,   1) if has_p_5d   else None,
            "combined_5d":   round(comb5, 1),
            "foreign_20d":   round(f20,  1),
            "inst_20d":      round(i20,  1),
            "pension_20d":   round(p20,  1) if has_p_20d  else None,
            "combined_20d":  round(f20+i20,  1),
            "foreign_60d":   round(f60,  1),
            "inst_60d":      round(i60,  1),
            "pension_60d":   round(p60,  1) if has_p_60d  else None,
            "combined_60d":  round(f60+i60,  1),
            "foreign_120d":  round(f120, 1),
            "inst_120d":     round(i120, 1),
            "pension_120d":  round(p120, 1) if has_p_120d else None,
            "combined_120d": round(f120+i120, 1),
            "foreign_250d":  round(f250, 1),
            "inst_250d":     round(i250, 1),
            "pension_250d":  round(p250, 1) if has_p_250d else None,
            "combined_250d": round(f250+i250, 1),
            "momentum_5d":   round(momentum, 2) if momentum is not None else None,
        }
        pension_has_data[code3] = has_p_5d or has_p_20d or has_p_60d or has_p_120d or has_p_250d

    # ── 교차 분석 종목 per 섹터 (max 6) ─────────────────────────────────
    dps_map        = _get_dps_cached(target_date)
    dps_growth_map = _get_dps_growth_cached(target_date)
    eps_map        = _get_eps_cached(target_date)
    bps_map        = _get_bps_cached(target_date)
    scores_60d_map = _get_scores_60d_cached(target_date)

    # cross_analysis stock 집합 (2개 이상 조합 통과)
    CROSS_KEYS = [
        ["high_dividend", "low_pbr", "score_60d_top20"],
        ["high_dividend", "low_per", "score_60d_top10"],
        ["high_dividend", "low_per"],
        ["high_dividend", "low_pbr"],
        ["high_dividend", "div_growth"],
        ["low_pbr",       "div_growth"],
    ]

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        feature_rows = conn.execute(
            """
            SELECT f.symbol, p.close
            FROM features f
            JOIN prices p ON p.symbol=f.symbol AND p.date=f.date
            WHERE f.date=?
            """,
            (target_date,),
        ).fetchall()

    close_map: Dict[str, float] = {r["symbol"]: r["close"] for r in feature_rows}

    all_syms = list(close_map.keys())
    per_pit_vals = sorted(
        (close_map[s] / eps_map[s]) for s in all_syms
        if eps_map.get(s) and eps_map[s] > 0 and s in close_map
    )
    pbr_pit_vals = sorted(
        (close_map[s] / bps_map[s]) for s in all_syms
        if bps_map.get(s) and bps_map[s] > 0 and s in close_map
    )
    div_vals = sorted(
        (dps_map[s] / close_map[s] * 100) for s in all_syms
        if dps_map.get(s) and close_map.get(s)
    )
    score_vals = sorted(scores_60d_map.values()) if scores_60d_map else []

    def _pct_rank2(sorted_vals: list, v: Optional[float]) -> Optional[float]:
        if v is None or not sorted_vals:
            return None
        return bisect.bisect_right(sorted_vals, v) / len(sorted_vals)

    def _passes2(sym: str, key: str) -> bool:
        cl = close_map.get(sym)
        if not cl:
            return False
        if key == "high_dividend":
            dps = dps_map.get(sym)
            dy = (dps / cl * 100) if dps else None
            return dy is not None and (_pct_rank2(div_vals, dy) or 0) >= 0.8
        if key == "low_per":
            eps = eps_map.get(sym)
            per = (cl / eps) if eps and eps > 0 else None
            return per is not None and (_pct_rank2(per_pit_vals, per) or 1) <= 0.2
        if key == "low_pbr":
            bps = bps_map.get(sym)
            pbr = (cl / bps) if bps and bps > 0 else None
            return pbr is not None and (_pct_rank2(pbr_pit_vals, pbr) or 1) <= 0.2
        if key == "div_growth":
            return bool(dps_growth_map.get(sym, False))
        if key == "score_60d_top20":
            sc = scores_60d_map.get(sym)
            return sc is not None and bool(score_vals) and (_pct_rank2(score_vals, sc) or 0) >= 0.8
        if key == "score_60d_top10":
            sc = scores_60d_map.get(sym)
            return sc is not None and bool(score_vals) and (_pct_rank2(score_vals, sc) or 0) >= 0.9
        return False

    cross_set: set = set()
    for s in all_syms:
        cnt = sum(all(_passes2(s, k) for k in keys) for keys in CROSS_KEYS)
        if cnt >= 2:
            cross_set.add(s)

    # 섹터별 교차 분석 종목 목록 (max 6, 60d 스코어 내림차순)
    # score_60d는 raw(0-1) 반환 — 프론트에서 ×100 처리
    sector_cross: Dict[str, list] = {}
    for code3 in valid_sectors:
        cands = [
            s for s in sector_symbols[code3]
            if s in cross_set and s in close_map
        ]
        cands.sort(key=lambda x: -(scores_60d_map.get(x) or 0))
        sector_cross[code3] = [
            {
                "symbol":      s,
                "name":        sym_name.get(s, s),
                "score_60d":   round(scores_60d_map[s], 4) if scores_60d_map.get(s) else None,
                "combo_count": sum(all(_passes2(s, k) for k in keys) for keys in CROSS_KEYS),
            }
            for s in cands[:6]
        ]

    # 섹터별 전체 종목 목록 (60d 스코어 내림차순, 5d 외국인/기관 순매수 분리)
    sector_stocks: Dict[str, list] = {}
    for code3 in valid_sectors:
        sym_list = []
        for s in sector_symbols[code3]:
            if s not in close_map:
                continue
            fa,    ia    = sym_5d_values.get(s,   (0.0, 0.0))
            fa20,  ia20  = sym_20d_values.get(s,  (0.0, 0.0))
            fa60,  ia60  = sym_60d_values.get(s,  (0.0, 0.0))
            fa120, ia120 = sym_120d_values.get(s, (0.0, 0.0))
            fa250, ia250 = sym_250d_values.get(s, (0.0, 0.0))
            sym_list.append({
                "symbol":       s,
                "name":         sym_name.get(s, s),
                "score_60d":    round(scores_60d_map[s], 4) if scores_60d_map.get(s) else None,
                "foreign_5d":   round(fa,    1),
                "inst_5d":      round(ia,    1),
                "foreign_20d":  round(fa20,  1),
                "inst_20d":     round(ia20,  1),
                "foreign_60d":  round(fa60,  1),
                "inst_60d":     round(ia60,  1),
                "foreign_120d": round(fa120, 1),
                "inst_120d":    round(ia120, 1),
                "foreign_250d": round(fa250, 1),
                "inst_250d":    round(ia250, 1),
            })
        sym_list.sort(key=lambda x: -(x["score_60d"] or 0))
        sector_stocks[code3] = sym_list

    # ── 응답 조립 ─────────────────────────────────────────────────────────
    sectors_out = []
    for code3 in sorted(valid_sectors):
        agg = sector_agg[code3]
        sectors_out.append({
            "code":        code3,
            "name":        ksic_names.get(code3, f"섹터 {code3}"),
            "stock_count": len(sector_symbols[code3]),
            **agg,
            "pension_available": pension_has_data.get(code3, False),
            "cross_stocks": sector_cross.get(code3, []),
            "stocks":       sector_stocks.get(code3, []),
        })

    # 기본 정렬: combined_5d 내림차순
    sectors_out.sort(key=lambda x: -x["combined_5d"])

    # 커버리지 통계
    syms_in_sectors = {s for c in valid_sectors for s in sector_symbols[c] if s in close_map}
    coverage = {
        "sector_count":       len(valid_sectors),
        "symbols_in_sectors": len(syms_in_sectors),
        "total_universe":     len(close_map),
    }

    result = {
        "date":                target_date,
        "pension_note":        "연기금 데이터는 KIS 종목별 조회 기준으로 최신 30거래일만 제공됩니다. 일부 종목 누락 가능.",
        "pension_latest_date": pension_latest_date,
        "cutoffs":             {"5d": cutoff_5d, "20d": cutoff_20d, "60d": cutoff_60d, "120d": cutoff_120d, "250d": cutoff_250d},
        "coverage":            coverage,
        "sectors":             sectors_out,
    }
    _state.sector_flow_cache = result
    _state.sector_flow_date  = target_date
    return result


@app.get("/api/screener/sector-flow/analysis", summary="섹터 자금 흐름 종합 분석 보고서")
async def screener_sector_flow_analysis() -> Dict[str, Any]:
    """
    섹터 자금 흐름 데이터를 기반으로 4가지 분석을 제공:
    1. 기간별 시장 전체 자금 흐름 요약 + 방향 판정
    2. 섹터 랭킹 (기간별 top5/bottom5) + 일관유입/유출/전환 분류
    3. 종목 쏠림 분석 (섹터 내 top1 종목 집중도)
    4. 교차 분석 연결 (6/6 종목의 섹터 흐름 연관)
    """
    # sector-flow 캐시 재사용 (없으면 직접 계산 — 같은 날짜 기준)
    sf = _state.sector_flow_cache
    if sf is None:
        # 캐시 미스: sector_flow 엔드포인트 로직을 재호출하여 채움
        try:
            sf = await screener_sector_flow()
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"sector-flow 데이터 로드 실패: {e}")

    sectors: list = sf.get("sectors", [])
    if not sectors:
        raise HTTPException(status_code=503, detail="섹터 데이터 없음")

    # ── 1. 기간별 시장 전체 자금 흐름 요약 ──────────────────────────────────
    periods = [("5d", "5일"), ("20d", "20일"), ("60d", "60일"), ("120d", "120일"), ("250d", "250일/1년")]
    market_totals: Dict[str, float] = {}
    for key, _ in periods:
        total = sum(s.get(f"combined_{key}", 0) or 0 for s in sectors)
        market_totals[key] = round(total, 1)

    # 방향 판정: 250d vs 60d vs 5d 비교
    v250 = market_totals.get("250d", 0)
    v60  = market_totals.get("60d",  0)
    v5   = market_totals.get("5d",   0)

    if v250 < 0 and v5 > 0:
        direction_label = "장기 유출 속 단기 반등"
        direction_color = "orange"
    elif v250 > 0 and v5 < 0:
        direction_label = "장기 유입 속 단기 이탈"
        direction_color = "orange"
    elif v250 > 0 and v60 > 0 and v5 > 0:
        direction_label = "전 기간 일관 유입"
        direction_color = "green"
    elif v250 < 0 and v60 < 0 and v5 < 0:
        direction_label = "전 기간 일관 유출"
        direction_color = "red"
    elif v60 > 0 and v5 > 0:
        direction_label = "중단기 유입 추세"
        direction_color = "green"
    elif v60 < 0 and v5 < 0:
        direction_label = "중단기 유출 추세"
        direction_color = "red"
    else:
        direction_label = "혼조세"
        direction_color = "gray"

    market_summary = {
        "totals": {k: market_totals[k] for k, _ in periods},
        "direction_label": direction_label,
        "direction_color": direction_color,
        "period_labels": {k: lbl for k, lbl in periods},
    }

    # ── 2. 섹터 랭킹 + 일관성 분류 ─────────────────────────────────────────
    def _sector_rank(period_key: str, top_n: int = 5) -> Dict[str, Any]:
        sorted_secs = sorted(sectors, key=lambda x: -(x.get(f"combined_{period_key}", 0) or 0))
        top5    = [{"code": s["code"], "name": s["name"], "value": s.get(f"combined_{period_key}", 0)} for s in sorted_secs[:top_n]]
        bottom5 = [{"code": s["code"], "name": s["name"], "value": s.get(f"combined_{period_key}", 0)} for s in sorted_secs[-top_n:]]
        return {"top": top5, "bottom": bottom5}

    sector_rankings = {k: _sector_rank(k) for k, _ in periods}

    # 일관성 분류 (5d/20d/60d 기준)
    consistent_inflow  = []  # 5d,20d,60d 모두 양수
    consistent_outflow = []  # 5d,20d,60d 모두 음수
    short_reversal     = []  # 60d 음수 + 5d 양수 (유출→유입 전환)
    short_exit         = []  # 60d 양수 + 5d 음수 (유입→유출 이탈)

    for s in sectors:
        c5  = s.get("combined_5d",  0) or 0
        c20 = s.get("combined_20d", 0) or 0
        c60 = s.get("combined_60d", 0) or 0
        entry = {"code": s["code"], "name": s["name"],
                 "combined_5d": c5, "combined_20d": c20, "combined_60d": c60,
                 "has_cross_stocks": bool(s.get("cross_stocks", []))}
        if c5 > 0 and c20 > 0 and c60 > 0:
            consistent_inflow.append(entry)
        elif c5 < 0 and c20 < 0 and c60 < 0:
            consistent_outflow.append(entry)
        elif c60 < 0 and c5 > 0:
            short_reversal.append(entry)
        elif c60 > 0 and c5 < 0:
            short_exit.append(entry)

    # 일관 유입 섹터: combined_5d 내림차순
    consistent_inflow.sort(key=lambda x: -x["combined_5d"])
    consistent_outflow.sort(key=lambda x: x["combined_5d"])
    short_reversal.sort(key=lambda x: -x["combined_5d"])
    short_exit.sort(key=lambda x: x["combined_5d"])

    sector_classification = {
        "consistent_inflow":  consistent_inflow,
        "consistent_outflow": consistent_outflow,
        "short_reversal":     short_reversal,
        "short_exit":         short_exit,
    }

    # ── 3. 종목 쏠림 분석 ──────────────────────────────────────────────────
    # 시총 데이터 로드 (prices.close × financials.shares_total / 1e8 → 억원 단위)
    analysis_date = sf.get("date", "")
    mktcap_map: Dict[str, float] = {}
    if analysis_date:
        try:
            import sqlite3 as _sqlite3
            with _sqlite3.connect(DB_PATH) as _mc:
                _rows = _mc.execute(
                    """
                    SELECT p.symbol, ROUND(p.close * f.shares_total / 1e8, 1) AS mktcap_억
                    FROM prices p
                    JOIN (
                        SELECT symbol, MAX(biz_year) AS by
                        FROM financials
                        WHERE shares_total > 0
                        GROUP BY symbol
                    ) latest ON p.symbol = latest.symbol
                    JOIN financials f ON f.symbol = p.symbol AND f.biz_year = latest.by
                    WHERE p.date = ?
                    """,
                    (analysis_date,),
                ).fetchall()
            mktcap_map = {r[0]: float(r[1]) for r in _rows if r[1] and r[1] > 0}
        except Exception:
            pass

    def _combined_5d(st: Dict) -> float:
        return (st.get("foreign_5d") or 0) + (st.get("inst_5d") or 0)

    def _combined_60d(st: Dict) -> float:
        return (st.get("foreign_60d") or 0) + (st.get("inst_60d") or 0)

    concentration_list = []
    for s in sectors:
        stocks_in_sector: list = s.get("stocks", [])
        if not stocks_in_sector:
            continue

        sector_total_5d = sum(_combined_5d(st) for st in stocks_in_sector)
        if abs(sector_total_5d) < 0.01:
            continue

        # 섹터 합이 양수일 때만 집중도 의미 있음 (유입 섹터 내 쏠림)
        if sector_total_5d <= 0:
            continue

        sorted_stocks = sorted(stocks_in_sector, key=lambda st: -_combined_5d(st))
        top1 = sorted_stocks[0] if sorted_stocks else None
        if top1 is None:
            continue

        top1_val = _combined_5d(top1)
        if top1_val <= 0:
            continue

        top1_sym = top1.get("symbol", "")

        # 시총 500억 미만 제외 (소형주 노이즈 방지)
        mktcap = mktcap_map.get(top1_sym)
        if mktcap is None or mktcap < 500:
            continue

        mktcap_ratio_pct = round(top1_val / mktcap * 100, 2)

        # 급증 비율: 5d 일평균 ÷ 60d 일평균
        top1_60d = _combined_60d(top1)
        surge_ratio: Optional[float] = None
        surge_label: Optional[str] = None
        if abs(top1_60d) <= 1.0:
            surge_label = "신규 유입" if top1_val > 0 else "신규 유출"
        else:
            daily_5d  = top1_val  / 5
            daily_60d = top1_60d  / 60
            if abs(daily_60d) > 0.001:
                ratio = daily_5d / daily_60d
                surge_ratio = round(ratio, 1)
                if ratio > 3:
                    surge_label = "급증"

        concentration_pct = round(top1_val / sector_total_5d * 100, 1)
        entry = {
            "sector_code":       s["code"],
            "sector_name":       s["name"],
            "sector_total_5d":   round(sector_total_5d, 1),
            "top1_symbol":       top1_sym,
            "top1_name":         top1.get("name", ""),
            "top1_value_5d":     round(top1_val, 1),
            "concentration_pct": concentration_pct,
            "is_concentrated":   concentration_pct > 50,
            "mktcap_ratio_pct":  mktcap_ratio_pct,
            "market_cap_억":      mktcap,
            "surge_ratio":       surge_ratio,
            "surge_label":       surge_label,
        }
        concentration_list.append(entry)

    # 시총 대비 비율 높은 순 정렬
    concentration_list.sort(key=lambda x: -x["mktcap_ratio_pct"])
    concentrated_sectors = [e for e in concentration_list if e["is_concentrated"]]

    # ── 4. 교차 분석 연결 ──────────────────────────────────────────────────
    # 교차 분석 종목 전체 집합 (sector_flow 캐시의 cross_stocks 재활용)
    inflow_sector_codes = {s["code"] for s in consistent_inflow}
    all_cross_symbols: set = set()
    sector_cross_map: Dict[str, list] = {}
    for s in sectors:
        cross_syms = [c["symbol"] for c in s.get("cross_stocks", [])]
        sector_cross_map[s["code"]] = cross_syms
        all_cross_symbols.update(cross_syms)

    total_cross = len(all_cross_symbols)
    cross_in_inflow_sectors = sum(
        1 for sym in all_cross_symbols
        if any(sym in sector_cross_map.get(code, []) for code in inflow_sector_codes)
    )

    # 교차 분석 종목 중 섹터 내 top1(5d 기준)인 종목 수
    sector_top1_symbols = {e["top1_symbol"] for e in concentration_list}
    cross_as_top1 = len(all_cross_symbols & sector_top1_symbols)

    cross_summary = {
        "total_cross_stocks":         total_cross,
        "cross_in_inflow_sectors":    cross_in_inflow_sectors,
        "cross_as_sector_top1":       cross_as_top1,
        "inflow_sector_count":        len(inflow_sector_codes),
    }

    return {
        "date":                   sf.get("date"),
        "data_source": {
            "label":                "외국인 + 기관계 순매수",
            "detail":               "기관계(inst)는 연기금 포함 기관 전체 합산 (KRX 분류 기준). pension_Nd 컬럼은 기관계 내 연기금 세부 내역.",
            "pension_latest_date":  sf.get("pension_latest_date"),
        },
        "market_summary":         market_summary,
        "sector_rankings":        sector_rankings,
        "sector_classification":  sector_classification,
        "concentration":          {
            "all":         concentration_list[:20],
            "concentrated": concentrated_sectors,
        },
        "cross_analysis":         cross_summary,
    }


@app.get("/api/watchlist", summary="관심 종목 목록 조회")
async def watchlist_list() -> List[Dict[str, Any]]:
    with _db_get_connection() as conn:
        rows = conn.execute(
            "SELECT symbol, name, added_at, memo FROM watchlist ORDER BY added_at DESC"
        ).fetchall()
    return [{"symbol": r[0], "name": r[1], "added_at": r[2], "memo": r[3]} for r in rows]


@app.post("/api/watchlist", summary="관심 종목 추가")
async def watchlist_add(body: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(body.get("symbol", "")).strip()
    name = str(body.get("name", "")).strip() or None
    memo = str(body.get("memo", "")).strip() or None
    if not symbol:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="symbol is required")
    with _db_get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (symbol, name, memo) VALUES (?,?,?)",
            (symbol, name, memo),
        )
        conn.commit()
    return {"symbol": symbol, "name": name, "memo": memo}


@app.delete("/api/watchlist/{symbol}", summary="관심 종목 제거")
async def watchlist_remove(symbol: str) -> Dict[str, Any]:
    with _db_get_connection() as conn:
        conn.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
        conn.commit()
    return {"symbol": symbol, "removed": True}


@app.get("/api/signals", summary="시그널 로그 조회")
async def signals_list(
    days: int = Query(default=7, ge=1, le=90),
) -> List[Dict[str, Any]]:
    import json as _json
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _db_get_connection() as conn:
        rows = conn.execute(
            """SELECT id, event_type, ticker, sector, message, data, created_at
               FROM signal_log
               WHERE created_at >= ?
               ORDER BY created_at DESC
               LIMIT 200""",
            (cutoff,),
        ).fetchall()
    result = []
    for row in rows:
        d = None
        if row[5]:
            try:
                d = _json.loads(row[5])
            except Exception:
                d = None
        result.append({
            "id": row[0],
            "event_type": row[1],
            "ticker": row[2],
            "sector": row[3],
            "message": row[4],
            "data": d,
            "created_at": row[6],
        })
    return result


@app.get("/api/portfolio/correlation", summary="보유 포지션 간 상관관계 매트릭스")
async def portfolio_correlation() -> Dict[str, Any]:
    """현재 open 60d paper_trades 종목들의 최근 60거래일 수익률 피어슨 상관계수 반환."""
    with _db_get_connection() as conn:
        rows = conn.execute(
            "SELECT pt.symbol, s.name FROM paper_trades pt "
            "LEFT JOIN stocks s ON pt.symbol = s.symbol "
            "WHERE pt.horizon = 60 AND pt.status = 'open'"
        ).fetchall()

    if len(rows) < 2:
        return {"symbols": [], "names": {}, "matrix": [], "avg_correlation": None, "message": "보유 포지션 없음"}

    symbols = [r[0] for r in rows]
    names = {r[0]: r[1] or r[0] for r in rows}

    # 최근 60거래일 일별 종가 수집
    price_series: Dict[str, list] = {}
    with _db_get_connection() as conn:
        trading_days = sorted(set(
            r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM market_index ORDER BY date DESC LIMIT 120"
            ).fetchall()
        ))[-60:]  # 가장 최근 60거래일

        if len(trading_days) < 10:
            return {"symbols": symbols, "names": names, "matrix": [], "avg_correlation": None, "message": "거래일 데이터 부족"}

        first_day = trading_days[0]
        last_day = trading_days[-1]

        for sym in symbols:
            price_rows = conn.execute(
                "SELECT date, close FROM prices WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date",
                (sym, first_day, last_day)
            ).fetchall()
            price_series[sym] = price_rows

    # 종목별 일별 수익률 계산
    def _daily_returns(price_rows):
        if len(price_rows) < 2:
            return []
        returns = []
        for i in range(1, len(price_rows)):
            prev = price_rows[i-1][1]
            curr = price_rows[i][1]
            if prev and prev > 0:
                returns.append((curr - prev) / prev)
            else:
                returns.append(0.0)
        return returns

    ret_map: Dict[str, list] = {sym: _daily_returns(price_series[sym]) for sym in symbols}

    # 피어슨 상관계수 계산 (공통 날짜 기준)
    def _pearson(a: list, b: list) -> float:
        n = min(len(a), len(b))
        if n < 5:
            return float("nan")
        a, b = a[:n], b[:n]
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
        den_a = sum((x - mean_a) ** 2 for x in a) ** 0.5
        den_b = sum((x - mean_b) ** 2 for x in b) ** 0.5
        if den_a < 1e-12 or den_b < 1e-12:
            return float("nan")
        return round(num / (den_a * den_b), 3)

    n = len(symbols)
    matrix = []
    off_diagonal = []
    for i in range(n):
        row_vals = []
        for j in range(n):
            if i == j:
                row_vals.append(1.0)
            else:
                r = _pearson(ret_map[symbols[i]], ret_map[symbols[j]])
                row_vals.append(r)
                if j > i and not (r != r):  # nan 제외
                    off_diagonal.append(r)
        matrix.append(row_vals)

    avg_corr = round(sum(off_diagonal) / len(off_diagonal), 3) if off_diagonal else None

    return {
        "symbols": symbols,
        "names": names,
        "matrix": matrix,
        "avg_correlation": avg_corr,
        "trading_days_used": len(trading_days),
    }


@app.get("/api/calendar", summary="월간 캘린더 이벤트 조회")
async def calendar_events(
    year: int = Query(default=2026, ge=2020, le=2030),
    month: int = Query(default=8, ge=1, le=12),
) -> List[Dict[str, Any]]:
    import json as _json
    import calendar as _cal

    # 월 범위 계산
    first_day = f"{year:04d}{month:02d}01"
    last_day_num = _cal.monthrange(year, month)[1]
    last_day = f"{year:04d}{month:02d}{last_day_num:02d}"

    events: List[Dict[str, Any]] = []

    with _db_get_connection() as conn:
        # 1. 포지션 진입일 (파랑) — horizon=60, open
        rows = conn.execute(
            """SELECT pt.symbol, pt.recommended_date, s.name
               FROM paper_trades pt
               LEFT JOIN stocks s ON pt.symbol = s.symbol
               WHERE pt.horizon = 60
                 AND pt.recommended_date BETWEEN ? AND ?""",
            (first_day, last_day),
        ).fetchall()
        for sym, rdate, name in rows:
            label = f"{name or sym}({sym})" if name else sym
            events.append({
                "date": rdate,
                "type": "position_entry",
                "color": "blue",
                "label": f"진입: {label}",
                "ticker": sym,
            })

        # 2. 포지션 만기일 (주황) — 진입일로부터 60거래일 후
        open_rows = conn.execute(
            """SELECT pt.symbol, pt.recommended_date, s.name
               FROM paper_trades pt
               LEFT JOIN stocks s ON pt.symbol = s.symbol
               WHERE pt.horizon = 60 AND pt.status = 'open'""",
        ).fetchall()
        # 거래일 목록 로드 (중복 제거 — market_index에 날짜당 KOSPI/KOSDAQ 2행)
        td_sorted = sorted(set(
            r[0] for r in conn.execute(
                "SELECT DISTINCT date FROM market_index ORDER BY date"
            ).fetchall()
        ))
        td_index = {d: i for i, d in enumerate(td_sorted)}
        last_td = td_sorted[-1] if td_sorted else "20260101"

        from datetime import date as _date, timedelta as _td
        def _estimate_expiry(rdate: str, offset: int = 60) -> str:
            """거래일 목록 내에 있으면 정확한 날짜, 범위 초과면 주말 제외 추정."""
            if rdate in td_index:
                idx_r = td_index[rdate] + offset
                if idx_r < len(td_sorted):
                    return td_sorted[idx_r]
                # 남은 거래일을 주말 건너뛰며 추정
                remaining = idx_r - len(td_sorted) + 1
                cur = _date(int(last_td[:4]), int(last_td[4:6]), int(last_td[6:8]))
                counted = 0
                while counted < remaining:
                    cur += _td(days=1)
                    if cur.weekday() < 5:  # 월~금
                        counted += 1
                return cur.strftime("%Y%m%d")
            # rdate가 목록에 없으면 달력 기준 근사 (60거래일 ≈ 84일)
            from datetime import date as _d2
            rd = _d2(int(rdate[:4]), int(rdate[4:6]), int(rdate[6:8]))
            return (rd + _td(days=int(offset * 7 / 5))).strftime("%Y%m%d")

        for sym, rdate, name in open_rows:
            expiry = _estimate_expiry(rdate, 60)
            expiry_y = int(expiry[:4])
            expiry_m = int(expiry[4:6])
            if expiry_y == year and expiry_m == month:
                label = f"{name or sym}({sym})" if name else sym
                events.append({
                    "date": expiry,
                    "type": "position_expiry",
                    "color": "orange",
                    "label": f"만기: {label}",
                    "ticker": sym,
                })

        # 3. 배당기준일 (초록)
        div_rows = conn.execute(
            """SELECT d.symbol, d.record_date, d.ex_dividend_date, d.dps, s.name
               FROM dividends d
               LEFT JOIN stocks s ON d.symbol = s.symbol
               WHERE d.record_date BETWEEN ? AND ?
                  OR d.ex_dividend_date BETWEEN ? AND ?""",
            (first_day, last_day, first_day, last_day),
        ).fetchall()
        for sym, rdate, ex_date, dps, name in div_rows:
            label = f"{name or sym}({sym})" if name else sym
            if rdate and first_day <= rdate <= last_day:
                events.append({
                    "date": rdate,
                    "type": "dividend_record",
                    "color": "green",
                    "label": f"배당기준일: {label}" + (f" ({dps:,}원)" if dps else ""),
                    "ticker": sym,
                })
            if ex_date and first_day <= ex_date <= last_day:
                events.append({
                    "date": ex_date,
                    "type": "dividend_exdate",
                    "color": "green",
                    "label": f"배당락일: {label}" + (f" ({dps:,}원)" if dps else ""),
                    "ticker": sym,
                })

        # 4. 시그널 이벤트 (빨강)
        sig_rows = conn.execute(
            """SELECT event_type, ticker, message, created_at
               FROM signal_log
               WHERE substr(replace(created_at, '-', ''), 1, 8) BETWEEN ? AND ?
               ORDER BY created_at""",
            (first_day, last_day),
        ).fetchall()
        for etype, ticker, message, created_at in sig_rows:
            date_part = created_at[:10].replace("-", "")
            events.append({
                "date": date_part,
                "type": "signal_event",
                "color": "red",
                "label": message,
                "ticker": ticker,
            })

    # 5. 시스템 일정 (보라) — 하드코딩
    system_schedules = [
        {"date": "20261001", "label": "정기 재학습 예정 (5d+60d)"},
        {"date": "20261201", "label": "정기 재학습 예정 (5d+60d)"},
    ]
    for s in system_schedules:
        sdate = s["date"]
        sy, sm = int(sdate[:4]), int(sdate[4:6])
        if sy == year and sm == month:
            events.append({
                "date": sdate,
                "type": "system_schedule",
                "color": "purple",
                "label": s["label"],
                "ticker": None,
            })

    # 날짜순 정렬
    events.sort(key=lambda x: x["date"])
    return events


@app.get("/api/stocks/search", summary="종목 코드/이름 자동완성 검색")
async def stocks_search(
    q: str = Query(default="", description="종목코드 또는 종목명 검색어"),
    limit: int = Query(default=15, ge=1, le=50),
) -> List[Dict[str, Any]]:
    q_str = q.strip()
    if not q_str:
        return []
    q_upper = q_str.upper()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT symbol, name, market FROM stocks
            WHERE (UPPER(symbol) LIKE ? OR name LIKE ?)
              AND name NOT LIKE '%ETF%'
              AND name NOT LIKE '%ETN%'
              AND name NOT LIKE '%스팩%'
            ORDER BY
              CASE
                WHEN UPPER(symbol) = ? THEN 0
                WHEN UPPER(symbol) LIKE ? THEN 1
                WHEN name LIKE ? THEN 2
                ELSE 3
              END,
              symbol
            LIMIT ?
            """,
            (f"%{q_upper}%", f"%{q_str}%", q_upper, f"{q_upper}%", f"{q_str}%", limit),
        ).fetchall()
    return [{"symbol": r["symbol"], "name": r["name"], "market": r["market"]} for r in rows]


@app.get("/api/stock/{symbol}/chart", summary="종목 주가 + 투자자별 순매수 차트 데이터")
async def stock_chart_data(
    symbol: str,
    period: str = Query(default="60d", description="조회 기간: 60d | 120d | 1y | 2y | 3y | all"),
) -> List[Dict[str, Any]]:
    period_map = {"60d": 60, "120d": 120, "1y": 252, "2y": 504, "3y": 756}
    if period == "all":
        limit_clause = ""
        query_params: tuple = (symbol, symbol, symbol, symbol)
    else:
        days = period_map.get(period, 60)
        limit_clause = "LIMIT ?"
        query_params = (symbol, days, symbol, symbol, symbol)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        # detail(백필 이력) 우선, kis(최근 30일 롤링)로 보완 — 겹치는 날짜는 detail 우선
        rows = conn.execute(
            f"""
            SELECT p.date, p.open, p.high, p.low, p.close, p.volume,
                   ROUND(k.foreign_net / 100.0, 2) AS foreign_net,
                   ROUND(k.inst_net    / 100.0, 2) AS inst_net,
                   ROUND(k.indiv_net   / 100.0, 2) AS indiv_net
            FROM (
                SELECT date, open, high, low, close, volume FROM prices
                WHERE symbol = ?
                ORDER BY date DESC
                {limit_clause}
            ) p
            LEFT JOIN (
                SELECT symbol, date,
                       foreign_value     AS foreign_net,
                       inst_total_value  AS inst_net,
                       indiv_value       AS indiv_net
                FROM investor_trading_kis_detail
                WHERE symbol = ?
                UNION ALL
                SELECT symbol, date,
                       foreign_net_value AS foreign_net,
                       inst_net_value    AS inst_net,
                       indiv_net_value   AS indiv_net
                FROM investor_trading_kis k2
                WHERE symbol = ?
                  AND NOT EXISTS (
                        SELECT 1 FROM investor_trading_kis_detail d
                        WHERE d.symbol = ? AND d.date = k2.date
                  )
            ) k ON k.date = p.date
            ORDER BY p.date ASC
            """,
            query_params,
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"종목 {symbol} 데이터 없음")
    return [
        {
            "date": r["date"],
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "volume": r["volume"],
            "foreign_net": r["foreign_net"],
            "inst_net": r["inst_net"],
            "indiv_net": r["indiv_net"],
        }
        for r in rows
    ]


_NAVER_STOCK_BASE  = "https://m.stock.naver.com"
_NAVER_CHART_BASE  = "https://fchart.stock.naver.com"
_NAVER_SEARCH_BASE = "https://ac.stock.naver.com"
_NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://m.stock.naver.com/",
}

def _naver_get(base: str, path: str, params: str = "") -> Response:
    url = base + path + (f"?{params}" if params else "")
    req = urllib.request.Request(url, headers=_NAVER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            ct = r.headers.get("Content-Type", "application/json")
        return Response(content=data, media_type=ct)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/api/naver-stock/{rest_path:path}", include_in_schema=False)
async def proxy_naver_stock(rest_path: str):
    return _naver_get(_NAVER_STOCK_BASE, f"/{rest_path}")

@app.get("/api/naver-chart/{rest_path:path}", include_in_schema=False)
async def proxy_naver_chart(rest_path: str):
    return _naver_get(_NAVER_CHART_BASE, f"/{rest_path}")

@app.get("/api/naver-search/{rest_path:path}", include_in_schema=False)
async def proxy_naver_search(rest_path: str):
    return _naver_get(_NAVER_SEARCH_BASE, f"/{rest_path}")


_dist = BACKEND_ROOT.parent / "dist"
if _dist.exists():
    from fastapi.responses import FileResponse

    # index.html은 매번 재검증, 해시 붙은 정적 자산(JS/CSS)은 장기 캐시
    _INDEX_HEADERS = {"Cache-Control": "no-cache"}
    _ASSET_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        static_file = (_dist / full_path).resolve()
        # dist 디렉토리 밖으로 탈출하는 경로 차단 (path traversal)
        if not str(static_file).startswith(str(_dist.resolve())):
            return FileResponse(str(_dist / "index.html"), headers=_INDEX_HEADERS)
        if static_file.is_file():
            headers = _ASSET_HEADERS if full_path.startswith("assets/") else _INDEX_HEADERS
            return FileResponse(str(static_file), headers=headers)
        return FileResponse(str(_dist / "index.html"), headers=_INDEX_HEADERS)
