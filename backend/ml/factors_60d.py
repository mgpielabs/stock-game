"""60d 모델 전용 PIT 팩터 계산 공용 모듈.

predictor.py(_compute_60d_factors_pit)와 train_60d.py(step 4 인라인)의
동일 로직을 한 곳에서 관리 — train-serving skew 방지.

공개 API:
  load_factor_data(conn)       → (eps_df, bps_df, dps_df) 원본 로드
  compute_eps_signals(eps_df)  → EPS YoY 성장률 + 가속도
  compute_roe_signals(...)     → ROE = EPS / BPS
  compute_dps_signals(dps_df)  → DPS YoY 성장률
  compute_bps_signals(bps_df)  → BPS YoY 성장률
  biz_year_pit(date_int)       → PIT 사업연도
  compute_factors_for_date(date_str, conn)   ← predictor.py용 (단일 날짜)
  join_pit_factors(df, ...)                  ← train_60d.py용 (전체 DF)
"""
import sqlite3
from typing import Tuple

import numpy as np
import pandas as pd


# ── 원본 데이터 로드 ──────────────────────────────────────────────

def load_factor_data(conn: sqlite3.Connection) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """eps_df, bps_df, dps_df 원본 로드 + 타입 정규화."""
    eps_df = pd.read_sql_query(
        "SELECT symbol, biz_year, eps FROM dividends "
        "WHERE eps IS NOT NULL ORDER BY symbol, biz_year",
        conn)
    bps_df = pd.read_sql_query(
        "SELECT symbol, biz_year, bps FROM financials "
        "WHERE bps IS NOT NULL ORDER BY symbol, biz_year",
        conn)
    dps_df = pd.read_sql_query(
        "SELECT symbol, biz_year, dps FROM dividends "
        "WHERE dps IS NOT NULL AND dps>0 AND dps<1000000 ORDER BY symbol, biz_year",
        conn)
    for _df in [eps_df, bps_df, dps_df]:
        _df["symbol"]   = _df["symbol"].astype(str).str.zfill(6)
        _df["biz_year"] = _df["biz_year"].astype(int)
    return eps_df, bps_df, dps_df


# ── 신호 계산 ────────────────────────────────────────────────────

def compute_eps_signals(eps_df: pd.DataFrame) -> pd.DataFrame:
    """EPS YoY 성장률 + 가속도. 전체 biz_year 포함 DataFrame 반환."""
    eps = eps_df.sort_values(["symbol", "biz_year"]).copy()
    eps["eps_prev"]         = eps.groupby("symbol")["eps"].shift(1)
    eps["eps_growth_yoy"]   = np.where(
        eps["eps_prev"].notna() & (eps["eps_prev"] != 0),
        ((eps["eps"] - eps["eps_prev"]) / eps["eps_prev"].abs()).clip(-5, 5),
        np.nan)
    eps["eps_growth_accel"] = (
        eps["eps_growth_yoy"] - eps.groupby("symbol")["eps_growth_yoy"].shift(1)
    ).clip(-5, 5)
    return eps


def compute_roe_signals(eps_df: pd.DataFrame, bps_df: pd.DataFrame) -> pd.DataFrame:
    """ROE = EPS / BPS (clip [-5, 5])."""
    roe = eps_df[["symbol", "biz_year", "eps"]].merge(
        bps_df[["symbol", "biz_year", "bps"]], on=["symbol", "biz_year"], how="inner")
    roe["roe_level"] = np.where(
        roe["bps"] != 0, (roe["eps"] / roe["bps"]).clip(-5, 5), np.nan)
    return roe


def compute_dps_signals(dps_df: pd.DataFrame) -> pd.DataFrame:
    """DPS YoY 성장률."""
    dps = dps_df.sort_values(["symbol", "biz_year"]).copy()
    dps["dps_prev"]       = dps.groupby("symbol")["dps"].shift(1)
    dps["dps_growth_yoy"] = np.where(
        dps["dps_prev"].notna() & (dps["dps_prev"] > 0),
        ((dps["dps"] - dps["dps_prev"]) / dps["dps_prev"]).clip(-5, 5),
        np.nan)
    return dps


def compute_bps_signals(bps_df: pd.DataFrame) -> pd.DataFrame:
    """BPS YoY 성장률."""
    bps_s = bps_df.sort_values(["symbol", "biz_year"]).copy()
    bps_s["bps_prev"]       = bps_s.groupby("symbol")["bps"].shift(1)
    bps_s["bps_growth_yoy"] = np.where(
        bps_s["bps_prev"].notna() & (bps_s["bps_prev"] != 0),
        ((bps_s["bps"] - bps_s["bps_prev"]) / bps_s["bps_prev"].abs()).clip(-5, 5),
        np.nan)
    return bps_s


# ── PIT 헬퍼 ────────────────────────────────────────────────────

def biz_year_pit(date_int: int) -> int:
    """날짜 정수(YYYYMMDD) → PIT 사업연도.
    공시지연 3개월 가정: 4월 이후는 전년도, 3월 이전은 전전년도."""
    year  = date_int // 10000
    month = (date_int % 10000) // 100
    return year - 1 if month >= 4 else year - 2


# ── predictor.py용 ───────────────────────────────────────────────

def compute_factors_for_date(date_str: str, conn: sqlite3.Connection) -> pd.DataFrame:
    """단일 날짜 PIT 기준 팩터 계산. predictor.py용.

    반환: symbol + eps_growth_yoy / eps_growth_accel / roe_level /
           dps_growth_yoy / bps_growth_yoy (outer merge, 없으면 NaN)."""
    eps_df, bps_df, dps_df = load_factor_data(conn)
    byp = biz_year_pit(int(date_str))

    eps_s = compute_eps_signals(eps_df)
    roe_s = compute_roe_signals(eps_df, bps_df)
    dps_s = compute_dps_signals(dps_df)
    bps_s = compute_bps_signals(bps_df)

    eps_pit = eps_s[eps_s["biz_year"] == byp][["symbol", "eps_growth_yoy", "eps_growth_accel"]]
    roe_pit = roe_s[roe_s["biz_year"] == byp][["symbol", "roe_level"]]
    dps_pit = dps_s[dps_s["biz_year"] == byp][["symbol", "dps_growth_yoy"]]
    bps_pit = bps_s[bps_s["biz_year"] == byp][["symbol", "bps_growth_yoy"]]

    return (
        eps_pit.merge(roe_pit, on="symbol", how="outer")
               .merge(dps_pit, on="symbol", how="outer")
               .merge(bps_pit, on="symbol", how="outer")
    )


# ── train_60d.py용 ──────────────────────────────────────────────

def join_pit_factors(df: pd.DataFrame,
                     eps_s: pd.DataFrame,
                     roe_s: pd.DataFrame,
                     dps_s: pd.DataFrame,
                     bps_s: pd.DataFrame) -> pd.DataFrame:
    """biz_year_pit 컬럼이 있는 df에 신호를 left-join. train_60d.py용.

    df는 ['symbol', 'biz_year_pit'] 컬럼을 포함해야 함."""
    def _jpit(d, sig, by_col, cols):
        s = sig[["symbol", by_col] + cols].rename(columns={by_col: "biz_year_pit"})
        return d.merge(s, on=["symbol", "biz_year_pit"], how="left")

    df = _jpit(df, eps_s, "biz_year", ["eps_growth_yoy", "eps_growth_accel"])
    df = _jpit(df, roe_s, "biz_year", ["roe_level"])
    df = _jpit(df, dps_s, "biz_year", ["dps_growth_yoy"])
    df = _jpit(df, bps_s, "biz_year", ["bps_growth_yoy"])
    return df
