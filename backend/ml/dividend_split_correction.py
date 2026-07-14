"""
[검증/보정 전용 — 기존 검증 파일·DB·모델 변경 없음] 배당 point-in-time 재계산의
"분할비율 미보정" 문제 수정 (2026-06-26).

배경 (chart_pattern_validation.py류와 동일한 "검증 전용" 카테고리): dividend_yield_pit_validation.py의
load_dividend_yield_pit()는 dps_pit(과거 공시 DPS) ÷ close(현재 pykrx 수정종가)로 point-in-time
수익률을 계산하는데, 해당 종목이 배당 공시 *이후* 주식분할/병합/무상증자/감자를 한 번이라도
했다면 분자(미조정 과거 DPS)와 분모(분할조정된 현재가)의 스케일이 어긋나 수익률이 왜곡됨
(미원화학 10배, INVENI 5배 등 — CLAUDE.md "고배당 배당수익률 버그 수정 + 재검증" 절 참고).

이 스크립트는 그 DPS를 분할/병합 비율로 보정한 버전을 "기존 로직과 나란히" 계산한다.
기존 dividend_yield_pit_validation.py, main.py의 _compute_dps_map() 등은 전혀 건드리지 않음.

비율 도출 방법(우선순위):
  1. DART에서 해당 종목의 주식분할/병합/무상증자/감자 결정 공시 원문을 찾아 분할전후
     발행주식총수(또는 1주당 가액)로 정확한 배수 K를 계산 (= shares_after/shares_before).
  2. 문서 파싱이 실패하면, "DART 공시 dividend_yield vs 우리 가격 기반 재계산 yield" 비율을
     가장 가까운 깨끗한 배수(2~25배 등)에 스냅한 값으로 대체 (이전 조사에서 89개 종목 중
     15개 샘플 검증 87% 적중 확인된 방법 — CLAUDE.md 작업 이력 참고).

보정 공식: dps_corrected = dps_raw / K_cumulative
  (해당 배당의 결산일(settlement_date) 이후 ~ 현재까지 발생한 모든 분할/병합/무상증자/감자의
   K를 누적곱). 우리 DB의 가격 시계열은 pykrx adjusted=True로 "항상 전체 기간 일관되게
   재조정된" 정적 시계열이므로(날마다 다시 조정되는 게 아님), 평가 시점(date)과 무관하게
   디스클로저당 단일 보정값이면 충분함(증명은 대화 이력 참고).

실행: python dividend_split_correction.py [--max-candidates N] [--use-cache]
출력:
  dividend_split_events.json       — DART에서 찾은 분할/병합/무상증자/감자 이벤트 원본
  dividend_split_correction_factors.csv — (symbol, biz_year)별 최종 보정값 + 근거(source)
"""

import argparse
import json
import re
import sqlite3
import sys
import time
import zipfile
import io
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
import requests

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(DATA_DIR))
from disclosure import dart_get, DART_API_KEY  # noqa: E402


def load_corp_code_map_readonly() -> dict:
    """corp_code_map.json을 그대로 읽기만 함 — load_corp_code_map()의 자동 갱신(파일 쓰기)을
    피하기 위해 직접 로드 (이 스크립트는 SELECT 전용 규칙)."""
    with open(DATA_DIR / "corp_code_map.json", "r", encoding="utf-8") as f:
        return json.load(f)

OUT_DIR = Path(__file__).parent
EVENTS_CACHE = OUT_DIR / "dividend_split_events.json"
RATIOS_CACHE = OUT_DIR / "dividend_split_event_ratios.json"
FACTORS_CSV = OUT_DIR / "dividend_split_correction_factors.csv"
IGNORE_FILE = OUT_DIR / ".dividend_split_ignore.json"

DB_PATH = DATA_DIR / "stocks.db"

EVENT_KEYWORDS = ["주식분할결정", "주식병합결정", "무상증자결정", "감자결정"]
CLEAN_MULTIPLES = [1.5, 2, 2.5, 3, 3.33, 4, 5, 6, 6.67, 7, 7.5, 8, 10, 12, 12.5, 15, 16, 20, 25, 30, 40, 50, 100]
ALL_MULTIPLES = CLEAN_MULTIPLES + [1 / m for m in CLEAN_MULTIPLES]


def load_split_ignore_symbols() -> set:
    """비율이 깨끗한 배수와 우연히 맞아떨어지지만 실제로는 분할/병합이 아닌 것으로
    개별 조사 확인된 종목(예: 388050 — DART 자체 공시 중복입력 오류). 이 종목들은
    candidates에서 항상 제외해 잘못된 보정이 들어가지 않게 함 (2026-06-26,
    backend/data/.scale_check_ignore.json과 같은 패턴)."""
    if not IGNORE_FILE.exists():
        return set()
    try:
        data = json.loads(IGNORE_FILE.read_text(encoding="utf-8"))
        return set(data.keys()) if isinstance(data, dict) else set(data)
    except Exception:
        return set()


# ── 1. 후보 종목 식별 (DART 호출 없음, 순수 SELECT) ──────────────────────

def identify_candidates(conn: sqlite3.Connection) -> pd.DataFrame:
    """dividends.dividend_yield(원본 공시값) vs dps/현재DB종가 재계산값 비율을 종목별로 계산.
    액면가 오분모 패턴(이미 알려진 별개 버그)은 제외."""
    rows = conn.execute(
        """
        SELECT symbol, biz_year, settlement_date, record_date, ex_dividend_date,
               dps, dividend_yield, par_value
        FROM dividends
        WHERE dps IS NOT NULL AND dps < 1000000 AND dividend_yield IS NOT NULL AND dividend_yield > 0
        """
    ).fetchall()

    out = []
    for sym, biz, settle, rec, exdiv, dps, dy, par in rows:
        ref_date = rec or exdiv or settle
        if not ref_date:
            continue
        r = conn.execute(
            "SELECT close FROM prices WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
            (sym, ref_date),
        ).fetchone()
        if not r or not r[0]:
            continue
        close = r[0]
        recalc = dps / close * 100
        ratio = recalc / dy

        # 액면가 오분모 패턴(별개 버그, 분할과 무관) 제외
        if par and par > 0 and abs(dps / par * 100 - dy) < 0.5:
            continue

        out.append({
            "symbol": sym, "biz_year": biz, "settlement_date": settle,
            "dps": dps, "dividend_yield": dy, "close": close,
            "recalc_yield": round(recalc, 4), "ratio": round(ratio, 4),
        })
    return pd.DataFrame(out)


def snap_to_multiple(ratio: float, tol: float = 0.10):
    best = min(ALL_MULTIPLES, key=lambda m: abs(m - ratio) / m)
    reldiff = abs(best - ratio) / best
    return (best, reldiff) if reldiff <= tol else (None, None)


# ── 2. DART에서 분할/병합/증자/감자 결정 공시 조회 ──────────────────────

def fetch_decision_events(symbol: str, corp_code: str) -> list:
    events = []
    for bgn, end in [("20210101", "20231231"), ("20240101", "20260626")]:
        res = dart_get(
            "https://opendart.fss.or.kr/api/list.json",
            {"crtfc_key": DART_API_KEY, "corp_code": corp_code, "bgn_de": bgn, "end_de": end, "page_count": 100},
        )
        for it in (res.get("list") or []):
            nm = it["report_nm"]
            if any(k in nm for k in EVENT_KEYWORDS) and "정정" not in nm:
                events.append({"rcept_dt": it["rcept_dt"], "report_nm": nm, "rcept_no": it["rcept_no"]})
        time.sleep(0.25)
    return events


_NUM = r"([0-9][0-9,]*)"


def _nums_after(text: str, anchor: str, n: int = 2):
    idx = text.find(anchor)
    if idx < 0:
        return []
    tail = text[idx + len(anchor): idx + len(anchor) + 400]
    found = re.findall(_NUM, tail)
    vals = []
    for f in found:
        try:
            vals.append(int(f.replace(",", "")))
        except ValueError:
            continue
        if len(vals) >= n:
            break
    return vals


def parse_event_ratio(report_nm: str, rcept_no: str):
    """공시 원문에서 분할전후/병합전후/증자전후/감자전후 수치를 찾아 K(=after/before)를 계산."""
    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no}, timeout=20,
        )
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        raw = z.read(z.namelist()[0])
        text = raw.decode("cp949", errors="replace")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
    except Exception:
        return None

    try:
        if "주식분할결정" in report_nm or "주식병합결정" in report_nm:
            vals = _nums_after(text, "1주당 가액(원)", 2)
            if len(vals) == 2 and vals[0] > 0:
                return vals[0] / vals[1]  # K = par_before/par_after = shares_after/shares_before
            vals = _nums_after(text, "보통주식 (주)", 2)
            if len(vals) == 2 and vals[0] > 0:
                return vals[1] / vals[0]
        elif "무상증자결정" in report_nm:
            new_shares = _nums_after(text, "신주의 종류와 수 보통주식 (주)", 1)
            before = _nums_after(text, "증자전 발행주식총수 보통주식 (주)", 1)
            if new_shares and before and before[0] > 0:
                return (before[0] + new_shares[0]) / before[0]
        elif "감자결정" in report_nm:
            vals = _nums_after(text, "보통주식(주)", 2)
            if len(vals) == 2 and vals[0] > 0:
                return vals[1] / vals[0]
            vals = _nums_after(text, "보통주식 (주)", 2)
            if len(vals) == 2 and vals[0] > 0:
                return vals[1] / vals[0]
    except Exception:
        return None
    return None


# ── 3. 메인 ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-candidates", type=int, default=0, help="0=전체")
    parser.add_argument("--use-cache", action="store_true", help="dividend_split_events.json 캐시 재사용")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/4] 후보 종목 식별 (DART 호출 없음, 가격×배당 비율 분석)"); print("=" * 70)
    cand_df = identify_candidates(conn)
    print(f"분석 대상 행: {len(cand_df)}")

    big_mismatch = cand_df[(cand_df["ratio"] < 0.7) | (cand_df["ratio"] > 1.3)].copy()
    snapped = big_mismatch["ratio"].apply(lambda r: snap_to_multiple(r, tol=0.10))
    big_mismatch["matched_multiple"] = snapped.apply(lambda x: x[0])
    candidates = big_mismatch.dropna(subset=["matched_multiple"])

    ignore_symbols = load_split_ignore_symbols()
    n_before_ignore = len(candidates)
    candidates = candidates[~candidates["symbol"].isin(ignore_symbols)]
    if n_before_ignore != len(candidates):
        print(f"무시 목록 제외: {n_before_ignore - len(candidates)}건 ({sorted(ignore_symbols)})")

    cand_symbols = sorted(candidates["symbol"].unique())
    if args.max_candidates:
        cand_symbols = cand_symbols[: args.max_candidates]
    print(f"분할/병합 의심 종목: {len(cand_symbols)}개 (행 {len(candidates)}건)")

    print("\n" + "=" * 70); print("[2/4] DART에서 분할/병합/증자/감자 결정 공시 조회"); print("=" * 70)
    if args.use_cache and EVENTS_CACHE.exists():
        events_by_symbol = json.loads(EVENTS_CACHE.read_text(encoding="utf-8"))
        print(f"캐시 사용: {len(events_by_symbol)}종목")
    else:
        cmap = load_corp_code_map_readonly()
        events_by_symbol = {}
        for i, sym in enumerate(cand_symbols):
            corp = cmap.get(sym)
            if not corp:
                continue
            events_by_symbol[sym] = fetch_decision_events(sym, corp)
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(cand_symbols)} 조회 완료")
        EVENTS_CACHE.write_text(json.dumps(events_by_symbol, ensure_ascii=False, indent=2), encoding="utf-8")
    n_with_events = sum(1 for v in events_by_symbol.values() if v)
    print(f"이벤트 발견 종목: {n_with_events}/{len(events_by_symbol)}")

    print("\n" + "=" * 70); print("[3/4] 공시 원문에서 정확한 배수(K) 파싱"); print("=" * 70)
    if args.use_cache and RATIOS_CACHE.exists():
        event_ratios = json.loads(RATIOS_CACHE.read_text(encoding="utf-8"))
        print(f"캐시 사용: {len(event_ratios)}종목")
    else:
        event_ratios = {}  # symbol -> list of (rcept_dt, K)
        for sym, events in events_by_symbol.items():
            parsed = []
            for ev in events:
                k = parse_event_ratio(ev["report_nm"], ev["rcept_no"])
                if k and k > 0:
                    parsed.append({"rcept_dt": ev["rcept_dt"], "report_nm": ev["report_nm"], "K": k})
                time.sleep(0.2)
            if parsed:
                event_ratios[sym] = parsed
        RATIOS_CACHE.write_text(json.dumps(event_ratios, ensure_ascii=False, indent=2), encoding="utf-8")
    n_doc_parsed = len(event_ratios)
    print(f"문서 파싱으로 K 확보: {n_doc_parsed}종목")

    print("\n" + "=" * 70); print("[4/4] (symbol, biz_year)별 보정값 산출"); print("=" * 70)
    out_rows = []
    for _, row in candidates.iterrows():
        sym, biz, settle = row["symbol"], row["biz_year"], row["settlement_date"]
        if sym in event_ratios:
            # settlement_date 이후 발생한 이벤트만 누적
            applicable = [e for e in event_ratios[sym] if not settle or e["rcept_dt"] > settle]
            if applicable:
                k_cum = 1.0
                for e in applicable:
                    k_cum *= e["K"]
                # 문서 파싱값이 경험적 비율(ratio_empirical)과 20% 넘게 어긋나면 파싱 오류로
                # 간주하고 경험적 스냅값으로 대체 (예: 194700, 278650 — 잘못된 숫자쌍 매칭)
                if row["ratio"] and abs(k_cum - row["ratio"]) / row["ratio"] <= 0.20:
                    out_rows.append({
                        "symbol": sym, "biz_year": biz, "K_cumulative": round(k_cum, 4),
                        "correction_factor": round(1 / k_cum, 6),
                        "source": "dart_document", "n_events": len(applicable),
                        "ratio_empirical": row["ratio"], "matched_multiple_empirical": row["matched_multiple"],
                    })
                    continue
        # 문서 파싱 실패 → 경험적 비율(스냅값)로 대체
        out_rows.append({
            "symbol": sym, "biz_year": biz, "K_cumulative": row["matched_multiple"],
            "correction_factor": round(1 / row["matched_multiple"], 6),
            "source": "empirical_fallback", "n_events": 0,
            "ratio_empirical": row["ratio"], "matched_multiple_empirical": row["matched_multiple"],
        })

    factors_df = pd.DataFrame(out_rows)
    factors_df["symbol"] = factors_df["symbol"].astype(str).str.zfill(6)  # pd.read_csv가 앞자리 0을
    # 날린 정수로 잘못 추론하는 것 방지(예: '005800'->5800) — 종목코드는 항상 텍스트로 저장
    factors_df.to_csv(FACTORS_CSV, index=False, encoding="utf-8-sig")
    print(f"보정값 {len(factors_df)}건 저장: {FACTORS_CSV}")
    print(factors_df["source"].value_counts().to_string())
    conn.close()


# ── 보정된 PIT 배당수익률 계산 (dividend_yield_pit_validation.py와 나란히, 그 파일은 미변경) ──

def load_dividend_yield_pit_corrected(conn: sqlite3.Connection, prices: pd.DataFrame, factors_csv: Path = FACTORS_CSV):
    """load_dividend_yield_pit()(dividend_yield_pit_validation.py)와 완전히 동일한 로직이나,
    dps_pit을 dividend_split_correction_factors.csv의 (symbol, biz_year)별 correction_factor로
    보정한 뒤 종가로 나눔. 보정값 없는 (symbol, biz_year)는 factor=1.0(미보정, 기존과 동일)."""
    DPS_SANITY_CAP = 1_000_000

    dps_df = pd.read_sql_query(
        f"SELECT symbol, biz_year, dps FROM dividends WHERE dps IS NOT NULL AND dps < {DPS_SANITY_CAP}",
        conn,
    )
    factors = pd.read_csv(factors_csv)[["symbol", "biz_year", "correction_factor"]]
    # symbol 컬럼 dtype 통일(zero-padded 종목코드 보존을 위해 문자열로)
    factors["symbol"] = factors["symbol"].astype(str).str.zfill(6)
    dps_df["symbol"] = dps_df["symbol"].astype(str).str.zfill(6)

    dps_df = dps_df.merge(factors, on=["symbol", "biz_year"], how="left")
    dps_df["correction_factor"] = dps_df["correction_factor"].fillna(1.0)
    dps_df["dps_corrected"] = dps_df["dps"] * dps_df["correction_factor"]

    dps_df["known_from"] = (dps_df["biz_year"] + 1).astype(str) + "0401"

    dps_pit = pd.Series(np.nan, index=prices.index)
    dates = prices["date"].values
    symbols = prices["symbol"].values

    dps_sorted = dps_df.sort_values(["symbol", "known_from"])
    for sym, grp in dps_sorted.groupby("symbol"):
        mask = symbols == sym
        if not mask.any():
            continue
        idx = np.where(mask)[0]
        d_for_sym = dates[idx]
        known_from = grp["known_from"].values
        dps_vals = grp["dps_corrected"].values
        pos = np.searchsorted(known_from, d_for_sym, side="right") - 1
        valid = pos >= 0
        dps_pit.iloc[idx[valid]] = dps_vals[pos[valid]]

    prices = prices.copy()
    prices["_dps_pit_corrected"] = dps_pit
    prices["_div_yield_pit_corrected"] = (prices["_dps_pit_corrected"] / prices["close"] * 100).where(prices["close"] > 0)

    rank = prices.groupby("date")["_div_yield_pit_corrected"].rank(pct=True)
    cond_high_div_corrected = (rank >= 0.8) & prices["_div_yield_pit_corrected"].notna()
    return cond_high_div_corrected, prices["_div_yield_pit_corrected"]


if __name__ == "__main__":
    main()
