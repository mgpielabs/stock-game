"""
모의투자 추적 모듈

추천 종목을 paper_trades 테이블에 기록하고,
보유 기간(horizon 거래일) 후 결과를 자동 청산 처리한다.

운용 규칙 v1 (2026-07-08 확정):
  - 진입가: 추천 익일 시가 (당일종가 fallback). 당일종가 진입은 실전 불가.
  - 비용: COMMISSION×2 + SELL_TAX + SLIPPAGE×2 ≈ 0.63% 왕복 차감
  - 5d 리밸런싱: 5거래일마다 신규 진입 (should_enter_5d)
  - 60d 리밸런싱: 60거래일마다 신규 진입 (should_enter_60d)
  - 유동성: 거래대금 10억 미만 종목 스킵
  - 상한가: 당일 상승률 ≥ 29% 종목 스킵 (익일 시가 확보 불가)
  - G2 게이트 차단일: paper_trades 신규진입 없음, shadow에만 기록
  참고: 백테스트(evaluate.py)는 당일종가 진입 가정이라 갭 효과(-2.32%p)만큼 이론 수치가 높음.
        라이브 수치와 직접 비교 시 이 갭을 알려진 구조적 차이로 처리.
"""

import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).parent.parent / "data" / "stocks.db"
MODELS_DIR = Path(__file__).parent.parent / "models"

# evaluate.py 비용 상수 재사용 — 중복 정의 금지
_ML_DIR = Path(__file__).parent.parent / "ml"
if str(_ML_DIR) not in sys.path:
    sys.path.insert(0, str(_ML_DIR))
from evaluate import COMMISSION, SELL_TAX, SLIPPAGE, MIN_VOLUME_KRW, LIMIT_UP

logger = logging.getLogger(__name__)

HOLDING_DAYS = 5  # 5d 슬리브 기본 청산 거래일 수 (호환성 유지)

# 2026-05-24: CatBoost + 약세장 가드 모델로 전환한 날짜
NEW_MODEL_START = "20260524"

# model_version별 성과를 믿을 만하다고 보는 최소 청산 건수 (CLAUDE.md "모델 개선 이력" 참고)
MIN_SAMPLE_FOR_TRUST = 5


# ── 비용 계산 (evaluate.py와 동일 공식) ─────────────────────────

def _net_ret(raw: float) -> float:
    """슬리피지 + 수수료 + 매도세 차감 후 순수익률."""
    buy_cost  = SLIPPAGE + COMMISSION             # 0.215%
    sell_cost = SLIPPAGE + COMMISSION + SELL_TAX  # 0.415%
    return (1 + raw) * (1 - buy_cost) * (1 - sell_cost) - 1


def _get_active_model_version(target_col: str = "target_5d") -> Optional[str]:
    """현재 운영 중인 모델 디렉터리명. server/predictor.py가 쓰는 ACTIVE 포인터 파일을
    그대로 읽음(같은 파일을 보는 거라 import로 결합할 필요 없음 — 단순 텍스트 파일 규약)."""
    pointer = MODELS_DIR / f"ACTIVE_{target_col}.txt"
    if pointer.exists():
        name = pointer.read_text(encoding="utf-8").strip()
        if name:
            return name
    return None


# ── 스키마 마이그레이션 ───────────────────────────────────────

def _ensure_schema() -> None:
    """paper_trades 테이블에 신규 컬럼이 없으면 추가 (기존 행은 NULL 유지)."""
    new_cols = [
        ("confidence_level",    "TEXT"),
        ("risk_level",          "TEXT"),
        ("risk_factors",        "TEXT"),              # JSON 문자열
        ("market_mode",         "TEXT"),
        ("bear_guard_active",   "INTEGER DEFAULT 0"),
        ("market",              "TEXT"),              # KOSPI / KOSDAQ
        ("model_version",       "TEXT"),              # 추천 당시 서빙 모델 (추적성)
        ("regime_gate_blocked", "INTEGER DEFAULT 0"), # G2 게이트 차단 상태 (2026-07-08)
        ("horizon",             "INTEGER DEFAULT 5"), # 5d 또는 60d (2026-07-08)
        ("entry_price",         "INTEGER"),           # 익일 시가 진입가 (청산 시 채워짐)
    ]
    with sqlite3.connect(DB_PATH) as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
        for col, typ in new_cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")
                logger.info("[paper_trader] 스키마 확장: %s %s 추가", col, typ)


_ensure_schema()


# ── 유틸 ────────────────────────────────────────────────────

def _symbol_market(symbol: str) -> Optional[str]:
    if symbol.endswith(".KS"):
        return "KOSPI"
    if symbol.endswith(".KQ"):
        return "KOSDAQ"
    return None


def _get_nth_trading_day_after(from_date: str, n: int) -> Optional[str]:
    """KOSPI 거래일 캘린더 기준, from_date 이후 n번째 거래일 반환."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT date FROM market_index
            WHERE code = '1001' AND date > ?
            ORDER BY date
            LIMIT 1 OFFSET ?
            """,
            (from_date, n - 1),
        ).fetchone()
    return row[0] if row else None


def _count_trading_days_between(start_excl: str, end_incl: str) -> int:
    """start_excl 초과 ~ end_incl 이하 KOSPI 거래일 수."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM market_index
            WHERE code = '1001' AND date > ? AND date <= ?
            """,
            (start_excl, end_incl),
        ).fetchone()
    return row[0] if row else 0


def _get_next_day_open(symbol: str, from_date: str) -> Optional[int]:
    """from_date 다음 거래일 시가. 없으면 None (prediction_logger.py와 동일 로직)."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT p.open FROM prices p "
            "JOIN market_index m ON m.date=p.date AND m.code='1001' "
            "WHERE p.symbol=? AND p.date > ? ORDER BY p.date LIMIT 1",
            (symbol, from_date),
        ).fetchone()
    return int(row[0]) if row and row[0] else None


# ── 리밸런싱 주기 판단 ────────────────────────────────────────

def should_enter_5d(today: str) -> bool:
    """마지막 5d 진입일로부터 5거래일 이상 경과 → True (백테스트 리밸런싱 주기 일치).
    기존 행(horizon IS NULL)도 5d 슬리브로 간주."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT MAX(recommended_date) FROM paper_trades "
            "WHERE horizon = 5 OR horizon IS NULL",
        ).fetchone()
    last = row[0] if row and row[0] else None
    if last is None:
        return True
    return _count_trading_days_between(last, today) >= 5


def should_enter_60d(today: str) -> bool:
    """마지막 60d 진입일로부터 60거래일 이상 경과 → True."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT MAX(recommended_date) FROM paper_trades WHERE horizon = 60",
        ).fetchone()
    last = row[0] if row and row[0] else None
    if last is None:
        return True
    return _count_trading_days_between(last, today) >= 60


# ── 추천 기록 ────────────────────────────────────────────────

def record_recommendations(
    recommended_date: str,
    predictions: List[Dict[str, Any]],
    market_mode: str = "aggressive",
    bear_guard_active: bool = False,
    model_version: Optional[str] = None,
    regime_gate_blocked: bool = False,
    horizon: int = 5,
) -> int:
    """
    추천 종목을 paper_trades에 삽입.
    같은 (symbol, recommended_date) 쌍은 IGNORE (중복 방지).

    유동성 필터 (거래대금 10억 미만) + 상한가 필터 (당일 상승률 ≥ 29%) 적용.
    predictions: predict_today() 응답의 predictions 리스트
    반환: 새로 삽입된 행 수
    """
    if not predictions:
        return 0

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        inserted = 0
        skipped_liquidity = 0
        skipped_limit_up = 0
        for pred in predictions:
            symbol = pred["symbol"]
            row = conn.execute(
                "SELECT close, volume FROM prices WHERE symbol = ? AND date = ?",
                (symbol, recommended_date),
            ).fetchone()
            price  = row[0] if row else None
            volume = row[1] if row else None

            # [유동성 필터] 거래대금 10억 미만 → 스킵
            if price and volume:
                volume_krw = price * volume
                if volume_krw < MIN_VOLUME_KRW:
                    logger.debug("[paper_trader] 유동성 스킵 %s (거래대금 %.0f원)", symbol, volume_krw)
                    skipped_liquidity += 1
                    continue

            # [상한가 필터] 당일 수익률 ≥ 29% → 익일 시가 확보 불확실, 스킵
            if price:
                prev_row = conn.execute(
                    "SELECT close FROM prices WHERE symbol = ? AND date < ? "
                    "ORDER BY date DESC LIMIT 1",
                    (symbol, recommended_date),
                ).fetchone()
                if prev_row and prev_row[0]:
                    today_ret = (price / prev_row[0]) - 1
                    if today_ret >= LIMIT_UP:
                        logger.debug("[paper_trader] 상한가 스킵 %s (당일 +%.1f%%)", symbol, today_ret * 100)
                        skipped_limit_up += 1
                        continue

            rf = pred.get("risk_factors")
            risk_factors_json = json.dumps(rf, ensure_ascii=False) if rf else None

            cur = conn.execute(
                """
                INSERT OR IGNORE INTO paper_trades
                    (symbol, recommended_date, recommended_rank,
                     recommended_prob, recommended_price,
                     confidence_level, risk_level, risk_factors,
                     market_mode, bear_guard_active, market, model_version,
                     regime_gate_blocked, horizon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    recommended_date,
                    pred.get("rank"),
                    pred.get("probability"),
                    price,                          # 참고용 당일 종가 (entry_price와 다름)
                    pred.get("confidence_level"),
                    pred.get("risk_level"),
                    risk_factors_json,
                    market_mode,
                    1 if bear_guard_active else 0,
                    _symbol_market(symbol),
                    model_version,
                    1 if regime_gate_blocked else 0,
                    horizon,
                ),
            )
            inserted += cur.rowcount

        conn.commit()

    if skipped_liquidity or skipped_limit_up:
        logger.info(
            "[paper_trader] %s 필터 스킵: 유동성=%d 상한가=%d",
            recommended_date, skipped_liquidity, skipped_limit_up,
        )
    logger.info(
        "[paper_trader] %s 추천 기록: %d건 삽입 (horizon=%d, mode=%s, guard=%s, model=%s)",
        recommended_date, inserted, horizon, market_mode, bear_guard_active, model_version,
    )
    return inserted


# ── 청산 ────────────────────────────────────────────────────

def close_expired_trades(today: str) -> Dict[str, int]:
    """
    open 상태에서 horizon 거래일 경과 거래를 청산 처리.

    진입가: 추천 익일 시가 (_get_next_day_open). 없으면 recommended_price(당일종가) fallback.
    수익률: _net_ret 비용 차감 후 순수익률.
    종가 데이터 없으면 expired로 표시.
    반환: {"closed": int, "expired": int}
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        open_trades = conn.execute(
            """
            SELECT id, symbol, recommended_date, recommended_price,
                   COALESCE(horizon, 5) AS horizon
            FROM paper_trades WHERE status = 'open'
            """,
        ).fetchall()

        n_closed = 0
        n_expired = 0
        for trade_id, symbol, rec_date, rec_price, horizon in open_trades:
            target_date = _get_nth_trading_day_after(rec_date, horizon)
            if target_date is None or target_date > today:
                continue

            # 청산가: horizon 거래일 후 종가
            price_row = conn.execute(
                "SELECT close FROM prices WHERE symbol = ? AND date = ?",
                (symbol, target_date),
            ).fetchone()

            if price_row is None:
                conn.execute(
                    """
                    UPDATE paper_trades
                    SET status = 'expired', close_date = ?,
                        updated_at = datetime('now','localtime')
                    WHERE id = ?
                    """,
                    (target_date, trade_id),
                )
                n_expired += 1
            else:
                close_price = price_row[0]

                # 진입가: 익일 시가 우선, 없으면 recommended_price(당일종가) fallback
                entry = _get_next_day_open(symbol, rec_date)
                actual_entry = entry if entry else rec_price

                if not actual_entry:
                    conn.execute(
                        """
                        UPDATE paper_trades
                        SET status = 'expired', close_date = ?,
                            updated_at = datetime('now','localtime')
                        WHERE id = ?
                        """,
                        (target_date, trade_id),
                    )
                    n_expired += 1
                else:
                    gross_ret = close_price / actual_entry - 1
                    net = _net_ret(gross_ret)
                    return_pct = round(net * 100, 4)
                    holding = _count_trading_days_between(rec_date, target_date)
                    conn.execute(
                        """
                        UPDATE paper_trades
                        SET status = 'closed',
                            close_date = ?, close_price = ?,
                            entry_price = ?,
                            return_pct = ?, holding_days = ?,
                            updated_at = datetime('now','localtime')
                        WHERE id = ?
                        """,
                        (target_date, close_price, actual_entry, return_pct, holding, trade_id),
                    )
                    n_closed += 1

        conn.commit()

    logger.info("[paper_trader] %s 기준 closed=%d expired=%d", today, n_closed, n_expired)
    return {"closed": n_closed, "expired": n_expired}


# ── 조회 ────────────────────────────────────────────────────

def get_active_trades_enriched(today: str) -> List[Dict[str, Any]]:
    """open 거래에 현재가·미실현 손익·보유 거래일 추가해 반환."""
    trades = get_active_trades()
    if not trades:
        return trades

    with sqlite3.connect(DB_PATH) as conn:
        for t in trades:
            row = conn.execute(
                "SELECT close FROM prices WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                (t["symbol"], today),
            ).fetchone()
            current_price = row[0] if row else None
            # 미실현 수익은 entry_price(익일시가) 기준, 없으면 recommended_price(당일종가) 사용
            ref_price = t.get("entry_price") or t.get("recommended_price")
            t["current_price"] = current_price
            t["unrealized_pct"] = (
                round((current_price / ref_price - 1) * 100, 2)
                if current_price and ref_price else None
            )
            t["holding_days"] = _count_trading_days_between(t["recommended_date"], today)

    return trades


def get_active_trades() -> List[Dict[str, Any]]:
    """현재 open 상태 거래 목록 (종목명 포함)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT pt.*, s.name
            FROM paper_trades pt
            LEFT JOIN stocks s ON pt.symbol = s.symbol
            WHERE pt.status = 'open'
            ORDER BY pt.recommended_date DESC, pt.recommended_rank
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def get_performance_by_model() -> Dict[str, Any]:
    """model_version별 성과 분리 집계.

    - NULL(레거시 — 운용규칙 도입 전 과거 거래), rejected_*(폐기 모델),
      pending_*(검증 대기), 5d/60d ACTIVE 포인터가 가리키는 것(현재 운영 모델),
      target_5d_regime_*(약세장 보조 모델), 그 외(과거 정상 운영 모델)로 분류.
    - 각 모델의 "누적 수익률"은 naive SUM이 아니라 추천일 배치별 평균 수익률의
      누적합(get_performance_timeline과 동일 계산 방식 — CLAUDE.md "참고" 항목 준수).
    - closed < MIN_SAMPLE_FOR_TRUST면 insufficient_sample=True (표본 부족 경고용).
    """
    active_5d = _get_active_model_version("target_5d")
    active_60d = _get_active_model_version("target_60d")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        version_rows = conn.execute("SELECT DISTINCT model_version FROM paper_trades").fetchall()
        version_keys = [r[0] for r in version_rows]

        models: List[Dict[str, Any]] = []
        for version in version_keys:
            if version is None:
                where, params = "model_version IS NULL", ()
            else:
                where, params = "model_version = ?", (version,)

            counts = conn.execute(
                f"""
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status='closed' AND return_pct>0 THEN 1 ELSE 0 END),
                    ROUND(AVG(CASE WHEN status='closed' THEN return_pct END), 2)
                FROM paper_trades WHERE {where}
                """,
                params,
            ).fetchone()
            total, open_n, closed_n, expired_n, win_n, avg_return = counts
            closed_n, win_n, open_n, expired_n = closed_n or 0, win_n or 0, open_n or 0, expired_n or 0

            # 배치(추천일)별 평균 수익률의 누적합 — 단순 SUM 대체
            batch_rows = conn.execute(
                f"""
                SELECT
                    ROUND(AVG(CASE WHEN status='closed' THEN return_pct END), 4) AS avg_ret,
                    SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_in_batch
                FROM paper_trades WHERE {where}
                GROUP BY recommended_date ORDER BY recommended_date
                """,
                params,
            ).fetchall()
            cumulative, any_closed = 0.0, False
            for br in batch_rows:
                if br["closed_in_batch"]:
                    cumulative += (br["avg_ret"] or 0.0)
                    any_closed = True

            is_rejected     = bool(version and version.startswith("rejected_"))
            is_pending      = bool(version and version.startswith("pending_"))
            is_current_5d   = bool(version and active_5d and version == active_5d)
            is_current_60d  = bool(version and active_60d and version == active_60d)
            is_current      = is_current_5d or is_current_60d
            is_regime_bear  = bool(version and not is_current and version.startswith("target_5d_regime_"))

            if version is None:
                label = "레거시 (운용규칙 도입 전)"
            elif is_rejected:
                label = "폐기된 모델"
            elif is_pending:
                label = "검증 대기 모델"
            elif is_current_5d:
                label = "현재 운영 모델 (5d)"
            elif is_current_60d:
                label = "현재 운영 모델 (60d)"
            elif is_regime_bear:
                label = "약세장 보조 모델"
            else:
                label = "과거 운영 모델"

            models.append({
                "model_version":         version,
                "label":                 label,
                "is_current":            is_current,
                "is_current_5d":         is_current_5d,
                "is_current_60d":        is_current_60d,
                "is_regime_bear":        is_regime_bear,
                "is_rejected":           is_rejected,
                "is_pending":            is_pending,
                "is_untracked":          version is None,
                "total":                 total,
                "open":                  open_n,
                "closed":                closed_n,
                "expired":               expired_n,
                "win_count":             win_n,
                "win_rate":              round(win_n / closed_n, 4) if closed_n else None,
                "avg_return_pct":        avg_return,
                "cumulative_return_pct": round(cumulative, 2) if any_closed else None,
                "insufficient_sample":   closed_n < MIN_SAMPLE_FOR_TRUST,
            })

    # 정렬: 현재 모델(5d→60d) → 약세장 보조 → 과거 → 추적 불가(레거시) 맨 뒤
    def _sort_key(m: Dict) -> tuple:
        if m["is_current_5d"]:   return (0, -m["total"])
        if m["is_current_60d"]:  return (1, -m["total"])
        if m["is_regime_bear"]:  return (2, -m["total"])
        if m["is_untracked"]:    return (5, -m["total"])
        return (3, -m["total"])
    models.sort(key=_sort_key)

    current_5d  = next((m for m in models if m["is_current_5d"]),  None)
    current_60d = next((m for m in models if m["is_current_60d"]), None)
    current     = current_5d or current_60d  # 기존 호환

    return {
        "active_model_version":     active_5d,   # 기존 호환
        "active_model_version_5d":  active_5d,
        "active_model_version_60d": active_60d,
        "current_model_stats":      current,
        "current_model_stats_5d":   current_5d,
        "current_model_stats_60d":  current_60d,
        "models":                   models,
    }


def get_performance_summary() -> Dict[str, Any]:
    """전체 성과 요약 (총 건수, 승률, 평균 수익률, best/worst, 최근 20건)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        total = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        status_counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM paper_trades GROUP BY status"
            ).fetchall()
        )

        perf = conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END),
                ROUND(AVG(return_pct), 2),
                ROUND(SUM(return_pct), 2),
                ROUND(AVG(holding_days), 1)
            FROM paper_trades WHERE status = 'closed'
            """,
        ).fetchone()

        best = conn.execute(
            """
            SELECT pt.symbol, s.name, pt.return_pct FROM paper_trades pt
            LEFT JOIN stocks s ON pt.symbol = s.symbol
            WHERE pt.status = 'closed' ORDER BY pt.return_pct DESC LIMIT 1
            """,
        ).fetchone()

        worst = conn.execute(
            """
            SELECT pt.symbol, s.name, pt.return_pct FROM paper_trades pt
            LEFT JOIN stocks s ON pt.symbol = s.symbol
            WHERE pt.status = 'closed' ORDER BY pt.return_pct ASC LIMIT 1
            """,
        ).fetchone()

        recent_rows = conn.execute(
            """
            SELECT pt.*, s.name FROM paper_trades pt
            LEFT JOIN stocks s ON pt.symbol = s.symbol
            ORDER BY pt.updated_at DESC LIMIT 20
            """,
        ).fetchall()
        recent_trades = [dict(r) for r in recent_rows]

    closed_cnt = perf[0] or 0
    win_count = perf[1] or 0

    return {
        "total_trades":     total,
        "open_trades":      status_counts.get("open", 0),
        "closed_trades":    status_counts.get("closed", 0),
        "expired_trades":   status_counts.get("expired", 0),
        "win_count":        win_count,
        "win_rate":         round(win_count / closed_cnt, 4) if closed_cnt else None,
        "avg_return_pct":   perf[2],
        "total_return_pct": perf[3],
        "avg_holding_days": perf[4],
        "best_trade":  {"symbol": best["symbol"],  "name": best["name"],  "return_pct": best["return_pct"]}  if best  else None,
        "worst_trade": {"symbol": worst["symbol"], "name": worst["name"], "return_pct": worst["return_pct"]} if worst else None,
        "recent_trades": recent_trades,
    }


# ── 날짜별 성과 추이 ─────────────────────────────────────────

def get_performance_timeline(
    days: int = 30,
    since: Optional[str] = None,
) -> Dict[str, Any]:
    """
    추천일 단위 성과 집계.
    since: YYYYMMDD — 이 날짜 이후만 집계 (None이면 전체)
    """
    cutoff_date = since or (
        datetime.now() - timedelta(days=days)
    ).strftime("%Y%m%d")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            """
            SELECT
                recommended_date,
                COUNT(*)                                              AS recommended_count,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END)   AS closed_count,
                ROUND(AVG(CASE WHEN status = 'closed' THEN return_pct END), 2) AS avg_return_pct,
                SUM(CASE WHEN status = 'closed' AND return_pct > 0 THEN 1 ELSE 0 END) AS win_count,
                GROUP_CONCAT(DISTINCT COALESCE(model_version, '')) AS model_versions
            FROM paper_trades
            WHERE recommended_date >= ?
            GROUP BY recommended_date
            ORDER BY recommended_date
            """,
            (cutoff_date,),
        ).fetchall()

        # 신뢰도별 성과 (신규 컬럼이 있는 행만)
        conf_rows = conn.execute(
            """
            SELECT
                confidence_level,
                COUNT(*)                                              AS cnt,
                ROUND(AVG(CASE WHEN status = 'closed' THEN return_pct END), 2) AS avg_return,
                SUM(CASE WHEN status = 'closed' AND return_pct > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END)   AS closed
            FROM paper_trades
            WHERE confidence_level IS NOT NULL AND recommended_date >= ?
            GROUP BY confidence_level
            """,
            (cutoff_date,),
        ).fetchall()

        # 시장 국면별 성과 (신규 컬럼이 있는 행만)
        mode_rows = conn.execute(
            """
            SELECT
                market_mode,
                COUNT(*)                                              AS cnt,
                ROUND(AVG(CASE WHEN status = 'closed' THEN return_pct END), 2) AS avg_return,
                SUM(CASE WHEN status = 'closed' AND return_pct > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END)   AS closed
            FROM paper_trades
            WHERE market_mode IS NOT NULL AND recommended_date >= ?
            GROUP BY market_mode
            """,
            (cutoff_date,),
        ).fetchall()

        # 위험도별 성과 (신규 컬럼이 있는 행만)
        risk_rows = conn.execute(
            """
            SELECT
                risk_level,
                COUNT(*)                                              AS cnt,
                ROUND(AVG(CASE WHEN status = 'closed' THEN return_pct END), 2) AS avg_return,
                SUM(CASE WHEN status = 'closed' AND return_pct > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END)   AS closed
            FROM paper_trades
            WHERE risk_level IS NOT NULL AND recommended_date >= ?
            GROUP BY risk_level
            """,
            (cutoff_date,),
        ).fetchall()

        # 신규 모델 데이터 건수
        new_model_count = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE recommended_date >= ?",
            (NEW_MODEL_START,),
        ).fetchone()[0]

    # 누적 수익률 계산 (배치별 avg_return 누적합)
    timeline = []
    cumulative = 0.0
    for r in rows:
        avg = r["avg_return_pct"] or 0.0
        cumulative = round(cumulative + avg, 2)
        closed = r["closed_count"] or 0
        wins = r["win_count"] or 0
        _versions = [v for v in (r["model_versions"] or "").split(",") if v]
        model_version = _versions[0] if len(_versions) == 1 else ("혼재" if len(_versions) > 1 else None)

        timeline.append({
            "date":                 r["recommended_date"],
            "recommended_count":    r["recommended_count"],
            "closed_count":         closed,
            "avg_return_pct":       r["avg_return_pct"],
            "win_rate":             round(wins / closed, 4) if closed else None,
            "cumulative_return_pct": cumulative,
            "model_version":        model_version,
        })

    def _group_stat(rows_: Any) -> Dict:
        out = {}
        for r in rows_:
            key = r[0] or "unknown"
            closed_ = r["closed"] or 0
            wins_ = r["wins"] or 0
            out[key] = {
                "count":      r["cnt"],
                "closed":     closed_,
                "avg_return": r["avg_return"],
                "win_rate":   round(wins_ / closed_, 4) if closed_ else None,
            }
        return out

    return {
        "cutoff_date":       cutoff_date,
        "new_model_start":   NEW_MODEL_START,
        "new_model_count":   new_model_count,
        "timeline":          timeline,
        "by_confidence":     _group_stat(conf_rows),
        "by_market_mode":    _group_stat(mode_rows),
        "by_risk":           _group_stat(risk_rows),
    }
