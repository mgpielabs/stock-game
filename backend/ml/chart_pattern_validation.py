"""
[검증 전용 — 운영 코드/모델 미사용, DB는 SELECT만] 차트 패턴 매칭 가설 검증

가설: "과거에 N일 후 많이 오른 종목들의 오르기 직전 차트 패턴"과 비슷한 패턴을
지금 보이는 종목은 앞으로 오를 가능성이 높다.

방법:
  1. in-sample(2022~2023)에서만 "상승 전조 패턴" 템플릿을 1회 구축하고 고정.
     각 템플릿 날짜에서 향후 20일 수익률 상위 5% 종목의 직전 W일(기본 40일) 정규화
     가격 패턴(window/window[0])을 템플릿으로 수집.
  2. 모든 테스트 시점(IS+OOS)의 모든(유동성 통과) 종목에 대해, 직전 W일 정규화 패턴과
     템플릿들의 상관계수 상위 10개 평균을 "패턴 유사도 점수"로 계산.
  3. 시점별 횡단면에서 유사도 상위10% vs 하위10% vs 전체, forward 5일/20일 수익률로 검증
     (만족군 vs 불만족군 diff-in-means — "vs 0" 비교는 강세장 드리프트로 오염되는 함정이
     이미 variant_screen_validation.py에서 확인됐음, 같은 기준 사용).
  4. IS/OOS 분리 + 시장국면(강세/횡보/약세, KOSPI 20일수익률 기준)별 분리.
  5. 다중비교 Bonferroni 보정.
  6. 패턴 유사도 점수와 단순 모멘텀(같은 윈도우 누적수익률)의 상관관계를 확인 —
     모멘텀/추세추종은 이미 ❌로 판명났으므로, 패턴매칭이 그걸 재포장한 것에
     불과한지 점검(정직성 체크).

과적합 방지:
  - 템플릿은 IS에서 1회만 구축, OOS에서 재정의하지 않음(고정 템플릿을 그대로 적용)
  - 템플릿 날짜와 테스트 날짜가 겹치지 않게 분리(자기 패턴과의 trivial 1.0 매칭 방지)
  - 템플릿 구성 시 종목 자기참조 제외(테스트 종목 자신이 만든 템플릿은 그 종목 검증에서 제외)

실행: python chart_pattern_validation.py
"""

import argparse
import sqlite3
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

import re
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from evaluate import MIN_VOLUME_KRW

pd.set_option("display.width", 160)

DATA_START = "20210901"
DATA_END = "20260131"   # OOS 2025-12-31 + 20일 forward 여유
IS_YEARS = [2022, 2023]
OOS_YEARS = [2024, 2025]

WINDOW = 40          # 패턴 길이(거래일) — --window로 덮어쓰기 가능(민감도 점검용)
TOP_PCT_WINNER = 0.05  # 템플릿 후보: 향후 20일 수익률 상위 5%
TEMPLATE_LABEL_HORIZON = 20
TEMPLATE_STRIDE = 10   # 템플릿 구축용 날짜 샘플링 간격
TEST_STRIDE = 5        # 테스트용 날짜 샘플링 간격
TOP_K_SIM = 10          # 유사도 점수 = 상위 K개 템플릿 상관계수 평균
MIN_LIQUID_UNIVERSE = 200

N_TESTS = 14  # 대략적 비교 횟수(아래 결과 섹션 참고) — Bonferroni 보정용
BONFERRONI_ALPHA = 0.05 / N_TESTS

ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)


def load_data(conn):
    names = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)
    etf_syms = set(names.loc[names["name"].fillna("").str.contains(ETF_PATTERN), "symbol"])

    df = pd.read_sql_query(
        "SELECT symbol, date, close, volume FROM prices WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(DATA_START, DATA_END),
    )
    df = df[~df["symbol"].isin(etf_syms)].reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    counts = df.groupby("symbol").size()
    keep = counts[counts >= 280].index
    return df[df["symbol"].isin(keep)].reset_index(drop=True)


def load_kospi_regime(conn):
    df = pd.read_sql_query(
        "SELECT date, close FROM market_index WHERE code='1001' AND date BETWEEN ? AND ? ORDER BY date",
        conn, params=(DATA_START, DATA_END),
    )
    df["close"] = df["close"].astype(float)
    df["ret_20d"] = df["close"].pct_change(20)
    regime = np.where(df["ret_20d"] > 0.03, "강세", np.where(df["ret_20d"] < -0.03, "약세", "횡보"))
    return pd.Series(regime, index=df["date"])


def _test_group(returns: pd.Series):
    r = returns.dropna()
    n = len(r)
    if n < 10:
        return None, None, None, n
    mean = float(r.mean() * 100)
    win = float((r > 0).mean())
    _, p = stats.ttest_1samp(r, 0)
    return mean, win, float(p), n


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
    global WINDOW
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=WINDOW, help="패턴 길이(거래일), 민감도 점검용")
    args = parser.parse_args()
    WINDOW = args.window

    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/6] 데이터 로드"); print("=" * 70)
    df = load_data(conn)
    kospi_regime = load_kospi_regime(conn)
    print(f"유니버스: {df['symbol'].nunique()}종목, {len(df)}행")

    close = df.pivot(index="date", columns="symbol", values="close")
    volume = df.pivot(index="date", columns="symbol", values="volume")
    value_ma20 = (close * volume).rolling(20, min_periods=20).mean()
    liquid = value_ma20 >= MIN_VOLUME_KRW

    dates = close.index.to_numpy()
    n_dates = len(dates)
    date_pos = {d: i for i, d in enumerate(dates)}

    fwd5 = close.shift(-5) / close - 1
    fwd20 = close.shift(-20) / close - 1
    print(f"전체 거래일: {n_dates} ({dates[0]} ~ {dates[-1]})")
    conn.close()

    # ── 템플릿/테스트 날짜 샘플링 ──────────────────────────────
    is_mask = pd.Series([d[:4] for d in dates]).astype(int).isin(IS_YEARS).to_numpy()
    oos_mask = pd.Series([d[:4] for d in dates]).astype(int).isin(OOS_YEARS).to_numpy()
    is_idx = np.where(is_mask)[0]
    oos_idx = np.where(oos_mask)[0]

    template_idx = is_idx[WINDOW::TEMPLATE_STRIDE]
    template_idx = template_idx[template_idx < n_dates - TEMPLATE_LABEL_HORIZON]
    template_date_set = set(dates[template_idx])

    test_idx_is = is_idx[WINDOW + 2::TEST_STRIDE]
    test_idx_is = test_idx_is[(test_idx_is < n_dates - 20) & ~np.isin(dates[test_idx_is], list(template_date_set))]
    test_idx_oos = oos_idx[WINDOW::TEST_STRIDE]
    test_idx_oos = test_idx_oos[test_idx_oos < n_dates - 20]

    print(f"템플릿 날짜: {len(template_idx)}개, 테스트 날짜(IS): {len(test_idx_is)}개, 테스트 날짜(OOS): {len(test_idx_oos)}개")

    # ── 종목별 슬라이딩 윈도우로 필요한 시점의 정규화 패턴 수집 ──────
    print("\n" + "=" * 70); print("[2/6] 정규화 패턴 추출 (종목별 슬라이딩 윈도우)"); print("=" * 70)
    needed_idx = np.unique(np.concatenate([template_idx, test_idx_is, test_idx_oos]))

    symbols = close.columns.to_numpy()
    close_vals = close.to_numpy(dtype=np.float64)

    pat_symbol, pat_date_idx, pat_vec = [], [], []
    for j, sym in enumerate(symbols):
        c = close_vals[:, j]
        if np.sum(~np.isnan(c)) < WINDOW + 20:
            continue
        sw = sliding_window_view(c, WINDOW)  # (n_dates-W+1, W)
        row_idx = needed_idx - (WINDOW - 1)
        valid_row = row_idx >= 0
        rows = sw[row_idx[valid_row]]
        first = rows[:, 0]
        ok = (~np.isnan(rows).any(axis=1)) & (first > 0)
        if ok.sum() == 0:
            continue
        norm = rows[ok] / first[ok, None]
        these_idx = needed_idx[valid_row][ok]
        pat_symbol.append(np.full(ok.sum(), sym))
        pat_date_idx.append(these_idx)
        pat_vec.append(norm.astype(np.float32))

    pat_symbol = np.concatenate(pat_symbol)
    pat_date_idx = np.concatenate(pat_date_idx)
    pat_vec = np.concatenate(pat_vec, axis=0)
    print(f"패턴 행 수: {len(pat_symbol)} (종목x시점)")

    pat_df = pd.DataFrame({"symbol": pat_symbol, "date_idx": pat_date_idx, "row": np.arange(len(pat_symbol))})

    # ── 템플릿(상승 전조 패턴) 구축 — IS 전용, 1회 고정 ──────────────
    print("\n" + "=" * 70); print("[3/6] 상승 전조 패턴 템플릿 구축 (IS 전용, 고정)"); print("=" * 70)
    template_rows = []
    for ti in template_idx:
        d = dates[ti]
        liq_today = liquid.loc[d]
        fwd_today = fwd20.loc[d]
        cand = liq_today[liq_today].index
        cand = cand.intersection(fwd_today.dropna().index)
        if len(cand) < MIN_LIQUID_UNIVERSE:
            continue
        ranked = fwd_today.loc[cand].rank(pct=True)
        winners = ranked[ranked >= 1 - TOP_PCT_WINNER].index
        sub = pat_df[(pat_df["date_idx"] == ti) & (pat_df["symbol"].isin(winners))]
        template_rows.append(sub)

    template_df = pd.concat(template_rows, ignore_index=True)
    template_mat = pat_vec[template_df["row"].to_numpy()]
    template_symbols = template_df["symbol"].to_numpy()
    print(f"템플릿 개수: {len(template_mat)} (날짜 {len(template_idx)}개 x 날짜당 상위{TOP_PCT_WINNER:.0%})")

    # z-score (행별) — 상관계수 벡터화용
    t_mean = template_mat.mean(axis=1, keepdims=True)
    t_std = template_mat.std(axis=1, keepdims=True)
    t_std[t_std == 0] = np.nan
    Zt = (template_mat - t_mean) / t_std
    valid_t = ~np.isnan(Zt).any(axis=1)
    Zt = Zt[valid_t]
    template_symbols = template_symbols[valid_t]
    print(f"유효 템플릿(분산>0): {len(Zt)}개")

    # ── 테스트 후보 패턴 유사도 점수 계산 ──────────────────────────
    print("\n" + "=" * 70); print("[4/6] 테스트 시점별 패턴 유사도 점수 계산"); print("=" * 70)

    def score_test_dates(test_idx, label):
        recs = []
        for ti in test_idx:
            d = dates[ti]
            liq_today = liquid.loc[d]
            cand_syms = liq_today[liq_today].index
            sub = pat_df[(pat_df["date_idx"] == ti) & (pat_df["symbol"].isin(cand_syms))]
            if len(sub) < MIN_LIQUID_UNIVERSE:
                continue
            X = pat_vec[sub["row"].to_numpy()]
            x_mean = X.mean(axis=1, keepdims=True)
            x_std = X.std(axis=1, keepdims=True)
            x_std[x_std == 0] = np.nan
            Zx = (X - x_mean) / x_std
            valid_x = ~np.isnan(Zx).any(axis=1)
            Zx = Zx[valid_x]
            syms = sub["symbol"].to_numpy()[valid_x]
            if len(Zx) == 0:
                continue

            corr = (Zx @ Zt.T) / WINDOW  # (n_cand, n_templates)

            # 자기참조 제외: 후보 종목 자신이 만든 템플릿은 그 종목 점수 계산에서 제외
            self_mask = syms[:, None] == template_symbols[None, :]
            corr_masked = np.where(self_mask, -np.inf, corr)

            k = min(TOP_K_SIM, corr_masked.shape[1])
            top_k = np.sort(corr_masked, axis=1)[:, -k:]
            score = top_k.mean(axis=1)
            Xv = X[valid_x]
            momentum = Xv[:, -1] / Xv[:, 0] - 1  # 윈도우 누적수익률(=단순 모멘텀)

            recs.append(pd.DataFrame({
                "date": d, "symbol": syms, "score": score, "momentum": momentum,
            }))
        out = pd.concat(recs, ignore_index=True) if recs else pd.DataFrame()
        print(f"  {label}: {len(out)}건 (날짜 {len(test_idx)}개)")
        return out

    is_scores = score_test_dates(test_idx_is, "IS")
    oos_scores = score_test_dates(test_idx_oos, "OOS")

    # forward 수익률 부착
    def attach_returns(scored: pd.DataFrame) -> pd.DataFrame:
        scored = scored.copy()
        scored["ret_fwd_5d"] = [fwd5.loc[d, s] if s in fwd5.columns else np.nan for d, s in zip(scored["date"], scored["symbol"])]
        scored["ret_fwd_20d"] = [fwd20.loc[d, s] if s in fwd20.columns else np.nan for d, s in zip(scored["date"], scored["symbol"])]
        return scored

    is_scores = attach_returns(is_scores)
    oos_scores = attach_returns(oos_scores)
    is_scores["regime"] = is_scores["date"].map(kospi_regime)
    oos_scores["regime"] = oos_scores["date"].map(kospi_regime)

    # 날짜별 횡단면 분위
    for sdf in (is_scores, oos_scores):
        sdf["score_rank"] = sdf.groupby("date")["score"].rank(pct=True)

    # ── 모멘텀과의 상관관계 (정직성 체크) ───────────────────────────
    print("\n" + "=" * 70); print("[5/6] 패턴 유사도 점수 vs 단순 모멘텀 상관관계 (정직성 체크)"); print("=" * 70)
    for label, sdf in [("IS", is_scores), ("OOS", oos_scores)]:
        corr = sdf[["score", "momentum"]].corr().iloc[0, 1]
        print(f"  {label}: corr(패턴유사도, 같은윈도우 모멘텀) = {corr:.3f}")

    # ── 핵심 검증: 상위10% vs 하위10% vs 전체 ───────────────────────
    print("\n" + "=" * 70); print("[6/6] 핵심 검증: 유사도 상위10% vs 하위10%"); print("=" * 70)

    summary_rows = []
    for period_label, sdf in [("IS(2022-23)", is_scores), ("OOS(2024-25)", oos_scores)]:
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            top = sdf[sdf["score_rank"] >= 0.9][horizon]
            bottom = sdf[sdf["score_rank"] <= 0.1][horizon]
            rest = sdf[(sdf["score_rank"] < 0.9)][horizon]
            r_top_vs_rest = diff_in_means(top, rest)
            r_bottom_vs_rest = diff_in_means(bottom, rest)
            summary_rows.append({
                "기간": period_label, "horizon": hname,
                "상위10%_평균%": round(r_top_vs_rest["mean_sat"], 3) if r_top_vs_rest["mean_sat"] is not None else None,
                "상위10%_승률": round(r_top_vs_rest["win_sat"], 3) if r_top_vs_rest["win_sat"] is not None else None,
                "상위10%_n": r_top_vs_rest["n_sat"],
                "나머지90%_평균%": round(r_top_vs_rest["mean_unsat"], 3) if r_top_vs_rest["mean_unsat"] is not None else None,
                "상위vs나머지_p": round(r_top_vs_rest["diff_p"], 4) if r_top_vs_rest["diff_p"] is not None else None,
                "하위10%_평균%": round(r_bottom_vs_rest["mean_sat"], 3) if r_bottom_vs_rest["mean_sat"] is not None else None,
                "하위10%_승률": round(r_bottom_vs_rest["win_sat"], 3) if r_bottom_vs_rest["win_sat"] is not None else None,
                "하위10%_n": r_bottom_vs_rest["n_sat"],
                "하위vs나머지_p": round(r_bottom_vs_rest["diff_p"], 4) if r_bottom_vs_rest["diff_p"] is not None else None,
            })
    summary_df = pd.DataFrame(summary_rows)
    print(summary_df.to_string(index=False))
    print(f"\nBonferroni 보정 임계값: {BONFERRONI_ALPHA:.5f} (총 {N_TESTS}회 비교 가정)")

    print("\n--- OOS 시장국면별 (5일 수익률, 상위10% vs 나머지) ---")
    for regime in ["강세", "횡보", "약세"]:
        sub = oos_scores[oos_scores["regime"] == regime]
        top = sub[sub["score_rank"] >= 0.9]["ret_fwd_5d"]
        rest = sub[sub["score_rank"] < 0.9]["ret_fwd_5d"]
        r = diff_in_means(top, rest)
        print(f"  {regime}: 상위10% n={r['n_sat']}, 평균={r['mean_sat']}, 승률={r['win_sat']}, "
              f"나머지평균={r['mean_unsat']}, p={r['diff_p']}")

    out = Path(__file__).parent / f"chart_pattern_validation_results_w{WINDOW}.csv"
    summary_df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n결과 저장: {out}")
    print(f"총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
