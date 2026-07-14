"""
[진단 전용 — 운영 코드/모델 수정 없음] 앙상블 가중치 탐색

현재 운영 모델(target_5d 최신 디렉터리)의 3개 pkl(cat/xgb/lgbm)을 그대로 불러와
기존 val set에 대해 다양한 가중치 조합의 precision@10/20/30 + AUC를 비교.
재학습 없음 — 이미 학습된 모델의 예측값만 재사용.

중요: 실제 서빙(server/predictor.py)은 이미 CatBoost 단독 모델만 사용 중이고
train.py의 "동일가중 앙상블"은 평가 리포트용으로만 계산되며 서빙에는 안 쓰임.
즉 이 스크립트의 결과는 ① train.py가 meta.json에 남기는 평가지표를 현실에 맞게
고치는 데 참고하거나, ② 만약 단일 cat보다 더 나은 블렌드가 있다면 서빙 로직에
멀티모델 블렌딩을 새로 추가하는 게 가치 있는지 판단하는 데 쓰임.

실행: python search_ensemble_weights.py
"""

import pickle
import sys
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000040)
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
from dataset import build_dataset
from evaluate import precision_at_topk

MODELS_DIR = Path(__file__).parent.parent / "models"
pd.set_option("display.width", 140)


def find_latest_model_dir(target_col: str = "target_5d") -> Path:
    candidates = sorted(
        MODELS_DIR.glob(f"{target_col}_*/model_cat.pkl"),
        key=lambda p: p.parent.stat().st_mtime,
    )
    return candidates[-1].parent


def main():
    model_dir = find_latest_model_dir()
    print(f"대상 모델: {model_dir.name}\n")

    print("데이터셋 빌드 중...")
    X_train, y_train, X_val, y_val, val_meta, _ = build_dataset(target_col="target_5d", val_days=252)

    probas = {}
    for name, fname in [("cat", "model_cat.pkl"), ("xgb", "model_xgb.pkl"), ("lgbm", "model_lgbm.pkl")]:
        with open(model_dir / fname, "rb") as f:
            mdl = pickle.load(f)
        probas[name] = mdl.predict_proba(X_val)[:, 1]

    combos = [
        ("CB 1.0 / XGB 0.0 / LGB 0.0 (CatBoost 단독)", (1.0, 0.0, 0.0)),
        ("CB 0.0 / XGB 1.0 / LGB 0.0 (XGB 단독)",       (0.0, 1.0, 0.0)),
        ("CB 0.0 / XGB 0.0 / LGB 1.0 (LGBM 단독)",      (0.0, 0.0, 1.0)),
        ("CB 0.34 / XGB 0.33 / LGB 0.33 (현재 운영 평가지표=동일가중)", (1/3, 1/3, 1/3)),
        ("CB 0.6 / XGB 0.3 / LGB 0.1",  (0.6, 0.3, 0.1)),
        ("CB 0.5 / XGB 0.4 / LGB 0.1",  (0.5, 0.4, 0.1)),
        ("CB 0.7 / XGB 0.3 / LGB 0.0",  (0.7, 0.3, 0.0)),
        ("CB 0.8 / XGB 0.2 / LGB 0.0",  (0.8, 0.2, 0.0)),
        ("CB 0.5 / XGB 0.5 / LGB 0.0",  (0.5, 0.5, 0.0)),
        ("CB 0.9 / XGB 0.1 / LGB 0.0",  (0.9, 0.1, 0.0)),
    ]

    rows = []
    for label, (wc, wx, wl) in combos:
        blend = wc * probas["cat"] + wx * probas["xgb"] + wl * probas["lgbm"]
        auc = roc_auc_score(y_val, blend)
        topk = precision_at_topk(y_val, blend, val_meta)
        rows.append({
            "조합": label, "AUC": round(auc, 4),
            "P@10": round(topk.get(10, float("nan")), 4),
            "P@20": round(topk.get(20, float("nan")), 4),
            "P@30": round(topk.get(30, float("nan")), 4),
        })

    out = pd.DataFrame(rows).sort_values("P@10", ascending=False)
    print(out.to_string(index=False))
    print()
    best = out.iloc[0]
    print(f"P@10 기준 최고 조합: {best['조합']} (P@10={best['P@10']})")


if __name__ == "__main__":
    main()
