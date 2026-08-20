"""
예측 이력 기록 + 만기 실현 결과 자동 계산

prediction_log  : 매일 5d/60d top-10 예측을 append-only로 기록
prediction_outcomes : 만기 도래 시 entry/exit 가격, 수익률, 시장수익률 기록

호출 위치:
  daily_pipeline.py 10단계 — 서버 재시작(3단계) 이후 독립적으로 실행
"""
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 백엔드 루트를 경로에 추가
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "ml"))
sys.path.insert(0, str(_ROOT / "data"))
sys.path.insert(0, str(_ROOT / "server"))

DB_PATH    = _ROOT / "data" / "stocks.db"
MODELS_DIR = _ROOT / "models"

logger = logging.getLogger(__name__)


# ── 스키마 보장 ─────────────────────────────────────────────────

def _ensure_schema() -> None:
    """prediction_log / prediction_outcomes 테이블 생성 (없으면)."""
    ddl = """
    CREATE TABLE IF NOT EXISTS prediction_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        predicted_at    TEXT NOT NULL,
        model           TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        rank            INTEGER NOT NULL,
        score           REAL,
        horizon         INTEGER NOT NULL,
        created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS prediction_outcomes (
        prediction_id     INTEGER NOT NULL REFERENCES prediction_log(id),
        entry_price       INTEGER,
        exit_price        INTEGER,
        return_pct        REAL,
        market_return_pct REAL,
        is_hit            INTEGER,
        settled_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (prediction_id)
    );
    CREATE INDEX IF NOT EXISTS idx_pred_log_date  ON prediction_log (predicted_at);
    CREATE INDEX IF NOT EXISTS idx_pred_log_model ON prediction_log (model, predicted_at);
    """
    # G2 게이트 차단일에도 "진입했을 top-10"을 별도 기록 — on vs off 라이브 성적 비교용
    shadow_ddl = """
    CREATE TABLE IF NOT EXISTS shadow_prediction_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        predicted_at    TEXT NOT NULL,
        model           TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        rank            INTEGER NOT NULL,
        score           REAL,
        horizon         INTEGER NOT NULL,
        gate            TEXT NOT NULL DEFAULT 'G2',
        created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS shadow_prediction_outcomes (
        shadow_id         INTEGER NOT NULL REFERENCES shadow_prediction_log(id),
        entry_price       INTEGER,
        exit_price        INTEGER,
        return_pct        REAL,
        market_return_pct REAL,
        is_hit            INTEGER,
        settled_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (shadow_id)
    );
    CREATE INDEX IF NOT EXISTS idx_shadow_log_date  ON shadow_prediction_log (predicted_at);
    CREATE INDEX IF NOT EXISTS idx_shadow_log_model ON shadow_prediction_log (model, predicted_at);
    """
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        conn.executescript(ddl)
        conn.executescript(shadow_ddl)


# ── 예측 이력 기록 ────────────────────────────────────────────

def log_predictions(
    predicted_at: str,
    model: str,
    predictions: List[Dict[str, Any]],
    horizon: int,
) -> int:
    """당일 예측 결과를 prediction_log에 append.

    이미 같은 (predicted_at, model)로 기록된 데이터가 있으면 스킵 (멱등).
    Returns: 기록된 행 수 (스킵 시 0)
    """
    _ensure_schema()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        # 이미 기록됐으면 스킵
        already = conn.execute(
            "SELECT COUNT(*) FROM prediction_log WHERE predicted_at=? AND model=?",
            (predicted_at, model),
        ).fetchone()[0]
        if already > 0:
            logger.info("[prediction_logger] 이미 기록됨: %s %s (%d행)", predicted_at, model, already)
            return 0

        records = [
            (predicted_at, model, str(p.get("symbol", "")).zfill(6),
             int(p.get("rank", i + 1)), float(p.get("probability", 0.0)), horizon)
            for i, p in enumerate(predictions)
        ]
        conn.executemany(
            "INSERT INTO prediction_log (predicted_at, model, symbol, rank, score, horizon) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            records,
        )
        logger.info("[prediction_logger] 기록: %s %s %d행", predicted_at, model, len(records))
        return len(records)


# ── 만기 실현 결과 계산 ───────────────────────────────────────

def _get_nth_trading_close(symbol: str, from_date: str, n: int) -> Optional[int]:
    """from_date 이후 n번째 거래일의 종가. 없으면 None."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        row = conn.execute(
            """
            SELECT p.close
            FROM prices p
            JOIN (
                SELECT date FROM market_index WHERE code='1001' AND date > ?
                ORDER BY date LIMIT ?
            ) m ON p.date = m.date
            WHERE p.symbol = ?
            ORDER BY p.date LIMIT 1 OFFSET ?
            """,
            (from_date, n, symbol, n - 1),
        ).fetchone()
    return int(row[0]) if row else None


def _get_next_day_open(symbol: str, from_date: str) -> Optional[int]:
    """from_date 다음 거래일 시가. 없으면 None."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        row = conn.execute(
            "SELECT p.open FROM prices p "
            "JOIN market_index m ON m.date=p.date AND m.code='1001' "
            "WHERE p.symbol=? AND p.date > ? ORDER BY p.date LIMIT 1",
            (symbol, from_date),
        ).fetchone()
    return int(row[0]) if row and row[0] else None


def _get_market_return(from_date: str, n_days: int) -> Optional[float]:
    """KOSPI from_date 이후 n_days 거래일 수익률. 없으면 None."""
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        rows = conn.execute(
            "SELECT close FROM market_index WHERE code='1001' AND date > ? "
            "ORDER BY date LIMIT ?",
            (from_date, n_days),
        ).fetchall()
    if len(rows) < n_days:
        return None
    entry = rows[0][0]
    exit_ = rows[-1][0]
    if not entry or entry == 0:
        return None
    return (exit_ - entry) / entry * 100.0


def settle_outcomes(today: str) -> Dict[str, int]:
    """만기 도래한 prediction_log 행에 대해 실현 결과를 계산해 prediction_outcomes에 저장.

    만기 기준: predicted_at + horizon 거래일 이내 날짜가 today 이전이면 만기 처리.
    단순화: horizon 거래일 이후 시점에 prices 데이터가 있으면 처리.
    Returns: {"settled": int, "skipped": int}
    """
    _ensure_schema()
    settled = 0
    skipped = 0

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        # 아직 outcome이 없는 prediction_log 행
        pending = conn.execute(
            """
            SELECT pl.id, pl.predicted_at, pl.model, pl.symbol, pl.rank, pl.score, pl.horizon
            FROM prediction_log pl
            LEFT JOIN prediction_outcomes po ON po.prediction_id = pl.id
            WHERE po.prediction_id IS NULL
            ORDER BY pl.predicted_at
            """,
        ).fetchall()

    for pid, predicted_at, model, symbol, rank, score, horizon in pending:
        # 진입가: 추천 익일 시가
        entry = _get_next_day_open(symbol, predicted_at)
        if entry is None:
            # 추천일 종가로 대체
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                row = conn.execute(
                    "SELECT close FROM prices WHERE symbol=? AND date=?",
                    (symbol, predicted_at),
                ).fetchone()
            entry = int(row[0]) if row and row[0] else None

        if entry is None:
            skipped += 1
            continue

        # 청산가: horizon 거래일 후 종가
        exit_price = _get_nth_trading_close(symbol, predicted_at, horizon)
        if exit_price is None:
            skipped += 1
            continue  # 아직 만기 미도래

        return_pct = (exit_price - entry) / entry * 100.0
        market_ret = _get_market_return(predicted_at, horizon)

        # 성공 기준: 5d=종가+5%, 60d=초과수익>0
        if horizon == 5:
            is_hit = 1 if return_pct >= 5.0 else 0
        else:
            is_hit = 1 if (market_ret is not None and return_pct > market_ret) else 0

        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO prediction_outcomes
                    (prediction_id, entry_price, exit_price, return_pct, market_return_pct, is_hit)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pid, entry, exit_price, round(return_pct, 4),
                 round(market_ret, 4) if market_ret is not None else None,
                 is_hit),
            )
        settled += 1

    logger.info("[prediction_logger] settle: settled=%d skipped=%d", settled, skipped)
    return {"settled": settled, "skipped": skipped}


# ── Shadow(게이트 차단일 가상 진입) 기록 ─────────────────────

def log_shadow_predictions(
    predicted_at: str,
    model: str,
    predictions: List[Dict[str, Any]],
    horizon: int,
    gate: str = "G2",
) -> int:
    """G2 게이트 차단일에 '진입했을 top-N'을 shadow_prediction_log에 기록.

    이미 같은 (predicted_at, model, gate)로 기록된 데이터가 있으면 스킵 (멱등).
    Returns: 기록된 행 수 (스킵 시 0)
    """
    _ensure_schema()
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        already = conn.execute(
            "SELECT COUNT(*) FROM shadow_prediction_log "
            "WHERE predicted_at=? AND model=? AND gate=?",
            (predicted_at, model, gate),
        ).fetchone()[0]
        if already > 0:
            logger.info("[prediction_logger] shadow 이미 기록됨: %s %s gate=%s", predicted_at, model, gate)
            return 0

        records = [
            (predicted_at, model, str(p.get("symbol", "")).zfill(6),
             int(p.get("rank", i + 1)), float(p.get("probability", 0.0)), horizon, gate)
            for i, p in enumerate(predictions)
        ]
        conn.executemany(
            "INSERT INTO shadow_prediction_log "
            "(predicted_at, model, symbol, rank, score, horizon, gate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            records,
        )
        logger.info("[prediction_logger] shadow 기록: %s %s gate=%s %d행", predicted_at, model, gate, len(records))
        return len(records)


def settle_shadow_outcomes(today: str) -> Dict[str, int]:
    """만기 도래한 shadow_prediction_log 행에 대해 실현 결과를 계산해 shadow_prediction_outcomes에 저장.

    settle_outcomes와 동일한 로직 — shadow_prediction_log/outcomes 테이블만 다름.
    """
    _ensure_schema()
    settled = 0
    skipped = 0

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        pending = conn.execute(
            """
            SELECT sl.id, sl.predicted_at, sl.model, sl.symbol, sl.rank, sl.score, sl.horizon
            FROM shadow_prediction_log sl
            LEFT JOIN shadow_prediction_outcomes so ON so.shadow_id = sl.id
            WHERE so.shadow_id IS NULL
            ORDER BY sl.predicted_at
            """,
        ).fetchall()

    for sid, predicted_at, model, symbol, rank, score, horizon in pending:
        entry = _get_next_day_open(symbol, predicted_at)
        if entry is None:
            with sqlite3.connect(DB_PATH, timeout=30) as conn:
                row = conn.execute(
                    "SELECT close FROM prices WHERE symbol=? AND date=?",
                    (symbol, predicted_at),
                ).fetchone()
            entry = int(row[0]) if row and row[0] else None

        if entry is None:
            skipped += 1
            continue

        exit_price = _get_nth_trading_close(symbol, predicted_at, horizon)
        if exit_price is None:
            skipped += 1
            continue

        return_pct = (exit_price - entry) / entry * 100.0
        market_ret = _get_market_return(predicted_at, horizon)

        if horizon == 5:
            is_hit = 1 if return_pct >= 5.0 else 0
        else:
            is_hit = 1 if (market_ret is not None and return_pct > market_ret) else 0

        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO shadow_prediction_outcomes
                    (shadow_id, entry_price, exit_price, return_pct, market_return_pct, is_hit)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sid, entry, exit_price, round(return_pct, 4),
                 round(market_ret, 4) if market_ret is not None else None,
                 is_hit),
            )
        settled += 1

    logger.info("[prediction_logger] shadow settle: settled=%d skipped=%d", settled, skipped)
    return {"settled": settled, "skipped": skipped}


def get_shadow_vs_live_performance() -> Dict[str, Any]:
    """v1.1 게이트 반사실 비교.

    5d: live(게이트ON 실제진입) vs shadow(게이트OFF 가상기록 — 차단구간에 기록됨)
    60d: live(게이트OFF 실제진입, v1.1) vs shadow_60d(게이트ON 가상기록 — 차단+60d 리밸런싱 날)

    각 쌍의 n<10이면 collecting 표시.
    """
    _ensure_schema()
    result: Dict[str, Any] = {}

    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        def _query_live(model: str):
            return conn.execute(
                """
                SELECT po.return_pct, po.is_hit
                FROM prediction_outcomes po
                JOIN prediction_log pl ON pl.id = po.prediction_id
                WHERE pl.model=? AND pl.rank <= 10 AND po.return_pct IS NOT NULL
                """,
                (model,),
            ).fetchall()

        def _query_shadow(model: str):
            return conn.execute(
                """
                SELECT so.return_pct, so.is_hit
                FROM shadow_prediction_outcomes so
                JOIN shadow_prediction_log sl ON sl.id = so.shadow_id
                WHERE sl.model=? AND sl.rank <= 10 AND so.return_pct IS NOT NULL
                """,
                (model,),
            ).fetchall()

        live_5d    = _query_live("5d")
        shadow_5d  = _query_shadow("5d")    # 게이트無 가상기록
        live_60d   = _query_live("60d")
        shadow_60d = _query_shadow("60d")   # 게이트有 가상기록

    MIN_N = 10

    def _summarize(rows, label):
        n = len(rows)
        if n < MIN_N:
            return {"status": "collecting", "n": n, "label": label}
        avg_ret = sum(r[0] for r in rows) / n
        hit_rate = sum(1 for r in rows if r[1] == 1) / n * 100.0
        return {
            "status": "ready",
            "n": n,
            "avg_return_pct": round(avg_ret, 4),
            "hit_rate_pct": round(hit_rate, 2),
            "label": label,
        }

    result["live"]               = _summarize(live_5d,    "5d 게이트ON(실제진입)")
    result["shadow_blocked"]     = _summarize(shadow_5d,  "5d 게이트OFF(차단구간 가상기록)")
    result["live_60d"]           = _summarize(live_60d,   "60d 게이트OFF(v1.1 실제진입)")
    result["shadow_blocked_60d"] = _summarize(shadow_60d, "60d 게이트ON(차단구간 가상기록)")

    return result


# ── 라이브 성과 집계 ─────────────────────────────────────────

def get_live_performance() -> Dict[str, Any]:
    """prediction_outcomes 기반 라이브 성과 집계.

    5d: top10 기준 P@10 (종가+5% 달성률)
    60d: top10 기준 평균 초과수익률 (시장 대비)
    표본 n<30이면 "collecting" 표시.
    data_as_of: prices 테이블 최신일 (성과 숫자와 함께 항상 노출)
    """
    _ensure_schema()
    MIN_SAMPLE = 30

    result: Dict[str, Any] = {}
    with sqlite3.connect(DB_PATH, timeout=30) as conn:
        prices_date = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
        result["data_as_of"] = prices_date
        for model, horizon in [("5d", 5), ("60d", 60)]:
            rows = conn.execute(
                """
                SELECT po.return_pct, po.market_return_pct, po.is_hit, pl.rank
                FROM prediction_outcomes po
                JOIN prediction_log pl ON pl.id = po.prediction_id
                WHERE pl.model=? AND pl.horizon=? AND pl.rank <= 10
                  AND po.return_pct IS NOT NULL
                """,
                (model, horizon),
            ).fetchall()

            n = len(rows)
            if n < MIN_SAMPLE:
                result[model] = {"status": "collecting", "n": n, "min_sample": MIN_SAMPLE}
                continue

            if horizon == 5:
                hit_rate = sum(1 for _, __, is_hit, ___ in rows if is_hit == 1) / n * 100.0
                result[model] = {
                    "status": "ready",
                    "n": n,
                    "metric": "P@10",
                    "value": round(hit_rate, 2),
                    "unit": "%",
                    "description": f"top10 종가+5% 달성률 {hit_rate:.1f}%",
                }
            else:
                excess_list = [
                    ret - mkt
                    for ret, mkt, _, __ in rows
                    if mkt is not None
                ]
                if not excess_list:
                    result[model] = {"status": "collecting", "n": n, "min_sample": MIN_SAMPLE}
                    continue
                avg_excess = sum(excess_list) / len(excess_list)
                result[model] = {
                    "status": "ready",
                    "n": len(excess_list),
                    "metric": "avg_excess_return",
                    "value": round(avg_excess, 4),
                    "unit": "%",
                    "description": f"top10 평균 초과수익 {avg_excess:+.2f}%",
                }
    return result


# ── CLI 실행 ─────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        stream=sys.stdout)

    parser = argparse.ArgumentParser(description="예측 이력 기록 + 만기 실측")
    parser.add_argument("--settle-only", action="store_true", help="만기 실측만 실행")
    parser.add_argument("--status",      action="store_true", help="라이브 성과 출력")
    args = parser.parse_args()

    _ensure_schema()

    if args.status:
        perf = get_live_performance()
        print(json.dumps(perf, ensure_ascii=False, indent=2))
        sys.exit(0)

    today = datetime.now().strftime("%Y%m%d")

    # 비거래일(주말·공휴일)이면 예측 기록 스킵 — 거래일에만 신규 예측 기록
    if not args.settle_only:
        with sqlite3.connect(DB_PATH) as _c:
            _is_td = _c.execute(
                "SELECT COUNT(*) FROM market_index WHERE code='1001' AND date=?", (today,)
            ).fetchone()[0]
        if not _is_td:
            logger.info("비거래일(%s) — 예측 기록 스킵, 실측 청산만 진행", today)
            args.settle_only = True

    if not args.settle_only:
        # 예측 기록: predictor.py를 직접 import해 예측 생성
        try:
            from predictor import predict_today, load_model, get_latest_feature_date, load_calibrator  # noqa
            from concentration_filter import RecommendationLog, filter_cooldown  # noqa
            from pathlib import Path as _Path
            from dataset import FEATURE_COLS_REDUCED  # noqa
            import json as _json

            booster, model_dir = load_model("target_5d")
            calibrator = load_calibrator(model_dir)
            latest_date = get_latest_feature_date()

            # raw top50 획득 (필터 후 충분한 후보 확보)
            preds_5d_raw = predict_today(booster, latest_date, top_n=50, calibrator=calibrator)

            # raw 예측을 shadow에 기록 (gate="raw_cooldown") — 비교용
            log_shadow_predictions(today, "5d", preds_5d_raw[:20], horizon=5, gate="raw_cooldown")

            # concentration_filter 적용 (main.py get_today_predictions와 동일)
            _rec_log = RecommendationLog()
            preds_5d_filtered, _excluded = filter_cooldown(
                preds_5d_raw, _rec_log, cooldown_days=5, today=today
            )
            if _excluded:
                logger.info("쿨다운 제외 %d종목: %s", len(_excluded), [p["symbol"] for p in _excluded])

            preds_5d = preds_5d_filtered[:10]
            n5 = log_predictions(today, "5d", preds_5d, horizon=5)
            logger.info("5d 예측 기록: %d건 (raw=%d, 필터후=%d)", n5, len(preds_5d_raw), len(preds_5d_filtered))

            try:
                from predictor import predict_60d  # noqa
                preds_60d = predict_60d(latest_date, top_n=10)
                n60 = log_predictions(today, "60d", preds_60d, horizon=60)
                logger.info("60d 예측 기록: %d건", n60)
            except Exception as e:
                logger.warning("60d 예측 기록 실패 (비치명적): %s", e)

        except Exception as exc:
            logger.error("예측 기록 실패: %s", exc)
            sys.exit(1)

    result = settle_outcomes(today)
    print(f"실측 완료: settled={result['settled']} skipped={result['skipped']}")

    shadow_result = settle_shadow_outcomes(today)
    print(f"shadow 실측 완료: settled={shadow_result['settled']} skipped={shadow_result['skipped']}")
