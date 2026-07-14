"""
기술적 지표 계산 모듈
모든 함수는 pandas Series/DataFrame을 받아 Series를 반환
외부 라이브러리 없이 순수 pandas/numpy 구현 (TA-Lib 불필요)
"""

import numpy as np
import pandas as pd


# ── 수익률 ───────────────────────────────────────────────────

def calc_returns(close: pd.Series) -> pd.DataFrame:
    """1/5/20/60일 수익률"""
    return pd.DataFrame({
        "ret_1d":  close.pct_change(1),
        "ret_5d":  close.pct_change(5),
        "ret_20d": close.pct_change(20),
        "ret_60d": close.pct_change(60),
    })


# ── 이동평균 ─────────────────────────────────────────────────

def calc_ma_deviations(close: pd.Series) -> pd.DataFrame:
    """이동평균 이격도: (종가/MA) - 1"""
    return pd.DataFrame({
        "ma5_dev":   close / close.rolling(5).mean()   - 1,
        "ma20_dev":  close / close.rolling(20).mean()  - 1,
        "ma60_dev":  close / close.rolling(60).mean()  - 1,
        "ma120_dev": close / close.rolling(120).mean() - 1,
    })


# ── RSI ──────────────────────────────────────────────────────

def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    # Wilder's smoothing (EMA alpha = 1/period)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_rsi(close: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({
        "rsi_7":  _rsi(close, 7),
        "rsi_14": _rsi(close, 14),
    })


# ── MACD (12/26/9) ───────────────────────────────────────────

def calc_macd(close: pd.Series) -> pd.DataFrame:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal    = macd_line.ewm(span=9, adjust=False).mean()
    hist      = macd_line - signal

    # 종가로 정규화 (비율로 만들어 종목 간 비교 가능하게)
    return pd.DataFrame({
        "macd":        macd_line / close,
        "macd_signal": signal    / close,
        "macd_hist":   hist      / close,
    })


# ── 볼린저밴드 ───────────────────────────────────────────────

def calc_bollinger(close: pd.Series, period: int = 20, std_k: float = 2.0) -> pd.DataFrame:
    mid    = close.rolling(period).mean()
    std    = close.rolling(period).std(ddof=0)
    upper  = mid + std_k * std
    lower  = mid - std_k * std
    band_w = upper - lower

    # 밴드 내 위치: 0(하단) ~ 1(상단), 범위 초과 가능
    bb_pct   = (close - lower) / band_w.replace(0, np.nan)
    # 밴드 폭 (변동성): band_width / mid
    bb_width = band_w / mid.replace(0, np.nan)

    return pd.DataFrame({"bb_pct": bb_pct, "bb_width": bb_width})


# ── 거래량 ───────────────────────────────────────────────────

def calc_volume_ratios(volume: pd.Series) -> pd.DataFrame:
    ma5  = volume.rolling(5).mean()
    ma20 = volume.rolling(20).mean()
    vr5  = volume / ma5.replace(0, np.nan)
    vr20 = volume / ma20.replace(0, np.nan)
    return pd.DataFrame({
        "vol_ratio_5d":  vr5,
        "vol_ratio_20d": vr20,
        "vol_surge":     (vr5 > 2.0).astype("Int64"),  # nullable int
    })


# ── ATR ──────────────────────────────────────────────────────

def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.DataFrame:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return pd.DataFrame({
        "atr_14":  atr,
        "atr_pct": atr / close.replace(0, np.nan),  # 종가 대비 비율
    })


# ── 패턴 ─────────────────────────────────────────────────────

def calc_patterns(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.DataFrame:
    prev_close = close.shift(1)
    total_range = (high - low).replace(0, np.nan)

    gap_pct      = (open_ - prev_close) / prev_close.replace(0, np.nan)
    body_ratio   = (close - open_).abs() / total_range
    body         = pd.concat([open_, close], axis=1)
    upper_shadow = (high - body.max(axis=1)) / total_range
    lower_shadow = (body.min(axis=1) - low) / total_range

    # 연속 상승/하락일수
    daily_up = (close > prev_close).astype(int)
    daily_dn = (close < prev_close).astype(int)

    def _streak(signal: pd.Series) -> pd.Series:
        group = (signal != signal.shift()).cumsum()
        streak = signal.groupby(group).cumcount() + 1
        return streak.where(signal.astype(bool), 0).astype("Int64")

    up_streak   = _streak(daily_up)
    down_streak = _streak(daily_dn)

    return pd.DataFrame({
        "gap_pct":      gap_pct,
        "body_ratio":   body_ratio,
        "upper_shadow": upper_shadow,
        "lower_shadow": lower_shadow,
        "up_streak":    up_streak,
        "down_streak":  down_streak,
    })


# ── 시계열 lag 피처 ──────────────────────────────────────────

def calc_lag_features(close: pd.Series, volume: pd.Series) -> pd.DataFrame:
    """수익률 lag, 거래량 비율 lag, 추세 일관성, 변동성, 가격 위치"""
    ret_1d = close.pct_change(1)
    vol_ma20 = volume.rolling(20, min_periods=1).mean()
    vol_ratio_raw = volume / vol_ma20.replace(0, np.nan)
    daily_up = (close > close.shift(1)).astype(float)

    feats: dict = {}

    # 수익률 lag (N일 전 일간 수익률)
    for lag in [1, 2, 3, 5, 10]:
        feats[f"ret_lag_{lag}"] = ret_1d.shift(lag)

    # 거래량 비율 lag
    for lag in [1, 3, 5]:
        feats[f"vol_ratio_lag_{lag}"] = vol_ratio_raw.shift(lag)

    # 추세 일관성
    feats["up_days_5"]  = daily_up.rolling(5,  min_periods=1).sum()
    feats["up_days_10"] = daily_up.rolling(10, min_periods=1).sum()

    # 변동성
    feats["volatility_5"]  = ret_1d.rolling(5,  min_periods=2).std()
    feats["volatility_20"] = ret_1d.rolling(20, min_periods=5).std()

    # 가격 위치
    for n in [20, 60]:
        lo = close.rolling(n, min_periods=1).min()
        hi = close.rolling(n, min_periods=1).max()
        feats[f"price_position_{n}"] = (close - lo) / (hi - lo).replace(0, np.nan)

    return pd.DataFrame(feats, index=close.index)


# ── 타겟 ─────────────────────────────────────────────────────

def calc_target(close: pd.Series) -> pd.Series:
    """다음날 종가 > 오늘 종가이면 1, 아니면 0, 마지막 행은 NaN"""
    next_ret = close.shift(-1) / close - 1
    return (next_ret > 0).astype("Int64").where(next_ret.notna())


# ── 전체 기술적 지표 통합 계산 ───────────────────────────────

def compute_all_technical(df: pd.DataFrame) -> pd.DataFrame:
    """
    입력: prices 테이블에서 읽은 DataFrame (date, open, high, low, close, volume)
    출력: 모든 기술적 피처가 담긴 DataFrame (index = date)
    최소 120개 행이 있어야 의미 있는 피처 생성 가능
    """
    if df.empty or len(df) < 5:
        return pd.DataFrame()

    # 인덱스 정렬
    df = df.sort_values("date").set_index("date")

    close  = df["close"].astype(float)
    open_  = df["open"].astype(float)
    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    volume = df["volume"].astype(float)

    parts = [
        calc_returns(close),
        calc_ma_deviations(close),
        calc_rsi(close),
        calc_macd(close),
        calc_bollinger(close),
        calc_volume_ratios(volume),
        calc_atr(high, low, close),
        calc_patterns(open_, high, low, close),
        calc_lag_features(close, volume),
        calc_target(close).rename("target"),
    ]

    result = pd.concat(parts, axis=1)
    result.index.name = "date"
    return result
