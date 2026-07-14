"""
[검증 전용 — 예측 모델/운영 코드 미사용] 추세추종 전략 단독 백테스트

규칙 (사용자 지정):
  진입: MA20 > MA60 > MA120 정배열 + ADX14 > 25(추세 강함 — 박스권 배제 기준으로도 사용,
        ADX는 방향 무관 추세의 "강도"를 측정하는 지표라 박스권에서는 구조적으로 낮게 나옴)
  청산: 종가가 MA20 아래로 마감(추세 이탈) 또는 손절
  손절: 진입가 대비 -7.5%(고정) / 진입가-2.5×ATR14(ATR 기반) 두 버전을 각각 백테스트해 비교
  포지션: 동시 보유 최대 10종목, 종목당 동일가중 10%(=1/10) — 빈 슬롯은 현금(0%)로 둠
          (신호가 적은 횡보장에는 자연스럽게 현금 비중이 높아지는 효과가 그대로 반영됨)

미래 누수 방지:
  - 지표(MA/ADX/ATR)는 각 시점까지의 과거 데이터로만 계산(rolling/ewm, shift 기반)
  - 신호는 t일 "종가 확정 후" 알 수 있으므로, 실제 진입은 t+1일 "시가"로 체결한다고 가정
  - 손절/추세이탈 청산도 그 날 바(low/close)로만 판단 — 미래 정보 사용 없음

비용: backend/ml/evaluate.py와 동일한 비용 상수(수수료/거래세/슬리피지) 재사용.
검증 대상이 아닌 기존 코드/모델은 전혀 수정하지 않음 — 이 파일은 신규 분석 스크립트.

실행: python trend_following_backtest.py
"""

import re
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

sys.path.insert(0, str(Path(__file__).parent))
from dataset import DB_PATH
from evaluate import COMMISSION, SELL_TAX, SLIPPAGE, MIN_VOLUME_KRW

pd.set_option("display.width", 160)

DATA_START  = "20221001"   # 지표 워밍업(MA120 등) 포함 로드 시작
EVAL_START  = "20230601"   # 실제 평가 구간 시작 (요청: 과거 2~3년)
EVAL_END    = "20260619"

MAX_POSITIONS  = 10
POS_WEIGHT     = 1.0 / MAX_POSITIONS
ADX_PERIOD     = 14
ATR_PERIOD     = 14
ADX_THRESHOLD  = 25.0
STOP_FIXED_PCT = 0.075
ATR_STOP_MULT  = 2.5
MIN_VALUE_MA20 = MIN_VOLUME_KRW  # 일 거래대금 20일 평균 최소 10억원

ENTRY_COST = COMMISSION + SLIPPAGE
EXIT_COST  = COMMISSION + SELL_TAX + SLIPPAGE

ETF_PATTERN = re.compile(
    r'ETF|ETN|레버리지|인버스|선물'
    r'|^(?:TIGER|KODEX|KOSEF|KINDEX|ARIRANG|HANARO|KBSTAR|TREX|ACE|RISE|SOL|TIMEFOLIO)\s',
    re.IGNORECASE,
)


# ── 지표 계산 (그룹별 — symbol마다 독립적인 rolling/ewm) ──────────

def calc_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.Series:
    """Wilder's ADX. df는 symbol 단일 종목, date 오름차순 정렬된 high/low/close 포함."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([
        high - low, (high - prev_close).abs(), (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx


def calc_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low, (high - prev_close).abs(), (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def load_universe(conn: sqlite3.Connection) -> pd.DataFrame:
    names = pd.read_sql_query("SELECT symbol, name FROM stocks", conn)
    etf_syms = set(names.loc[names["name"].fillna("").str.contains(ETF_PATTERN), "symbol"])

    prices = pd.read_sql_query(
        "SELECT symbol, date, open, high, low, close, volume FROM prices "
        "WHERE date BETWEEN ? AND ? ORDER BY symbol, date",
        conn, params=(DATA_START, EVAL_END),
    )
    prices = prices[~prices["symbol"].isin(etf_syms)].reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume"):
        prices[c] = prices[c].astype(float)
    prices["value"] = prices["close"] * prices["volume"]

    # 종목당 최소 행수(지표 워밍업 + 평가기간) 미달이면 제외
    counts = prices.groupby("symbol").size()
    keep = counts[counts >= 150].index
    prices = prices[prices["symbol"].isin(keep)].reset_index(drop=True)
    return prices


def build_indicators(prices: pd.DataFrame) -> pd.DataFrame:
    g = prices.groupby("symbol", group_keys=False)
    prices["ma20"]  = g["close"].transform(lambda s: s.rolling(20).mean())
    prices["ma60"]  = g["close"].transform(lambda s: s.rolling(60).mean())
    prices["ma120"] = g["close"].transform(lambda s: s.rolling(120).mean())
    prices["value_ma20"] = g["value"].transform(lambda s: s.rolling(20).mean())

    adx_list, atr_list = [], []
    for _, sub in prices.groupby("symbol", sort=False):
        adx_list.append(calc_adx(sub))
        atr_list.append(calc_atr(sub))
    prices["adx14"] = pd.concat(adx_list)
    prices["atr14"] = pd.concat(atr_list)

    prices["aligned"] = (prices["ma20"] > prices["ma60"]) & (prices["ma60"] > prices["ma120"])
    prices["entry_ok"] = (
        prices["aligned"] & (prices["adx14"] > ADX_THRESHOLD)
        & (prices["value_ma20"] >= MIN_VALUE_MA20)
    )
    # 참고용 보강 조건(사용자가 지정한 원래 규칙엔 없음) — "정배열"은 이동평균 순서만
    # 보장할 뿐 "현재 종가가 MA20 위"라는 보장이 없어, 가격이 이미 MA20 아래로 꺾이기
    # 시작한 시점에도 진입이 가능함. entry_ok_confirmed는 이 공백을 메운 비교용 버전.
    prices["entry_ok_confirmed"] = prices["entry_ok"] & (prices["close"] > prices["ma20"])
    prices["trend_break"] = prices["close"] < prices["ma20"]
    return prices


def load_kospi(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT date, open, high, low, close FROM market_index WHERE code='1001' "
        "AND date BETWEEN ? AND ? ORDER BY date",
        conn, params=(DATA_START, EVAL_END),
    )
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["adx14"] = calc_adx(df)
    df["ret"] = df["close"].pct_change()
    return df


# ── 시뮬레이션 ────────────────────────────────────────────────

def simulate(prices: pd.DataFrame, dates: List[str], stop_mode: str, entry_col: str = "entry_ok") -> Tuple[pd.Series, pd.DataFrame]:
    """stop_mode: 'fixed' or 'atr'. 반환: (날짜별 포트폴리오 수익률 Series, 거래 내역 DataFrame)"""
    by_date = prices.set_index(["date", "symbol"]).sort_index()
    # 빠른 조회를 위해 date -> {symbol: row} 매핑
    date_groups = {d: g.droplevel(0) for d, g in by_date.groupby(level=0)}

    active: Dict[str, Dict] = {}
    pending_entries: Dict[str, str] = {}  # symbol -> 신호 발생일 (다음날 시가 진입 대기)
    daily_returns: List[float] = []
    trades: List[Dict] = []

    prev_close: Dict[str, float] = {}

    for i, d in enumerate(dates):
        day_df = date_groups.get(d)
        if day_df is None:
            daily_returns.append(0.0)
            continue

        day_ret = 0.0

        # 1) 오늘 시가로 대기 중인 진입 처리
        for sym, sig_date in list(pending_entries.items()):
            if sym in active or len(active) >= MAX_POSITIONS:
                pending_entries.pop(sym, None)
                continue
            if sym not in day_df.index:
                continue
            row = day_df.loc[sym]
            if pd.isna(row["open"]) or row["open"] <= 0:
                continue
            entry_exec = row["open"] * (1 + ENTRY_COST)
            atr_at_entry = row["atr14"]
            if stop_mode == "fixed":
                stop_price = entry_exec * (1 - STOP_FIXED_PCT)
            else:
                stop_price = entry_exec - ATR_STOP_MULT * (atr_at_entry if pd.notna(atr_at_entry) else entry_exec * STOP_FIXED_PCT / ATR_STOP_MULT)
            active[sym] = {"entry_date": d, "entry_exec": entry_exec, "stop_price": stop_price}
            pending_entries.pop(sym, None)
            # 진입일 당일 수익 기여: 시가 체결가 -> 종가
            if pd.notna(row["close"]):
                day_ret += POS_WEIGHT * (row["close"] / entry_exec - 1)
                prev_close[sym] = row["close"]

        # 2) 활성 포지션 청산/유지 판단 + 일일 수익 기여
        for sym in list(active.keys()):
            if sym not in day_df.index:
                continue
            if active[sym]["entry_date"] == d:
                continue  # 위에서 이미 처리(진입일)
            row = day_df.loc[sym]
            pc = prev_close.get(sym)
            if pc is None or pd.isna(row["close"]):
                continue

            stop_price = active[sym]["stop_price"]
            stop_hit = pd.notna(row["low"]) and row["low"] <= stop_price
            trend_break = bool(row["trend_break"]) if pd.notna(row["trend_break"]) else False

            if stop_hit:
                exit_exec = stop_price * (1 - EXIT_COST)
                day_ret += POS_WEIGHT * (exit_exec / pc - 1)
                _close_trade(trades, sym, active[sym], d, exit_exec, "stop_loss")
                del active[sym]
                prev_close.pop(sym, None)
            elif trend_break:
                exit_exec = row["close"] * (1 - EXIT_COST)
                day_ret += POS_WEIGHT * (exit_exec / pc - 1)
                _close_trade(trades, sym, active[sym], d, exit_exec, "trend_break")
                del active[sym]
                prev_close.pop(sym, None)
            else:
                day_ret += POS_WEIGHT * (row["close"] / pc - 1)
                prev_close[sym] = row["close"]

        # 3) 오늘 신규 신호 포착 (내일 시가 진입 대기열에 등록)
        if len(active) + len(pending_entries) < MAX_POSITIONS * 2:  # 대기열 과다 방지용 여유 캡
            today_signals = day_df[day_df[entry_col] == True]
            for sym in today_signals.index:
                if sym not in active and sym not in pending_entries:
                    pending_entries[sym] = d

        daily_returns.append(day_ret)

    ret_series = pd.Series(daily_returns, index=dates)
    trades_df = pd.DataFrame(trades)
    return ret_series, trades_df


def _close_trade(trades: List[Dict], sym: str, pos: Dict, exit_date: str, exit_exec: float, reason: str) -> None:
    gross_entry = pos["entry_exec"]
    net_ret = exit_exec / gross_entry - 1
    trades.append({
        "symbol": sym, "entry_date": pos["entry_date"], "exit_date": exit_date,
        "net_return_pct": round(net_ret * 100, 2), "exit_reason": reason,
    })


# ── 성과 지표 ─────────────────────────────────────────────────

def perf_stats(ret_series: pd.Series) -> Dict:
    equity = (1 + ret_series).cumprod()
    total_return = float(equity.iloc[-1] - 1)
    n_days = len(ret_series)
    cagr = float(equity.iloc[-1] ** (252 / n_days) - 1) if n_days > 0 else None
    roll_max = equity.cummax()
    drawdown = equity / roll_max - 1
    mdd = float(drawdown.min())
    return {"total_return_pct": round(total_return * 100, 2), "cagr_pct": round(cagr * 100, 2) if cagr else None,
            "mdd_pct": round(mdd * 100, 2), "n_days": n_days}


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)

    print("=" * 70); print("[1/4] 데이터 로드 + 지표 계산"); print("=" * 70)
    prices = load_universe(conn)
    print(f"유니버스: {prices['symbol'].nunique()}종목, {len(prices)}행")
    prices = build_indicators(prices)

    kospi = load_kospi(conn)
    conn.close()

    eval_dates = sorted(prices.loc[prices["date"] >= EVAL_START, "date"].unique())
    print(f"평가구간: {eval_dates[0]} ~ {eval_dates[-1]} ({len(eval_dates)}거래일)")
    print(f"지표계산 완료 ({time.time()-t0:.0f}s)\n")

    print("=" * 70); print("[2/4] 시뮬레이션 (고정-7.5% 손절 / ATR-2.5x 손절 / 참고용 보강판)"); print("=" * 70)
    results = {}
    configs = [("fixed", "entry_ok"), ("atr", "entry_ok"), ("fixed_confirmed(참고용)", "entry_ok_confirmed")]
    for label, entry_col in configs:
        stop_mode = "atr" if label == "atr" else "fixed"
        t1 = time.time()
        ret_series, trades_df = simulate(prices, eval_dates, stop_mode=stop_mode, entry_col=entry_col)
        results[label] = (ret_series, trades_df)
        print(f"  [{label}] 거래 {len(trades_df)}건, 시뮬레이션 {time.time()-t1:.0f}s")
    print()

    print("=" * 70); print("[3/4] 성과 vs KOSPI 단순보유"); print("=" * 70)
    kospi_eval = kospi[kospi["date"].isin(eval_dates)].set_index("date")["ret"].fillna(0)
    kospi_eval = kospi_eval.reindex(eval_dates).fillna(0)
    bench_stats = perf_stats(kospi_eval)

    rows = []
    for mode, (ret_series, trades_df) in results.items():
        s = perf_stats(ret_series)
        closed = trades_df[trades_df["net_return_pct"].notna()] if not trades_df.empty else trades_df
        win_rate = float((closed["net_return_pct"] > 0).mean()) if len(closed) else None
        avg_trade = float(closed["net_return_pct"].mean()) if len(closed) else None
        rows.append({
            "전략(손절방식)": f"trend_{mode}" if "참고용" not in mode else mode,
            "총수익률%": s["total_return_pct"], "CAGR%": s["cagr_pct"],
            "MDD%": s["mdd_pct"], "거래수": len(closed), "승률": round(win_rate, 3) if win_rate else None,
            "평균거래수익%": round(avg_trade, 2) if avg_trade else None,
        })
    rows.append({"전략(손절방식)": "KOSPI 단순보유", "총수익률%": bench_stats["total_return_pct"],
                 "CAGR%": bench_stats["cagr_pct"], "MDD%": bench_stats["mdd_pct"],
                 "거래수": None, "승률": None, "평균거래수익%": None})
    summary = pd.DataFrame(rows)
    print(summary.to_string(index=False))
    print()

    print("=" * 70); print("[4/4] 추세장 vs 횡보장 구간별 성과"); print("=" * 70)
    kospi_regime = kospi.set_index("date")["adx14"].reindex(eval_dates)
    regime = np.where(kospi_regime > ADX_THRESHOLD, "추세장(KOSPI ADX>25)", "횡보장(KOSPI ADX<=25)")
    regime = pd.Series(regime, index=eval_dates)
    print(f"추세장 일수: {(regime=='추세장(KOSPI ADX>25)').sum()}, 횡보장 일수: {(regime=='횡보장(KOSPI ADX<=25)').sum()}\n")

    for mode, (ret_series, _) in results.items():
        print(f"--- {mode if '참고용' in mode else f'trend_{mode}'} ---")
        for r in regime.unique():
            mask = regime == r
            sub_ret = ret_series[mask]
            cum = float((1 + sub_ret).prod() - 1) * 100
            print(f"  {r}: 구간내 누적수익 {cum:+.2f}%  (일평균 {sub_ret.mean()*100:+.3f}%, {mask.sum()}일)")
    print("--- KOSPI 단순보유 ---")
    for r in regime.unique():
        mask = regime == r
        sub_ret = kospi_eval[mask]
        cum = float((1 + sub_ret).prod() - 1) * 100
        print(f"  {r}: 구간내 누적수익 {cum:+.2f}%  (일평균 {sub_ret.mean()*100:+.3f}%, {mask.sum()}일)")
    print()

    for mode, (_, trades_df) in results.items():
        safe_name = re.sub(r"[^0-9A-Za-z_]+", "_", mode)
        out = Path(__file__).parent / f"trend_following_trades_{safe_name}.csv"
        trades_df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"거래내역 저장: {out} ({len(trades_df)}행)")

    print(f"\n총 소요시간: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
