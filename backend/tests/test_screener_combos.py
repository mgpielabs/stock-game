"""
회귀 테스트: ScreenerPage.tsx COMBO_PRESETS 구조 검증.

- 프리셋 6개 존재 확인
- 2개의 3-필터 조합 키 정확성
- (3조건) 라벨 포함 여부
- oosRate 형식
- 프리셋 클릭 시 자동 정렬 설정 (3-필터→score_60d, 2-필터→dividend_yield)
- 60d 스코어 컬럼 및 정렬 옵션 존재 확인
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent

SCREENER_PATH = ROOT / "src" / "pages" / "ScreenerPage.tsx"
MAIN_PY = ROOT / "backend" / "server" / "main.py"


def _read_screener() -> str:
    return SCREENER_PATH.read_text(encoding="utf-8")


def test_combo_presets_has_six_entries():
    """COMBO_PRESETS에 6개의 프리셋이 있어야 한다."""
    src = _read_screener()
    # oosRate: '숫자%' 패턴으로 실제 값 개수 카운트 (interface 정의 제외)
    count = len(re.findall(r"oosRate:\s*'[^']*'", src))
    assert count == 6, f"COMBO_PRESETS oosRate 개수가 {count}개 — 6개 필요"


def test_three_filter_preset_high_dividend_low_pbr_60d():
    """고배당+저PBR+60d상위20% 프리셋이 정확한 키를 가져야 한다."""
    src = _read_screener()
    assert "'high_dividend', 'low_pbr', 'score_60d_top20'" in src, (
        "고배당+저PBR+60d상위20% 3-필터 프리셋 키가 없거나 순서가 틀림"
    )


def test_three_filter_preset_high_dividend_low_per_60d():
    """고배당+저PER+60d상위10% 프리셋이 정확한 키를 가져야 한다."""
    src = _read_screener()
    assert "'high_dividend', 'low_per', 'score_60d_top10'" in src, (
        "고배당+저PER+60d상위10% 3-필터 프리셋 키가 없거나 순서가 틀림"
    )


def test_three_filter_labels_contain_marker():
    """3-필터 조합 2개 모두 (3조건) 라벨을 포함해야 한다."""
    src = _read_screener()
    count = src.count("(3조건)")
    assert count >= 2, f"(3조건) 라벨이 {count}개 — 최소 2개 필요"


def test_all_oos_rates_have_percent_sign():
    """모든 oosRate 값이 % 기호로 끝나야 한다."""
    src = _read_screener()
    # oosRate: '숫자%' 패턴 찾기
    matches = re.findall(r"oosRate:\s*'([^']*)'", src)
    assert len(matches) == 6, f"oosRate 파싱 개수가 {len(matches)}개 — 6개 필요"
    for rate in matches:
        assert rate.endswith("%"), f"oosRate '{rate}'가 %로 끝나지 않음"


def test_combo_presets_sorted_descending():
    """COMBO_PRESETS가 OOS 승률 내림차순으로 정렬돼 있어야 한다."""
    src = _read_screener()
    rates_raw = re.findall(r"oosRate:\s*'([^']*)'", src)
    rates = [float(r.replace("%", "")) for r in rates_raw]
    assert rates == sorted(rates, reverse=True), (
        f"COMBO_PRESETS OOS 승률 순서가 내림차순 아님: {rates}"
    )


def test_43_combo_reference_in_screener():
    """43조합 검증 참조 문구가 있어야 한다."""
    src = _read_screener()
    assert "43조합" in src, "ScreenerPage.tsx에 43조합 참조 없음"


# ── 프리셋 자동 정렬 테스트 ──────────────────────────────────────────────────

def test_apply_preset_sets_score_60d_for_3filter():
    """3-필터 프리셋 클릭 시 score_60d 정렬이 설정돼야 한다."""
    src = _read_screener()
    # applyPreset 함수 내부에 score_60d 정렬 로직이 있어야 함
    assert "'score_60d'" in src or '"score_60d"' in src, "score_60d 정렬 값이 소스에 없음"
    # 3-필터 조건부 분기가 존재해야 함
    assert "preset.keys.length" in src, "preset.keys.length 조건문이 applyPreset에 없음"


def test_apply_preset_sets_dividend_yield_for_2filter():
    """2-필터 프리셋 클릭 시 dividend_yield 정렬이 설정돼야 한다."""
    src = _read_screener()
    assert "'dividend_yield'" in src or '"dividend_yield"' in src, (
        "dividend_yield 정렬 값이 소스에 없음"
    )


def test_apply_preset_sets_desc_direction():
    """프리셋 클릭 시 내림차순(desc)으로 정렬 방향이 설정돼야 한다."""
    src = _read_screener()
    # applyPreset 함수 내에 'desc' 설정이 있어야 함 (setSortDir 호출)
    assert "setSortDir('desc')" in src or 'setSortDir("desc")' in src, (
        "applyPreset에 setSortDir('desc') 호출 없음"
    )


# ── 60d 스코어 컬럼 / 정렬 옵션 테스트 ─────────────────────────────────────

def test_score_60d_sort_option_in_select():
    """정렬 select에 score_60d 옵션이 있어야 한다."""
    src = _read_screener()
    assert 'value="score_60d"' in src, "정렬 select에 score_60d 옵션 없음"


def test_score_60d_column_header_in_table():
    """테이블에 60d 스코어 컬럼 헤더가 있어야 한다."""
    src = _read_screener()
    assert "60d 스코어" in src, "테이블 헤더에 '60d 스코어' 없음"


def test_score_60d_column_has_tooltip():
    """60d 스코어 컬럼 헤더에 툴팁(title 속성)이 있어야 한다."""
    src = _read_screener()
    # 툴팁이 추가됐는지 핵심 문구로 확인
    assert "CatBoost(80%)" in src and "XGBoost(20%)" in src, (
        "60d 스코어 툴팁에 앙상블 가중치 설명(CatBoost 80%, XGBoost 20%) 없음"
    )
    assert "90th percentile" in src or "percentile" in src, (
        "60d 스코어 툴팁에 백분위 설명 없음"
    )
    assert "ⓘ" in src, "60d 스코어 헤더에 ⓘ 마크 없음"


def test_score_60d_rendered_in_stock_row():
    """StockRow가 score_60d 값을 렌더링해야 한다."""
    src = _read_screener()
    assert "score_60d" in src, "StockRow에 score_60d 렌더링 없음"
    # 퍼센트 형식으로 표시하는 코드가 있어야 함
    assert "score_60d * 100" in src or "score_60d*100" in src, (
        "score_60d를 % 형식으로 변환하는 코드 없음"
    )


def test_score_60d_in_backend_sort_key_map():
    """백엔드 sort_key_map에 score_60d가 있어야 한다."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert '"score_60d"' in src or "'score_60d'" in src, (
        "backend main.py sort_key_map에 score_60d 없음"
    )
    # lambda 형태로 정의돼 있어야 함
    assert 'sort_key_map' in src, "sort_key_map 자체가 없음"


# ── 교차 분석 테스트 ──────────────────────────────────────────────────────────

def test_cross_analysis_endpoint_exists_in_backend():
    """백엔드에 /api/screener/cross-analysis 엔드포인트가 정의돼 있어야 한다."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert '/api/screener/cross-analysis' in src, (
        "backend main.py에 /api/screener/cross-analysis 엔드포인트 없음"
    )


def test_cross_analysis_endpoint_has_six_presets():
    """교차 분석 엔드포인트 내부에 6개 CROSS_PRESETS가 정의돼 있어야 한다."""
    src = MAIN_PY.read_text(encoding="utf-8")
    # CROSS_PRESETS 섹션 내 oos_rate 필드 개수로 확인
    count = len(re.findall(r'"oos_rate":\s*"[^"]*%"', src))
    assert count >= 6, f"cross-analysis 엔드포인트 내 oos_rate 항목이 {count}개 — 6개 필요"


def test_cross_analysis_endpoint_has_combo_flags():
    """교차 분석 엔드포인트 응답에 combo_flags 필드가 포함돼야 한다."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert '"combo_flags"' in src or "'combo_flags'" in src, (
        "cross-analysis 응답에 combo_flags 필드 없음"
    )
    assert '"combo_count"' in src or "'combo_count'" in src, (
        "cross-analysis 응답에 combo_count 필드 없음"
    )


def test_cross_analysis_frontend_has_toggle_button():
    """프론트엔드에 교차 분석 토글 버튼이 있어야 한다."""
    src = _read_screener()
    assert "교차 분석" in src, "ScreenerPage.tsx에 '교차 분석' 텍스트 없음"
    assert "handleToggleCrossMode" in src, "handleToggleCrossMode 핸들러가 없음"
    assert "crossMode" in src, "crossMode state가 없음"


def test_cross_analysis_frontend_has_combo_badges():
    """프론트엔드 교차 분석 뷰에 조합 배지(N번✅/—) 렌더링 코드가 있어야 한다."""
    src = _read_screener()
    # combo_flags 순회로 각 조합 배지를 렌더링하는 코드
    assert "combo_flags" in src, "ScreenerPage.tsx에 combo_flags 렌더링 없음"
    assert "combo_count" in src, "ScreenerPage.tsx에 combo_count 렌더링 없음"
    # combo_flags 순회 + 번/✅/— 패턴이 함께 있어야 함
    assert "번{flag" in src or "combo_flags.map" in src, (
        "조합 배지 순회(combo_flags.map) 코드가 없음"
    )
    assert "✅" in src and "—" in src, "조합 배지 ✅/— 기호가 없음"


def test_cross_analysis_frontend_has_cross_analysis_response_type():
    """aiRecommend.ts에 CrossAnalysisResponse 타입이 정의돼 있어야 한다."""
    ts_path = ROOT / "src" / "api" / "aiRecommend.ts"
    src = ts_path.read_text(encoding="utf-8")
    assert "CrossAnalysisResponse" in src, "aiRecommend.ts에 CrossAnalysisResponse 타입 없음"
    assert "CrossAnalysisStock" in src, "aiRecommend.ts에 CrossAnalysisStock 타입 없음"
    assert "CrossAnalysisPreset" in src, "aiRecommend.ts에 CrossAnalysisPreset 타입 없음"
    assert "crossAnalysis" in src, "aiRecommend.ts screenerApi에 crossAnalysis 메서드 없음"


def test_cross_analysis_presets_match_combo_presets_order():
    """백엔드 CROSS_PRESETS와 프론트 COMBO_PRESETS의 OOS 승률 순서가 일치해야 한다."""
    backend_src = MAIN_PY.read_text(encoding="utf-8")
    frontend_src = _read_screener()

    # 백엔드에서 교차 분석 섹션의 oos_rate 추출
    backend_rates_raw = re.findall(r'"oos_rate":\s*"([^"]*%)"', backend_src)
    # 프론트에서 COMBO_PRESETS oosRate 추출
    frontend_rates_raw = re.findall(r"oosRate:\s*'([^']*)'", frontend_src)

    assert len(backend_rates_raw) >= 6, f"백엔드 oos_rate가 {len(backend_rates_raw)}개 — 6개 필요"
    assert len(frontend_rates_raw) == 6, f"프론트 oosRate가 {len(frontend_rates_raw)}개 — 6개 필요"

    # 백엔드 첫 6개와 프론트 6개가 같은 순서인지 확인
    backend_first6 = backend_rates_raw[:6]
    assert backend_first6 == frontend_rates_raw, (
        f"백엔드 CROSS_PRESETS OOS 승률 순서가 프론트 COMBO_PRESETS와 다름:\n"
        f"  백엔드: {backend_first6}\n  프론트: {frontend_rates_raw}"
    )


def test_cross_analysis_sort_state_exists():
    """교차 분석 뷰에 crossSortBy 정렬 상태가 있어야 한다."""
    src = _read_screener()
    assert "crossSortBy" in src, "ScreenerPage.tsx에 crossSortBy 상태가 없음"
    assert "setCrossSortBy" in src, "ScreenerPage.tsx에 setCrossSortBy setter가 없음"
    # 세 가지 정렬 값이 모두 존재해야 함
    assert "'combo_count'" in src, "crossSortBy 'combo_count' 값이 없음"
    assert "'score_60d'" in src or '"score_60d"' in src, "crossSortBy 'score_60d' 값이 없음"


def test_cross_analysis_sort_dropdown_has_three_options():
    """교차 분석 정렬 드롭다운에 3가지 옵션이 있어야 한다."""
    src = _read_screener()
    assert "value=\"combo_count\"" in src or "value='combo_count'" in src, (
        "정렬 드롭다운에 'combo_count' 옵션 없음"
    )
    assert "60d 모델 스코어" in src, "정렬 드롭다운에 '60d 모델 스코어' 옵션 없음"
    assert "배당수익률(실질)" in src, "정렬 드롭다운에 '배당수익률(실질)' 옵션 없음"
    assert "sortedCrossStocks" in src, "sortedCrossStocks useMemo 파생값이 없음"
