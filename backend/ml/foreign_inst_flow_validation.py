"""
[검증 전용 — 운영 코드/모델 미사용, DB는 SELECT만] "외국인/연기금 순매수가 장기 수익률을
예측하는가" 가설 검증 (2026-06-26).

⚠️ 데이터 제약 (검증 시작 전 확인된 사실 — 결과 해석 시 반드시 감안):
  1. pykrx 투자자별 매매동향 API(get_market_trading_value_by_date 등)는 KRX_ID/KRX_PW
     로그인을 요구하도록 막혀 있어(2026-06 기준 재확인) 신규 수집 불가 — 기존
     `flows` 테이블에 이미 있는 것만 사용(이번 검증도 SELECT 전용 규칙).
  2. `flows.inst_net`(기관 순매수)은 테이블 전체가 100% NULL — 스키마 주석에 "향후 확장"
     이라고 돼 있던 그대로, 실제로 채워진 적이 없음. **연기금/기관 쪽은 데이터가 전혀
     없어 이번 검증에서 제외** — 가설의 "연기금" 부분은 검증 불가능, 보류.
  3. `flows.foreign_net`은 스키마 주석상 "외국인 순매수"가 아니라 "**외국인 보유비율(%)**"
     (수준값, level) — 순매수 플로우가 아니라 보유 지분율 스냅샷. 일별 변화량(diff)을
     "그날 외국인이 사들였다(보유비율 상승)/팔았다(하락)"의 근사 프록시로 쓸 수밖에 없음
     (지분율 상승은 보통 순매수를 반영하지만, 유통주식수 변동 등에 의한 잡음도 섞일 수 있음).
  4. 실제 연속 수집 구간은 2026-05-07~2026-06-23 단 **6~7주**뿐(1년 공백 이후 — CLAUDE.md
     "flows/fundamentals 테이블 1년 공백" 참고)이고 그 안에서도 날짜가 듬성듬성(약 25일).
     → **multi-year IS(2022-24)/OOS(2025-26) 워크포워드 자체가 불가능**. 이 스크립트는
     기존 신호 검증과 "같은 틀"(diff-in-means, 시장국면별, Bonferroni, 모멘텀 상관)을
     쓰지만 표본기간이 6~7주뿐이라는 한계는 구조적으로 해결 불가 — **결론은 참고용,
     보류 권장** (factor_screen_validation.py의 Tier3 처리와 동일한 입장).

신호 정의 (외국인 보유비율 변화 기반):
  - cond_foreign_streak3: 3일 연속 보유비율 상승 (기존 factor_screen_validation.py의
    load_tier3() "외국인순매수(3일연속)"와 동일 로직 — 재사용)
  - cond_foreign_top20: 당일 횡단면 기준 보유비율 변화량(diff) 상위 20%

검증: 가용 전체 구간을 시점 순으로 반(半)분해 "전반/후반"으로 나눠 방향 일관성만 참고로
확인(절대 IS/OOS 동급 아님) + 전체 구간 단일 검정 + 시장국면별 + 모멘텀(20일 누적수익률)
과의 상관계수.

실행: python foreign_inst_flow_validation.py
"""

import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000040)
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sqlite3
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from factor_screen_validation import _test_group, ETF_PATTERN

pd.set_option("display.width", 160)

FLOW_START = "20260501"  # 1년 공백 이후 실제 연속 구간 시작 근방
N_TESTS = 4  # 조건 2개 x horizon 2개
BONFERRONI_ALPHA = 0.05 / N_TESTS


def load_prices_recent(conn: sqlite3.Connection, start: str) -> pd.DataFrame:
    """factor_screen_validation.load_prices()와 동일 로직이나, 그 파일의 하드코딩된
    DATA_END(20260619 — flows 최신일 20260623보다 이전)에 묶이지 않도록 현재 prices
    최신일까지 동적으로 로드."""
    names = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)
    etf_syms = set(names.loc[names["name"].fillna("").str.contains(ETF_PATTERN), "symbol"])
    end = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]

    df = pd.read_sql_query(
        "SELECT symbol, date, close, volume FROM prices WHERE date BETWEEN ? AND ? ORDER BY symbol, date",
        conn, params=(start, end),
    )
    df = df[~df["symbol"].isin(etf_syms)].reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["value"] = df["close"] * df["volume"]
    return df


def load_kospi_regime_recent(conn: sqlite3.Connection, start: str) -> pd.Series:
    """factor_screen_validation.load_kospi_regime()과 동일 로직, 동적 종료일 버전."""
    end = conn.execute("SELECT MAX(date) FROM market_index WHERE code='1001'").fetchone()[0]
    df = pd.read_sql_query(
        "SELECT date, close FROM market_index WHERE code='1001' AND date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end),
    )
    df["close"] = df["close"].astype(float)
    df["ret_20d"] = df["close"].pct_change(20)
    regime = np.where(df["ret_20d"] > 0.03, "강세", np.where(df["ret_20d"] < -0.03, "약세", "횡보"))
    return pd.Series(regime, index=df["date"])


def load_flows(conn: sqlite3.Connection, prices: pd.DataFrame) -> pd.DataFrame:
    flows = pd.read_sql_query(
        "SELECT symbol, date, foreign_net FROM flows WHERE foreign_net IS NOT NULL AND date >= ?",
        conn, params=(FLOW_START,),
    )
    merged = prices.merge(flows, on=["symbol", "date"], how="left")

    merged["foreign_chg"] = merged.groupby("symbol")["foreign_net"].diff()
    merged["date_dt"] = pd.to_datetime(merged["date"], format="%Y%m%d")
    gap = merged.groupby("symbol")["date_dt"].diff().dt.days
    merged.loc[gap > 5, "foreign_chg"] = np.nan  # 듬성듬성한 수집 간격 고려, 5일 넘으면 연속성 단절로 간주

    streak3 = (
        merged.groupby("symbol")["foreign_chg"].transform(lambda s: (s > 0).rolling(3).sum() == 3)
        & merged.groupby("symbol")["foreign_chg"].transform(lambda s: s.notna().rolling(3).sum() == 3)
    )
    merged["cond_foreign_streak3"] = streak3.fillna(False)

    rank = merged.groupby("date")["foreign_chg"].rank(pct=True)
    merged["cond_foreign_top20"] = (rank >= 0.8) & merged["foreign_chg"].notna()

    valid_window = merged["date"] >= FLOW_START
    merged["cond_foreign_streak3"] &= valid_window
    merged["cond_foreign_top20"] &= valid_window
    return merged


def diff_in_means(sat: pd.Series, unsat: pd.Series):
    sat_mean, sat_win, sat_p, sat_n = _test_group(sat)
    unsat_mean, unsat_win, unsat_p, unsat_n = _test_group(unsat)
    diff_p = None
    if sat_n >= 10 and unsat_n >= 10:
        _, diff_p = stats.ttest_ind(sat.dropna(), unsat.dropna(), equal_var=False)
    return {
        "n_sat": sat_n, "mean_sat": sat_mean, "win_sat": sat_win,
        "n_unsat": unsat_n, "mean_unsat": unsat_mean, "win_unsat": unsat_win,
        "diff_p": diff_p,
    }


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/6] 데이터 로드 (가격 + 외국인 보유비율, 2026-04~)"); print("=" * 70)
    prices = load_prices_recent(conn, "20260401")  # diff 계산용 약간의 사전여유만 남기고 시작

    g = prices.groupby("symbol", group_keys=False)
    prices["ret_fwd_5d"] = g["close"].transform(lambda s: s.shift(-5) / s - 1)
    prices["ret_fwd_20d"] = g["close"].transform(lambda s: s.shift(-20) / s - 1)
    prices["mom_20d"] = g["close"].transform(lambda s: s.pct_change(20))
    prices["value_ma20"] = g["value"].transform(lambda s: s.rolling(20).mean())

    merged = load_flows(conn, prices)
    kospi_regime = load_kospi_regime_recent(conn, "20260201")
    conn.close()

    dates_used = sorted(merged.loc[merged["foreign_chg"].notna(), "date"].unique())
    print(f"외국인 보유비율 변화 계산 가능 날짜: {len(dates_used)}개 ({dates_used[0] if dates_used else None} ~ {dates_used[-1] if dates_used else None})")
    print(f"보유비율 데이터 보유 종목: {merged.loc[merged['foreign_net'].notna(),'symbol'].nunique()}개")

    print("\n" + "=" * 70); print("[2/6] 신호 발생 건수"); print("=" * 70)
    for label, col in [("3일연속 보유비율상승", "cond_foreign_streak3"), ("당일 상위20%(보유비율 변화)", "cond_foreign_top20")]:
        for h in ["ret_fwd_5d", "ret_fwd_20d"]:
            n = (merged[col] & merged[h].notna()).sum()
            print(f"  {label} / {h}: 유효표본 n={n}")

    print("\n" + "=" * 70); print("[3/6] 전체 구간 단일 검정 (diff-in-means, 시계열 분리 불가 — 참고용)"); print("=" * 70)
    rows = []
    for label, col in [("외국인 3일연속 보유비율상승", "cond_foreign_streak3"), ("외국인 당일 상위20%", "cond_foreign_top20")]:
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            liquid = merged["value_ma20"] >= 1_000_000_000
            base = merged[liquid]
            sat = base[base[col]][horizon]
            unsat = base[~base[col]][horizon]
            r = diff_in_means(sat, unsat)
            sig = r["diff_p"] is not None and r["diff_p"] < BONFERRONI_ALPHA
            rows.append({
                "조건": label, "기간": hname,
                "만족군_n": r["n_sat"], "만족군_평균%": round(r["mean_sat"], 3) if r["mean_sat"] is not None else None,
                "만족군_승률": round(r["win_sat"], 3) if r["win_sat"] is not None else None,
                "불만족군_평균%": round(r["mean_unsat"], 3) if r["mean_unsat"] is not None else None,
                "diff_p": round(r["diff_p"], 4) if r["diff_p"] is not None else None,
                "Bonferroni유의(0.0125)": sig,
            })
    result_df = pd.DataFrame(rows)
    print(result_df.to_string(index=False))

    out = Path(__file__).parent / "foreign_inst_flow_validation_results.csv"
    result_df.to_csv(out, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 70); print("[4/6] 전반/후반 방향 일관성 참고 체크 (절대 IS/OOS 동급 아님)"); print("=" * 70)
    mid_date = dates_used[len(dates_used) // 2] if dates_used else None
    half_rows = []
    if mid_date:
        for label, col in [("외국인 3일연속 보유비율상승", "cond_foreign_streak3"), ("외국인 당일 상위20%", "cond_foreign_top20")]:
            for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
                liquid = merged["value_ma20"] >= 1_000_000_000
                for half_label, half_mask in [("전반", merged["date"] < mid_date), ("후반", merged["date"] >= mid_date)]:
                    base = merged[liquid & half_mask]
                    sat = base[base[col]][horizon]
                    mean, win, p, n = _test_group(sat)
                    half_rows.append({"조건": label, "기간": hname, "구간": half_label, "n": n,
                                       "평균%": round(mean, 3) if mean is not None else None,
                                       "승률": round(win, 3) if win is not None else None})
        half_df = pd.DataFrame(half_rows)
        print(half_df.to_string(index=False))
        half_df.to_csv(Path(__file__).parent / "foreign_inst_flow_validation_halfsplit.csv", index=False, encoding="utf-8-sig")
    else:
        print("유효 날짜 없음 — 전반/후반 분리 불가")

    print("\n" + "=" * 70); print("[5/6] 시장국면별 (5일 수익률)"); print("=" * 70)
    merged["regime"] = merged["date"].map(kospi_regime)
    print("이 구간에 존재하는 국면:", sorted(merged["regime"].dropna().unique().tolist()))
    regime_rows = []
    liquid = merged["value_ma20"] >= 1_000_000_000
    for label, col in [("외국인 3일연속 보유비율상승", "cond_foreign_streak3"), ("외국인 당일 상위20%", "cond_foreign_top20")]:
        for regime in ["강세", "횡보", "약세"]:
            sub = merged[liquid & (merged["regime"] == regime)]
            sat = sub[sub[col]]["ret_fwd_5d"]
            mean, win, p, n = _test_group(sat)
            regime_rows.append({"조건": label, "국면": regime, "n": n,
                                 "평균%": round(mean, 3) if mean is not None else None,
                                 "승률": round(win, 3) if win is not None else None, "p": p})
            print(f"  [{label}] {regime}: n={n}, 평균={mean if mean is not None else float('nan'):.2f}%, "
                  f"승률={win if win is not None else float('nan'):.1%}, p={p}")
    pd.DataFrame(regime_rows).to_csv(Path(__file__).parent / "foreign_inst_flow_validation_regime.csv",
                                      index=False, encoding="utf-8-sig")

    print("\n" + "=" * 70); print("[6/6] 모멘텀(20일 누적수익률)과의 상관관계 — 재포장 여부 확인"); print("=" * 70)
    valid = merged["foreign_chg"].notna() & merged["mom_20d"].notna()
    corr_chg = merged.loc[valid, ["foreign_chg", "mom_20d"]].corr().iloc[0, 1]
    print(f"  corr(외국인 보유비율 일간변화량, 20일 모멘텀) = {corr_chg:.3f}")
    rank_valid = merged["foreign_chg"].notna() & merged["mom_20d"].notna()
    chg_rank = merged.loc[rank_valid].groupby("date")["foreign_chg"].rank(pct=True)
    mom_rank = merged.loc[rank_valid].groupby("date")["mom_20d"].rank(pct=True)
    corr_rank = np.corrcoef(chg_rank, mom_rank)[0, 1]
    print(f"  corr(횡단면 랭크 기준) = {corr_rank:.3f}")

    print(f"\n총 소요시간: {time.time()-t0:.0f}s")
    print(f"결과 저장: {out}")
    print("\n⚠️ 결론: 표본기간이 6~7주(가용 날짜 ~25개)뿐이라 multi-year IS/OOS 워크포워드 불가 —")
    print("   위 수치는 참고용. 연기금(inst_net)은 DB에 데이터 자체가 없어 검증 불가(보류).")


if __name__ == "__main__":
    main()
