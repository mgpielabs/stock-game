import { useCallback, useEffect, useRef, useState } from 'react'
import { createChart, createSeriesMarkers, CandlestickSeries, HistogramSeries, LineSeries, type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type SeriesMarker, type SeriesType, type Time } from 'lightweight-charts'
import { fetchCandles, type CandleBar } from '../api/yahooFinance'

type TF = 'D' | 'W' | 'M'

function sanitizeCandles(candles: CandleBar[]): CandleBar[] {
  if (candles.length === 0) return candles
  // 유효한 가격 범위 기준: 전체 중앙값의 1/20 ~ 20배
  const closes = candles.map((c) => c.close).filter((v) => v > 0).sort((a, b) => a - b)
  if (closes.length === 0) return candles
  const median = closes[Math.floor(closes.length / 2)]
  const lo = median / 20
  const hi = median * 20
  return candles.filter(
    (c) => c.open > 0 && c.high > 0 && c.low > 0 && c.close > 0
      && c.close >= lo && c.close <= hi
      && c.open >= lo && c.open <= hi
      && c.high >= c.low
  )
}

function aggregateCandles(candles: CandleBar[], tf: TF): CandleBar[] {
  if (tf === 'D') return candles
  const groups = new Map<string, CandleBar>()
  for (const c of candles) {
    const d = new Date(c.time * 1000)
    let key: string
    let t: number
    if (tf === 'W') {
      const day = d.getUTCDay()
      const diff = day === 0 ? -6 : 1 - day
      const mon = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + diff))
      key = mon.toISOString().slice(0, 10)
      t = Math.floor(mon.getTime() / 1000)
    } else {
      key = `${d.getUTCFullYear()}-${d.getUTCMonth()}`
      t = Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) / 1000)
    }
    const g = groups.get(key)
    if (!g) groups.set(key, { time: t, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume })
    else { g.high = Math.max(g.high, c.high); g.low = Math.min(g.low, c.low); g.close = c.close; g.volume += c.volume }
  }
  return Array.from(groups.values()).sort((a, b) => a.time - b.time)
}

function calcRSI(candles: CandleBar[], period = 14): { time: number; value: number }[] {
  if (candles.length < period + 1) return []
  const results: { time: number; value: number }[] = []
  let avgGain = 0
  let avgLoss = 0
  for (let i = 1; i <= period; i++) {
    const diff = candles[i].close - candles[i - 1].close
    if (diff > 0) avgGain += diff
    else avgLoss += Math.abs(diff)
  }
  avgGain /= period
  avgLoss /= period
  results.push({ time: candles[period].time, value: 100 - 100 / (1 + avgGain / (avgLoss || 0.001)) })
  for (let i = period + 1; i < candles.length; i++) {
    const diff = candles[i].close - candles[i - 1].close
    avgGain = (avgGain * (period - 1) + (diff > 0 ? diff : 0)) / period
    avgLoss = (avgLoss * (period - 1) + (diff < 0 ? Math.abs(diff) : 0)) / period
    results.push({ time: candles[i].time, value: 100 - 100 / (1 + avgGain / (avgLoss || 0.001)) })
  }
  return results
}

interface TradeMark {
  type: 'buy' | 'sell'
  timestamp: number
}

interface StockChartProps {
  symbol: string
  candles?: CandleBar[]
  cutoffDate?: number
  trades?: TradeMark[]
  startDate?: number
  changePct?: number | null
  isFullscreen?: boolean
  onToggleFullscreen?: () => void
  showSidePanel?: boolean
  onToggleSidePanel?: () => void
  compactHeight?: number
  hideControls?: boolean
}

const TF_OPTS: { label: string; value: TF }[] = [
  { label: '일봉', value: 'D' },
  { label: '주봉', value: 'W' },
  { label: '월봉', value: 'M' },
]

export default function StockChart({ symbol, candles: externalCandles, cutoffDate, trades, startDate, changePct, isFullscreen = false, onToggleFullscreen, showSidePanel, onToggleSidePanel, compactHeight, hideControls = false }: StockChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const rsiContainerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const rsiChartRef = useRef<IChartApi | null>(null)
  const rsiSeriesRef = useRef<ISeriesApi<SeriesType> | null>(null)
  const seriesRef = useRef<ISeriesApi<SeriesType> | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<SeriesType> | null>(null)
  const maSeriesRefs = useRef<ISeriesApi<SeriesType>[]>([])
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const initialFitDoneRef = useRef(false)
  const isSyncingRef = useRef(false)
  const [showRsi, setShowRsi] = useState(false)
  const [rsiValue, setRsiValue] = useState<number | null>(null)
  const [tf, setTf] = useState<TF>('D')
  const allBarsRef = useRef<CandleBar[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [chartHeight, setChartHeight] = useState(() =>
    compactHeight ?? Math.max(420, Math.min(720, window.innerHeight - 360))
  )
  const [volumeRatio, setVolumeRatio] = useState(0.25)
  const isHistoryMode = externalCandles !== undefined

  const calcFullscreenHeight = useCallback((rsiVisible: boolean) => {
    return window.innerHeight - 48 - (rsiVisible ? 152 : 0)
  }, [])

  useEffect(() => {
    const updateHeight = () => {
      if (compactHeight) {
        setChartHeight(compactHeight)
      } else if (isFullscreen) {
        setChartHeight(calcFullscreenHeight(showRsi))
      } else {
        setChartHeight(Math.max(420, Math.min(720, window.innerHeight - 360)))
      }
    }
    updateHeight()
    window.addEventListener('resize', updateHeight)
    return () => window.removeEventListener('resize', updateHeight)
  }, [isFullscreen, showRsi, calcFullscreenHeight, compactHeight])

  useEffect(() => {
    chartRef.current?.applyOptions({ height: chartHeight })
  }, [chartHeight])

  useEffect(() => {
    requestAnimationFrame(() => {
      if (rsiContainerRef.current && rsiChartRef.current) {
        rsiChartRef.current.applyOptions({ width: rsiContainerRef.current.clientWidth })
      }
    })
  }, [showRsi])

  useEffect(() => {
    seriesRef.current?.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: volumeRatio } })
    volumeSeriesRef.current?.priceScale().applyOptions({ scaleMargins: { top: 1 - volumeRatio, bottom: 0 } })
    maSeriesRefs.current.forEach((s) => s.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: volumeRatio } }))
  }, [volumeRatio])

  useEffect(() => {
    if (!containerRef.current) return

    chartRef.current = createChart(containerRef.current, {
      layout: {
        background: { color: '#111827' },
        textColor: '#9CA3AF',
      },
      grid: {
        vertLines: { color: '#1F2937' },
        horzLines: { color: '#1F2937' },
      },
      crosshair: {
        vertLine: { color: '#6366F1' },
        horzLine: { color: '#6366F1' },
      },
      rightPriceScale: {
        borderColor: '#374151',
      },
      timeScale: {
        borderColor: '#374151',
        timeVisible: true,
        tickMarkFormatter: (time: number) => {
          const d = new Date(time * 1000)
          const y = d.getUTCFullYear()
          const m = String(d.getUTCMonth() + 1).padStart(2, '0')
          const day = String(d.getUTCDate()).padStart(2, '0')
          return `${y}.${m}.${day}`
        },
      },
      localization: {
        timeFormatter: (time: number) => {
          const d = new Date(time * 1000)
          const y = d.getUTCFullYear()
          const m = String(d.getUTCMonth() + 1).padStart(2, '0')
          const day = String(d.getUTCDate()).padStart(2, '0')
          return `${y}.${m}.${day}`
        },
      },
      width: containerRef.current.clientWidth,
      height: chartHeight,
    })

    const resizeObserver = new ResizeObserver(() => {
      if (containerRef.current && chartRef.current)
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth })
      if (rsiContainerRef.current && rsiChartRef.current)
        rsiChartRef.current.applyOptions({ width: rsiContainerRef.current.clientWidth })
    })

    resizeObserver.observe(containerRef.current)

    // RSI 차트
    if (rsiContainerRef.current) {
      rsiChartRef.current = createChart(rsiContainerRef.current, {
        layout: { background: { color: '#111827' }, textColor: '#9CA3AF' },
        grid: { vertLines: { color: '#1F2937' }, horzLines: { color: '#1F2937' } },
        crosshair: { vertLine: { color: '#6366F1' }, horzLine: { color: '#6366F1' } },
        rightPriceScale: { borderColor: '#374151', scaleMargins: { top: 0.1, bottom: 0.1 } },
        timeScale: { borderColor: '#374151', timeVisible: true, visible: false },
        width: rsiContainerRef.current.clientWidth,
        height: 120,
        handleScroll: false,
        handleScale: false,
      })
      rsiSeriesRef.current = rsiChartRef.current.addSeries(LineSeries, {
        color: '#818CF8',
        lineWidth: 2,
        lastValueVisible: false,
        priceLineVisible: false,
      })
      resizeObserver.observe(rsiContainerRef.current)
    }

    // 시간축 동기화 (메인 ↔ RSI)
    chartRef.current.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (isSyncingRef.current || !range) return
      isSyncingRef.current = true
      rsiChartRef.current?.timeScale().setVisibleLogicalRange(range)
      isSyncingRef.current = false
    })
    rsiChartRef.current?.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (isSyncingRef.current || !range) return
      isSyncingRef.current = true
      chartRef.current?.timeScale().setVisibleLogicalRange(range)
      isSyncingRef.current = false
    })

    // 크로스헤어 동기화 + RSI 값 표시
    chartRef.current.subscribeCrosshairMove((param) => {
      if (!rsiSeriesRef.current || !rsiChartRef.current) return
      if (param.time) {
        const rsiData = param.seriesData.get(rsiSeriesRef.current as ISeriesApi<SeriesType>)
        if (rsiData && 'value' in rsiData) setRsiValue(Math.round((rsiData as { value: number }).value))
        try { rsiChartRef.current.setCrosshairPosition(0, param.time, rsiSeriesRef.current) } catch { /* 데이터 없는 시점 무시 */ }
      } else {
        setRsiValue(null)
        rsiChartRef.current.clearCrosshairPosition()
      }
    })

    return () => {
      resizeObserver.disconnect()
      chartRef.current?.remove()
      rsiChartRef.current?.remove()
      chartRef.current = null
      rsiChartRef.current = null
      rsiSeriesRef.current = null
      seriesRef.current = null
      volumeSeriesRef.current = null
      maSeriesRefs.current = []
      markersPluginRef.current = null
      initialFitDoneRef.current = false
    }
  }, [])

  const MA_CONFIGS = [
    { period: 5,  color: '#FBBF24' },
    { period: 20, color: '#A78BFA' },
    { period: 60, color: '#34D399' },
  ]

  const calcMA = (bars: CandleBar[], period: number) => {
    return bars.map((b, i) => {
      if (i < period - 1) return null
      const avg = bars.slice(i - period + 1, i + 1).reduce((s, c) => s + c.close, 0) / period
      return { time: b.time as unknown as import('lightweight-charts').Time, value: avg }
    }).filter((v): v is { time: import('lightweight-charts').Time; value: number } => v !== null)
  }

  const ensureSeries = () => {
    if (!chartRef.current || seriesRef.current) return

    const series = chartRef.current.addSeries(CandlestickSeries, {
      upColor: '#EF4444', downColor: '#3B82F6',
      borderUpColor: '#EF4444', borderDownColor: '#3B82F6',
      wickUpColor: '#EF4444', wickDownColor: '#3B82F6',
      priceScaleId: 'right',
    })
    series.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: volumeRatio } })
    seriesRef.current = series

    const volumeSeries = chartRef.current.addSeries(HistogramSeries, {
      priceScaleId: 'volume',
      priceFormat: { type: 'volume' },
    })
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 1 - volumeRatio, bottom: 0 } })
    volumeSeriesRef.current = volumeSeries

    maSeriesRefs.current = MA_CONFIGS.map(({ color }) => {
      const s = chartRef.current!.addSeries(LineSeries, {
        color, lineWidth: 1, priceScaleId: 'right',
        crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
      })
      s.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: volumeRatio } })
      return s
    })
  }

  const applyBars = (bars: CandleBar[], overrideTf?: TF) => {
    if (!chartRef.current) return
    ensureSeries()
    if (!seriesRef.current || !volumeSeriesRef.current) return

    allBarsRef.current = bars  // 원본 보존 (tf 변경 시 재집계용)

    // 사용자가 이미 차트를 조작했으면 현재 뷰를 저장해서 복원
    const savedRange = initialFitDoneRef.current
      ? chartRef.current.timeScale().getVisibleLogicalRange()
      : null

    const cutoffSec = cutoffDate != null ? Math.floor(cutoffDate / 1000) : Infinity
    const daily = sanitizeCandles(bars.filter((b) => b.time <= cutoffSec))
    const filtered = aggregateCandles(daily, overrideTf ?? tf)

    const formatted = filtered.map((b) => ({
      time: b.time as unknown as import('lightweight-charts').Time,
      open: b.open, high: b.high, low: b.low, close: b.close,
    }))
    const volumeFormatted = filtered.map((b) => ({
      time: b.time as unknown as import('lightweight-charts').Time,
      value: b.volume,
      color: b.close >= b.open ? '#EF444466' : '#3B82F666',
    }))

    seriesRef.current.setData(formatted)
    volumeSeriesRef.current.setData(volumeFormatted)
    maSeriesRefs.current.forEach((s, i) => s.setData(calcMA(filtered, MA_CONFIGS[i].period)))

    if (rsiSeriesRef.current) {
      const rsiData = calcRSI(filtered).map((d) => ({
        time: d.time as unknown as import('lightweight-charts').Time,
        value: d.value,
      }))
      rsiSeriesRef.current.setData(rsiData)
    }

    if (isHistoryMode && formatted.length > 0) {
      type Marker = SeriesMarker<Time>
      const markers: Marker[] = []

      if (startDate != null) {
        const startSec = Math.floor(startDate / 1000)
        const startCandle = filtered.reduce((best, c) =>
          Math.abs(c.time - startSec) < Math.abs(best.time - startSec) ? c : best
        , filtered[0])
        if (startCandle) {
          markers.push({
            time: startCandle.time as unknown as import('lightweight-charts').Time,
            position: 'belowBar',
            color: '#818CF8',
            shape: 'arrowUp',
            text: '시작',
          })
        }
      }

      for (const trade of trades ?? []) {
        const tradeSec = Math.floor(trade.timestamp / 1000)
        const candle = filtered.reduce((best, c) =>
          Math.abs(c.time - tradeSec) < Math.abs(best.time - tradeSec) ? c : best
        , filtered[0])
        if (!candle) continue
        markers.push({
          time: candle.time as unknown as import('lightweight-charts').Time,
          position: trade.type === 'buy' ? 'belowBar' : 'aboveBar',
          color: trade.type === 'buy' ? '#22C55E' : '#F87171',
          shape: trade.type === 'buy' ? 'arrowUp' : 'arrowDown',
          text: trade.type === 'buy' ? '매수' : '매도',
        })
      }
      markers.push({
        time: formatted[formatted.length - 1].time,
        position: 'aboveBar', color: '#F59E0B', shape: 'circle', text: '현재',
      })
      markers.sort((a, b) => (a.time as number) - (b.time as number))
      if (markersPluginRef.current) {
        markersPluginRef.current.setMarkers(markers)
      } else {
        markersPluginRef.current = createSeriesMarkers(seriesRef.current as never, markers) as ISeriesMarkersPluginApi<Time>
      }
    }

    if (!initialFitDoneRef.current) {
      chartRef.current.timeScale().fitContent()
      initialFitDoneRef.current = true
    } else if (savedRange) {
      chartRef.current.timeScale().setVisibleLogicalRange(savedRange)
    }
  }

  // 외부 캔들이 주어지면 그것을 사용 (역사 시뮬레이션 모드)
  useEffect(() => {
    if (externalCandles === undefined) return
    setError(null)
    setLoading(false)
    applyBars(externalCandles)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [externalCandles, cutoffDate, trades, tf])

  // tf 변경 시 재집계 (fetch 모드 — 원본 데이터 재사용)
  useEffect(() => {
    if (externalCandles !== undefined) return  // history 모드는 위 effect가 처리
    if (allBarsRef.current.length === 0) return
    initialFitDoneRef.current = false  // tf 바뀌면 전체 기간으로 재fit
    applyBars(allBarsRef.current, tf)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tf])

  // 내부 fetch 모드 (실시간) — 항상 2년치 데이터 fetch
  useEffect(() => {
    if (externalCandles !== undefined) return
    if (!chartRef.current) return
    let cancelled = false

    const load = async () => {
      setLoading(true)
      setError(null)
      try {
        const bars: CandleBar[] = await fetchCandles(symbol, '3y')
        if (cancelled) return
        initialFitDoneRef.current = false
        applyBars(bars)
      } catch (e) {
        if (!cancelled) setError('차트 데이터를 불러오지 못했습니다.')
        console.error(e)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    return () => { cancelled = true }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, externalCandles])

  return (
    <div className={isFullscreen
      ? 'flex flex-col h-full bg-gray-900'
      : 'bg-gray-900 rounded-xl border border-gray-800 overflow-hidden'
    }>
      <div className={`flex items-center justify-between border-b border-gray-800 ${hideControls ? 'px-3 py-1.5' : 'px-4 py-3'}`}>
        <div className="flex items-center gap-3">
          {!hideControls && <span className="text-sm text-gray-400 font-medium">주가 차트</span>}
          {changePct != null && (
            <span className={`${hideControls ? 'text-xs' : 'text-sm'} font-bold tabular-nums ${changePct >= 0 ? 'text-red-400' : 'text-blue-400'}`}>
              {changePct >= 0 ? '▲' : '▼'} {Math.abs(changePct).toFixed(2)}%
            </span>
          )}
          <div className="flex items-center gap-2 text-xs text-gray-400">
            <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5 bg-[#FBBF24]" />MA5</span>
            <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5 bg-[#A78BFA]" />MA20</span>
            <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5 bg-[#34D399]" />MA60</span>
          </div>
          {!hideControls && (
            <button
              onClick={() => setShowRsi((v) => !v)}
              className={`text-xs px-2 py-0.5 rounded border transition-colors border-l border-gray-700 ml-1 ${showRsi ? 'bg-indigo-600/30 border-indigo-500/40 text-indigo-300' : 'border-gray-700 text-gray-500 hover:text-gray-300'}`}
            >
              RSI
            </button>
          )}
          {!hideControls && (
            <div className="flex items-center gap-1.5 text-xs text-gray-400 border-l border-gray-700 pl-3">
              <span>거래량</span>
              <input
                type="range"
                min={10}
                max={45}
                value={Math.round(volumeRatio * 100)}
                onChange={(e) => setVolumeRatio(Number(e.target.value) / 100)}
                className="w-16 h-1 accent-indigo-500 cursor-pointer"
              />
              <span className="w-6 text-right">{Math.round(volumeRatio * 100)}%</span>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <div className="flex gap-1">
            {TF_OPTS.map((t) => (
              <button
                key={t.value}
                onClick={() => setTf(t.value)}
                className={`px-2.5 py-1 text-xs rounded-lg transition-colors ${
                  tf === t.value
                    ? 'bg-indigo-600 text-white'
                    : 'text-gray-400 hover:text-white hover:bg-gray-800'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          {isFullscreen && onToggleSidePanel && (
            <button
              onClick={onToggleSidePanel}
              title={showSidePanel ? '패널 숨기기' : '패널 보이기'}
              className="p-1.5 text-gray-400 hover:text-white hover:bg-gray-700 rounded-lg transition-colors"
            >
              <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                {showSidePanel
                  ? <path d="M19 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2zm-7 0v18" />
                  : <path d="M19 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2z" />
                }
              </svg>
            </button>
          )}
          {onToggleFullscreen && <button
            onClick={onToggleFullscreen}
            title={isFullscreen ? '축소 (ESC)' : '전체화면'}
            className="p-1.5 text-gray-400 hover:text-white hover:bg-gray-700 rounded-lg transition-colors"
          >
            {isFullscreen ? (
              <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/>
              </svg>
            ) : (
              <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
              </svg>
            )}
          </button>}
        </div>
      </div>
      <div className="relative">
        <div ref={containerRef} />
        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-gray-900/80">
            <span className="text-gray-400 text-sm">로딩 중...</span>
          </div>
        )}
        {error && (
          <div className="absolute inset-0 flex items-center justify-center bg-gray-900/80">
            <span className="text-red-400 text-sm">{error}</span>
          </div>
        )}
      </div>

      <div className={showRsi ? 'border-t border-gray-800' : 'overflow-hidden h-0'}>
        <div className="flex items-center gap-3 px-4 py-1.5 text-xs text-gray-500">
          <span className="text-indigo-400 font-medium">RSI(14)</span>
          {rsiValue != null && (
            <span className={`font-bold ${rsiValue >= 70 ? 'text-red-400' : rsiValue <= 30 ? 'text-blue-400' : 'text-indigo-300'}`}>
              {rsiValue}
              {rsiValue >= 70 ? ' 과매수' : rsiValue <= 30 ? ' 과매도' : ''}
            </span>
          )}
          <span className="ml-auto text-gray-600">70 과매수 · 30 과매도</span>
        </div>
        <div className="relative">
          <div ref={rsiContainerRef} />
          <div className="absolute inset-0 pointer-events-none">
            <div className="absolute w-full border-t border-red-500/20" style={{ top: '10%' }} />
            <div className="absolute w-full border-t border-blue-500/20" style={{ top: '90%' }} />
          </div>
        </div>
      </div>
    </div>
  )
}
