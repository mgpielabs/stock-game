"""
회귀 테스트: 배당 분할 보정계수 방향 확인

KSD 진단 후 수정된 종목 (KSD 무상증자 방향 반전 수정, 2026-07-01):
  000227 / 2022, 2024, 2025 → cf = 1.0 (무보정)
  006740 / 2023              → cf = 1.0
  262840 / 2025              → cf = 1.0

가격 검증으로 수정된 종목 (경험값 오판 수정, 2026-07-06):
  006740 / 2021, 2022 → cf = 1.0
    근거: 배당 지급 시점 pykrx 가격 대비 DART yield 일치 확인(~0.6%/~7.3%).
    KSD rev_split(2026-06-26)은 가격 미반영(1110→1109, 이벤트 없음).
    경험값 K=0.2(cf=5.0)는 저배당 종목을 역분할로 오판한 수식 오류.

이 값들이 CSV에 그대로 있어야 하며,
기존에 잘못됐던 방향(cf=2.0 또는 cf=2.5, cf=5.0)이 아닌지 확인.

일반 요건: correction_factor > 0, CF 없는 행이 있으면 안 됨.
"""
from pathlib import Path

import pandas as pd
import pytest

CSV_PATH = (
    Path(__file__).parent.parent / "ml" / "dividend_split_correction_factors.csv"
)

# 방향역전 수정 + 경험값 오판 수정 완료 후 기대값 (cf=1.0 = 무보정)
FIXED_ROWS = [
    ("000227", 2022, 1.0),
    ("000227", 2024, 1.0),
    ("000227", 2025, 1.0),
    ("006740", 2021, 1.0),  # 2026-07-06: 경험값 cf=5.0 오판 수정
    ("006740", 2022, 1.0),  # 2026-07-06: 경험값 cf=5.0 오판 수정
    ("006740", 2023, 1.0),
    ("262840", 2025, 1.0),
]


@pytest.fixture(scope="module")
def cf_df():
    if not CSV_PATH.exists():
        pytest.skip("dividend_split_correction_factors.csv 없음")
    df = pd.read_csv(CSV_PATH, dtype={"symbol": str})
    df["symbol"] = df["symbol"].str.zfill(6)
    return df


def test_fixed_rows_cf_is_one(cf_df):
    """KSD 방향역전 수정 대상 행의 correction_factor가 1.0인지 확인.
    행이 CSV에 없으면 보정 안 함(=cf=1.0과 동일)이므로 허용.
    존재하는 행만 검사해서 잘못된 cf(예: 5.0, 2.5, 2.0)로 남아있지 않은지 확인."""
    errors = []
    for sym, byz, expected_cf in FIXED_ROWS:
        row = cf_df[(cf_df["symbol"] == sym) & (cf_df["biz_year"] == byz)]
        if row.empty:
            continue  # CSV에 없음 = 보정 없음 = cf=1.0과 동일, 허용
        actual_cf = float(row["correction_factor"].iloc[0])
        if abs(actual_cf - expected_cf) > 1e-6:
            errors.append(
                f"  ({sym}, {byz}): cf={actual_cf:.6f}, 기대={expected_cf:.6f} — "
                "방향역전 수정이 CSV에 반영 안 됐거나 재실행으로 덮어써짐"
            )
    assert not errors, "방향역전 수정 실패:\n" + "\n".join(errors)


def test_all_cf_positive(cf_df):
    """모든 correction_factor가 양수여야 한다."""
    bad = cf_df[cf_df["correction_factor"] <= 0]
    assert bad.empty, (
        f"correction_factor ≤ 0인 행 {len(bad)}건:\n{bad[['symbol','biz_year','correction_factor']].to_string()}"
    )


def test_no_null_cf(cf_df):
    """correction_factor가 NULL인 행이 없어야 한다."""
    bad = cf_df[cf_df["correction_factor"].isna()]
    assert bad.empty, f"correction_factor NULL 행 {len(bad)}건 발견"


def test_006740_2021_2022_corrected_to_one(cf_df):
    """006740의 2021·2022는 경험값 오판(cf=5.0) 수정 후 cf=1.0이어야 한다.
    pykrx 가격 검증 결과 KSD rev_split이 가격에 미반영됨을 확인 (2026-07-06).
    FIXED_ROWS에서 이미 커버되나 명시적 단독 테스트로도 유지."""
    for byz in (2021, 2022):
        row = cf_df[(cf_df["symbol"] == "006740") & (cf_df["biz_year"] == byz)]
        if row.empty:
            continue  # 해당 행 없으면 스킵
        cf = float(row["correction_factor"].iloc[0])
        assert abs(cf - 1.0) < 1e-6, (
            f"006740 biz_year={byz}: cf={cf:.6f}, 경험값 오판 수정 후 1.0이어야 함 — "
            "5.0으로 되어 있으면 수정이 CSV에 미반영된 것"
        )
