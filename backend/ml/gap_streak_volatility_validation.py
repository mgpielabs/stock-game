"""
[검증 전용 — 운영 코드/모델 미사용]
prices 테이블만으로 검증 가능한 패턴 (2026-07-01)

이미 기각된 것 제외(CLAUDE.md "신호 연구 > 계절성/캘린더" 참고):
  요일효과/월별효과/월중효과(윈도우드레싱)/명절 → 다중비교보정 후 전멸

신규 검증 대상 (4~8번):
  4. 갭 패턴  — (open-prev_close)/prev_close 상승갭/하락갭 후 당일·5일 수익률
  5. 연속 하락 — N일 연속 close<prev_close 후 5일 수익률 (평균회귀 가설)
  6. 연속 상승 — N일 연속 close>prev_close 후 5일 수익률 (추세vs반전)
  7. 변동성    — 전일 intraday range(high-low)/close 이후 수익률
  8. 갭+거래량 조합

방법: 기존 factor_screen_validation.py와 동일
  - walk-forward IS(2022~2023)/OOS(2024~2026) 분리
  - diff-in-means(만족군 vs 불만족군 비교, 시장 드리프트 편향 제거)
  - 유동성 필터(20일 평균 거래대금 ≥ 10억원)
  - 시장국면별(강세/횡보/약세, KOSPI 20일 수익률 ±3%)
  - Bonferroni 다중비교 보정
"""

import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00000040)  # IDLE
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sqlite3
import re
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH

# ── 상수 ──────────────────────────────────────────────────────
IS_START, IS_END = "20220101", "20231231"
OOS_START, OOS_END = "20240101", "20260630"
MIN_VOL_KRW = 1_000_000_000      # 20일 평균 거래대금 10억원
BONFERRONI_K = 30                 # 검증할 총 가설 수(보수적 추정)
ALPHA = 0.05 / BONFERRONI_K      # ≈ 0.00167
MIN_N = 100

ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)

REGIME_BULL = "강세"
REGIME_SIDE = "횡보"
REGIME_BEAR = "약세"


# ── 로드 ──────────────────────────────────────────────────────

def load_prices() -> pd.DataFrame:
    print("  prices 로드 중...", flush=True)
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            """
            SELECT p.symbol, p.date, p.open, p.high, p.low, p.close, p.volume, s.name
            FROM prices p
            JOIN stocks s ON p.symbol = s.symbol
            WHERE p.close IS NOT NULL AND p.close > 0
              AND p.open  IS NOT NULL AND p.open  > 0
              AND p.high  IS NOT NULL AND p.low   IS NOT NULL
              AND p.volume > 0
            ORDER BY p.symbol, p.date
            """, conn,
        )
    df = df[~df["name"].apply(lambda n: bool(ETF_PATTERN.search(n)) if isinstance(n, str) else False)]
    df = df.drop(columns="name")
    print(f"  prices: {len(df):,}행, {df['symbol'].nunique():,}종목", flush=True)
    return df


def load_kospi_regime() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        idx = pd.read_sql_query(
            "SELECT date, close FROM market_index WHERE code='1001' ORDER BY date", conn
        )
    idx["ret_20d"] = idx["close"].pct_change(20)
    idx["regime"] = REGIME_SIDE
    idx.loc[idx["ret_20d"] >  0.03, "regime"] = REGIME_BULL
    idx.loc[idx["ret_20d"] < -0.03, "regime"] = REGIME_BEAR
    return idx.set_index("date")[["regime"]]


# ── 시그널 계산 ───────────────────────────────────────────────

def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """종목별로 모든 시그널을 한번에 계산"""
    print("  시그널 계산 중...", flush=True)
    df = df.copy()
    df["volume_krw"] = df["close"] * df["volume"]  # groupby 전에 먼저 생성

    g = df.groupby("symbol", sort=False)

    df["prev_close"] = g["close"].shift(1)
    df["prev_high"]  = g["high"].shift(1)
    df["prev_low"]   = g["low"].shift(1)

    # 전진 수익률
    df["ret_fwd_1d"] = g["close"].transform(lambda c: c.shift(-1) / c - 1)
    df["ret_fwd_5d"] = g["close"].transform(lambda c: c.shift(-5) / c - 1)

    # 유동성: 20일 평균 거래대금
    df["vol_krw_20d"] = g["volume_krw"].transform(lambda v: v.rolling(20, min_periods=10).mean())

    # --- 4. 갭 ---
    df["gap_pct"] = (df["open"] - df["prev_close"]) / df["prev_close"]

    # --- 5/6. 연속 하락/상승 (streak 계산) ---
    def streak(series: pd.Series) -> pd.Series:
        """연속 하락(-1)/상승(+1) 일수 계산"""
        diff = (series > series.shift(1)).astype(int) * 2 - 1  # +1 상승, -1 하락
        # 방향이 바뀌면 카운터 리셋
        result = diff.copy().astype(float)
        for i in range(1, len(diff)):
            if diff.iloc[i] == diff.iloc[i-1]:
                result.iloc[i] = result.iloc[i-1] + diff.iloc[i]
            # else: 방향 바뀜 → diff.iloc[i] 그대로(±1 시작)
        return result

    # 벡터화 streak 계산
    up_direction = (df["close"] > df["prev_close"]).astype(int)
    # 부호 변환: +1(상승)/−1(하락) 런 인코딩
    changed = up_direction != up_direction.shift(1)
    run_id = changed.groupby(df["symbol"]).cumsum()
    run_len = run_id.groupby([df["symbol"], run_id]).cumcount() + 1
    df["down_streak"] = np.where(up_direction == 0, run_len, 0)
    df["up_streak"]   = np.where(up_direction == 1, run_len, 0)

    # --- 7. 전일 변동성(intraday range) ---
    df["prev_range_pct"] = (df["prev_high"] - df["prev_low"]) / df["prev_close"]

    # --- 8. 전일 거래량 비율 (갭+거래량 조합용) ---
    df["vol_ratio_prev"] = g["volume_krw"].transform(
        lambda v: v.shift(1) / v.rolling(20, min_periods=10).mean().shift(1)
    )

    df = df.dropna(subset=["prev_close", "ret_fwd_5d", "vol_krw_20d"])
    df = df[df["vol_krw_20d"] >= MIN_VOL_KRW]
    print(f"  유동성 필터 후: {len(df):,}행", flush=True)
    return df


# ── 검증 엔진 ─────────────────────────────────────────────────

def _ttest(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float, float]:
    """diff-in-means: a(만족군) vs b(불만족군) → mean_a, mean_b, diff, p"""
    if len(a) < MIN_N or len(b) < MIN_N:
        return float("nan"), float("nan"), float("nan"), float("nan")
    _, p = stats.ttest_ind(a, b, equal_var=False)
    return float(a.mean()), float(b.mean()), float(a.mean() - b.mean()), float(p)


def evaluate(df_period: pd.DataFrame, cond: pd.Series, label: str, regime_df, results: list):
    """하나의 조건(시그널)을 IS/OOS + 국면별로 평가"""
    for period_name, period_df in [
        ("IS", df_period[df_period["date"].between(IS_START, IS_END)]),
        ("OOS", df_period[df_period["date"].between(OOS_START, OOS_END)]),
    ]:
        cond_p = cond.loc[period_df.index]
        y = period_df["ret_fwd_5d"].values
        c = cond_p.values.astype(bool)

        ma, mb, diff, p = _ttest(y[c], y[~c])
        win_a = float(np.mean(y[c] > 0)) if c.sum() >= MIN_N else float("nan")
        win_b = float(np.mean(y[~c] > 0)) if (~c).sum() >= MIN_N else float("nan")
        results.append({
            "label": label, "period": period_name, "regime": "전체",
            "n_cond": int(c.sum()), "n_other": int((~c).sum()),
            "mean_cond_%": round(ma * 100, 3) if ma == ma else float("nan"),
            "mean_other_%": round(mb * 100, 3) if mb == mb else float("nan"),
            "diff_%": round(diff * 100, 3) if diff == diff else float("nan"),
            "win_rate_cond": round(win_a, 3),
            "p_value": round(p, 4) if p == p else float("nan"),
            "significant": bool(p < ALPHA) if p == p else False,
        })

        # 국면별
        period_df2 = period_df.join(regime_df, on="date", how="left")
        for regime in [REGIME_BULL, REGIME_SIDE, REGIME_BEAR]:
            mask = period_df2["regime"] == regime
            y_r = period_df.loc[mask, "ret_fwd_5d"].values
            c_r = cond_p.loc[mask].values.astype(bool)
            if len(y_r) < MIN_N * 2:
                continue
            ma_r, mb_r, diff_r, p_r = _ttest(y_r[c_r], y_r[~c_r])
            win_r = float(np.mean(y_r[c_r] > 0)) if c_r.sum() >= MIN_N else float("nan")
            results.append({
                "label": label, "period": period_name, "regime": regime,
                "n_cond": int(c_r.sum()), "n_other": int((~c_r).sum()),
                "mean_cond_%": round(ma_r * 100, 3) if ma_r == ma_r else float("nan"),
                "mean_other_%": round(mb_r * 100, 3) if mb_r == mb_r else float("nan"),
                "diff_%": round(diff_r * 100, 3) if diff_r == diff_r else float("nan"),
                "win_rate_cond": round(win_r, 3),
                "p_value": round(p_r, 4) if p_r == p_r else float("nan"),
                "significant": bool(p_r < ALPHA) if p_r == p_r else False,
            })


# ── 메인 ─────────────────────────────────────────────────────

def main():
    t0 = time.time()
    print("=" * 65)
    print("[1] 데이터 로드")
    print("=" * 65)
    df = load_prices()
    regime_df = load_kospi_regime()
    df = compute_signals(df)

    results = []

    print("\n" + "=" * 65)
    print("[2] 패턴별 검증")
    print("=" * 65)

    # ────────────────────────────────────────────────────────────
    # 4. 갭 패턴
    # ────────────────────────────────────────────────────────────
    print("\n--- 4. 갭 패턴 ---")
    for threshold, name in [(0.01, "갭업>1%"), (0.02, "갭업>2%"), (0.03, "갭업>3%"),
                             (-0.01, "갭다운<-1%"), (-0.02, "갭다운<-2%"), (-0.03, "갭다운<-3%")]:
        if threshold > 0:
            cond = df["gap_pct"] > threshold
        else:
            cond = df["gap_pct"] < threshold
        print(f"  {name}: {cond.sum():,}건")
        evaluate(df, cond, f"gap_{name}", regime_df, results)

    # 갭 1일 수익률도 추가 (당일이 이미 움직임 — fwd_1d 기준)
    for threshold, name in [(0.02, "갭업>2%_1d"), (-0.02, "갭다운<-2%_1d")]:
        if threshold > 0:
            cond = df["gap_pct"] > threshold
        else:
            cond = df["gap_pct"] < threshold
        subset = df.copy()
        subset["ret_fwd_5d"] = subset["ret_fwd_1d"]   # 1일 수익률로 교체
        evaluate(subset, cond, f"gap_{name}", regime_df, results)

    # ────────────────────────────────────────────────────────────
    # 5. 연속 하락 후 반등 (평균회귀 가설)
    # ────────────────────────────────────────────────────────────
    print("\n--- 5. 연속 하락 후 반등 ---")
    for n in [2, 3, 5, 7]:
        cond = df["down_streak"] >= n
        print(f"  {n}일+ 연속하락: {cond.sum():,}건")
        evaluate(df, cond, f"down_streak>={n}d", regime_df, results)

    # ────────────────────────────────────────────────────────────
    # 6. 연속 상승 후 (추세지속 vs 반전)
    # ────────────────────────────────────────────────────────────
    print("\n--- 6. 연속 상승 후 ---")
    for n in [2, 3, 5, 7]:
        cond = df["up_streak"] >= n
        print(f"  {n}일+ 연속상승: {cond.sum():,}건")
        evaluate(df, cond, f"up_streak>={n}d", regime_df, results)

    # ────────────────────────────────────────────────────────────
    # 7. 전일 변동성 상위 → 이후 수익률
    # ────────────────────────────────────────────────────────────
    print("\n--- 7. 전일 변동성(intraday range) ---")
    # 횡단면 상위/하위 20% 기준
    df["range_rank"] = df.groupby("date")["prev_range_pct"].rank(pct=True)
    for label, cond in [
        ("변동성상위20%", df["range_rank"] >= 0.8),
        ("변동성하위20%", df["range_rank"] <= 0.2),
        ("변동성상위5%",  df["range_rank"] >= 0.95),
    ]:
        print(f"  {label}: {cond.sum():,}건")
        evaluate(df, cond, f"vol_{label}", regime_df, results)

    # ────────────────────────────────────────────────────────────
    # 8. 갭 + 거래량 조합
    # ────────────────────────────────────────────────────────────
    print("\n--- 8. 갭+거래량 조합 ---")
    hi_vol = df["vol_ratio_prev"] >= 2.0   # 전일 거래량 2배 이상
    for threshold, gap_name in [(0.02, "갭업>2%"), (-0.02, "갭다운<-2%")]:
        if threshold > 0:
            gap_cond = df["gap_pct"] > threshold
        else:
            gap_cond = df["gap_pct"] < threshold
        cond = gap_cond & hi_vol
        print(f"  {gap_name}+고거래량: {cond.sum():,}건")
        evaluate(df, cond, f"combo_{gap_name}_hvol", regime_df, results)

    # 연속하락 + 고거래량 (반등 신호?)
    cond = (df["down_streak"] >= 3) & hi_vol
    print(f"  3일연속하락+고거래량: {cond.sum():,}건")
    evaluate(df, cond, "combo_down3_hvol", regime_df, results)

    # ────────────────────────────────────────────────────────────
    # 결과 정리
    # ────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("[3] 결과 분석")
    print("=" * 65)

    res = pd.DataFrame(results)
    out_path = Path(__file__).parent / "gap_streak_vol_validation_results.csv"
    res.to_csv(out_path, index=False)
    print(f"전체 결과: {out_path}")

    # OOS 전체 기준 유의한 것
    oos_sig = res[(res["period"] == "OOS") & (res["regime"] == "전체") & res["significant"]]
    print(f"\nOOS 전체 구간 유의(Bonferroni 보정): {len(oos_sig)}건")
    if not oos_sig.empty:
        print(oos_sig[["label","n_cond","mean_cond_%","mean_other_%","diff_%","win_rate_cond","p_value"]].to_string(index=False))

    # OOS 국면별 유의한 것
    oos_regime_sig = res[(res["period"] == "OOS") & (res["regime"] != "전체") & res["significant"]]
    print(f"\nOOS 국면별 유의: {len(oos_regime_sig)}건")
    if not oos_regime_sig.empty:
        print(oos_regime_sig[["label","regime","n_cond","mean_cond_%","mean_other_%","diff_%","win_rate_cond","p_value"]].to_string(index=False))

    # IS/OOS 방향 일치 여부 (단순 비교)
    print("\n--- IS/OOS 방향 일치 여부 (전체 국면, 주요 시그널) ---")
    pivot = res[res["regime"] == "전체"].pivot_table(
        index="label", columns="period", values=["diff_%", "p_value", "significant"]
    ).round(4)
    both_sig = []
    for label in res["label"].unique():
        is_row  = res[(res["label"]==label)&(res["period"]=="IS")&(res["regime"]=="전체")]
        oos_row = res[(res["label"]==label)&(res["period"]=="OOS")&(res["regime"]=="전체")]
        if is_row.empty or oos_row.empty:
            continue
        is_d  = is_row.iloc[0]["diff_%"]
        oos_d = oos_row.iloc[0]["diff_%"]
        oos_p = oos_row.iloc[0]["p_value"]
        oos_s = oos_row.iloc[0]["significant"]
        same_sign = (is_d * oos_d > 0) if (is_d == is_d and oos_d == oos_d) else False
        if oos_s or (same_sign and oos_p < 0.05):
            both_sig.append({
                "label": label,
                "IS_diff_%": is_d, "OOS_diff_%": oos_d,
                "OOS_p": oos_p, "OOS_sig_bonf": oos_s, "direction_match": same_sign,
            })
    if both_sig:
        print(pd.DataFrame(both_sig).to_string(index=False))
    else:
        print("  (없음 — OOS 전멸 또는 방향 불일치)")

    print(f"\n총 소요: {time.time()-t0:.1f}초")
    print("=" * 65)


if __name__ == "__main__":
    main()
