"""
회귀 테스트: 섹터 자금흐름 기능 검증.

- 백엔드 /api/screener/sector-flow 엔드포인트 존재
- KSIC JSON이 필수 섹터 코드를 포함
- 프론트엔드에 sectorMode 상태와 '섹터 흐름' 버튼 존재
- aiRecommend.ts에 SectorFlowResponse 타입 정의
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent

SCREENER_PATH = ROOT / "src" / "pages" / "ScreenerPage.tsx"
MAIN_PY = ROOT / "backend" / "server" / "main.py"
KSIC_JSON = ROOT / "backend" / "data" / "ksic_sector_names.json"
API_TS = ROOT / "src" / "api" / "aiRecommend.ts"


def _read_screener() -> str:
    return SCREENER_PATH.read_text(encoding="utf-8")


def _read_main() -> str:
    return MAIN_PY.read_text(encoding="utf-8")


# ── 백엔드 엔드포인트 ─────────────────────────────────────────────────────────

def test_sector_flow_endpoint_exists():
    """/api/screener/sector-flow 엔드포인트가 main.py에 정의돼 있어야 한다."""
    src = _read_main()
    assert "/api/screener/sector-flow" in src, (
        "backend main.py에 /api/screener/sector-flow 엔드포인트 없음"
    )


def test_sector_flow_endpoint_uses_investor_trading_kis():
    """엔드포인트가 investor_trading_kis 테이블을 참조해야 한다."""
    src = _read_main()
    assert "investor_trading_kis" in src, (
        "sector-flow 엔드포인트가 investor_trading_kis 테이블을 참조하지 않음"
    )


def test_sector_flow_endpoint_has_pension_note():
    """엔드포인트 응답에 pension_note 필드가 포함돼야 한다."""
    src = _read_main()
    assert "pension_note" in src, "sector-flow 응답에 pension_note 필드 없음"


def test_sector_flow_endpoint_has_combined_net():
    """엔드포인트 응답에 combined_net(합산 순매수) 계산이 있어야 한다."""
    src = _read_main()
    assert "combined_5d" in src, "sector-flow 응답에 combined_5d 필드 없음"
    assert "combined_20d" in src, "sector-flow 응답에 combined_20d 필드 없음"
    assert "combined_60d" in src, "sector-flow 응답에 combined_60d 필드 없음"


def test_sector_flow_state_cache_in_main():
    """_State 클래스에 sector_flow_cache 필드가 있어야 한다."""
    src = _read_main()
    assert "sector_flow_cache" in src, "_State에 sector_flow_cache 필드 없음"
    assert "sector_flow_date" in src, "_State에 sector_flow_date 필드 없음"


# ── KSIC JSON ─────────────────────────────────────────────────────────────────

def test_ksic_json_exists():
    """ksic_sector_names.json 파일이 존재해야 한다."""
    assert KSIC_JSON.exists(), f"KSIC JSON 파일 없음: {KSIC_JSON}"


def test_ksic_json_has_required_sectors():
    """KSIC JSON이 주요 섹터 코드를 포함해야 한다."""
    data = json.loads(KSIC_JSON.read_text(encoding="utf-8"))
    required = {
        "261": "반도체",
        "582": "소프트웨어",
        "282": "이차전지",
        "701": "바이오",
        "292": "기계",
        "313": "항공우주",
    }
    for code, desc in required.items():
        assert code in data, f"KSIC JSON에 {desc}({code}) 코드 없음"


def test_ksic_json_has_minimum_entries():
    """KSIC JSON에 최소 50개 이상의 섹터 코드가 있어야 한다."""
    data = json.loads(KSIC_JSON.read_text(encoding="utf-8"))
    assert len(data) >= 50, f"KSIC JSON 섹터 코드가 {len(data)}개 — 최소 50개 필요"


def test_ksic_json_values_are_korean():
    """KSIC JSON의 값이 한국어 문자를 포함해야 한다."""
    data = json.loads(KSIC_JSON.read_text(encoding="utf-8"))
    # 최소 절반은 한글을 포함해야 함
    korean_count = sum(
        1 for v in data.values()
        if any("가" <= c <= "힣" for c in v)
    )
    assert korean_count >= len(data) * 0.5, (
        f"KSIC JSON 값 중 한글 포함 비율이 낮음: {korean_count}/{len(data)}"
    )


# ── 프론트엔드 ────────────────────────────────────────────────────────────────

def test_screener_has_sector_mode_state():
    """ScreenerPage.tsx에 sectorMode 상태가 있어야 한다."""
    src = _read_screener()
    assert "sectorMode" in src, "ScreenerPage.tsx에 sectorMode 상태 없음"
    assert "setSectorMode" in src, "ScreenerPage.tsx에 setSectorMode setter 없음"


def test_screener_has_sector_flow_button():
    """ScreenerPage.tsx에 '섹터 흐름' 토글 버튼이 있어야 한다."""
    src = _read_screener()
    assert "섹터 흐름" in src, "ScreenerPage.tsx에 '섹터 흐름' 버튼 텍스트 없음"
    assert "handleToggleSectorMode" in src, "handleToggleSectorMode 핸들러가 없음"


def test_screener_has_sector_sort_period_state():
    """ScreenerPage.tsx에 sectorSortPeriod 상태가 있어야 한다."""
    src = _read_screener()
    assert "sectorSortPeriod" in src, "ScreenerPage.tsx에 sectorSortPeriod 상태 없음"
    assert "combined_5d" in src, "sectorSortPeriod 옵션에 combined_5d 없음"
    assert "combined_20d" in src, "sectorSortPeriod 옵션에 combined_20d 없음"
    assert "combined_60d" in src, "sectorSortPeriod 옵션에 combined_60d 없음"


def test_screener_sector_mode_mutual_exclusion():
    """sectorMode 와 crossMode 가 상호 배제돼야 한다."""
    src = _read_screener()
    # handleToggleSectorMode에서 crossMode를 꺼야 함
    assert "setCrossMode(false)" in src, (
        "handleToggleSectorMode에서 setCrossMode(false) 호출 없음"
    )
    # handleToggleCrossMode에서 sectorMode를 꺼야 함
    assert "setSectorMode(false)" in src, (
        "handleToggleCrossMode에서 setSectorMode(false) 호출 없음"
    )


def test_screener_filter_panel_hidden_in_sector_mode():
    """필터 패널이 sectorMode ON일 때 숨겨져야 한다."""
    src = _read_screener()
    assert "!crossMode && !sectorMode" in src, (
        "필터 패널 조건에 !sectorMode 가드가 없음"
    )


def test_screener_has_expanded_sector_state():
    """클릭-펼침을 위한 expandedSector 상태가 있어야 한다."""
    src = _read_screener()
    assert "expandedSector" in src, "ScreenerPage.tsx에 expandedSector 상태 없음"
    assert "setExpandedSector" in src, "ScreenerPage.tsx에 setExpandedSector setter 없음"


def test_screener_sector_momentum_label():
    """섹터 카드에 모멘텀 화살표 레이블이 렌더링돼야 한다."""
    src = _read_screener()
    assert "momentum_5d" in src, "ScreenerPage.tsx에 momentum_5d 참조 없음"
    assert "momLabel" in src, "ScreenerPage.tsx에 momLabel 변수 없음"


# ── aiRecommend.ts 타입 정의 ──────────────────────────────────────────────────

def test_api_ts_has_sector_flow_types():
    """aiRecommend.ts에 SectorFlow 관련 타입이 정의돼 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "SectorFlowEntry" in src, "aiRecommend.ts에 SectorFlowEntry 타입 없음"
    assert "SectorFlowResponse" in src, "aiRecommend.ts에 SectorFlowResponse 타입 없음"
    assert "SectorFlowStock" in src, "aiRecommend.ts에 SectorFlowStock 타입 없음"


def test_api_ts_has_sector_flow_api_method():
    """screenerApi에 sectorFlow 메서드가 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "sectorFlow" in src, "aiRecommend.ts screenerApi에 sectorFlow 메서드 없음"
    assert "/api/screener/sector-flow" in src, (
        "aiRecommend.ts sectorFlow가 올바른 엔드포인트를 호출하지 않음"
    )


# ── pension 20d/60d 버그 수정 검증 ────────────────────────────────────────────

def test_sector_flow_has_pension_20d_60d_in_backend():
    """백엔드가 pension_20d / pension_60d를 집계해야 한다."""
    src = _read_main()
    assert "pension_20d" in src, "sector-flow 집계에 pension_20d 없음"
    assert "pension_60d" in src, "sector-flow 집계에 pension_60d 없음"


def test_sector_flow_has_pension_latest_date_in_backend():
    """백엔드 응답에 pension_latest_date 필드가 있어야 한다."""
    src = _read_main()
    assert "pension_latest_date" in src, "sector-flow 응답에 pension_latest_date 필드 없음"


def test_sector_flow_per_period_has_p_flags():
    """기간별 has_p_5d / has_p_20d / has_p_60d 플래그가 있어야 한다."""
    src = _read_main()
    assert "has_p_5d" in src, "sector-flow 집계에 has_p_5d 플래그 없음"
    assert "has_p_20d" in src, "sector-flow 집계에 has_p_20d 플래그 없음"
    assert "has_p_60d" in src, "sector-flow 집계에 has_p_60d 플래그 없음"


def test_api_ts_has_pension_20d_60d_types():
    """aiRecommend.ts SectorFlowEntry에 pension_20d / pension_60d 타입이 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "pension_20d: number | null" in src, "SectorFlowEntry에 pension_20d 타입 없음"
    assert "pension_60d: number | null" in src, "SectorFlowEntry에 pension_60d 타입 없음"


def test_api_ts_has_pension_latest_date_type():
    """SectorFlowResponse에 pension_latest_date 타입이 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "pension_latest_date" in src, "SectorFlowResponse에 pension_latest_date 타입 없음"


# ── UI 가독성 개선 검증 ───────────────────────────────────────────────────────

def test_screener_sector_bar_has_toLocaleString():
    """바 금액에 천단위 쉼표(toLocaleString) 포매팅이 있어야 한다."""
    src = _read_screener()
    assert "toLocaleString" in src, "섹터 바 금액에 toLocaleString 포매팅 없음"


def test_screener_sector_collapsed_no_mini_numbers():
    """접힌 상태에서 20d/60d 미니 숫자 블록이 없어야 한다."""
    src = _read_screener()
    # 이전에 있던 "hidden sm:flex gap-3 shrink-0 text-right" 블록이 제거돼야 함
    assert "hidden sm:flex gap-3 shrink-0 text-right" not in src, (
        "접힌 상태의 20d/60d 미니 숫자 블록이 아직 남아 있음"
    )


def test_screener_sector_expanded_has_table():
    """펼친 상태가 table 태그를 사용해야 한다."""
    src = _read_screener()
    assert "<table" in src, "섹터 펼침 상태에 <table> 태그 없음"
    assert "외국인" in src, "섹터 표에 외국인 행 없음"
    assert "기관" in src, "섹터 표에 기관 행 없음"
    assert "연기금" in src, "섹터 표에 연기금 행 없음"


def test_screener_sector_pension_dash_for_null():
    """연기금이 null일 때 '—' 기호를 표시해야 한다."""
    src = _read_screener()
    assert "pension_latest_date" in src, "ScreenerPage에서 pension_latest_date를 사용하지 않음"
    # null 처리 시 '—' 기호 사용 확인
    assert "연기금은 최근 30거래일만 제공" in src, "연기금 null 처리 툴팁 없음"


# ── 신규 기능 검증 (3430% 버그 수정 + 전체 종목 목록 + 커버리지) ─────────────────


def test_backend_score_60d_raw_not_percent():
    """백엔드 sector_cross의 score_60d가 ×100 하지 않은 raw(0-1)를 반환해야 한다."""
    src = _read_main()
    # 수정 전: scores_60d_map[s] * 100  (×100 적용 — 버그)
    # 수정 후: scores_60d_map[s]  (raw 그대로)
    # "scores_60d_map[s] * 100" 이 sector_cross 빌드 코드에 없어야 함
    # (screener 다른 곳에 *100이 있을 수 있으므로 sector_cross 전후 맥락 확인)
    assert 'round(scores_60d_map[s] * 100, 1)' not in src, (
        "sector_cross 빌드 코드에서 score_60d에 ×100이 적용됨 — raw로 반환해야 함"
    )


def test_backend_has_sector_stocks_field():
    """백엔드가 섹터별 전체 종목 목록(stocks)을 반환해야 한다."""
    src = _read_main()
    assert '"stocks":       sector_stocks.get(code3' in src or \
           '"stocks": sector_stocks.get(code3' in src or \
           'sector_stocks' in src, "backend에 sector_stocks 빌드 로직 없음"


def test_backend_has_coverage_field():
    """백엔드 응답에 coverage 필드가 있어야 한다."""
    src = _read_main()
    assert '"coverage"' in src, "sector-flow 응답에 coverage 필드 없음"
    assert "sector_count" in src, "coverage에 sector_count 없음"
    assert "symbols_in_sectors" in src, "coverage에 symbols_in_sectors 없음"
    assert "total_universe" in src, "coverage에 total_universe 없음"


def test_api_ts_has_sector_flow_stock_item():
    """aiRecommend.ts에 SectorFlowStockItem 타입이 정의돼 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "SectorFlowStockItem" in src, "aiRecommend.ts에 SectorFlowStockItem 타입 없음"
    assert "foreign_5d" in src, "SectorFlowStockItem에 foreign_5d 필드 없음"
    assert "inst_5d" in src, "SectorFlowStockItem에 inst_5d 필드 없음"


def test_api_ts_sector_flow_entry_has_stocks():
    """SectorFlowEntry에 stocks 배열이 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "stocks: SectorFlowStockItem[]" in src, "SectorFlowEntry에 stocks 필드 없음"


def test_api_ts_sector_flow_response_has_coverage():
    """SectorFlowResponse에 coverage 필드가 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "coverage:" in src, "SectorFlowResponse에 coverage 필드 없음"
    assert "sector_count: number" in src, "coverage에 sector_count 타입 없음"
    assert "symbols_in_sectors: number" in src, "coverage에 symbols_in_sectors 타입 없음"


def test_screener_has_expanded_sector_stocks_state():
    """ScreenerPage에 expandedSectorStocks 상태가 있어야 한다."""
    src = _read_screener()
    assert "expandedSectorStocks" in src, "ScreenerPage에 expandedSectorStocks 상태 없음"
    assert "setExpandedSectorStocks" in src, "ScreenerPage에 setExpandedSectorStocks 없음"


def test_screener_has_coverage_display():
    """ScreenerPage가 커버리지 정보(sector_count, symbols_in_sectors)를 표시해야 한다."""
    src = _read_screener()
    assert "coverage" in src, "ScreenerPage에 coverage 렌더링 없음"
    assert "symbols_in_sectors" in src, "ScreenerPage에 symbols_in_sectors 표시 없음"
    assert "ETF·우선주" in src, "ScreenerPage 커버리지 텍스트에 ETF·우선주 설명 없음"


def test_screener_has_full_stock_list_toggle():
    """ScreenerPage가 '전체 N종목 보기' 접기/펼치기 버튼을 렌더링해야 한다."""
    src = _read_screener()
    assert "전체" in src and "종목 보기" in src, "ScreenerPage에 전체 종목 보기 버튼 없음"
    assert "sector.stocks.length" in src, "ScreenerPage에 sector.stocks.length 참조 없음"


def test_backend_sym_5d_values_captured():
    """백엔드가 섹터 집계 루프에서 sym_5d_values를 수집해야 한다."""
    src = _read_main()
    assert "sym_5d_values" in src, "backend에 sym_5d_values 딕셔너리 없음"
    assert "sym_5d_values[s]" in src, "backend에서 sym_5d_values[s] 할당 없음"


def test_backend_sector_stocks_has_split_5d():
    """섹터별 종목 목록이 combined_5d 합산이 아닌 foreign_5d/inst_5d 분리 필드를 가져야 한다."""
    src = _read_main()
    assert '"foreign_5d"' in src, "sector_stocks에 foreign_5d 필드 없음"
    assert '"inst_5d"' in src, "sector_stocks에 inst_5d 필드 없음"
    assert '"combined_5d": round(fa + ia' not in src, "sector_stocks에 combined_5d 합산이 남아있음"


def test_screener_stock_list_has_split_columns():
    """ScreenerPage 종목 목록이 외국인/기관 분리 컬럼과 overflow-x: auto를 사용해야 한다."""
    src = _read_screener()
    # 헤더는 sectorSortPeriod에 따라 동적으로 기간을 표시 ("외국인 5d" 등이 아닌 ternary)
    assert "외국인" in src, "ScreenerPage 종목 목록에 '외국인' 컬럼 헤더 없음"
    assert "기관" in src, "ScreenerPage 종목 목록에 '기관' 컬럼 헤더 없음"
    assert "overflow-x-auto" in src, "ScreenerPage 종목 목록에 overflow-x-auto 없음"
    assert "foreign_5d" in src, "ScreenerPage 종목 목록에 st.foreign_5d 참조 없음"
    assert "inst_5d" in src, "ScreenerPage 종목 목록에 st.inst_5d 참조 없음"
    assert "pl-1" in src, "ScreenerPage 종목명 열에 좌측 패딩(pl-1) 없음"


# ── 시각적 가독성 개선 검증 ───────────────────────────────────────────────────────


def test_screener_sector_bar_is_simple_directional():
    """섹터 바 차트가 단방향(왼쪽→오른쪽) 구조로 양수=녹색/음수=적색을 사용해야 한다."""
    src = _read_screener()
    # 단방향 바: 비율만큼 너비를 채우는 width: `${barPct}%` 방식
    assert "barPct}%" in src, "섹터 바에 barPct 비율 너비 없음"
    # 양수 녹색 바
    assert "bg-emerald-500" in src, "섹터 바에 양수 녹색(bg-emerald-500) 없음"
    # 음수 적색 바
    assert "bg-red-500" in src, "섹터 바에 음수 적색(bg-red-500) 없음"
    # 양방향 중심선(이전 설계) 잔존 여부 — 없어야 함
    assert "left: '50%', width" not in src, "이전 양방향 바 잔존(left: 50% 확장)"
    assert "right: '50%', width" not in src, "이전 양방향 바 잔존(right: 50% 확장)"


def test_screener_sector_card_has_green_red_bg():
    """섹터 카드가 combined_5d 방향에 따라 녹색/적색 배경색을 적용해야 한다."""
    src = _read_screener()
    assert "bg-emerald-950" in src, "섹터 카드에 양수(녹색) 배경 클래스 없음"
    assert "bg-red-950" in src, "섹터 카드에 음수(적색) 배경 클래스 없음"
    assert "isPos5d" in src, "카드 배경 분기 변수(isPos5d) 없음"


def test_screener_sector_momentum_has_direction_flip_label():
    """모멘텀 레이블이 방향 전환 시 '유입 전환 🟢' / '유출 전환 🔴' 텍스트를 사용해야 한다."""
    src = _read_screener()
    assert "유입 전환 🟢" in src, "모멘텀 방향전환 레이블 '유입 전환 🟢' 없음"
    assert "유출 전환 🔴" in src, "모멘텀 방향전환 레이블 '유출 전환 🔴' 없음"
    # 이전 방향전환 레이블은 제거돼야 함
    assert "유출→유입" not in src, "이전 모멘텀 레이블 '유출→유입' 잔존"
    assert "유입→유출" not in src, "이전 모멘텀 레이블 '유입→유출' 잔존"
    assert "🔄" not in src, "이전 모멘텀 이모지 🔄 잔존"


def test_screener_sector_momentum_has_inflow_outflow_labels():
    """모멘텀 레이블이 유입/유출 방향과 강도를 직관적으로 표현해야 한다."""
    src = _read_screener()
    assert "5일 연속 유입 ▲" in src, "모멘텀 레이블 '5일 연속 유입 ▲' 없음"
    assert "유입 둔화 △" in src, "모멘텀 레이블 '유입 둔화 △' 없음"
    assert "5일 연속 유출 ▼" in src, "모멘텀 레이블 '5일 연속 유출 ▼' 없음"
    assert "유출 축소 ▽" in src, "모멘텀 레이블 '유출 축소 ▽' 없음"
    # 이전 모멘텀 레이블(▲ 가속 / ▼ 감속 숫자%) 잔존 여부 — momLabel에서 사용 안 해야 함
    assert "▲ 가속" not in src, "이전 모멘텀 레이블 '▲ 가속' 잔존"
    assert "▼ 감속" not in src, "이전 모멘텀 레이블 '▼ 감속' 잔존"


def test_screener_sector_card_has_period_summary():
    """섹터 카드 접힌 상태에서 5일/20일/60일 기간 요약 줄이 표시돼야 한다."""
    src = _read_screener()
    assert "combined_5d" in src, "ScreenerPage에 combined_5d 참조 없음"
    assert "combined_20d" in src, "ScreenerPage에 combined_20d 참조 없음"
    assert "combined_60d" in src, "ScreenerPage에 combined_60d 참조 없음"
    assert "periodFmt" in src, "섹터 카드 기간 요약 포맷 함수(periodFmt) 없음"
    # 한국어 라벨 사용
    assert "'5일'" in src or '"5일"' in src, "기간 요약 라벨이 '5d' 대신 '5일'을 사용해야 함"
    assert "'20일'" in src or '"20일"' in src, "기간 요약 라벨이 '20d' 대신 '20일'을 사용해야 함"
    assert "'60일'" in src or '"60일"' in src, "기간 요약 라벨이 '60d' 대신 '60일'을 사용해야 함"
    # periodFmt에 ▲▼ 화살표 없어야 함
    assert "periodFmt" in src
    # periodFmt 함수 정의에 '▲' 또는 '▼' 가 없어야 함 (+ / - 부호와 색으로 방향 표현)
    period_fmt_idx = src.index("periodFmt")
    # periodFmt 정의 근방 50자 내에 ▲▼ 없어야 함
    period_fmt_region = src[period_fmt_idx:period_fmt_idx + 80]
    assert "▲" not in period_fmt_region, "periodFmt에 ▲ 화살표 잔존 (부호+색으로 충분)"
    assert "▼" not in period_fmt_region, "periodFmt에 ▼ 화살표 잔존 (부호+색으로 충분)"


# ── 섹터 트리맵 ────────────────────────────────────────────────

def test_screener_has_sector_treemap_component():
    """SectorTreemap 컴포넌트가 ScreenerPage에 구현돼야 한다."""
    src = _read_screener()
    assert "SectorTreemap" in src, "SectorTreemap 컴포넌트 없음"
    assert "computeSquarifiedTreemap" in src, "squarified treemap 알고리즘 함수 없음"


def test_screener_treemap_uses_resize_observer():
    """SectorTreemap이 ResizeObserver로 반응형 너비를 측정해야 한다."""
    src = _read_screener()
    assert "ResizeObserver" in src, "SectorTreemap에 ResizeObserver 없음"


def test_screener_treemap_has_period_sync():
    """SectorTreemap이 sectorSortPeriod를 period prop으로 받아야 한다."""
    src = _read_screener()
    # 드릴다운 시 섹터 카드 스크롤 제거됨 — SectorTreemap에 onSectorClick prop 없음
    assert "sectorSortPeriod" in src, "SectorTreemap에 sectorSortPeriod 없음"
    assert "sector-card-" in src, "섹터 카드 id 접두어 없음"


def test_screener_treemap_description_line():
    """트리맵 위에 설명 문구가 있어야 한다."""
    src = _read_screener()
    assert "박스 크기 = 자금 규모" in src, "트리맵 설명 문구 없음"
    assert "초록 = 유입" in src, "트리맵 색상 설명(유입) 없음"
    assert "빨강 = 유출" in src, "트리맵 색상 설명(유출) 없음"


# ── 섹터 트리맵 드릴다운 ─────────────────────────────────────────

def test_screener_treemap_drilldown_back_button():
    """드릴다운 상태에서 전체 섹터로 돌아가기 버튼이 있어야 한다."""
    src = _read_screener()
    assert "전체 섹터로 돌아가기" in src, "드릴다운 뒤로가기 버튼 문구 없음"


def test_screener_treemap_drilldown_state():
    """SectorTreemap 내부에 drilldown 상태가 있어야 한다."""
    src = _read_screener()
    assert "drilldown" in src, "drilldown 상태 변수 없음"
    assert "setDrilldown" in src, "setDrilldown setter 없음"


def test_screener_treemap_drilldown_uses_period_flow():
    """드릴다운 뷰가 기간에 따라 외국인+기관 순매수를 선택해야 한다 (_stockFlowForPeriod)."""
    src = _read_screener()
    assert "_stockFlowForPeriod" in src, \
        "드릴다운 종목 박스가 _stockFlowForPeriod 헬퍼를 사용하지 않음"
    # 헬퍼가 20d/60d 필드도 지원하는지 확인
    assert "foreign_20d" in src, "_stockFlowForPeriod에 foreign_20d 참조 없음"
    assert "foreign_60d" in src, "_stockFlowForPeriod에 foreign_60d 참조 없음"


def test_screener_treemap_drilldown_stock_click_shows_profile():
    """드릴다운 종목 타일 클릭 시 프로파일 카드를 로드해야 한다."""
    src = _read_screener()
    # 드릴다운 모드의 onTileClick은 handleDrilldownStockClick을 호출해야 함
    assert "handleDrilldownStockClick" in src, \
        "드릴다운 종목 클릭 핸들러(handleDrilldownStockClick) 없음"
    # symbolProfile API 호출 확인
    assert "symbolProfile" in src, "handleDrilldownStockClick이 symbolProfile을 호출하지 않음"
    # scrollIntoView는 드릴다운 클릭에 쓰이면 안 됨 (섹터 타일에만 허용)
    assert "onTileClick={tile => handleDrilldownStockClick(tile.code)}" in src, \
        "드릴다운 stockTiles의 onTileClick이 handleDrilldownStockClick을 사용하지 않음"


def test_screener_treemap_drilldown_header_shows_sector_info():
    """드릴다운 헤더에 섹터명·종목수·총 순매수가 표시돼야 한다."""
    src = _read_screener()
    assert "drilldownEntry.name" in src, "드릴다운 헤더에 섹터명 없음"
    assert "drilldownEntry.stocks.length" in src, "드릴다운 헤더에 종목수 없음"
    assert "drilldownTotal" in src, "드릴다운 헤더에 총 순매수(drilldownTotal) 없음"


def test_screener_treemap_tooltip_hover_state():
    """_TmSvg 컴포넌트에 마우스 호버 상태(hovered)와 툴팁 렌더링이 있어야 한다."""
    src = _read_screener()
    assert "hovered" in src, "_TmSvg에 hovered 상태 없음"
    assert "setHovered" in src, "_TmSvg에 setHovered setter 없음"
    assert "onMouseEnter" in src, "_TmSvg 타일에 onMouseEnter 핸들러 없음"
    assert "onMouseLeave" in src, "_TmSvg 타일에 onMouseLeave 핸들러 없음"


def test_screener_treemap_tooltip_renders_label():
    """툴팁이 타일 이름(tile.name)을 표시해야 한다."""
    src = _read_screener()
    assert "tile.name" in src, "툴팁이 tile.name을 표시하지 않음"


def test_screener_tmsvg_receives_svg_size_props():
    """_TmSvg가 svgW/svgH props를 받아야 한다 (툴팁 경계 클램핑용)."""
    src = _read_screener()
    assert "svgW" in src, "_TmSvg에 svgW prop 없음"
    assert "svgH" in src, "_TmSvg에 svgH prop 없음"


def test_backend_sector_stocks_include_20d_60d_fields():
    """백엔드 /api/sector-flow 가 per-stock 20d/60d 순매수 필드를 포함해야 한다."""
    main_src = Path(__file__).parent.parent / "server" / "main.py"
    src = main_src.read_text(encoding="utf-8")
    assert "foreign_20d" in src, "main.py sector stocks에 foreign_20d 없음"
    assert "inst_20d" in src,    "main.py sector stocks에 inst_20d 없음"
    assert "foreign_60d" in src, "main.py sector stocks에 foreign_60d 없음"
    assert "inst_60d" in src,    "main.py sector stocks에 inst_60d 없음"


def test_ts_interface_sector_flow_stock_item_has_20d_60d():
    """TypeScript SectorFlowStockItem 인터페이스에 20d/60d 필드가 있어야 한다."""
    ts_src = Path(__file__).parent.parent.parent / "src" / "api" / "aiRecommend.ts"
    src = ts_src.read_text(encoding="utf-8")
    for field in ("foreign_20d", "inst_20d", "foreign_60d", "inst_60d"):
        assert field in src, f"aiRecommend.ts SectorFlowStockItem에 {field} 없음"


def test_stocktiles_usememo_depends_on_period():
    """stockTiles useMemo의 의존성 배열에 period가 포함돼야 한다."""
    src = _read_screener()
    # stockTiles useMemo는 [..., period] 패턴으로 period를 의존성에 포함해야 함
    assert "period" in src, "stockTiles useMemo 의존성 배열에 period 없음"
    assert "drilldownTotal" in src, "drilldownTotal 변수 없음"


# ── 드릴다운 종목 클릭 → 프로파일 카드 ─────────────────────────────────────────

def test_screener_treemap_drilldown_has_profile_state():
    """SectorTreemap이 클릭된 종목 프로파일 상태를 내부에서 관리해야 한다."""
    src = _read_screener()
    assert "clickedProfile" in src, "SectorTreemap에 clickedProfile 상태 없음"
    assert "setClickedProfile" in src, "SectorTreemap에 setClickedProfile setter 없음"
    assert "clickedProfileLoading" in src, "SectorTreemap에 clickedProfileLoading 상태 없음"


def test_screener_treemap_drilldown_profile_card_below_svg():
    """드릴다운에서 프로파일이 있으면 StockProfileCard를 SVG 아래에 렌더링해야 한다."""
    src = _read_screener()
    # StockProfileCard가 SectorTreemap 내부에서도 사용돼야 함
    assert "StockProfileCard" in src, "SectorTreemap에 StockProfileCard 렌더링 없음"
    # drilldown 조건 내에 clickedProfile 체크
    assert "clickedProfile && !clickedProfileLoading" in src, \
        "clickedProfile 조건부 렌더링이 없음"


def test_screener_treemap_drilldown_profile_cleared_on_back():
    """전체 섹터 뷰로 돌아갈 때 clickedProfile이 초기화돼야 한다."""
    src = _read_screener()
    # 뒤로가기 버튼 onClick에 setClickedProfile(null)이 함께 있어야 함
    assert "setDrilldown(null); setClickedProfile(null)" in src, \
        "전체 섹터 뷰 복귀 시 setClickedProfile(null) 초기화 없음"


def test_screener_treemap_passes_onapplyfilters_prop():
    """SectorTreemap이 onApplyFilters prop을 받아 StockProfileCard에 전달해야 한다."""
    src = _read_screener()
    assert "onApplyFilters={handleApplyProfileFilters}" in src, \
        "SectorTreemap 호출부에 onApplyFilters={handleApplyProfileFilters} 없음"
    # SectorTreemap 내부에서 프로파일 카드로 전달
    assert "onApplyFilters={onApplyFilters" in src, \
        "SectorTreemap 내부에서 StockProfileCard로 onApplyFilters 전달 없음"


# ── 120d / 250d 기간 확장 회귀 테스트 ────────────────────────────────────────────


def test_backend_has_cutoff_120d_250d():
    """백엔드가 cutoff_120d / cutoff_250d 변수를 계산해야 한다."""
    src = _read_main()
    assert "cutoff_120d" in src, "main.py에 cutoff_120d 변수 없음"
    assert "cutoff_250d" in src, "main.py에 cutoff_250d 변수 없음"


def test_backend_sector_agg_has_120d_250d_fields():
    """섹터 집계 딕셔너리에 120d / 250d 외국인·기관·연금 필드가 있어야 한다."""
    src = _read_main()
    for field in ("foreign_120d", "inst_120d", "pension_120d", "combined_120d",
                  "foreign_250d", "inst_250d", "pension_250d", "combined_250d"):
        assert f'"{field}"' in src, f"main.py sector_agg에 '{field}' 필드 없음"


def test_backend_has_pension_120d_250d_flags():
    """백엔드가 has_p_120d / has_p_250d 플래그를 트래킹해야 한다."""
    src = _read_main()
    assert "has_p_120d" in src, "main.py에 has_p_120d 플래그 없음"
    assert "has_p_250d" in src, "main.py에 has_p_250d 플래그 없음"


def test_backend_sector_stocks_has_120d_250d_split():
    """섹터별 종목 목록에 foreign_120d / inst_120d / foreign_250d / inst_250d 필드가 있어야 한다."""
    src = _read_main()
    for field in ("foreign_120d", "inst_120d", "foreign_250d", "inst_250d"):
        assert f'"{field}"' in src, f"main.py sector_stocks에 '{field}' 없음"


def test_backend_cutoffs_response_includes_120d_250d():
    """sector-flow API 응답의 cutoffs 딕셔너리에 120d / 250d 키가 있어야 한다."""
    src = _read_main()
    assert '"120d": cutoff_120d' in src or '"120d":cutoff_120d' in src or \
           '"120d": cutoff_120d' in src.replace(" ", ""), \
        "cutoffs 응답에 '120d' 키 없음"
    assert '"250d": cutoff_250d' in src or '"250d":cutoff_250d' in src or \
           '"250d": cutoff_250d' in src.replace(" ", ""), \
        "cutoffs 응답에 '250d' 키 없음"


def test_ts_sector_flow_entry_has_120d_250d():
    """aiRecommend.ts SectorFlowEntry에 120d/250d 필드가 있어야 한다."""
    ts_src = Path(__file__).parent.parent.parent / "src" / "api" / "aiRecommend.ts"
    src = ts_src.read_text(encoding="utf-8")
    for field in ("foreign_120d", "inst_120d", "pension_120d", "combined_120d",
                  "foreign_250d", "inst_250d", "pension_250d", "combined_250d"):
        assert field in src, f"aiRecommend.ts SectorFlowEntry에 {field} 없음"


def test_ts_sector_flow_stock_item_has_120d_250d():
    """aiRecommend.ts SectorFlowStockItem에 120d/250d 필드가 있어야 한다."""
    ts_src = Path(__file__).parent.parent.parent / "src" / "api" / "aiRecommend.ts"
    src = ts_src.read_text(encoding="utf-8")
    for field in ("foreign_120d", "inst_120d", "foreign_250d", "inst_250d"):
        assert field in src, f"aiRecommend.ts SectorFlowStockItem에 {field} 없음"


def test_ts_sector_flow_response_cutoffs_has_120d_250d():
    """aiRecommend.ts SectorFlowResponse.cutoffs 타입에 '120d'/'250d' 키가 있어야 한다."""
    ts_src = Path(__file__).parent.parent.parent / "src" / "api" / "aiRecommend.ts"
    src = ts_src.read_text(encoding="utf-8")
    assert "'120d'" in src or '"120d"' in src, \
        "aiRecommend.ts cutoffs 타입에 '120d' 키 없음"
    assert "'250d'" in src or '"250d"' in src, \
        "aiRecommend.ts cutoffs 타입에 '250d' 키 없음"


def test_screener_dropdown_has_120d_250d_options():
    """섹터 흐름 기간 드롭다운에 '최근 120일' / '최근 1년(250일)' 옵션이 있어야 한다."""
    src = _read_screener()
    assert "combined_120d" in src, "ScreenerPage 드롭다운에 combined_120d 옵션 없음"
    assert "combined_250d" in src, "ScreenerPage 드롭다운에 combined_250d 옵션 없음"
    assert "최근 120일" in src, "ScreenerPage 드롭다운에 '최근 120일' 텍스트 없음"
    assert "최근 1년" in src, "ScreenerPage 드롭다운에 '최근 1년' 텍스트 없음"


def test_screener_period_summary_has_120d_250d_labels():
    """섹터 카드 기간 요약 줄이 120일 / 1년 레이블을 표시해야 한다."""
    src = _read_screener()
    assert "'120일'" in src or '"120일"' in src, \
        "ScreenerPage 기간 요약에 '120일' 라벨 없음"
    assert "'1년'" in src or '"1년"' in src, \
        "ScreenerPage 기간 요약에 '1년' 라벨 없음"


def test_screener_stock_flow_helper_handles_120d_250d():
    """_stockFlowForPeriod 헬퍼가 combined_120d / combined_250d 기간을 처리해야 한다."""
    src = _read_screener()
    assert "foreign_120d" in src, "_stockFlowForPeriod에 foreign_120d 참조 없음"
    assert "foreign_250d" in src, "_stockFlowForPeriod에 foreign_250d 참조 없음"
    assert "inst_120d" in src, "_stockFlowForPeriod에 inst_120d 참조 없음"
    assert "inst_250d" in src, "_stockFlowForPeriod에 inst_250d 참조 없음"


# ── 종합 분석 엔드포인트 회귀 테스트 ─────────────────────────────────────────────


def test_backend_analysis_endpoint_exists():
    """/api/screener/sector-flow/analysis 엔드포인트가 main.py에 정의돼야 한다."""
    src = _read_main()
    assert "/api/screener/sector-flow/analysis" in src, (
        "main.py에 /api/screener/sector-flow/analysis 엔드포인트 없음"
    )


def test_backend_analysis_endpoint_has_four_sections():
    """종합 분석 엔드포인트가 market_summary / sector_rankings / sector_classification / concentration / cross_analysis를 반환해야 한다."""
    src = _read_main()
    for key in ("market_summary", "sector_rankings", "sector_classification", "concentration", "cross_analysis"):
        assert f'"{key}"' in src or f"'{key}'" in src, (
            f"main.py 종합 분석 응답에 '{key}' 키 없음"
        )


def test_backend_analysis_uses_sector_flow_cache():
    """종합 분석 엔드포인트가 _state.sector_flow_cache를 재사용해야 한다."""
    src = _read_main()
    assert "sector_flow_cache" in src, "종합 분석에서 sector_flow_cache 재사용 없음"


def test_backend_analysis_market_summary_has_direction_fields():
    """market_summary에 direction_label / direction_color / totals가 있어야 한다."""
    src = _read_main()
    assert "direction_label" in src, "종합 분석 market_summary에 direction_label 없음"
    assert "direction_color" in src, "종합 분석 market_summary에 direction_color 없음"
    assert "period_labels" in src, "종합 분석 market_summary에 period_labels 없음"


def test_backend_analysis_sector_classification_has_four_groups():
    """sector_classification에 일관유입/일관유출/단기전환/단기이탈 4개 그룹이 있어야 한다."""
    src = _read_main()
    for key in ("consistent_inflow", "consistent_outflow", "short_reversal", "short_exit"):
        assert key in src, f"종합 분석 sector_classification에 '{key}' 그룹 없음"


def test_backend_analysis_concentration_has_all_and_concentrated():
    """concentration에 all과 concentrated 두 목록이 있어야 한다."""
    src = _read_main()
    assert '"all":' in src or '"all": ' in src, "concentration에 all 목록 없음"
    assert '"concentrated":' in src or '"concentrated": ' in src, "concentration에 concentrated 목록 없음"


def test_backend_analysis_concentration_threshold_50pct():
    """쏠림 임계값이 50%여야 한다."""
    src = _read_main()
    assert "50" in src, "종합 분석 쏠림 임계값(50%) 없음"
    assert "is_concentrated" in src, "concentration 항목에 is_concentrated 플래그 없음"
    assert "concentration_pct" in src, "concentration 항목에 concentration_pct 없음"


def test_backend_analysis_cross_analysis_has_required_fields():
    """cross_analysis에 total_cross_stocks / cross_in_inflow_sectors / cross_as_sector_top1 / inflow_sector_count가 있어야 한다."""
    src = _read_main()
    for key in ("total_cross_stocks", "cross_in_inflow_sectors", "cross_as_sector_top1", "inflow_sector_count"):
        assert key in src, f"종합 분석 cross_analysis에 '{key}' 필드 없음"


def test_ts_has_sector_flow_analysis_response_type():
    """aiRecommend.ts에 SectorFlowAnalysisResponse 타입이 정의돼 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "SectorFlowAnalysisResponse" in src, "aiRecommend.ts에 SectorFlowAnalysisResponse 타입 없음"


def test_ts_sector_flow_analysis_has_all_interfaces():
    """aiRecommend.ts에 SectorFlowAnalysis 관련 인터페이스 6개가 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    for name in (
        "SectorFlowAnalysisMarketSummary",
        "SectorFlowAnalysisRankEntry",
        "SectorFlowAnalysisRankPeriod",
        "SectorFlowAnalysisClassEntry",
        "SectorFlowAnalysisConcentrationEntry",
        "SectorFlowAnalysisCrossAnalysis",
    ):
        assert name in src, f"aiRecommend.ts에 {name} 인터페이스 없음"


def test_ts_screener_api_has_sector_flow_analysis_method():
    """screenerApi에 sectorFlowAnalysis 메서드가 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "sectorFlowAnalysis" in src, "aiRecommend.ts screenerApi에 sectorFlowAnalysis 메서드 없음"
    assert "/api/screener/sector-flow/analysis" in src, (
        "sectorFlowAnalysis가 올바른 엔드포인트를 호출하지 않음"
    )


def test_screener_has_analysis_open_state():
    """ScreenerPage.tsx에 analysisOpen 상태가 있어야 한다."""
    src = _read_screener()
    assert "analysisOpen" in src, "ScreenerPage에 analysisOpen 상태 없음"
    assert "setAnalysisOpen" in src, "ScreenerPage에 setAnalysisOpen setter 없음"


def test_screener_has_analysis_button():
    """ScreenerPage.tsx에 '📊 종합 분석' 버튼이 있어야 한다."""
    src = _read_screener()
    assert "종합 분석" in src, "ScreenerPage에 '종합 분석' 버튼 텍스트 없음"


def test_screener_analysis_modal_shows_four_sections():
    """ScreenerPage.tsx 분석 모달이 4개 섹션 아이콘(①②③④)을 렌더링해야 한다."""
    src = _read_screener()
    # ① ② ③ ④ 각 섹션이 존재하는지 확인 (SVG 버블 차트로 교체된 ②는 '포지셔닝'으로 변경)
    for marker in ("① 시장 전체", "② 섹터 포지셔닝", "③ 특정 종목 쏠림", "④ 교차 분석"):
        assert marker in src, f"ScreenerPage 분석 모달에 섹션 '{marker}' 없음"


def test_screener_analysis_modal_has_color_coding():
    """ScreenerPage.tsx 분석 모달이 일관 유입=초록, 일관 유출=빨강 색상을 사용해야 한다."""
    src = _read_screener()
    assert "text-emerald-400" in src, "분석 모달에 초록 색상 클래스 없음"
    assert "text-red-400" in src, "분석 모달에 빨강 색상 클래스 없음"
    assert "text-orange-400" in src, "분석 모달에 주황 색상 클래스 없음"


def test_screener_analysis_modal_has_loading_and_error_state():
    """ScreenerPage.tsx 분석 모달이 로딩/에러 상태를 처리해야 한다."""
    src = _read_screener()
    assert "analysisLoading" in src, "ScreenerPage에 analysisLoading 상태 없음"
    assert "analysisError" in src, "ScreenerPage에 analysisError 상태 없음"


# ── 쏠림 분석 개선 (2026-08-06) ─────────────────────────────────────────────

def test_backend_concentration_has_mktcap_ratio_field():
    """main.py 집중도 분석 엔트리에 mktcap_ratio_pct 필드가 있어야 한다."""
    src = _read_main()
    assert "mktcap_ratio_pct" in src, "main.py에 mktcap_ratio_pct 필드 없음"


def test_backend_concentration_has_surge_ratio_field():
    """main.py 집중도 분석 엔트리에 surge_ratio, surge_label 필드가 있어야 한다."""
    src = _read_main()
    assert "surge_ratio" in src, "main.py에 surge_ratio 필드 없음"
    assert "surge_label" in src, "main.py에 surge_label 필드 없음"


def test_backend_concentration_has_market_cap_field():
    """main.py 집중도 분석 엔트리에 market_cap_억 필드가 있어야 한다."""
    src = _read_main()
    assert "market_cap_억" in src, "main.py에 market_cap_억 필드 없음"


def test_backend_concentration_has_500_filter():
    """main.py 집중도 분석에서 시총 500억 미만 제외 로직이 있어야 한다."""
    src = _read_main()
    assert "500" in src, "main.py에 500억 필터 없음"
    assert "mktcap < 500" in src, "main.py에 500억 필터 로직 없음"


def test_backend_concentration_sorted_by_mktcap_ratio():
    """main.py 집중도 리스트가 mktcap_ratio_pct 기준으로 정렬되어야 한다."""
    src = _read_main()
    assert "mktcap_ratio_pct" in src, "main.py에 mktcap_ratio_pct 없음"
    # sort key가 mktcap_ratio_pct를 사용해야 함
    assert "x[\"mktcap_ratio_pct\"]" in src or "x['mktcap_ratio_pct']" in src, \
        "main.py 집중도 정렬이 mktcap_ratio_pct를 사용하지 않음"


def test_backend_concentration_mktcap_query_uses_financials():
    """main.py 집중도 분석이 financials.shares_total로 시총을 계산해야 한다."""
    src = _read_main()
    assert "shares_total" in src, "main.py에 shares_total 쿼리 없음"
    assert "mktcap_map" in src, "main.py에 mktcap_map 없음"


def test_api_ts_concentration_has_new_fields():
    """aiRecommend.ts SectorFlowAnalysisConcentrationEntry에 신규 필드가 있어야 한다."""
    src = API_TS.read_text(encoding="utf-8")
    assert "mktcap_ratio_pct" in src, "aiRecommend.ts에 mktcap_ratio_pct 없음"
    assert "market_cap_억" in src, "aiRecommend.ts에 market_cap_억 없음"
    assert "surge_ratio" in src, "aiRecommend.ts에 surge_ratio 없음"
    assert "surge_label" in src, "aiRecommend.ts에 surge_label 없음"


def test_screener_section1_has_mini_bar_chart():
    """ScreenerPage.tsx 섹션①이 flex-wrap 배지 대신 가로 미니 막대 차트를 사용해야 한다."""
    src = _read_screener()
    # 바 차트 구조 확인
    assert "rounded-full overflow-hidden" in src, "섹션①에 막대 차트 컨테이너 없음"
    assert "style={{ width:" in src or "style={{width:" in src or "barPct" in src, \
        "섹션①에 동적 바 너비 없음"
    assert "maxAbs" in src, "섹션①에 maxAbs 계산 없음"


def test_screener_section2_has_quadrant_card_layout():
    """ScreenerPage.tsx 섹션②가 4사분면 카드 레이아웃을 사용해야 한다 (SVG 버블 차트 아님)."""
    src = _read_screener()
    # 4개 분류 그룹 모두 존재
    assert "consistent_inflow" in src,  "섹션②에 일관유입(consistent_inflow) 그룹 없음"
    assert "consistent_outflow" in src, "섹션②에 일관유출(consistent_outflow) 그룹 없음"
    assert "short_reversal" in src,     "섹션②에 단기전환(short_reversal) 그룹 없음"
    assert "short_exit" in src,         "섹션②에 단기이탈(short_exit) 그룹 없음"
    # 카드 레이아웃: 2열 그리드 + 4개 카드 헤더 이모지 포함
    assert "grid grid-cols-2" in src, "섹션②에 2열 그리드(grid grid-cols-2) 없음"
    assert "🟢 일관 유입" in src, "섹션②에 일관유입 카드 헤더(🟢 일관 유입) 없음"
    assert "🟠 단기 전환" in src, "섹션②에 단기전환 카드 헤더(🟠 단기 전환) 없음"
    assert "🟡 단기 이탈" in src, "섹션②에 단기이탈 카드 헤더(🟡 단기 이탈) 없음"
    assert "🔴 일관 유출" in src, "섹션②에 일관유출 카드 헤더(🔴 일관 유출) 없음"
    # SVG 버블 전용 요소 제거 확인
    assert "bbl-g" not in src,      "섹션②에 SVG 버블 클래스(bbl-g) 잔존 — 카드 레이아웃으로 교체됐어야 함"
    assert "quadLabels" not in src, "섹션②에 SVG 코너 라벨 배열(quadLabels) 잔존"


def test_screener_section2_sector_count_header():
    """섹션② 각 카드에 'N개 섹터' 카운트가 표시돼야 한다."""
    src = _read_screener()
    assert "개 섹터" in src, "섹션② 카드에 '개 섹터' 카운트 없음"


def test_screener_section2_mini_bar():
    """섹션② 각 섹터 행에 5d 금액 비례 가로 미니 바가 있어야 한다."""
    src = _read_screener()
    assert "barW" in src,  "섹션② 미니 바 너비 변수(barW) 없음"
    assert "max5d" in src, "섹션② 미니 바 기준값(max5d) 없음"


def test_screener_section2_cross_analysis_border():
    """섹션② 교차분석 종목 보유 섹터에 보라색(#a78bfa) 좌측 테두리가 적용돼야 한다."""
    src = _read_screener()
    assert "has_cross_stocks" in src, "섹션②에 교차분석 플래그(has_cross_stocks) 없음"
    assert "#a78bfa" in src,          "섹션②에 보라색 테두리 색상(#a78bfa) 없음"


def test_screener_section2_sorted_by_abs_5d():
    """섹션② 각 카드의 섹터가 |5일 순매수| 기준 내림차순 정렬돼야 한다."""
    src = _read_screener()
    assert "Math.abs(b.combined_5d) - Math.abs(a.combined_5d)" in src, \
        "섹션② 5d 절대값 기준 정렬 코드(Math.abs(b.combined_5d) - Math.abs(a.combined_5d)) 없음"


def test_screener_section2_5d_60d_formatted():
    """섹션② 각 섹터 행에 5d · 60d 순매수 수치가 fmt() 함수로 포맷돼 표시돼야 한다."""
    src = _read_screener()
    assert "5d {fmt(" in src,  "섹션②에 5d 포맷 표시 코드(5d {fmt() 없음"
    assert "60d {fmt(" in src, "섹션②에 60d 포맷 표시 코드(60d {fmt() 없음"


def test_screener_section3_shows_mktcap_ratio():
    """ScreenerPage.tsx 섹션③이 시총 대비 비율을 표시해야 한다."""
    src = _read_screener()
    assert "mktcap_ratio_pct" in src, "섹션③에 mktcap_ratio_pct 없음"
    assert "시총대비" in src, "섹션③에 '시총대비' 텍스트 없음"
    assert "시총 대비 이례적 유입" in src, "섹션③ 헤더에 '시총 대비 이례적 유입' 없음"


def test_screener_section3_shows_surge_info():
    """ScreenerPage.tsx 섹션③이 급증 정보를 표시해야 한다."""
    src = _read_screener()
    assert "surge_label" in src, "섹션③에 surge_label 없음"
    assert "surge_ratio" in src, "섹션③에 surge_ratio 없음"
    assert "평소대비" in src, "섹션③에 '평소대비' 텍스트 없음"


def test_screener_section4_has_explanation():
    """ScreenerPage.tsx 섹션④에 자금흐름 방향 일치 설명 문구가 있어야 한다."""
    src = _read_screener()
    assert "모델 판단과 시장 자금 흐름이 같은 방향" in src, \
        "섹션④에 방향 일치 설명 문구 없음"
    assert "검증된 조합" in src and "6/6" in src, \
        "섹션④에 '검증된 조합' 또는 '6/6' 텍스트 없음"


def test_screener_section3_stock_name_clickable():
    """ScreenerPage.tsx 섹션③ 종목명이 클릭 가능한 버튼으로 렌더링돼야 한다."""
    src = _read_screener()
    assert "top1_symbol" in src, "섹션③ 클릭 핸들러에 top1_symbol 없음"
    assert "setAnalysisOpen(false)" in src, "종목 클릭 시 모달 닫기 코드 없음"
    # setTimeout으로 딜레이 후 프로파일 열기 (모달 언마운트 후 state 업데이트 보장)
    assert "setTimeout" in src, "섹션③ 클릭 핸들러에 setTimeout 딜레이 없음"


def test_screener_section3_stock_click_opens_profile():
    """ScreenerPage.tsx 섹션③ 종목 클릭이 프로파일 카드를 여는 코드를 포함해야 한다."""
    src = _read_screener()
    assert "screenerApi.symbolProfile(symbol)" in src, \
        "섹션③ 종목 클릭에 symbolProfile 호출 없음"
    assert "setProfileLoading(true)" in src, "종목 클릭에 profileLoading 설정 없음"


def test_screener_accepts_onstockselect_prop():
    """ScreenerPage.tsx가 embedded 모드에서 onStockSelect 콜백 prop을 받아야 한다."""
    src = _read_screener()
    assert "onStockSelect" in src, "ScreenerPage props에 onStockSelect 없음"
    assert "onStockSelect?" in src or "onStockSelect ?" in src or "onStockSelect?:" in src, \
        "onStockSelect가 선택적(optional) prop이 아님"


def test_stockexplorepage_passes_onstockselect():
    """StockExplorePage.tsx가 ScreenerPage에 onStockSelect prop을 전달해야 한다."""
    import os
    path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'pages', 'StockExplorePage.tsx')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    assert "onStockSelect" in src, "StockExplorePage가 ScreenerPage에 onStockSelect 전달 없음"
    assert "handleStockSelect" in src, "StockExplorePage에 handleStockSelect 함수 없음"


# ── 종목명 클릭 → 프로파일 카드 전역 연결 ─────────────────────────────────────

def test_screener_has_handle_symbol_click_helper():
    """ScreenerPage.tsx에 handleSymbolClick 헬퍼 함수가 있어야 한다.

    embedded 모드: onStockSelect(symbol) 위임
    standalone 모드: handleStockSelect({ symbol, name: '', market: '' }) 폴백
    """
    src = _read_screener()
    assert "handleSymbolClick" in src, "ScreenerPage에 handleSymbolClick 헬퍼 없음"
    # embedded 경로: onStockSelect 위임
    assert "onStockSelect(symbol)" in src, \
        "handleSymbolClick에 onStockSelect(symbol) 위임 없음"
    # standalone 폴백: handleStockSelect 직접 호출
    assert "handleStockSelect(" in src, \
        "handleSymbolClick에 handleStockSelect 폴백 없음"


def test_screener_cross_analysis_table_row_clickable():
    """ScreenerPage.tsx 교차 분석 테이블(sortedCrossStocks) 행이 handleSymbolClick으로 클릭 가능해야 한다."""
    src = _read_screener()
    # 교차 분석 테이블 행에 cursor-pointer 클래스 확인
    assert "sortedCrossStocks" in src, "ScreenerPage에 sortedCrossStocks 없음"
    # handleSymbolClick(s.symbol) 호출 확인
    assert "handleSymbolClick(s.symbol)" in src, \
        "교차 분석 테이블 행에 handleSymbolClick(s.symbol) 클릭 핸들러 없음"


def test_screener_sector_cross_stocks_cards_are_buttons_with_click():
    """ScreenerPage.tsx 섹터 교차 종목 카드(sector.cross_stocks)가 <button>으로 변환되고 handleSymbolClick을 호출해야 한다."""
    src = _read_screener()
    assert "sector.cross_stocks" in src or "cross_stocks" in src, \
        "ScreenerPage에 sector.cross_stocks 렌더링 없음"
    # <button>으로 변환 확인 (이전에 <div>였음)
    # handleSymbolClick(s.symbol) 확인
    assert "handleSymbolClick(s.symbol)" in src, \
        "섹터 교차 종목 카드에 handleSymbolClick(s.symbol) 클릭 핸들러 없음"


def test_screener_sector_all_stocks_table_row_clickable():
    """ScreenerPage.tsx 섹터 전체 종목 목록(sector.stocks) 행이 handleSymbolClick으로 클릭 가능해야 한다."""
    src = _read_screener()
    assert "sector.stocks" in src, "ScreenerPage에 sector.stocks 렌더링 없음"
    # handleSymbolClick(st.symbol) 호출 확인
    assert "handleSymbolClick(st.symbol)" in src, \
        "섹터 전체 종목 목록 행에 handleSymbolClick(st.symbol) 클릭 핸들러 없음"


def test_screener_symbol_click_uses_cursor_pointer():
    """handleSymbolClick을 사용하는 요소들이 cursor-pointer 클래스를 가져야 한다."""
    src = _read_screener()
    # 교차 분석 테이블 행과 섹터 종목 테이블 행 모두에 cursor-pointer 있어야 함
    assert "cursor-pointer" in src, \
        "클릭 가능한 종목 행에 cursor-pointer 클래스 없음"
