"""
전체 파이프라인: 전종목 3년치 수집 → 피처 → 학습 → 서버 교체
백그라운드 실행용 스크립트

실행법:
  python run_full_pipeline.py            # 전체 재학습 (25 trials, ~30분)
  python run_full_pipeline.py --quick    # 빠른 재학습 (15 trials, 수집 스킵, ~2-3분)

로그: backend/pipeline_full.log
"""

import argparse
import ctypes
import logging
import subprocess
import sys
import time
import socket

# Windows cp949 콘솔에서 UTF-8 문자 출력 시 UnicodeEncodeError 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import date, timedelta
from pathlib import Path

# ── 경로 설정 ──────────────────────────────────────────────────
ROOT      = Path(__file__).parent          # backend/
DATA_DIR  = ROOT / "data"
FEAT_DIR  = ROOT / "features"
ML_DIR    = ROOT / "ml"
SRV_DIR   = ROOT / "server"
LOG_FILE  = ROOT / "pipeline_full.log"

START_DATE   = (date.today() - timedelta(days=5 * 365)).strftime("%Y%m%d")
END_DATE     = date.today().strftime("%Y%m%d")
TRIALS_FULL  = 25
TRIALS_QUICK = 15
VAL_DAYS     = 252  # 1년치 검증 (target_5d 기준)

# 전종목 데이터 수집 시 병렬 스레드 수 (기본 16 → 낮춰서 대역폭 점유 완화).
# 주말 새벽(StockGame-WeeklyRetrain)에 게임/인터넷 핑에 영향 최소화하기 위함 —
# daily_pipeline.py의 증분 수집(보통 스킵되는 종목이 많아 가벼움)과 달리 전체 재학습은
# 전종목 펀더멘털/수급을 매번 무조건 다시 호출해(collect_fundamentals_and_flows,
# 증분 스킵 없음) 네트워크 부담이 큼. 6으로 낮추면 동시 연결 수가 16→6(약 2.7배 감소)로
# 줄어 체감 대역폭 점유가 크게 줄지만, 요청당 지연이 짧아(<1s) 전체 소요시간 증가는
# 수 분 수준 — 재학습이 몇 시간씩 늘어나지는 않음.
COLLECTOR_WORKERS = 6

# 콘솔 창 숨김 + CPU 우선순위 최저 (daily_pipeline.py와 동일 패턴 — 게임 등 포그라운드
# 작업에 영향 안 주도록 유휴 시간에만 CPU 사용). 서버 재시작만 NORMAL 우선순위로 분리.
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.IDLE_PRIORITY_CLASS
SERVER_FLAGS     = subprocess.CREATE_NO_WINDOW | subprocess.NORMAL_PRIORITY_CLASS

PROCESS_SET_INFORMATION       = 0x0200
PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000


def _set_background_io(pid: int) -> None:
    """CPU뿐 아니라 디스크 I/O·메모리 우선순위까지 백그라운드 수준으로 낮춤
    (daily_pipeline.py와 동일 — CreateProcess의 creationflags로는 설정 불가해
    프로세스 시작 후 핸들에 별도로 적용)."""
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
    if not handle:
        return
    try:
        ctypes.windll.kernel32.SetPriorityClass(handle, PROCESS_MODE_BACKGROUND_BEGIN)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)

# ── 로거 설정 ──────────────────────────────────────────────────
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
    log.info("=" * 60)
    log.info("  %s", label)
    log.info("=" * 60)


def run(cmd: list[str], cwd: Path, label: str, dry_run: bool = False) -> None:
    """서브프로세스 실행 — stdout/stderr 실시간 로그에 기록.
    콘솔 창 숨김 + IDLE 우선순위(daily_pipeline.py와 동일)로 게임 등 포그라운드 작업 방해 안 함."""
    log.info("[실행] %s", " ".join(str(c) for c in cmd))
    if dry_run:
        log.info("[DRY-RUN] 건너뜀")
        return
    t0 = time.time()
    proc = subprocess.Popen(
        [sys.executable] + [str(c) for c in cmd],
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
    proc.wait()
    elapsed = time.time() - t0
    if proc.returncode != 0:
        log.error("%s 실패 (exit=%d, %.0fs)", label, proc.returncode, elapsed)
        sys.exit(proc.returncode)
    log.info("%s 완료 (%.0fs)", label, elapsed)


SERVER_PORT = 8001
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
SERVER_DIR  = ROOT / "server"


def restart_server(dry_run: bool = False) -> None:
    """PM2로 관리되는 서버를 reload (무중단 모델 교체)"""
    _sep("6단계: 서버 재시작")
    if dry_run:
        log.info("[DRY-RUN] 서버 재시작 건너뜀")
        return

    # PM2 reload: 무중단으로 새 모델 적용 (Windows에서 npx는 shell=True 필요)
    result = subprocess.run(
        "npx pm2 reload stock-backend",
        capture_output=True, text=True, shell=True,
        creationflags=SUBPROCESS_FLAGS,
    )
    if result.returncode == 0:
        log.info("PM2 reload 완료")
    else:
        # PM2 없을 경우 fallback: 포트 강제 종료 후 직접 기동
        # (콘솔 창 숨김 + 정상 우선순위 — daily_pipeline.py의 SERVER_FLAGS와 동일:
        #  실시간 API 응답 지연을 막기 위해 서버만 IDLE이 아닌 NORMAL 우선순위 유지)
        log.warning("PM2 reload 실패 — 직접 재시작: %s", result.stderr.strip())
        subprocess.run(
            ["powershell", "-Command",
             f"$p=Get-NetTCPConnection -LocalPort {SERVER_PORT} -State Listen -EA SilentlyContinue;"
             "if($p){{Stop-Process -Id $p.OwningProcess -Force -EA SilentlyContinue}}"],
            capture_output=True,
            creationflags=SUBPROCESS_FLAGS,
        )
        time.sleep(2)
        subprocess.Popen(
            [str(VENV_PYTHON), "-m", "uvicorn", "main:app", "--port", str(SERVER_PORT)],
            cwd=str(SERVER_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | SERVER_FLAGS,
        )

    # 헬스체크
    for attempt in range(15):
        time.sleep(3)
        try:
            with socket.create_connection(("127.0.0.1", SERVER_PORT), timeout=3):
                log.info("서버 응답 확인 (시도 %d)", attempt + 1)
                break
        except OSError:
            pass
    else:
        log.warning("헬스체크 실패 — 서버 로그 확인")


# ── 메인 ──────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="전체 재학습 파이프라인")
    parser.add_argument("--quick", action="store_true",
                        help="빠른 재학습: 데이터 수집 스킵 + 15 trials (~2-3분)")
    parser.add_argument("--dry-run", action="store_true", help="실행 없이 단계만 출력")
    args = parser.parse_args()

    trials = TRIALS_QUICK if args.quick else TRIALS_FULL
    mode   = "빠른 재학습" if args.quick else "전체 재학습"

    log.info("")
    log.info("★★★ %s 파이프라인 시작 ★★★", mode)
    log.info("기간: %s ~ %s | trials=%d | val_days=%d", START_DATE, END_DATE, trials, VAL_DAYS)
    if args.dry_run:
        log.info("[DRY-RUN 모드]")
    log.info("")

    if not args.quick:
        # 1. 전종목 3년치 OHLCV 수집 (--workers로 동시 연결 수 제한 — 위 COLLECTOR_WORKERS 설명 참고)
        _sep("1단계: 전종목 데이터 수집 (3년)")
        run(
            ["collector.py", "--start", START_DATE, "--end", END_DATE,
             "--workers", str(COLLECTOR_WORKERS)],
            cwd=DATA_DIR,
            label="수집",
            dry_run=args.dry_run,
        )

        # 2. DB 상태 확인
        _sep("2단계: DB 확인")
        run(["check_db.py"], cwd=DATA_DIR, label="DB체크", dry_run=args.dry_run)
    else:
        log.info("[빠른 재학습] 데이터 수집 스킵 — 일일 파이프라인이 최신 데이터를 이미 수집함")

    # 3. 피처 엔지니어링 (증분 — 새 날짜만 계산)
    _sep("3단계: 피처 엔지니어링")
    run(["pipeline.py"], cwd=FEAT_DIR, label="피처", dry_run=args.dry_run)

    # 4. 모델 학습
    _sep(f"4단계: 모델 학습 ({trials} trials)")
    run(
        ["train.py", "--target", "target_5d", "--trials", str(trials),
         "--val-days", str(VAL_DAYS)],
        cwd=ML_DIR,
        label="학습",
        dry_run=args.dry_run,
    )

    # 5. 국면별(bull/bear) 분리 모델 — 약세장 가드 활성화 시 bear 모델로 전환하는 데 사용.
    #    기존 통합 모델(4단계)은 그대로 두고 별도 디렉터리에 추가 저장.
    _sep("5단계: 국면별(bull/bear) 분리 모델 학습")
    run(
        ["train_regime.py", "--val-days", str(VAL_DAYS)],
        cwd=ML_DIR,
        label="국면별학습",
        dry_run=args.dry_run,
    )

    # 6. 서버 재시작
    restart_server(dry_run=args.dry_run)

    log.info("")
    log.info("★★★ %s 파이프라인 완료 ★★★", mode)
    log.info("로그: %s", LOG_FILE)


if __name__ == "__main__":
    main()
