@echo off
title StockGame Data Update
echo ============================================
echo  데이터 수동 업데이트 시작 (daily_pipeline.py)
echo  - 시세/피처 증분 수집, 서버 재시작, 모의투자 청산
echo  - 섹터/BPS DART 수집은 skip 로직으로 이어받음
echo  - 무거운 단계는 내부적으로 낮은 CPU 우선순위로 실행됨
echo ============================================
echo.

"%~dp0backend\.venv\Scripts\python.exe" "%~dp0backend\daily_pipeline.py"

echo.
echo ============================================
echo  완료! 이 창을 닫아도 됩니다.
echo  로그: backend\pipeline_daily.log
echo ============================================
pause
