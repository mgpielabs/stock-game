"""
KIS Open API 공용 토큰 발급/캐시 모듈.
kis_investor_collector.py, kis_investor_backfill.py 양쪽이 동일한 함수를 import해서 사용.
토큰 캐시 파일: backend/data/.kis_token_cache.json (gitignore 처리됨)
"""
import json
import os
import time
from pathlib import Path

import requests

TOKEN_CACHE_PATH = Path(__file__).parent / ".kis_token_cache.json"
BASE_URL = "https://openapi.koreainvestment.com:9443"


def get_access_token() -> str:
    """KIS 토큰 발급. 분당 발급 제한이 있어 파일에 캐시해서 만료 전까지 재사용."""
    if TOKEN_CACHE_PATH.exists():
        try:
            cached = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("expires_at", 0) > time.time() + 300:
                return cached["access_token"]
        except Exception:
            pass

    app_key = os.getenv("KIS_APP_KEY")
    app_secret = os.getenv("KIS_APP_SECRET")
    resp = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        headers={"content-type": "application/json"},
        data=json.dumps({
            "grant_type": "client_credentials",
            "appkey": app_key,
            "appsecret": app_secret,
        }),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    expires_in = int(data.get("expires_in", 86400))
    TOKEN_CACHE_PATH.write_text(
        json.dumps({"access_token": token, "expires_at": time.time() + expires_in}),
        encoding="utf-8",
    )
    return token
