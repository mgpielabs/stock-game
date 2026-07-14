# v1.2 전환 핸드오프 — 5d 운용 중단 (60d 단독)

> 이 문서만 읽고 전환을 실행할 수 있도록 작성됨.  
> 작성: 2026-07-14 | 기반: `allocation_backtest.py` + `운용 규칙 v1.1`

---

## 1. 전환 내용

| 항목 | v1.1 (현재) | v1.2 (전환 후) |
|---|---|---|
| 슬리브 배분 | 5d 30% + 60d 70% | **60d 100%** |
| 5d paper_trades 진입 | 5거래일 주기 자동 기록 | **중단** (shadow 기록만 유지) |
| 60d paper_trades 진입 | 60거래일 주기 자동 기록 | 변경 없음 |
| prediction_log (5d) | daily_pipeline step 10 자동 기록 | **변경 없음** (모델 신호는 계속 추적) |
| G2 게이트 (5d) | bear 2연속 차단 | 무의미해짐 (진입 자체가 없음) |

---

## 2. 전환 근거 수치

### 배분 4자 대조 (IS 2022~2024 + OOS 2025~2026, `allocation_backtest.py`)

| 배분 | 총수익 | MDD | 비고 |
|---|---|---|---|
| (a) 30:70 현행 v1.1 | +113.3% | -31.5% | — |
| **(b) 60d 단독 0:100** | **+184.5%** | **-24.0%** | **총수익 +71.2%p, MDD 7.5%p 개선** |
| (c) 15:85 | +148.9% | -27.7% | |
| (d) 참고 50:50 | +65.8% | -36.4% | 5d 높을수록 단조 악화 |

### 5d 단독 성과 (G2 게이트, 1억 기준)

| 연도 | 5d 수익 | 판정 |
|---|---|---|
| 2022 | -48.9% | IS |
| 2023 | -11.6% | IS |
| 2024 | +1.3% | IS |
| 2025 | +40.6% | OOS |
| 2026 YTD | -26.6% | OOS |
| **누적** | **-52.9%** | **구조적 drag** |

### 분산 효과 없음

- 5d/60d 월별 수익률 상관: **+0.244** (양의 상관 — 헤지 효과 없음)  
- 5d 손실월 60d 평균: **-3.13%** (5d가 나쁠 때 60d도 나쁨)

---

## 3. 전환 조건 (사전 정의 — 결과 보기 전 고정됨)

두 조건 **모두** 충족 시에만 전환 실행.

### 조건 (a): 60d 첫 만기 실측

- 60d 첫 배치: **2026-07-01** (4종목, status=open)
- 예상 만기: **2026-09-22** 전후 (60 거래일)
- 확인 방법:
  ```sql
  SELECT recommended_date, symbol, status, close_date, return_pct
  FROM paper_trades
  WHERE horizon = 60
  ORDER BY recommended_date, symbol;
  ```
- **통과 기준**: 1~2기간 청산 완료, 60d 모델 초과수익(return_pct - 당일 market_index 수익)이  
  val 기준선 K10=+0.195 / K20=+0.170의 **절반(±0.1) 이상** (백테스트 완전 재현은 불필요, 구현 정상 확인 목적)

### 조건 (b): 5d 라이브·shadow 일관성

- 5d 라이브 현재 성과 확인:
  ```
  GET http://127.0.0.1:8001/api/live-performance
  ```
- 5d shadow(게이트 차단 가상기록) vs live 비교:
  ```
  GET http://127.0.0.1:8001/api/paper/shadow-vs-live
  ```
- **통과 기준**: 5d 라이브가 "구조적 열위"(누적 음수, 승률 < 50%) 패턴과 **모순되지 않음**  
  *(라이브가 갑자기 크게 좋아졌으면 전환 재검토)*

---

## 4. 실행 절차

### 4-1. 코드 변경 — `backend/server/main.py`

수정 대상: `lifespan()` 함수 내 `# ── 5d 슬리브` 블록 (현재 약 293~315번째 줄)

**현재 (v1.1)**:
```python
# ── 5d 슬리브: G2 게이트 적용 ──
if _gate_blocked:
    # G2 차단 → 5d 신규진입 없음, shadow에만 기록
    from prediction_logger import log_shadow_predictions
    log_shadow_predictions(_state.latest_date, "5d", top10, horizon=5)
    logger.info("G2 게이트 차단 — 5d shadow 기록 (%s)", _gate.get("reason", ""))
else:
    # 5거래일 주기 리밸런싱 (백테스트 일치)
    if should_enter_5d(_state.latest_date):
        inserted = record_recommendations(
            _state.latest_date, top10,
            ...
            horizon=5,
        )
        ...
    else:
        logger.info("5d 리밸런싱 주기 미도달 — 스킵 (%s)", _state.latest_date)
```

**변경 후 (v1.2)**:
```python
# ── 5d 슬리브: v1.2 — 5d 운용 중단, shadow 기록만 유지 ──
# 근거: allocation_backtest.py (2026-07-12) — 5d cum -52.9%, 60d 단독이 +71.2%p 우위
from prediction_logger import log_shadow_predictions
log_shadow_predictions(_state.latest_date, "5d", top10, horizon=5)
logger.info("v1.2: 5d 운용 중단 — shadow 기록만 (%s)", _state.latest_date)
# (G2 게이트 로직 불필요 — 진입 자체가 없음)
```

**변경 요약**: 기존 `if _gate_blocked: ... else: if should_enter_5d(...):` 블록 전체를  
단순 shadow 기록 3줄로 교체. `_gate_blocked` / `should_enter_5d` 분기 제거.

### 4-2. 검증

```powershell
# 1. pytest 전체
cd c:\01coding\stock-game\backend
uv run python -m pytest tests/ -v

# 2. PM2 재시작
npx pm2 restart stock-backend --update-env

# 3. 코드 변경 반영 확인 (로그에서 "v1.2" 문자열 확인)
npx pm2 logs stock-backend --lines 30

# 4. /status 페이지 확인
# http://127.0.0.1:8001/status
```

### 4-3. CLAUDE.md 기록

`운용 규칙` 절의 슬리브 표에서:
- `슬리브 비중` 행: `5d 30% / 60d 70%` → `5d 0% / 60d 100%`
- `G2 게이트` 행: v1.2에서 5d 진입이 없으므로 "미적용 (v1.2)"으로 표기

`관찰 규칙` 행동 트리거 표에서:
- `60d 첫 만기 도달` 트리거 항목: "실행 완료 (2026-{날짜})" 로 갱신

---

## 5. 전환 이후 유지되는 것

| 항목 | 계속 유지 |
|---|---|
| `prediction_log` 5d 기록 | ✅ daily_pipeline step 10 — `predict_today()` 직접 호출, main.py 변경 무관 |
| 5d shadow 기록 | ✅ 위 코드에서 `log_shadow_predictions` 계속 실행 |
| 5d 모델 서빙 | ✅ `/api/predictions/today` 변경 없음 |
| 60d 진입 | ✅ 변경 없음 |
| G2 게이트 계산 | ✅ (shadow 기록용 및 API 표시용으로 유지) |
| paper_trades 기존 5d 청산 | ✅ `daily_pipeline.py` step 4 (`close_expired_trades`) — 변경 없음 |

---

## 6. 확인 쿼리 모음

```sql
-- 60d 배치 현황
SELECT recommended_date, COUNT(*) n, status, MIN(close_date) first_close
FROM paper_trades WHERE horizon = 60
GROUP BY recommended_date, status
ORDER BY recommended_date;

-- 60d 만기 완료 성과
SELECT recommended_date, COUNT(*) n, 
       ROUND(AVG(return_pct), 2) avg_ret,
       SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) wins
FROM paper_trades WHERE horizon = 60 AND status = 'closed'
GROUP BY recommended_date;

-- 5d 라이브 성과 (전체)
SELECT COUNT(*) n,
       ROUND(AVG(return_pct), 2) avg_ret,
       SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) win_rate
FROM paper_trades WHERE horizon = 5 AND status = 'closed';

-- shadow vs live 비교 (API)
-- GET http://127.0.0.1:8001/api/paper/shadow-vs-live
```

---

## 7. 전환 실패 조건 (조건 불충족 시)

| 상황 | 대응 |
|---|---|
| 60d 첫 만기가 크게 음수 (시장 초과수익 << 0) | v1.1 유지, 다음 만기 추가 확인 |
| 5d 라이브가 갑자기 좋아짐 (연속 3기간 양수) | 전환 재검토, 5d 슬리브 유지 검토 |
| pytest 실패 | 코드 문제 먼저 수정 |
| PM2 재시작 실패 | `운영.md` B절 참고 |
