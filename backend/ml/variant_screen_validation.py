"""
[검증 전용 — 운영 코드/모델 미사용] "가짜로 판명" 필터 3종(거래량급증/모멘텀상위/52주신고가)의
변형 탐색 — 원래 정의가 가짜였다고 변형도 전부 가짜인지, 진짜 edge가 있는 변형이 숨어있는지 확인.

factor_screen_validation.py와 동일한 데이터/방법론(load_prices, load_kospi_regime)을 재사용하되,
변형이 수십 개라 다중비교 문제가 훨씬 심각함 — 그래서 이 스크립트는 두 단계로 보정을 분리함:

  [1단계: 본 검사] 변형 N개 x horizon(5일/20일) = 2N개 검정. IS(2022-24)/OOS(2025-26) p-value를
    각각 모아 Benjamini-Hochberg FDR(q=0.05)로 보정. "✅ 진짜"는 IS·OOS 둘 다 FDR 통과 +
    부호 일치만 인정(저PBR 재검증과 동일 기준). Bonferroni(0.05/2N, 훨씬 엄격)도 같이 표시해서
    "FDR만 통과/Bonferroni도 통과"를 구분 — 후자가 더 신뢰도 높음.
  [2단계: 국면 분석] 1단계에서 "✅ 진짜"가 아니었던 변형만(저PBR이 약세장에서 살아난 것처럼,
    전체는 무의미해도 특정 국면에서 살아있는지 확인) OOS 5일 기준 강세/횡보/약세로 쪼개 재검정.
    이것도 별도의 검정 묶음이므로 그 안에서 다시 FDR 보정(q=0.05) — "국면조건부"도 우연통과
    방지 없이는 의미 없음.

정직성 원칙: 몇 개 시도해서 몇 개 통과했는지 항상 분모/분자로 명시. 효과크기(평균수익률)가
통계적으론 유의해도 0.3%p 미만이면 "유의하나 실용성 낮음"으로 별도 표시.

실행: python variant_screen_validation.py (IDLE 우선순위, 운영 데이터 SELECT만)
"""

import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from factor_screen_validation import load_prices, load_kospi_regime, _test_group, IN_SAMPLE_YEARS, OOS_YEARS

pd.set_option("display.width", 180)
pd.set_option("display.max_rows", 200)

MIN_EFFECT_PCT = 0.3  # 평균수익률 절대값이 이 미만이면 "유의하나 실용성 낮음"


# ── 다중비교 보정 ─────────────────────────────────────────────

def bh_fdr(pvals: List[Optional[float]], q: float = 0.05) -> List[bool]:
    """Benjamini-Hochberg FDR. None은 미검정으로 취급(통과 불가). 반환: 원래 순서의 통과 여부."""
    idx_valid = [i for i, p in enumerate(pvals) if p is not None]
    if not idx_valid:
        return [False] * len(pvals)
    m = len(idx_valid)
    sorted_idx = sorted(idx_valid, key=lambda i: pvals[i])
    passed = [False] * len(pvals)
    max_k = 0
    for rank, i in enumerate(sorted_idx, start=1):
        if pvals[i] <= (rank / m) * q:
            max_k = rank
    for rank, i in enumerate(sorted_idx, start=1):
        if rank <= max_k:
            passed[i] = True
    return passed


# ── 변형 조건 빌드 ─────────────────────────────────────────────

def build_variant_conditions(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[Tuple[str, str]]]:
    g = df.groupby("symbol", group_keys=False)
    variants: List[Tuple[str, str]] = []

    df["ret_fwd_5d"] = g["close"].transform(lambda s: s.shift(-5) / s - 1)
    df["ret_fwd_20d"] = g["close"].transform(lambda s: s.shift(-20) / s - 1)
    from evaluate import MIN_VOLUME_KRW
    df["value_ma20"] = g["value"].transform(lambda s: s.rolling(20).mean())
    df["liquid"] = df["value_ma20"] >= MIN_VOLUME_KRW

    df["ret_1d"] = g["close"].transform(lambda s: s.pct_change())

    # ── 거래량 변형 ──
    df["vol_ma60"] = g["volume"].transform(lambda s: s.rolling(60).mean())
    df["vol_ratio_60"] = df["volume"] / df["vol_ma60"].replace(0, np.nan)
    for mult in [2, 3, 5, 10]:
        col = f"v_vol_{mult}x"
        df[col] = df["vol_ratio_60"] >= mult
        variants.append((f"거래량 {mult}배+", col))
    for mult in [2, 5]:
        col_up = f"v_vol_{mult}x_up"
        df[col_up] = (df["vol_ratio_60"] >= mult) & (df["ret_1d"] > 0)
        variants.append((f"거래량 {mult}배+ & 당일상승", col_up))
        col_dn = f"v_vol_{mult}x_dn"
        df[col_dn] = (df["vol_ratio_60"] >= mult) & (df["ret_1d"] < 0)
        variants.append((f"거래량 {mult}배+ & 당일하락", col_dn))

    # ── 모멘텀 변형 (기간) ──
    for period in [1, 3, 5, 10, 20, 60]:
        ret_col = f"_ret_{period}d"
        df[ret_col] = g["close"].transform(lambda s, p=period: s.pct_change(p))
        rank = df.groupby("date")[ret_col].rank(pct=True)
        top_col = f"v_mom_{period}d_top20"
        df[top_col] = rank >= 0.8
        variants.append((f"모멘텀 상위20%({period}일)", top_col))
        bot_col = f"v_mom_{period}d_bot20"
        df[bot_col] = rank <= 0.2
        variants.append((f"역추세(모멘텀 하위20%, {period}일)", bot_col))

    # 강도 변형 (20일 기준 10%/30%)
    rank20 = df.groupby("date")["_ret_20d"].rank(pct=True)
    df["v_mom_20d_top10"] = rank20 >= 0.9
    variants.append(("모멘텀 상위10%(20일, 강한강도)", "v_mom_20d_top10"))
    df["v_mom_20d_top30"] = rank20 >= 0.7
    variants.append(("모멘텀 상위30%(20일, 약한강도)", "v_mom_20d_top30"))

    # ── 52주 신고가 변형 ──
    df["high_252"] = g["close"].transform(lambda s: s.rolling(252, min_periods=200).max())
    for pct in [0.95, 0.98, 1.0]:
        col = f"v_near_high_{int(pct*100)}"
        df[col] = df["close"] >= pct * df["high_252"]
        label = "52주신고가 돌파(=100%)" if pct == 1.0 else f"52주신고가 근접({int(pct*100)}%+)"
        variants.append((label, col))
    # 돌파 직후: 어제는 신고가가 아니었는데 오늘 새로 신고가 갱신
    prev_high = g["high_252"].shift(1)
    df["v_breakout_fresh"] = (df["close"] >= df["high_252"]) & (df["close"].shift(1).fillna(0) < prev_high.fillna(np.inf))
    variants.append(("52주신고가 돌파 직후(갱신 당일)", "v_breakout_fresh"))
    # 반대: 52주 신고가에서 많이 떨어진(80% 미만) 종목
    df["v_far_from_high"] = df["close"] < 0.80 * df["high_252"]
    variants.append(("52주신고가 대비 80%미만(저조)", "v_far_from_high"))

    return df, variants


def evaluate(df: pd.DataFrame, cond_col: pd.Series, horizon: str, years: List[int]) -> Dict:
    mask_year = df["date"].str[:4].astype(int).isin(years)
    base = df[mask_year & df["liquid"]]
    cond = cond_col.reindex(df.index, fill_value=False)
    sat = base[cond.loc[base.index]]
    mean, win, p, n = _test_group(sat[horizon])
    return {"mean_pct": mean, "win": win, "p": p, "n": n}


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/4] 데이터 로드 + 변형 조건 계산"); print("=" * 70)
    prices = load_prices(conn)
    print(f"유니버스: {prices['symbol'].nunique()}종목, {len(prices)}행")
    prices, variants = build_variant_conditions(prices)
    kospi_regime = load_kospi_regime(conn)
    conn.close()
    print(f"변형 후보 {len(variants)}개 x 2 horizon = {len(variants)*2}회 검정 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/4] 1단계 본 검사 — IS(2022-24)/OOS(2025-26), FDR + Bonferroni"); print("=" * 70)
    rows = []
    for label, col in variants:
        for horizon, hname in [("ret_fwd_5d", "5일"), ("ret_fwd_20d", "20일")]:
            is_r = evaluate(prices, prices[col], horizon, IN_SAMPLE_YEARS)
            oos_r = evaluate(prices, prices[col], horizon, OOS_YEARS)
            rows.append({
                "변형": label, "컬럼": col, "기간": hname,
                "IS_평균%": is_r["mean_pct"], "IS_승률": is_r["win"], "IS_n": is_r["n"], "IS_p": is_r["p"],
                "OOS_평균%": oos_r["mean_pct"], "OOS_승률": oos_r["win"], "OOS_n": oos_r["n"], "OOS_p": oos_r["p"],
            })
    result = pd.DataFrame(rows)

    n_tests = len(result)
    bonf_alpha = 0.05 / n_tests
    result["IS_FDR통과"] = bh_fdr(list(result["IS_p"]), q=0.05)
    result["OOS_FDR통과"] = bh_fdr(list(result["OOS_p"]), q=0.05)
    result["부호일치"] = [
        (a is not None and b is not None and np.sign(a) == np.sign(b))
        for a, b in zip(result["IS_평균%"], result["OOS_평균%"])
    ]
    result["IS_Bonf통과"] = [p is not None and p < bonf_alpha for p in result["IS_p"]]
    result["OOS_Bonf통과"] = [p is not None and p < bonf_alpha for p in result["OOS_p"]]

    def _verdict(r):
        fdr_pass = r["IS_FDR통과"] and r["OOS_FDR통과"] and r["부호일치"]
        if not fdr_pass:
            return "❌ 1단계 기각"
        bonf_pass = r["IS_Bonf통과"] and r["OOS_Bonf통과"]
        weak = abs(r["OOS_평균%"]) < MIN_EFFECT_PCT if r["OOS_평균%"] is not None else True
        if bonf_pass:
            return "✅ 진짜(Bonferroni도 통과)" + (" — 효과크기 작음" if weak else "")
        return "✅ 진짜(FDR만 통과)" + (" — 효과크기 작음" if weak else "")

    result["판정"] = result.apply(_verdict, axis=1)
    n_pass = (result["IS_FDR통과"] & result["OOS_FDR통과"] & result["부호일치"]).sum()
    print(f"검정 {n_tests}회(변형 {len(variants)}개 x 2 horizon) 중 1단계(IS+OOS FDR 통과 + 부호일치) "
          f"통과: {n_pass}건 — {'우연 가능성 낮음' if n_pass else '예상대로 대부분 가짜'}")
    print(f"Bonferroni 임계값: {bonf_alpha:.6f} (0.05/{n_tests})\n")

    cols_show = ["변형", "기간", "IS_평균%", "IS_승률", "IS_n", "OOS_평균%", "OOS_승률", "OOS_n", "판정"]
    print(result[cols_show].to_string(index=False))
    print()

    out1 = Path(__file__).parent / "variant_screen_results.csv"
    result.to_csv(out1, index=False, encoding="utf-8-sig")
    print(f"저장: {out1}\n")

    print("=" * 70); print("[3/4] 2단계 국면 분석 — 1단계 기각된 변형만, OOS 5일 국면별 재검정"); print("=" * 70)
    prices["regime"] = prices["date"].map(kospi_regime)
    sub_oos = prices[prices["date"].str[:4].astype(int).isin(OOS_YEARS) & prices["liquid"]]

    # 주의: 만족군 평균을 0과 비교하면 그 국면 자체의 시장 드리프트(강세장=우상향, 약세장 후
    # 반등 등)와 혼동됨 — 강세장에 거의 모든 조건이 "0보다 유의하게 높음"으로 나오는 게 그 증거
    # (실제로 1차 시도에서 72건 중 54건이 통과해 드리프트 오염임을 확인). 그래서 국면별로는
    # 만족군 vs 그 국면의 불만족군(diff-in-means)으로 비교해 국면 자체의 드리프트를 통제함.
    failed = result[(result["기간"] == "5일") & (result["판정"] == "❌ 1단계 기각")]["컬럼"].unique()
    regime_rows = []
    for col in failed:
        label = next(l for l, c in variants if c == col)
        for regime in ["강세", "횡보", "약세"]:
            grp = sub_oos[sub_oos["regime"] == regime]
            cond = grp[col]
            sat, unsat = grp[cond]["ret_fwd_5d"].dropna(), grp[~cond]["ret_fwd_5d"].dropna()
            sat_mean = float(sat.mean() * 100) if len(sat) else None
            unsat_mean = float(unsat.mean() * 100) if len(unsat) else None
            sat_win = float((sat > 0).mean()) if len(sat) else None
            diff_p = None
            if len(sat) >= 30 and len(unsat) >= 30:
                _, diff_p = stats.ttest_ind(sat, unsat, equal_var=False)
            regime_rows.append({
                "변형": label, "컬럼": col, "국면": regime,
                "만족군평균%": round(sat_mean, 3) if sat_mean is not None else None,
                "불만족군평균%": round(unsat_mean, 3) if unsat_mean is not None else None,
                "차이%p": round(sat_mean - unsat_mean, 3) if (sat_mean is not None and unsat_mean is not None) else None,
                "만족군승률": round(sat_win, 3) if sat_win is not None else None,
                "n": len(sat), "p": round(diff_p, 6) if diff_p is not None else None,
            })
    regime_df = pd.DataFrame(regime_rows)
    n_regime_tests = regime_df["p"].notna().sum()
    regime_df["FDR통과"] = bh_fdr(list(regime_df["p"]), q=0.05)
    regime_df["효과충분"] = regime_df["차이%p"].abs() >= MIN_EFFECT_PCT
    regime_df["표본충분"] = regime_df["n"] >= 100
    regime_df["국면조건부_인정"] = regime_df["FDR통과"] & regime_df["효과충분"] & regime_df["표본충분"]

    n_regime_pass = regime_df["국면조건부_인정"].sum()
    print(f"국면별 재검정 {n_regime_tests}회(기각된 변형 {len(failed)}개 x 3국면, 5일horizon만) 중 "
          f"FDR(q=0.05)+효과크기({MIN_EFFECT_PCT}%p+)+표본(n≥100) 모두 통과: {n_regime_pass}건")
    if n_regime_pass:
        print(regime_df[regime_df["국면조건부_인정"]][
            ["변형", "국면", "만족군평균%", "불만족군평균%", "차이%p", "만족군승률", "n", "p"]
        ].to_string(index=False))
    else:
        print("→ 국면 쪼개기로도 살아나는 변형 없음 (저PBR과 달리 이 가짜 필터들은 국면조건부도 아님)")
    print()

    out2 = Path(__file__).parent / "variant_screen_regime_results.csv"
    regime_df.to_csv(out2, index=False, encoding="utf-8-sig")
    print(f"저장: {out2}\n")

    print("=" * 70); print("[4/4] 최종 요약"); print("=" * 70)
    print(f"1단계(전체 평균 edge): {n_tests}회 검정 중 {n_pass}건 통과")
    print(f"2단계(국면조건부): {n_regime_tests}회 검정 중 {n_regime_pass}건 통과")
    print(f"총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
