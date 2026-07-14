"""
31.9% 재분석 자동 트리거 체크.

paper_trades에 model_version이 기록된(NOT NULL) + 청산 완료(status='closed')
거래가 READY_THRESHOLD건(기본 30) 이상 쌓이면, ml/analyze_paper_trades_reanalysis.py를
1회 실행해 backend/reanalysis_report.md를 생성한다.
이미 실행했으면(플래그 파일 backend/data/.reanalysis_done 존재) 조건과 무관하게
조용히 스킵 — --force로 강제 재실행 가능.

daily_pipeline.py 마지막 단계에서 호출됨. 항상 exit 0(비치명적) — 이 체크가
실패해도 일일 파이프라인 전체를 막지 않음.

실행: python check_reanalysis_ready.py [--threshold 30] [--force] [--dry-run]
로그: backend/check_reanalysis.log
"""

import argparse
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

# Windows cp949 콘솔에서 UTF-8 문자(★, — 등) 출력 시 UnicodeEncodeError 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT       = Path(__file__).parent.parent
ML_DIR     = ROOT / "ml"
DB_PATH    = ROOT / "data" / "stocks.db"
FLAG_FILE  = ROOT / "data" / ".reanalysis_done"
REPORT_FILE = ROOT / "reanalysis_report.md"
PYTHON     = ROOT / ".venv" / "Scripts" / "python.exe"
DEFAULT_THRESHOLD = 30

# 게임 등 포그라운드 작업 방해 안 하도록 유휴 우선순위로 실행 (daily_pipeline.py와 동일 패턴)
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.IDLE_PRIORITY_CLASS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "check_reanalysis.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def count_ready_trades() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM paper_trades "
            "WHERE status='closed' AND model_version IS NOT NULL"
        ).fetchone()
    return row[0] if row else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="31.9% 재분석 자동 트리거 체크")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--force", action="store_true", help="플래그 무시하고 강제 재실행")
    parser.add_argument("--dry-run", action="store_true", help="조건만 체크, 실제 분석/플래그 기록은 안 함")
    args = parser.parse_args()

    try:
        if FLAG_FILE.exists() and not args.force:
            log.info("이미 재분석 완료됨(%s) — 스킵", FLAG_FILE.name)
            return 0

        count = count_ready_trades()
        log.info("model_version 기록 + 청산완료 거래 수: %d (기준 %d)", count, args.threshold)

        if count < args.threshold:
            log.info("조건 미충족 — 재분석 보류")
            return 0

        log.info("조건 충족 — 재분석 실행 준비")
        if args.dry_run:
            log.info("[DRY-RUN] 실제 분석 실행/플래그 기록은 건너뜀")
            return 0

        proc = subprocess.run(
            [str(PYTHON), "analyze_paper_trades_reanalysis.py", "--threshold", str(args.threshold)],
            cwd=str(ML_DIR),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=SUBPROCESS_FLAGS,
        )
        for line in (proc.stdout or "").splitlines():
            log.info("[재분석] %s", line)
        if proc.returncode != 0:
            log.warning("재분석 스크립트가 정상 종료되지 않음(exit=%d): %s", proc.returncode, (proc.stderr or "").strip())
            return 0  # 비치명적 — daily_pipeline 전체를 막지 않음

        FLAG_FILE.write_text(f"reanalysis completed, report={REPORT_FILE.name}\n", encoding="utf-8")
        log.info("★ 31.9%% 재분석 완료 — %s 확인", REPORT_FILE.name)
        return 0
    except Exception as exc:
        log.error("재분석 체크 중 예외(비치명적): %s", exc)
        return 0


if __name__ == "__main__":
    sys.exit(main())
