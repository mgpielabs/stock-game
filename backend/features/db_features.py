"""
features 테이블 DDL + CRUD
모든 피처는 (symbol, date) 복합 PK로 저장
"""

import bisect
import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import numpy as np
import pandas as pd

# collector의 DB와 동일한 파일 사용
DB_PATH = Path(__file__).parent.parent / "data" / "stocks.db"

logger = logging.getLogger(__name__)

DDL_FEATURES = """
CREATE TABLE IF NOT EXISTS features (
    symbol          TEXT NOT NULL,
    date            TEXT NOT NULL,

    -- ── 수익률 ──────────────────────────────────────────
    ret_1d          REAL,       -- 1일 수익률
    ret_5d          REAL,       -- 5일 수익률
    ret_20d         REAL,       -- 20일 수익률
    ret_60d         REAL,       -- 60일 수익률

    -- ── 이동평균 이격도 ──────────────────────────────────
    ma5_dev         REAL,       -- (종가/MA5) - 1
    ma20_dev        REAL,
    ma60_dev        REAL,
    ma120_dev       REAL,

    -- ── RSI ─────────────────────────────────────────────
    rsi_7           REAL,
    rsi_14          REAL,

    -- ── MACD (12/26/9) ──────────────────────────────────
    macd            REAL,       -- MACD 선
    macd_signal     REAL,       -- 시그널 선
    macd_hist       REAL,       -- 히스토그램

    -- ── 볼린저밴드 ───────────────────────────────────────
    bb_pct          REAL,       -- 밴드 내 위치 0(하단)~1(상단)
    bb_width        REAL,       -- 밴드 폭 / 중심선 (변동성 측정)

    -- ── 거래량 ──────────────────────────────────────────
    vol_ratio_5d    REAL,       -- 거래량 / 5일 평균
    vol_ratio_20d   REAL,       -- 거래량 / 20일 평균
    vol_surge       INTEGER,    -- vol_ratio_5d > 2 이면 1

    -- ── ATR ─────────────────────────────────────────────
    atr_14          REAL,       -- 14일 Average True Range
    atr_pct         REAL,       -- ATR / 종가 (정규화)

    -- ── 패턴 ─────────────────────────────────────────────
    gap_pct         REAL,       -- (시가 - 전일종가) / 전일종가
    body_ratio      REAL,       -- |종가-시가| / (고가-저가), 장대양봉 판별
    upper_shadow    REAL,       -- 윗꼬리 비율
    lower_shadow    REAL,       -- 아랫꼬리 비율
    up_streak       INTEGER,    -- 연속 상승일수
    down_streak     INTEGER,    -- 연속 하락일수

    -- ── 시장 컨텍스트 ────────────────────────────────────
    rel_market_1d   REAL,       -- 종목 1일 수익률 - 시장 평균 1일 수익률
    rel_market_5d   REAL,       -- 5일 기준
    rel_market_20d  REAL,       -- 20일 기준
    rel_sector_5d   REAL,       -- 동일 시장 섹터 대비 (시장=KOSPI/KOSDAQ)
    rel_sector_20d  REAL,

    -- ── 수급 (외국인 보유비율 기반) ──────────────────────
    foreign_rate    REAL,       -- 당일 외국인 보유비율 (%)
    foreign_1d_chg  REAL,       -- 1일 변화
    foreign_5d_chg  REAL,       -- 5일 누적 변화 (순매수 방향 proxy)
    foreign_trend   REAL,       -- 10일 선형회귀 기울기 (추세)

    -- ── 펀더멘털 ─────────────────────────────────────────
    per             REAL,       -- PER
    pbr             REAL,       -- PBR

    -- ── 시장 국면 ─────────────────────────────────────────
    kospi_ma200_ratio    REAL,  -- KOSPI 종가 / 200일 MA (>1 강세)
    kospi_volatility_20d REAL,  -- KOSPI 20일 수익률 표준편차
    kosdaq_kospi_ratio   REAL,  -- KOSDAQ 5일 수익률 - KOSPI 5일 수익률
    market_breadth       REAL,  -- 전일 대비 상승 종목 비율

    -- ── 시계열 lag 피처 ──────────────────────────────────────
    ret_lag_1        REAL,      -- 1일 전 일간 수익률
    ret_lag_2        REAL,
    ret_lag_3        REAL,
    ret_lag_5        REAL,
    ret_lag_10       REAL,
    vol_ratio_lag_1  REAL,      -- 1일 전 거래량 비율 (vol/20일평균)
    vol_ratio_lag_3  REAL,
    vol_ratio_lag_5  REAL,
    up_days_5        REAL,      -- 최근 5일 중 상승일 수
    up_days_10       REAL,      -- 최근 10일 중 상승일 수
    volatility_5     REAL,      -- 최근 5일 수익률 표준편차
    volatility_20    REAL,      -- 최근 20일 수익률 표준편차
    price_position_20 REAL,     -- (현재가-20일최저)/(20일최고-20일최저)
    price_position_60 REAL,     -- (현재가-60일최저)/(60일최고-60일최저)

    -- ── 타겟 (학습용) ────────────────────────────────────
    target          INTEGER,    -- 다음날 상승=1, 하락=0, NULL=데이터없음

    PRIMARY KEY (symbol, date),
    FOREIGN KEY (symbol) REFERENCES stocks(symbol)
);

CREATE INDEX IF NOT EXISTS idx_features_date ON features (date);
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


_REGIME_COLS = [
    ("kospi_ma200_ratio",    "REAL"),
    ("kospi_volatility_20d", "REAL"),
    ("kosdaq_kospi_ratio",   "REAL"),
    ("market_breadth",       "REAL"),
    ("ret_lag_1",            "REAL"),
    ("ret_lag_2",            "REAL"),
    ("ret_lag_3",            "REAL"),
    ("ret_lag_5",            "REAL"),
    ("ret_lag_10",           "REAL"),
    ("vol_ratio_lag_1",      "REAL"),
    ("vol_ratio_lag_3",      "REAL"),
    ("vol_ratio_lag_5",      "REAL"),
    ("up_days_5",            "REAL"),
    ("up_days_10",           "REAL"),
    ("volatility_5",         "REAL"),
    ("volatility_20",        "REAL"),
    ("price_position_20",    "REAL"),
    ("price_position_60",    "REAL"),
    # 수급 신호 피처 (investor_trading_kis_detail 기반, 2026-07-02 추가)
    # 기각됨(2026-07-02): D안 재학습에서 SHAP 0.14%/0.00%, 독립예측력 없음 확인.
    # 컬럼 및 증분계산은 유지(재계산 비용), FEATURE_COLS_REDUCED에서는 제외.
    ("frgn_norm_cum10",      "REAL"),
    ("frgn_streak3",         "REAL"),
]


def _migrate_features_table(conn: sqlite3.Connection) -> None:
    """기존 features 테이블에 시장 국면 컬럼이 없으면 ALTER TABLE로 추가"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(features)")}
    for col, typ in _REGIME_COLS:
        if col not in existing:
            conn.execute(f"ALTER TABLE features ADD COLUMN {col} {typ}")
            logger.info("features 컬럼 추가: %s %s", col, typ)


def init_features_table():
    with transaction() as conn:
        conn.executescript(DDL_FEATURES)
        _migrate_features_table(conn)
    logger.info("features 테이블 초기화 완료")


def get_last_feature_date(symbol: str) -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(date) FROM features WHERE symbol=?", (symbol,)
        ).fetchone()
    return row[0] if row else None


def upsert_features(records: List[Dict]):
    """
    features 테이블에 upsert (ON CONFLICT REPLACE)
    재계산 시 최신 값으로 덮어쓰기 위해 REPLACE 사용
    """
    if not records:
        return

    cols = [
        "symbol", "date",
        "ret_1d", "ret_5d", "ret_20d", "ret_60d",
        "ma5_dev", "ma20_dev", "ma60_dev", "ma120_dev",
        "rsi_7", "rsi_14",
        "macd", "macd_signal", "macd_hist",
        "bb_pct", "bb_width",
        "vol_ratio_5d", "vol_ratio_20d", "vol_surge",
        "atr_14", "atr_pct",
        "gap_pct", "body_ratio", "upper_shadow", "lower_shadow",
        "up_streak", "down_streak",
        "rel_market_1d", "rel_market_5d", "rel_market_20d",
        "rel_sector_5d", "rel_sector_20d",
        "foreign_rate", "foreign_1d_chg", "foreign_5d_chg", "foreign_trend",
        "per", "pbr",
        "kospi_ma200_ratio", "kospi_volatility_20d", "kosdaq_kospi_ratio", "market_breadth",
        "ret_lag_1", "ret_lag_2", "ret_lag_3", "ret_lag_5", "ret_lag_10",
        "vol_ratio_lag_1", "vol_ratio_lag_3", "vol_ratio_lag_5",
        "up_days_5", "up_days_10",
        "volatility_5", "volatility_20",
        "price_position_20", "price_position_60",
        # frgn_norm_cum10, frgn_streak3: pipeline.py 계산 제거됨(L1 수정 2026-07-05).
        # 컬럼은 DB에 유지, upsert 대상에서만 제외(기존 저장분 보존).
        "target",
    ]
    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)

    with transaction() as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO features ({col_list}) VALUES ({placeholders})",
            records,
        )


def load_prices_for_symbol(symbol: str, start: Optional[str] = None) -> pd.DataFrame:
    """prices 테이블에서 단일 종목 가격 데이터 로드"""
    q = "SELECT date, open, high, low, close, volume FROM prices WHERE symbol=?"
    params = [symbol]
    if start:
        q += " AND date >= ?"
        params.append(start)
    q += " ORDER BY date ASC"

    with get_connection() as conn:
        df = pd.read_sql_query(q, conn, params=params)
    return df


def load_all_closes(start: Optional[str] = None) -> pd.DataFrame:
    """
    전종목 종가를 date × symbol 피벗 테이블로 반환
    시장 평균 수익률 계산에 사용
    """
    q = "SELECT date, symbol, close FROM prices"
    params = []
    if start:
        q += " WHERE date >= ?"
        params.append(start)
    q += " ORDER BY date"

    with get_connection() as conn:
        df = pd.read_sql_query(q, conn, params=params)

    if df.empty:
        return pd.DataFrame()

    pivot = df.pivot(index="date", columns="symbol", values="close")
    return pivot


def load_flows(symbol: str, start: Optional[str] = None) -> pd.DataFrame:
    q = "SELECT date, foreign_net FROM flows WHERE symbol=?"
    params = [symbol]
    if start:
        q += " AND date >= ?"
        params.append(start)
    q += " ORDER BY date ASC"

    with get_connection() as conn:
        df = pd.read_sql_query(q, conn, params=params)
    return df


def load_fundamentals_latest(symbol: str) -> Dict:
    """[조사 결과 look-ahead bias 확인됨 — 2026-06-27]
    date 필터가 없어 항상 "가장 최근" PER/PBR을 과거 모든 날짜에 동일하게 broadcast함.
    신규 학습 피처는 load_per_pbr_pit()을 사용할 것. 이 함수는 기존 검증 스크립트 호환용으로만 보존."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT per, pbr FROM fundamentals WHERE symbol=? ORDER BY date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    if row:
        return {"per": row[0], "pbr": row[1]}
    return {"per": None, "pbr": None}


def load_per_pbr_pit(symbol: str, dates: pd.Index, close: pd.Series) -> pd.DataFrame:
    """point-in-time PER/PBR (ml/per_validation.py·pbr_validation.py와 동일한 방식 재사용).

    dividends.eps / financials.bps는 사업연도 단위로만 존재하므로, 공시지연 약 3개월을
    가정해 (사업연도+1)년 4/1부터 그 EPS/BPS를 "그 시점에 알 수 있었던 값"으로 취급한다.
    date <= 평가일자 조건의 가장 최근 값만 사용해 look-ahead bias를 제거한다.

    dates: 'YYYYMMDD' 문자열 인덱스 (combined.index와 동일)
    close: dates와 동일 인덱스의 종가 시리즈
    """
    with get_connection() as conn:
        eps_rows = conn.execute(
            "SELECT biz_year, eps FROM dividends WHERE symbol=? AND eps IS NOT NULL ORDER BY biz_year",
            (symbol,),
        ).fetchall()
        bps_rows = conn.execute(
            "SELECT biz_year, bps FROM financials WHERE symbol=? AND bps IS NOT NULL ORDER BY biz_year",
            (symbol,),
        ).fetchall()

    dates_arr = pd.Index(dates).astype(str).to_numpy()
    per_pit = pd.Series(np.nan, index=dates)
    pbr_pit = pd.Series(np.nan, index=dates)

    if eps_rows:
        known_from = np.array([f"{biz_year + 1}0401" for biz_year, _ in eps_rows])
        eps_vals = np.array([eps for _, eps in eps_rows], dtype=float)
        order = np.argsort(known_from)
        known_from, eps_vals = known_from[order], eps_vals[order]
        pos = np.searchsorted(known_from, dates_arr, side="right") - 1
        valid = pos >= 0
        eps_pit = np.full(len(dates_arr), np.nan)
        eps_pit[valid] = eps_vals[pos[valid]]
        with np.errstate(divide="ignore", invalid="ignore"):
            per_vals = close.to_numpy() / eps_pit
        per_vals[eps_pit <= 0] = np.nan
        per_pit = pd.Series(per_vals, index=dates)

    if bps_rows:
        known_from = np.array([f"{biz_year + 1}0401" for biz_year, _ in bps_rows])
        bps_vals = np.array([bps for _, bps in bps_rows], dtype=float)
        order = np.argsort(known_from)
        known_from, bps_vals = known_from[order], bps_vals[order]
        pos = np.searchsorted(known_from, dates_arr, side="right") - 1
        valid = pos >= 0
        bps_pit = np.full(len(dates_arr), np.nan)
        bps_pit[valid] = bps_vals[pos[valid]]
        with np.errstate(divide="ignore", invalid="ignore"):
            pbr_vals = close.to_numpy() / bps_pit
        pbr_vals[bps_pit <= 0] = np.nan
        pbr_pit = pd.Series(pbr_vals, index=dates)

    return pd.DataFrame({"per": per_pit, "pbr": pbr_pit})


def load_all_symbols_with_market() -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT symbol, market FROM stocks ORDER BY market, symbol"
        ).fetchall()
    return [{"symbol": r[0], "market": r[1]} for r in rows]


def load_frgn_features(symbol: str, dates: pd.Index) -> pd.DataFrame:
    """frgn_norm_cum10 / frgn_streak3 계산 (investor_trading_kis_detail 기반, PIT 보장).

    rolling 자체가 D까지의 데이터만 사용하므로 look-ahead 없음.
    dates: features의 날짜 인덱스 (YYYYMMDD 문자열). kis_detail에 없는 날짜는 NaN.
    """
    if len(dates) == 0:
        return pd.DataFrame({"frgn_norm_cum10": [], "frgn_streak3": []}, index=dates)

    # rolling 10 워밍업 여유 15거래일 포함해 시작일 이전부터 로드
    try:
        d_start = (datetime.strptime(str(min(dates)), "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
    except ValueError:
        d_start = "20200101"

    with get_connection() as conn:
        kis = pd.read_sql_query(
            """SELECT date,
                      COALESCE(foreign_value, 0) AS foreign_value,
                      close
               FROM investor_trading_kis_detail
               WHERE symbol = ? AND date >= ?
               ORDER BY date ASC""",
            conn, params=(symbol, d_start),
        )
        shares_rows = conn.execute(
            "SELECT biz_year, shares_total FROM financials "
            "WHERE symbol=? AND shares_total IS NOT NULL AND shares_total > 0 "
            "ORDER BY biz_year",
            (symbol,),
        ).fetchall()

    empty = pd.DataFrame(
        {"frgn_norm_cum10": np.nan, "frgn_streak3": np.nan},
        index=dates,
    )
    if kis.empty:
        return empty

    # PIT shares lookup
    shares_entries = sorted(
        (f"{int(by) + 1}0401", float(sh)) for by, sh in shares_rows
    )
    known_froms = [e[0] for e in shares_entries]

    def _get_shares(date_str: str) -> float:
        if not shares_entries:
            return np.nan
        idx = bisect.bisect_right(known_froms, date_str) - 1
        if idx < 0:
            return shares_entries[0][1]
        return shares_entries[idx][1]

    kis = kis.set_index("date")
    fv = kis["foreign_value"].astype(float)
    cl = kis["close"].astype(float)

    sh = np.array([_get_shares(d) for d in fv.index], dtype=float)
    mktcap = cl.values * sh
    mktcap[mktcap == 0] = np.nan

    cum10 = fv.rolling(10, min_periods=5).sum().values / mktcap
    pos = (fv > 0).astype(float)
    streak3 = pos.rolling(3, min_periods=3).min().values

    kis_result = pd.DataFrame(
        {"frgn_norm_cum10": cum10, "frgn_streak3": streak3},
        index=fv.index,
    )

    # features 날짜 기준 left join (kis_detail에 없는 날짜는 NaN)
    result = pd.DataFrame(index=dates)
    result = result.join(kis_result, how="left")
    return result


def load_symbol_sector_map() -> Dict[str, Optional[str]]:
    """symbol -> sector(DART induty_code) 매핑. sector_collector.py로 수집한 값,
    아직 수집 안 된 종목은 None(market_context.py가 시장 평균으로 폴백)."""
    with get_connection() as conn:
        rows = conn.execute("SELECT symbol, sector FROM stocks").fetchall()
    return {r[0]: r[1] for r in rows}


def load_index_closes(start: Optional[str] = None) -> pd.DataFrame:
    """
    market_index 테이블에서 KOSPI(1001)/KOSDAQ(2001) 종가를 날짜 × 코드 피벗으로 반환
    반환: DataFrame  index=date  columns=['1001','2001']
    """
    q = "SELECT date, code, close FROM market_index"
    params = []
    if start:
        q += " WHERE date >= ?"
        params.append(start)
    q += " ORDER BY date"

    with get_connection() as conn:
        df = pd.read_sql_query(q, conn, params=params)

    if df.empty:
        return pd.DataFrame(columns=["1001", "2001"])

    pivot = df.pivot(index="date", columns="code", values="close")
    pivot.columns.name = None
    return pivot
