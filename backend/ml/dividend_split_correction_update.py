"""
일일 파이프라인용: 배당 분할/병합 보정계수(dividend_split_correction_factors.csv) 증분 갱신
(2026-06-26, daily_pipeline.py 8단계에서 호출).

dividend_split_correction.py의 전체 재계산(전 종목 DART 재조회)은 무겁고 매일 돌릴 필요
없음 — 이 스크립트는 가격×배당 비율로 "분할/병합 의심" 신호를 가볍게 재탐지(DART 호출
없음)한 뒤, 기존 factors CSV와 달라진(신규 또는 갱신 필요) 종목만 추려서 DART로 확증을
"최소한으로만" 시도한다.

흐름:
  1. dividends/prices 테이블에서 가격×배당 비율 재계산 (DART 호출 없음,
     dividend_split_correction.identify_candidates() 재사용).
  2. 기존 factors CSV에 있는 (symbol, biz_year)는 ratio_empirical이 ±15% 이내로 같으면
     스킵(이미 올바르게 보정돼 있음). 없거나 ratio가 그만큼 바뀌었으면 "갱신 필요"로 분류
     (= 새 분할/병합이 발생했거나 처음 발견된 경우).
  3. "갱신 필요" 종목만 DART로 분할/병합/증자/감자 결정 공시를 찾아 원문에서 정확한 배수를
     파싱 시도 → 실패하거나 경험적 비율과 20% 넘게 어긋나면 경험적 스냅값으로 대체
     (dividend_split_correction.py와 동일한 안전장치).
  4. factors CSV를 갱신(기존 행 update + 신규 행 append)하고 저장.

항상 exit 0(비치명적) — 실패해도 daily_pipeline.py 전체를 막지 않음.
실행: python dividend_split_correction_update.py [--dry-run]
로그: backend/dividend_split_update.log
"""

import argparse
import logging
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000040)
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
import sqlite3

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from dividend_split_correction import (  # noqa: E402
    identify_candidates, snap_to_multiple, fetch_decision_events, parse_event_ratio,
    load_corp_code_map_readonly, load_split_ignore_symbols, DB_PATH, FACTORS_CSV,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "dividend_split_update.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

RATIO_CHANGE_THRESHOLD = 0.15  # 기존 factors와 이 이상 달라지면 "재발생/갱신 필요"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="감지만 하고 CSV는 갱신 안 함")
    args = parser.parse_args()

    try:
        t0 = time.time()
        conn = sqlite3.connect(DB_PATH)

        candidates = identify_candidates(conn)
        big = candidates[(candidates["ratio"] < 0.7) | (candidates["ratio"] > 1.3)].copy()
        snapped = big["ratio"].apply(lambda r: snap_to_multiple(r, tol=0.10))
        big["matched_multiple"] = snapped.apply(lambda x: x[0])
        big = big.dropna(subset=["matched_multiple"])
        ignore_symbols = load_split_ignore_symbols()
        big = big[~big["symbol"].isin(ignore_symbols)]
        log.info("가격×배당 비율 재탐지: %d건(분할/병합 의심, 무시목록 %d종목 제외)", len(big), len(ignore_symbols))

        if FACTORS_CSV.exists():
            existing = pd.read_csv(FACTORS_CSV, dtype={"symbol": str})
            existing["symbol"] = existing["symbol"].str.zfill(6)
            n_before = len(existing)
            existing = existing[~existing["symbol"].isin(ignore_symbols)]
            if n_before != len(existing):
                log.info("기존 factors에서 무시목록 종목 %d건 제거", n_before - len(existing))
        else:
            existing = pd.DataFrame(columns=[
                "symbol", "biz_year", "K_cumulative", "correction_factor",
                "source", "n_events", "ratio_empirical", "matched_multiple_empirical",
            ])
        existing_keyed = existing.set_index(["symbol", "biz_year"]) if len(existing) else existing

        needs_update = []
        for _, row in big.iterrows():
            key = (row["symbol"], row["biz_year"])
            if len(existing_keyed) and key in existing_keyed.index:
                old_ratio = existing_keyed.loc[key, "ratio_empirical"]
                if isinstance(old_ratio, pd.Series):
                    old_ratio = old_ratio.iloc[0]
                if old_ratio and abs(row["ratio"] - old_ratio) / old_ratio <= RATIO_CHANGE_THRESHOLD:
                    continue  # 이미 반영돼 있고 안 바뀜 — 스킵
            needs_update.append(row)

        if not needs_update:
            log.info("신규/갱신 필요 종목 없음 — 보정계수 변경 없음 (%.0fs)", time.time() - t0)
            conn.close()
            return 0

        upd_df = pd.DataFrame(needs_update)
        new_symbols = sorted(upd_df["symbol"].unique())
        log.info("신규/갱신 필요: %d건, %d종목 — DART 확증 조회 시작", len(upd_df), len(new_symbols))

        if args.dry_run:
            log.info("[DRY-RUN] DART 조회/CSV 갱신 건너뜀. 대상: %s", new_symbols)
            conn.close()
            return 0

        cmap = load_corp_code_map_readonly()
        event_ratios = {}
        for sym in new_symbols:
            corp = cmap.get(sym)
            if not corp:
                continue
            events = fetch_decision_events(sym, corp)
            parsed = []
            for ev in events:
                k = parse_event_ratio(ev["report_nm"], ev["rcept_no"])
                if k and k > 0:
                    parsed.append({"rcept_dt": ev["rcept_dt"], "report_nm": ev["report_nm"], "K": k})
                time.sleep(0.2)
            if parsed:
                event_ratios[sym] = parsed

        new_rows = []
        for _, row in upd_df.iterrows():
            sym, biz, settle = row["symbol"], row["biz_year"], row["settlement_date"]
            applicable = [e for e in event_ratios.get(sym, []) if not settle or e["rcept_dt"] > settle]
            if applicable:
                k_cum = 1.0
                for e in applicable:
                    k_cum *= e["K"]
                if row["ratio"] and abs(k_cum - row["ratio"]) / row["ratio"] <= 0.20:
                    new_rows.append({
                        "symbol": sym, "biz_year": biz, "K_cumulative": round(k_cum, 4),
                        "correction_factor": round(1 / k_cum, 6),
                        "source": "dart_document", "n_events": len(applicable),
                        "ratio_empirical": row["ratio"], "matched_multiple_empirical": row["matched_multiple"],
                    })
                    continue
            new_rows.append({
                "symbol": sym, "biz_year": biz, "K_cumulative": row["matched_multiple"],
                "correction_factor": round(1 / row["matched_multiple"], 6),
                "source": "empirical_fallback", "n_events": 0,
                "ratio_empirical": row["ratio"], "matched_multiple_empirical": row["matched_multiple"],
            })

        new_df = pd.DataFrame(new_rows)
        if len(existing):
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["symbol", "biz_year"], keep="last")
        else:
            combined = new_df
        combined["symbol"] = combined["symbol"].astype(str).str.zfill(6)
        combined.to_csv(FACTORS_CSV, index=False, encoding="utf-8-sig")

        n_doc = (new_df["source"] == "dart_document").sum()
        n_emp = (new_df["source"] == "empirical_fallback").sum()
        log.info(
            "보정계수 갱신 완료: 신규/갱신 %d건(DART원문 %d / 경험적추정 %d), 전체 %d건 (%.0fs)",
            len(new_df), n_doc, n_emp, len(combined), time.time() - t0,
        )
        conn.close()
        return 0
    except Exception as exc:
        log.error("배당 분할보정 갱신 중 예외(비치명적): %s", exc)
        return 0


if __name__ == "__main__":
    sys.exit(main())
