"""
회귀 테스트: 60d 학습 분할의 purge gap 존재 확인

train_60d.py의 PURGE_DAYS=60 상수가 코드에 선언돼 있는지,
그리고 분할 로직이 purge_date를 사용하는지 소스 수준에서 검증.

현재 서빙 모델(target_60d_*)의 meta.json에 purge_days가 기록돼 있으면
그 값도 확인 (C1 수정 후 재학습 시 meta.json에 기록하면 자동 활성화).
"""
import ast
import re
import sys
from pathlib import Path

import pytest

BACKEND  = Path(__file__).parent.parent
ML_DIR   = BACKEND / "ml"
MODEL_DIR = BACKEND / "models"

TRAIN_60D = ML_DIR / "train_60d.py"
REQUIRED_PURGE_DAYS = 60


@pytest.mark.skipif(not TRAIN_60D.exists(), reason="train_60d.py 없음")
def test_purge_days_constant_exists():
    """PURGE_DAYS = 60 상수가 train_60d.py에 선언돼 있어야 한다."""
    src = TRAIN_60D.read_text(encoding="utf-8")
    match = re.search(r"PURGE_DAYS\s*=\s*(\d+)", src)
    assert match, "train_60d.py에 PURGE_DAYS 상수가 없음"
    value = int(match.group(1))
    assert value == REQUIRED_PURGE_DAYS, (
        f"PURGE_DAYS={value}, 기대값={REQUIRED_PURGE_DAYS}. "
        "60d 라벨 누수 방지용 정화 간격이 줄어들었음."
    )


@pytest.mark.skipif(not TRAIN_60D.exists(), reason="train_60d.py 없음")
def test_purge_logic_applied():
    """train_60d.py에서 purge_date를 계산하고 df_tr 분할에 적용하는 로직이 있어야 한다."""
    src = TRAIN_60D.read_text(encoding="utf-8")
    # purge_date 계산 존재 여부 확인
    assert "purge_date" in src, (
        "train_60d.py에 purge_date 변수가 없음 — C1 수정이 누락됐거나 코드가 변경됨"
    )
    # df_tr이 purge_date 기준으로 필터링되는지 확인
    assert re.search(r"df_tr\s*=.+purge_date", src, re.DOTALL), (
        "df_tr이 purge_date 기준으로 필터링되지 않음 — purge gap이 적용 안 됐을 수 있음"
    )


def test_active_60d_model_purge_recorded():
    """현재 서빙 60d 모델의 meta.json에 purge_days가 기록돼 있으면 60임을 확인.
    미기록이면 '재학습 필요' 경고만 출력 (실패 아님 — 재학습 전 상태가 정상)."""
    import json
    pointer = MODEL_DIR / "ACTIVE_target_60d.txt"
    if not pointer.exists():
        pytest.skip("ACTIVE_target_60d.txt 없음")

    model_name = pointer.read_text(encoding="utf-8").strip()
    model_dir  = MODEL_DIR / model_name
    # 60d는 model_meta.json, 5d는 meta.json 사용
    meta_path = next(
        (model_dir / f for f in ("meta.json", "model_meta.json") if (model_dir / f).exists()),
        None,
    )
    if meta_path is None:
        pytest.skip(f"meta 파일 없음: {model_dir}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if "purge_days" not in meta:
        # 경고만 — 재학습 전 기존 모델은 purge_days 미기록이 정상
        pytest.warns(
            UserWarning,
            match="",
        ) if False else None
        import warnings
        warnings.warn(
            f"60d 서빙 모델 '{model_name}'의 meta.json에 purge_days 없음 — "
            "C1 수정 후 재학습하면 자동 기록됨",
            UserWarning,
        )
        return

    assert meta["purge_days"] == REQUIRED_PURGE_DAYS, (
        f"meta.json의 purge_days={meta['purge_days']}, 기대={REQUIRED_PURGE_DAYS}"
    )
