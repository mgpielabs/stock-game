"""
KSD 법인액션 공식 데이터로 dividend_split_correction_factors.csv 검증/갱신

KIS API ksdinfo_* TR 3개 조회:
  - ksdinfo_bonus_issue  (HHKDB669101C0): 무상증자  K = 1 + fix_rate/100
  - ksdinfo_rev_split    (HHKDB669105C0): 액면분할/병합  K = bf_face/af_face
  - ksdinfo_cap_dcrs     (HHKDB669106C0): 감자  K = reduce_cap_rate/100

사용법:
  uv run python backend/ml/ksd_corporate_actions.py --fetch        # KSD 조회 + 저장
  uv run python backend/ml/ksd_corporate_actions.py --compare      # CSV vs KSD 비교
  uv run python backend/ml/ksd_corporate_actions.py --apply        # KSD 검증값으로 CSV 갱신
  uv run python backend/ml/ksd_corporate_actions.py --dry-run      # 변경 예정 미리보기
"""
import sys, os, json, time, math, csv, shutil
import argparse
import requests
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

ROOT    = Path(__file__).parent.parent
ENV     = ROOT / ".env"
TOKEN_CACHE = ROOT / "data" / ".kis_token_cache.json"
CSV_PATH    = Path(__file__).parent / "dividend_split_correction_factors.csv"
IGNORE_JSON = Path(__file__).parent / ".dividend_split_ignore.json"
KSD_CACHE   = Path(__file__).parent / "ksd_corporate_actions_cache.json"

BASE_URL = "https://openapi.koreainvestment.com:9443"
FETCH_START = "20200101"
FETCH_END   = datetime.today().strftime("%Y%m%d")

# ── 깨끗한 배수 스냅 (K 오류 보정용) ──────────────────────────
CLEAN_MULTIPLES = [
    1.5, 2, 2.5, 3, 3.33, 4, 5, 6, 6.67, 7, 7.5, 8, 10, 12, 12.5, 15, 16, 20, 25, 30, 40, 50, 100
]


# ── 환경/토큰 ───────────────────────────────────────────────────
def load_env():
    env = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def load_token(env):
    """토큰 발급. 분당 발급 제한이 있어 파일에 캐시해서 만료 전까지 재사용.
    kis_investor_collector.py와 동일한 방식 — expires_at으로 만료 체크."""
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            if cached.get("expires_at", 0) > time.time() + 300:
                return cached["access_token"]
        except Exception:
            pass
    # 토큰 발급
    r = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": env["KIS_APP_KEY"],
            "appsecret": env["KIS_APP_SECRET"],
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    tok = data["access_token"]
    expires_in = int(data.get("expires_in", 86400))
    TOKEN_CACHE.write_text(
        json.dumps({"access_token": tok, "expires_at": time.time() + expires_in}),
        encoding="utf-8",
    )
    return tok


def make_headers(env, token, tr_id):
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "authorization": f"Bearer {token}",
        "appkey": env["KIS_APP_KEY"],
        "appsecret": env["KIS_APP_SECRET"],
        "tr_id": tr_id,
    }


# ── KSD TR 조회 ─────────────────────────────────────────────────
def fetch_bonus_issue(env, token, f_dt, t_dt):
    """무상증자 (ksdinfo_bonus_issue, HHKDB669101C0)"""
    rows = []
    cts = ""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/ksdinfo/bonus-issue"
    while True:
        params = {
            "SHT_CD": "",          # 공백=전종목
            "CTS": cts,
            "F_DT": f_dt,
            "T_DT": t_dt,
        }
        r = requests.get(url, params=params,
                         headers=make_headers(env, token, "HHKDB669101C0"),
                         timeout=15)
        body = r.json()
        if body.get("rt_cd") != "0":
            print(f"  bonus_issue 오류: {body.get('msg1','?')[:80]}")
            break
        output = body.get("output1") or body.get("output", [])
        for rec in output:
            fix_rate = float(rec.get("fix_rate", 0) or 0)
            if fix_rate <= 0:
                continue
            rows.append({
                "symbol":      rec.get("sht_cd", "").strip().zfill(6),
                "action_type": "bonus_issue",
                "record_date": rec.get("record_date", "").replace("-", ""),
                "list_date":   rec.get("list_date", "").replace("-", ""),
                "K_raw":       1 + fix_rate / 100,
                "detail":      f"fix_rate={fix_rate}",
            })
        cts = body.get("ctx_area_cts", "")
        if not cts or not cts.strip():
            break
        time.sleep(0.2)
    return rows


def fetch_rev_split(env, token, f_dt, t_dt):
    """액면교체 — 주식분할/병합 (ksdinfo_rev_split, HHKDB669105C0)"""
    rows = []
    cts = ""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/ksdinfo/rev-split"
    while True:
        params = {
            "SHT_CD":    "",
            "CTS":       cts,
            "F_DT":      f_dt,
            "T_DT":      t_dt,
            "MARKET_GB": "0",    # 0=전체
        }
        r = requests.get(url, params=params,
                         headers=make_headers(env, token, "HHKDB669105C0"),
                         timeout=15)
        body = r.json()
        if body.get("rt_cd") != "0":
            print(f"  rev_split 오류: {body.get('msg1','?')[:80]}")
            break
        output = body.get("output1") or body.get("output", [])
        for rec in output:
            bf = float(rec.get("inter_bf_face_amt", 0) or 0)
            af = float(rec.get("inter_af_face_amt", 0) or 0)
            if bf <= 0 or af <= 0:
                continue
            K = bf / af  # 분할: bf>af → K>1; 병합: bf<af → K<1
            rows.append({
                "symbol":      rec.get("sht_cd", "").strip().zfill(6),
                "action_type": "rev_split",
                "record_date": rec.get("record_date", "").replace("-", ""),
                "list_date":   rec.get("list_dt", "").replace("-", ""),
                "K_raw":       K,
                "detail":      f"bf={bf:.0f}→af={af:.0f}",
            })
        cts = body.get("ctx_area_cts", "")
        if not cts or not cts.strip():
            break
        time.sleep(0.2)
    return rows


def fetch_cap_dcrs(env, token, f_dt, t_dt):
    """감자 (ksdinfo_cap_dcrs, HHKDB669106C0)"""
    rows = []
    cts = ""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/ksdinfo/cap-dcrs"
    while True:
        params = {
            "SHT_CD": "",
            "CTS":    cts,
            "F_DT":   f_dt,
            "T_DT":   t_dt,
        }
        r = requests.get(url, params=params,
                         headers=make_headers(env, token, "HHKDB669106C0"),
                         timeout=15)
        body = r.json()
        if body.get("rt_cd") != "0":
            print(f"  cap_dcrs 오류: {body.get('msg1','?')[:80]}")
            break
        output = body.get("output1") or body.get("output", [])
        for rec in output:
            rate = float(rec.get("reduce_cap_rate", 0) or 0)
            dcrs_type = rec.get("reduce_cap_type", "")
            if rate <= 0:
                continue
            # reduce_cap_rate: 감자율(감소 비율). 잔존 비율 = 1 - rate/100
            # 예: rate=50 → 50% 감자 → K=0.5 (주식 절반 소각)
            # 예: rate=0.08 → 0.08% 감자 → K=0.9992 (사실상 무시 가능)
            K = 1.0 - rate / 100.0
            if K <= 0:
                continue  # 100% 감자는 상장폐지 → 보정 대상 아님
            rows.append({
                "symbol":      rec.get("sht_cd", "").strip().zfill(6),
                "action_type": "cap_dcrs",
                "record_date": rec.get("record_date", "").replace("-", ""),
                "list_date":   rec.get("list_dt", "").replace("-", ""),
                "K_raw":       K,
                "detail":      f"rate={rate}({dcrs_type})",
            })
        cts = body.get("ctx_area_cts", "")
        if not cts or not cts.strip():
            break
        time.sleep(0.2)
    return rows


# ── event_date → biz_year 매핑 ──────────────────────────────────
def event_date_to_biz_years(event_date: str, dividends_df: pd.DataFrame, symbol: str):
    """
    법인액션 event_date 이후 최초 DPS 공시 biz_year를 반환.
    분할 이전 DPS는 보정 필요, 이후 DPS는 보정 불필요.
    → 영향받는 biz_year 목록 반환 (event 이전 배당이 pykrx 수정가와 스케일 불일치)
    """
    sym_div = dividends_df[dividends_df["symbol"] == symbol].copy()
    if sym_div.empty:
        return []
    # 공시는 보통 결산연도 다음해 3~4월
    # known_from = biz_year + 1 + "0401" → biz_year의 DPS가 언제 공시되나
    # event_date 기준: event 이후의 시가(pykrx)는 이미 분할 반영
    # event 이전 DPS가 아직 비보정 → event 이전 배당 biz_year가 보정 대상
    event_yyyymm = int(event_date[:6]) if len(event_date) >= 6 else 99999999

    affected = []
    for _, row in sym_div.iterrows():
        biz_y = int(row["biz_year"])
        # known_from ≈ (biz_y+1)-04-01 (사업보고서 제출 기준)
        known_yyyymm = (biz_y + 1) * 100 + 4
        if known_yyyymm < event_yyyymm:
            affected.append(biz_y)
    return affected


# ── 메인 로직 ────────────────────────────────────────────────────
def snap_to_clean(K):
    """K를 가장 가까운 CLEAN_MULTIPLES로 스냅, 10% 초과 시 원래 값 반환"""
    best, best_err = K, float("inf")
    for m in CLEAN_MULTIPLES:
        err = abs(K / m - 1)
        if err < best_err:
            best_err, best = err, m
    return best if best_err < 0.10 else K


def fetch_all(env, token):
    print(f"KSD 조회 범위: {FETCH_START} ~ {FETCH_END}")
    all_rows = []

    print("  1/3 ksdinfo_bonus_issue 조회...")
    rows = fetch_bonus_issue(env, token, FETCH_START, FETCH_END)
    print(f"     {len(rows)}건")
    all_rows.extend(rows)

    print("  2/3 ksdinfo_rev_split 조회...")
    rows = fetch_rev_split(env, token, FETCH_START, FETCH_END)
    print(f"     {len(rows)}건")
    all_rows.extend(rows)

    print("  3/3 ksdinfo_cap_dcrs 조회...")
    rows = fetch_cap_dcrs(env, token, FETCH_START, FETCH_END)
    print(f"     {len(rows)}건")
    all_rows.extend(rows)

    KSD_CACHE.write_text(json.dumps(all_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  캐시 저장: {KSD_CACHE} ({len(all_rows)}건)")
    return all_rows


def compare(dry_run=False):
    if not KSD_CACHE.exists():
        print("KSD 캐시 없음. --fetch 먼저 실행.")
        return

    ksd_rows = json.loads(KSD_CACHE.read_text(encoding="utf-8"))
    df_ksd = pd.DataFrame(ksd_rows)
    df_ksd["symbol"] = df_ksd["symbol"].astype(str).str.zfill(6)

    df_csv = pd.read_csv(CSV_PATH, dtype={"symbol": str})
    df_csv["symbol"] = df_csv["symbol"].astype(str).str.zfill(6)

    ignore = {}
    if IGNORE_JSON.exists():
        ignore = json.loads(IGNORE_JSON.read_text(encoding="utf-8"))

    # dividends 로드 (event_date → biz_year 매핑)
    import sqlite3
    DB = ROOT / "data" / "stocks.db"
    with sqlite3.connect(DB) as conn:
        divs = pd.read_sql_query(
            "SELECT symbol, biz_year, dps FROM dividends WHERE dps IS NOT NULL",
            conn)
    divs["symbol"] = divs["symbol"].astype(str).str.zfill(6)

    print("=" * 70)
    print("[KSD vs CSV 비교]")
    print(f"  KSD 이벤트: {len(df_ksd)}건 / CSV 현행: {len(df_csv)}건")
    print()

    new_cf_list = []  # (symbol, biz_year, old_cf, new_cf, reason)
    unmatched_ksd = []  # CSV에 없는 KSD 이벤트
    mismatch_list = []  # K 값이 10%+ 차이

    for _, krow in df_ksd.iterrows():
        sym = krow["symbol"]
        ev_date = str(krow.get("record_date", krow.get("list_date", "")))

        # ignore 목록 제외
        if sym in ignore:
            continue

        K_ksd = float(krow["K_raw"])
        cf_ksd = round(1.0 / K_ksd, 6)

        # 영향받는 biz_year 목록
        affected_years = event_date_to_biz_years(ev_date, divs, sym)
        if not affected_years:
            continue

        for by in affected_years:
            # CSV에 해당 (symbol, biz_year) 있나?
            match = df_csv[(df_csv["symbol"] == sym) & (df_csv["biz_year"] == by)]
            if match.empty:
                unmatched_ksd.append({
                    "symbol": sym, "biz_year": by, "event": krow["action_type"],
                    "K_ksd": K_ksd, "cf_ksd": cf_ksd, "ev_date": ev_date,
                })
                continue

            cf_csv = float(match.iloc[0]["correction_factor"])
            K_csv  = 1.0 / cf_csv
            rel_diff = abs(K_ksd / K_csv - 1)
            if rel_diff > 0.10:
                mismatch_list.append({
                    "symbol": sym, "biz_year": by,
                    "K_csv": round(K_csv, 4), "K_ksd": round(K_ksd, 4),
                    "cf_csv": round(cf_csv, 6), "cf_ksd": round(cf_ksd, 6),
                    "rel_diff_pct": round(rel_diff * 100, 1),
                    "action": krow["action_type"], "detail": krow["detail"],
                })

    print(f"[KSD에는 있는데 CSV에 없음 — 누락 후보]: {len(unmatched_ksd)}건")
    for r in unmatched_ksd[:20]:
        print(f"  {r['symbol']} biz_year={r['biz_year']} {r['event']} "
              f"K={r['K_ksd']:.4f} cf={r['cf_ksd']:.4f} (ev={r['ev_date']})")
    if len(unmatched_ksd) > 20:
        print(f"  ... 외 {len(unmatched_ksd)-20}건")

    print()
    print(f"[K값 10%+ 불일치 — 교체 후보]: {len(mismatch_list)}건")
    for r in sorted(mismatch_list, key=lambda x: -x["rel_diff_pct"])[:30]:
        print(f"  {r['symbol']} biz={r['biz_year']} {r['action']}: "
              f"CSV K={r['K_csv']:.4f}→KSD K={r['K_ksd']:.4f} "
              f"({r['rel_diff_pct']:.1f}%) [{r['detail']}]")
    if len(mismatch_list) > 30:
        print(f"  ... 외 {len(mismatch_list)-30}건")

    return unmatched_ksd, mismatch_list


def apply_ksd(dry_run=False):
    """KSD 검증값으로 CSV 갱신 (mismatch만 교체, unmatched는 추가)"""
    result = compare(dry_run=True)
    if result is None:
        return
    unmatched_ksd, mismatch_list = result

    df_csv = pd.read_csv(CSV_PATH, dtype={"symbol": str})
    df_csv["symbol"] = df_csv["symbol"].astype(str).str.zfill(6)

    ignore = {}
    if IGNORE_JSON.exists():
        ignore = json.loads(IGNORE_JSON.read_text(encoding="utf-8"))

    changes = 0

    # 불일치 교체
    for r in mismatch_list:
        if r["symbol"] in ignore:
            continue
        # cap_dcrs는 감자율이 작아(< 2%) 분할 보정값을 덮어쓰는 사고 방지
        # K_ksd ≈ 1.0이면 실질 보정 없음 — 기존 값 그대로 유지
        if r["action"] == "cap_dcrs" and abs(r["K_ksd"] - 1.0) < 0.05:
            continue
        mask = (df_csv["symbol"] == r["symbol"]) & (df_csv["biz_year"] == r["biz_year"])
        if mask.sum() == 0:
            continue
        old_cf = df_csv.loc[mask, "correction_factor"].iloc[0]
        new_cf = round(r["cf_ksd"], 6)
        print(f"  교체: {r['symbol']} biz={r['biz_year']} cf {old_cf:.6f}→{new_cf:.6f} "
              f"({r['action']} {r['detail']})")
        if not dry_run:
            df_csv.loc[mask, "correction_factor"] = new_cf
            df_csv.loc[mask, "K_cumulative"]      = round(r["K_ksd"], 6)
            df_csv.loc[mask, "source"]            = "ksd_verified"
        changes += 1

    # 누락 추가 (unmatched_ksd)
    new_rows = []
    for r in unmatched_ksd:
        if r["symbol"] in ignore:
            continue
        # cap_dcrs K≈1.0은 추가 안 함 (분할 보정 불필요, 다른 이벤트와 충돌 방지)
        if r["event"] == "cap_dcrs" and abs(r["K_ksd"] - 1.0) < 0.05:
            continue
        # 이미 CSV에 있는지 재확인
        mask = (df_csv["symbol"] == r["symbol"]) & (df_csv["biz_year"] == r["biz_year"])
        if mask.sum() > 0:
            continue
        K = snap_to_clean(r["K_ksd"])
        new_rows.append({
            "symbol":            r["symbol"],
            "biz_year":          r["biz_year"],
            "correction_factor": round(1.0 / K, 6),
            "K_cumulative":      round(K, 6),
            "source":            "ksd_new",
            "notes":             r["event"],
        })
        print(f"  추가: {r['symbol']} biz={r['biz_year']} cf={round(1.0/K,6):.6f} ({r['event']})")
        changes += 1

    print(f"\n총 {changes}건 변경 예정")

    if dry_run:
        print("(dry-run — 실제 반영 없음)")
        return

    if new_rows:
        df_new = pd.DataFrame(new_rows)
        df_csv = pd.concat([df_csv, df_new], ignore_index=True)

    # 백업 후 저장
    backup = CSV_PATH.parent / f"{CSV_PATH.stem}_PRE_KSD_{datetime.today().strftime('%Y%m%d_%H%M%S')}.csv"
    shutil.copy(CSV_PATH, backup)
    df_csv = df_csv.sort_values(["symbol", "biz_year"]).reset_index(drop=True)
    df_csv.to_csv(CSV_PATH, index=False)
    print(f"저장 완료: {CSV_PATH}  (백업: {backup})")


# ── CLI ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch",   action="store_true", help="KSD TR 조회 + 캐시 저장")
    parser.add_argument("--compare", action="store_true", help="캐시 vs CSV 비교만")
    parser.add_argument("--apply",   action="store_true", help="캐시 기반 CSV 갱신")
    parser.add_argument("--dry-run", action="store_true", help="변경 예정만 출력")
    args = parser.parse_args()

    if args.fetch or args.apply:
        env   = load_env()
        token = load_token(env)
        if args.fetch:
            fetch_all(env, token)

    if args.apply:
        apply_ksd(dry_run=args.dry_run)
    elif args.compare or args.dry_run:
        compare(dry_run=args.dry_run)
    elif not args.fetch:
        parser.print_help()


if __name__ == "__main__":
    main()
