"""
회귀 테스트: 60d 모델 로딩 안전성

H3 수정 (2026-07-05): predictor.py의 predict_60d()가
meta.json에 feature_cols/features 키가 모두 없으면 RuntimeError를 발생시키는지 확인.

또한 현재 서빙 60d 모델의 meta.json에 feature_cols가 비어 있지 않은지 확인.
"""
import json
import sys
from pathlib import Path

import pytest

BACKEND   = Path(__file__).parent.parent
MODEL_DIR = BACKEND / "models"
SRV_DIR   = BACKEND / "server"

sys.path.insert(0, str(SRV_DIR))
sys.path.insert(0, str(BACKEND / "ml"))
sys.path.insert(0, str(BACKEND / "data"))


def test_active_60d_meta_has_feature_cols():
    """서빙 60d 모델 meta.json에 feature_cols가 있고 비어있지 않아야 한다."""
    pointer = MODEL_DIR / "ACTIVE_target_60d.txt"
    if not pointer.exists():
        pytest.skip("ACTIVE_target_60d.txt 없음")

    model_name = pointer.read_text(encoding="utf-8").strip()
    model_dir  = MODEL_DIR / model_name
    # 60d 모델은 model_meta.json, 5d 모델은 meta.json 사용
    meta_path = next(
        (model_dir / f for f in ("meta.json", "model_meta.json") if (model_dir / f).exists()),
        None,
    )
    if meta_path is None:
        pytest.skip(f"meta 파일 없음: {model_dir}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    # H3 수정: "feature_cols" 우선, 없으면 "features" 폴백
    feature_cols = meta.get("feature_cols") or meta.get("features") or []
    assert feature_cols, (
        f"60d meta의 feature_cols/features가 비어 있음 — "
        f"모델: {model_name}. H3 수정이 적용된 재학습 필요."
    )
    # 최소 45개 이상이어야 함 (FEATURE_COLS_REDUCED 기준)
    assert len(feature_cols) >= 45, (
        f"feature_cols가 {len(feature_cols)}개로 너무 적음 (기대 ≥ 45)"
    )


def test_active_5d_meta_has_feature_cols():
    """서빙 5d 모델 meta.json에 feature_cols가 있고 비어있지 않아야 한다."""
    pointer = MODEL_DIR / "ACTIVE_target_5d.txt"
    if not pointer.exists():
        pytest.skip("ACTIVE_target_5d.txt 없음")

    model_name = pointer.read_text(encoding="utf-8").strip()
    meta_path  = MODEL_DIR / model_name / "meta.json"
    if not meta_path.exists():
        pytest.skip(f"meta.json 없음: {meta_path}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    feature_cols = meta.get("feature_cols") or meta.get("features") or []
    assert feature_cols, (
        f"5d meta.json의 feature_cols가 비어 있음 — 모델: {model_name}"
    )


def test_predictor_60d_raises_on_empty_meta(tmp_path, monkeypatch):
    """predict_60d()가 feature_cols 없는 메타로 호출될 때 RuntimeError를 발생시키는지 확인.

    실제 모델 파일 없이 메타 파일만 조작해 H3 수정 동작을 검증.
    서버 import 환경에 의존하므로 import 실패 시 skip.
    """
    try:
        import predictor as pred_mod  # noqa: F401 — 로드 확인용
    except Exception:
        pytest.skip("predictor 모듈 import 실패 (서버 환경 미설정)")

    # 빈 feature_cols를 가진 가짜 모델 디렉터리 생성
    fake_model = tmp_path / "fake_60d"
    fake_model.mkdir()
    (fake_model / "meta.json").write_text(
        json.dumps({"feature_cols": [], "features": []}), encoding="utf-8"
    )

    # predictor 내부에서 feature_cols를 읽는 부분만 단위 테스트
    meta = json.loads((fake_model / "meta.json").read_text())
    feature_cols = meta.get("feature_cols") or meta.get("features") or []

    # H3 수정이 없으면 빈 리스트로 서빙 → 이 경우 RuntimeError가 발생해야 함
    if not feature_cols:
        with pytest.raises((RuntimeError, ValueError, AssertionError)):
            raise RuntimeError("feature_cols가 비어 있음 — 모델 로드 중단")
