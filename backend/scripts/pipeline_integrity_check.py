"""
파이프라인 무결성 체크 (daily_pipeline.py 11단계)

다음 3가지를 검사하고, 위반 시 exit code 1로 종료:
  1. prices 최신 날짜 = market_index 최신 날짜 (수집 공백 감지)
  2. 당일 신규 행수가 과거 20일 평균의 ±50% 범위 내 (이상 소실/폭발 감지)
  3. 핵심 피처(atr_pct, vol_ratio_20d, ma120_dev) 결측률 전일 대비 +10%p 급증 없음

항상 exit 0을 목표로 하되, 조용한 성공을 막는 게 목적.
위반 = 로그에 ERROR 명시 + sys.exit(1).
"""
import logging
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "stocks.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# 위반 판정 기준
PRICE_ROW_BAND = 0.50       # 당일 행수 = 과거 20일 평균 ±50%
FEATURE_NAN_JUMP = 0.10     # 핵심 피처 결측률 전일 대비 +10%p
CORE_FEATURES = ["atr_pct", "vol_ratio_20d", "ma120_dev"]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def check_prices_freshness() -> bool:
    """prices 최신일 == market_index 최신 거래일인지 확인."""
    with _conn() as conn:
        prices_max = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
        idx_max    = conn.execute(
            "SELECT MAX(date) FROM market_index WHERE code='1001'"
        ).fetchone()[0]

    if not prices_max or not idx_max:
        log.warning("[무결성] prices 또는 market_index 데이터 없음 — 스킵")
        return True  # 초기 상태라 실패 처리 안 함

    # 날짜 차이 (캘린더 기준 — YYYYMMDD 정수 차이의 근사)
    # 실제 거래일 캘린더 조회 대신 7일(1주) 이하면 정상으로 처리
    diff = int(idx_max) - int(prices_max)
    if diff > 7:
        log.error(
            "[무결성] prices 최신일(%s) < market_index 최신일(%s) — "
            "prices 수집이 %s 이후 멈췄을 가능성",
            prices_max, idx_max, prices_max,
        )
        return False

    log.info("[무결성] prices 최신일=%s market_index 최신일=%s OK", prices_max, idx_max)
    return True


def check_daily_row_count() -> bool:
    """오늘 신규 prices 행수가 과거 20일 평균의 ±50% 범위 내인지 확인."""
    with _conn() as conn:
        # 최신 날짜 기준 최근 20거래일 날짜별 행수
        rows = conn.execute(
            """
            SELECT date, COUNT(*) as cnt
            FROM prices
            GROUP BY date
            ORDER BY date DESC
            LIMIT 21
            """
        ).fetchall()

    if len(rows) < 2:
        log.warning("[무결성] prices 행수 이력 부족 — 스킵")
        return True

    today_date, today_cnt = rows[0]
    past_cnts = [r[1] for r in rows[1:]]
    avg_past = sum(past_cnts) / len(past_cnts)

    lo = avg_past * (1 - PRICE_ROW_BAND)
    hi = avg_past * (1 + PRICE_ROW_BAND)

    if not (lo <= today_cnt <= hi):
        log.error(
            "[무결성] %s prices 행수=%d, 과거20일평균=%.0f (허용범위 %.0f~%.0f) — "
            "수집 이상 또는 상장폐지 급증 의심",
            today_date, today_cnt, avg_past, lo, hi,
        )
        return False

    log.info(
        "[무결성] %s prices 행수=%d (평균=%.0f, ±50%% 범위 내) OK",
        today_date, today_cnt, avg_past,
    )
    return True


def check_feature_nan_spike() -> bool:
    """핵심 피처의 결측률이 전일 대비 +10%p 이상 급증하지 않았는지 확인."""
    with _conn() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "features" not in tables:
            log.warning("[무결성] features 테이블 없음 — 스킵")
            return True

        # 최근 2 거래일의 날짜 가져오기
        recent_dates = conn.execute(
            "SELECT DISTINCT date FROM features ORDER BY date DESC LIMIT 2"
        ).fetchall()

    if len(recent_dates) < 2:
        log.warning("[무결성] features 날짜 이력 부족 — 스킵")
        return True

    today_date = recent_dates[0][0]
    prev_date  = recent_dates[1][0]

    violations = []
    with _conn() as conn:
        for feat in CORE_FEATURES:
            # 컬럼 존재 여부 먼저 확인
            cols = {r[1] for r in conn.execute("PRAGMA table_info(features)").fetchall()}
            if feat not in cols:
                continue

            for label, date_val in [("today", today_date), ("prev", prev_date)]:
                row = conn.execute(
                    f"SELECT COUNT(*), SUM({feat} IS NULL) FROM features WHERE date=?",
                    (date_val,),
                ).fetchone()
                total, null_cnt = row[0], (row[1] or 0)
                if total == 0:
                    continue
                nan_rate = null_cnt / total
                if label == "today":
                    today_rate = nan_rate
                else:
                    prev_rate = nan_rate

            jump = today_rate - prev_rate
            if jump > FEATURE_NAN_JUMP:
                violations.append(
                    f"{feat}: {prev_rate:.1%}→{today_rate:.1%} (+{jump:.1%}p)"
                )

    if violations:
        log.error(
            "[무결성] 핵심 피처 결측률 급증 감지 (%s → %s):\n  %s",
            prev_date, today_date, "\n  ".join(violations),
        )
        return False

    log.info(
        "[무결성] 핵심 피처(%s) 결측률 정상 (%s→%s) OK",
        ", ".join(CORE_FEATURES), prev_date, today_date,
    )
    return True


def main() -> None:
    if not DB_PATH.exists():
        log.error("[무결성] stocks.db 없음: %s", DB_PATH)
        sys.exit(1)

    log.info("=" * 50)
    log.info("파이프라인 무결성 체크 시작")
    log.info("=" * 50)

    results = [
        check_prices_freshness(),
        check_daily_row_count(),
        check_feature_nan_spike(),
    ]

    passed = sum(results)
    total  = len(results)
    log.info("=" * 50)
    log.info("무결성 체크 결과: %d/%d 통과", passed, total)
    log.info("=" * 50)

    if not all(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
