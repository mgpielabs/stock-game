"""
회귀 테스트: daily_pipeline.py 주말·공휴일 가드

핵심 불변:
  1. 주말 + --force         → 가드 발동 (스킵)  — /api/update 가 주말에 눌려도 막힌다
  2. 주말 + --force-weekend → 가드 우회 (진행)  — 명시적 주말 강제 실행만 허용
  3. 주말 + 플래그 없음     → 가드 발동 (스킵)  — 기본 동작
  4. 평일 + --force         → 이미최신 가드 우회 (진행) — 수동 버튼 정상 동작
  5. 공휴일 + 플래그 없음   → 가드 발동 (스킵)  — 광복절 등 평일 공휴일
  6. 공휴일 + --force       → 가드 발동 (스킵)  — /api/update 도 공휴일엔 막힌다
  7. 공휴일 + --force-weekend → 가드 우회 (진행) — 명시적 강제 실행만 허용
"""
import sys
import importlib
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))


def _load_pipeline() -> types.ModuleType:
    """daily_pipeline 모듈을 새로 로드 (상태 격리)."""
    if "daily_pipeline" in sys.modules:
        del sys.modules["daily_pipeline"]
    return importlib.import_module("daily_pipeline")


def _run_main(dp: types.ModuleType, argv: list) -> str:
    """
    dp(이미 로드+패치된 모듈)의 main()을 argv로 실행하고
    _write_skip_status 에 넘어간 reason을 반환.
    파이프라인이 실제로 실행된(가드 모두 통과) 경우 "ran" 반환.
    """
    captured: list = []

    def fake_write_skip(reason: str) -> None:
        captured.append(reason)

    with (
        patch.object(dp, "_write_skip_status", side_effect=fake_write_skip),
        patch.object(dp, "acquire_lock", return_value=False),  # 실제 파이프라인 차단
        patch("sys.argv", ["daily_pipeline.py"] + argv),
    ):
        try:
            dp.main()
        except SystemExit:
            pass

    return captured[0] if captured else "ran"


# ─── 테스트 1: 주말 + --force → 가드 발동 ────────────────────────────────────

def test_weekend_force_still_blocked():
    """주말에 --force 를 써도 주말 가드가 발동돼야 한다.
    /api/update 가 주말에 호출될 때 파이프라인이 실행되지 않아야 함."""
    dp = _load_pipeline()
    with patch.object(dp, "is_weekday", return_value=False):
        reason = _run_main(dp, ["--force"])
    assert reason == "weekend", (
        f"주말 + --force 에서 가드가 발동해야 하는데 '{reason}' 이 반환됨. "
        "--force 가 주말 가드를 우회하면 안 됨."
    )


# ─── 테스트 2: 주말 + --force-weekend → 가드 우회 ────────────────────────────

def test_weekend_force_weekend_bypasses_guard():
    """주말에 --force-weekend 를 쓰면 주말 가드를 우회해야 한다."""
    dp = _load_pipeline()
    with (
        patch.object(dp, "is_weekday", return_value=False),
        patch.object(dp, "_is_already_current", return_value=True),
    ):
        reason = _run_main(dp, ["--force-weekend"])
    # 가드가 우회되면 acquire_lock 으로 진행하다 False 반환 → "ran"
    assert reason == "ran", (
        f"주말 + --force-weekend 는 주말 가드를 우회해야 하는데 '{reason}' 이 반환됨."
    )


# ─── 테스트 3: 주말 + 플래그 없음 → 가드 발동 ───────────────────────────────

def test_weekend_no_flags_blocked():
    """주말에 아무 플래그 없이 실행하면 주말 가드가 발동돼야 한다."""
    dp = _load_pipeline()
    with patch.object(dp, "is_weekday", return_value=False):
        reason = _run_main(dp, [])
    assert reason == "weekend", (
        f"주말 + 플래그 없음에서 가드가 발동해야 하는데 '{reason}' 이 반환됨."
    )


# ─── 테스트 4: 평일 + --force → 이미최신 가드 우회 ──────────────────────────

def test_weekday_force_bypasses_already_current():
    """평일에 --force 를 쓰면 '이미최신' 가드를 우회해야 한다."""
    dp = _load_pipeline()
    with (
        patch.object(dp, "is_weekday", return_value=True),
        patch.object(dp, "_is_trading_day", return_value=True),
        patch.object(dp, "_is_already_current", return_value=True),
    ):
        reason = _run_main(dp, ["--force"])
    assert reason == "ran", (
        f"평일 + --force 는 이미최신 가드를 우회해야 하는데 '{reason}' 이 반환됨."
    )


# ─── 테스트 5: 평일 + 플래그 없음 + 이미최신 → 스킵 ────────────────────────

def test_weekday_already_current_skipped():
    """평일이라도 데이터가 이미 최신이고 --force 없으면 스킵해야 한다."""
    dp = _load_pipeline()
    with (
        patch.object(dp, "is_weekday", return_value=True),
        patch.object(dp, "_is_trading_day", return_value=True),
        patch.object(dp, "_is_already_current", return_value=True),
    ):
        reason = _run_main(dp, [])
    assert reason == "already_current", (
        f"평일 + 이미최신 + 플래그 없음에서 'already_current' 를 기대했는데 '{reason}' 이 반환됨."
    )


# ─── 테스트 7: 공휴일 + 플래그 없음 → 가드 발동 ─────────────────────────────

def test_holiday_no_flags_blocked():
    """평일 공휴일(광복절 등)에 플래그 없이 실행하면 공휴일 가드가 발동돼야 한다."""
    dp = _load_pipeline()
    with (
        patch.object(dp, "is_weekday", return_value=True),         # 평일처럼 보임
        patch.object(dp, "_is_trading_day", return_value=False),   # 하지만 휴장일
    ):
        reason = _run_main(dp, [])
    assert reason == "holiday", (
        f"공휴일 + 플래그 없음에서 'holiday' 를 기대했는데 '{reason}' 이 반환됨."
    )


# ─── 테스트 8: 공휴일 + --force → 가드 여전히 발동 ──────────────────────────

def test_holiday_force_still_blocked():
    """공휴일에 --force 를 써도 공휴일 가드가 발동돼야 한다.
    /api/update 수동 버튼이 공휴일에 눌려도 파이프라인이 실행되지 않아야 함."""
    dp = _load_pipeline()
    with (
        patch.object(dp, "is_weekday", return_value=True),
        patch.object(dp, "_is_trading_day", return_value=False),
    ):
        reason = _run_main(dp, ["--force"])
    assert reason == "holiday", (
        f"공휴일 + --force 에서 가드가 발동해야 하는데 '{reason}' 이 반환됨. "
        "--force 가 공휴일 가드를 우회하면 안 됨."
    )


# ─── 테스트 9: 공휴일 + --force-weekend → 가드 우회 ─────────────────────────

def test_holiday_force_weekend_bypasses():
    """공휴일에 --force-weekend 를 쓰면 공휴일 가드를 우회해야 한다."""
    dp = _load_pipeline()
    with (
        patch.object(dp, "is_weekday", return_value=True),
        patch.object(dp, "_is_trading_day", return_value=False),
        patch.object(dp, "_is_already_current", return_value=True),
    ):
        reason = _run_main(dp, ["--force-weekend"])
    assert reason == "ran", (
        f"공휴일 + --force-weekend 는 공휴일 가드를 우회해야 하는데 '{reason}' 이 반환됨."
    )


# ─── 테스트 6: 소스 코드 레벨 검증 ──────────────────────────────────────────

def test_force_flag_cannot_bypass_weekend_in_source():
    """daily_pipeline.py 소스에서 주말 가드가 args.force_weekend 만으로 우회되는지 확인."""
    src = (BACKEND_DIR / "daily_pipeline.py").read_text(encoding="utf-8")

    # 이전 버그 패턴: "not force" 가 주말 가드에 쓰이면 --force 로 우회 가능
    assert "not is_weekday() and not force" not in src, (
        "주말 가드가 'force'(--force 포함) 변수로 우회 가능한 패턴이 남아 있음. "
        "'args.force_weekend' 만 써야 함."
    )

    # 올바른 패턴이 존재해야 함
    assert "not is_weekday() and not args.force_weekend" in src, (
        "주말 가드가 'args.force_weekend' 만으로 우회하는 패턴이 소스에 없음."
    )
