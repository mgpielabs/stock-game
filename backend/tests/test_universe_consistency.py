"""train-serving 유니버스 일치 검증.

재발방지 테스트 — 학습(dataset.py)과 서빙(predictor.py)이 동일한 ETF 필터를
적용하는지, 그리고 서빙 유니버스가 학습 유니버스의 부분집합인지 보장.

"필터·규칙은 학습·서빙 양쪽에 적용됐는지 테스트로 강제.
 한쪽에만 있으면 조용한 skew." (2026-07-28 유니버스 정제 교훈)
"""
import re
import sqlite3
import sys
import urllib.request
import json
from pathlib import Path

import pytest

BACKEND = Path(__file__).parent.parent
DB_PATH = BACKEND / "data" / "stocks.db"

sys.path.insert(0, str(BACKEND / "server"))
sys.path.insert(0, str(BACKEND / "ml"))


# ---------------------------------------------------------------------------
# [1] 패턴 문자열 일치 — 학습·서빙이 동일한 ETF 필터를 사용하는지
# ---------------------------------------------------------------------------

def test_etf_pattern_identical_in_dataset_and_predictor():
    """dataset._ETF_PATTERN == predictor._ETF_PATTERN — 동일 패턴 사용 보장."""
    from dataset import _ETF_PATTERN as ds_pat
    from predictor import _ETF_PATTERN as pred_pat
    assert ds_pat == pred_pat, (
        f"ETF 패턴 불일치:\n"
        f"  dataset  : {ds_pat!r}\n"
        f"  predictor: {pred_pat!r}\n"
        "두 파일 중 하나를 변경하면 다른 쪽도 동기화할 것."
    )


@pytest.mark.skipif(not DB_PATH.exists(), reason="stocks.db 없음 — CI/오프라인 환경")
def test_etf_symbol_sets_match():
    """dataset._etf_symbols()와 predictor._get_etf_symbols()가 동일한 심볼 집합을 반환."""
    from dataset import _etf_symbols as ds_etf
    from predictor import _get_etf_symbols as pred_etf, invalidate_etf_cache

    invalidate_etf_cache()  # 캐시 클리어 후 fresh 계산

    with sqlite3.connect(DB_PATH) as conn:
        ds_set = ds_etf(conn)

    pred_set = pred_etf()

    only_ds = ds_set - pred_set
    only_pred = pred_set - ds_set

    assert not only_ds and not only_pred, (
        f"ETF 심볼 집합 불일치:\n"
        f"  dataset에만 있음 ({len(only_ds)}개): {sorted(only_ds)[:10]}\n"
        f"  predictor에만 있음 ({len(only_pred)}개): {sorted(only_pred)[:10]}"
    )


# ---------------------------------------------------------------------------
# [2] 서빙 결과에 ETF/인버스 심볼이 0개인지 — 라이브 API 기준
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DB_PATH.exists(), reason="stocks.db 없음")
def test_serving_result_has_no_etf():
    """GET /api/predictions/today top-10 결과에 ETF 패턴 종목이 없음."""
    pat = re.compile(
        r'ETF|ETN|레버리지|인버스|선물'
        r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
        re.IGNORECASE,
    )
    try:
        with urllib.request.urlopen("http://127.0.0.1:8001/api/predictions/today", timeout=10) as r:
            data = json.loads(r.read())
    except Exception as exc:
        pytest.skip(f"서버 미응답 — {exc}")

    predictions = data.get("predictions", [])
    assert predictions, "predictions 빈 목록 — 서버 응답 이상"

    with sqlite3.connect(DB_PATH) as conn:
        name_map = dict(conn.execute("SELECT symbol, name FROM stocks").fetchall())

    etf_hits = [
        (p["symbol"], name_map.get(p["symbol"], "?"))
        for p in predictions
        if pat.search(name_map.get(p["symbol"], "") or "")
    ]
    assert not etf_hits, (
        f"서빙 결과에 ETF/인버스 종목이 포함됨: {etf_hits}\n"
        "predictor._load_features_for_date()의 ETF 필터가 작동하지 않고 있습니다."
    )


# ---------------------------------------------------------------------------
# [3] 서빙 유니버스 ⊆ 학습 유니버스 — ETF가 서빙에만 새로 포함되지 않음
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not DB_PATH.exists(), reason="stocks.db 없음")
def test_serving_universe_subset_of_training_universe():
    """서빙 유니버스에서 ETF 제외 후 잔여 종목이 학습 유니버스에 있어야 함.

    즉, 서빙이 ETF를 제거한 뒤 남은 종목들은 학습에서도 허용된 종목이어야 함.
    학습 유니버스 = features 테이블의 전체 심볼 − ETF 심볼.
    서빙 유니버스 = predictor가 최신 features 날짜에서 반환하는 심볼.
    """
    from predictor import _get_etf_symbols, invalidate_etf_cache, _load_features_for_date

    invalidate_etf_cache()
    etf_syms = _get_etf_symbols()

    with sqlite3.connect(DB_PATH) as conn:
        # 최신 features 날짜 찾기
        latest_date = conn.execute(
            "SELECT MAX(date) FROM features"
        ).fetchone()[0]
        if not latest_date:
            pytest.skip("features 테이블 비어 있음")

        # 학습 유니버스: features 전체 심볼 − ETF
        all_feature_syms = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM features WHERE date = ?", (latest_date,)
            ).fetchall()
        }

    training_universe = all_feature_syms - etf_syms

    # 서빙 유니버스: _load_features_for_date가 반환하는 심볼
    serving_df = _load_features_for_date(latest_date)
    serving_universe = set(serving_df["symbol"].tolist())

    # 서빙 유니버스 ⊆ 학습 유니버스
    serving_only = serving_universe - training_universe
    assert not serving_only, (
        f"서빙 유니버스에 학습에 없는 종목이 포함됨 ({len(serving_only)}개):\n"
        f"  {sorted(serving_only)[:20]}\n"
        "train-serving skew 발생 — 필터 동기화 확인 필요."
    )
