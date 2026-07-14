"""
features.per / features.pbr point-in-time 재계산 (look-ahead bias 수정, 2026-06-27)

배경: load_fundamentals_latest()가 date 필터 없이 "가장 최근 PER/PBR"을 모든 과거
날짜에 동일하게 broadcast하던 버그(per는 0% SHAP라 무해, pbr은 #3/9.7% SHAP라 모델
평가 전체를 오염시킴 — CLAUDE.md "신호 연구" 섹션의 per_validation.py/pbr_validation.py와
동일 방식으로 이미 검증된 point-in-time 로직을 재사용).

이 스크립트는 새로 피처 전체를 재계산하지 않고(비용 큼), 이미 저장된 features 테이블의
per/pbr 두 컬럼만 point-in-time 값으로 UPDATE한다. pipeline.py도 이번에 같은 로직
(load_per_pbr_pit)을 쓰도록 수정되어, 이후 신규/증분 계산분은 자동으로 point-in-time이 됨.

실행: python fix_per_pbr_pit.py
"""
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from db_features import get_connection, load_per_pbr_pit


def main():
    with get_connection() as conn:
        symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM features ORDER BY symbol").fetchall()]

    print(f"대상 종목 수: {len(symbols)}")
    t0 = time.time()
    n_updated_rows = 0

    for i, symbol in enumerate(symbols, 1):
        with get_connection() as conn:
            feat_dates = pd.read_sql_query(
                "SELECT date FROM features WHERE symbol=? ORDER BY date", conn, params=[symbol]
            )["date"]
            if feat_dates.empty:
                continue
            prices = pd.read_sql_query(
                "SELECT date, close FROM prices WHERE symbol=? ORDER BY date", conn, params=[symbol]
            )

        close_series = prices.set_index("date")["close"].reindex(feat_dates).astype(float)
        close_series.index = feat_dates.values

        fund_pit = load_per_pbr_pit(symbol, feat_dates, close_series)

        rows = [
            (
                None if pd.isna(per) else float(per),
                None if pd.isna(pbr) else float(pbr),
                symbol,
                d,
            )
            for d, per, pbr in zip(feat_dates, fund_pit["per"], fund_pit["pbr"])
        ]

        with get_connection() as conn:
            conn.executemany(
                "UPDATE features SET per=?, pbr=? WHERE symbol=? AND date=?", rows
            )
            conn.commit()

        n_updated_rows += len(rows)

        if i % 200 == 0 or i == len(symbols):
            elapsed = time.time() - t0
            print(f"[{i}/{len(symbols)}] {symbol} 완료 | 누적 {n_updated_rows}행 | {elapsed:.1f}초")

    print(f"완료: {len(symbols)}종목, {n_updated_rows}행 업데이트, {time.time()-t0:.1f}초")


if __name__ == "__main__":
    main()
