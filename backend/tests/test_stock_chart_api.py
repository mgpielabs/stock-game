"""
회귀 테스트: /api/stock/{symbol}/chart 엔드포인트 + 프론트 차트 컴포넌트

1. main.py에 /api/stock/{symbol}/chart 엔드포인트 정의 확인
2. SQL이 prices와 investor_trading_kis_detail을 JOIN하는지 확인
3. period_map에 60d/120d/1y가 모두 있는지 확인
4. StockChartPoint 인터페이스가 aiRecommend.ts에 있는지 확인
5. screenerApi.stockChart 메서드가 있는지 확인
6. StockInvestorChart 컴포넌트가 ScreenerPage.tsx에 있는지 확인
7. StockProfileCard 안에 StockInvestorChart 렌더링이 있는지 확인
8. 투자자 데이터 없음 안내 문구가 있는지 확인
9. SQL이 investor_trading_kis(롤링 수집)도 UNION해야 한다
10. 겹치는 날짜는 detail 우선 (NOT EXISTS 패턴)
"""

from pathlib import Path

BACKEND = Path(__file__).parent.parent
MAIN_PY = BACKEND / "server" / "main.py"
SRC = Path(__file__).parent.parent.parent / "src"
AI_RECOMMEND_TS = SRC / "api" / "aiRecommend.ts"
SCREENER_PAGE_TSX = SRC / "pages" / "ScreenerPage.tsx"


def _read_main() -> str:
    return MAIN_PY.read_text(encoding="utf-8")


def _read_api_ts() -> str:
    return AI_RECOMMEND_TS.read_text(encoding="utf-8")


def _read_screener() -> str:
    return SCREENER_PAGE_TSX.read_text(encoding="utf-8")


# ── 백엔드 엔드포인트 ─────────────────────────────────────────────────────────

def test_chart_endpoint_defined_in_main():
    """/api/stock/{symbol}/chart 엔드포인트가 main.py에 정의돼 있어야 한다."""
    src = _read_main()
    assert "/api/stock/{symbol}/chart" in src, \
        "/api/stock/{symbol}/chart 엔드포인트가 main.py에 없음"


def test_chart_endpoint_joins_prices_and_investor():
    """prices와 investor_trading_kis_detail을 JOIN해야 한다."""
    src = _read_main()
    assert "investor_trading_kis_detail" in src, \
        "investor_trading_kis_detail 참조가 main.py에 없음"
    assert "FROM" in src and "prices" in src, \
        "prices 테이블 쿼리가 main.py에 없음"


def test_chart_endpoint_period_map():
    """period_map에 60d, 120d, 1y, 2y, 3y가 모두 있어야 한다."""
    src = _read_main()
    assert '"60d"' in src or "'60d'" in src, "period_map에 60d가 없음"
    assert '"120d"' in src or "'120d'" in src, "period_map에 120d가 없음"
    assert '"1y"' in src or "'1y'" in src, "period_map에 1y가 없음"
    assert '"2y"' in src or "'2y'" in src, "period_map에 2y가 없음"
    assert '"3y"' in src or "'3y'" in src, "period_map에 3y가 없음"


def test_chart_endpoint_returns_foreign_and_inst():
    """외국인/기관/개인 순매수 필드를 반환해야 한다."""
    src = _read_main()
    assert "foreign_net" in src, "foreign_net 필드가 main.py에 없음"
    assert "inst_net" in src, "inst_net 필드가 main.py에 없음"
    assert "indiv_net" in src, "indiv_net 필드가 main.py에 없음"


def test_chart_endpoint_unit_eok_won():
    """수급 차트 단위: DB(백만원) → 억원 변환(÷100)이 있어야 한다."""
    src = _read_main()
    assert "/ 100.0" in src, "백만원→억원 변환(/ 100.0)이 main.py 차트 엔드포인트에 없음"
    # 차트 엔드포인트에서 기존 1÷1000000 방식이 남아있으면 안 됨
    # (main.py 다른 곳에 1000000 문자열이 있을 수 있으나 차트 SQL 내에선 제거됨)
    chart_section_start = src.find("/api/stock/{symbol}/chart")
    chart_section = src[chart_section_start:chart_section_start + 2000] if chart_section_start >= 0 else ""
    assert "/ 1000000" not in chart_section, "차트 엔드포인트 내에 구식 / 1000000 변환이 남아있음"


# ── 프론트엔드 타입 및 API ────────────────────────────────────────────────────

def test_stock_chart_point_interface_in_ts():
    """StockChartPoint 인터페이스가 aiRecommend.ts에 있어야 한다."""
    src = _read_api_ts()
    assert "StockChartPoint" in src, "StockChartPoint 인터페이스가 aiRecommend.ts에 없음"


def test_screener_api_stock_chart_method():
    """screenerApi에 stockChart 메서드가 있어야 한다."""
    src = _read_api_ts()
    assert "stockChart" in src, "stockChart 메서드가 aiRecommend.ts screenerApi에 없음"


def test_stock_chart_api_path():
    """/api/stock/ 경로가 aiRecommend.ts에 있어야 한다."""
    src = _read_api_ts()
    assert "/api/stock/" in src, "/api/stock/ 경로가 aiRecommend.ts에 없음"


# ── 프론트엔드 컴포넌트 ────────────────────────────────────────────────────────

def test_stock_investor_chart_component_in_screener():
    """StockInvestorChart 컴포넌트가 ScreenerPage.tsx에 있어야 한다."""
    src = _read_screener()
    assert "StockInvestorChart" in src, "StockInvestorChart 컴포넌트가 ScreenerPage.tsx에 없음"


def test_stock_profile_card_renders_investor_chart():
    """StockProfileCard 안에서 StockInvestorChart를 렌더링해야 한다."""
    src = _read_screener()
    # StockProfileCard 정의 이후 StockInvestorChart 사용이 있는지
    profile_pos = src.find("export function StockProfileCard")
    assert profile_pos >= 0, "StockProfileCard 정의를 찾을 수 없음"
    # StockProfileCard 이후에 <StockInvestorChart 사용이 있는지
    after_profile = src[profile_pos:]
    assert "<StockInvestorChart" in after_profile, \
        "StockProfileCard 안에 <StockInvestorChart 렌더링이 없음"


def test_no_investor_data_notice():
    """투자자 데이터 없음 안내 문구가 있어야 한다."""
    src = _read_screener()
    assert "투자자 데이터 없음" in src, "'투자자 데이터 없음' 안내 문구가 ScreenerPage.tsx에 없음"


def test_lightweight_charts_used_in_screener():
    """lightweight-charts import가 ScreenerPage.tsx에 있어야 한다."""
    src = _read_screener()
    assert "lightweight-charts" in src, "lightweight-charts import가 ScreenerPage.tsx에 없음"
    assert "CandlestickSeries" in src, "CandlestickSeries import가 없음"


def test_period_buttons_present():
    """60일/120일/1년/2년/3년/전체 차트 기간 버튼이 모두 있어야 한다."""
    src = _read_screener()
    for label in ("60일", "120일", "1년", "2년", "3년", "전체"):
        assert label in src, f"{label} 기간 버튼이 없음"


# ── UNION 패턴 회귀 테스트 ──────────────────────────────────────────────────────

def test_chart_endpoint_also_uses_kis_table():
    """investor_trading_kis(롤링 30일 수집)도 참조해야 한다."""
    src = _read_main()
    assert "investor_trading_kis" in src, \
        "investor_trading_kis 테이블이 main.py 차트 엔드포인트에 없음"


def test_chart_endpoint_uses_union_all():
    """두 테이블을 UNION ALL로 합쳐야 한다."""
    src = _read_main()
    assert "UNION ALL" in src, \
        "UNION ALL 패턴이 main.py에 없음 — detail+kis 두 테이블을 합쳐야 함"


def test_chart_endpoint_not_exists_for_dedup():
    """겹치는 날짜 제거에 NOT EXISTS 패턴을 사용해야 한다."""
    src = _read_main()
    assert "NOT EXISTS" in src, \
        "NOT EXISTS 패턴이 main.py에 없음 — detail 우선 중복 제거 로직 필요"


def test_chart_endpoint_kis_column_mapping():
    """investor_trading_kis의 컬럼명(foreign_net_value 등)을 올바르게 매핑해야 한다."""
    src = _read_main()
    assert "foreign_net_value" in src, \
        "foreign_net_value(kis 테이블 컬럼) 매핑이 main.py에 없음"
    assert "inst_net_value" in src, \
        "inst_net_value(kis 테이블 컬럼) 매핑이 main.py에 없음"
    assert "indiv_net_value" in src, \
        "indiv_net_value(kis 테이블 컬럼) 매핑이 main.py에 없음"


# ── 신규 기능 회귀 테스트 (단위 억원 / 거래량 / 확대 모달) ──────────────────────────

def test_chart_endpoint_includes_volume():
    """prices.volume이 chart API 응답에 포함돼야 한다."""
    src = _read_main()
    assert "p.volume" in src, "p.volume SELECT가 main.py chart 엔드포인트에 없음"
    assert '"volume"' in src or "'volume'" in src, \
        "volume 필드가 chart 반환 딕셔너리에 없음"


def test_stock_chart_point_has_volume_field():
    """StockChartPoint 인터페이스에 volume 필드가 있어야 한다."""
    src = _read_api_ts()
    assert "volume" in src, "volume 필드가 StockChartPoint에 없음"


def test_volume_bar_in_screener():
    """거래량 바 차트가 ScreenerPage.tsx에 있어야 한다."""
    src = _read_screener()
    assert "volume" in src, "volume 참조가 ScreenerPage에 없음"
    assert "거래량" in src, "'거래량' 레이블이 ScreenerPage에 없음"


def test_chart_expand_button_in_screener():
    """차트 확대 버튼/모달 기능이 ScreenerPage.tsx에 있어야 한다."""
    src = _read_screener()
    assert "expanded" in src or "setExpanded" in src, \
        "차트 확대(expanded) 상태가 ScreenerPage에 없음"
    assert "Escape" in src, "ESC 키 닫기 기능이 ScreenerPage에 없음"


# ── lightweight-charts 구현 회귀 테스트 ────────────────────────────────────────

def test_candlestick_series_in_screener():
    """CandlestickSeries가 ScreenerPage.tsx에 사용돼야 한다."""
    src = _read_screener()
    assert "CandlestickSeries" in src, "CandlestickSeries가 ScreenerPage에 없음"


def test_ma_lines_in_screener():
    """MA5/MA20/MA60 이동평균선 참조가 ScreenerPage.tsx에 있어야 한다."""
    src = _read_screener()
    assert "MA5" in src or "ma5" in src, "MA5 참조가 ScreenerPage에 없음"
    assert "MA20" in src or "ma20" in src, "MA20 참조가 ScreenerPage에 없음"
    assert "MA60" in src or "ma60" in src, "MA60 참조가 ScreenerPage에 없음"


def test_korean_candle_colors_in_screener():
    """한국식 캔들 색상(양봉=빨강/음봉=파랑)이 ScreenerPage.tsx에 있어야 한다."""
    src = _read_screener()
    assert "#ef4444" in src, "양봉 색상(#ef4444 빨강)이 ScreenerPage에 없음"
    assert "#3b82f6" in src, "음봉 색상(#3b82f6 파랑)이 ScreenerPage에 없음"


def test_set_visible_range_in_screener():
    """setVisibleRange 호출이 ScreenerPage.tsx에 있어야 한다 (기간 버튼 동작)."""
    src = _read_screener()
    assert "setVisibleRange" in src, \
        "setVisibleRange가 ScreenerPage에 없음 — 기간 버튼은 re-fetch 없이 visible range만 조정해야 함"


def test_stock_chart_point_has_ohlc_fields():
    """StockChartPoint에 open/high/low 필드가 있어야 한다."""
    src = _read_api_ts()
    assert "open" in src, "open 필드가 StockChartPoint에 없음"
    assert "high" in src, "high 필드가 StockChartPoint에 없음"
    assert "low" in src, "low 필드가 StockChartPoint에 없음"


def test_chart_endpoint_includes_ohlc():
    """main.py chart 엔드포인트 SQL에 open/high/low가 SELECT되어야 한다."""
    src = _read_main()
    chart_start = src.find("/api/stock/{symbol}/chart")
    assert chart_start >= 0, "chart 엔드포인트를 찾을 수 없음"
    section = src[chart_start:chart_start + 3000]
    assert "p.open" in section or "open" in section, "open이 chart SQL에 없음"
    assert "p.high" in section or "high" in section, "high가 chart SQL에 없음"
    assert "p.low" in section or "low" in section, "low가 chart SQL에 없음"


def test_stock_investor_chart_accepts_name_prop():
    """StockInvestorChart 컴포넌트가 name prop을 받아야 한다."""
    src = _read_screener()
    assert "name = ''" in src or 'name = ""' in src, \
        "StockInvestorChart에 name 기본값이 없음 — name?: string 선택 prop 이어야 함"
    assert "name?: string" in src, \
        "StockInvestorChart에 name?: string prop 선언이 없음"


def test_chart_modal_header_shows_name():
    """차트 확대 모달 헤더에 종목명(name)이 포함되어야 한다."""
    src = _read_screener()
    # 모달 헤더에서 name이 포함된 표시식이 있어야 함
    assert "name ? ` ${name}` : ''" in src or "name ? ` ${name}`" in src, \
        "차트 모달 헤더에 종목명 표시식이 없음 — {symbol}{name ? ` ${name}` : ''} 차트 패턴 필요"


def test_chart_inline_header_shows_name():
    """인라인 차트 헤더(모달 아닌 카드 내)에도 종목명이 표시되어야 한다."""
    src = _read_screener()
    # 인라인 헤더와 모달 헤더 양쪽에서 name 조건식이 사용되는지 — 최소 2회 등장
    count = src.count("name ? ` ${name}` : ''")
    assert count >= 2, \
        f"종목명 표시식이 {count}개뿐 — 인라인 헤더와 모달 헤더 양쪽(최소 2개) 필요"


def test_stock_investor_chart_call_passes_name():
    """StockProfileCard 내에서 StockInvestorChart를 호출할 때 name prop을 전달해야 한다."""
    src = _read_screener()
    assert "StockInvestorChart symbol={s.symbol} name={s.name}" in src, \
        "StockInvestorChart 호출에 name={s.name} 전달이 없음"
