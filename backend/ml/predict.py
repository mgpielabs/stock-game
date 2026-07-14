"""
학습된 모델로 특정 날짜의 종목별 상승 확률 예측
"""

import argparse
import os
import sqlite3
from pathlib import Path
from typing import Optional

import lightgbm as lgb
import pandas as pd

from dataset import DB_PATH, FEATURE_COLS

MODELS_DIR = Path(__file__).parent.parent / "models"


def load_model(target_col: str = "target_1d", version: Optional[str] = None) -> lgb.Booster:
    """
    models/{target_col}_{version}/model.lgb 로드.
    version 미지정 시 가장 최신 버전 사용(폴더 mtime 기준 — 실험용 폴더 이름이
    섞여도 문자열 정렬로 잘못된 폴더가 선택되지 않도록 함).
    """
    candidates = list(MODELS_DIR.glob(f"{target_col}_*/model.lgb"))
    if not candidates:
        raise FileNotFoundError(f"저장된 모델 없음: {target_col}")

    if version:
        candidates = [p for p in candidates if version in str(p)]
        if not candidates:
            raise FileNotFoundError(f"버전 없음: {version}")

    model_path = max(candidates, key=lambda p: os.path.getmtime(p))
    return lgb.Booster(model_file=str(model_path))


def predict_for_date(
    date: str,
    target_col: str = "target_1d",
    top_k: int = 10,
    version: Optional[str] = None,
) -> pd.DataFrame:
    """
    특정 날짜의 전종목 피처로 상승 확률 예측.
    반환: DataFrame[rank, symbol, proba] — 확률 내림차순, top_k 행.
    top_k=0 이면 전종목 반환.
    """
    model = load_model(target_col, version)

    with sqlite3.connect(DB_PATH) as conn:
        features = pd.read_sql_query(
            "SELECT * FROM features WHERE date = ?",
            conn,
            params=(date,),
        )

    if features.empty:
        raise ValueError(f"피처 없음: {date}")

    available = [c for c in FEATURE_COLS if c in features.columns]
    X = features[available].astype(float)

    features = features.copy()
    features["proba"] = model.predict(X)
    result = (
        features[["symbol", "proba"]]
        .sort_values("proba", ascending=False)
        .reset_index(drop=True)
    )
    if top_k > 0:
        result = result.head(top_k)

    result.index = result.index + 1  # 1-based rank
    result.index.name = "rank"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="종목 상승 확률 예측")
    parser.add_argument("--date",    type=str, required=True,   help="예측 기준일 YYYYMMDD")
    parser.add_argument("--target",  choices=["target_1d", "target_5d"], default="target_1d")
    parser.add_argument("--top-k",   type=int, default=10,      help="상위 K 종목 (0=전체)")
    parser.add_argument("--version", type=str, default=None,    help="모델 버전 (미지정시 최신)")
    args = parser.parse_args()

    result = predict_for_date(
        args.date, target_col=args.target, top_k=args.top_k, version=args.version
    )
    print(result.to_string())


if __name__ == "__main__":
    main()
