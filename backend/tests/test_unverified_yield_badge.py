"""
has_unverified_yield 뱃지 로직 회귀 테스트.
actual_yield > 15% 이면서 기존 경고 뱃지(특별/병합환산/중단의심)가 없는 종목에만 표시되는지 검증.
화이트리스트(.dividend_yield_verified.json) 종목은 검증 완료로 뱃지 제외.
"""
import pytest


def _build_stock(
    actual_yield=None,
    has_special=False,
    has_split=False,
    has_suspended=False,
    symbol="000000",
    verified_symbols=None,
):
    """screener 엔드포인트가 계산하는 has_unverified_yield 로직을 그대로 재현."""
    if verified_symbols is None:
        verified_symbols = set()
    s = {
        "symbol": symbol,
        "actual_yield": actual_yield,
        "has_special_dividend": has_special,
        "has_split_adjusted": has_split,
        "has_div_suspended": has_suspended,
    }
    ay = s.get("actual_yield")
    is_verified = s["symbol"] in verified_symbols
    s["has_unverified_yield"] = (
        ay is not None
        and ay > 15
        and not is_verified
        and not s["has_special_dividend"]
        and not s["has_split_adjusted"]
        and not s["has_div_suspended"]
    )
    return s


def test_normal_yield_no_badge():
    """일반 배당수익률(15% 이하)은 미검증 뱃지 없음."""
    s = _build_stock(actual_yield=5.0)
    assert s["has_unverified_yield"] is False


def test_boundary_exactly_15_no_badge():
    """정확히 15%는 뱃지 없음 (> 이므로 15 초과부터)."""
    s = _build_stock(actual_yield=15.0)
    assert s["has_unverified_yield"] is False


def test_above_15_no_existing_badges_gets_badge():
    """15% 초과이고 기존 뱃지 없으면 미검증 뱃지 표시."""
    s = _build_stock(actual_yield=15.1)
    assert s["has_unverified_yield"] is True


def test_above_15_with_special_dividend_no_badge():
    """15% 초과여도 특별배당 뱃지가 있으면 미검증 뱃지 불필요."""
    s = _build_stock(actual_yield=30.0, has_special=True)
    assert s["has_unverified_yield"] is False


def test_above_15_with_split_adjusted_no_badge():
    """15% 초과여도 병합환산 뱃지가 있으면 미검증 뱃지 불필요."""
    s = _build_stock(actual_yield=20.0, has_split=True)
    assert s["has_unverified_yield"] is False


def test_above_15_with_div_suspended_no_badge():
    """15% 초과여도 중단의심 뱃지가 있으면 미검증 뱃지 불필요."""
    s = _build_stock(actual_yield=16.0, has_suspended=True)
    assert s["has_unverified_yield"] is False


def test_null_actual_yield_no_badge():
    """actual_yield가 None이면 미검증 뱃지 없음."""
    s = _build_stock(actual_yield=None)
    assert s["has_unverified_yield"] is False


def test_known_anomaly_eaholdings_gets_badge():
    """에이홀딩스(035810) 케이스: actual_yield=30.4%, 뱃지 없음 → 미검증 표시."""
    s = _build_stock(actual_yield=30.4, symbol="035810")
    assert s["has_unverified_yield"] is True


def test_known_verified_ivykimyoung_no_badge():
    """아이비김영(339950): DART 확인 완료(DPS=300원, 배당성향=64.7%) → 미검증 뱃지 없음."""
    verified = {"339950"}
    s = _build_stock(actual_yield=15.4, symbol="339950", verified_symbols=verified)
    assert s["has_unverified_yield"] is False


def test_ivykimyoung_without_whitelist_gets_badge():
    """아이비김영(339950)이 화이트리스트에 없으면 미검증 뱃지 표시 (회귀 방지)."""
    s = _build_stock(actual_yield=15.4, symbol="339950", verified_symbols=set())
    assert s["has_unverified_yield"] is True


def test_hyundai_elevator_gets_badge():
    """현대엘리베이(017800) 케이스: actual_yield=15.9%, 뱃지 없음 → 미검증 표시."""
    s = _build_stock(actual_yield=15.9, symbol="017800")
    assert s["has_unverified_yield"] is True


def test_legitimate_high_yield_below_15_no_badge():
    """정상 고배당 종목(14%대 이하)은 미검증 뱃지 없음."""
    for y in [10.0, 11.2, 12.5, 14.9]:
        s = _build_stock(actual_yield=y)
        assert s["has_unverified_yield"] is False, f"actual_yield={y} should not get badge"


def test_quarterly_dividend_company_annual_dps_below_threshold_no_badge():
    """분기 배당 종목이라도 연간 DPS 합산 actual_yield가 15% 이하면 미검증 뱃지 없음.

    DART 사업보고서(11011)는 분기 배당 포함 연간 누적 DPS를 기재함.
    수집기가 이 값을 올바르게 가져오는 한 실제 연간 DPS가 DB에 저장되며,
    actual_yield = 연간DPS ÷ 기준일종가 가 15% 이하이면 뱃지 없음.
    ex) 현대차(005380) 2024 DPS=12,000원 / 종가~200,000원 → actual_yield ≈ 6%
    """
    # 분기 배당 종목 대표 케이스 (연간 DPS 합산, 15% 미만 고배당)
    for sym, yield_pct in [
        ("005380", 6.0),   # 현대차 biz_year=2024 DPS=12,000원
        ("005490", 4.5),   # POSCO홀딩스
        ("033780", 7.0),   # KT&G
        ("010130", 5.0),   # 고려아연
    ]:
        s = _build_stock(actual_yield=yield_pct, symbol=sym)
        assert s["has_unverified_yield"] is False, (
            f"{sym} 분기배당 종목 actual_yield={yield_pct}%가 미검증 뱃지 없어야 함"
        )


def test_quarterly_dividend_company_verified_when_high_yield():
    """분기 배당 종목이 고배당(>15%)이어도 화이트리스트에 있으면 미검증 뱃지 없음.

    아이비김영(339950)은 연 1회 결산 배당 종목으로,
    DART 사업보고서(11011) 직접 확인으로 DPS=300원(2025) 검증 완료.
    분기 보고서(11012/11013/11014)에서 thstrm=- 확인 — 분기 지급 없음.
    """
    verified = {"339950"}
    s = _build_stock(actual_yield=15.4, symbol="339950", verified_symbols=verified)
    assert s["has_unverified_yield"] is False

    # 화이트리스트 없으면 뱃지 표시 (회귀 방지)
    s2 = _build_stock(actual_yield=15.4, symbol="339950", verified_symbols=set())
    assert s2["has_unverified_yield"] is True
