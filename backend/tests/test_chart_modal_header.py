"""
회귀 테스트: AIRecommendPage.tsx 차트 확대 모달 헤더 종목코드+종목명 (항목 D)

검증 항목:
  1. fullscreen 모달 포털 안에 symbol 표시 코드 존재
  2. fullscreen 모달 포털 안에 name 표시 코드 존재
  3. chartFullscreen state가 정의됨
  4. createPortal이 fullscreen 모달에 사용됨
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
AI_PAGE = ROOT / "src" / "pages" / "AIRecommendPage.tsx"

# 실제 코드 패턴: chartFullscreen && detail && createPortal(
_PORTAL_PATTERN = "chartFullscreen && detail && createPortal"


def _src() -> str:
    return AI_PAGE.read_text(encoding="utf-8")


# ── 1/2. fullscreen 모달에 symbol + name 표시 ─────────────────────

def test_fullscreen_modal_shows_symbol():
    """차트 fullscreen 모달 헤더에 symbol 변수를 표시하는 코드가 있어야 한다."""
    src = _src()
    idx_fullscreen = src.find(_PORTAL_PATTERN)
    assert idx_fullscreen != -1, (
        f"chartFullscreen createPortal 블록을 찾을 수 없음 (패턴: {_PORTAL_PATTERN!r})"
    )
    # 모달 내부(약 700자)에 symbol 참조가 있어야 함
    modal_snippet = src[idx_fullscreen : idx_fullscreen + 700]
    assert "{symbol}" in modal_snippet, (
        "fullscreen 모달 헤더에 {symbol} 표시 코드 없음 — "
        "확대 모달에 종목코드가 표시되지 않을 수 있음"
    )


def test_fullscreen_modal_shows_name():
    """차트 fullscreen 모달 헤더에 name 변수를 표시하는 코드가 있어야 한다."""
    src = _src()
    idx_fullscreen = src.find(_PORTAL_PATTERN)
    assert idx_fullscreen != -1, (
        f"chartFullscreen createPortal 블록을 찾을 수 없음 (패턴: {_PORTAL_PATTERN!r})"
    )
    modal_snippet = src[idx_fullscreen : idx_fullscreen + 700]
    assert "{name" in modal_snippet, (
        "fullscreen 모달 헤더에 name 표시 코드 없음 — "
        "확대 모달에 종목명이 표시되지 않을 수 있음"
    )


# ── 3. chartFullscreen state ──────────────────────────────────────

def test_chart_fullscreen_state_defined():
    """AIRecommendPage에 chartFullscreen useState가 정의돼 있어야 한다."""
    src = _src()
    assert "chartFullscreen" in src, "chartFullscreen state가 AIRecommendPage에 없음"
    assert "setChartFullscreen" in src, "setChartFullscreen setter가 없음"


# ── 4. createPortal 사용 ──────────────────────────────────────────

def test_fullscreen_uses_create_portal():
    """차트 fullscreen 모달이 createPortal을 사용해야 한다."""
    src = _src()
    assert "createPortal" in src, "AIRecommendPage에 createPortal import/사용 없음"
    assert _PORTAL_PATTERN in src, (
        f"chartFullscreen 모달이 createPortal을 사용하지 않음 (패턴: {_PORTAL_PATTERN!r})"
    )


# ── 5. 닫기 버튼 ─────────────────────────────────────────────────

def test_fullscreen_modal_has_close_button():
    """fullscreen 모달에 닫기 버튼이 있어야 한다."""
    src = _src()
    idx_fullscreen = src.find(_PORTAL_PATTERN)
    assert idx_fullscreen != -1, (
        f"chartFullscreen createPortal 블록 없음 (패턴: {_PORTAL_PATTERN!r})"
    )
    modal_snippet = src[idx_fullscreen : idx_fullscreen + 900]
    assert "setChartFullscreen(false)" in modal_snippet, (
        "fullscreen 모달에 닫기(setChartFullscreen(false)) 버튼 없음"
    )
