"""
확률 calibration 공용 유틸 — train.py(재학습 시 calibrator 갱신)와
calibration_analysis.py(진단/검증)가 함께 사용. 모델 학습마다 일관된 방식으로
보정함수를 만들고 검증하기 위해 분리(2026-06-22, 진단#7 후속).
"""

from typing import Tuple

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression


def decile_gap(proba: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    """예측확률 N분위별 |평균예측확률 - 실제적중률| 의 평균 — calibration 정도 측정용."""
    df = pd.DataFrame({"proba": proba, "y": y})
    df["decile"] = pd.qcut(df["proba"], bins, labels=False, duplicates="drop")
    g = df.groupby("decile").agg(p=("proba", "mean"), a=("y", "mean"))
    return float((g["p"] - g["a"]).abs().mean())


def fit_calibrator_with_holdout_check(
    proba: np.ndarray, y: np.ndarray, dates: pd.Series, holdout_frac: float = 0.3,
) -> Tuple[IsotonicRegression, float, float]:
    """
    시간순으로 (1-holdout_frac):holdout_frac 분리 — 앞쪽으로만 학습한 calibrator를
    완전히 안 본 뒤쪽 구간에 적용해 calibration gap이 실제로 줄어드는지 확인(과적합 검증).
    검증 끝나면 최종 calibrator는 전체 데이터로 다시 학습해 반환 — held-out 분리는
    검증 목적일 뿐, 운영에 적용하는 최종본은 데이터를 최대한 활용.

    반환: (final_calibrator, held_out_gap_전, held_out_gap_후)
    """
    dates_arr = pd.Series(dates).values
    unique_dates = sorted(pd.Series(dates).unique())
    cutoff = unique_dates[int(len(unique_dates) * (1 - holdout_frac))]
    fit_mask = dates_arr < cutoff
    test_mask = ~fit_mask

    check = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    check.fit(proba[fit_mask], y[fit_mask])
    calibrated_test = check.transform(proba[test_mask])

    gap_before = decile_gap(proba[test_mask], y[test_mask])
    gap_after = decile_gap(calibrated_test, y[test_mask])

    final = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    final.fit(proba, y)
    return final, gap_before, gap_after
