"""서빙 모델 버전 검증 — ACTIVE 포인터가 regime 모델을 가리키지 않는지,
bear_regime 로드 경로가 main.py 서빙 코드에서 제거됐는지 확인."""
import re
from pathlib import Path

MODELS_DIR = Path(__file__).parent.parent / "models"
MAIN_PY = Path(__file__).parent.parent / "server" / "main.py"


def test_active_5d_not_regime():
    """ACTIVE_target_5d.txt가 regime 모델을 가리키지 않음"""
    ptr = MODELS_DIR / "ACTIVE_target_5d.txt"
    assert ptr.exists(), "ACTIVE_target_5d.txt 없음"
    active_dir = ptr.read_text(encoding="utf-8").strip()
    assert "regime" not in active_dir, (
        f"5d ACTIVE 포인터가 regime 모델을 가리킴: {active_dir}"
    )


def test_active_60d_not_regime():
    """ACTIVE_target_60d.txt가 regime 모델을 가리키지 않음"""
    ptr = MODELS_DIR / "ACTIVE_target_60d.txt"
    assert ptr.exists(), "ACTIVE_target_60d.txt 없음"
    active_dir = ptr.read_text(encoding="utf-8").strip()
    assert "regime" not in active_dir, (
        f"60d ACTIVE 포인터가 regime 모델을 가리킴: {active_dir}"
    )


def test_bear_regime_load_removed_from_main():
    """main.py 서빙 경로에서 load_bear_regime_model 활성 호출이 없음.
    import 선언은 허용, 실제 함수 호출(주석 아닌 코드)만 검사."""
    src = MAIN_PY.read_text(encoding="utf-8")
    active_calls = re.findall(
        r"^\s*[^#\n].*load_bear_regime_model\s*\(",
        src,
        re.MULTILINE,
    )
    assert len(active_calls) == 0, (
        f"main.py에 load_bear_regime_model 활성 호출 잔존: {active_calls}"
    )


def test_active_5d_dir_exists():
    """ACTIVE 포인터가 가리키는 5d 모델 디렉토리가 실제로 존재함"""
    ptr = MODELS_DIR / "ACTIVE_target_5d.txt"
    if not ptr.exists():
        return
    active_dir = ptr.read_text(encoding="utf-8").strip()
    assert (MODELS_DIR / active_dir).is_dir(), (
        f"5d ACTIVE 디렉토리 없음: {active_dir}"
    )


def test_active_60d_dir_exists():
    """ACTIVE 포인터가 가리키는 60d 모델 디렉토리가 실제로 존재함"""
    ptr = MODELS_DIR / "ACTIVE_target_60d.txt"
    if not ptr.exists():
        return
    active_dir = ptr.read_text(encoding="utf-8").strip()
    assert (MODELS_DIR / active_dir).is_dir(), (
        f"60d ACTIVE 디렉토리 없음: {active_dir}"
    )
