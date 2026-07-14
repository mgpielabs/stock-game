"""
수급 피처 계산
외국인 보유비율(%)의 변화로 순매수 방향 추정
"""

import numpy as np
import pandas as pd


def calc_flow_features(
    price_dates: pd.Index,
    flows_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    입력:
        price_dates : prices 테이블의 날짜 인덱스 (기준축)
        flows_df    : flows 테이블 DataFrame (date, foreign_net 컬럼)
                      foreign_net = 외국인 보유비율 (%)

    반환: DataFrame with flow 피처 컬럼 (인덱스=날짜)

    피처:
        foreign_rate    : 외국인 보유비율 (%)
        foreign_1d_chg  : 1일 변화량 (순매수 방향)
        foreign_5d_chg  : 5일 누적 변화 (추세)
        foreign_trend   : 10일 선형회귀 기울기 (정규화)
    """
    if flows_df.empty or "foreign_net" not in flows_df.columns:
        # 수급 데이터 없으면 NaN으로 채운 DataFrame 반환
        return pd.DataFrame(
            index=price_dates,
            columns=["foreign_rate", "foreign_1d_chg", "foreign_5d_chg", "foreign_trend"],
            dtype=float,
        )

    # flows 데이터를 price 날짜 인덱스에 맞춰 정렬
    flows_series = (
        flows_df.set_index("date")["foreign_net"]
        .reindex(price_dates)       # 거래일 기준으로 재인덱스
        .ffill(limit=5)             # 수급 데이터 없는 날은 최대 5거래일까지 전일 값으로 채움
                                    # (flows는 일별 표시용. 역산의 30거래일 상한과 용도가 다름)
        .astype(float)
    )

    foreign_rate   = flows_series
    foreign_1d_chg = flows_series.diff(1)
    foreign_5d_chg = flows_series.diff(5)

    def _slope_10d(series: pd.Series) -> pd.Series:
        from numpy.lib.stride_tricks import sliding_window_view
        arr = series.values.astype(float)
        x_c = np.arange(10) - 4.5  # x - x.mean(), mean of 0..9 = 4.5
        denom = (x_c ** 2).sum()
        windows = sliding_window_view(arr, 10)           # (N-9, 10), zero-copy
        y_c = windows - windows.mean(axis=1, keepdims=True)
        slopes = (x_c * y_c).sum(axis=1) / denom
        out = np.full(len(arr), np.nan)
        out[9:] = slopes
        out[9:][np.isnan(windows).any(axis=1)] = np.nan
        return pd.Series(out, index=series.index)

    foreign_trend = _slope_10d(flows_series)

    return pd.DataFrame({
        "foreign_rate":   foreign_rate,
        "foreign_1d_chg": foreign_1d_chg,
        "foreign_5d_chg": foreign_5d_chg,
        "foreign_trend":  foreign_trend,
    }, index=price_dates)
