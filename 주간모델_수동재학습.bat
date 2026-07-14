@echo off
title StockGame Weekly Retrain
echo ============================================
echo  주간 전체 재학습 시작 (run_full_pipeline.py)
echo  - 전종목 3년치 재수집 + 피처 재계산 + 모델 재학습 (약 30분)
echo  - 완료 시 운영 모델 자동 교체 + 서버 반영
echo  - 무거운 단계는 내부적으로 낮은 CPU 우선순위로 실행됨
echo ============================================
echo.

"%~dp0backend\.venv\Scripts\python.exe" "%~dp0backend\run_full_pipeline.py"

echo.
echo ============================================
echo  완료! 이 창을 닫아도 됩니다.
echo  로그: backend\pipeline_full.log
echo ============================================
pause
