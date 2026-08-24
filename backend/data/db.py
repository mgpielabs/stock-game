"""
SQLite 스키마 정의 및 DB 헬퍼
테이블: stocks / prices / fundamentals / flows
"""

import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, List, Dict

DB_PATH = Path(__file__).parent / "stocks.db"

logger = logging.getLogger(__name__)

DDL = """
-- 종목 마스터
CREATE TABLE IF NOT EXISTS stocks (
    symbol      TEXT PRIMARY KEY,   -- '005930' (6자리)
    name        TEXT NOT NULL,
    market      TEXT NOT NULL,      -- 'KOSPI' | 'KOSDAQ'
    sector      TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 일별 OHLCV + 시가총액
CREATE TABLE IF NOT EXISTS prices (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,      -- 'YYYYMMDD'
    open        INTEGER,
    high        INTEGER,
    low         INTEGER,
    close       INTEGER,
    volume      INTEGER,
    market_cap  INTEGER,            -- 억원
    PRIMARY KEY (symbol, date),
    FOREIGN KEY (symbol) REFERENCES stocks(symbol)
);

-- 일별 PER / PBR / DIV (펀더멘털)
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    per         REAL,
    pbr         REAL,
    div_yield   REAL,
    PRIMARY KEY (symbol, date),
    FOREIGN KEY (symbol) REFERENCES stocks(symbol)
);

-- 일별 수급 데이터
-- foreign_net: 외국인 보유비율(%) — 일별 변화값으로 순매수 방향 추정
-- inst_net: 기관 순매수 (현재 미수집, 향후 확장용)
-- foreign_rate_source: 'actual'(실측) | 'estimated'(역산 추정) | NULL(미수집)
CREATE TABLE IF NOT EXISTS flows (
    symbol              TEXT NOT NULL,
    date                TEXT NOT NULL,
    foreign_net         REAL,           -- 외국인 보유비율 (%)
    inst_net            REAL,           -- 기관 순매수 (향후 확장)
    foreign_rate_source TEXT,           -- 'actual' | 'estimated' | NULL
    PRIMARY KEY (symbol, date),
    FOREIGN KEY (symbol) REFERENCES stocks(symbol)
);

-- 종목별 일별 투자자매매동향 (한국투자증권 KIS Open API, inquire-investor)
-- 개인/외국인/기관계 3종만 분리됨.
-- [정정, 2026-06-27] 아래 investor_trading_kis_detail(investor-trade-by-stock-daily 기반)에서
-- 종목단위 연기금 분리가 실제로 가능함을 확인 — 이 테이블/경로는 동결(active 유지, 신규
-- 마이그레이션 전까지는 daily_pipeline/main.py 경고플래그가 계속 참조).
-- 거래대금(*_value)은 KIS 응답상 백만원 단위로 추정(단위 미확정, raw 그대로 저장)
CREATE TABLE IF NOT EXISTS investor_trading_kis (
    symbol              TEXT NOT NULL,
    date                TEXT NOT NULL,      -- 'YYYYMMDD'
    close               INTEGER,            -- 종가(참고용)
    indiv_net_qty       INTEGER,            -- 개인 순매수 수량(주)
    foreign_net_qty     INTEGER,            -- 외국인 순매수 수량(주)
    inst_net_qty        INTEGER,            -- 기관계 순매수 수량(주)
    indiv_net_value     INTEGER,            -- 개인 순매수 거래대금(단위 미확정)
    foreign_net_value   INTEGER,            -- 외국인 순매수 거래대금
    inst_net_value      INTEGER,            -- 기관계 순매수 거래대금
    collected_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (symbol, date),
    FOREIGN KEY (symbol) REFERENCES stocks(symbol)
);

-- 종목별 일별 투자자매매동향 상세 (KIS investor-trade-by-stock-daily, FHPTJ04160001)
-- 2026-06-27 신설 — 위 investor_trading_kis와 달리 기관을 8개로 세분화(연기금 포함)하고
-- 단일 날짜 앵커+페이지네이션으로 수년치 백필 가능(30거래일/콜). 순매수 수량(ntby_qty) +
-- 순매수 거래대금(ntby_tr_pbmn) 둘 다 저장(API 호출수는 동일, 같은 응답에서 더 파싱하는
-- 것뿐이라 추가비용 없음) — 매수/매도 원시값(별도 _seln_/_shnu_ 필드)은 미저장.
CREATE TABLE IF NOT EXISTS investor_trading_kis_detail (
    symbol              TEXT NOT NULL,
    date                TEXT NOT NULL,        -- 'YYYYMMDD'
    close               INTEGER,              -- 종가(참고/정합성검증용)
    indiv_qty           INTEGER,              -- 개인 순매수 수량
    foreign_qty         INTEGER,              -- 외국인 전체 순매수 수량
    foreign_reg_qty     INTEGER,              -- 외국인 등록 순매수 수량
    foreign_nreg_qty    INTEGER,              -- 외국인 비등록 순매수 수량
    inst_total_qty      INTEGER,              -- 기관계 합계 순매수 수량
    securities_qty      INTEGER,              -- 증권 순매수 수량
    trust_qty           INTEGER,              -- 투자신탁 순매수 수량
    pe_fund_qty         INTEGER,              -- 사모펀드 순매수 수량
    bank_qty            INTEGER,              -- 은행 순매수 수량
    insurance_qty       INTEGER,              -- 보험 순매수 수량
    merchant_bank_qty   INTEGER,              -- 종금 순매수 수량
    pension_qty         INTEGER,              -- 기금(연기금) 순매수 수량
    etc_qty             INTEGER,              -- 기타(법인+단체) 순매수 수량
    indiv_value         INTEGER,              -- 개인 순매수 거래대금
    foreign_value       INTEGER,              -- 외국인 전체 순매수 거래대금
    foreign_reg_value   INTEGER,              -- 외국인 등록 순매수 거래대금
    foreign_nreg_value  INTEGER,              -- 외국인 비등록 순매수 거래대금
    inst_total_value    INTEGER,              -- 기관계 합계 순매수 거래대금
    securities_value    INTEGER,              -- 증권 순매수 거래대금
    trust_value         INTEGER,              -- 투자신탁 순매수 거래대금
    pe_fund_value        INTEGER,             -- 사모펀드 순매수 거래대금
    bank_value           INTEGER,             -- 은행 순매수 거래대금
    insurance_value      INTEGER,             -- 보험 순매수 거래대금
    merchant_bank_value  INTEGER,             -- 종금 순매수 거래대금
    pension_value        INTEGER,             -- 기금(연기금) 순매수 거래대금
    etc_value             INTEGER,            -- 기타(법인+단체) 순매수 거래대금
    collected_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (symbol, date),
    FOREIGN KEY (symbol) REFERENCES stocks(symbol)
);

-- investor_trading_kis_detail 백필 진행상황 체크포인트 (중단/재개용)
CREATE TABLE IF NOT EXISTS investor_backfill_progress (
    symbol                TEXT PRIMARY KEY,
    oldest_date_collected TEXT,               -- 지금까지 백필된 가장 오래된 날짜
    target_start_date     TEXT NOT NULL,      -- 목표 시작일 (예: '20210101')
    status                TEXT NOT NULL DEFAULT 'pending',  -- pending|in_progress|done|no_more_data|failed
    n_calls               INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,
    updated_at            TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (symbol) REFERENCES stocks(symbol)
);

-- 시장 지수 일봉 (KOSPI=1001, KOSDAQ=2001)
CREATE TABLE IF NOT EXISTS market_index (
    date    TEXT NOT NULL,
    code    TEXT NOT NULL,  -- '1001'=KOSPI, '2001'=KOSDAQ
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    volume  REAL,
    PRIMARY KEY (date, code)
);

-- 모의투자 추적
CREATE TABLE IF NOT EXISTS paper_trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    recommended_date  TEXT NOT NULL,    -- YYYYMMDD
    recommended_rank  INTEGER,          -- 추천 순위 (1~N)
    recommended_prob  REAL,             -- 모델 확률 (0~1)
    recommended_price INTEGER,          -- 추천일 종가 (매수가 기준)
    status            TEXT NOT NULL DEFAULT 'open',  -- 'open'|'closed'|'expired'
    close_date        TEXT,             -- 청산일 YYYYMMDD
    close_price       INTEGER,          -- 청산가 (종가)
    return_pct        REAL,             -- 수익률 (%)
    holding_days      INTEGER,          -- 보유 거래일 수
    model_version     TEXT,             -- 서빙 모델 디렉터리명 (추적성용)
    regime_gate_blocked INTEGER NOT NULL DEFAULT 0,  -- G2 게이트 차단 여부 (0=실진입, 1=shadow)
    created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (symbol, recommended_date)
);

-- 역사 시뮬레이션 거래 이력 (영속 아카이브)
CREATE TABLE IF NOT EXISTS history_trades (
    id          TEXT PRIMARY KEY,          -- frontend crypto.randomUUID()
    session_id  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,             -- 'buy' | 'sell'
    quantity    INTEGER NOT NULL,
    price       REAL NOT NULL,
    total       REAL NOT NULL,
    game_ts     INTEGER NOT NULL,          -- 종목 기준 날짜 (Unix ms)
    note        TEXT,
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 배당 (DART 사업보고서 "배당에 관한 사항" 기준, 사업연도당 1행)
-- pykrx의 EPS/BPS/DIV/DPS 비공식 API가 KRX 로그인 요구로 막혀(2026-06 기준) DART로 대체.
-- ex_dividend_date/record_date는 공시에 명시값이 없어 결산일 기준 거래일 캘린더로 계산한 근사값.
CREATE TABLE IF NOT EXISTS dividends (
    symbol            TEXT NOT NULL,
    biz_year          INTEGER NOT NULL,   -- 사업연도 (예: 2023)
    settlement_date   TEXT,               -- 결산일 'YYYYMMDD' (DART stlm_dt)
    record_date       TEXT,               -- 배당기준일 'YYYYMMDD' (결산일 기준 가장 가까운 거래일, 추정)
    ex_dividend_date  TEXT,               -- 배당락일 'YYYYMMDD' (배당기준일 1거래일 전, 추정)
    dps               INTEGER,            -- 주당 현금배당금(원) — 종목 유형(보통/우선)에 맞는 값
    dividend_yield    REAL,               -- 현금배당수익률(%), DART 공시 기준값(공시 시점 기준이라 일별 시세와는 약간 다를 수 있음)
    payout_ratio      REAL,               -- (연결)현금배당성향(%)
    eps               INTEGER,            -- (연결)주당순이익(원), 참고용
    par_value         INTEGER,            -- 주당액면가액(원), 참고용
    stock_type        TEXT,               -- '보통주' | '우선주'
    corp_code         TEXT,               -- DART corp_code (추적용)
    created_at        TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (symbol, biz_year),
    FOREIGN KEY (symbol) REFERENCES stocks(symbol)
);

-- 재무제표 기반 BPS (DART 자본총계+주식총수, 사업연도당 1행) — 저PBR 스크리너 필터 검증용
-- 자본총계는 연결기준(CFS), 주식수는 보통주+우선주 합계. 회사당 2회 DART 호출(재무제표+주식총수).
CREATE TABLE IF NOT EXISTS financials (
    symbol            TEXT NOT NULL,
    biz_year          INTEGER NOT NULL,
    settlement_date   TEXT,               -- 결산일 'YYYYMMDD' (재무제표 thstrm_dt 기반, 근사)
    total_equity       REAL,               -- 자본총계(원)
    shares_total       REAL,               -- 발행주식총수(보통+우선)
    bps                REAL,               -- 주당순자산 = total_equity / shares_total
    corp_code         TEXT,
    created_at        TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (symbol, biz_year),
    FOREIGN KEY (symbol) REFERENCES stocks(symbol)
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_dividends_symbol   ON dividends      (symbol);
CREATE INDEX IF NOT EXISTS idx_dividends_year     ON dividends      (biz_year);
CREATE INDEX IF NOT EXISTS idx_financials_symbol  ON financials     (symbol);
CREATE INDEX IF NOT EXISTS idx_financials_year    ON financials     (biz_year);
CREATE INDEX IF NOT EXISTS idx_prices_date        ON prices         (date);
CREATE INDEX IF NOT EXISTS idx_prices_symbol_vol  ON prices         (symbol, volume);
CREATE INDEX IF NOT EXISTS idx_fund_date          ON fundamentals   (date);
CREATE INDEX IF NOT EXISTS idx_flows_date         ON flows          (date);
CREATE INDEX IF NOT EXISTS idx_mktidx_code        ON market_index   (code, date);
CREATE INDEX IF NOT EXISTS idx_paper_date         ON paper_trades   (recommended_date);
CREATE INDEX IF NOT EXISTS idx_paper_status       ON paper_trades   (status);
CREATE INDEX IF NOT EXISTS idx_history_session    ON history_trades (session_id);
CREATE INDEX IF NOT EXISTS idx_itk_symbol         ON investor_trading_kis        (symbol);
CREATE INDEX IF NOT EXISTS idx_itkd_symbol        ON investor_trading_kis_detail (symbol);
CREATE INDEX IF NOT EXISTS idx_ibp_status         ON investor_backfill_progress  (status);

-- 매크로 지표 (yfinance 외부 + DB 내부 집계, 시각화 전용)
CREATE TABLE IF NOT EXISTS macro_indicators (
    date      TEXT NOT NULL,
    indicator TEXT NOT NULL,
    value     REAL,
    PRIMARY KEY (date, indicator)
);
CREATE INDEX IF NOT EXISTS idx_macro_date ON macro_indicators (date DESC);

-- 예측 이력 (append-only, 수정 금지 — 라이브 성과 추적 기반)
-- score = raw 확률(calibrated 아님), 모델 랭킹과 동일 기준
CREATE TABLE IF NOT EXISTS prediction_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    predicted_at    TEXT NOT NULL,    -- 예측 실행 날짜 YYYYMMDD
    model           TEXT NOT NULL,    -- '5d' | '60d'
    symbol          TEXT NOT NULL,
    rank            INTEGER NOT NULL,
    score           REAL,             -- raw 확률 (0~1)
    horizon         INTEGER NOT NULL, -- 5 or 60
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 예측 실현 결과 (만기 도래 시 자동 계산, prediction_log와 1:1)
-- entry_price = 추천 익일 시가 (없으면 추천일 종가 대체)
-- exit_price  = 만기 시점 종가 (5d→5거래일 후, 60d→60거래일 후)
CREATE TABLE IF NOT EXISTS prediction_outcomes (
    prediction_id     INTEGER NOT NULL REFERENCES prediction_log(id),
    entry_price       INTEGER,
    exit_price        INTEGER,
    return_pct        REAL,           -- 수익률 (%)
    market_return_pct REAL,           -- 같은 기간 KOSPI 수익률 (초과수익 계산용)
    is_hit            INTEGER,        -- 1=성공: 5d는 종가+5%, 60d는 초과수익>0
    settled_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (prediction_id)
);

CREATE INDEX IF NOT EXISTS idx_pred_log_date  ON prediction_log (predicted_at);
CREATE INDEX IF NOT EXISTS idx_pred_log_model ON prediction_log (model, predicted_at);

CREATE TABLE IF NOT EXISTS watchlist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL UNIQUE,
    name       TEXT,
    added_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    memo       TEXT
);

-- 시그널 로그 (파이프라인 실행 시 규칙 기반 이벤트 감지 → INSERT)
-- event_type: foreign_surge | score60d_entry | watchlist_price_jump | sector_quadrant | position_event
CREATE TABLE IF NOT EXISTS signal_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    ticker      TEXT,           -- 종목코드 (nullable: 섹터 이벤트 등)
    sector      TEXT,           -- 섹터명 (nullable)
    message     TEXT NOT NULL,  -- 사람이 읽기 좋은 요약
    data        TEXT,           -- JSON 추가 데이터
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_signal_log_created ON signal_log (created_at);
CREATE INDEX IF NOT EXISTS idx_signal_log_type    ON signal_log (event_type);

CREATE TABLE IF NOT EXISTS my_portfolio (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT NOT NULL,
    name       TEXT NOT NULL,
    buy_price  REAL NOT NULL,
    quantity   REAL NOT NULL,
    buy_date   TEXT NOT NULL,
    memo       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_db(conn: sqlite3.Connection) -> None:
    """기존 DB에 누락된 컬럼을 방어적으로 추가한다.
    PRAGMA table_info로 먼저 확인하므로 이미 존재하는 컬럼은 건너뜀.
    """
    migrations = [
        ("flows",        "foreign_rate_source", "TEXT"),
        ("paper_trades", "model_version",        "TEXT"),
        ("paper_trades", "regime_gate_blocked",  "INTEGER NOT NULL DEFAULT 0"),
    ]
    for table, col, col_type in migrations:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            logger.info("마이그레이션: %s.%s 컬럼 추가", table, col)


def init_db():
    """DB 파일 생성 + 스키마 적용 + 누락 컬럼 마이그레이션"""
    with transaction() as conn:
        conn.executescript(DDL)
        _migrate_db(conn)
    logger.info("DB 초기화 완료: %s", DB_PATH)


_ALLOWED_TABLES = {
    "prices", "features", "prediction_log", "prediction_outcomes",
    "market_index", "flows", "fundamentals", "dividends", "financials",
    "investor_trading_kis", "investor_trading_kis_detail",
}


def get_latest_date(symbol: str, table: str = "prices") -> Optional[str]:
    """특정 종목의 해당 테이블 마지막 날짜 반환 (증분 업데이트용)"""
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"get_latest_date: 허용되지 않은 테이블: {table!r}")
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT MAX(date) FROM {table} WHERE symbol = ?", (symbol,)
        ).fetchone()
    return row[0] if row else None


def get_all_symbols() -> List[Dict]:
    """저장된 전체 종목 목록 반환"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT symbol, name, market FROM stocks ORDER BY market, symbol"
        ).fetchall()
    return [{"symbol": r[0], "name": r[1], "market": r[2]} for r in rows]


def upsert_stocks(records: List[Dict]):
    """종목 마스터 upsert"""
    with transaction() as conn:
        conn.executemany(
            """
            INSERT INTO stocks (symbol, name, market, sector, updated_at)
            VALUES (:symbol, :name, :market, :sector, datetime('now','localtime'))
            ON CONFLICT(symbol) DO UPDATE SET
                name=excluded.name, market=excluded.market,
                sector=COALESCE(excluded.sector, sector),
                updated_at=excluded.updated_at
            """,
            records,
        )


def insert_prices(records: List[Dict]):
    """prices IGNORE (이미 있는 날짜 스킵)"""
    with transaction() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO prices
                (symbol, date, open, high, low, close, volume, market_cap)
            VALUES
                (:symbol, :date, :open, :high, :low, :close, :volume, :market_cap)
            """,
            records,
        )


def insert_fundamentals(records: List[Dict]):
    with transaction() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO fundamentals (symbol, date, per, pbr, div_yield)
            VALUES (:symbol, :date, :per, :pbr, :div_yield)
            """,
            records,
        )


def insert_flows(records: List[Dict]):
    with transaction() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO flows (symbol, date, foreign_net, inst_net)
            VALUES (:symbol, :date, :foreign_net, :inst_net)
            """,
            records,
        )


def insert_investor_trading_kis(records: List[Dict]):
    """investor_trading_kis upsert (날짜 재조회 시 최신값으로 덮어씀 — KIS가 최근 ~30거래일
    롤링 윈도우만 주는 구조라 같은 날짜를 여러 번 받을 수 있음)"""
    with transaction() as conn:
        conn.executemany(
            """
            INSERT INTO investor_trading_kis
                (symbol, date, close, indiv_net_qty, foreign_net_qty, inst_net_qty,
                 indiv_net_value, foreign_net_value, inst_net_value, collected_at)
            VALUES
                (:symbol, :date, :close, :indiv_net_qty, :foreign_net_qty, :inst_net_qty,
                 :indiv_net_value, :foreign_net_value, :inst_net_value, datetime('now','localtime'))
            ON CONFLICT(symbol, date) DO UPDATE SET
                close=excluded.close,
                indiv_net_qty=excluded.indiv_net_qty,
                foreign_net_qty=excluded.foreign_net_qty,
                inst_net_qty=excluded.inst_net_qty,
                indiv_net_value=excluded.indiv_net_value,
                foreign_net_value=excluded.foreign_net_value,
                inst_net_value=excluded.inst_net_value,
                collected_at=excluded.collected_at
            """,
            records,
        )


_DETAIL_CATEGORIES = [
    "indiv", "foreign", "foreign_reg", "foreign_nreg", "inst_total",
    "securities", "trust", "pe_fund", "bank", "insurance",
    "merchant_bank", "pension", "etc",
]
_DETAIL_COLS = [f"{c}_qty" for c in _DETAIL_CATEGORIES] + [f"{c}_value" for c in _DETAIL_CATEGORIES]


def insert_investor_trading_kis_detail(records: List[Dict]):
    """investor_trading_kis_detail upsert. records의 각 dict는 symbol/date/close +
    _DETAIL_COLS 26개(13개 카테고리 x 수량/대금) 키를 가져야 함."""
    cols = ["symbol", "date", "close"] + _DETAIL_COLS
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    update_list = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("symbol", "date"))
    with transaction() as conn:
        conn.executemany(
            f"""
            INSERT INTO investor_trading_kis_detail ({col_list}, collected_at)
            VALUES ({placeholders}, datetime('now','localtime'))
            ON CONFLICT(symbol, date) DO UPDATE SET
                {update_list},
                collected_at=excluded.collected_at
            """,
            records,
        )


def get_backfill_progress(symbol: str) -> Optional[Dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT symbol, oldest_date_collected, target_start_date, status, n_calls "
            "FROM investor_backfill_progress WHERE symbol = ?",
            (symbol,),
        ).fetchone()
    if not row:
        return None
    return {
        "symbol": row[0], "oldest_date_collected": row[1],
        "target_start_date": row[2], "status": row[3], "n_calls": row[4],
    }


def upsert_backfill_progress(symbol: str, target_start_date: str, oldest_date_collected: Optional[str],
                              status: str, n_calls_delta: int = 0, last_error: Optional[str] = None):
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO investor_backfill_progress
                (symbol, oldest_date_collected, target_start_date, status, n_calls, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(symbol) DO UPDATE SET
                oldest_date_collected = COALESCE(?, oldest_date_collected),
                status = ?,
                n_calls = n_calls + ?,
                last_error = ?,
                updated_at = datetime('now','localtime')
            """,
            (symbol, oldest_date_collected, target_start_date, status, n_calls_delta, last_error,
             oldest_date_collected, status, n_calls_delta, last_error),
        )


def init_paper_trades_table():
    """paper_trades 테이블 생성 (없으면)"""
    with transaction() as conn:
        conn.executescript(DDL)
    logger.info("paper_trades 테이블 초기화 완료")


def get_latest_index_date(code: str) -> Optional[str]:
    """market_index 테이블에서 특정 지수의 마지막 날짜 반환"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(date) FROM market_index WHERE code = ?", (code,)
        ).fetchone()
    return row[0] if row else None


def insert_index_ohlcv(records: List[Dict]):
    """market_index IGNORE (이미 있는 날짜 스킵)"""
    with transaction() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO market_index (date, code, open, high, low, close, volume)
            VALUES (:date, :code, :open, :high, :low, :close, :volume)
            """,
            records,
        )
