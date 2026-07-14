"""
예측 및 SHAP 분석 핵심 로직
모든 DB 접근, 모델 추론, SHAP 계산을 담당
"""

import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ml/ 모듈을 import 경로에 추가
sys.path.insert(0, str(Path(__file__).parent.parent / "ml"))

import pickle
from typing import Union
import catboost as cb
from dataset import DB_PATH, FEATURE_COLS, FEATURE_COLS_REDUCED
from ensemble_config import CAT_BLEND_WEIGHT, XGB_BLEND_WEIGHT

# 서빙 시 사용할 피처 목록. _reload_model_and_cache() → set_serving_feature_cols()로
# 모델 로드 때 meta.json에서 읽어와 설정됨(train-serving 일관성 보장).
# 기본값 = FEATURE_COLS_REDUCED (재학습 이후 기준). 구형 모델 로드 시 57개로 덮어씌워짐.
_serving_feature_cols: List[str] = FEATURE_COLS_REDUCED


def set_serving_feature_cols(cols: List[str]) -> None:
    """모델 로드 시 meta.json["feature_cols"]를 여기에 설정. 이후 예측 전체에 반영됨."""
    global _serving_feature_cols
    _serving_feature_cols = cols
    logger.info("서빙 피처 목록 갱신: %d개", len(cols))
from trade_strategy import calculate_strategy

# LightGBM은 폴백 전용 (구버전 .lgb 파일 지원)
try:
    import lightgbm as lgb
    _LGB_AVAILABLE = True
except ImportError:
    _LGB_AVAILABLE = False

MODELS_DIR = Path(__file__).parent.parent / "models"

# CatBoost+XGBoost 가중 블렌드 가중치 (LightGBM 제외) — ensemble_config.py에서 공용으로
# 가져옴(train.py가 calibrator 학습 시 동일 블렌드를 재현해야 하므로 한 곳에서만 정의, 2026-06-22).
# 근거: backend/ml/search_ensemble_weights.py — 현재 운영 모델(target_5d_20260620_160105)의
# val set(252거래일)에서 CatBoost 단독 P@10=43.45%였는데 0.8/0.2 블렌드가 P@10=43.57%,
# P@20=42.26%(동률), P@30=41.27%(거의 동률)로 모든 K에서 CatBoost 단독과 같거나 나음.
# LightGBM은 모든 조합에서 추가해도 도움이 안 돼(가장 과적합+최저성능) 제외.
# 주의: 단일 val 구간 기준의 작은 차이(<0.2%p)라 노이즈일 수 있음 — 모의투자 실거래로 추가 검증 필요.


class _CatXgbBlend:
    """CatBoost(주) + XGBoost(보조) 가중 평균 블렌드. LightGBM은 포함 안 함(위 주석 참고).
    sklearn 스타일 predict_proba(X) -> (n, 2) 인터페이스를 유지해 기존 호출부와 호환."""

    def __init__(self, cat_model: "cb.CatBoostClassifier", xgb_model, cat_weight: float, xgb_weight: float):
        self.cat = cat_model
        self.xgb = xgb_model
        self.cat_weight = cat_weight
        self.xgb_weight = xgb_weight

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        p1 = (
            self.cat_weight * self.cat.predict_proba(X)[:, 1]
            + self.xgb_weight * self.xgb.predict_proba(X)[:, 1]
        )
        return np.column_stack([1 - p1, p1])


_AnyModel = Union["cb.CatBoostClassifier", "lgb.Booster", _CatXgbBlend]

MIN_MARKET_VOL = 1_000_000_000  # 시장 추세 계산 시 최소 거래대금 (10억)

# 피처 한국어 라벨 (UI 표시용)
FEATURE_LABELS: Dict[str, str] = {
    "ret_1d": "1일 수익률", "ret_5d": "5일 수익률",
    "ret_20d": "20일 수익률", "ret_60d": "60일 수익률",
    "ma5_dev": "5일MA 이격도", "ma20_dev": "20일MA 이격도",
    "ma60_dev": "60일MA 이격도", "ma120_dev": "120일MA 이격도",
    "rsi_7": "RSI(7)", "rsi_14": "RSI(14)",
    "macd": "MACD", "macd_signal": "MACD Signal", "macd_hist": "MACD Hist",
    "bb_pct": "볼린저 위치", "bb_width": "볼린저 폭",
    "vol_ratio_5d": "거래량비(5일)", "vol_ratio_20d": "거래량비(20일)",
    "vol_surge": "거래량 급등",
    "vol_krw_5d": "거래대금(5일평균)", "vol_krw_20d": "거래대금(20일평균)",
    "atr_14": "ATR(14)", "atr_pct": "ATR 비율",
    "gap_pct": "갭 비율", "body_ratio": "몸통 비율",
    "upper_shadow": "윗꼬리", "lower_shadow": "아래꼬리",
    "up_streak": "연속 상승일", "down_streak": "연속 하락일",
    "rel_market_1d": "시장대비(1일)", "rel_market_5d": "시장대비(5일)",
    "rel_market_20d": "시장대비(20일)",
    "rel_sector_5d": "섹터대비(5일)", "rel_sector_20d": "섹터대비(20일)",
    "foreign_rate": "외국인 비율", "foreign_1d_chg": "외국인 1일변화",
    "foreign_5d_chg": "외국인 5일변화", "foreign_trend": "외국인 추세",
    "per": "PER", "pbr": "PBR",
    # lag 피처
    "ret_lag_1": "1일전 수익률", "ret_lag_2": "2일전 수익률",
    "ret_lag_3": "3일전 수익률", "ret_lag_5": "5일전 수익률",
    "ret_lag_10": "10일전 수익률",
    "vol_ratio_lag_1": "1일전 거래량비", "vol_ratio_lag_3": "3일전 거래량비",
    "vol_ratio_lag_5": "5일전 거래량비",
    "up_days_5": "5일 상승일수", "up_days_10": "10일 상승일수",
    "volatility_5": "5일 변동성", "volatility_20": "20일 변동성",
    "price_position_20": "20일 가격위치", "price_position_60": "60일 가격위치",
}


# ── 모델 관련 ─────────────────────────────────────────────────

def get_market_trend(days: int = 30) -> Dict:
    """
    최근 N거래일 유동성 종목(10억+)의 중앙값 일간 수익률로 시장 추세 산출.
    반환: trend(bull/sideways/bear), label, ret_5d_pct, ret_20d_pct, confidence, badge, dates, returns
    """
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            """SELECT symbol, date, close, CAST(close AS REAL)*CAST(volume AS REAL) AS vol_krw
               FROM prices
               WHERE date >= (
                 SELECT MIN(d) FROM (
                   SELECT DISTINCT date AS d FROM prices ORDER BY date DESC LIMIT ?
                 )
               )
               ORDER BY symbol, date""",
            conn, params=(days + 5,)
        )

    if df.empty:
        return {"trend": "unknown", "label": "알 수 없음", "ret_5d_pct": 0, "ret_20d_pct": 0,
                "confidence": "low", "badge": None, "dates": [], "returns": []}

    df = df[df["vol_krw"] >= MIN_MARKET_VOL].copy()
    df["ret"] = df.groupby("symbol")["close"].pct_change()
    daily_ret = (
        df.dropna(subset=["ret"])
        .groupby("date")["ret"]
        .median()
        .sort_index()
        .tail(days)
    )

    if len(daily_ret) < 5:
        return {"trend": "unknown", "label": "알 수 없음", "ret_5d_pct": 0, "ret_20d_pct": 0,
                "confidence": "low", "badge": None, "dates": [], "returns": []}

    ret_5d  = float((1 + daily_ret.tail(5)).prod() - 1)
    ret_20d = float((1 + daily_ret.tail(20)).prod() - 1)

    if ret_20d > 0.03:
        trend, label, confidence, badge = "bull", "강세장", "high", None
    elif ret_20d < -0.03:
        trend, label, confidence, badge = "bear", "약세장", "low", "신중 권고"
    else:
        trend, label, confidence, badge = "sideways", "횡보장", "medium", None

    return {
        "trend":        trend,
        "label":        label,
        "ret_5d_pct":   round(ret_5d * 100, 2),
        "ret_20d_pct":  round(ret_20d * 100, 2),
        "confidence":   confidence,
        "badge":        badge,
        "dates":        list(daily_ret.index[-20:]),
        "returns":      [round(v * 100, 3) for v in daily_ret.tail(20).values],
    }


def _active_model_pointer_path(target_col: str) -> Path:
    return MODELS_DIR / f"ACTIVE_{target_col}.txt"


def set_active_model_dir(target_col: str, dir_name: str) -> None:
    """운영 모델을 명시적으로 지정. train.py가 정식 학습 완료 시 호출 — 이걸 호출하지
    않는 한(=tune_catboost.py, train_2022.py, 진단/실험용 ad-hoc 재학습 등) 아무리
    `target_5d_YYYYMMDD_HHMMSS` 형식으로 새 디렉터리를 만들어도 자동 서빙되지 않음."""
    _active_model_pointer_path(target_col).write_text(dir_name, encoding="utf-8")


def find_latest_model_dir(target_col: str = "target_1d") -> Optional[Path]:
    """운영 모델 디렉터리 결정.

    1순위: models/ACTIVE_{target_col}.txt에 명시된 디렉터리 (존재하고 유효하면 무조건 이것
           — mtime은 더 이상 보지 않음). train.py가 정식 학습 완료 시 set_active_model_dir()로
           갱신하므로 `POST /api/admin/retrain` 자동 반영 동작은 그대로 유지됨.
    2순위(포인터가 없거나 가리키는 디렉터리가 무효할 때만): mtime 기준 스캔 + 디렉터리명이
           정확히 `{target_col}_YYYYMMDD_HHMMSS` 형식인 것만 후보(화이트리스트) — 마이그레이션/
           최초 실행 안전망. 찾으면 포인터 파일도 같이 써서 이후엔 항상 1순위를 타게 함(자가 치유).

    (2026-06-21: rejected_/pending_ 이름 변경을 깜빡한 실험 모델이 mtime 최신이라 잠깐
    운영에 올라간 사고 발생. 재현 테스트 결과 "이름 형식은 정상인데 내용이 안 좋은" 디렉터리는
    화이트리스트만으론 못 막는다는 게 확인돼 포인터 파일을 1차 방어선으로 추가함.)"""
    pointer = _active_model_pointer_path(target_col)
    if pointer.exists():
        name = pointer.read_text(encoding="utf-8").strip()
        model_dir = MODELS_DIR / name
        if name and ((model_dir / "model_cat.pkl").exists() or (model_dir / "model.lgb").exists()):
            return model_dir
        logger.warning("ACTIVE 포인터(%s)가 가리키는 모델이 없음 — mtime 안전망으로 폴백", name)

    import re
    pattern = re.compile(rf"^{re.escape(target_col)}_\d{{8}}_\d{{6}}$")

    # 디렉토리 생성 시간 기준 정렬 (이름 알파벳 순 X — "tuned" 접두어가 숫자보다 ASCII 뒤에 위치해 오정렬됨)
    candidates = sorted(
        (p for p in MODELS_DIR.glob(f"{target_col}_*/model_cat.pkl") if pattern.match(p.parent.name)),
        key=lambda p: p.parent.stat().st_mtime,
    )
    found = candidates[-1].parent if candidates else None
    if found is None:
        candidates_lgb = sorted(
            (p for p in MODELS_DIR.glob(f"{target_col}_*/model.lgb") if pattern.match(p.parent.name)),
            key=lambda p: p.parent.stat().st_mtime,
        )
        found = candidates_lgb[-1].parent if candidates_lgb else None

    if found is not None:
        set_active_model_dir(target_col, found.name)
    return found


def load_model(target_col: str = "target_1d") -> Tuple[_AnyModel, Path]:
    model_dir = find_latest_model_dir(target_col)
    if model_dir is None:
        raise FileNotFoundError(f"저장된 모델 없음: {target_col}")
    cat_pkl = model_dir / "model_cat.pkl"
    xgb_pkl = model_dir / "model_xgb.pkl"
    if cat_pkl.exists():
        with open(cat_pkl, "rb") as f:
            cat_model = pickle.load(f)
        if xgb_pkl.exists():
            with open(xgb_pkl, "rb") as f:
                xgb_model = pickle.load(f)
            return _CatXgbBlend(cat_model, xgb_model, CAT_BLEND_WEIGHT, XGB_BLEND_WEIGHT), model_dir
        return cat_model, model_dir
    # 폴백: LightGBM
    if _LGB_AVAILABLE:
        return lgb.Booster(model_file=str(model_dir / "model.lgb")), model_dir
    raise FileNotFoundError(f"model_cat.pkl 없음: {model_dir}")


def find_latest_bear_regime_model_dir(target_col: str = "target_5d") -> Optional[Path]:
    """train_regime.py가 저장한 국면별 분리 모델 중 최신 bear 모델 디렉터리를 찾음
    (model_cat_bear.pkl 존재 + 디렉터리명이 target_{col}_regime_YYYYMMDD_HHMMSS 형식인 것만 —
    compare_regime_model.py가 만든 실험용 target_{col}_regime_compare_* 디렉터리는 제외)."""
    import re
    pattern = re.compile(rf"^{re.escape(target_col)}_regime_\d{{8}}_\d{{6}}$")
    candidates = [
        p for p in MODELS_DIR.glob(f"{target_col}_regime_*/model_cat_bear.pkl")
        if pattern.match(p.parent.name)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.parent.stat().st_mtime).parent


def load_bear_regime_model(target_col: str = "target_5d") -> Optional[Tuple["cb.CatBoostClassifier", Path]]:
    """약세장 가드 활성화 시 사용할 bear 전용 모델 로드. 없으면 None(기존 통합 모델만 사용)."""
    model_dir = find_latest_bear_regime_model_dir(target_col)
    if model_dir is None:
        return None
    with open(model_dir / "model_cat_bear.pkl", "rb") as f:
        model = pickle.load(f)
    return model, model_dir


def load_calibrator(model_dir: Path) -> Optional[Any]:
    """저장된 확률 calibrator(calibrator.pkl, IsotonicRegression) 로드 — 없으면 None
    (보정 없이 raw 확률을 그대로 표시). train.py가 정식 학습 시마다 같이 저장(2026-06-22,
    진단#7 후속). 순위/추천 선정에는 전혀 쓰이지 않고 화면 표시값 계산에만 사용."""
    path = model_dir / "calibrator.pkl"
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as exc:
        logger.warning("calibrator 로드 실패(%s) — 보정 없이 raw 확률 사용: %s", path, exc)
        return None


def load_model_info(model_dir: Path) -> Dict:
    meta_path = model_dir / "meta.json"
    if not meta_path.exists():
        return {}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    v = meta.get("version", "")
    if len(v) >= 15:
        meta["trained_at"] = f"{v[:4]}-{v[4:6]}-{v[6:8]} {v[9:11]}:{v[11:13]}:{v[13:15]}"
    return meta


def load_backtest_data(model_dir: Path, target_col: str = "target_1d") -> Optional[Dict]:
    if target_col == "target_5d":
        p5 = model_dir / "backtest_5d_data.json"
        if p5.exists():
            return json.loads(p5.read_text(encoding="utf-8"))
    p = model_dir / "backtest_data.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ── KOSPI 약세장 신호 ─────────────────────────────────────────

def get_kospi_bear_signal() -> Dict:
    """
    KOSPI market_index 데이터로 약세장 신호 계산.
    반환:
      ret_5d      : KOSPI 최근 5거래일 수익률
      reduce_half : True → top_n 절반 (5d_ret < -3%)
      reduce_quarter : True → top_n 1/4 (5d_ret < -5%)
      guard_active: True → 어떤 형태로든 가드 작동 중
      guard_reason: 사람이 읽기 좋은 이유 문자열
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT date, close FROM market_index "
                "WHERE code='1001' ORDER BY date DESC LIMIT 10"
            ).fetchall()
    except Exception:
        rows = []

    if len(rows) < 6:
        return {
            "ret_5d": 0.0,
            "reduce_half": False,
            "reduce_quarter": False,
            "guard_active": False,
            "guard_reason": "KOSPI 데이터 부족",
        }

    rows_sorted = sorted(rows, key=lambda r: r[0])  # 날짜 오름차순
    latest_close = float(rows_sorted[-1][1])
    close_5d_ago = float(rows_sorted[-6][1])
    ret_5d = (latest_close / close_5d_ago - 1) if close_5d_ago > 0 else 0.0

    reduce_quarter = ret_5d < -0.05
    reduce_half    = (not reduce_quarter) and ret_5d < -0.03

    guard_active = reduce_half or reduce_quarter
    if reduce_quarter:
        reason = f"KOSPI 5일 수익률 {ret_5d*100:.1f}% (<-5%) — top_n 1/4 축소"
    elif reduce_half:
        reason = f"KOSPI 5일 수익률 {ret_5d*100:.1f}% (<-3%) — top_n 절반 축소"
    else:
        reason = f"KOSPI 5일 수익률 {ret_5d*100:.1f}% (정상)"

    return {
        "ret_5d":          round(ret_5d * 100, 2),
        "reduce_half":     bool(reduce_half),
        "reduce_quarter":  bool(reduce_quarter),
        "guard_active":    bool(guard_active),
        "guard_reason":    reason,
    }


# ── G2 레짐 게이트 ────────────────────────────────────────────
# regime_gate_cumulative.py(2026-07-08) 누적합 기준 검증:
# IS G2 +80.1%p / OOS G2 +155.8%p. avg_ret bias 제거 후도 동일 방향.
# OOS bear 독립 구간 1개(2025-01-02~03-24)라 통계 보류, 페이퍼 전용 채택.

def get_regime_gate_g2() -> Dict:
    """
    KOSPI ma60/ma120 교차 기반 G2 레짐 게이트 상태.
    G2: bear 2연속 → 신규진입 차단, bull 2연속(차단 상태에서) → 재개.
    bear = KOSPI ma60 <= ma120
    반환:
      regime        : 최신일 'bull' | 'bear'
      bear_streak   : 현재 연속 bear 일수
      bull_streak   : 현재 연속 bull 일수
      gate_blocked  : True → 신규진입 차단
      reason        : 상태 설명
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT date, close FROM market_index WHERE code='1001' ORDER BY date"
            ).fetchall()
    except Exception:
        rows = []

    if len(rows) < 120:
        return {
            "regime": "bull",
            "bear_streak": 0,
            "bull_streak": 0,
            "gate_blocked": False,
            "reason": "KOSPI 데이터 부족 — 게이트 미작동",
        }

    closes = np.array([float(r[1]) for r in rows])

    def _rolling_mean(arr: np.ndarray, n: int) -> np.ndarray:
        result = np.full(len(arr), np.nan)
        for i in range(n - 1, len(arr)):
            result[i] = arr[i - n + 1 : i + 1].mean()
        return result

    ma60  = _rolling_mean(closes, 60)
    ma120 = _rolling_mean(closes, 120)

    blocked = False
    bear_streak = 0
    bull_streak = 0

    for i in range(len(closes)):
        if np.isnan(ma60[i]) or np.isnan(ma120[i]):
            continue
        if ma60[i] <= ma120[i]:   # bear
            bear_streak += 1
            bull_streak = 0
        else:                      # bull
            bull_streak += 1
            bear_streak = 0

        if bear_streak >= 2:
            blocked = True
        if blocked and bull_streak >= 2:
            blocked = False

    last_valid = next(
        (i for i in range(len(closes) - 1, -1, -1)
         if not np.isnan(ma60[i]) and not np.isnan(ma120[i])),
        None,
    )
    latest_regime = "bear" if (last_valid is not None and ma60[last_valid] <= ma120[last_valid]) else "bull"

    if blocked:
        reason = (
            f"G2 게이트 차단 — bear {bear_streak}연속 후 bull {bull_streak}연속"
            " (재개: 2연속 bull 필요)"
        )
    elif latest_regime == "bear" and bear_streak == 1:
        reason = "G2 게이트 허용 — bear 1연속 (차단 조건: 2연속)"
    else:
        streak_cnt = bull_streak if latest_regime == "bull" else bear_streak
        reason = f"G2 게이트 허용 — {latest_regime} {streak_cnt}연속"

    return {
        "regime":       latest_regime,
        "bear_streak":  int(bear_streak),
        "bull_streak":  int(bull_streak),
        "gate_blocked": bool(blocked),
        "reason":       reason,
    }


# ── 변동성 국면 (저변동성 구간 신뢰도 경고) ────────────────────
# 진단(backend/ml/diagnose_model.py) 결과: kospi_volatility_20d 4분위 중
# Q1(저변동성)은 top10 정밀도가 24.7%로 베이스레이트 수준까지 떨어짐(Q4는 41.8%).
# 단, 실제 5일 보유 수익률 자체는 Q1에서도 양(+)이라(백테스트 검증, 손실 확대 근거 없음)
# 추천 개수를 줄이는 조치는 적용하지 않고 "신뢰도 낮음" 경고만 표시한다.
# 약세장 가드(get_kospi_bear_signal)는 "방향"(하락 여부), 이건 "크기"(변동성)라
# 서로 독립적인 신호 — 동시에 활성화될 수 있으며 그래도 무방함(중복 아님).
_VOL_QUARTILE_PRECISION_HINT = {"Q1": 0.247, "Q2": 0.317, "Q3": 0.412, "Q4": 0.418}


def get_volatility_regime() -> Dict:
    """
    현재 KOSPI 20일 변동성(kospi_volatility_20d)이 과거 분포 기준 어느 4분위에
    속하는지 판별. Q1~Q2(저변동성)면 low_vol_warning=True.
    반환: quartile, current_value, thresholds, low_vol_warning,
          historical_precision_at_10(진단 시점 참고용 정밀도), reason
    """
    # kospi_volatility_20d는 시장 전체에 broadcast된 값이라 종목 무관하게 날짜당 동일함
    # (market_regime.py가 KOSPI 지수만으로 계산해 모든 symbol 행에 그대로 복사).
    # 예전엔 `SELECT DISTINCT date, ... FROM features`로 4M+ 행 전체를 스캔해 매 요청마다
    # ~27초가 걸렸음(/api/predictions/today 30초 지연의 원인) — 종목 하나(005930, 상장폐지
    # 위험 없는 대형주)만 조회해도 동일한 값을 훨씬 빠르게 얻을 수 있어 변경.
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT date, kospi_volatility_20d FROM features "
                "WHERE symbol = '005930' AND kospi_volatility_20d IS NOT NULL ORDER BY date"
            ).fetchall()
    except Exception:
        rows = []

    if len(rows) < 20:
        return {
            "quartile": None, "current_value": None, "thresholds": None,
            "low_vol_warning": False, "historical_precision_at_10": None,
            "reason": "변동성 데이터 부족",
        }

    values = [r[1] for r in rows]
    current = float(values[-1])
    q1, q2, q3 = (float(v) for v in np.percentile(values, [25, 50, 75]))

    if current <= q1:
        quartile = "Q1"
    elif current <= q2:
        quartile = "Q2"
    elif current <= q3:
        quartile = "Q3"
    else:
        quartile = "Q4"

    low_vol_warning = quartile in ("Q1", "Q2")
    precision_hint = _VOL_QUARTILE_PRECISION_HINT[quartile]

    return {
        "quartile":                   quartile,
        "current_value":              round(current, 5),
        "thresholds":                 {"q1": round(q1, 5), "q2": round(q2, 5), "q3": round(q3, 5)},
        "low_vol_warning":            low_vol_warning,
        "historical_precision_at_10": precision_hint,
        "reason": (
            f"현재 변동성 {quartile} 구간 (20일 KOSPI 표준편차 {current:.4f}) — "
            f"진단 시점 기준 이 구간 top10 정밀도 약 {precision_hint*100:.0f}%"
        ),
    }


# ── DB 쿼리 ──────────────────────────────────────────────────

def get_latest_feature_date() -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT MAX(date) FROM features").fetchone()
    if not row or row[0] is None:
        raise RuntimeError("features 테이블이 비어 있음")
    return row[0]


def _date_minus_days(date_str: str, days: int) -> str:
    """YYYYMMDD에서 N일 전 날짜 반환."""
    dt = datetime.strptime(date_str, "%Y%m%d")
    return (dt - timedelta(days=days)).strftime("%Y%m%d")


def _compute_rolling_price_features(prices_df: pd.DataFrame, target_date: str) -> pd.DataFrame:
    """
    prices_df (symbol, date, close, volume)에서
    vol_krw_5d, vol_krw_20d 계산 후 target_date 행만 반환.
    """
    if prices_df.empty:
        return pd.DataFrame(columns=["symbol", "vol_krw_5d", "vol_krw_20d", "log_market_cap"])

    prices_df = prices_df.copy()
    prices_df["volume_krw"] = (
        prices_df["close"].astype(float) *
        prices_df["volume"].fillna(0).astype(float)
    )
    prices_df = prices_df.sort_values(["symbol", "date"])

    prices_df["vol_krw_5d"] = prices_df.groupby("symbol")["volume_krw"].transform(
        lambda x: np.log1p(x.rolling(5, min_periods=1).mean())
    )
    prices_df["vol_krw_20d"] = prices_df.groupby("symbol")["volume_krw"].transform(
        lambda x: np.log1p(x.rolling(20, min_periods=5).mean())
    )

    return prices_df[prices_df["date"] == target_date][
        ["symbol", "vol_krw_5d", "vol_krw_20d"]
    ]


def _enrich_with_price_features(feat_df: pd.DataFrame, date: str) -> pd.DataFrame:
    """
    features DataFrame에 vol_krw_5d, vol_krw_20d, log_market_cap 컬럼 추가.
    prices 테이블 최근 35일 데이터를 로드해 rolling 계산.
    """
    if feat_df.empty:
        return feat_df

    start_date = _date_minus_days(date, 35)
    symbols = set(feat_df["symbol"].tolist())

    with sqlite3.connect(DB_PATH) as conn:
        if len(symbols) <= 50:
            ph = ",".join("?" * len(symbols))
            prices_df = pd.read_sql_query(
                f"SELECT symbol, date, close, volume FROM prices "
                f"WHERE symbol IN ({ph}) AND date>=? AND date<=? ORDER BY symbol, date",
                conn, params=(*sorted(symbols), start_date, date),
            )
        else:
            # 심볼이 많으면 날짜 범위로만 로드 후 in-memory 필터 (SQLite 파라미터 한계 회피)
            prices_df = pd.read_sql_query(
                "SELECT symbol, date, close, volume FROM prices "
                "WHERE date>=? AND date<=? ORDER BY symbol, date",
                conn, params=(start_date, date),
            )
            prices_df = prices_df[prices_df["symbol"].isin(symbols)]

    enriched = _compute_rolling_price_features(prices_df, date)

    feat_df = feat_df.copy()
    if enriched.empty:
        feat_df["vol_krw_5d"]  = np.nan
        feat_df["vol_krw_20d"] = np.nan
    else:
        feat_df = feat_df.merge(enriched, on="symbol", how="left")

    return feat_df


def _load_features_for_date(date: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        feat_df = pd.read_sql_query(
            "SELECT * FROM features WHERE date = ?", conn, params=(date,)
        )
    return _enrich_with_price_features(feat_df, date)


# ── 전체 순위 캐시 (날짜별, 서버 재시작 전까지 유효) ──────────────
_rank_cache: dict[str, tuple[list[str], list[float]]] = {}

def _get_rank_order(model: "_AnyModel", date: str) -> tuple[list[str], list[float]]:
    """전체 종목 확률 계산 + 순위 순서 반환 — 같은 날짜는 캐시 재사용."""
    if date in _rank_cache:
        return _rank_cache[date]
    all_df = _load_features_for_date(date)
    if all_df.empty:
        _rank_cache[date] = ([], [])
        return [], []
    avail = [c for c in _serving_feature_cols if c in all_df.columns]
    p_all = _predict_proba(model, all_df[avail].astype(float).values)
    order = list(all_df["symbol"].values[np.argsort(p_all)[::-1]])
    _rank_cache[date] = (order, list(p_all))
    return order, list(p_all)


# ── 신뢰도 / 위험도 계산 ─────────────────────────────────────

def _compute_confidence(prob: float, vol_ratio_20d, ret_5d) -> str:
    """HIGH / MEDIUM / LOW"""
    def _ok(v):
        return v is not None and not (isinstance(v, float) and np.isnan(v))

    if (prob >= 0.65
            and _ok(vol_ratio_20d) and float(vol_ratio_20d) >= 1.2
            and _ok(ret_5d) and float(ret_5d) > 0):
        return "HIGH"
    if prob >= 0.60:
        return "MEDIUM"
    return "LOW"


def _compute_risk(
    volatility_20,
    ret_5d,
    market: str,
    vol20_threshold: Optional[float],
) -> Tuple[str, List[str]]:
    """(risk_level, risk_factors)"""
    factors: List[str] = []

    def _fval(v):
        try:
            f = float(v)
            return None if np.isnan(f) else f
        except (TypeError, ValueError):
            return None

    v20 = _fval(volatility_20)
    r5  = _fval(ret_5d)

    if v20 is not None and vol20_threshold is not None and v20 > vol20_threshold:
        factors.append("고변동성")
    if r5 is not None and r5 > 0.20:
        factors.append("단기 급등")
    if market == "KOSDAQ":
        factors.append("코스닥")

    level = "HIGH" if len(factors) >= 2 else ("MEDIUM" if factors else "LOW")
    return level, factors


def _top_reasons(
    feature_names: List[str],
    raw_values: np.ndarray,
    shap_values: np.ndarray,
    n: int = 3,
) -> List[Dict]:
    """양의 SHAP 상위 n개 → top_reasons 리스트."""
    triplets = sorted(
        zip(feature_names, raw_values, shap_values),
        key=lambda x: x[2],   # SHAP 내림차순 (양의 영향 우선)
        reverse=True,
    )
    result = []
    for feat, val, sv in triplets:
        if sv <= 0:
            break
        result.append({
            "feature": feat,
            "value":   None if (isinstance(val, float) and np.isnan(val)) else round(float(val), 4),
            "impact":  round(float(sv), 3),
            "label":   FEATURE_LABELS.get(feat, feat),
        })
        if len(result) >= n:
            break
    return result


# ── SHAP ────────────────────────────────────────────────────

def _predict_proba(model: _AnyModel, X: np.ndarray) -> np.ndarray:
    """모델 종류에 무관하게 양성 확률 반환."""
    if isinstance(model, (cb.CatBoostClassifier, _CatXgbBlend)):
        return model.predict_proba(X)[:, 1]
    # LightGBM Booster
    return model.predict(X)


def _compute_shap(model: _AnyModel, X: np.ndarray, feature_names: Optional[List[str]] = None) -> np.ndarray:
    """SHAP 값 계산. 반환: (n_samples, n_features)
    _CatXgbBlend는 내부 CatBoost 모델 기준 SHAP을 그대로 사용(비중이 가장 큰 모델이라 근사로 충분,
    가중평균 SHAP을 정확히 계산하려면 XGBoost SHAP도 더해야 하나 1단계 범위 밖)."""
    cat_model = model.cat if isinstance(model, _CatXgbBlend) else model
    if isinstance(cat_model, cb.CatBoostClassifier):
        pool = cb.Pool(X, feature_names=feature_names or [])
        contrib = cat_model.get_feature_importance(pool, type="ShapValues")  # (n, n_features + 1)
        return contrib[:, :-1]
    # LightGBM Booster
    contrib = model.predict(X, pred_contrib=True)  # (n, n_features + 1)
    return contrib[:, :-1]


def _shap_entries(
    feature_names: List[str],
    raw_values: np.ndarray,
    shap_values: np.ndarray,
    top_k: Optional[int] = None,
) -> List[Dict]:
    pairs = sorted(
        zip(feature_names, raw_values, shap_values),
        key=lambda x: abs(x[2]),
        reverse=True,
    )
    if top_k is not None:
        pairs = pairs[:top_k]
    return [
        {
            "feature":   feat,
            "label":     FEATURE_LABELS.get(feat, feat),
            "value":     None if np.isnan(val) else round(float(val), 4),
            "shap":      round(float(sv), 4),
            "direction": "up" if sv > 0 else "down",
        }
        for feat, val, sv in pairs
    ]


# ── 예측 함수 ─────────────────────────────────────────────────

def _fetch_close_prices(symbols: List[str], date: str) -> Dict[str, float]:
    """종목 리스트의 해당 날짜 종가를 배치 조회."""
    if not symbols:
        return {}
    ph = ",".join("?" * len(symbols))
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                f"SELECT symbol, close FROM prices WHERE symbol IN ({ph}) AND date = ?",
                (*symbols, date),
            ).fetchall()
        return {sym: float(close) for sym, close in rows if close is not None}
    except Exception:
        return {}


def _fetch_stock_info(symbols: List[str]) -> Dict[str, Dict[str, str]]:
    """종목 리스트의 종목명/KOSPI·KOSDAQ 시장 구분을 배치 조회.
    프론트에서 종목당 별도 외부 API(fetchQuote)로 이름을 받아오던 걸 없애기 위함 —
    이미 로컬 DB에 있는 이름을 응답에 바로 포함시켜 N개 외부 호출의 지연을 제거."""
    if not symbols:
        return {}
    ph = ",".join("?" * len(symbols))
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                f"SELECT symbol, name, market FROM stocks WHERE symbol IN ({ph})",
                symbols,
            ).fetchall()
        return {r[0]: {"name": r[1], "market": r[2]} for r in rows}
    except Exception:
        return {}


def predict_today(
    model: _AnyModel,
    date: str,
    top_n: int = 30,
    shap_top_k: int = 6,
    calibrator: Optional[Any] = None,
) -> List[Dict]:
    """특정 날짜 전종목 예측 → 확률 상위 top_n, 각 SHAP 상위 shap_top_k 포함."""
    features_df = _load_features_for_date(date)
    if features_df.empty:
        return []

    available = [c for c in _serving_feature_cols if c in features_df.columns]
    X = features_df[available].astype(float).values

    proba = _predict_proba(model, X)

    sorted_idx = np.argsort(proba)[::-1][:top_n]
    X_top = X[sorted_idx]
    shap_vals = _compute_shap(model, X_top, feature_names=available)

    top_symbols = [features_df["symbol"].iloc[i] for i in sorted_idx]
    close_map = _fetch_close_prices(top_symbols, date)
    info_map  = _fetch_stock_info(top_symbols)

    # 위험도 판단용 전체 vol20 70th percentile (전체 후보 기준)
    _vol20_col = "volatility_20"
    _vol20_threshold: Optional[float] = None
    if _vol20_col in features_df.columns:
        _all_v20 = features_df[_vol20_col].dropna()
        if len(_all_v20) > 0:
            _vol20_threshold = float(_all_v20.quantile(0.70))

    result = []
    for rank, (orig_idx, shap_row) in enumerate(zip(sorted_idx, shap_vals), start=1):
        sym = features_df["symbol"].iloc[orig_idx]
        features_row = features_df.iloc[orig_idx].to_dict()
        features_row["close"] = close_map.get(sym)

        vr5  = features_df["vol_ratio_5d"].iloc[orig_idx]  if "vol_ratio_5d"  in features_df.columns else None
        vr20 = features_df["vol_ratio_20d"].iloc[orig_idx] if "vol_ratio_20d" in features_df.columns else None
        r1d  = features_df["ret_1d"].iloc[orig_idx]         if "ret_1d"         in features_df.columns else None
        r5d  = features_df["ret_5d"].iloc[orig_idx]         if "ret_5d"         in features_df.columns else None
        v20  = features_df[_vol20_col].iloc[orig_idx]       if _vol20_col        in features_df.columns else None
        per  = features_df["per"].iloc[orig_idx]             if "per"             in features_df.columns else None

        def _safe(v):
            try:
                f = float(v)
                return None if np.isnan(f) else round(f, 4)
            except (TypeError, ValueError):
                return None

        def _safe2(v, digits=2):
            try:
                f = float(v)
                return None if np.isnan(f) else round(f, digits)
            except (TypeError, ValueError):
                return None

        prob_val  = float(proba[orig_idx])
        # 화면 표시용 보정확률 — 순위는 위에서 이미 raw proba로 정렬 완료(sorted_idx), 영향 없음
        prob_calibrated = float(calibrator.transform([prob_val])[0]) if calibrator is not None else prob_val
        sym_info  = info_map.get(sym, {})
        mkt       = sym_info.get("market") or "UNKNOWN"
        name      = sym_info.get("name") or sym
        X_row     = X_top[rank - 1]

        # SHAP 기반 필드들 (1회 계산으로 shap_top과 top_reasons 모두 도출)
        shap_all_entries = _shap_entries(available, X_row, shap_row, top_k=None)
        shap_top_entries = shap_all_entries[:shap_top_k]
        reasons = _top_reasons(available, X_row, shap_row, n=3)

        # 신뢰도 / 위험도
        conf_level                 = _compute_confidence(prob_val, vr20, r5d)
        risk_level, risk_factors   = _compute_risk(v20, r5d, mkt, _vol20_threshold)

        result.append({
            "rank":             rank,
            "symbol":           sym,
            "name":             name,
            "market":           mkt,
            "probability":      round(prob_val, 4),
            "probability_calibrated": round(prob_calibrated, 4),
            "shap_top":         shap_top_entries,
            "strategy":         calculate_strategy(sym, date, features_row),
            "vol_ratio_5d":     _safe2(vr5),
            "vol_ratio_20d":    _safe2(vr20),
            "ret_1d":           _safe(r1d),
            "ret_5d":           _safe(r5d),
            "per":              _safe2(per),
            # 신규: 추천 이유 / 신뢰도 / 위험도
            "top_reasons":      reasons,
            "confidence_level": conf_level,
            "risk_level":       risk_level,
            "risk_factors":     risk_factors,
        })
    return result


def predict_ticker(
    model: _AnyModel,
    symbol: str,
    date: str,
    price_days: int = 60,
    calibrator: Optional[Any] = None,
) -> Optional[Dict]:
    """특정 종목 상세 분석 — 확률, 전체 SHAP, 가격 히스토리, 최근 피처값."""
    with sqlite3.connect(DB_PATH) as conn:
        feat_df = pd.read_sql_query(
            "SELECT * FROM features WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            conn, params=(symbol, date),
        )
        # 거래정지 등으로 OHLC/거래량이 0으로 채워진 행은 day budget에서 제외 —
        # 안 그러면 장기 거래정지 종목은 price_days만큼 더 과거로 못 가고
        # 정지 기간의 placeholder 행만 가득 채워서 가져오게 됨
        price_df = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume FROM prices "
            "WHERE symbol = ? AND date <= ? AND open > 0 AND high > 0 AND low > 0 AND volume > 0 "
            "ORDER BY date DESC LIMIT ?",
            conn, params=(symbol, date, price_days),
        )

    if feat_df.empty:
        return None

    actual_date = feat_df["date"].iloc[0]
    feat_df = _enrich_with_price_features(feat_df, actual_date)

    available = [c for c in _serving_feature_cols if c in feat_df.columns]
    X = feat_df[available].astype(float).values

    proba     = float(_predict_proba(model, X)[0])
    proba_calibrated = float(calibrator.transform([proba])[0]) if calibrator is not None else proba
    shap_vals = _compute_shap(model, X, feature_names=available)[0]

    # 전체 종목 중 순위 (캐시 재사용)
    order, _ = _get_rank_order(model, date)
    rank = order.index(symbol) + 1 if symbol in order else None

    price_df = price_df.sort_values("date")
    price_history = [
        {
            "time":   r["date"],
            "open":   float(r["open"]),
            "high":   float(r["high"]),
            "low":    float(r["low"]),
            "close":  float(r["close"]),
            "volume": int(r["volume"]) if pd.notna(r["volume"]) else 0,
        }
        for r in price_df.to_dict(orient="records")
    ]

    recent_features = {
        col: (
            round(float(feat_df[col].iloc[0]), 4)
            if col in feat_df.columns and pd.notna(feat_df[col].iloc[0])
            else None
        )
        for col in _serving_feature_cols
    }

    features_row = feat_df.iloc[0].to_dict()
    if price_history:
        features_row["close"] = price_history[-1]["close"]

    return {
        "symbol":          symbol,
        "date":            actual_date,
        "probability":     round(proba, 4),
        "probability_calibrated": round(proba_calibrated, 4),
        "rank":            rank,
        "shap_full":       _shap_entries(available, X[0], shap_vals),
        "price_history":   price_history,
        "recent_features": recent_features,
        "strategy":        calculate_strategy(symbol, actual_date, features_row),
    }


# ── 60d 중기 예측 ─────────────────────────────────────────────

def _compute_60d_factors_pit(date: str) -> pd.DataFrame:
    """60d 모델 전용 팩터를 예측 시점에 PIT로 계산.
    factors_60d.compute_factors_for_date()로 위임 — train-serving skew 방지.
    반환: symbol + eps_growth_yoy / eps_growth_accel / roe_level / dps_growth_yoy / bps_growth_yoy."""
    from factors_60d import compute_factors_for_date  # ml/ 경로 이미 sys.path에 있음
    with sqlite3.connect(DB_PATH) as conn:
        return compute_factors_for_date(date, conn)


_model_60d_cache: Optional[Tuple] = None   # (model, model_dir, feat_cols)

def predict_60d(date: str, top_n: int = 30) -> List[Dict]:
    """60d 중기 예측 — features 기반 45피처 + PIT 팩터 5개 → 확률 상위 top_n 반환."""
    global _model_60d_cache

    # 모델 로드 (서버당 1회 캐시)
    if _model_60d_cache is None:
        model_60d, model_dir_60d = load_model("target_60d")
        # meta 파일 양쪽 지원: model_meta.json["features"] (60d 초기) OR meta.json["feature_cols"] (5d와 통일)
        meta_60d: Optional[dict] = None
        for fname, key in [("model_meta.json", "features"), ("meta.json", "feature_cols")]:
            p = model_dir_60d / fname
            if p.exists():
                meta_60d = json.loads(p.read_text(encoding="utf-8"))
                feat_cols_60d = meta_60d.get(key) or []
                if feat_cols_60d:
                    break
        else:
            feat_cols_60d = []
        if not feat_cols_60d:
            raise RuntimeError(
                f"predict_60d: 피처 목록 로드 실패 — {model_dir_60d} 에 "
                "model_meta.json[features] 또는 meta.json[feature_cols]가 없거나 비어 있음"
            )
        _model_60d_cache = (model_60d, model_dir_60d, feat_cols_60d)

    model_60d, model_dir_60d, feat_cols_60d = _model_60d_cache

    # 기본 features 로드
    features_df = _load_features_for_date(date)
    if features_df.empty:
        return []

    features_df["symbol"] = features_df["symbol"].astype(str).str.zfill(6)

    # neg_pbr 파생 (features 테이블 pbr 컬럼)
    if "pbr" in features_df.columns:
        features_df["neg_pbr"] = -features_df["pbr"].astype(float)

    # PIT 팩터 merge
    pit_factors = _compute_60d_factors_pit(date)
    features_df = features_df.merge(pit_factors, on="symbol", how="left")

    available = [c for c in feat_cols_60d if c in features_df.columns]
    if not available:
        logger.error("predict_60d: 사용 가능한 피처 없음 (feat_cols=%s)", feat_cols_60d[:5])
        return []

    X = features_df[available].astype(float).values
    proba = _predict_proba(model_60d, X)

    sorted_idx   = np.argsort(proba)[::-1][:top_n]
    top_symbols  = [features_df["symbol"].iloc[i] for i in sorted_idx]
    close_map    = _fetch_close_prices(top_symbols, date)
    info_map     = _fetch_stock_info(top_symbols)

    result = []
    for rank, orig_idx in enumerate(sorted_idx, start=1):
        sym      = features_df["symbol"].iloc[orig_idx]
        sym_info = info_map.get(sym, {})
        prob_val = float(proba[orig_idx])

        def _safe(v):
            try:
                f = float(v)
                return None if np.isnan(f) else round(f, 4)
            except (TypeError, ValueError):
                return None

        result.append({
            "rank":         rank,
            "symbol":       sym,
            "name":         sym_info.get("name") or sym,
            "market":       sym_info.get("market") or "UNKNOWN",
            "probability":  round(prob_val, 4),
            "close":        close_map.get(sym),
            "eps_growth_yoy":   _safe(features_df["eps_growth_yoy"].iloc[orig_idx] if "eps_growth_yoy" in features_df.columns else None),
            "dps_growth_yoy":   _safe(features_df["dps_growth_yoy"].iloc[orig_idx] if "dps_growth_yoy" in features_df.columns else None),
            "roe_level":        _safe(features_df["roe_level"].iloc[orig_idx] if "roe_level" in features_df.columns else None),
            "neg_pbr":          _safe(features_df["neg_pbr"].iloc[orig_idx] if "neg_pbr" in features_df.columns else None),
        })
    return result


def invalidate_60d_model_cache() -> None:
    """재학습 후 60d 모델 캐시 무효화 — _reload_model_and_cache()에서 호출."""
    global _model_60d_cache
    _model_60d_cache = None
