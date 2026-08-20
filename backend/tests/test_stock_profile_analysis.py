"""
회귀 테스트: 종목 프로파일 카드 섹터·수급·추세 분석 기능.

검증 대상:
- 백엔드 /api/screener가 sector / sector_name 필드를 반환
- KSIC 매핑 헬퍼(_sector_name, _get_ksic_names)가 main.py에 존재
- aiRecommend.ts ScreenerStock에 sector / sector_name 필드 존재
- ScreenerPage.tsx에 분석 헬퍼(getTrendInfo, getSupplyInfo) 존재
- StockInvestorChart에 onDataLoaded prop 존재
- StockProfileCard에 섹터명·추세·수급 섹션 렌더링 코드 존재
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
SCREENER_PATH = ROOT / "src" / "pages" / "ScreenerPage.tsx"
MAIN_PY = ROOT / "backend" / "server" / "main.py"
API_TS = ROOT / "src" / "api" / "aiRecommend.ts"


def _src_screener() -> str:
    return SCREENER_PATH.read_text(encoding="utf-8")


def _src_main() -> str:
    return MAIN_PY.read_text(encoding="utf-8")


def _src_api_ts() -> str:
    return API_TS.read_text(encoding="utf-8")


# ── 백엔드: 섹터 매핑 ────────────────────────────────────────────────────────

def test_main_has_ksic_names_loader():
    """`_get_ksic_names()` 함수가 main.py에 정의돼야 한다."""
    src = _src_main()
    assert "_get_ksic_names" in src, "main.py에 _get_ksic_names 함수 없음"


def test_main_has_sector_name_helper():
    """`_sector_name()` 헬퍼가 main.py에 정의돼야 한다."""
    src = _src_main()
    assert "_sector_name" in src, "main.py에 _sector_name 헬퍼 없음"


def test_main_screener_sql_selects_sector():
    """screener 엔드포인트 SQL이 s.sector를 SELECT 해야 한다."""
    src = _src_main()
    assert "s.sector" in src, "screener SQL에 s.sector SELECT 없음"


def test_main_screener_adds_sector_name_to_stocks():
    """screener 엔드포인트가 stocks 리스트에 sector_name 필드를 추가해야 한다."""
    src = _src_main()
    assert 'sector_name' in src, "main.py screener에 sector_name 필드 추가 없음"
    assert '_sector_name(s.get("sector"))' in src or '_sector_name(' in src, \
        "main.py에 _sector_name() 호출 없음"


def test_main_ksic_names_cache_global():
    """KSIC 이름 캐시가 모듈 레벨 글로벌 변수여야 한다."""
    src = _src_main()
    assert "_ksic_names_cache" in src, "main.py에 _ksic_names_cache 글로벌 캐시 없음"


def test_main_sector_name_uses_3digit_key():
    """_sector_name()이 KSIC 5~6자리 코드를 3자리 키로 슬라이싱해야 한다."""
    src = _src_main()
    assert "[:3]" in src, "_sector_name()에 3자리 슬라이싱 없음"


# ── TypeScript: ScreenerStock 인터페이스 ─────────────────────────────────────

def test_api_ts_screener_stock_has_sector():
    """aiRecommend.ts ScreenerStock에 sector 필드가 있어야 한다."""
    src = _src_api_ts()
    assert "sector?" in src or "sector: " in src, \
        "aiRecommend.ts ScreenerStock에 sector 필드 없음"


def test_api_ts_screener_stock_has_sector_name():
    """aiRecommend.ts ScreenerStock에 sector_name 필드가 있어야 한다."""
    src = _src_api_ts()
    assert "sector_name?" in src or "sector_name:" in src, \
        "aiRecommend.ts ScreenerStock에 sector_name 필드 없음"


# ── ScreenerPage.tsx: 분석 헬퍼 ──────────────────────────────────────────────

def test_screener_has_trend_info_function():
    """ScreenerPage.tsx에 getTrendInfo 함수가 있어야 한다."""
    src = _src_screener()
    assert "getTrendInfo" in src, "ScreenerPage.tsx에 getTrendInfo 함수 없음"


def test_screener_has_supply_info_function():
    """ScreenerPage.tsx에 getSupplyInfo 함수가 있어야 한다."""
    src = _src_screener()
    assert "getSupplyInfo" in src, "ScreenerPage.tsx에 getSupplyInfo 함수 없음"


def test_screener_trend_info_detects_golden_cross():
    """getTrendInfo가 골든크로스 감지 로직을 포함해야 한다."""
    src = _src_screener()
    assert "골든크로스" in src, "getTrendInfo에 골든크로스 감지 없음"
    assert "데드크로스" in src, "getTrendInfo에 데드크로스 감지 없음"


def test_screener_trend_info_has_alignment_labels():
    """getTrendInfo가 정배열·역배열 레이블을 포함해야 한다."""
    src = _src_screener()
    assert "강세 정배열" in src, "getTrendInfo에 '강세 정배열' 레이블 없음"
    assert "약세 역배열" in src, "getTrendInfo에 '약세 역배열' 레이블 없음"


def test_screener_supply_info_uses_foreign_net():
    """getSupplyInfo가 foreign_net 필드를 사용해야 한다."""
    src = _src_screener()
    assert "foreign_net" in src, "getSupplyInfo에 foreign_net 참조 없음"


def test_screener_supply_info_computes_streak():
    """getSupplyInfo가 연속 매수/매도 streak을 계산해야 한다."""
    src = _src_screener()
    assert "streak" in src, "getSupplyInfo에 streak 계산 없음"
    assert "연속" in src, "getSupplyInfo에 '연속' 문자열 없음"


def test_screener_supply_info_5day_window():
    """getSupplyInfo가 최근 5거래일 슬라이스를 사용해야 한다."""
    src = _src_screener()
    assert "slice(-5)" in src, "getSupplyInfo에 최근 5거래일 slice(-5) 없음"


# ── ScreenerPage.tsx: StockInvestorChart onDataLoaded ────────────────────────

def test_investor_chart_has_on_data_loaded_prop():
    """StockInvestorChart가 onDataLoaded 선택적 prop을 받아야 한다."""
    src = _src_screener()
    assert "onDataLoaded" in src, "StockInvestorChart에 onDataLoaded prop 없음"
    # 선택적 prop (? 붙음)
    assert "onDataLoaded?" in src, "onDataLoaded가 선택적(?) prop이 아님"


def test_investor_chart_calls_on_data_loaded_on_load():
    """StockInvestorChart가 데이터 로드 성공 시 onDataLoaded를 호출해야 한다."""
    src = _src_screener()
    assert "onDataLoaded" in src and "?.(d)" in src, \
        "StockInvestorChart에 onDataLoaded?.(d) 호출 없음"


def test_investor_chart_uses_ref_for_callback():
    """StockInvestorChart가 onDataLoaded를 ref로 래핑해 useEffect 의존성에서 안전해야 한다."""
    src = _src_screener()
    assert "onDataLoadedRef" in src, "StockInvestorChart에 onDataLoadedRef 없음"


# ── ScreenerPage.tsx: StockProfileCard 섹터·추세·수급 렌더링 ─────────────────

def test_profile_card_has_chart_data_state():
    """StockProfileCard에 chartData 상태가 있어야 한다."""
    src = _src_screener()
    assert "chartData" in src, "StockProfileCard에 chartData state 없음"
    assert "setChartData" in src, "StockProfileCard에 setChartData setter 없음"


def test_profile_card_passes_on_data_loaded_to_chart():
    """StockProfileCard가 StockInvestorChart에 onDataLoaded={setChartData}를 전달해야 한다."""
    src = _src_screener()
    assert "onDataLoaded={setChartData}" in src, \
        "StockInvestorChart 호출에 onDataLoaded={setChartData} 없음"


def test_profile_card_renders_sector_name():
    """StockProfileCard 헤더에 s.sector_name 렌더링 코드가 있어야 한다."""
    src = _src_screener()
    assert "s.sector_name" in src, "StockProfileCard에 s.sector_name 렌더링 없음"


def test_profile_card_renders_trend_section():
    """StockProfileCard에 추세 분석 섹션이 있어야 한다."""
    src = _src_screener()
    assert "추세 분석" in src, "StockProfileCard에 '추세 분석' 섹션 없음"
    assert "trendInfo" in src, "StockProfileCard에 trendInfo 사용 없음"


def test_profile_card_renders_supply_section():
    """StockProfileCard에 수급 분석 섹션이 있어야 한다."""
    src = _src_screener()
    assert "수급 (최근 5거래일)" in src, "StockProfileCard에 '수급 (최근 5거래일)' 섹션 없음"
    assert "supplyInfo" in src, "StockProfileCard에 supplyInfo 사용 없음"


def test_profile_card_has_overall_summary_line():
    """StockProfileCard에 섹터·추세·수급을 결합한 종합 한줄 요약이 있어야 한다."""
    src = _src_screener()
    # 종합 요약 줄은 '·' 구분자로 섹터·추세·수급을 join
    assert "filter(Boolean).join" in src, \
        "StockProfileCard에 종합 한줄 요약 join 로직 없음"


def test_profile_card_trend_has_color_coding():
    """trendInfo 렌더링이 color 필드를 사용해야 한다."""
    src = _src_screener()
    assert "trendInfo.color" in src, "StockProfileCard에 trendInfo.color 렌더링 없음"
    assert "trendInfo.detail" in src, "StockProfileCard에 trendInfo.detail 렌더링 없음"


def test_profile_card_supply_has_color_coding():
    """supplyInfo 렌더링이 foreignColor 필드를 사용해야 한다."""
    src = _src_screener()
    assert "supplyInfo.foreignColor" in src, \
        "StockProfileCard에 supplyInfo.foreignColor 렌더링 없음"
    assert "supplyInfo.line" in src, "StockProfileCard에 supplyInfo.line 렌더링 없음"
