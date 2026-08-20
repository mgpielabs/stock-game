"""
회귀 테스트: AIRecommendPage.tsx BacktestSection 해석 로직 검증.

- chartInterpretation 로직의 TypeScript 구현과 대응하는 Python 버전으로 동작 검증
- StatCard grade/desc 문자열이 코드에 실제로 존재하는지 확인
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
AI_PAGE = ROOT / "src" / "pages" / "AIRecommendPage.tsx"


def _read_ai_page() -> str:
    return AI_PAGE.read_text(encoding="utf-8")


# ── chartInterpretation Python 대응 로직 ─────────────────────────────────────

def chart_interpretation_py(dates: list, values: list) -> str:
    """AIRecommendPage.tsx의 chartInterpretation() TypeScript 함수와 동일 로직."""
    if not values or not dates:
        return ''
    current = values[-1]
    if current < 0:
        return f"원금 손실 구간 진입 (현재 {current:.1f}%)."
    peak = max(values)
    drop_from_peak = current - peak
    if peak > 2 and drop_from_peak < -2:
        peak_idx = values.index(peak)
        peak_date = dates[peak_idx] if peak_idx < len(dates) else ''
        peak_label = ''
        if len(peak_date) >= 6:
            year = peak_date[:4]
            month = int(peak_date[4:6])
            peak_label = f"{year}년 {month}월 이후 지속 하락 추세."
        return f"최고 +{peak:.1f}%에서 현재 +{current:.1f}%로 하락 중 (고점 대비 {drop_from_peak:.1f}%). {peak_label}"
    n = len(values)
    recent = values[max(0, n - 20):]
    recent_delta = recent[-1] - recent[0]
    if recent_delta > 1:
        trend = '최근 상승 추세.'
    elif recent_delta < -1:
        trend = '최근 하락 추세.'
    else:
        trend = '최근 횡보.'
    return f"현재 +{current:.1f}%. {trend}"


# ── 해석 로직 단위 테스트 ──────────────────────────────────────────────────

def test_negative_return_shows_loss_message():
    """음수 수익률이면 '원금 손실 구간 진입' 메시지를 반환한다."""
    result = chart_interpretation_py(['20250101'], [-5.3])
    assert '원금 손실 구간 진입' in result
    assert '-5.3%' in result


def test_drop_from_peak_shows_drawdown():
    """고점 대비 하락 중이면 최고값·현재값·하락폭을 포함한다."""
    dates = [f'20250{i:02d}01' for i in range(1, 6)]
    values = [10.0, 50.0, 74.1, 40.0, 27.0]
    result = chart_interpretation_py(dates, values)
    assert '74.1%' in result
    assert '27.0%' in result
    assert '-47.1%' in result


def test_rising_trend():
    """최근 20일 상승 추세면 '최근 상승 추세.' 포함한다."""
    dates = [f'2025010{i}' for i in range(1, 6)]
    values = [5.0, 10.0, 15.0, 20.0, 25.0]
    result = chart_interpretation_py(dates, values)
    assert '최근 상승 추세.' in result


def test_flat_trend():
    """최근 변화가 1% 이내면 '최근 횡보.' 포함한다."""
    dates = ['20250101', '20250102', '20250103']
    values = [20.0, 20.3, 20.5]
    result = chart_interpretation_py(dates, values)
    assert '최근 횡보.' in result


def test_empty_values_returns_empty():
    """값 없으면 빈 문자열 반환."""
    assert chart_interpretation_py([], []) == ''


# ── 오늘의 대시보드 컴포넌트 존재 확인 ──────────────────────────────────────
# (구 BacktestSection/StatCard 제거 후 신규 대시보드 5-섹션 구조 검증)

def test_dashboard_section_components_in_source():
    """오늘의 대시보드 5개 섹션 컴포넌트가 소스에 정의돼 있어야 한다."""
    src = _read_ai_page()
    assert 'function MarketBanner(' in src, "MarketBanner 컴포넌트 없음"
    assert 'function Open60dSection(' in src, "Open60dSection 컴포넌트 없음"
    assert 'function SectorFlowCard(' in src, "SectorFlowCard 컴포넌트 없음"
    assert 'function PresetShortcuts(' in src, "PresetShortcuts 컴포넌트 없음"
    assert 'function SystemStatusSection(' in src, "SystemStatusSection 컴포넌트 없음"


def test_sector_flow_momentum_labels_in_source():
    """섹터 자금 흐름 모멘텀 라벨 4가지가 소스에 있어야 한다."""
    src = _read_ai_page()
    assert '지속 유입' in src, "'지속 유입' 라벨 없음"
    assert '반전 유입' in src, "'반전 유입' 라벨 없음"
    assert '지속 유출' in src, "'지속 유출' 라벨 없음"
    assert '반전 유출' in src, "'반전 유출' 라벨 없음"


def test_system_status_strings_in_source():
    """SystemStatusSection 상태 문자열이 소스에 있어야 한다."""
    src = _read_ai_page()
    assert '시스템 상태' in src, "'시스템 상태' 헤더 없음"
    assert '재학습 권장' in src, "'재학습 권장' 경고 문자열 없음"
    assert 'retrain_due' in src, "retrain_due 필드 참조 없음"


def test_live_p10_display_in_source():
    """라이브 P@10 표시 코드가 소스에 있어야 한다."""
    src = _read_ai_page()
    assert '5d 라이브 P@10' in src, "'5d 라이브 P@10' 표시 없음"
    assert 'LivePerformanceResponse' in src, "LivePerformanceResponse 타입 임포트 없음"
    assert 'livePerf' in src, "livePerf 상태변수 없음"


def test_open60d_days_display_in_source():
    """60d 포지션 경과일/잔여일 표시 코드가 소스에 있어야 한다."""
    src = _read_ai_page()
    assert '60d 포지션 현황' in src, "'60d 포지션 현황' 헤더 없음"
    assert 'remaining_days' in src, "remaining_days 필드 참조 없음"
    assert 'elapsed_days' in src, "elapsed_days 필드 참조 없음"


def test_prices_stale_display_in_source():
    """prices_stale 상태 표시 코드가 소스에 있어야 한다."""
    src = _read_ai_page()
    assert 'prices_stale' in src, "prices_stale 필드 참조 없음"
    assert 'prices_latest_date' in src, "prices_latest_date 필드 참조 없음"


def test_dashboard_sections_rendered_in_jsx():
    """5개 섹션이 JSX 렌더 블록에 실제로 호출돼 있어야 한다."""
    src = _read_ai_page()
    assert '<MarketBanner' in src, "<MarketBanner> JSX 호출 없음"
    assert '<Open60dSection' in src, "<Open60dSection> JSX 호출 없음"
    assert '<SectorFlowCard' in src, "<SectorFlowCard> JSX 호출 없음"
    assert '<SystemStatusSection' in src, "<SystemStatusSection> JSX 호출 없음"


# ─── ModeSelectPage 카드 병합 테스트 ─────────────────────────────────────────

def _read_mode_select_page() -> str:
    p = ROOT / "src" / "pages" / "ModeSelectPage.tsx"
    return p.read_text(encoding="utf-8")


def test_mode_select_has_single_analysis_card():
    """'종목 분석' 카드가 정확히 1개 있어야 한다."""
    src = _read_mode_select_page()
    assert src.count('>종목 분석<') == 1, "'종목 분석' 제목이 1개 있어야 함"


def test_mode_select_analysis_card_links_to_recommend():
    """'종목 분석' 카드는 /recommend로 이동해야 한다."""
    src = _read_mode_select_page()
    # /recommend to= 가 반드시 있어야 한다
    assert 'to="/recommend"' in src, "/recommend 링크 없음"


def test_mode_select_no_separate_screener_card():
    """'종목 스크리너' 단독 카드가 없어야 한다 (병합됨)."""
    src = _read_mode_select_page()
    assert '>종목 스크리너<' not in src, "별도 '종목 스크리너' 카드가 잔존함"
    assert '>AI 추천 종목<' not in src, "별도 'AI 추천 종목' 카드가 잔존함"


def test_mode_select_description_text():
    """합쳐진 카드에 사용자 지정 설명 문구가 포함돼야 한다."""
    src = _read_mode_select_page()
    assert '대시보드에서 시장 현황' in src, "지정 설명 문구 없음"
    assert '스크리너에서 검증된 조합' in src, "스크리너 설명 문구 없음"


def test_mode_select_three_main_cards():
    """메인 카드가 역사·종목분석·모의투자 3개여야 한다."""
    src = _read_mode_select_page()
    assert '>역사 시뮬레이션<' in src, "역사 시뮬레이션 카드 없음"
    assert '>종목 분석<' in src, "종목 분석 카드 없음"
    assert '>AI 모의투자 추적<' in src, "AI 모의투자 추적 카드 없음"


def test_mode_select_server_status_badge():
    """서버 ON/OFF 상태 배지가 종목 분석 카드에 유지돼야 한다."""
    src = _read_mode_select_page()
    assert '서버 ON' in src, "서버 ON 배지 텍스트 없음"
    assert '서버 OFF' in src, "서버 OFF 배지 텍스트 없음"
    assert 'aiOnline' in src, "aiOnline 상태 참조 없음"
