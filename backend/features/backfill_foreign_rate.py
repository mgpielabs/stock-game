#!/usr/bin/env python3
"""
backfill_foreign_rate.py — 외국인 보유비율(flows.foreign_net) 역산 백필.

flows.foreign_net 실측 앵커 → investor_trading_kis_detail.foreign_qty 누적합
÷ financials.shares_total(point-in-time) 로 빈 날짜 보유비율 추정.

갭 상한: 30 TD (검증 가능 구간 최대 7 TD, 30 TD는 보수적 임계치)
방어조건: [0,100] 범위 이탈 / 일간 변화 >10%p / 분할·증자 이벤트 / 중국주(9XXXX)
결과: flows 테이블 foreign_rate_source='estimated' 행 추가 (실측 'actual' 절대 덮어쓰지 않음)
"""
import argparse
import bisect
import csv
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
from data.db import get_connection  # noqa: E402

# ─── 파라미터 ────────────────────────────────────────────────────────────────
MAX_GAP_TD      = 30      # 갭 상한(거래일). 이보다 먼 날짜는 NaN 유지.
MAX_DAILY_DELTA = 10.0    # %p/일 — gap 내 단일 날짜가 이를 초과하면 전체 gap 거부
CHINESE_PREFIX  = "9"     # 코드 9XXXX: 중국 상장사 — 전면 제외

SPLIT_CSV    = BACKEND_DIR / "ml" / "dividend_split_correction_factors.csv"
IGNORE_JSON  = BACKEND_DIR / "ml" / ".dividend_split_ignore.json"

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


# ─── 스키마 마이그레이션 ──────────────────────────────────────────────────────
def migrate_schema(conn) -> None:
    """flows 테이블에 foreign_rate_source 컬럼 추가 (없는 경우)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(flows)")}
    if "foreign_rate_source" not in cols:
        conn.execute("ALTER TABLE flows ADD COLUMN foreign_rate_source TEXT")
        log.info("flows 테이블에 foreign_rate_source 컬럼 추가")
    # 기존 실측값(foreign_net IS NOT NULL) → 'actual' 마킹 (멱등)
    conn.execute(
        "UPDATE flows SET foreign_rate_source='actual'"
        " WHERE foreign_net IS NOT NULL AND foreign_rate_source IS NULL"
    )
    conn.commit()


# ─── 보조 데이터 로드 ─────────────────────────────────────────────────────────
def load_split_events() -> dict:
    """symbol → set(biz_year:int). 분할/증자 이벤트가 있는 종목-연도."""
    ignore_syms: set = set()
    ignore_pairs: set = set()

    if IGNORE_JSON.exists():
        raw = json.loads(IGNORE_JSON.read_text(encoding="utf-8"))
        items = raw if isinstance(raw, list) else raw.get("ignore", [])
        for item in items:
            if isinstance(item, dict):
                sym = str(item.get("symbol", "")).zfill(6)
                yr  = item.get("biz_year")
                if yr:
                    ignore_pairs.add((sym, int(yr)))
                else:
                    ignore_syms.add(sym)
            else:
                ignore_syms.add(str(item).zfill(6))

    events: dict = defaultdict(set)
    if SPLIT_CSV.exists():
        with open(SPLIT_CSV, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sym = row.get("symbol", "").strip().zfill(6)
                yr  = row.get("biz_year", "").strip()
                if not sym or not yr:
                    continue
                pair = (sym, int(yr))
                if sym in ignore_syms or pair in ignore_pairs:
                    continue
                events[sym].add(int(yr))
        log.info(
            "분할/증자 이벤트: %d건 (%d종목)",
            sum(len(v) for v in events.values()),
            len(events),
        )
    else:
        log.warning("SPLIT_CSV 없음 — 방어조건 3 (분할이벤트) 비활성화")

    return dict(events)


def load_shares(conn) -> dict:
    """symbol → {biz_year:int → shares_total:float}."""
    rows = conn.execute(
        "SELECT symbol, biz_year, shares_total FROM financials"
        " WHERE shares_total IS NOT NULL AND shares_total > 0"
    ).fetchall()
    out: dict = defaultdict(dict)
    for sym, yr, sh in rows:
        out[sym][int(yr)] = float(sh)
    log.info("shares_total 로드: %d종목", len(out))
    return dict(out)


def get_shares_pit(sym: str, date_str: str, shares_map: dict):
    """공시 3개월 지연 가정. 4월 이후 → 전년도, 4월 이전 → 2년 전."""
    yr = int(date_str[:4])
    mo = int(date_str[4:6])
    known_yr = (yr - 1) if mo >= 4 else (yr - 2)
    sym_map = shares_map.get(sym, {})
    # known_yr 이하의 가장 최근 연도
    avail = {y: s for y, s in sym_map.items() if y <= known_yr}
    if not avail:
        # 그보다 이른 시점이라도 available한 게 있으면 사용
        avail = sym_map
    if not avail:
        return None
    return avail[max(avail)]


# ─── 핵심: 종목별 갭 역산 ─────────────────────────────────────────────────────
def process_symbol(
    sym: str,
    anchors: list,       # [(date_str, rate), ...] sorted by date asc
    qty_map: dict,       # {date_str: foreign_qty(int)}
    trading_cal: list,   # 전역 거래일 캘린더 (sorted YYYYMMDD strings)
    shares_map: dict,
    split_events: dict,
) -> tuple:
    """
    Returns:
        writes: [(date_str, estimated_rate:float)]
        stats:  dict of rejection/skip counters
    """
    writes: list = []
    stats: dict = defaultdict(int)

    # 방어조건 4: 중국 상장사 전면 제외
    if sym.startswith(CHINESE_PREFIX):
        stats["rejected_chinese"] += 1
        return writes, stats

    for i in range(len(anchors) - 1):
        d_a, rate_a = anchors[i]      # 이른 앵커
        d_b, rate_b = anchors[i + 1]  # 늦은 앵커

        # 두 앵커 사이 거래일 (exclusive)
        lo = bisect.bisect_right(trading_cal, d_a)
        hi = bisect.bisect_left(trading_cal, d_b)
        gap_days = trading_cal[lo:hi]
        n_gap = len(gap_days)
        if n_gap == 0:
            continue  # 연속 거래일이라 gap 없음

        # 방어조건 3: 분할/증자 이벤트 (gap 구간에 해당 연도 포함 시 전체 거부)
        yr_a = int(d_a[:4])
        yr_b = int(d_b[:4])
        sym_events = split_events.get(sym, set())
        if any(yr in sym_events for yr in range(yr_a, yr_b + 1)):
            stats["rejected_split"] += n_gap
            continue

        # point-in-time shares_total
        shares = get_shares_pit(sym, d_a, shares_map)
        if not shares:
            stats["skipped_no_shares"] += n_gap
            continue

        # gap 날짜별 qty 수집
        gap_qtys = [qty_map.get(d) for d in gap_days]
        if any(q is None for q in gap_qtys):
            stats["skipped_no_qty"] += sum(1 for q in gap_qtys if q is None)
            continue

        # 방어조건 2: 일간 변화폭 체크 (gap 날짜 기준)
        daily_deltas = [float(q) / shares * 100.0 for q in gap_qtys]
        if any(abs(dd) > MAX_DAILY_DELTA for dd in daily_deltas):
            stats["rejected_large_delta"] += n_gap
            continue

        # ── 순방향 추정 (from d_a) ──────────────────────────────────────────
        # rate(ti) = rate(d_a) + Σ daily_delta(t_a+1 .. ti)
        cum_fwd = 0.0
        est_fwd: list = []
        for dd in daily_deltas:
            cum_fwd += dd
            est_fwd.append(rate_a + cum_fwd)

        # ── 역방향 추정 (from d_b) ──────────────────────────────────────────
        # rate(d_b) = rate(tk) + qty(d_b)/shares → rate(tk) = rate(d_b) - qty(d_b)/shares
        # 이후 rate(tk-j) = rate(tk) - Σ daily_delta(tk-j+1 .. tk)
        qty_db = qty_map.get(d_b)
        bwd_ok = qty_db is not None
        if bwd_ok:
            cum_bwd = float(qty_db) / shares * 100.0
            est_bwd: list = [None] * n_gap
            for j in range(n_gap - 1, -1, -1):
                est_bwd[j] = rate_b - cum_bwd
                cum_bwd += daily_deltas[j]
        else:
            est_bwd = [None] * n_gap

        # ── 날짜별: 더 가까운 앵커 선택 (MAX_GAP_TD 이내만) ─────────────────
        for j, d in enumerate(gap_days):
            dist_a = j + 1          # d_a로부터 거래일 거리 (1-based)
            dist_b = n_gap - j      # d_b로부터 거래일 거리 (1-based)

            can_use_fwd = dist_a <= MAX_GAP_TD
            can_use_bwd = dist_b <= MAX_GAP_TD and bwd_ok and est_bwd[j] is not None

            if can_use_fwd and can_use_bwd:
                est = est_fwd[j] if dist_a <= dist_b else est_bwd[j]
            elif can_use_fwd:
                est = est_fwd[j]
            elif can_use_bwd:
                est = est_bwd[j]
            else:
                stats["skipped_too_far"] += 1
                continue

            # 방어조건 1: 범위 [0, 100] 체크
            if est < 0.0 or est > 100.0:
                stats["rejected_out_of_range"] += 1
                continue

            writes.append((d, round(est, 4)))
            stats["estimated"] += 1

    return writes, stats


# ─── DB 쓰기 ─────────────────────────────────────────────────────────────────
UPSERT_SQL = """
    INSERT INTO flows (symbol, date, foreign_net, foreign_rate_source)
    VALUES (?, ?, ?, 'estimated')
    ON CONFLICT(symbol, date) DO UPDATE SET
        foreign_net          = excluded.foreign_net,
        foreign_rate_source  = 'estimated'
    WHERE flows.foreign_net IS NULL
"""


def write_estimates(conn, sym: str, writes: list) -> None:
    if not writes:
        return
    conn.executemany(UPSERT_SQL, [(sym, d, v) for d, v in writes])


# ─── 리포트 ──────────────────────────────────────────────────────────────────
def print_report(conn, global_stats: dict, dry_run: bool) -> None:
    print()
    print("=" * 68)
    print(f"{'외국인비율 역산 백필 결과':^68}")
    print("=" * 68)

    # foreign_rate_source 컬럼 존재 여부 확인
    cols = {r[1] for r in conn.execute("PRAGMA table_info(flows)")}
    has_src_col = "foreign_rate_source" in cols

    print("\n[flows 행 분포]")
    if has_src_col:
        rows = conn.execute(
            "SELECT COALESCE(foreign_rate_source,'NULL'), COUNT(*) FROM flows GROUP BY 1"
        ).fetchall()
        total = sum(c for _, c in rows)
        for src, cnt in sorted(rows):
            pct = cnt / total * 100 if total else 0
            print(f"  {src:>12}: {cnt:>9,} 행  ({pct:.1f}%)")
        print(f"  {'합계':>12}: {total:>9,} 행")
    else:
        total = conn.execute("SELECT COUNT(*) FROM flows WHERE foreign_net IS NOT NULL").fetchone()[0]
        print(f"  {'actual':>12}: {total:>9,} 행  (100.0%)")
        print(f"  {'estimated':>12}: {'[dry-run — 미적용]':>9}")
        print(f"  {'합계':>12}: {total:>9,} 행")

    if global_stats:
        print("\n[방어조건 거부/스킵 통계]")
        for key, cnt in sorted(global_stats.items()):
            print(f"  {key:>30}: {cnt:>9,}")

    print("\n[estimated 비율 상위 20 종목]")
    if has_src_col:
        top = conn.execute("""
            SELECT symbol,
                   SUM(CASE WHEN foreign_rate_source='actual'    THEN 1 ELSE 0 END) AS n_actual,
                   SUM(CASE WHEN foreign_rate_source='estimated' THEN 1 ELSE 0 END) AS n_est
            FROM flows
            WHERE foreign_net IS NOT NULL
            GROUP BY symbol
            HAVING n_est > 0
            ORDER BY CAST(n_est AS REAL) / (n_actual + n_est) DESC
            LIMIT 20
        """).fetchall()
        if top:
            print(f"  {'symbol':>8}  {'actual':>7}  {'estimated':>9}  {'est비율':>7}")
            for sym, na, ne in top:
                ratio = ne / (na + ne) * 100 if (na + ne) else 0
                print(f"  {sym:>8}  {na:>7}  {ne:>9}  {ratio:>6.1f}%")
        else:
            print("  (estimated 행 없음)")
    else:
        print("  (dry-run — 적용 전 상태)")

    if dry_run:
        print("\n※ --dry-run 모드: DB에 실제 쓰기 없음.")
    print("=" * 68)


# ─── 진입점 ──────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="외국인 보유비율 역산 백필")
    ap.add_argument("--dry-run", action="store_true", help="DB 변경 없이 결과만 출력")
    ap.add_argument("--symbol", help="테스트: 특정 종목코드만 처리")
    ap.add_argument("--status", action="store_true", help="현재 flows 분포만 출력 후 종료")
    args = ap.parse_args()

    conn = get_connection()
    conn.execute("PRAGMA journal_mode=WAL")

    if args.status:
        print_report(conn, {}, dry_run=True)
        conn.close()
        return

    # 스키마 마이그레이션
    if not args.dry_run:
        migrate_schema(conn)

    # 보조 데이터 로드
    log.info("보조 데이터 로드 중...")
    split_events = load_split_events()
    shares_map   = load_shares(conn)

    # 전역 거래일 캘린더
    log.info("거래일 캘린더 로드 중...")
    trading_cal: list = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM investor_trading_kis_detail ORDER BY date"
        ).fetchall()
    ]
    log.info("  %d 거래일 (%s ~ %s)", len(trading_cal), trading_cal[0], trading_cal[-1])

    # flows 앵커 로드 — 실측값('actual')만 사용, 추정값은 앵커 제외(오차 누적 방지)
    log.info("flows 앵커 로드 중...")
    q = ("SELECT symbol, date, foreign_net FROM flows"
         " WHERE foreign_net IS NOT NULL"
         " AND COALESCE(foreign_rate_source,'actual') = 'actual'")
    if args.symbol:
        q += " AND symbol = ?"
        flows_rows = conn.execute(q, (args.symbol,)).fetchall()
    else:
        flows_rows = conn.execute(q).fetchall()

    anchors_by_sym: dict = defaultdict(list)
    for sym, dt, rate in flows_rows:
        anchors_by_sym[sym].append((dt, float(rate)))
    # 날짜 오름차순 정렬 보장
    for sym in anchors_by_sym:
        anchors_by_sym[sym].sort(key=lambda x: x[0])
    log.info("  %d 종목, %d 앵커", len(anchors_by_sym), len(flows_rows))

    # KIS qty 로드 (flows에 있는 종목만)
    syms_list = list(anchors_by_sym.keys())
    log.info("KIS foreign_qty 로드 중 (%d 종목)...", len(syms_list))
    CHUNK = 500
    kis_by_sym: dict = defaultdict(dict)
    for start in range(0, len(syms_list), CHUNK):
        batch = syms_list[start:start + CHUNK]
        ph = ",".join("?" * len(batch))
        kis_rows = conn.execute(
            f"SELECT symbol, date, foreign_qty"
            f" FROM investor_trading_kis_detail"
            f" WHERE symbol IN ({ph}) AND foreign_qty IS NOT NULL",
            batch,
        ).fetchall()
        for sym, dt, qty in kis_rows:
            kis_by_sym[sym][dt] = qty
    log.info("  %d 종목 qty 로드 완료", len(kis_by_sym))

    # 종목별 처리
    global_stats: dict = defaultdict(int)
    total_writes = 0
    n_syms = len(anchors_by_sym)
    commit_every = 1000  # 종목 단위 커밋 간격

    log.info("종목별 역산 처리 시작 (%d 종목)...", n_syms)
    for idx, (sym, anchors) in enumerate(anchors_by_sym.items(), 1):
        writes, stats = process_symbol(
            sym=sym,
            anchors=anchors,
            qty_map=kis_by_sym.get(sym, {}),
            trading_cal=trading_cal,
            shares_map=shares_map,
            split_events=split_events,
        )

        if not args.dry_run:
            write_estimates(conn, sym, writes)

        for k, v in stats.items():
            global_stats[k] += v
        total_writes += len(writes)

        if idx % commit_every == 0:
            if not args.dry_run:
                conn.commit()
            log.info("  %d/%d 처리 중... (역산 누적: %d)", idx, n_syms, total_writes)

    if not args.dry_run:
        conn.commit()

    log.info("처리 완료: 역산 셀 %d개", total_writes)
    print_report(conn, global_stats, args.dry_run)
    conn.close()


if __name__ == "__main__":
    main()
