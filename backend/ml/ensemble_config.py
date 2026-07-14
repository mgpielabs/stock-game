"""
CatBoost+XGBoost 운영 블렌드 가중치 — predictor.py(서빙)와 train.py(재학습 시 calibrator
학습)가 공유. 한쪽만 바꾸고 다른 쪽을 안 바꾸면 calibration이 실제 서빙 확률분포와
어긋나는 버그가 생기므로 공용 모듈로 분리(2026-06-22).

근거: backend/ml/search_ensemble_weights.py — CatBoost 단독보다 0.8/0.2 블렌드가
모든 K에서 같거나 나음. LightGBM은 제외(모든 조합에서 도움 안 됨).
"""

CAT_BLEND_WEIGHT = 0.8
XGB_BLEND_WEIGHT = 0.2
