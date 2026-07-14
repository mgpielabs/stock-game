"""
KIS FHPST04760000 신용대주잔고 소표본 수집
- 시총 상위 50 + 중형 50 = 100종목
- 2021년부터 현재까지 최대 기간 수집
- DB 테이블: credit_balance_kis

실행: uv run --project backend python backend/analysis/collect_credit_balance.py
"""

import sys
import os
import json
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "data"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from kis_auth import get_access_token, BASE_URL
import requests

DB_PATH = ROOT / "data" / "stocks.db"

# ── 테이블 생성 ──────────────────────────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS credit_balance_kis (
    symbol         TEXT NOT NULL,
    date           TEXT NOT NULL,
    stln_rmnd_rate REAL,
    stln_rmnd_stcn INTEGER,
    stln_new_stcn  INTEGER,
    stln_rdmp_stcn INTEGER,
    loan_rmnd_rate REAL,
    PRIMARY KEY (symbol, date)
);
"""

UPSERT_SQL = """
INSERT OR REPLACE INTO credit_balance_kis
    (symbol, date, stln_rmnd_rate, stln_rmnd_stcn, stln_new_stcn, stln_rdmp_stcn, loan_rmnd_rate)
VALUES (?, ?, ?, ?, ?, ?, ?);
"""


def init_db(conn: sqlite3.Connection):
    conn.execute(CREATE_SQL)
    conn.commit()


def get_100_symbols() -> list[str]:
    """시총 상위 50 + 중형 50 선택."""
    with sqlite3.connect(DB_PATH) as conn:
        # stocks 테이블에 mktcap 있는지 확인
        cols = {r[1] for r in conn.execute("PRAGMA table_info(stocks)")}
        if "mktcap" in cols:
            df_large = conn.execute(
                "SELECT symbol FROM stocks WHERE mktcap IS NOT NULL "
                "ORDER BY mktcap DESC LIMIT 50"
            ).fetchall()
            df_mid = conn.execute(
                "SELECT symbol FROM stocks WHERE mktcap IS NOT NULL "
                "ORDER BY mktcap DESC LIMIT 150"
            ).fetchall()
            large = [r[0] for r in df_large]
            # 중형: 51위~100위 (대형 제외)
            mid = [r[0] for r in df_mid if r[0] not in set(large)][:50]
        else:
            # fallback: prices로 최근 거래대금 상위 100 (volume × close 근사)
            print("  stocks.mktcap 없음 — 거래대금 기준 100종목 선택")
            rows = conn.execute("""
                SELECT p.symbol
                FROM prices p
                WHERE p.date >= '20260601'
                  AND p.symbol NOT LIKE '1%'
                  AND p.symbol NOT LIKE '9%'
                GROUP BY p.symbol
                HAVING COUNT(*) >= 5
                ORDER BY AVG(p.volume * p.close) DESC
                LIMIT 150
            """).fetchall()
            all_syms = [r[0] for r in rows]
            large = all_syms[:50]
            mid   = all_syms[50:100]

    symbols = large + mid
    # 6자리 zfill
    symbols = [str(s).zfill(6) for s in symbols]
    print(f"수집 대상: {len(symbols)}종목 (대형 {len(large)} + 중형 {len(mid)})")
    return symbols


def call_credit_balance(token: str, symbol: str, date: str) -> list[dict]:
    """FHPST04760000 호출. 오류 시 빈 리스트 반환."""
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": os.environ["KIS_APP_KEY"],
        "appsecret": os.environ["KIS_APP_SECRET"],
        "tr_id": "FHPST04760000",
    }
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_cond_scr_div_code": "20476",
        "fid_input_iscd": symbol,
        "fid_input_date_1": date,
    }
    try:
        r = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/daily-credit-balance",
            headers=headers, params=params, timeout=10,
        )
        data = r.json()
        if data.get("rt_cd") == "0":
            return data.get("output", [])
        return []
    except Exception:
        return []


def rows_to_records(symbol: str, rows: list[dict]) -> list[tuple]:
    records = []
    for r in rows:
        date = r.get("deal_date", "")
        if not date:
            continue
        stln_rate = r.get("whol_stln_rmnd_rate")
        stln_stcn = r.get("whol_stln_rmnd_stcn")
        stln_new  = r.get("whol_stln_new_stcn")
        stln_rdmp = r.get("whol_stln_rdmp_stcn")
        loan_rate = r.get("whol_loan_rmnd_rate")

        def to_float(v):
            try: return float(v) if v not in (None, "", "0") or v == "0" else float(v)
            except: return None

        def to_int(v):
            try: return int(v) if v not in (None, "") else None
            except: return None

        records.append((
            symbol, date,
            to_float(stln_rate),
            to_int(stln_stcn),
            to_int(stln_new),
            to_int(stln_rdmp),
            to_float(loan_rate),
        ))
    return records


def collect_symbol(token: str, symbol: str, start_date: str, end_date: str) -> int:
    """symbol의 start_date~end_date 전체 수집. 30행씩 반복 호출."""
    inserted = 0
    # 기준일을 end_date부터 시작해서 start_date 이전까지 거슬러 올라감
    current = datetime.strptime(end_date, "%Y%m%d")
    start_dt = datetime.strptime(start_date, "%Y%m%d")

    all_records: list[tuple] = []
    seen_dates: set[str] = set()

    while current >= start_dt:
        date_str = current.strftime("%Y%m%d")
        rows = call_credit_balance(token, symbol, date_str)
        if not rows:
            # 데이터 없음 — 더 이전으로 가도 없을 수 있지만 계속 시도
            current -= timedelta(days=45)  # 30거래일 ≈ 42일
            time.sleep(0.2)
            continue

        records = rows_to_records(symbol, rows)
        new_recs = [r for r in records if r[1] not in seen_dates]
        all_records.extend(new_recs)
        for r in new_recs:
            seen_dates.add(r[1])

        # 반환된 가장 이른 날짜를 기준으로 다음 기준일 설정
        batch_dates = [r[1] for r in records if r[1]]
        if batch_dates:
            earliest = min(batch_dates)
            earliest_dt = datetime.strptime(earliest, "%Y%m%d")
            current = earliest_dt - timedelta(days=1)
        else:
            current -= timedelta(days=45)

        time.sleep(0.25)

    if all_records:
        with sqlite3.connect(DB_PATH) as conn:
            conn.executemany(UPSERT_SQL, all_records)
            conn.commit()
        inserted = len(all_records)

    return inserted


def main():
    print("=" * 60)
    print("신용대주잔고 소표본 수집 (KIS FHPST04760000)")
    print("=" * 60)

    token = get_access_token()
    print("토큰 획득 완료")

    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
    print("테이블 초기화 완료")

    symbols = get_100_symbols()

    # 수집 기간: 2021-01-01 ~ 오늘
    START_DATE = "20210101"
    END_DATE   = datetime.now().strftime("%Y%m%d")
    print(f"수집 기간: {START_DATE} ~ {END_DATE}")

    total_inserted = 0
    n = len(symbols)

    for i, sym in enumerate(symbols, 1):
        t0 = time.time()
        inserted = collect_symbol(token, sym, START_DATE, END_DATE)
        elapsed = time.time() - t0
        total_inserted += inserted
        print(f"[{i:3d}/{n}] {sym}: {inserted}행  ({elapsed:.1f}s)")

        # 토큰 갱신 (1시간 만료 대비 — 50종목마다)
        if i % 50 == 0:
            token = get_access_token()
            print("  토큰 갱신")

    print(f"\n수집 완료: 총 {total_inserted:,}행")

    # 결과 확인
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) "
            "FROM credit_balance_kis"
        ).fetchone()
        print(f"DB 확인: {row[0]:,}행, {row[1]}종목, {row[2]}~{row[3]}")


if __name__ == "__main__":
    main()
