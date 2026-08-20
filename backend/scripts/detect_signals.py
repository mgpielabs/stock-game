"""
시그널 감지 스크립트 — daily_pipeline.py 마지막 단계에서 실행.
5가지 규칙 기반 이벤트를 감지하여 signal_log 테이블에 INSERT.

이벤트 타입:
  foreign_surge      : 특정 종목 5일 외국인 순매수가 20일 평균 대비 3배 이상
  score60d_entry     : 전일 상위20% 밖 → 오늘 상위10% 진입
  watchlist_price_jump: 관심종목 전일 대비 ±5% 이상 가격 변동
  sector_quadrant    : 섹터 사분면 전환 (일관유출→단기전환, 단기이탈→일관유입 등)
  position_event     : 60d 포지션 만기 D-5 알림, 수익률 ±10% 돌파
"""

import sys
import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from data.db import DB_PATH, get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [detect_signals] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _insert(conn: sqlite3.Connection, event_type: str, ticker, sector, message: str, data: dict):
    conn.execute(
        "INSERT INTO signal_log (event_type, ticker, sector, message, data) VALUES (?,?,?,?,?)",
        (event_type, ticker, sector, message, json.dumps(data, ensure_ascii=False) if data else None),
    )


def _already_logged_today(conn: sqlite3.Connection, event_type: str, ticker, message: str) -> bool:
    """같은 날 동일 (event_type, ticker, message) 중복 방지."""
    prefix = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT 1 FROM signal_log WHERE event_type=? AND ticker IS ? AND message=? AND created_at LIKE ?",
        (event_type, ticker, message, f"{prefix}%"),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
#  이벤트 1: 외국인 순매수 급변                                                 #
# --------------------------------------------------------------------------- #
def detect_foreign_surge(conn: sqlite3.Connection, today: str) -> int:
    """5일 외국인 순매수 합산 ≥ 20일 평균 3배 이상인 종목."""
    sql = """
    WITH recent AS (
        SELECT
            k.symbol,
            s.name,
            SUM(CASE WHEN k.date > date(?, '-6 days') THEN k.foreign_net_qty ELSE 0 END) AS sum_5d,
            AVG(k.foreign_net_qty) AS avg_20d
        FROM investor_trading_kis k
        JOIN stocks s ON s.symbol = k.symbol
        WHERE k.date > date(?, '-21 days')
          AND k.date <= ?
        GROUP BY k.symbol
        HAVING COUNT(k.date) >= 15
    )
    SELECT symbol, name, sum_5d, avg_20d
    FROM recent
    WHERE avg_20d > 0
      AND sum_5d >= avg_20d * 3
      AND sum_5d > 10000
    ORDER BY (sum_5d / avg_20d) DESC
    LIMIT 10
    """
    rows = conn.execute(sql, (today, today, today)).fetchall()
    count = 0
    for symbol, name, sum_5d, avg_20d in rows:
        ratio = round(sum_5d / avg_20d, 1) if avg_20d else 0
        msg = f"{name}({symbol}) 외국인 순매수 급변 (5일 합산이 20일 평균의 {ratio}배)"
        if not _already_logged_today(conn, "foreign_surge", symbol, msg):
            _insert(conn, "foreign_surge", symbol, None, msg, {
                "sum_5d": int(sum_5d), "avg_20d": round(float(avg_20d), 0), "ratio": ratio
            })
            count += 1
    return count


# --------------------------------------------------------------------------- #
#  이벤트 2: 60d 스코어 상위 진입                                               #
# --------------------------------------------------------------------------- #
def detect_score60d_entry(conn: sqlite3.Connection, today: str) -> int:
    """전일 스코어 상위20% 밖이었다가 오늘 상위10%로 진입한 종목."""
    # prediction_log에서 60d 예측 rank 사용 (오늘 vs 어제)
    sql_today = """
        SELECT symbol, rank,
               (SELECT COUNT(*) FROM prediction_log p2
                WHERE p2.model='60d' AND p2.predicted_at=p.predicted_at) AS total
        FROM prediction_log p
        WHERE model='60d' AND predicted_at=?
    """
    sql_prev = """
        SELECT symbol, rank,
               (SELECT COUNT(*) FROM prediction_log p2
                WHERE p2.model='60d' AND p2.predicted_at=p.predicted_at) AS total
        FROM prediction_log p
        WHERE model='60d' AND predicted_at=(
            SELECT predicted_at FROM prediction_log WHERE model='60d' AND predicted_at < ?
            ORDER BY predicted_at DESC LIMIT 1
        )
    """
    today_rows = {r[0]: (r[1], r[2]) for r in conn.execute(sql_today, (today,)).fetchall()}
    prev_rows  = {r[0]: (r[1], r[2]) for r in conn.execute(sql_prev,  (today,)).fetchall()}

    count = 0
    for symbol, (rank, total) in today_rows.items():
        if total <= 0:
            continue
        pct_today = rank / total
        if pct_today > 0.10:
            continue  # 오늘 상위10% 아님
        prev = prev_rows.get(symbol)
        if not prev:
            continue
        prev_rank, prev_total = prev
        if prev_total <= 0:
            continue
        pct_prev = prev_rank / prev_total
        if pct_prev <= 0.20:
            continue  # 전일에도 이미 상위20% 안에 있었음
        # 전일 상위20% 밖 → 오늘 상위10% 진입
        row = conn.execute("SELECT name FROM stocks WHERE symbol=?", (symbol,)).fetchone()
        name = row[0] if row else symbol
        msg = f"{name}({symbol}) 60d 스코어 상위10% 진입 (전일 순위 {prev_rank}/{prev_total})"
        if not _already_logged_today(conn, "score60d_entry", symbol, msg):
            _insert(conn, "score60d_entry", symbol, None, msg, {
                "rank_today": rank, "total_today": total, "pct_today": round(pct_today, 3),
                "rank_prev": prev_rank, "pct_prev": round(pct_prev, 3),
            })
            count += 1
    return count


# --------------------------------------------------------------------------- #
#  이벤트 3: 관심종목 가격 급변                                                 #
# --------------------------------------------------------------------------- #
def detect_watchlist_price_jump(conn: sqlite3.Connection, today: str) -> int:
    """관심종목 중 전일 대비 ±5% 이상 변동."""
    watchlist_rows = conn.execute("SELECT symbol, name FROM watchlist").fetchall()
    if not watchlist_rows:
        return 0

    count = 0
    for symbol, wl_name in watchlist_rows:
        rows = conn.execute(
            "SELECT date, close FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 2",
            (symbol,)
        ).fetchall()
        if len(rows) < 2:
            continue
        close_today, close_prev = rows[0][1], rows[1][1]
        if not close_prev or close_prev == 0:
            continue
        chg_pct = (close_today - close_prev) / close_prev * 100
        if abs(chg_pct) < 5.0:
            continue
        name = wl_name or symbol
        direction = "상승" if chg_pct > 0 else "하락"
        msg = f"[관심종목] {name}({symbol}) 전일 대비 {chg_pct:+.1f}% {direction}"
        if not _already_logged_today(conn, "watchlist_price_jump", symbol, msg):
            _insert(conn, "watchlist_price_jump", symbol, None, msg, {
                "date": rows[0][0], "close": close_today, "prev_close": close_prev,
                "chg_pct": round(chg_pct, 2),
            })
            count += 1
    return count


# --------------------------------------------------------------------------- #
#  이벤트 4: 섹터 사분면 전환                                                   #
# --------------------------------------------------------------------------- #
def detect_sector_quadrant(conn: sqlite3.Connection, today: str) -> int:
    """섹터 사분면 전환 감지 (일관유출→단기전환, 단기이탈→일관유입 등).

    sector_flows_cache가 없으므로 investor_trading_kis_detail 테이블에서
    섹터별 외국인+기관계 순매수를 집계하여 현재 사분면 판단 후 signal_log의
    최근 기록과 비교.
    """
    # 섹터별 5일/20일 흐름 집계
    sql = """
    WITH sector_flow AS (
        SELECT
            s.sector,
            SUM(CASE WHEN k.date > date(?, '-6 days') THEN k.foreign_net_qty + k.inst_net_qty ELSE 0 END) AS flow_5d,
            AVG(k.foreign_net_qty + k.inst_net_qty) AS avg_20d
        FROM investor_trading_kis k
        JOIN stocks s ON s.symbol = k.symbol
        WHERE k.date > date(?, '-21 days') AND k.date <= ?
          AND s.sector IS NOT NULL
        GROUP BY s.sector
        HAVING COUNT(DISTINCT k.date) >= 10
    )
    SELECT sector, flow_5d, avg_20d,
           CASE
             WHEN flow_5d > 0 AND avg_20d > 0 THEN '일관유입'
             WHEN flow_5d > 0 AND avg_20d <= 0 THEN '단기전환'
             WHEN flow_5d <= 0 AND avg_20d > 0 THEN '단기이탈'
             ELSE '일관유출'
           END AS quadrant
    FROM sector_flow
    WHERE sector != ''
    """
    rows = conn.execute(sql, (today, today, today)).fetchall()

    # 이전에 기록된 사분면 가져오기 (최근 2일치)
    prev_sql = """
        SELECT ticker, data FROM signal_log
        WHERE event_type='sector_quadrant'
          AND created_at >= date('now', '-2 days')
        ORDER BY created_at DESC
    """
    prev_records = {}
    for prow in conn.execute(prev_sql).fetchall():
        if prow[0] and prow[0] not in prev_records:
            try:
                d = json.loads(prow[1] or "{}")
                prev_records[prow[0]] = d.get("quadrant")
            except Exception:
                pass

    # 전환 감지
    TRANSITIONS = {
        ("일관유출", "단기전환"): "📈 일관유출 → 단기전환",
        ("단기이탈", "일관유입"): "📈 단기이탈 → 일관유입",
        ("일관유입", "단기이탈"): "📉 일관유입 → 단기이탈",
        ("단기전환", "일관유출"): "📉 단기전환 → 일관유출",
    }
    count = 0
    for sector, flow_5d, avg_20d, quadrant in rows:
        prev_q = prev_records.get(sector)
        if not prev_q or prev_q == quadrant:
            # 기록 없으면 오늘 기준 저장 (다음날 비교용)
            if not prev_q:
                _insert(conn, "sector_quadrant", None, sector,
                        f"섹터 '{sector}' 현재 사분면: {quadrant} (기준 기록)",
                        {"quadrant": quadrant, "flow_5d": float(flow_5d), "avg_20d": float(avg_20d)})
            continue
        label = TRANSITIONS.get((prev_q, quadrant))
        if not label:
            continue
        msg = f"섹터 '{sector}' {label} 전환"
        if not _already_logged_today(conn, "sector_quadrant", sector, msg):
            _insert(conn, "sector_quadrant", None, sector, msg, {
                "prev_quadrant": prev_q, "quadrant": quadrant,
                "flow_5d": float(flow_5d), "avg_20d": float(avg_20d),
            })
            count += 1
    return count


# --------------------------------------------------------------------------- #
#  이벤트 5: 60d 포지션 이벤트                                                 #
# --------------------------------------------------------------------------- #
def detect_position_events(conn: sqlite3.Connection, today: str) -> int:
    """60d 포지션: D-5 만기 알림 + 수익률 ±10% 돌파."""
    # paper_trades에서 model='60d' open 거래 조회 (model_version으로 60d 구분)
    open_trades = conn.execute(
        """
        SELECT pt.id, pt.symbol, s.name, pt.recommended_date, pt.recommended_price,
               pt.model_version
        FROM paper_trades pt
        LEFT JOIN stocks s ON s.symbol = pt.symbol
        WHERE pt.status='open' AND pt.model_version LIKE '%60d%'
        """
    ).fetchall()

    count = 0
    for trade_id, symbol, name, rec_date, rec_price, model_ver in open_trades:
        # 보유 거래일 수 계산
        holding_sql = """
            SELECT COUNT(*) FROM market_index
            WHERE date > ? AND date <= ? AND code='1001'
        """
        holding_days = conn.execute(holding_sql, (rec_date, today)).fetchone()[0]
        days_left = 60 - holding_days

        # 현재 종가
        price_row = conn.execute(
            "SELECT close FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1",
            (symbol,)
        ).fetchone()
        if not price_row or not rec_price:
            continue
        cur_close = price_row[0]
        ret_pct = (cur_close - rec_price) / rec_price * 100

        disp_name = name or symbol

        # D-5 만기 알림
        if 0 <= days_left <= 5:
            msg = f"[60d 포지션] {disp_name}({symbol}) 만기 D-{days_left} (수익률 {ret_pct:+.1f}%)"
            if not _already_logged_today(conn, "position_event", symbol, msg):
                _insert(conn, "position_event", symbol, None, msg, {
                    "trade_id": trade_id, "days_left": days_left,
                    "ret_pct": round(ret_pct, 2), "horizon": "60d",
                })
                count += 1

        # ±10% 수익률 돌파 (최근 기록 중복 체크: 7일 이내 동일 방향 신호 없으면 기록)
        if abs(ret_pct) >= 10.0:
            direction = "수익" if ret_pct >= 0 else "손실"
            milestone = 10 * (int(abs(ret_pct)) // 10)
            msg = f"[60d 포지션] {disp_name}({symbol}) {direction} {milestone}% 돌파 ({ret_pct:+.1f}%)"
            # 7일 이내 같은 milestone 기록 여부 확인
            dup = conn.execute(
                """SELECT 1 FROM signal_log
                   WHERE event_type='position_event' AND ticker=? AND message=?
                     AND created_at >= date('now', '-7 days')""",
                (symbol, msg)
            ).fetchone()
            if not dup:
                _insert(conn, "position_event", symbol, None, msg, {
                    "trade_id": trade_id, "ret_pct": round(ret_pct, 2),
                    "milestone": milestone, "horizon": "60d",
                })
                count += 1
    return count


# --------------------------------------------------------------------------- #
#  메인                                                                        #
# --------------------------------------------------------------------------- #
def main():
    today = _today()
    log.info("시그널 감지 시작: %s", today)

    conn = get_connection()
    # signal_log 테이블 없으면 생성 (init_db가 이미 했을 수 있음, 방어적)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            ticker      TEXT,
            sector      TEXT,
            message     TEXT NOT NULL,
            data        TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()

    total = 0
    try:
        n = detect_foreign_surge(conn, today)
        log.info("외국인 순매수 급변: %d건", n); total += n

        n = detect_score60d_entry(conn, today)
        log.info("60d 스코어 상위 진입: %d건", n); total += n

        n = detect_watchlist_price_jump(conn, today)
        log.info("관심종목 가격 급변: %d건", n); total += n

        n = detect_sector_quadrant(conn, today)
        log.info("섹터 사분면 전환: %d건", n); total += n

        n = detect_position_events(conn, today)
        log.info("60d 포지션 이벤트: %d건", n); total += n

        conn.commit()
        log.info("시그널 감지 완료: 총 %d건 기록", total)
    except Exception as exc:
        log.error("시그널 감지 오류: %s", exc, exc_info=True)
        conn.rollback()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
