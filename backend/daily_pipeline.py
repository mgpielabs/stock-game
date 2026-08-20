"""
일일 증분 파이프라인: 오늘치 데이터 수집 → 피처 업데이트 → 서버 재시작
평일 16:30 자동 실행 (Task Scheduler 또는 수동 실행 모두 가능)

실행법:
  python daily_pipeline.py            # 바로 실행
  python daily_pipeline.py --dry-run  # 실행 없이 단계만 출력

소요 시간: 약 10~20분 (종목 수에 따라 다름)
로그: backend/pipeline_daily.log
"""

import argparse
import ctypes
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

# 콘솔이 cp949인 환경(.bat 더블클릭 등)에서 UnicodeEncodeError 방지 — 자식 프로세스들에도
# 동일하게 적용된 패턴(collector.py 등 참고, 2026-06-23)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT     = Path(__file__).parent
DATA_DIR = ROOT / "data"
FEAT_DIR = ROOT / "features"
SRV_DIR  = ROOT / "server"
SCRIPTS_DIR = ROOT / "scripts"
ML_DIR   = ROOT / "ml"
LOG_FILE = ROOT / "pipeline_daily.log"
PYTHON   = ROOT / ".venv" / "Scripts" / "python.exe"

# 중복 실행 방지용 락 — 작업 스케줄러/수동 .bat/인앱 버튼(POST /api/update) 어느 경로로
# 시작했든 같은 락을 공유해서 동시 실행을 막음(2026-06-23)
LOCK_FILE   = ROOT / "daily_pipeline.pid"
STATUS_FILE = ROOT / "pipeline_status.json"

# 콘솔 창 숨김 + CPU 우선순위 최저(게임 등 포그라운드 작업에 영향 안 주도록 유휴 시간에만 CPU 사용)
# — 데이터 수집/피처 계산(run())에만 적용. 서버는 SERVER_FLAGS로 별도 처리(정상 우선순위 유지)
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.IDLE_PRIORITY_CLASS

# 서버 프로세스용: 콘솔 창만 숨기고 우선순위는 정상 유지(실시간 API 응답 지연 방지)
SERVER_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.NORMAL_PRIORITY_CLASS

# SetPriorityClass 전용 값 — CreateProcess의 creationflags로는 설정 불가, 프로세스 시작 후
# 핸들에 별도로 적용해야 함. CPU뿐 아니라 디스크 I/O·메모리 우선순위까지 백그라운드 수준으로 낮춤
PROCESS_SET_INFORMATION       = 0x0200
PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000


# ── 파이프라인 진행 상태 추적 ─────────────────────────────────────────────────
# 외부 실행(작업 스케줄러 .vbs)이어도 프론트엔드가 단계/경과시간/하트비트를 볼 수 있도록
# 상태를 JSON 파일에 기록. Unix timestamp 사용 — Python(KST)과 JS(UTC) 모두 epoch 기준.
_pl_started_at:       float = 0.0
_pl_current_stage:    int   = 0
_pl_current_stage_name: str = ""
_pl_last_write_t:     float = 0.0
_pl_finished_ok:      bool  = False


def _write_pipeline_status(stage: int, stage_name: str, status: str = "running") -> None:
    """stage/heartbeat/started_at을 STATUS_FILE에 원자적으로 기록."""
    global _pl_last_write_t
    try:
        STATUS_FILE.write_text(
            json.dumps({
                "started_at":         _pl_started_at,
                "current_stage":      stage,
                "current_stage_name": stage_name,
                "total_stages":       11,
                "last_heartbeat":     time.time(),
                "status":             status,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        _pl_last_write_t = time.time()
    except Exception:
        pass


def _touch_heartbeat() -> None:
    """장시간 단계에서도 하트비트를 30초마다 갱신 — 프론트 '응답 없음' 오탐 방지."""
    if time.time() - _pl_last_write_t < 30:
        return
    _write_pipeline_status(_pl_current_stage, _pl_current_stage_name)


def _write_skip_status(reason: str) -> None:
    """스킵 상태를 STATUS_FILE에 기록 — 프론트에서 '스킵됨' 배너 표시 가능."""
    try:
        now = time.time()
        STATUS_FILE.write_text(
            json.dumps({
                "started_at":         now,
                "current_stage":      0,
                "current_stage_name": "",
                "total_stages":       11,
                "last_heartbeat":     now,
                "status":             "skipped",
                "reason":             reason,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _is_already_current(_now: "datetime | None" = None) -> bool:
    """prices 수집 상태와 현재 시각을 비교해 중복 실행 여부 판단.

    규칙:
      1. 직전 거래일 데이터가 없으면          → 실행  (밀린 데이터 따라잡기)
      2. 16:30 이전 AND 직전 거래일 데이터 있음 → 스킵  (장 중이라 새 데이터 없음)
      3. 16:30 이후 AND 오늘 데이터 있음       → 스킵  (이미 최신)
      4. 16:30 이후 AND 오늘 데이터 없음       → 실행  (장 마감 후 수집 필요)

    직전 거래일은 market_index 테이블의 오늘 이전 MAX(date)로 판단한다.
    _now 파라미터는 테스트 전용 — 실제 실행에서는 None.
    """
    try:
        now = _now if _now is not None else datetime.now()
        today_str = now.strftime("%Y%m%d")
        db_path = DATA_DIR / "stocks.db"

        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
            latest_prices_date = row[0] if row else None

            row2 = conn.execute(
                "SELECT MAX(date) FROM market_index WHERE date < ?", (today_str,)
            ).fetchone()
            prev_trading_day = row2[0] if row2 else None

        # 직전 거래일을 알 수 없거나 prices 자체가 비었으면 실행
        if prev_trading_day is None or latest_prices_date is None:
            return False

        # 직전 거래일 데이터도 없으면 밀린 데이터가 있다 → 실행
        if latest_prices_date < prev_trading_day:
            return False

        # 직전 거래일 데이터까지는 있음
        after_1630 = now.hour > 16 or (now.hour == 16 and now.minute >= 30)

        if not after_1630:
            return True  # 장 중이고 직전 거래일 최신 → 스킵

        # 16:30 이후: 오늘 데이터가 있어야 스킵
        return latest_prices_date == today_str

    except Exception:
        return False  # DB 접근 실패 시 스킵하지 않음(안전 방향)


# ─────────────────────────────────────────────────────────────────────────────


def _set_background_io(pid: int) -> None:
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
    if not handle:
        return
    try:
        ctypes.windll.kernel32.SetPriorityClass(handle, PROCESS_MODE_BACKGROUND_BEGIN)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def _sep(label: str) -> None:
    global _pl_current_stage, _pl_current_stage_name
    log.info("=" * 60)
    log.info("  %s", label)
    log.info("=" * 60)
    m = re.match(r"(\d+)단계:\s*(.*)", label)
    if m:
        _pl_current_stage      = int(m.group(1))
        _pl_current_stage_name = m.group(2).strip()
        _write_pipeline_status(_pl_current_stage, _pl_current_stage_name)


def run(cmd: list, cwd: Path, label: str, dry_run: bool = False) -> None:
    full_cmd = [str(PYTHON)] + [str(c) for c in cmd]
    log.info("[실행] %s", " ".join(full_cmd))
    if dry_run:
        log.info("[DRY-RUN] 건너뜀")
        return
    t0 = time.time()
    proc = subprocess.Popen(
        full_cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=SUBPROCESS_FLAGS,
    )
    _set_background_io(proc.pid)
    for line in proc.stdout:
        log.info("[%s] %s", label, line.rstrip())
        _touch_heartbeat()
    proc.wait()
    elapsed = time.time() - t0
    if proc.returncode != 0:
        log.error("%s 실패 (exit=%d, %.0fs)", label, proc.returncode, elapsed)
        sys.exit(proc.returncode)
    log.info("%s 완료 (%.0fs)", label, elapsed)


# 섹터/BPS 분할 수집 하루 한도 — 둘을 합쳐 하루 ~5,000회 DART 호출 이내로 유지
# (sector 1건=1회 호출, bps 1건=2회 호출). DART burst 차단이 풀린 뒤부터 자동으로 진행되며
# skip 로직 덕분에 다 채워지면 자동으로 "남은 작업 없음"이 되어 멈춤 (2026-06-22)
SECTOR_DAILY_MAX_JOBS = 1500   # 1500회 호출/일
BPS_DAILY_MAX_JOBS = 1750      # 3500회 호출/일

def run_optional(cmd: list, cwd: Path, label: str, dry_run: bool = False) -> None:
    """run()과 동일하나 실패해도 파이프라인을 막지 않음 — 섹터/BPS 분할 수집처럼 DART 한도에
    걸려도 다음날 또 시도하면 되는 비치명적 보조 작업용 (2026-06-22)."""
    full_cmd = [str(PYTHON)] + [str(c) for c in cmd]
    log.info("[실행] %s", " ".join(full_cmd))
    if dry_run:
        log.info("[DRY-RUN] 건너뜀")
        return
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            full_cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=SUBPROCESS_FLAGS,
        )
        _set_background_io(proc.pid)
        for line in proc.stdout:
            log.info("[%s] %s", label, line.rstrip())
            _touch_heartbeat()
        proc.wait()
        elapsed = time.time() - t0
        if proc.returncode != 0:
            log.warning("%s 실패했지만 비치명적 — 계속 진행 (exit=%d, %.0fs)", label, proc.returncode, elapsed)
        else:
            log.info("%s 완료 (%.0fs)", label, elapsed)
    except Exception as exc:
        log.warning("%s 실행 중 오류(비치명적): %s", label, exc)


SERVER_PORT = 8001


def kill_port(port: int) -> None:
    subprocess.run(
        [
            "powershell", "-Command",
            f"$p=Get-NetTCPConnection -LocalPort {port} -State Listen -EA SilentlyContinue;"
            "if($p){Stop-Process -Id $p.OwningProcess -Force -EA SilentlyContinue;"
            "Write-Output 'Killed'}",
        ],
        capture_output=True,
        creationflags=SUBPROCESS_FLAGS,
    )
    time.sleep(2)


def restart_server(dry_run: bool = False) -> None:
    _sep(f"3단계: 서버 재시작 (포트 {SERVER_PORT})")
    if dry_run:
        log.info("[DRY-RUN] 서버 재시작 건너뜀")
        return

    kill_port(SERVER_PORT)
    log.info("기존 서버 종료 완료 — 새 프로세스로 시작합니다")

    subprocess.Popen(
        [str(PYTHON), "-m", "uvicorn", "main:app", "--port", str(SERVER_PORT)],
        cwd=str(SRV_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | SERVER_FLAGS,
    )

    for attempt in range(15):
        time.sleep(3)
        try:
            with socket.create_connection(("127.0.0.1", SERVER_PORT), timeout=3):
                log.info("서버 응답 확인 (시도 %d)", attempt + 1)
                break
        except OSError:
            pass
    else:
        log.warning("헬스체크 실패 — 서버 로그 확인: backend/server/server.log")


def is_weekday() -> bool:
    return datetime.now().weekday() < 5


def _is_trading_day(today_str: str) -> bool:
    """오늘이 KRX 거래일인지 pykrx로 확인한다.

    KRX는 15:30에 닫히므로 16:30 파이프라인 실행 시점에는 당일 인덱스 데이터가
    존재한다. 공휴일(광복절 등)에는 빈 DataFrame이 반환 → False.
    네트워크 오류 등 판단 불가 시에는 True(안전 방향)를 반환해 파이프라인을 실행.
    """
    try:
        from pykrx import stock as _stock
        df = _stock.get_index_ohlcv_by_date(today_str, today_str, "1001")
        return df is not None and len(df) > 0
    except Exception:
        return True  # 불확실 → 실행 (안전 방향)


def _is_pid_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def acquire_lock() -> bool:
    """이미 실행 중인 다른 daily_pipeline.py(.bat이든 API든)가 있으면 False."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if _is_pid_alive(pid):
                return False
        except Exception:
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="일일 증분 파이프라인")
    parser.add_argument("--dry-run", action="store_true", help="실행 없이 단계만 출력")
    parser.add_argument(
        "--force", action="store_true",
        help="'이미최신' 가드만 우회 — POST /api/update(수동 버튼)에서 사용. 주말 가드는 우회 불가."
    )
    parser.add_argument(
        "--force-weekend", action="store_true",
        help="주말 가드 + 이미최신 가드 모두 우회 — 주말 강제 실행이 꼭 필요할 때만 사용."
    )
    parser.add_argument(
        "--no-server-restart", action="store_true",
        help="3단계 서버 재시작을 건너뜀 — POST /api/update(인앱 버튼)처럼 이 파이프라인을 "
             "호출한 서버 프로세스 자신이 살아있어야 할 때 사용. 호출 측이 완료 후 "
             "_reload_model_and_cache()로 인프로세스 핫리로드를 직접 처리해야 함."
    )
    args = parser.parse_args()

    # 이 프로세스 자체도 백그라운드 모드로 설정 (작업 스케줄러 자동 실행 시 게임 등 방해 없도록)
    # PROCESS_MODE_BACKGROUND_BEGIN: CPU + 디스크 I/O 우선순위를 모두 백그라운드 수준으로 낮춤
    try:
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(),
            PROCESS_MODE_BACKGROUND_BEGIN
        )
    except Exception:
        pass

    # 주말 가드: --force-weekend 만 우회 가능. --force 는 우회 불가.
    if not is_weekday() and not args.force_weekend:
        log.info("비거래일 — 일일 파이프라인 스킵 (--force-weekend 로만 강제 실행 가능)")
        _write_skip_status("weekend")
        return

    # 공휴일 가드: 평일이지만 KRX 휴장일(광복절 대체휴일 등). --force-weekend 만 우회 가능.
    _today_for_guard = date.today().strftime("%Y%m%d")
    if not _is_trading_day(_today_for_guard) and not args.force_weekend:
        log.info("휴장일(공휴일) — 일일 파이프라인 스킵 (--force-weekend 로만 강제 실행 가능)")
        _write_skip_status("holiday")
        return

    # 이미최신 가드: --force 또는 --force-weekend 로 우회 가능.
    force = args.force or args.force_weekend
    if not force and _is_already_current():
        log.info("오늘 데이터 이미 최신 — 중복 실행 스킵 (--force 로 강제 실행 가능)")
        _write_skip_status("already_current")
        return

    if not acquire_lock():
        log.warning("다른 daily_pipeline.py 실행이 이미 진행 중(.bat 또는 인앱 버튼) — 종료")
        sys.exit(1)

    global _pl_started_at, _pl_finished_ok
    _pl_started_at  = time.time()
    _pl_finished_ok = False
    _write_pipeline_status(0, "파이프라인 시작")

    try:
        today = date.today().strftime("%Y%m%d")
        log.info("")
        log.info("=== 일일 증분 파이프라인 시작: %s ===", today)
        if args.dry_run:
            log.info("[DRY-RUN 모드]")

        # 1. 오늘치 데이터만 증분 수집 (INSERT OR IGNORE — 중복 안전)
        _sep("1단계: 오늘치 데이터 증분 수집")
        run(["collector.py"], cwd=DATA_DIR, label="수집", dry_run=args.dry_run)

        # 1-b. 지수 일봉 증분 수집 (market_index — 모의투자 거래일 계산에 필요)
        run(["index_collector.py"], cwd=DATA_DIR, label="지수수집", dry_run=args.dry_run)

        # 2. 마지막 피처 날짜 이후만 피처 계산 (자동 증분)
        _sep("2단계: 피처 증분 업데이트")
        run(["pipeline.py"], cwd=FEAT_DIR, label="피처", dry_run=args.dry_run)

        # 3. 서버 재시작 (새 피처 반영 + lifespan에서 record_recommendations 자동 호출)
        # --no-server-restart면 건너뜀 — 호출한 서버 프로세스 본인이 죽으면 안 되므로
        # (예: 인앱 "데이터 업데이트" 버튼). 그 경우 호출 측이 핫리로드를 직접 처리.
        if args.no_server_restart:
            _sep("3단계: 서버 재시작 (건너뜀, 호출 측이 인프로세스 핫리로드 처리)")
        else:
            restart_server(dry_run=args.dry_run)

        # 4. 모의투자 만기 거래 청산 (lifespan 실패 시 보험)
        _sep("4단계: 모의투자 만기 거래 청산")
        if args.dry_run:
            log.info("[DRY-RUN] close_expired_trades(%s) 건너뜀", today)
        else:
            try:
                sys.path.insert(0, str(SRV_DIR))
                from paper_trader import close_expired_trades  # type: ignore[import]
                result = close_expired_trades(today)
                log.info("청산 완료: closed=%d expired=%d", result["closed"], result["expired"])
            except Exception as exc:
                log.error("모의투자 청산 오류 (비치명적): %s", exc)

        # 5. 31.9% 재분석 준비 상태 체크 (model_version 기록 거래가 충분히 쌓이면 1회 자동 분석)
        _sep("5단계: 재분석 준비 상태 체크")
        run(["check_reanalysis_ready.py"], cwd=SCRIPTS_DIR, label="재분석체크", dry_run=args.dry_run)

        # 6. 섹터/BPS 분할 수집 — 하루 한도 내에서 조금씩, skip 로직으로 다 채워지면 자동 중단
        # (DART burst 차단 중이면 실패해도 비치명적 — run_optional이 파이프라인을 막지 않음)
        _sep("6단계: 섹터/BPS 분할 수집")
        run_optional(
            ["sector_collector.py", "--max-jobs", str(SECTOR_DAILY_MAX_JOBS)],
            cwd=DATA_DIR, label="섹터분할수집", dry_run=args.dry_run,
        )
        run_optional(
            ["bps_collector.py", "--max-jobs", str(BPS_DAILY_MAX_JOBS)],
            cwd=DATA_DIR, label="BPS분할수집", dry_run=args.dry_run,
        )

        # 7. 가격 데이터 정합성 점검 — 전일 대비 2.5배+ 비율 이상 감지 시 pykrx 전체
        # 재조회로 비교해서 진짜 스케일 버그(분할 미반영 등)면 자동 수정, 진짜 시세
        # 변동이면 보존 (2026-06-24, 백필↔증분 경계 버그 91건 사후 예방 루틴)
        _sep("7단계: 가격 데이터 정합성 점검")
        run_optional(
            ["price_integrity_check.py"],
            cwd=DATA_DIR, label="정합성점검", dry_run=args.dry_run,
        )

        # 8. 배당 분할/병합 보정계수 증분 갱신 — 가격×배당 비율로 새 분할/병합 의심 종목을
        # 가볍게 재탐지(DART 호출 없음)한 뒤, 달라진 것만 DART로 최소 확증 (2026-06-26,
        # "고배당 point-in-time 분할비율 보정" 재발방지 — 7단계와 같은 결의 자동 점검)
        _sep("8단계: 배당 분할/병합 보정계수 갱신")
        run_optional(
            ["dividend_split_correction_update.py"],
            cwd=ML_DIR, label="배당분할보정갱신", dry_run=args.dry_run,
        )

        # 9. KIS(한국투자증권) Open API로 종목별 투자자매매동향(개인/외국인/기관계) 일별 수집
        # — 이 API는 호출 시점 기준 최근 ~30거래일 롤링 윈도우만 주므로 매일 누적해야
        # 끊김 없이 쌓임 (2026-06-27, 연기금은 종목 단위로 분리 안 됨 — CLAUDE.md 참고)
        _sep("9단계: KIS 투자자매매동향(외국인/기관) 수집")
        run_optional(
            ["kis_investor_collector.py"],
            cwd=DATA_DIR, label="KIS투자자수집", dry_run=args.dry_run,
        )

        # 9-2. KIS investor_trading_kis_detail 증분 갱신 — 연기금(pension) 포함 8개 투자자 유형
        # — 백필 API(FHPTJ04160001)는 앵커 기준 최근 ~30거래일 반환. 7일 이상 갱신 안 된
        # 종목부터 400건씩 순차 갱신. daily_pipeline 16:30 실행이라 TIME LIMIT(15:40~) 내 정상 작동.
        # — run_optional: API 장애/TIME LIMIT 발생 시에도 파이프라인 전체를 막지 않음 (2026-08-09)
        _sep("9-2단계: KIS 연기금 포함 투자자상세(detail) 증분 갱신")
        run_optional(
            ["kis_investor_detail_updater.py", "--max-jobs", "400"],
            cwd=DATA_DIR, label="KIS연기금상세갱신", dry_run=args.dry_run,
        )

        # 10. 예측 이력 기록 + 만기 실현 결과 계산 (append-only, 수정 금지)
        # — 서버 재시작(3단계) 이후 실행해야 최신 모델로 예측 가능
        # — run_optional: 서버 미기동이나 모델 미로드 시에도 파이프라인 전체를 막지 않음
        _sep("10단계: 예측 이력 기록 + 만기 실측")
        run_optional(
            ["prediction_logger.py"],
            cwd=SRV_DIR, label="예측이력", dry_run=args.dry_run,
        )

        # 11. 파이프라인 무결성 체크 — 위반 시 exit 1로 조용한 성공 차단
        # (prices 최신일 공백, 행수 이상 소실, 핵심 피처 결측률 급증)
        # run_optional: 체크 실패 자체가 운영을 막으면 안 됨 — 로그로만 경보
        _sep("11단계: 파이프라인 무결성 체크")
        run_optional(
            ["pipeline_integrity_check.py"],
            cwd=SCRIPTS_DIR, label="무결성체크", dry_run=args.dry_run,
        )

        # 12. 시그널 감지 — 규칙 기반 이벤트 탐지 및 signal_log INSERT
        _sep("12단계: 시그널 감지")
        run_optional(
            ["detect_signals.py"],
            cwd=SCRIPTS_DIR, label="시그널감지", dry_run=args.dry_run,
        )

        _pl_finished_ok = True
        log.info("")
        log.info("=== 일일 파이프라인 완료: %s ===", today)
        log.info("로그: %s", LOG_FILE)
    finally:
        _write_pipeline_status(
            _pl_current_stage,
            _pl_current_stage_name,
            "done" if _pl_finished_ok else "error",
        )
        release_lock()


if __name__ == "__main__":
    main()
