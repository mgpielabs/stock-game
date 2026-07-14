"""
회귀 테스트: look-ahead bias 재유입 방지

features.per / features.pbr이 point-in-time 기준으로 계산됐는지 확인.
load_per_pbr_pit()로 재계산한 값과 features 테이블 저장값이 일치해야 함.
불일치 = look-ahead bias 재유입 또는 파이프라인 불일치.

60d 팩터 5종도 동일하게 검증:
  예측 시점의 _compute_60d_factors_pit()가 반환하는 값이
  features 테이블에 저장된 값과 동일해야 함.
  (60d 팩터는 features 테이블에 저장하지 않으므로 이 부분은 생략
   — 대신 compute_factors_for_date()의 내부 일관성만 스모크 테스트)
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BACKEND = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND / "features"))
sys.path.insert(0, str(BACKEND / "data"))
sys.path.insert(0, str(BACKEND / "ml"))

DB_PATH = BACKEND / "data" / "stocks.db"

SAMPLE_N = 10       # 검사할 (symbol, date) 쌍 수
RTOL     = 1e-4     # 상대 허용 오차 (부동소수점 반올림 차이 허용)


def _biz_year_pit(date_int) -> int:
    """features.date(YYYYMMDD int 또는 str) → biz_year_pit."""
    d = int(str(date_int).replace("-", ""))  # TEXT '20230927' 또는 int 20230927 모두 대응
    year  = d // 10000
    month = (d % 10000) // 100
    return year - 1 if month >= 4 else year - 2


@pytest.mark.skipif(
    not DB_PATH.exists(), reason="stocks.db 없음 — CI/오프라인 환경"
)
def test_per_no_lookahead(conn):
    """features.per가 PIT 기준(dividends.eps ÷ 종가)과 일치하는지 확인."""
    # features.per, features.pbr이 non-null인 샘플
    rows = conn.execute(
        """
        SELECT f.symbol, f.date, f.per, p.close
        FROM features f
        JOIN prices p ON p.symbol=f.symbol AND p.date=CAST(f.date AS TEXT)
        WHERE f.per IS NOT NULL
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (SAMPLE_N,),
    ).fetchall()

    if not rows:
        pytest.skip("features.per 데이터 없음")

    mismatches = []
    for symbol, date_int, stored_per, close in rows:
        byz = _biz_year_pit(date_int)
        # dividends 테이블에서 PIT EPS 가져오기
        row = conn.execute(
            "SELECT eps FROM dividends WHERE symbol=? AND biz_year=? AND eps IS NOT NULL",
            (str(symbol).zfill(6), byz),
        ).fetchone()
        if row is None:
            continue  # PIT EPS 없으면 features.per도 NULL이어야 함 (검사 스킵)

        eps = row[0]
        if eps == 0 or close == 0:
            continue

        expected_per = close / eps
        if not np.isclose(stored_per, expected_per, rtol=RTOL, atol=1.0):
            mismatches.append((symbol, date_int, stored_per, expected_per))

    assert not mismatches, (
        f"look-ahead 의심 {len(mismatches)}건 발견:\n" +
        "\n".join(f"  {sym} {dt}: stored={s:.2f} expected={e:.2f}"
                  for sym, dt, s, e in mismatches[:5])
    )


@pytest.mark.skipif(
    not DB_PATH.exists(), reason="stocks.db 없음"
)
def test_pbr_no_lookahead(conn):
    """features.pbr이 PIT 기준(financials.bps ÷ 종가)과 일치하는지 확인."""
    rows = conn.execute(
        """
        SELECT f.symbol, f.date, f.pbr, p.close
        FROM features f
        JOIN prices p ON p.symbol=f.symbol AND p.date=CAST(f.date AS TEXT)
        WHERE f.pbr IS NOT NULL
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (SAMPLE_N,),
    ).fetchall()

    if not rows:
        pytest.skip("features.pbr 데이터 없음")

    mismatches = []
    for symbol, date_int, stored_pbr, close in rows:
        byz = _biz_year_pit(date_int)
        row = conn.execute(
            "SELECT bps FROM financials WHERE symbol=? AND biz_year=? AND bps IS NOT NULL",
            (str(symbol).zfill(6), byz),
        ).fetchone()
        if row is None:
            continue

        bps = row[0]
        if bps == 0 or close == 0:
            continue

        expected_pbr = close / bps
        if not np.isclose(stored_pbr, expected_pbr, rtol=RTOL, atol=0.01):
            mismatches.append((symbol, date_int, stored_pbr, expected_pbr))

    assert not mismatches, (
        f"look-ahead 의심 {len(mismatches)}건 발견:\n" +
        "\n".join(f"  {sym} {dt}: stored={s:.4f} expected={e:.4f}"
                  for sym, dt, s, e in mismatches[:5])
    )


@pytest.mark.skipif(
    not DB_PATH.exists(), reason="stocks.db 없음"
)
def test_60d_factors_smoke(conn):
    """factors_60d.compute_factors_for_date()가 동일 날짜에 재호출해도 같은 값을 반환하는지."""
    from factors_60d import compute_factors_for_date

    # 최신 features 날짜 가져오기
    row = conn.execute("SELECT MAX(date) FROM features").fetchone()
    if not row or not row[0]:
        pytest.skip("features 데이터 없음")
    date_str = str(row[0])

    result1 = compute_factors_for_date(date_str, conn)
    result2 = compute_factors_for_date(date_str, conn)

    if result1.empty:
        pytest.skip("60d 팩터 결과 없음")

    pd.testing.assert_frame_equal(
        result1.reset_index(drop=True),
        result2.reset_index(drop=True),
        check_exact=False,
        rtol=1e-6,
    )
