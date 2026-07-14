"""
[1회성 테스트 전용 — DB 변경 없음] 한국투자증권(KIS) Open API의 "종목별 투자자매매동향"
(inquire-investor, TR_ID FHKST01010900) 응답에 연기금이 기관계와 분리된 필드로 오는지
확인하기 위한 단발 호출 스크립트.

2026-06-27 실제 실행 결과(005930): 응답 필드는 prsn_*(개인)/frgn_*(외국인)/orgn_*(기관계)
21개뿐 — 연기금/기금 관련 필드 전무. 연기금은 KIS에서도 시장 전체 단위 API
(inquire-investor-daily-by-market, FHPTJ04040000)에만 분리돼 있고 종목 단위로는 제공 안 됨.
이 결론에 따라 backend/data/kis_investor_collector.py는 개인/외국인/기관계 3종만 수집함
(CLAUDE.md "KIS Open API 투자자매매동향 수집 파이프라인" 참고).

조회성 API만 호출함(시세/투자자 동향) — 주문/계좌 API는 전혀 건드리지 않음.
.env의 KIS_APP_KEY/KIS_APP_SECRET을 읽어 토큰 발급 → 1회 조회 → 원본 응답 그대로 출력.

실행: python kis_investor_field_test.py [종목코드, 기본 005930]
"""

import json
import os
import sys
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    BACKEND_DIR = Path(__file__).resolve().parent.parent
    load_dotenv(BACKEND_DIR / ".env")
except ImportError:
    pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
BASE_URL = "https://openapi.koreainvestment.com:9443"  # 실전투자 도메인


def get_access_token() -> str:
    resp = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        headers={"content-type": "application/json"},
        data=json.dumps({
            "grant_type": "client_credentials",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
        }),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_investor_trend(token: str, ticker: str) -> dict:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010900",
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker,
    }
    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor",
        headers=headers, params=params, timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "005930"

    if not APP_KEY or not APP_SECRET:
        print("KIS_APP_KEY / KIS_APP_SECRET이 .env에 설정되지 않았습니다.")
        print("backend/.env 파일에 실제 값을 채운 뒤 다시 실행하세요.")
        sys.exit(1)

    print(f"종목코드: {ticker}")
    print("토큰 발급 중...")
    token = get_access_token()
    print("토큰 발급 완료. 투자자매매동향 조회 중...")

    data = fetch_investor_trend(token, ticker)
    print("\n=== 원본 응답 ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    output = data.get("output", [])
    if isinstance(output, list) and output:
        keys = list(output[0].keys())
    elif isinstance(output, dict):
        keys = list(output.keys())
    else:
        keys = []

    print("\n=== 응답 필드 목록 ===")
    for k in keys:
        print(" ", k)

    pension_keywords = ["기금", "연기금", "pension", "fund"]
    hits = [k for k in keys if any(kw in k.lower() or kw in k for kw in pension_keywords)]
    print("\n=== 연기금/기금 관련 필드 매칭 결과 ===")
    if hits:
        print("발견됨:", hits)
    else:
        print("연기금/기금 관련 필드 없음 — 기관계(orgn)에 합산돼 있을 가능성")


if __name__ == "__main__":
    main()
