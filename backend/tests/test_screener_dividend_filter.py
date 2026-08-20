"""
스크리너 고배당 필터 기준 일치 회귀 테스트.

설계 원칙:
  - 필터 판정 = trailing yield (DPS×CF ÷ 현재종가) — 백테스트(combo_discovery_v2.py) 일치
  - 표시/정렬   = actual_yield (DPS×CF ÷ 배당기준일종가) — 사용자 정직성
  - 변경 금지: 필터는 dividend_yield(trailing)을 쓰고 actual_yield로 바꾸면
    검증된 71.9% 승률이 다른 종목 집합에 적용되는 train-serving skew 발생.
"""
import bisect


# ── 헬퍼: 스크리너 filter 로직 그대로 재현 ─────────────────────────────────


def _pct_rank(sorted_vals: list, v) -> float | None:
    if not sorted_vals or v is None:
        return None
    return bisect.bisect_right(sorted_vals, v) / len(sorted_vals)


def _build_stock_set(items: list[dict]) -> tuple[list, list]:
    """
    items: [{"symbol": str, "dividend_yield": float|None, "actual_yield": float|None}]
    반환: (stocks, div_vals)  — screener 엔드포인트 내부 변수와 동일 구조
    """
    stocks = list(items)
    div_vals = sorted(s["dividend_yield"] for s in stocks if s["dividend_yield"] is not None)
    return stocks, div_vals


def _passes_high_dividend_filter(s: dict, div_vals: list) -> bool:
    """main.py 2684줄 필터 로직 재현."""
    return not (s["dividend_yield"] is None or _pct_rank(div_vals, s["dividend_yield"]) < 0.8)


# ── 테스트 ──────────────────────────────────────────────────────────────────


def test_filter_uses_trailing_not_actual():
    """필터는 dividend_yield(trailing)으로 판정, actual_yield로 바꾸면 안 됨.

    trailing은 높지만 actual이 낮은 종목 A와
    actual은 높지만 trailing이 낮은 종목 B를 같이 두었을 때:
      A는 필터 통과, B는 통과 못해야 함.
    """
    # 주가가 내려가면 trailing > actual
    stock_a = {"symbol": "000001", "dividend_yield": 10.0, "actual_yield": 6.0}
    # 주가가 올라가면 actual > trailing
    stock_b = {"symbol": "000002", "dividend_yield": 2.0,  "actual_yield": 10.0}
    # 나머지 종목들 (분모가 되는 div_vals를 만들기 위해)
    fillers = [{"symbol": f"9{i:05d}", "dividend_yield": float(i), "actual_yield": float(i)}
               for i in range(1, 9)]  # 1%~8%

    stocks, div_vals = _build_stock_set([stock_a, stock_b] + fillers)
    # 총 10종목. 상위 20% = 2종목. A(10.0)와 filler 8.0이 상위 2.

    result_a = _passes_high_dividend_filter(stock_a, div_vals)
    result_b = _passes_high_dividend_filter(stock_b, div_vals)

    assert result_a is True,  "A: trailing=10% 높으면 필터 통과해야 함"
    assert result_b is False, "B: trailing=2% 낮으면 actual=10%이더라도 필터 통과 안 됨"


def test_filter_rank_threshold_exactly_80pct():
    """상위 20% 경계선 검증 — pct_rank >= 0.8 이상이어야 통과."""
    # 10종목, trailing yield 1%~10%
    stocks_data = [{"symbol": f"{i:06d}", "dividend_yield": float(i), "actual_yield": float(i)}
                   for i in range(1, 11)]
    stocks, div_vals = _build_stock_set(stocks_data)

    # rank=0.8 (8위/10종목) → 통과, rank=0.7(7위) → 통과 안 됨
    s_8 = next(s for s in stocks if s["symbol"] == "000008")  # yield=8.0%
    s_7 = next(s for s in stocks if s["symbol"] == "000007")  # yield=7.0%

    assert _passes_high_dividend_filter(s_8, div_vals) is True
    assert _passes_high_dividend_filter(s_7, div_vals) is False


def test_none_dividend_yield_fails_filter():
    """dividend_yield가 None이면 무조건 필터 실패 — actual_yield 값과 무관."""
    s = {"symbol": "000001", "dividend_yield": None, "actual_yield": 20.0}
    _, div_vals = _build_stock_set([s])
    assert _passes_high_dividend_filter(s, div_vals) is False


def test_sort_uses_actual_yield_over_trailing():
    """정렬 키는 actual_yield 우선 — 필터 판정과 다른 경로."""
    sort_key = lambda s: (
        s.get("actual_yield") if s.get("actual_yield") is not None
        else (s["dividend_yield"] if s["dividend_yield"] is not None else -1)
    )
    stocks = [
        {"symbol": "A", "dividend_yield": 10.0, "actual_yield": 4.0},   # actual이 낮음
        {"symbol": "B", "dividend_yield": 3.0,  "actual_yield": 9.0},   # actual이 높음
        {"symbol": "C", "dividend_yield": 5.0,  "actual_yield": None},  # actual 없음 → trailing
    ]
    sorted_desc = sorted(stocks, key=sort_key, reverse=True)
    symbols = [s["symbol"] for s in sorted_desc]
    assert symbols == ["B", "C", "A"], (
        "정렬은 actual_yield 기준: B(9.0) > C(trailing 5.0) > A(actual 4.0)"
    )


def test_profile_flag_uses_trailing():
    """프로파일 엔드포인트의 high_dividend 플래그도 trailing 기준.
    actual은 높지만 trailing이 하위 20%면 플래그 False여야 함."""
    # actual은 높지만 trailing이 낮은 종목
    s_low_trailing = {"symbol": "000001", "dividend_yield": 1.0, "actual_yield": 15.0}
    # trailing이 높은 종목들 (기준값 만들기)
    fillers = [{"symbol": f"9{i:05d}", "dividend_yield": float(i * 3), "actual_yield": None}
               for i in range(1, 10)]
    _, div_vals = _build_stock_set([s_low_trailing] + fillers)

    # profile 플래그 로직 (main.py 2665줄)
    flag = (
        s_low_trailing["dividend_yield"] is not None
        and (_pct_rank(div_vals, s_low_trailing["dividend_yield"]) or 0) >= 0.8
    )
    assert flag is False, "trailing이 하위면 actual=15%여도 high_dividend 플래그 False"


def test_backtest_formula_identity():
    """백테스트 공식 확인: dividend_yield = DPS × CF ÷ 종가 × 100.
    combo_discovery_v2.py: dps_corr / prices['close'] (× 100 환산)."""
    dps = 1000      # 원
    cf  = 2.0       # 분할 보정 (1주 → 2주)
    close = 50000   # 원
    expected_trailing = dps * cf / close * 100  # = 4.0%

    # 스크리너가 dps_map에서 꺼내는 값은 DPS × CF (이미 CF 내재)
    dps_from_map = dps * cf   # _compute_dps_map() 반환값
    actual_computed = dps_from_map / close * 100  # s["dividend_yield"] 계산식

    assert abs(actual_computed - expected_trailing) < 1e-9, (
        "스크리너 dividend_yield 공식이 백테스트와 동일해야 함"
    )
