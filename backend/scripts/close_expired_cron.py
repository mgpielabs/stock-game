"""
모의투자 자동 청산 크론 스크립트

PM2 cron_restart '0 16 * * 1-5' 로 평일 16:00에 실행.
close_expired_trades() 호출 후 결과 로깅하고 종료.
"""

import logging
import sys
from datetime import date
from pathlib import Path

# paper_trader 경로 추가
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

from paper_trader import close_expired_trades  # noqa: E402

today = date.today().strftime("%Y%m%d")
logger.info("=== 모의투자 자동 청산 시작: %s ===", today)

result = close_expired_trades(today)
logger.info("청산 완료: closed=%d expired=%d", result["closed"], result["expired"])
logger.info("=== 완료 ===")
