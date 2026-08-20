"""
회귀 테스트: prediction_logger.py 인코딩 + 업데이트 배너 타이머 버그 방지.

[Bug 1] prediction_logger.py가 daily_pipeline.py의 자식 프로세스로 실행될 때
        logging이 기본 stderr + cp949로 출력되어 한글이 깨지던 문제.
        sys.stdout.reconfigure(encoding="utf-8") + stream=sys.stdout 으로 수정.

[Bug 2] 락파일은 있으나 pipeline_status.json에 started_at 미기록(경합 윈도우) 시
        elapsed_sec=None을 반환해 프론트엔드가 "0s"를 고정 표시하던 문제.
        elapsed 기본값을 0으로, 락파일 mtime을 폴백 앵커로 수정.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent  # stock-game/

# ── Bug 1: prediction_logger.py 인코딩 설정 ────────────────────────────────


def _parse_main_block(src: str) -> list[ast.stmt]:
    """if __name__ == '__main__': 블록 내 AST 노드 목록을 반환."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            return node.body
    return []


def test_prediction_logger_has_stdout_reconfigure():
    """prediction_logger.py의 __main__ 블록에 sys.stdout.reconfigure 호출이 있어야 한다."""
    src = (ROOT / "backend" / "server" / "prediction_logger.py").read_text(encoding="utf-8")
    assert "sys.stdout.reconfigure" in src, (
        "prediction_logger.py에 sys.stdout.reconfigure 없음 — "
        "daily_pipeline.py 자식 프로세스로 실행 시 한글 인코딩 깨짐"
    )


def test_prediction_logger_has_stderr_reconfigure():
    """예외 트레이스백(stderr)도 UTF-8로 출력돼야 한다."""
    src = (ROOT / "backend" / "server" / "prediction_logger.py").read_text(encoding="utf-8")
    assert "sys.stderr.reconfigure" in src, (
        "prediction_logger.py에 sys.stderr.reconfigure 없음 — "
        "stderr 출력(예외 메시지 등)이 cp949로 나와 일지 깨질 수 있음"
    )


def test_prediction_logger_basicconfig_uses_stdout():
    """basicConfig가 stream=sys.stdout을 써야 한다.
    기본값(stderr)은 sys.stdout.reconfigure의 영향을 받지 않음.
    """
    src = (ROOT / "backend" / "server" / "prediction_logger.py").read_text(encoding="utf-8")
    # stream=sys.stdout 이 basicConfig 호출에 포함돼 있는지 확인
    # AST 파싱 대신 단순 텍스트 검색 (basicConfig 인자 순서 무관)
    assert "stream=sys.stdout" in src, (
        "prediction_logger.py basicConfig에 stream=sys.stdout 없음 — "
        "reconfigure 효과가 로그 핸들러에 전달되지 않음"
    )


# ── Bug 2: _update_status() elapsed 기본값 + 락파일 mtime 폴백 ──────────────


def test_update_status_elapsed_default_is_zero_not_none():
    """main.py _update_status() 외부 실행 경로에서 elapsed 기본값이 0이어야 한다.

    Optional[int] = None 이면 running 중 elapsed_sec=None → 프론트 '0s' 고정.
    int = 0 으로 변경돼 있어야 test_update_status_elapsed_sec_type 통과.
    """
    src = (ROOT / "backend" / "server" / "main.py").read_text(encoding="utf-8")
    # elapsed: int = 0 이 외부 실행 경합 윈도우 처리 코드에 있어야 함
    # "elapsed: Optional[int] = None" 패턴이 _update_status 에서 사라졌는지 검사
    # (정식 패턴: elapsed: int = 0)
    assert "elapsed: int = 0" in src, (
        "main.py _update_status()에 'elapsed: int = 0' 없음 — "
        "외부 실행(bat) 감지 경로에서 elapsed_sec=None 반환 가능"
    )


def test_update_status_uses_lock_file_mtime_fallback():
    """started_at 없을 때 락파일 mtime을 폴백 앵커로 사용해야 한다.

    pipeline_status.json이 아직 기록되지 않은 경합 윈도우에서도
    started_at을 추정해 프론트 타이머가 0s에 고정되지 않게 함.
    """
    src = (ROOT / "backend" / "server" / "main.py").read_text(encoding="utf-8")
    assert "st_mtime" in src, (
        "main.py에 락파일 mtime 폴백 없음 — "
        "pipeline_status.json 미기록 윈도우에서 started_at=None 반환"
    )
    # 폴백 코드가 _UPDATE_LOCK_FILE 을 참조해야 함
    assert "_UPDATE_LOCK_FILE.stat().st_mtime" in src, (
        "main.py에 '_UPDATE_LOCK_FILE.stat().st_mtime' 없음 — 락파일 mtime 폴백 미구현"
    )
