#!/usr/bin/env python3
"""
add_frgn_features.py
수급 신호 스캔 생존 조합 2개를 features 테이블 피처로 생성.

frgn_norm_cum10 : 최근 10거래일 외국인 순매수 대금 / 시가총액 (누적합, 정규화)
frgn_streak3    : 3거래일 연속 외국인 순매수 boolean (1.0 / 0.0 / NaN)

소스: investor_trading_kis_detail.foreign_value (2020-12-21~, PIT 보장)
시가총액 = investor_trading_kis_detail.close × financials.shares_total (PIT, 3개월 지연 가정)
Point-in-time: rolling(10) 자체가 D 이전 데이터만 사용 — 미래 누수 없음.
"""
import sys
import sqlite3
import bisect
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = Path(__file__).resolve().parent.parent / "data" / "stocks.db"

BATCH_UPDATE = 50_000   # 커밋 간격


# ─── 공통 유틸 ───────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ─── 스키마 마이그레이션 ─────────────────────────────────────────────────────

def add_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    added = []
    for col, typ in [("frgn_norm_cum10", "REAL"), ("frgn_streak3", "REAL")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE features ADD COLUMN {col} {typ}")
            added.append(col)
    conn.commit()
    print(f"컬럼 추가: {added if added else '이미 존재'}")


# ─── PIT shares 로드 ─────────────────────────────────────────────────────────

def load_shares_pit(conn) -> dict:
    """symbol → [(known_from_YYYYMMDD, shares_total), ...] sorted ASC"""
    rows = conn.execute("""
        SELECT symbol, biz_year, shares_total
        FROM financials
        WHERE shares_total IS NOT NULL AND shares_total > 0
        ORDER BY symbol, biz_year
    """).fetchall()

    out = defaultdict(list)
    for sym, biz_year, sh in rows:
        known_from = f"{int(biz_year) + 1}0401"   # 3개월 공시지연 가정
        out[str(sym).zfill(6)].append((known_from, float(sh)))
    for sym in out:
        out[sym].sort()
    return dict(out)


def _get_shares_at(sym: str, date_str: str, shares_map: dict):
    """날짜 date_str에서 알 수 있는 가장 최신 shares_total (PIT)"""
    entries = shares_map.get(sym, [])
    if not entries:
        return np.nan
    known_froms = [e[0] for e in entries]
    idx = bisect.bisect_right(known_froms, date_str) - 1
    if idx < 0:
        return entries[0][1]   # 공시 전이라도 최초값 사용 (근사)
    return entries[idx][1]


# ─── 종목별 피처 계산 ────────────────────────────────────────────────────────

def compute_for_symbol(sym: str, grp: pd.DataFrame, shares_map: dict) -> pd.DataFrame:
    """
    grp: investor_trading_kis_detail rows for one symbol, sorted by date ASC
         columns: date, foreign_value, close

    Returns: DataFrame[date, frgn_norm_cum10, frgn_streak3]
    """
    fv    = grp["foreign_value"].astype(float).values
    close = grp["close"].astype(float).values
    dates = grp["date"].values

    # PIT shares per row (벡터화: 1249 dates 이하라 list comprehension 빠름)
    sym6 = str(sym).zfill(6)
    sh = np.fromiter(
        (_get_shares_at(sym6, d, shares_map) for d in dates),
        dtype=float, count=len(dates),
    )
    mktcap = close * sh
    mktcap[mktcap == 0] = np.nan

    # frgn_norm_cum10: rolling 10일 누적합 / 시가총액
    # pandas Series rolling은 자동으로 과거→현재 방향, PIT 보장
    fv_s = pd.Series(fv)
    cum10 = fv_s.rolling(10, min_periods=5).sum().values
    frgn_norm_cum10 = cum10 / mktcap

    # frgn_streak3: 3일 연속 양수 → 1.0
    pos_s = (fv_s > 0).astype(float)
    streak3 = pos_s.rolling(3, min_periods=3).min().values  # 1.0 / 0.0 / NaN

    return pd.DataFrame({
        "date":            dates,
        "frgn_norm_cum10": frgn_norm_cum10,
        "frgn_streak3":    streak3,
    })


# ─── 메인 ────────────────────────────────────────────────────────────────────

def main():
    print("=== frgn_norm_cum10 / frgn_streak3 피처 생성 ===\n")

    conn = get_conn()
    add_columns(conn)

    # ── 1. 보조 데이터 로드 ──────────────────────────────────────────────────
    print("shares PIT 로딩...", flush=True)
    shares_map = load_shares_pit(conn)
    print(f"  {len(shares_map):,} 종목\n")

    print("investor_trading_kis_detail 로딩...", flush=True)
    flow = pd.read_sql_query("""
        SELECT symbol, date,
               COALESCE(foreign_value, 0) AS foreign_value,
               close
        FROM investor_trading_kis_detail
        WHERE symbol NOT LIKE '9%'
        ORDER BY symbol, date
    """, conn)
    print(f"  {len(flow):,} 행, {flow['symbol'].nunique():,} 종목")
    print(f"  날짜범위: {flow['date'].min()} ~ {flow['date'].max()}\n")

    # features 테이블 종목 목록
    feat_syms = set(
        r[0] for r in conn.execute("SELECT DISTINCT symbol FROM features").fetchall()
    )
    common_syms = set(flow["symbol"]) & feat_syms
    print(f"features 종목: {len(feat_syms):,}  |  kis_detail 종목: {flow['symbol'].nunique():,}  |  공통: {len(common_syms):,}\n")

    # ── 2. 종목별 피처 계산 ──────────────────────────────────────────────────
    print("종목별 피처 계산 중...", flush=True)
    records = []   # list of (frgn_norm_cum10, frgn_streak3, symbol, date)

    done = 0
    for sym, grp in flow.groupby("symbol", sort=False):
        if sym not in common_syms:
            continue

        result = compute_for_symbol(sym, grp.reset_index(drop=True), shares_map)

        # NaN 행은 features UPDATE 시 NULL 처리 (skip하지 않음 — NULL도 의미 있음)
        for row in result.itertuples(index=False):
            cum_val  = None if np.isnan(row.frgn_norm_cum10) else float(row.frgn_norm_cum10)
            str_val  = None if np.isnan(row.frgn_streak3)    else float(row.frgn_streak3)
            records.append((cum_val, str_val, sym, row.date))

        done += 1
        if done % 500 == 0:
            print(f"  {done:,} / {len(common_syms):,} 종목...", flush=True)

    print(f"  계산 완료: {len(records):,} 레코드\n")

    # ── 3. features 테이블 일괄 UPDATE ───────────────────────────────────────
    print("features 테이블 업데이트 중...", flush=True)
    n_done = 0
    for start in range(0, len(records), BATCH_UPDATE):
        batch = records[start : start + BATCH_UPDATE]
        conn.executemany("""
            UPDATE features
            SET frgn_norm_cum10 = ?,
                frgn_streak3    = ?
            WHERE symbol = ? AND date = ?
        """, batch)
        conn.commit()
        n_done += len(batch)
        print(f"  {n_done:,} / {len(records):,} 업데이트...", flush=True)

    print(f"  완료: {n_done:,} 레코드 처리\n")

    # ── 4. 커버리지 리포트 ───────────────────────────────────────────────────
    print("=" * 72)
    print("커버리지 리포트 (연도별)")
    print("=" * 72)
    rows = conn.execute("""
        SELECT
            substr(date,1,4)                                         AS yr,
            COUNT(*)                                                  AS total,
            SUM(CASE WHEN frgn_norm_cum10 IS NOT NULL THEN 1 ELSE 0 END) AS cum_nn,
            SUM(CASE WHEN frgn_streak3    IS NOT NULL THEN 1 ELSE 0 END) AS str_nn,
            -- 기존 foreign_rate 비교
            SUM(CASE WHEN foreign_rate    IS NOT NULL THEN 1 ELSE 0 END) AS fr_nn
        FROM features
        GROUP BY yr
        ORDER BY yr
    """).fetchall()

    print(f"{'연도':>5}  {'전체행':>10}  {'cum10 비NULL':>12}  {'cum10%':>7}  {'streak3 비NULL':>14}  {'streak3%':>8}  {'기존 foreign_rate%':>18}")
    t_all = t_cum = t_str = t_fr = 0
    for yr, tot, cum, s3, fr in rows:
        t_all += tot; t_cum += cum; t_str += s3; t_fr += fr
        print(f"  {yr}  {tot:>10,}  {cum:>12,}  {cum/tot*100:>6.1f}%  {s3:>14,}  {s3/tot*100:>7.1f}%  {fr/tot*100:>17.1f}%")
    print("-" * 72)
    print(f"{'전체':>5}  {t_all:>10,}  {t_cum:>12,}  {t_cum/t_all*100:>6.1f}%  {t_str:>14,}  {t_str/t_all*100:>7.1f}%  {t_fr/t_all*100:>17.1f}%")
    print()

    # 비율이 낮으면 이유 분석
    print("커버리지 낮은 이유 분석:")
    r = conn.execute("""
        SELECT
            SUM(CASE WHEN f.date < '20210104' THEN 1 ELSE 0 END) AS before_kis,
            SUM(CASE WHEN k.symbol IS NULL     THEN 1 ELSE 0 END) AS no_kis_sym
        FROM features f
        LEFT JOIN (SELECT DISTINCT symbol FROM investor_trading_kis_detail) k
              ON f.symbol = k.symbol
    """).fetchone()
    print(f"  kis_detail 시작일(2020-12-21) 이전 features 행: {r[0]:,}")
    print(f"  kis_detail에 없는 종목의 features 행:           {r[1]:,}")

    conn.close()
    print("\n완료.")


if __name__ == "__main__":
    main()
