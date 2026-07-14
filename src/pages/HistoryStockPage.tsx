import { useEffect, useState, useCallback } from 'react'
import { useParams, Link, useNavigate, useLocation } from 'react-router-dom'
import { fetchCandlesCached, getCandleAt, type CandleBar } from '../api/yahooFinance'
import { WATCHLIST } from '../api/constants'
import { getSyntheticName, getSyntheticCandles, isSynthetic, SYNTHETIC_STOCKS, ALTERNATE_STOCKS } from '../api/syntheticStocks'
import { useHistoryStore } from '../store/historyStore'
import StockChart from '../components/StockChart'
import HistoryTradePanel from '../components/HistoryTradePanel'
import { formatKRW, formatNumber, formatChangePercent, formatChange, changeColor, changeBg } from '../utils/format'

const watchlistNameMap = Object.fromEntries(WATCHLIST.map((w) => [w.symbol, w.name]))
const randomStockNameMap = Object.fromEntries([...SYNTHETIC_STOCKS, ...ALTERNATE_STOCKS].map((s) => [s.symbol, s.name]))

export default function HistoryStockPage() {
  const { symbol } = useParams<{ symbol: string }>()
  const decoded = symbol ? decodeURIComponent(symbol) : ''
  const navigate = useNavigate()

  const { stockPositions, startDate, tradeHistory, holdings, sell, completeStock, customSymbols, addCustomSymbol } = useHistoryStore()
  const gameDate = stockPositions[decoded] ?? startDate
  const location = useLocation()
  const locationName = (location.state as { name?: string } | null)?.name

  const [allCandles, setAllCandles] = useState<CandleBar[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!decoded) return
    if (isSynthetic(decoded)) {
      setAllCandles(getSyntheticCandles(decoded))
      setLoading(false)
      return
    }
    setLoading(true)
    setError(null)
    fetchCandlesCached(decoded, '3y')
      .then((candles) => {
        setAllCandles(candles)
      })
      .catch(() => setError('데이터를 불러오지 못했습니다.'))
      .finally(() => setLoading(false))
  }, [decoded])

  const cutoffSec = Math.floor(gameDate / 1000)
  const visibleCandles = allCandles.filter((c) => c.time <= cutoffSec)

  const currentCandle = getCandleAt(allCandles, gameDate)
  const currentIdx = allCandles.findLastIndex((c) => c.time <= cutoffSec)

  const nextCandle = allCandles.find((c) => c.time > cutoffSec)
  const nextTradingDateMs = nextCandle ? nextCandle.time * 1000 : gameDate + 86_400_000
  const hasReachedEnd = !nextCandle && allCandles.length > 0 && !loading
  const nextDayChangePct =
    nextCandle && currentCandle && currentCandle.close > 0
      ? ((nextCandle.close - currentCandle.close) / currentCandle.close) * 100
      : null
  const prevCandle = currentIdx > 0 ? allCandles[currentIdx - 1] : null

  const price = currentCandle?.close ?? null
  const change = price != null && prevCandle ? price - prevCandle.close : null
  const changePct =
    change != null && prevCandle && prevCandle.close > 0
      ? (change / prevCandle.close) * 100
      : null

  const startCandle = startDate != null ? getCandleAt(allCandles, startDate) : null
  const startPrice = startCandle?.close ?? null
  const buyHoldPct =
    startPrice != null && price != null && startPrice > 0
      ? ((price - startPrice) / startPrice) * 100
      : null

  const name = isSynthetic(decoded)
    ? getSyntheticName(decoded)
    : (watchlistNameMap[decoded] ?? randomStockNameMap[decoded] ?? customSymbols[decoded] ?? locationName ?? decoded)

  useEffect(() => {
    if (!isSynthetic(decoded) && !watchlistNameMap[decoded] && allCandles.length > 0) {
      addCustomSymbol(decoded, locationName ?? customSymbols[decoded] ?? decoded)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allCandles.length])

  const [infoOpen, setInfoOpen] = useState(true)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [showPanel, setShowPanel] = useState(true)

  const handleToggleFullscreen = useCallback(() => setIsFullscreen((v) => !v), [])
  const handleTogglePanel = useCallback(() => setShowPanel((v) => !v), [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setIsFullscreen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const gameDateStr = new Date(gameDate).toLocaleDateString('ko-KR', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })

  const handleComplete = useCallback(() => {
    const holding = holdings[decoded]
    if (holding && price != null) {
      sell(decoded, holding.name, price, holding.quantity, true)
    }
    completeStock(decoded)
    navigate(`/history/results/${encodeURIComponent(decoded)}`)
  }, [decoded, holdings, price, sell, completeStock, navigate])

  return (
    <main className="max-w-6xl mx-auto px-4 py-6">
      <Link
        to="/history"
        className="text-gray-400 hover:text-gray-300 text-sm mb-4 inline-flex items-center gap-1 transition-colors"
      >
        ← 목록으로
      </Link>

      {error && (
        <div className="mt-4 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-3 text-red-400 text-sm">
          {error}
        </div>
      )}

      <div className="mt-3 mb-4 flex items-center gap-2">
        <span className="bg-amber-500/20 text-amber-400 border border-amber-500/30 text-xs font-semibold px-2.5 py-1 rounded-full">
          {gameDateStr} 기준
        </span>
        {!loading && (
          <button
            onClick={() => setInfoOpen((v) => !v)}
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 bg-gray-800 hover:bg-gray-700 border border-gray-700 px-2.5 py-1 rounded-full transition-colors"
          >
            {infoOpen ? '▲ 접기' : `▼ ${name}  ${price != null ? formatKRW(price) : ''}${changePct != null ? `  ${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%` : ''}`}
          </button>
        )}
      </div>

      {infoOpen && (
        <div className="mb-6">
          {loading ? (
            <div className="animate-pulse space-y-2">
              <div className="h-7 w-40 bg-gray-800 rounded" />
              <div className="h-9 w-56 bg-gray-800 rounded" />
            </div>
          ) : (
            <div className="flex flex-wrap items-end gap-4">
              <div>
                <div className="flex items-center gap-2 mb-1">
                  <h1 className="text-white text-2xl font-bold">{name}</h1>
                  <span className="text-gray-400 text-sm">{decoded}</span>
                </div>
                {price != null ? (
                  <div className="flex items-end gap-3">
                    <span className="text-white text-3xl font-bold">{formatKRW(price)}</span>
                    {change != null && (
                      <span className={`text-lg font-semibold ${changeColor(change)}`}>
                        {formatChange(change)}
                      </span>
                    )}
                    {changePct != null && (
                      <span className={`text-sm px-2.5 py-0.5 rounded-full font-medium ${changeBg(changePct)}`}>
                        {formatChangePercent(changePct)}
                      </span>
                    )}
                  </div>
                ) : (
                  <p className="text-gray-400 text-sm">해당 날짜 데이터 없음</p>
                )}
              </div>
            </div>
          )}

          {currentCandle && !loading && (
            <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3">
              {[
                { label: '시가', value: formatKRW(currentCandle.open) },
                { label: '고가', value: formatKRW(currentCandle.high) },
                { label: '저가', value: formatKRW(currentCandle.low) },
                { label: '거래량', value: `${formatNumber(currentCandle.volume)}주` },
              ].map((item) => (
                <div key={item.label} className="bg-gray-900 border border-gray-800 rounded-xl px-3 py-2.5">
                  <p className="text-gray-400 text-xs mb-1">{item.label}</p>
                  <p className="text-white text-sm font-semibold">{item.value}</p>
                </div>
              ))}
            </div>
          )}

          {buyHoldPct != null && startPrice != null && !loading && (
            <div className="mt-3 flex items-center gap-3 bg-gray-900 border border-gray-800 rounded-xl px-4 py-3">
              <span className="text-xs text-gray-400 shrink-0">게임 시작가 기준 바이앤홀드</span>
              <span className="text-xs text-gray-500">{formatKRW(startPrice)} →</span>
              <span className={`text-sm font-bold ${buyHoldPct >= 0 ? 'text-red-400' : 'text-blue-400'}`}>
                {buyHoldPct >= 0 ? '+' : ''}{buyHoldPct.toFixed(2)}%
              </span>
              <span className="text-xs text-gray-500">({formatKRW(price!)})</span>
            </div>
          )}
        </div>
      )}

      {isFullscreen && (
        <div className="fixed inset-0 z-50 flex bg-gray-900">
          <div className="flex-1 min-w-0">
            <StockChart
              symbol={decoded}
              candles={visibleCandles}
              cutoffDate={gameDate}
              startDate={startDate}
              changePct={changePct}
              trades={tradeHistory
                .filter((t) => t.symbol === decoded)
                .map((t) => ({ type: t.type, timestamp: t.timestamp }))}
              isFullscreen={true}
              onToggleFullscreen={handleToggleFullscreen}
              showSidePanel={showPanel}
              onToggleSidePanel={handleTogglePanel}
            />
          </div>
          {showPanel && (
            <div className="w-80 shrink-0 overflow-y-auto border-l border-gray-800 p-3 space-y-3">
              {!loading ? (
                <HistoryTradePanel
                  symbol={decoded}
                  name={name}
                  price={price}
                  gameDate={gameDate}
                  nextTradingDateMs={nextTradingDateMs}
                  hasReachedEnd={hasReachedEnd}
                  onComplete={handleComplete}
                  startPrice={startPrice}
                  nextDayChangePct={nextDayChangePct}
                />
              ) : (
                <div className="bg-gray-900 rounded-xl border border-gray-800 p-4 animate-pulse h-64" />
              )}
            </div>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2">
          <StockChart
            symbol={decoded}
            candles={visibleCandles}
            cutoffDate={gameDate}
            startDate={startDate}
            changePct={changePct}
            trades={tradeHistory
              .filter((t) => t.symbol === decoded)
              .map((t) => ({ type: t.type, timestamp: t.timestamp }))}
            isFullscreen={false}
            onToggleFullscreen={handleToggleFullscreen}
          />
        </div>
        <div className="space-y-3">
          {!loading ? (
            <HistoryTradePanel
              symbol={decoded}
              name={name}
              price={price}
              gameDate={gameDate}
              nextTradingDateMs={nextTradingDateMs}
              hasReachedEnd={hasReachedEnd}
              onComplete={handleComplete}
              startPrice={startPrice}
              nextDayChangePct={nextDayChangePct}
            />
          ) : (
            <div className="bg-gray-900 rounded-xl border border-gray-800 p-4 animate-pulse h-64" />
          )}
        </div>
      </div>
    </main>
  )
}
