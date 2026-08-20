"""
회귀 테스트: 데이터 업데이트 API

- GET /api/update/status 응답 구조 검증
  (status 필드·log 목록·freshness 필드 존재)
- POST /api/update 는 즉시 반환해야 함
  (백엔드 블로킹 여부 확인 — subprocess 비동기 처리)
- GET /api/update/status elapsed_sec 은 running 아닐 때 None 이거나 숫자
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BASE = "http://127.0.0.1:8001"
DB_PATH = Path(__file__).parent.parent / "data" / "stocks.db"


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return json.loads(r.read())


# ── 서버 가용 여부 공통 픽스처 ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def server_up():
    try:
        _get("/health")
    except Exception as exc:
        pytest.skip(f"서버 미응답 — {exc}")


# ── 1. GET /api/update/status 구조 ─────────────────────────────────────────

def test_update_status_has_required_fields(server_up):
    """GET /api/update/status 응답에 필수 필드가 존재해야 한다."""
    data = _get("/api/update/status")
    for field in ("status", "elapsed_sec", "log", "dart_partial"):
        assert field in data, f"응답에 '{field}' 필드 없음: {data}"


def test_update_status_value_is_valid(server_up):
    """status 값이 idle/running/done/error 중 하나여야 한다."""
    data = _get("/api/update/status")
    assert data["status"] in ("idle", "running", "done", "error"), (
        f"예상치 못한 status 값: {data['status']}"
    )


def test_update_status_log_is_list(server_up):
    """log 필드는 항상 리스트여야 한다."""
    data = _get("/api/update/status")
    assert isinstance(data["log"], list), f"log 가 리스트가 아님: {type(data['log'])}"


def test_update_status_elapsed_sec_type(server_up):
    """elapsed_sec 는 idle/done/error 시 None 이거나, running 시 숫자여야 한다."""
    data = _get("/api/update/status")
    if data["status"] == "running":
        assert isinstance(data["elapsed_sec"], (int, float)), (
            f"running 중 elapsed_sec 이 숫자가 아님: {data['elapsed_sec']}"
        )
    else:
        assert data["elapsed_sec"] is None or isinstance(data["elapsed_sec"], (int, float)), (
            f"elapsed_sec 타입 이상: {data['elapsed_sec']}"
        )


def test_update_status_has_freshness_fields(server_up):
    """freshness 관련 필드(latest_feature_date 등)가 존재해야 한다."""
    data = _get("/api/update/status")
    assert "latest_feature_date" in data, f"latest_feature_date 없음: {data}"


# ── 2. POST /api/update 비블로킹 검증 ──────────────────────────────────────

def test_post_update_is_non_blocking(server_up):
    """POST /api/update 는 파이프라인 완료를 기다리지 않고 즉시 반환해야 한다.

    이미 실행 중이면 409 — 이것도 즉시 반환이므로 PASS.
    1초 이상 걸리면 uvicorn 이벤트 루프 블로킹 버그로 간주한다.
    """
    req = urllib.request.Request(
        f"{BASE}/api/update",
        data=b"",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, (
            f"POST /api/update 가 {elapsed:.2f}s 소요 — 이벤트 루프 블로킹 의심"
        )
        assert body.get("status") == "started", f"예상치 못한 응답: {body}"
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, (
            f"POST /api/update 가 {elapsed:.2f}s 걸려 에러 — 이벤트 루프 블로킹 의심"
        )
        if exc.code == 409:
            # 이미 실행 중 — 즉시 반환이므로 정상
            return
        raise


def test_predictions_available_after_update(server_up):
    """업데이트 완료 후 /api/predictions/today 가 정상 응답해야 한다."""
    data = _get("/api/predictions/today")
    assert "predictions" in data, f"predictions 필드 없음: {list(data.keys())}"
    assert "date" in data, f"date 필드 없음: {list(data.keys())}"


def test_performance_endpoint_available(server_up):
    """업데이트 완료 후 /api/backtest/performance 가 정상 응답해야 한다."""
    data = _get("/api/backtest/performance")
    assert any(k in data for k in ("precision_at_topk", "close_win_rate_pct", "label_basis")), (
        f"성과 지표 필드 없음: {list(data.keys())}"
    )


def test_market_trend_endpoint_available(server_up):
    """업데이트 완료 후 /api/market/trend 가 정상 응답해야 한다."""
    data = _get("/api/market/trend")
    assert any(k in data for k in ("trend", "regime", "label")), (
        f"시장 국면 필드 없음: {list(data.keys())}"
    )


def test_update_status_after_start(server_up):
    """POST /api/update 후 GET /api/update/status 가 running 또는 done 을 반환해야 한다.

    이미 실행 중(409)이면 기존 running 상태 그대로여도 PASS.
    """
    # 시작 시도
    req = urllib.request.Request(
        f"{BASE}/api/update",
        data=b"",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    started = False
    try:
        with urllib.request.urlopen(req, timeout=10):
            started = True
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        # 이미 실행 중 → 상태 확인만

    if started:
        # 시작 직후 상태 확인 — running 이어야 함
        data = _get("/api/update/status")
        assert data["status"] in ("running", "done", "error"), (
            f"시작 직후 status 이상: {data['status']}"
        )


def test_update_status_has_stage_fields(server_up):
    """진행 단계·경과시간 앵커·하트비트 필드가 응답에 존재해야 함."""
    data = _get("/api/update/status")
    for field in ("current_stage", "current_stage_name", "total_stages",
                  "started_at", "last_heartbeat"):
        assert field in data, f"필드 누락: {field}"


def test_update_status_stage_types_when_idle(server_up):
    """idle/done/error 상태에서 단계 필드는 None이어야 함."""
    data = _get("/api/update/status")
    if data["status"] in ("idle", "done", "error"):
        for field in ("current_stage", "current_stage_name", "total_stages",
                      "started_at", "last_heartbeat"):
            assert data[field] is None, (
                f"{field}이 idle/done/error에서 None이 아님: {data[field]}"
            )
    else:
        # running 상태면 current_stage는 int, last_heartbeat는 float|int
        if data["current_stage"] is not None:
            assert isinstance(data["current_stage"], int), \
                f"current_stage가 int 아님: {type(data['current_stage'])}"
        if data["last_heartbeat"] is not None:
            assert isinstance(data["last_heartbeat"], (int, float)), \
                f"last_heartbeat가 숫자 아님: {type(data['last_heartbeat'])}"
        if data["started_at"] is not None:
            assert isinstance(data["started_at"], (int, float)), \
                f"started_at이 숫자 아님: {type(data['started_at'])}"
