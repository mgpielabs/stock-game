import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  screenerApi, aiApi, updateApi, paperTradingApi, getWatchlist, toggleWatchlist,
  type ScreenerStock, type ScreenerRegime, type ScreenerFilters,
  type TickerDetail, type UpdateStatus,
  type StockSearchResult, type SymbolProfile, type FilterFlags,
  type CrossAnalysisResponse,
  type SectorFlowResponse,
  type SectorFlowStockItem,
  type StockChartPoint,
  type ActiveTrade,
} from '../api/aiRecommend'
import { DetailModal, BadgeTooltip } from './AIRecommendPage'
import {
  createChart, CandlestickSeries, HistogramSeries, LineSeries,
  type IChartApi, type Time,
} from 'lightweight-charts'
import {
  LineChart, Line, XAxis, YAxis, Tooltip as RechartTooltip,
  Legend, ResponsiveContainer, CartesianGrid,
} from 'recharts'

// ── 필터 정의 — backend/ml/factor_screen_validation.py 워크포워드 검증 결과 그대로 반영 ──

export type Badge = 'verified' | 'conditional' | 'unverified' | 'model_60d' | 'utility'

export interface FilterDef {
  key: keyof ScreenerFilters
  label: string
  badge: Badge
  tooltip: string
}

export const FILTERS: FilterDef[] = [
  {
    key: 'high_dividend', label: '고배당 상위 20%', badge: 'conditional',
    tooltip: 'DPS÷실제종가로 직접 재계산(DART 공시 자체의 액면가 오분모 오류 수정). 표본 213→2,699종목 재검증 결과 전체평균 무의미 — 약세장 65.8%, 강세장 48.7%(역효과). 저PBR과 조합하면 단독보다 유의하게 나음(아래 추천 조합 참고).',
  },
  {
    key: 'rsi_oversold', label: 'RSI 과매도 (<30)', badge: 'conditional',
    tooltip: '평균회귀 신호 — 약세장 승률 80%, 횡보장 60%. 강세장에서는 37%로 역효과. 시장 국면 배너를 확인하세요.',
  },
  {
    key: 'bb_lower', label: '볼린저 밴드 하단 터치', badge: 'conditional',
    tooltip: '평균회귀 신호 — 약세장 승률 77%, 횡보장 62%. 강세장에서는 41%로 역효과.',
  },
  {
    key: 'low_per', label: '저PER 하위 20%', badge: 'conditional',
    tooltip: '재검증(2026-06-23) 결과 전체평균 무의미(OOS 49~51%, 이전 56~62%는 소표본 과대추정). 약세장 58.2%, 강세장 46.8%(역효과). PER은 EPS 역산 PIT값(per_pit).',
  },
  {
    key: 'low_pbr', label: '저PBR 하위 20%', badge: 'conditional',
    tooltip: '재검증(2026-06-22) 결과 전체평균 무의미. 약세장 60.7%, 강세장 47.0%(역효과). 고배당과 조합하면 단독보다 유의하게 나음(아래 추천 조합).',
  },
  {
    key: 'div_growth', label: '배당 성장 상위 25%', badge: 'conditional',
    tooltip: 'YoY DPS 성장률 상위 25%. 350만행 OOS 60d +3.79%p(p=0.000). 변동성 피처와 r<0.12 독립. 커버리지 ~40%(배당 없는 종목 미표시).',
  },
  {
    key: 'eps_growth_top', label: 'EPS 성장 상위 20%', badge: 'model_60d',
    tooltip: '60d 모델 검증 팩터 (IS/OOS 생존) — EPS YoY 성장률 상위 20%. OOS 60d +1.47%p(p=0.000). 공시지연 3개월 반영 PIT 계산.',
  },
  {
    key: 'eps_accel', label: 'EPS 성장 가속', badge: 'model_60d',
    tooltip: '60d 모델 검증 팩터 (IS/OOS 생존) — 전년 대비 EPS 성장이 가속 중인 종목. OOS 60d +2.16%p(p=0.000).',
  },
  {
    key: 'roe_top', label: 'ROE 상위 20%', badge: 'model_60d',
    tooltip: '60d 모델 검증 팩터 (IS/OOS 생존) — ROE(EPS÷BPS) 상위 20%. OOS 60d +2.52%p(p=0.000).',
  },
  {
    key: 'bps_growth_top', label: 'BPS 성장 상위 20%', badge: 'model_60d',
    tooltip: '60d 모델 포함 팩터 (SHAP 1.55%) — BPS YoY 성장률 상위 20%. 커버리지 ~39%.',
  },
  {
    key: 'score_60d_top20', label: '60d 모델 추천 상위 20%', badge: 'model_60d',
    tooltip: '60d 중기 예측 모델 점수 상위 20% (전체 약 2,800종목 기준). 첫 호출 시 추론에 수 초 소요.',
  },
  {
    key: 'score_60d_top10', label: '60d 모델 추천 상위 10%', badge: 'model_60d',
    tooltip: '60d 중기 예측 모델 점수 상위 10%. 첫 호출 시 추론에 수 초 소요.',
  },
  {
    key: 'min_vol20d', label: '거래대금 20일 10억+', badge: 'utility',
    tooltip: '20일 평균 거래대금 10억 이상 — 유동성 필터. 운용 규칙과 동일한 기준 (MIN_VOLUME_KRW=10억).',
  },
  {
    key: 'exclude_high_atr', label: '극단 변동성 제외', badge: 'utility',
    tooltip: 'ATR(평균진폭) 상위 10% 종목 제외 — ⚡ 변동성↑ 배지와 동일 계열 신호. IS/OOS p=0.0000 (고변동성 = 이후 수익 하락 패턴).',
  },
]

// ── 추천 조합 프리셋 — 검증된 필터 조합을 동시에 켬 (combo_validation.py / combo_60d_score_test.py 검증 통과) ──

export interface ComboPreset {
  label: string
  keys: (keyof ScreenerFilters)[]
  oosRate: string
  tooltip: string
}

export const COMBO_PRESETS: ComboPreset[] = [
  {
    label: '고배당 + 저PBR + 60d상위20% (3조건)',
    keys: ['high_dividend', 'low_pbr', 'score_60d_top20'],
    oosRate: '76.1%',
    tooltip: '36+7=43조합 검증(2026-07-31) — 60d 모델 스코어 상위 20%를 추가한 3-필터 조합. OOS 초과승률 76.1%(기준 고배당+저PBR 60.5% 대비 +15.6%p, p<0.001). IS 66.7% / OOS 76.1% 방향 일치. Jaccard 0.43(기존 조합과 독립). 60d 스코어 단독은 OOS 비유의 — 기존 필터와 결합 시에만 유효.',
  },
  {
    label: '고배당 + 저PER + 60d상위10% (3조건)',
    keys: ['high_dividend', 'low_per', 'score_60d_top10'],
    oosRate: '75.7%',
    tooltip: '36+7=43조합 검증(2026-07-31) — 60d 모델 스코어 상위 10%를 추가한 3-필터 조합. OOS 초과승률 75.7%(기준 고배당+저PER 66.5% 대비 +8.5%p, p<0.001). IS 69.6% / OOS 75.7% 방향 일치. Jaccard 0.41(기존 조합과 독립). 60d 스코어 단독은 OOS 비유의 — 기존 필터와 결합 시에만 유효.',
  },
  {
    label: '고배당 + 저PER',
    keys: ['high_dividend', 'low_per'],
    oosRate: '71.9%',
    tooltip: '36+7=43조합 검증(2026-07-14) — 초과승률 71.9%(단독 최고 대비 +9.2%p, p<0.001) + 절대 20d 수익률도 개선(OOS +4.18% vs 단독 최고 +2.05%, p<0.001). IS/OOS 방향 일치. Jaccard <0.7(다른 조합과 중복 없음). 36개 중 Δ 최고.',
  },
  {
    label: '고배당 + 저PBR',
    keys: ['high_dividend', 'low_pbr'],
    oosRate: '68.8%',
    tooltip: '고배당을 DPS÷실제종가로 재계산한 기준으로 최종 재검증(2026-06-23)해도 20일 기준 단독보다 유의하게 나음(OOS p=0.0001). 약세장 승률 71.3%. 2026-07-14 전수 36+7=43조합 검증에서도 재확인(OOS 초과승률 68.8% +5.1%p, 절대수익 +3.25% vs +2.37%, p<0.001).',
  },
  {
    label: '고배당 + 배당성장',
    keys: ['high_dividend', 'div_growth'],
    oosRate: '67.5%',
    tooltip: '36+7=43조합 검증(2026-07-14) — 초과승률 67.5%(단독 최고 대비 +5.2%p, p<0.001) + 절대 20d 수익률도 개선(OOS +3.06% vs 단독 최고 +2.25%, p<0.001). IS/OOS 방향 일치. n=11,577.',
  },
  {
    label: '저PBR + 배당성장',
    keys: ['low_pbr', 'div_growth'],
    oosRate: '63.9%',
    tooltip: '36+7=43조합 검증(2026-07-14) — 초과승률 63.9%(단독 최고 대비 +6.1%p, p<0.001) + 절대 20d 수익률도 개선(OOS +2.71% vs 단독 최고 +2.36%, p<0.001). IS/OOS 방향 일치. n=11,737.',
  },
]

export const BADGE_STYLE: Record<Badge, { icon: string; cls: string; label: string }> = {
  verified:    { icon: '✅', cls: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30', label: '검증됨' },
  conditional: { icon: '⚠️', cls: 'bg-yellow-500/15 text-yellow-400 border-yellow-500/30', label: '시장국면 조건부' },
  unverified:  { icon: '❓', cls: 'bg-gray-700/40 text-gray-400 border-gray-600/40', label: '미검증' },
  model_60d:   { icon: '📈', cls: 'bg-teal-500/15 text-teal-400 border-teal-500/30', label: '60d 검증' },
  utility:     { icon: '🔧', cls: 'bg-slate-600/30 text-slate-400 border-slate-500/30', label: '품질/유동성' },
}

function FilterBadge({ badge }: { badge: Badge }) {
  const s = BADGE_STYLE[badge]
  return (
    <span className={`text-xs px-1.5 py-0.5 rounded border whitespace-nowrap ${s.cls}`}>
      {s.icon} {s.label}
    </span>
  )
}

// ── 종목 검색창 ──────────────────────────────────────────────

export function StockSearchBox({
  onSelect,
}: {
  onSelect: (result: StockSearchResult) => void
}) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<StockSearchResult[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current)
    if (!query.trim()) { setResults([]); setOpen(false); return }
    timerRef.current = setTimeout(async () => {
      setLoading(true)
      try {
        const res = await screenerApi.stocksSearch(query.trim())
        setResults(res)
        setOpen(true)
      } catch { setResults([]) }
      finally { setLoading(false) }
    }, 250)
  }, [query])

  // 외부 클릭 시 드롭다운 닫기
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const handleSelect = (r: StockSearchResult) => {
    setQuery('')
    setOpen(false)
    setResults([])
    onSelect(r)
  }

  return (
    <div ref={containerRef} className="relative">
      <div className="relative">
        <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
        <input
          type="text"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onFocus={() => { if (results.length > 0) setOpen(true) }}
          placeholder="종목명 또는 코드 검색 (예: 삼성전자, 005930)"
          className="w-full bg-gray-800 border border-gray-700 rounded-xl pl-9 pr-4 py-2.5 text-sm text-gray-200 placeholder-gray-500 focus:outline-none focus:border-sky-500/60 focus:bg-gray-800/80 transition-colors"
        />
        {loading && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 text-xs">검색 중...</span>
        )}
      </div>

      {open && results.length > 0 && (
        <div className="absolute top-full mt-1 left-0 right-0 z-50 bg-gray-900 border border-gray-700 rounded-xl shadow-xl overflow-hidden max-h-64 overflow-y-auto">
          {results.map(r => (
            <button
              key={r.symbol}
              onClick={() => handleSelect(r)}
              className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-gray-800 transition-colors text-left"
            >
              <span className="font-mono text-xs text-sky-400 w-14 shrink-0">{r.symbol}</span>
              <span className="text-gray-200 text-sm flex-1 truncate">{r.name}</span>
              <span className="text-gray-500 text-xs shrink-0">{r.market}</span>
            </button>
          ))}
        </div>
      )}

      {open && !loading && query.trim() && results.length === 0 && (
        <div className="absolute top-full mt-1 left-0 right-0 z-50 bg-gray-900 border border-gray-700 rounded-xl shadow-xl px-4 py-3">
          <p className="text-gray-500 text-sm">종목을 찾을 수 없습니다</p>
        </div>
      )}
    </div>
  )
}

// ── 종목 프로파일 카드 ────────────────────────────────────────

// boolean 필터 키만 허용 (sort_by/sort_dir/limit 제외)
export type BooleanFilterKey = Exclude<keyof ScreenerFilters, 'sort_by' | 'sort_dir' | 'limit'>

export const FLAG_FILTER_MAP: { key: keyof FilterFlags; filterKey: BooleanFilterKey; label: string; badge: Badge }[] = [
  { key: 'high_dividend',   filterKey: 'high_dividend',   label: '고배당 상위 20%',    badge: 'conditional' },
  { key: 'rsi_oversold',    filterKey: 'rsi_oversold',    label: 'RSI 과매도 (<30)',   badge: 'conditional' },
  { key: 'bb_lower',        filterKey: 'bb_lower',        label: '볼린저 밴드 하단',   badge: 'conditional' },
  { key: 'low_per',         filterKey: 'low_per',         label: '저PER 하위 20%',     badge: 'conditional' },
  { key: 'low_pbr',         filterKey: 'low_pbr',         label: '저PBR 하위 20%',     badge: 'conditional' },
  { key: 'div_growth',      filterKey: 'div_growth',      label: '배당 성장 상위 25%', badge: 'conditional' },
  { key: 'eps_growth_top',  filterKey: 'eps_growth_top',  label: 'EPS 성장 상위 20%', badge: 'model_60d' },
  { key: 'eps_accel',       filterKey: 'eps_accel',       label: 'EPS 성장 가속',      badge: 'model_60d' },
  { key: 'roe_top',         filterKey: 'roe_top',         label: 'ROE 상위 20%',       badge: 'model_60d' },
  { key: 'bps_growth_top',  filterKey: 'bps_growth_top',  label: 'BPS 성장 상위 20%', badge: 'model_60d' },
  { key: 'score_60d_top10', filterKey: 'score_60d_top10', label: '60d 모델 상위 10%', badge: 'model_60d' },
  { key: 'score_60d_top20', filterKey: 'score_60d_top20', label: '60d 모델 상위 20%', badge: 'model_60d' },
  { key: 'min_vol20d',      filterKey: 'min_vol20d',      label: '거래대금 20일 10억+', badge: 'utility' },
  { key: 'exclude_high_atr', filterKey: 'exclude_high_atr', label: '극단 변동성 제외', badge: 'utility' },
]

export type ChartPeriod = '60d' | '120d' | '1y' | '2y' | '3y' | 'all'


export function hasInvestorData(data: StockChartPoint[]): boolean {
  return data.some(d => d.foreign_net != null || d.inst_net != null || d.indiv_net != null)
}

const LW_COLORS = {
  up: '#ef4444', down: '#3b82f6',
  ma5: '#fbbf24', ma20: '#a78bfa', ma60: '#34d399',
  foreign: '#3b82f6', inst: '#f97316', indiv: '#9ca3af',
}

function _toTime(yyyymmdd: string): Time {
  return `${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6, 8)}` as Time
}

function _computeMA(closes: number[], n: number): (number | null)[] {
  return closes.map((_, i) => {
    if (i < n - 1) return null
    let sum = 0
    for (let k = i - n + 1; k <= i; k++) sum += closes[k]
    return sum / n
  })
}

function _periodDays(p: ChartPeriod): number {
  if (p === '60d')  return 60
  if (p === '120d') return 120
  if (p === '2y')   return 504
  if (p === '3y')   return 756
  if (p === 'all')  return 9999
  return 252 // '1y'
}

interface ChartRefs {
  priceChart: IChartApi
  investChart: IChartApi | null
}

function _buildCharts(
  priceEl: HTMLElement,
  investEl: HTMLElement | null,
  data: StockChartPoint[],
  hasInvestor: boolean,
  darkBg = '#111827',
): ChartRefs {
  const gridColor = '#1f2937'
  const textColor = '#6b7280'
  const baseOpts = {
    layout: { background: { color: darkBg }, textColor },
    grid: { vertLines: { color: gridColor }, horzLines: { color: gridColor } },
    crosshair: { mode: 1 },
    timeScale: { borderColor: gridColor, timeVisible: false },
    rightPriceScale: { borderColor: gridColor },
    autoSize: true,
  }

  // ── 가격 + 거래량 차트 ──
  const priceChart = createChart(priceEl, baseOpts)

  const candleSeries = priceChart.addSeries(CandlestickSeries, {
    upColor: LW_COLORS.up, downColor: LW_COLORS.down,
    borderUpColor: LW_COLORS.up, borderDownColor: LW_COLORS.down,
    wickUpColor: LW_COLORS.up, wickDownColor: LW_COLORS.down,
  })
  candleSeries.setData(
    data.map(d => ({
      time: _toTime(d.date),
      open:  d.open  ?? d.close,
      high:  d.high  ?? d.close,
      low:   d.low   ?? d.close,
      close: d.close,
    }))
  )

  const closes = data.map(d => d.close)
  const times = data.map(d => _toTime(d.date))
  const maDefs = [
    { n: 5,  color: LW_COLORS.ma5  },
    { n: 20, color: LW_COLORS.ma20 },
    { n: 60, color: LW_COLORS.ma60 },
  ]
  for (const { n, color } of maDefs) {
    const maSeries = priceChart.addSeries(LineSeries, {
      color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    })
    const maVals = _computeMA(closes, n)
    maSeries.setData(
      maVals.map((v, i) => v != null ? { time: times[i], value: v } : null)
            .filter((x): x is { time: Time; value: number } => x != null)
    )
  }

  // 거래량: 같은 priceChart에 별도 pricescale로 하단 20%
  const volSeries = priceChart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
  })
  priceChart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.80, bottom: 0 } })
  let prevClose = data[0]?.close ?? 0
  volSeries.setData(
    data.map(d => {
      const color = d.close >= prevClose ? LW_COLORS.up + '99' : LW_COLORS.down + '99'
      prevClose = d.close
      return { time: _toTime(d.date), value: d.volume ?? 0, color }
    })
  )

  // ── 투자자 수급 차트 ──
  let investChart: IChartApi | null = null
  if (hasInvestor && investEl) {
    investChart = createChart(investEl, {
      ...baseOpts,
      rightPriceScale: { borderColor: gridColor, scaleMargins: { top: 0.05, bottom: 0.05 } },
    })

    const foreignSeries = investChart.addSeries(HistogramSeries, {
      color: LW_COLORS.foreign + 'cc', priceLineVisible: false, lastValueVisible: false,
    })
    foreignSeries.setData(
      data.filter(d => d.foreign_net != null)
          .map(d => ({
            time: _toTime(d.date),
            value: d.foreign_net!,
            color: d.foreign_net! >= 0 ? LW_COLORS.foreign + 'cc' : LW_COLORS.foreign + '55',
          }))
    )

    const instSeries = investChart.addSeries(LineSeries, {
      color: LW_COLORS.inst, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    })
    instSeries.setData(
      data.filter(d => d.inst_net != null)
          .map(d => ({ time: _toTime(d.date), value: d.inst_net! }))
    )

    const indivSeries = investChart.addSeries(LineSeries, {
      color: LW_COLORS.indiv, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
    })
    indivSeries.setData(
      data.filter(d => d.indiv_net != null)
          .map(d => ({ time: _toTime(d.date), value: d.indiv_net! }))
    )

    // X축 동기화
    let _syncing = false
    priceChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (_syncing || !investChart) return
      _syncing = true
      investChart.timeScale().setVisibleLogicalRange(range!)
      _syncing = false
    })
    investChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (_syncing) return
      _syncing = true
      priceChart.timeScale().setVisibleLogicalRange(range!)
      _syncing = false
    })
  }

  return { priceChart, investChart }
}

function _applyPeriodRange(chart: IChartApi, data: StockChartPoint[], period: ChartPeriod) {
  const days = _periodDays(period)
  const n = Math.min(days, data.length)
  const from = _toTime(data[data.length - n].date)
  const to   = _toTime(data[data.length - 1].date)
  chart.timeScale().setVisibleRange({ from, to })
}

interface ChartCanvasProps {
  symbol: string
  data: StockChartPoint[]
  period: ChartPeriod
  priceHeight: number
  investHeight: number
  flex?: boolean
}

function ChartCanvas({ symbol, data, period, priceHeight, investHeight, flex }: ChartCanvasProps) {
  const priceRef  = useRef<HTMLDivElement>(null)
  const investRef = useRef<HTMLDivElement>(null)
  const chartsRef = useRef<ChartRefs | null>(null)
  const hasInvestor = hasInvestorData(data)

  // Create charts once on mount
  useEffect(() => {
    if (!priceRef.current) return
    const refs = _buildCharts(
      priceRef.current,
      hasInvestor ? investRef.current : null,
      data,
      hasInvestor,
    )
    chartsRef.current = refs
    _applyPeriodRange(refs.priceChart, data, period)
    return () => {
      refs.priceChart.remove()
      refs.investChart?.remove()
      chartsRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol])  // remount on symbol change only; key prop handles modal

  // Update visible range on period change (no rebuild)
  useEffect(() => {
    const refs = chartsRef.current
    if (!refs) return
    _applyPeriodRange(refs.priceChart, data, period)
  }, [period, data])

  if (flex) {
    return (
      <div className="flex flex-col" style={{ height: '100%' }}>
        <div ref={priceRef} style={{ flex: 1 }} />
        {hasInvestor
          ? <div ref={investRef} style={{ height: investHeight }} />
          : <div className="text-center text-gray-600 text-xs py-2">투자자 데이터 없음</div>
        }
      </div>
    )
  }

  return (
    <div>
      <div ref={priceRef} style={{ height: priceHeight }} />
      {hasInvestor
        ? <div ref={investRef} style={{ height: investHeight }} />
        : <div className="text-center text-gray-600 text-xs py-2">투자자 데이터 없음</div>
      }
    </div>
  )
}

// ── 종목 분석 헬퍼 ──

interface TrendInfo { label: string; color: string; detail: string }

export function getTrendInfo(data: StockChartPoint[]): TrendInfo | null {
  if (data.length < 20) return null
  const closes = data.map(d => d.close)
  const ma5  = _computeMA(closes, 5)
  const ma20 = _computeMA(closes, 20)
  const ma60 = closes.length >= 60 ? _computeMA(closes, 60) : null
  const last = closes.length - 1
  const ma5L  = ma5[last] as number
  const ma20L = ma20[last] as number
  const ma60L = ma60 ? (ma60[last] ?? null) : null

  // 골든/데드 크로스 (최근 5일 이내)
  let crossType: 'golden' | 'dead' | null = null
  for (let i = Math.max(1, last - 4); i <= last; i++) {
    const [p5, c5, p20, c20] = [ma5[i-1], ma5[i], ma20[i-1], ma20[i]]
    if (p5 != null && c5 != null && p20 != null && c20 != null) {
      if (p5 < p20 && c5 >= c20) { crossType = 'golden'; break }
      if (p5 > p20 && c5 <= c20) { crossType = 'dead'; break }
    }
  }
  if (crossType === 'golden') return { label: '골든크로스', color: 'text-yellow-400', detail: 'MA5가 MA20을 상향돌파' }
  if (crossType === 'dead')   return { label: '데드크로스',  color: 'text-red-400',    detail: 'MA5가 MA20을 하향돌파' }

  const close = closes[last]
  if (close > ma5L && ma5L > ma20L && (ma60L == null || ma20L > ma60L))
    return { label: '강세 정배열', color: 'text-emerald-400', detail: `종가 > MA5 > MA20${ma60L ? ' > MA60' : ''}` }
  if (close < ma5L && ma5L < ma20L && (ma60L == null || ma20L < ma60L))
    return { label: '약세 역배열', color: 'text-red-400',    detail: `종가 < MA5 < MA20${ma60L ? ' < MA60' : ''}` }
  if (close > ma20L)
    return { label: '단기 반등 중', color: 'text-sky-400',    detail: `종가 MA20(${ma20L.toFixed(0)}) 상회` }
  return   { label: '조정 중',     color: 'text-gray-400',   detail: `종가 MA20(${ma20L.toFixed(0)}) 하회` }
}

export interface SupplyInfo { line: string; foreignColor: string }

export function getSupplyInfo(data: StockChartPoint[]): SupplyInfo | null {
  if (data.length === 0) return null
  const recent = data.slice(-5)
  if (!recent.some(d => d.foreign_net != null && d.foreign_net !== 0)) return null

  const fSum = recent.reduce((a, d) => a + (d.foreign_net ?? 0), 0)
  const iSum = recent.reduce((a, d) => a + (d.inst_net ?? 0), 0)

  // 외국인 연속 streak
  let streak = 0
  for (let i = data.length - 1; i >= 0; i--) {
    const v = data[i].foreign_net ?? 0
    if (streak === 0) { streak = v > 0 ? 1 : v < 0 ? -1 : 0 }
    else if (streak > 0 && v > 0) streak++
    else if (streak < 0 && v < 0) streak--
    else break
  }

  const fDir = fSum >= 0 ? '매수' : '매도'
  const iDir = iSum >= 0 ? '매수' : '매도'
  const fAmt = `${fSum >= 0 ? '+' : ''}${fSum.toFixed(0)}억`
  const iAmt = `${iSum >= 0 ? '+' : ''}${iSum.toFixed(0)}억`
  const streakStr = Math.abs(streak) >= 2 ? ` ${Math.abs(streak)}일 연속` : ''

  const line = `외국인 5일${streakStr} ${fDir} (${fAmt}), 기관 ${iDir} (${iAmt})`
  const foreignColor = fSum > 0 ? 'text-sky-400' : 'text-red-400'
  return { line, foreignColor }
}

export function StockInvestorChart({ symbol, name = '', onDataLoaded, priceHeight = 270, investHeight = 130, flex = false }: {
  symbol: string; name?: string; onDataLoaded?: (data: StockChartPoint[]) => void
  priceHeight?: number; investHeight?: number; flex?: boolean
}) {
  const [period, setPeriod] = useState<ChartPeriod>('60d')
  const [data, setData] = useState<StockChartPoint[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const onDataLoadedRef = useRef(onDataLoaded)
  onDataLoadedRef.current = onDataLoaded

  // 1년치 데이터를 한 번만 로드하고 기간 버튼은 visible range만 조정
  useEffect(() => {
    setLoading(true)
    setError(false)
    screenerApi.stockChart(symbol, 'all')
      .then(d => { setData(d); setLoading(false); onDataLoadedRef.current?.(d) })
      .catch(() => { setError(true); setLoading(false) })
  }, [symbol])

  useEffect(() => {
    if (!expanded) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') setExpanded(false) }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [expanded])

  const periods: { key: ChartPeriod; label: string }[] = [
    { key: '60d',  label: '60일' },
    { key: '120d', label: '120일' },
    { key: '1y',   label: '1년' },
    { key: '2y',   label: '2년' },
    { key: '3y',   label: '3년' },
    { key: 'all',  label: '전체' },
  ]

  const periodButtons = (
    <div className="flex gap-1">
      {periods.map(p => (
        <button
          key={p.key}
          onClick={() => setPeriod(p.key)}
          className={`text-xs px-2 py-0.5 rounded transition-colors ${
            period === p.key
              ? 'bg-sky-500/20 border border-sky-500/40 text-sky-400'
              : 'bg-gray-800/50 border border-gray-700/50 text-gray-500 hover:text-gray-400'
          }`}
        >
          {p.label}
        </button>
      ))}
    </div>
  )

  const legend = data && hasInvestorData(data) ? (
    <div className="flex items-center gap-3 text-[10px] text-gray-500 flex-wrap mt-1">
      <span className="flex items-center gap-1"><span style={{ color: LW_COLORS.up }}>▲</span> 양봉</span>
      <span className="flex items-center gap-1"><span style={{ color: LW_COLORS.down }}>▼</span> 음봉</span>
      <span className="flex items-center gap-1"><span style={{ background: LW_COLORS.ma5  }} className="inline-block w-5 h-0.5" /> MA5</span>
      <span className="flex items-center gap-1"><span style={{ background: LW_COLORS.ma20 }} className="inline-block w-5 h-0.5" /> MA20</span>
      <span className="flex items-center gap-1"><span style={{ background: LW_COLORS.ma60 }} className="inline-block w-5 h-0.5" /> MA60</span>
      <span className="flex items-center gap-1"><span style={{ background: LW_COLORS.foreign }} className="inline-block w-2.5 h-2.5 rounded-sm opacity-80" /> 외국인</span>
      <span className="flex items-center gap-1"><span style={{ background: LW_COLORS.inst    }} className="inline-block w-2.5 h-2.5 rounded-sm opacity-80" /> 기관</span>
      <span className="flex items-center gap-1"><span style={{ background: LW_COLORS.indiv   }} className="inline-block w-2.5 h-2.5 rounded-sm opacity-80" /> 개인</span>
    </div>
  ) : null

  return (
    <div className={flex ? 'h-full flex flex-col gap-1' : 'space-y-1'}>
      <div className="flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-gray-300 text-xs font-medium">{symbol}{name ? ` ${name}` : ''} 차트</span>
          <span className="text-gray-600 text-xs">|</span>
          <span className="text-gray-500 text-xs">기간</span>
          {periodButtons}
        </div>
        <button
          onClick={() => setExpanded(true)}
          title="차트 확대 (ESC로 닫기)"
          className="text-gray-600 hover:text-gray-300 transition-colors p-0.5 rounded"
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"
            strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4">
            <path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4" />
          </svg>
        </button>
      </div>

      {loading && (
        <div className="flex items-center justify-center h-32 text-gray-500 text-xs">차트 로딩 중...</div>
      )}
      {error && (
        <div className="flex items-center justify-center h-20 text-gray-600 text-xs">차트 데이터를 불러올 수 없습니다</div>
      )}

      {!loading && !error && data && (
        <div className={flex ? 'flex-1 min-h-0 flex flex-col' : ''}>
          <ChartCanvas
            key={symbol}
            symbol={symbol}
            data={data}
            period={period}
            priceHeight={flex ? 0 : priceHeight}
            investHeight={investHeight}
            flex={flex}
          />
          {legend}
        </div>
      )}

      {expanded && data && (
        <div
          className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4"
          onClick={e => { if (e.target === e.currentTarget) setExpanded(false) }}
        >
          <div className="bg-gray-900 border border-gray-700 rounded-xl p-4 flex flex-col"
               style={{ width: '95vw', height: '85vh' }}>
            <div className="flex items-center justify-between mb-2 shrink-0">
              <div className="flex items-center gap-3">
                <span className="text-gray-200 text-sm font-medium">{symbol}{name ? ` ${name}` : ''} 차트</span>
                <div className="flex items-center gap-2">
                  <span className="text-gray-500 text-xs">기간</span>
                  {periodButtons}
                </div>
              </div>
              <button
                onClick={() => setExpanded(false)}
                className="text-gray-500 hover:text-gray-200 transition-colors text-xl leading-none px-1"
                title="닫기 (ESC)"
              >
                ✕
              </button>
            </div>
            <div className="flex-1 min-h-0">
              <ChartCanvas
                key={`${symbol}-modal`}
                symbol={symbol}
                data={data}
                period={period}
                priceHeight={0}
                investHeight={150}
                flex
              />
            </div>
            {legend}
          </div>
        </div>
      )}
    </div>
  )
}

export function StockProfileCard({
  profile,
  onClose,
  onApplyFilters,
  aiRank,
  aiProb,
  onAddToCompare,
  inCompareList,
  onToggleWatchlist,
  inWatchlist,
}: {
  profile: SymbolProfile
  onClose: () => void
  onApplyFilters: (filters: Partial<ScreenerFilters>) => void
  aiRank?: number | null
  aiProb?: number | null
  onAddToCompare?: (symbol: string) => void
  inCompareList?: boolean
  onToggleWatchlist?: (symbol: string, name: string) => void
  inWatchlist?: boolean
}) {
  const { stock: s, filter_flags: flags } = profile
  const passedFlags = FLAG_FILTER_MAP.filter(f => flags[f.key])
  const [chartData, setChartData] = useState<StockChartPoint[] | null>(null)
  const trendInfo  = chartData ? getTrendInfo(chartData) : null
  const supplyInfo = chartData ? getSupplyInfo(chartData) : null

  const handleApplyFilters = () => {
    const newFilters: Partial<ScreenerFilters> = {}
    // score_60d_top10이 true면 top20은 중복 — top10만 적용
    FLAG_FILTER_MAP.forEach(f => {
      if (f.key === 'score_60d_top20' && flags['score_60d_top10']) return
      if (flags[f.key]) newFilters[f.filterKey] = true
    })
    onApplyFilters(newFilters)
  }

  return (
    <div className="bg-gray-900 border border-sky-500/30 rounded-xl p-4 space-y-4">
      {/* 헤더 */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono text-sky-400 font-bold">{s.symbol}</span>
            <span className="text-white font-semibold text-lg">{s.name}</span>
            <span className="text-xs px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 border border-gray-700">{s.market}</span>
            {s.sector_name && (
              <span className="text-xs text-gray-500">· {s.sector_name}</span>
            )}
            {aiRank != null && aiRank <= 30 && (
              <span className="text-xs px-2 py-0.5 rounded bg-emerald-500/15 border border-emerald-500/30 text-emerald-400">
                🤖 AI 추천 #{aiRank}{aiProb != null ? ` (${Math.round(aiProb * 100)}%)` : ''}
              </span>
            )}
            {aiRank != null && aiRank > 30 && (
              <span className="text-xs text-gray-500">오늘 AI 추천에 없음</span>
            )}
          </div>
          <p className="text-2xl font-bold text-white mt-1">{s.close.toLocaleString()}원</p>
          {/* 종합 한줄 요약 — 섹터는 즉시, 추세·수급은 차트 로드 후 */}
          {(s.sector_name || trendInfo || supplyInfo) && (
            <p className="text-xs text-gray-400 mt-1 leading-relaxed">
              {[
                s.sector_name ?? null,
                trendInfo ? trendInfo.label : null,
                supplyInfo ? (supplyInfo.foreignColor === 'text-sky-400' ? '외국인 매수 유입' : '외국인 매도 중') : null,
              ].filter(Boolean).join(' · ')}
            </p>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {onToggleWatchlist && (
            <button
              onClick={() => onToggleWatchlist(s.symbol, s.name)}
              title={inWatchlist ? '관심 종목 해제' : '관심 종목 추가'}
              className={`text-lg px-1 py-0.5 transition-colors ${
                inWatchlist ? 'text-yellow-400 hover:text-yellow-300' : 'text-gray-600 hover:text-yellow-400'
              }`}
            >
              {inWatchlist ? '★' : '☆'}
            </button>
          )}
          {onAddToCompare && (
            <button
              onClick={() => onAddToCompare(s.symbol)}
              className={`text-xs px-2 py-1 rounded border transition-colors ${
                inCompareList
                  ? 'border-violet-400/60 bg-violet-500/20 text-violet-300'
                  : 'border-gray-600/60 bg-gray-800/60 text-gray-400 hover:border-violet-400/50 hover:text-violet-300'
              }`}
            >
              {inCompareList ? '✓ 비교중' : '+ 비교'}
            </button>
          )}
          <button
            onClick={() => window.open(`/chart?ticker=${s.symbol}`, '_blank', 'width=1200,height=900')}
            title="새 창으로 열기"
            className="text-gray-500 hover:text-gray-300 transition-colors p-1 mt-0.5"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
            </svg>
          </button>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 transition-colors p-1 mt-0.5">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      {/* 주요 수치 */}
      <div className="grid grid-cols-4 sm:grid-cols-8 gap-2">
        {[
          { label: 'PER', value: s.per_pit != null ? s.per_pit.toFixed(1) : '—' },
          { label: 'PBR', value: s.pbr_pit != null ? s.pbr_pit.toFixed(2) : '—' },
          { label: 'RSI', value: s.rsi_14 != null ? s.rsi_14.toFixed(0) : '—' },
          { label: '거래량비', value: s.vol_ratio_20d != null ? `${s.vol_ratio_20d.toFixed(1)}x` : '—' },
          { label: '20일수익', value: s.ret_20d != null ? `${(s.ret_20d >= 0 ? '+' : '')}${(s.ret_20d * 100).toFixed(1)}%` : '—' },
          { label: 'ATR%', value: s.atr_pct != null ? `${(s.atr_pct * 100).toFixed(1)}%` : '—' },
          { label: '외국인', value: s.foreign_rate != null ? `${s.foreign_rate.toFixed(1)}%` : '—' },
        ].map(item => (
          <div key={item.label} className="bg-gray-800/60 rounded-lg px-2 py-2 text-center">
            <p className="text-gray-500 text-xs">{item.label}</p>
            <p className="text-gray-200 text-sm font-medium mt-0.5">{item.value}</p>
          </div>
        ))}
        {/* 배당(실질) — 뱃지 툴팁이 필요해 별도 렌더 */}
        <div className="bg-gray-800/60 rounded-lg px-2 py-2 text-center">
          <p className="text-gray-500 text-xs">배당(실질)</p>
          <p className="text-gray-200 text-sm font-medium mt-0.5 flex flex-wrap items-center justify-center gap-0.5">
            {s.actual_yield != null || s.dividend_yield != null ? (
              <>
                <span>{s.actual_yield != null ? `${s.actual_yield.toFixed(1)}%` : `${s.dividend_yield!.toFixed(1)}%`}</span>
                {s.actual_yield != null && s.dividend_yield != null && (
                  <span className="text-gray-500 text-xs">(현재가 {s.dividend_yield.toFixed(1)}%)</span>
                )}
                {s.has_special_dividend && (
                  <span className="bg-amber-500/20 text-amber-400 text-[9px] px-1 py-0.5 rounded leading-tight cursor-help" title="특별배당 또는 payout 100%+ — 일회성 배당일 수 있어 내년 동일 금액 지급 불확실">특별</span>
                )}
                {s.has_split_adjusted && (
                  <span className="bg-amber-500/20 text-amber-400 text-[9px] px-1 py-0.5 rounded leading-tight cursor-help" title="주식분할/병합으로 DPS가 환산됨 — 실제 배당금 총액이 늘어난 것은 아님">병합환산</span>
                )}
                {s.has_div_suspended && (
                  <span className="bg-red-500/20 text-red-400 text-[9px] px-1 py-0.5 rounded leading-tight cursor-help" title="FY2025 배당 공시 없음 확인 — 올해 배당이 중단됐을 가능성 있음. 표시된 수익률은 직전 연도(FY2024) 기준">중단의심</span>
                )}
                {s.has_unverified_yield && (
                  <span className="bg-red-600/20 text-red-300 text-[9px] px-1 py-0.5 rounded leading-tight cursor-help" title="배당수익률 15% 초과이나 특별배당·분할·중단 여부가 확인되지 않음 — 데이터 오류 또는 일회성 배당일 가능성이 높으므로 개별 확인 필요">미검증</span>
                )}
              </>
            ) : '—'}
          </p>
        </div>
      </div>

      {/* 주가 + 투자자 차트 */}
      <StockInvestorChart symbol={s.symbol} name={s.name} onDataLoaded={setChartData} />

      {/* 추세 분석 */}
      {trendInfo && (
        <div className="bg-gray-800/40 rounded-lg px-3 py-2.5 space-y-1">
          <p className="text-gray-500 text-xs font-medium">추세 분석</p>
          <div className="flex items-center gap-2">
            <span className={`text-sm font-semibold ${trendInfo.color}`}>{trendInfo.label}</span>
            <span className="text-gray-500 text-xs">{trendInfo.detail}</span>
          </div>
        </div>
      )}

      {/* 수급 분석 */}
      {supplyInfo && (
        <div className="bg-gray-800/40 rounded-lg px-3 py-2.5 space-y-1">
          <p className="text-gray-500 text-xs font-medium">수급 (최근 5거래일)</p>
          <p className={`text-sm ${supplyInfo.foreignColor}`}>{supplyInfo.line}</p>
        </div>
      )}

      {/* 필터 통과 배지 */}
      <div>
        <p className="text-gray-500 text-xs font-medium mb-2">해당 필터</p>
        <div className="flex flex-wrap gap-1.5">
          {FLAG_FILTER_MAP.map(f => {
            const passes = flags[f.key]
            const bs = BADGE_STYLE[f.badge]
            return passes ? (
              <span key={f.key} className={`text-xs px-2 py-0.5 rounded-full border ${bs.cls}`}>
                {bs.icon} {f.label}
              </span>
            ) : (
              <span key={f.key} className="text-xs px-2 py-0.5 rounded-full border border-gray-700/50 text-gray-600">
                — {f.label}
              </span>
            )
          })}
        </div>
      </div>

      {/* 이 조건으로 검색 버튼 */}
      {passedFlags.length > 0 ? (
        <button
          onClick={handleApplyFilters}
          className="w-full py-2 rounded-lg bg-sky-500/15 border border-sky-500/30 text-sky-400 text-sm font-medium hover:bg-sky-500/25 transition-colors"
        >
          이 조건으로 검색 ({passedFlags.length}개 필터 적용)
        </button>
      ) : (
        <p className="text-gray-500 text-xs text-center py-1">이 종목은 현재 어떤 필터 조건에도 해당하지 않습니다</p>
      )}
    </div>
  )
}

// ── 데이터 업데이트 버튼 (인앱, 2026-06-23 — 자동 스케줄 비활성화로 수동 트리거 필요) ──

function fmtUpdateDate(d: string | null): string {
  if (!d) return '—'
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`
}

/**
 * 현재 시각(KST) 기준으로 "기대되는 최신 거래일"을 YYYYMMDD 문자열로 반환.
 * - 주말이면 직전 금요일
 * - 평일 16:30 이전이면 전날(금요일로 소급 포함)
 * - 평일 16:30 이후면 당일
 */
export function expectedLatestTradingDay(now: Date = new Date()): string {
  // KST = UTC+9
  const kst = new Date(now.getTime() + 9 * 60 * 60 * 1000)
  const dow = kst.getUTCDay() // 0=일, 1=월 ... 6=토
  const hhmm = kst.getUTCHours() * 60 + kst.getUTCMinutes() // KST 시분(분 단위)
  const CUTOFF = 16 * 60 + 30 // 16:30

  // 오늘이 최신이 될 수 있는지 (평일 + 16:30 이후)
  const todayIsLatest = dow >= 1 && dow <= 5 && hhmm >= CUTOFF

  // 기준 날짜: 최신이면 오늘, 아니면 어제
  const base = new Date(kst)
  if (!todayIsLatest) base.setUTCDate(base.getUTCDate() - 1)

  // 주말이면 직전 금요일로 소급
  const baseDow = base.getUTCDay()
  if (baseDow === 0) base.setUTCDate(base.getUTCDate() - 2) // 일요일 → 금요일
  else if (baseDow === 6) base.setUTCDate(base.getUTCDate() - 1) // 토요일 → 금요일

  const y = base.getUTCFullYear()
  const m = String(base.getUTCMonth() + 1).padStart(2, '0')
  const d = String(base.getUTCDate()).padStart(2, '0')
  return `${y}${m}${d}`
}

/**
 * 두 YYYYMMDD 문자열 사이의 거래일 수(근사).
 * 공휴일은 고려하지 않고 주말만 제외.
 */
export function tradingDayDiff(older: string, newer: string): number {
  const toDate = (s: string) => {
    const y = +s.slice(0, 4), m = +s.slice(4, 6) - 1, d = +s.slice(6, 8)
    return new Date(y, m, d)
  }
  const a = toDate(older)
  const b = toDate(newer)
  if (b <= a) return 0
  let count = 0
  const cur = new Date(a)
  cur.setDate(cur.getDate() + 1) // older 다음날부터
  while (cur <= b) {
    const dow = cur.getDay()
    if (dow !== 0 && dow !== 6) count++
    cur.setDate(cur.getDate() + 1)
  }
  return count
}

/** 신선도 판정 결과 */
export type DataFreshness = { lag: number; label: string; color: 'green' | 'orange' | 'red' }

export function calcDataFreshness(dataDate: string | null, now: Date = new Date()): DataFreshness | null {
  if (!dataDate) return null
  const expected = expectedLatestTradingDay(now)
  const lag = tradingDayDiff(dataDate, expected)
  if (lag === 0) return { lag, label: '✅ 최신', color: 'green' }
  if (lag === 1) return { lag, label: `⚠ ${lag}일 전`, color: 'orange' }
  return { lag, label: `🔴 ${lag}일 전`, color: 'red' }
}

export function DataUpdateBanner({ initialDate, onComplete }: { initialDate?: string | null; onComplete?: () => void }) {
  const [status, setStatus] = useState<UpdateStatus | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [pollKick, setPollKick] = useState(0)
  const [logExpanded, setLogExpanded] = useState(false)
  // 클라이언트 경과시간 카운터 — started_at(Unix초) 기준 1초마다 증가
  const [elapsedSec, setElapsedSec] = useState(0)
  const prevStatusRef = useRef<string | null>(null)
  const onCompleteRef = useRef(onComplete)
  useEffect(() => { onCompleteRef.current = onComplete }, [onComplete])

  const poll = useCallback(async (): Promise<UpdateStatus | null> => {
    try {
      const s = await updateApi.getStatus()
      if (prevStatusRef.current === 'running' && s.status === 'done') {
        onCompleteRef.current?.()
      }
      prevStatusRef.current = s.status
      setStatus(s)
      return s
    } catch {
      return null
    }
  }, [])

  // 마운트 시 + pollKick 변경 시(버튼 클릭 후) 폴링 체인 시작
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const tick = async () => {
      const s = await poll()
      if (cancelled) return
      if (s?.status === 'running') timer = setTimeout(tick, 3000)
    }
    tick()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [poll, pollKick])

  // 클라이언트 경과시간 — started_at 앵커 기반 1초 카운터 (폴링 주기와 독립)
  const running = status?.status === 'running'
  useEffect(() => {
    if (!running || !status?.started_at) { setElapsedSec(0); return }
    const anchor = status.started_at * 1000  // Unix초 → ms
    const tick = () => setElapsedSec(Math.floor((Date.now() - anchor) / 1000))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [running, status?.started_at])

  // 하트비트 stale 판정 — 마지막 갱신이 300초(5분) 넘으면 경고
  const heartbeatAgoSec = status?.last_heartbeat
    ? (Date.now() / 1000) - status.last_heartbeat
    : null
  const heartbeatStale = heartbeatAgoSec !== null && heartbeatAgoSec > 300

  const handleClick = async () => {
    setStartError(null)
    setStarting(true)
    try {
      await updateApi.start()
      setPollKick(k => k + 1)
    } catch (e) {
      setStartError(e instanceof Error ? e.message : '시작 실패')
    } finally {
      setStarting(false)
    }
  }

  const fmtElapsed = (s: number) => s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`

  const dataDate = status?.latest_feature_date ?? initialDate ?? null
  const freshness = calcDataFreshness(dataDate)
  const isStale = freshness !== null && freshness.color === 'red'

  const bannerBg = running
    ? heartbeatStale
      ? 'bg-amber-950/40 border-amber-600/40'
      : 'bg-blue-950/30 border-blue-700/30'
    : isStale
      ? 'bg-red-950/20 border-red-800/40'
      : 'bg-gray-900 border-gray-800'

  const recentLogs = (status?.log ?? []).filter(l => l.trim()).slice(-5)

  return (
    <div className={`rounded-xl border px-4 py-3 space-y-2 transition-colors ${bannerBg}`}>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        {/* 왼쪽: 데이터 기준일 + 경고 배지 */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-gray-400 text-xs flex items-center gap-1.5 flex-wrap">
            데이터 기준: <span className="text-gray-200 font-medium">{fmtUpdateDate(dataDate)}</span>
            {freshness && (
              <span className={`font-medium ${
                freshness.color === 'green' ? 'text-emerald-400' :
                freshness.color === 'orange' ? 'text-amber-400' : 'text-red-400'
              }`}>{freshness.label}</span>
            )}
          </span>
          {status?.foreign_rate_warning && (
            <BadgeTooltip text="외국인비율은 업데이트를 안 돌린 날만큼 영구히 공백이 생기는 구조입니다 — 업데이트를 눌러 채우세요. (PER/PBR은 point-in-time 방식으로 자동 보정되어 더 이상 해당 없음)" placement="top-end">
              <span className="text-xs px-2 py-0.5 rounded bg-yellow-500/15 text-yellow-400 border border-yellow-500/30 cursor-default">
                ⚠️ 외국인비율 {status?.foreign_rate_stale_days}일째 갱신 안 됨 (PER/PBR은 자동 보정됨)
              </span>
            </BadgeTooltip>
          )}
          {status?.dart_partial && (
            <span className="text-xs px-2 py-0.5 rounded bg-sky-500/15 text-sky-400 border border-sky-500/30">
              ℹ️ 섹터/BPS 일부만 받음(DART 한도) — 다음 업데이트에 이어받음
            </span>
          )}
        </div>

        {/* 오른쪽: 상태 텍스트 + 버튼 */}
        <div className="flex items-center gap-2">
          {status?.status === 'error' && !running && (
            <span className="text-red-400 text-xs">업데이트 실패 — 서버는 정상, 아래 로그 확인</span>
          )}
          {status?.status === 'done' && !running && (
            <span className="text-emerald-400 text-xs">✓ 완료</span>
          )}
          {status?.status === 'skipped' && !running && (
            <span className="text-gray-400 text-xs">
              {status.reason === 'weekend' ? '스킵됨 (비거래일)' : status.reason === 'already_current' ? '스킵됨 (이미 최신)' : '스킵됨'}
            </span>
          )}
          <button
            onClick={handleClick}
            disabled={running || starting}
            className={`text-xs font-medium px-3 py-1.5 rounded-lg border transition-colors whitespace-nowrap ${
              running || starting
                ? 'border-gray-700 text-gray-500 cursor-not-allowed'
                : isStale
                  ? 'border-red-500/60 text-red-400 hover:bg-red-500/10 animate-pulse'
                  : 'border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10'
            }`}
          >
            {running ? `업데이트 중... ${fmtElapsed(elapsedSec)}` : starting ? '시작 중...' : '데이터 업데이트'}
          </button>
        </div>
      </div>

      {/* running 시 진행 단계 + 하트비트 경고 */}
      {running && (
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="flex items-center gap-2 min-w-0">
            {status?.current_stage != null && (
              <span className="text-xs text-blue-300 font-medium whitespace-nowrap">
                {status.current_stage}/{status.total_stages ?? 11}단계
              </span>
            )}
            {status?.current_stage_name && (
              <span className="text-xs text-gray-400 truncate">{status.current_stage_name}</span>
            )}
          </div>
          {heartbeatStale && (
            <span className="text-xs text-amber-400 font-medium whitespace-nowrap">
              ⚠ 응답 없음 — 중단 의심 ({Math.floor((heartbeatAgoSec ?? 0) / 60)}분 전 마지막 신호)
            </span>
          )}
          {/* 로그 펼치기 버튼 */}
          {recentLogs.length > 0 && (
            <button
              onClick={() => setLogExpanded(v => !v)}
              className="text-xs text-gray-500 hover:text-gray-300 flex items-center gap-1 ml-auto shrink-0"
            >
              로그 {logExpanded ? '▲' : '▼'}
            </button>
          )}
        </div>
      )}

      {/* 확장 로그 영역 */}
      {running && logExpanded && recentLogs.length > 0 && (
        <div className="border border-gray-700 rounded-lg bg-gray-950 px-3 py-2 space-y-0.5">
          {recentLogs.slice(-3).map((line, i) => (
            <p key={i} className="text-xs text-gray-400 font-mono truncate">{line}</p>
          ))}
        </div>
      )}

      {startError && <p className="text-red-400 text-xs">{startError}</p>}
    </div>
  )
}

// ── 시장 국면 배너 ────────────────────────────────────────────

export function RegimeBanner({ regime }: { regime: ScreenerRegime | null }) {
  if (!regime) return null
  const isBear = regime.trend === 'bear'
  const isBull = regime.trend === 'bull'
  const color = isBear ? 'border-red-500/30 bg-red-500/10 text-red-300'
    : isBull ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
    : 'border-yellow-500/30 bg-yellow-500/10 text-yellow-300'

  return (
    <div className={`rounded-xl border px-4 py-3 space-y-1 ${color}`}>
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-bold text-sm">
          {isBear ? '🔴' : isBull ? '🟢' : '🟡'} 현재 시장 국면: {regime.label}
        </span>
        {regime.ret_20d_pct != null && (
          <span className="text-xs opacity-80">(KOSPI 20일 수익률 {regime.ret_20d_pct >= 0 ? '+' : ''}{regime.ret_20d_pct.toFixed(1)}%)</span>
        )}
      </div>
      <p className="text-xs opacity-90">{regime.mean_reversion_reason}</p>
    </div>
  )
}

// ── 결과 테이블 행 ────────────────────────────────────────────

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null) return '—'
  return (v >= 0 ? '+' : '') + (v * 100).toFixed(digits) + '%'
}

function StockRow({ stock, starred, onToggleStar, onClick }: {
  stock: ScreenerStock
  starred: boolean
  onToggleStar: () => void
  onClick: () => void
}) {
  return (
    <tr className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors cursor-pointer" onClick={onClick}>
      <td className="px-3 py-2.5">
        <button
          onClick={(e) => { e.stopPropagation(); onToggleStar() }}
          className={`text-base transition-colors ${starred ? 'text-yellow-400' : 'text-gray-600 hover:text-gray-400'}`}
        >★</button>
      </td>
      <td className="px-3 py-2.5">
        <span className="text-white font-mono text-xs">{stock.symbol}</span>
        <span className="text-gray-300 text-xs ml-1.5">{stock.name}</span>
        <span className="text-gray-500 text-xs ml-1.5">{stock.market}</span>
      </td>
      <td className="px-3 py-2.5 text-right text-gray-300 text-xs">{stock.close.toLocaleString()}원</td>
      <td className="px-3 py-2.5 text-right text-gray-400 text-xs">{stock.per_pit != null ? stock.per_pit.toFixed(1) : '—'}</td>
      <td className="px-3 py-2.5 text-right text-gray-400 text-xs">{stock.pbr_pit != null ? stock.pbr_pit.toFixed(2) : '—'}</td>
      <td className="px-3 py-2.5 text-right text-gray-400 text-xs">{stock.rsi_14 != null ? stock.rsi_14.toFixed(0) : '—'}</td>
      <td className="px-3 py-2.5 text-right text-xs">
        {(stock.actual_yield != null || stock.dividend_yield != null) ? (
          <span className="inline-flex flex-col items-end gap-0.5">
            <span className="inline-flex items-center gap-1">
              <span className="text-gray-200 font-medium">
                {stock.actual_yield != null ? `${stock.actual_yield.toFixed(1)}%` : `${stock.dividend_yield!.toFixed(1)}%`}
              </span>
              {stock.has_special_dividend && (
                <span className="bg-amber-500/20 text-amber-400 text-[9px] px-1 py-0.5 rounded leading-tight cursor-help" title="특별배당 또는 payout 100%+ — 일회성 배당일 수 있어 내년 동일 금액 지급 불확실">특별</span>
              )}
              {stock.has_split_adjusted && (
                <span className="bg-amber-500/20 text-amber-400 text-[9px] px-1 py-0.5 rounded leading-tight cursor-help" title="주식분할/병합으로 DPS가 환산됨 — 실제 배당금 총액이 늘어난 것은 아님">병합환산</span>
              )}
              {stock.has_div_suspended && (
                <span className="bg-red-500/20 text-red-400 text-[9px] px-1 py-0.5 rounded leading-tight cursor-help" title="FY2025 배당 공시 없음 확인 — 올해 배당이 중단됐을 가능성 있음. 표시된 수익률은 직전 연도(FY2024) 기준">중단의심</span>
              )}
              {stock.has_unverified_yield && (
                <span className="bg-red-600/20 text-red-300 text-[9px] px-1 py-0.5 rounded leading-tight cursor-help" title="배당수익률 15% 초과이나 특별배당·분할·중단 여부가 확인되지 않음 — 데이터 오류 또는 일회성 배당일 가능성이 높으므로 개별 확인 필요">미검증</span>
              )}
            </span>
            {stock.actual_yield != null && stock.dividend_yield != null && (
              <span className="text-gray-500 text-[10px]">현재가 {stock.dividend_yield.toFixed(1)}%</span>
            )}
          </span>
        ) : '—'}
      </td>
      <td className="px-3 py-2.5 text-right text-gray-400 text-xs">{stock.vol_ratio_20d != null ? `${stock.vol_ratio_20d.toFixed(1)}x` : '—'}</td>
      <td className="px-3 py-2.5 text-right text-xs font-medium">{fmtPct(stock.ret_20d)}</td>
      <td className="px-3 py-2.5 text-right text-teal-400 text-xs">{stock.score_60d != null ? `${(stock.score_60d * 100).toFixed(1)}%` : '—'}</td>
    </tr>
  )
}

// ── 섹터 트리맵 ────────────────────────────────────────────────

interface _TmItem {
  code: string; name: string; val: number; hasCross: boolean
}
interface _TmTile extends _TmItem {
  x: number; y: number; w: number; h: number
}

function _tmWorst(row: number[], shorter: number): number {
  if (!row.length || shorter === 0) return Infinity
  const s = row.reduce((a, b) => a + b, 0)
  if (s === 0) return Infinity
  const maxR = Math.max(...row), minR = Math.min(...row)
  const s2 = s * s, w2 = shorter * shorter
  return Math.max(s2 / (w2 * minR), maxR * w2 / s2)
}

function _squarify(
  items: Array<_TmItem & { area: number }>,
  x: number, y: number, w: number, h: number,
  result: _TmTile[],
) {
  if (!items.length || w < 1 || h < 1) return
  const shorter = Math.min(w, h)
  let row: number[] = [], rowItems: typeof items = [], i = 0
  while (i < items.length) {
    const a = items[i].area
    const newRow = [...row, a]
    if (!row.length || _tmWorst(row, shorter) >= _tmWorst(newRow, shorter)) {
      row.push(a); rowItems.push(items[i]); i++
    } else { break }
  }
  const rowSum = row.reduce((a, b) => a + b, 0)
  let off = 0
  if (w >= h) {
    const thickness = rowSum / h
    for (const ri of rowItems) {
      const cellH = ri.area * h / rowSum
      result.push({ ...ri, x, y: y + off, w: thickness, h: cellH })
      off += cellH
    }
    _squarify(items.slice(i), x + thickness, y, w - thickness, h, result)
  } else {
    const thickness = rowSum / w
    for (const ri of rowItems) {
      const cellW = ri.area * w / rowSum
      result.push({ ...ri, x: x + off, y, w: cellW, h: thickness })
      off += cellW
    }
    _squarify(items.slice(i), x, y + thickness, w, h - thickness, result)
  }
}

function computeSquarifiedTreemap(
  items: _TmItem[], containerW: number, containerH: number,
): _TmTile[] {
  if (!items.length || containerW < 4 || containerH < 4) return []
  const totalAbs = items.reduce((s, i) => s + Math.abs(i.val), 0)
  if (totalAbs === 0) return []
  const totalArea = containerW * containerH
  const sorted = [...items].sort((a, b) => Math.abs(b.val) - Math.abs(a.val))
  const sized = sorted.map(i => ({ ...i, area: (Math.abs(i.val) / totalAbs) * totalArea }))
  const result: _TmTile[] = []
  _squarify(sized, 0, 0, containerW, containerH, result)
  return result
}

const _TM_H = 260
const _TM_GAP = 2
const _TM_MIN_PCT = 0.005 // 전체 절댓값 대비 0.5% 미만 → 기타 묶기

function _tmTileColor(val: number, maxAbs: number, isOther: boolean): string {
  if (isOther) return '#1f2937'
  const intensity = Math.min(1, Math.max(0, Math.abs(val) / maxAbs))
  if (val >= 0) {
    // emerald: #022c22 → #34d399
    return `rgb(${Math.round(2+50*intensity)},${Math.round(44+167*intensity)},${Math.round(34+119*intensity)})`
  }
  // red: #450a0a → #ef4444
  return `rgb(${Math.round(69+170*intensity)},${Math.round(10+58*intensity)},${Math.round(10+58*intensity)})`
}

function _TmSvg({
  tiles, onTileClick, isOther, svgW, svgH,
}: {
  tiles: _TmTile[]
  onTileClick: (tile: _TmTile) => void
  isOther: (code: string) => boolean
  svgW: number
  svgH: number
}) {
  const [hovered, setHovered] = useState<{ tile: _TmTile; mx: number; my: number } | null>(null)
  const maxAbs = Math.max(1, ...tiles.filter(t => !isOther(t.code)).map(t => Math.abs(t.val)))

  const tooltip = (() => {
    if (!hovered) return null
    const { tile, mx, my } = hovered
    const amtStr = `${tile.val >= 0 ? '+' : ''}${Math.round(tile.val).toLocaleString()}억`
    const tw = Math.max(100, Math.max(tile.name.length, amtStr.length) * 7 + 20)
    const th = 42
    let tx = mx + 12
    let ty = my + 14
    if (tx + tw > svgW - 4) tx = mx - tw - 8
    if (ty + th > svgH - 4) ty = my - th - 8
    return (
      <g style={{ pointerEvents: 'none' }}>
        <rect x={tx} y={ty} width={tw} height={th} rx={4}
          fill="rgba(15,23,42,0.97)" stroke="rgba(255,255,255,0.18)" strokeWidth={1} />
        <text x={tx + 10} y={ty + 14} fontSize={11} fontWeight="600"
          fill="rgba(255,255,255,0.95)">{tile.name}</text>
        <text x={tx + 10} y={ty + 30} fontSize={11}
          fill={tile.val >= 0 ? '#34d399' : '#f87171'}>{amtStr}</text>
      </g>
    )
  })()

  return (
    <>
      {tiles.map(tile => {
        const bx = tile.x + _TM_GAP / 2, by = tile.y + _TM_GAP / 2
        const bw = Math.max(0, tile.w - _TM_GAP), bh = Math.max(0, tile.h - _TM_GAP)
        const other = isOther(tile.code)
        const showName = bw > 38 && bh > 20
        const showAmt = bw > 55 && bh > 42
        const fs = Math.max(9, Math.min(13, bw / 7))
        const label = tile.name.length > 7 ? tile.name.slice(0, 6) + '…' : tile.name
        return (
          <g key={tile.code}
            onClick={() => !other && onTileClick(tile)}
            onMouseEnter={e => setHovered({ tile, mx: e.nativeEvent.offsetX, my: e.nativeEvent.offsetY })}
            onMouseMove={e => setHovered(h => h ? { ...h, mx: e.nativeEvent.offsetX, my: e.nativeEvent.offsetY } : null)}
            onMouseLeave={() => setHovered(null)}
            style={{ cursor: other ? 'default' : 'pointer' }}>
            <rect x={bx} y={by} width={bw} height={bh} fill={_tmTileColor(tile.val, maxAbs, other)} rx={3} />
            {tile.hasCross && (
              <rect x={bx} y={by} width={bw} height={bh} fill="none" stroke="#a855f7" strokeWidth={2} rx={3} />
            )}
            {showName && (
              <text x={bx + bw / 2} y={by + bh / 2 - (showAmt ? 7 : 0)}
                textAnchor="middle" dominantBaseline="middle"
                fontSize={fs} fontWeight="600" fill="rgba(255,255,255,0.92)"
                style={{ pointerEvents: 'none', userSelect: 'none' }}>
                {label}
              </text>
            )}
            {showAmt && (
              <text x={bx + bw / 2} y={by + bh / 2 + 9}
                textAnchor="middle" dominantBaseline="middle"
                fontSize={Math.max(8, fs - 2)} fill="rgba(255,255,255,0.65)"
                style={{ pointerEvents: 'none', userSelect: 'none' }}>
                {tile.val >= 0 ? '+' : ''}{Math.round(tile.val).toLocaleString()}억
              </text>
            )}
          </g>
        )
      })}
      {tooltip}
    </>
  )
}

type PeriodKey = 'combined_5d' | 'combined_20d' | 'combined_60d' | 'combined_120d' | 'combined_250d'

const SECTOR_PERIOD_LABELS: Record<PeriodKey, string> = {
  combined_5d:   '최근 5일',
  combined_20d:  '최근 20일',
  combined_60d:  '최근 60일',
  combined_120d: '최근 120일',
  combined_250d: '최근 1년(250일)',
}

function _computeSectorTiles(
  sectors: SectorFlowResponse['sectors'],
  period: PeriodKey,
  w: number,
  h: number,
): _TmTile[] {
  if (w < 4 || h < 4) return []
  const totalAbs = sectors.reduce((s, sec) => s + Math.abs(sec[period] as number), 0)
  if (totalAbs === 0) return []
  const threshold = totalAbs * _TM_MIN_PCT
  const main: _TmItem[] = []
  let otherVal = 0, otherCount = 0
  for (const sec of sectors) {
    const v = sec[period] as number
    if (Math.abs(v) >= threshold) {
      main.push({ code: sec.code, name: sec.name, val: v, hasCross: sec.cross_stocks.length > 0 })
    } else { otherVal += v; otherCount++ }
  }
  if (otherCount > 0) {
    main.push({ code: '__other__', name: `기타 ${otherCount}개`, val: otherVal, hasCross: false })
  }
  return computeSquarifiedTreemap(main, w, h)
}

function _stockFlowForPeriod(
  st: SectorFlowStockItem,
  period: PeriodKey,
): number {
  if (period === 'combined_20d')  return st.foreign_20d  + st.inst_20d
  if (period === 'combined_60d')  return st.foreign_60d  + st.inst_60d
  if (period === 'combined_120d') return st.foreign_120d + st.inst_120d
  if (period === 'combined_250d') return st.foreign_250d + st.inst_250d
  return st.foreign_5d + st.inst_5d
}

/* ───────────────────────────────────────────
   비교 기능 — 플로팅 바
─────────────────────────────────────────── */
export function CompareFloatingBar({
  symbols, names, onOpen, onRemove, onClear,
}: {
  symbols: string[]
  names: Record<string, string>
  onOpen: () => void
  onRemove: (symbol: string) => void
  onClear: () => void
}) {
  if (symbols.length < 1) return null
  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 flex justify-center pb-4 pointer-events-none">
      <div className="pointer-events-auto bg-gray-900/95 border border-violet-500/40 rounded-xl px-4 py-3 shadow-2xl backdrop-blur flex items-center gap-3 max-w-xl w-full mx-4">
        <div className="flex-1 flex flex-wrap items-center gap-1.5">
          {symbols.map(sym => (
            <span key={sym} className="flex items-center gap-1 bg-violet-500/15 border border-violet-500/30 rounded-full px-2 py-0.5 text-xs text-violet-300">
              {names[sym] ?? sym}
              <button onClick={() => onRemove(sym)} className="text-violet-400/60 hover:text-violet-300 leading-none">×</button>
            </span>
          ))}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {symbols.length >= 2 && (
            <button
              onClick={onOpen}
              className="px-3 py-1.5 rounded-lg bg-violet-500 hover:bg-violet-400 text-white text-xs font-semibold transition-colors"
            >
              비교하기 ({symbols.length}개)
            </button>
          )}
          {symbols.length < 2 && (
            <span className="text-gray-500 text-xs">1개 더 추가하세요</span>
          )}
          <button onClick={onClear} className="text-gray-500 hover:text-gray-300 text-sm leading-none">✕</button>
        </div>
      </div>
    </div>
  )
}

/* ───────────────────────────────────────────
   비교 기능 — 비교 뷰 오버레이
─────────────────────────────────────────── */
const OVERLAY_COLORS = ['#60a5fa', '#fb923c', '#4ade80']
type ComparePeriod = '60d' | '120d' | '1y' | 'all'

// 날짜 문자열(YYYYMMDD) → 기간 기준 필터
function filterByPeriod(dates: string[], period: ComparePeriod): string[] {
  if (period === 'all') return dates
  const days = period === '60d' ? 60 : period === '120d' ? 120 : 252
  return dates.slice(-days)
}

function fmtDate(d: string) {
  return d.length === 8 ? `${d.slice(4, 6)}/${d.slice(6, 8)}` : d
}
function fmtDateLong(s: string) {
  return s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s
}

// 공통 날짜 교집합 후 기준점(=100) 정규화
function buildNormalizedRows(
  symbols: string[],
  results: StockChartPoint[][],
  period: ComparePeriod,
): { rows: Record<string, number | null>[]; startDate: string } {
  if (results.every(r => r.length === 0)) return { rows: [], startDate: '' }

  // 공통 날짜 교집합
  const sets = results.map(pts => new Set(pts.map(p => p.date)))
  const commonDates = results[0]
    .map(p => p.date)
    .filter(d => sets.every(s => s.has(d)))
    .sort()

  const filtered = filterByPeriod(commonDates, period)
  if (filtered.length === 0) return { rows: [], startDate: '' }

  // 기준일 종가 맵
  const closeMap = results.map(pts => {
    const m: Record<string, number> = {}
    pts.forEach(p => { m[p.date] = p.close })
    return m
  })
  const bases = symbols.map((_, i) => closeMap[i][filtered[0]] ?? 1)

  const rows = filtered.map(date => {
    const row: Record<string, number | null> = { date: date as unknown as number }
    symbols.forEach((sym, i) => {
      const c = closeMap[i][date]
      row[sym] = c != null ? (c / bases[i]) * 100 : null
    })
    return row
  })

  return { rows, startDate: filtered[0] }
}

// 수급 데이터 rows (원금액, 누적)
function buildFlowRows(
  symbols: string[],
  results: StockChartPoint[][],
  period: ComparePeriod,
  field: 'foreign_net' | 'inst_net',
): Record<string, number | null>[] {
  if (results.every(r => r.length === 0)) return []

  const sets = results.map(pts => new Set(pts.map(p => p.date)))
  const commonDates = results[0]
    .map(p => p.date)
    .filter(d => sets.every(s => s.has(d)))
    .sort()
  const filtered = filterByPeriod(commonDates, period)
  if (filtered.length === 0) return []

  const flowMaps = results.map(pts => {
    const m: Record<string, number | null> = {}
    pts.forEach(p => { m[p.date] = p[field] })
    return m
  })

  return filtered.map(date => {
    const row: Record<string, number | null> = { date: date as unknown as number }
    symbols.forEach((sym, i) => { row[sym] = flowMaps[i][date] ?? null })
    return row
  })
}

export function StockCompareView({
  symbols, profiles, onClose, onRemove,
}: {
  symbols: string[]
  profiles: Record<string, SymbolProfile>
  onClose: () => void
  onRemove: (symbol: string) => void
  onApplyFilters: (filters: Partial<ScreenerFilters>) => void
  onAddToCompare: (symbol: string) => void
  onToggleWatchlist?: (symbol: string, name: string) => void
  watchlist?: string[]
}) {
  const names: Record<string, string> = {}
  symbols.forEach(sym => { names[sym] = profiles[sym]?.stock.name ?? sym })

  const [period, setPeriod] = useState<ComparePeriod>('1y')
  const [rawData, setRawData] = useState<Record<string, StockChartPoint[]>>({})
  const [loading, setLoading] = useState(true)

  // 데이터 fetch — 모든 기간 한 번에 (all 기준)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setRawData({})
    Promise.all(symbols.map(sym =>
      screenerApi.stockChart(sym, 'all').catch(() => [] as StockChartPoint[])
    )).then(results => {
      if (cancelled) return
      const map: Record<string, StockChartPoint[]> = {}
      symbols.forEach((sym, i) => { map[sym] = results[i] })
      setRawData(map)
      setLoading(false)
    }).catch(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [symbols.join(',')])

  const results = symbols.map(sym => rawData[sym] ?? [])
  const { rows: priceRows, startDate } = useMemo(
    () => buildNormalizedRows(symbols, results, period),
    [rawData, period, symbols.join(',')]
  )
  const foreignRows = useMemo(
    () => buildFlowRows(symbols, results, period, 'foreign_net'),
    [rawData, period, symbols.join(',')]
  )
  const instRows = useMemo(
    () => buildFlowRows(symbols, results, period, 'inst_net'),
    [rawData, period, symbols.join(',')]
  )
  const hasFlow = symbols.some((sym) => {
    const d = rawData[sym] ?? []
    return d.some(p => p.foreign_net != null || p.inst_net != null)
  })

  const PERIODS: { key: ComparePeriod; label: string }[] = [
    { key: '60d', label: '60일' },
    { key: '120d', label: '120일' },
    { key: '1y', label: '1년' },
    { key: 'all', label: '전체' },
  ]

  const tooltipStyle = { backgroundColor: '#1e293b', border: '1px solid #374151', borderRadius: 8 }
  const labelStyle  = { color: '#94a3b8', fontSize: 11 }

  const commonLineProps = {
    dot: false as const,
    strokeWidth: 2,
    connectNulls: true,
  }

  // 지표 비교 테이블
  const METRICS: { key: keyof ScreenerStock; label: string; fmt: (v: number) => string; lower?: boolean }[] = [
    { key: 'per_pit',       label: 'PER',    fmt: v => v.toFixed(1), lower: true },
    { key: 'pbr_pit',       label: 'PBR',    fmt: v => v.toFixed(2), lower: true },
    { key: 'rsi_14',        label: 'RSI',    fmt: v => v.toFixed(0) },
    { key: 'vol_ratio_20d', label: '거래량비', fmt: v => `${v.toFixed(1)}x` },
    { key: 'atr_pct',       label: 'ATR%',   fmt: v => `${v.toFixed(1)}%`, lower: true },
    { key: 'foreign_rate',  label: '외국인%', fmt: v => `${v.toFixed(1)}%` },
    { key: 'dividend_yield',label: '배당%',  fmt: v => `${v.toFixed(1)}%` },
    { key: 'score_60d',     label: '60d점수', fmt: v => v.toFixed(3) },
  ]

  return (
    <div className="fixed inset-0 z-50 bg-slate-950 overflow-y-auto flex flex-col">

      {/* ── 헤더 ── */}
      <div className="sticky top-0 z-10 bg-slate-950/95 border-b border-gray-800/60 px-4 py-2.5 flex items-center gap-3 backdrop-blur shrink-0">
        <span className="text-white font-bold text-sm">종목 비교</span>
        {/* 범례 칩 */}
        <div className="flex items-center gap-2">
          {symbols.map((sym, i) => (
            <span key={sym} className="flex items-center gap-1.5 text-xs">
              <span className="w-3 h-0.5 rounded-full inline-block" style={{ backgroundColor: OVERLAY_COLORS[i], height: 3 }} />
              <span style={{ color: OVERLAY_COLORS[i] }}>{names[sym]}</span>
              <button onClick={() => onRemove(sym)} className="text-gray-600 hover:text-gray-400 leading-none ml-0.5">×</button>
            </span>
          ))}
        </div>
        <div className="flex-1" />
        {/* 기간 선택 */}
        <div className="flex items-center gap-1 bg-gray-900 border border-gray-800 rounded-lg p-0.5">
          {PERIODS.map(p => (
            <button
              key={p.key}
              onClick={() => setPeriod(p.key)}
              className={`text-xs px-2.5 py-1 rounded transition-colors ${
                period === p.key
                  ? 'bg-violet-500/30 text-violet-300 border border-violet-500/40'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >{p.label}</button>
          ))}
        </div>
        <button onClick={onClose} className="text-gray-400 hover:text-white text-xs px-3 py-1.5 rounded border border-gray-700 hover:border-gray-500 transition-colors">
          ✕ 닫기
        </button>
      </div>

      {loading ? (
        <div className="flex-1 flex items-center justify-center text-gray-500 text-sm">차트 데이터 로딩 중…</div>
      ) : (
        <div className="flex-1 min-h-0 flex flex-col gap-3 p-3">

          {/* ① 정규화 가격 오버레이 — 화면 50%+ */}
          <div className="shrink-0 bg-gray-900/70 border border-gray-800/60 rounded-xl p-3" style={{ minHeight: '45vh' }}>
            <div className="flex items-center justify-between mb-2">
              <span className="text-gray-300 text-xs font-medium">정규화 가격 비교 (기준: {fmtDateLong(startDate)} = 100)</span>
            </div>
            {priceRows.length === 0 ? (
              <div className="flex items-center justify-center h-48 text-gray-600 text-sm">공통 거래일 없음</div>
            ) : (
              <ResponsiveContainer width="100%" height={Math.max(window.innerHeight * 0.42, 260)}>
                <LineChart data={priceRows} margin={{ top: 4, right: 16, left: -8, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis dataKey="date" tickFormatter={fmtDate} tick={{ fill: '#6b7280', fontSize: 10 }}
                    interval="preserveStartEnd" minTickGap={70} />
                  <YAxis tick={{ fill: '#6b7280', fontSize: 10 }} tickFormatter={(v: number) => v.toFixed(0)}
                    domain={['auto', 'auto']} />
                  <RechartTooltip
                    contentStyle={tooltipStyle} labelStyle={labelStyle} itemStyle={{ fontSize: 12 }}
                    formatter={(value: unknown, name: unknown) => {
                      const v = typeof value === 'number' ? value.toFixed(1) : '—'
                      const n = typeof name === 'string' ? (names[name] ?? name) : String(name)
                      return [v, n]
                    }}
                    labelFormatter={(l: unknown) => fmtDateLong(String(l))}
                  />
                  <Legend formatter={(v: string) => <span style={{ color: '#d1d5db', fontSize: 12 }}>{names[v] ?? v}</span>} />
                  {symbols.map((sym, i) => (
                    <Line key={sym} type="monotone" dataKey={sym}
                      stroke={OVERLAY_COLORS[i % OVERLAY_COLORS.length]} {...commonLineProps} />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>

          {/* ② 수급 비교 — 외국인 / 기관 */}
          {hasFlow && (
            <div className="grid grid-cols-2 gap-3 shrink-0">
              {[
                { rows: foreignRows, label: '외국인 순매수 (억원)', key: 'foreign' },
                { rows: instRows,    label: '기관 순매수 (억원)',  key: 'inst'    },
              ].map(({ rows, label, key }) => (
                <div key={key} className="bg-gray-900/70 border border-gray-800/60 rounded-xl p-3">
                  <span className="text-gray-400 text-xs font-medium">{label}</span>
                  {rows.length === 0 ? (
                    <div className="flex items-center justify-center h-28 text-gray-600 text-xs">데이터 없음</div>
                  ) : (
                    <ResponsiveContainer width="100%" height={160}>
                      <LineChart data={rows} margin={{ top: 4, right: 8, left: -16, bottom: 4 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                        <XAxis dataKey="date" tickFormatter={fmtDate} tick={{ fill: '#6b7280', fontSize: 9 }}
                          interval="preserveStartEnd" minTickGap={60} />
                        <YAxis tick={{ fill: '#6b7280', fontSize: 9 }} tickFormatter={(v: number) => v.toFixed(0)} />
                        <RechartTooltip
                          contentStyle={tooltipStyle} labelStyle={labelStyle} itemStyle={{ fontSize: 11 }}
                          formatter={(value: unknown, name: unknown) => {
                            const v = typeof value === 'number' ? value.toFixed(1) : '—'
                            const n = typeof name === 'string' ? (names[name] ?? name) : String(name)
                            return [v, n]
                          }}
                          labelFormatter={(l: unknown) => fmtDateLong(String(l))}
                        />
                        {symbols.map((sym, i) => (
                          <Line key={sym} type="monotone" dataKey={sym}
                            stroke={OVERLAY_COLORS[i % OVERLAY_COLORS.length]} {...commonLineProps} />
                        ))}
                      </LineChart>
                    </ResponsiveContainer>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* ③ 지표 비교 테이블 */}
          <div className="bg-gray-900/70 border border-gray-800/60 rounded-xl overflow-auto shrink-0">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-gray-800">
                  <th className="text-left px-3 py-2 text-gray-500 font-normal w-24">지표</th>
                  {symbols.map((sym, i) => (
                    <th key={sym} className="px-3 py-2 text-center font-semibold"
                      style={{ color: OVERLAY_COLORS[i] }}>
                      {names[sym]}<br />
                      <span className="text-gray-600 font-mono font-normal text-[10px]">{sym}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {METRICS.map(({ key, label, fmt, lower }) => {
                  const vals = symbols.map(sym => {
                    const v = profiles[sym]?.stock[key]
                    return typeof v === 'number' ? v : null
                  })
                  const numericVals = vals.filter((v): v is number => v != null)
                  const best = numericVals.length > 0
                    ? (lower ? Math.min(...numericVals) : Math.max(...numericVals))
                    : null

                  return (
                    <tr key={key} className="border-b border-gray-800/50 hover:bg-gray-800/20">
                      <td className="px-3 py-2 text-gray-500">{label}</td>
                      {vals.map((v, i) => {
                        const isBest = v != null && v === best
                        return (
                          <td key={i} className={`px-3 py-2 text-center tabular-nums ${
                            isBest ? 'font-bold' : 'text-gray-300'
                          }`} style={isBest ? { color: OVERLAY_COLORS[i] } : {}}>
                            {v != null ? fmt(v) : '—'}
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
                {/* 현재가 행 */}
                <tr className="border-b border-gray-800/50">
                  <td className="px-3 py-2 text-gray-500">현재가</td>
                  {symbols.map((sym, i) => {
                    const c = profiles[sym]?.stock.close
                    return (
                      <td key={i} className="px-3 py-2 text-center text-gray-200 tabular-nums">
                        {c != null ? c.toLocaleString() + '원' : '—'}
                      </td>
                    )
                  })}
                </tr>
                {/* 시장 */}
                <tr>
                  <td className="px-3 py-2 text-gray-500">시장</td>
                  {symbols.map((sym, i) => (
                    <td key={i} className="px-3 py-2 text-center text-gray-400"
                      style={{ color: OVERLAY_COLORS[i] }}>
                      {profiles[sym]?.stock.market ?? '—'}
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
          </div>

        </div>
      )}
    </div>
  )
}

function SectorTreemap({
  sectors, period, compareMode = false, periodB = 'combined_60d', onStockSelect,
  onPeriodChange, onToggleCompare, onPeriodBChange, onApplyFilters, initialDrilldown,
}: {
  sectors: SectorFlowResponse['sectors']
  period: PeriodKey
  compareMode?: boolean
  periodB?: PeriodKey
  onStockSelect?: (symbol: string) => void
  onPeriodChange?: (p: PeriodKey) => void
  onToggleCompare?: () => void
  onPeriodBChange?: (p: PeriodKey) => void
  onApplyFilters?: (filters: Partial<ScreenerFilters>) => void
  initialDrilldown?: string | null
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [tmWidth, setTmWidth] = useState(0)
  // null = 섹터 전체 뷰, sector code = 해당 섹터 드릴다운 뷰
  const [drilldown, setDrilldown] = useState<string | null>(null)
  const prevInitialDrilldownRef = useRef<string | null | undefined>(undefined)
  useEffect(() => {
    if (initialDrilldown != null && initialDrilldown !== prevInitialDrilldownRef.current) {
      setDrilldown(initialDrilldown)
    }
    // 매크로로 돌아갔다가 같은 섹터를 다시 선택해도 드릴다운을 적용한다.
    prevInitialDrilldownRef.current = initialDrilldown
  }, [initialDrilldown])
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [fsW, setFsW] = useState(0)
  const [fsH, setFsH] = useState(0)
  const [clickedProfile, setClickedProfile] = useState<SymbolProfile | null>(null)
  const [clickedProfileLoading, setClickedProfileLoading] = useState(false)

  // 전체화면: 크기 추적 + ESC 닫기
  useEffect(() => {
    if (!isFullscreen) return
    const updateSize = () => {
      setFsW(window.innerWidth)
      setFsH(window.innerHeight - 56) // 56px 헤더 높이
    }
    updateSize()
    window.addEventListener('resize', updateSize)
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setIsFullscreen(false) }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('resize', updateSize)
      window.removeEventListener('keydown', onKey)
    }
  }, [isFullscreen])

  const handleDrilldownStockClick = (symbol: string) => {
    onStockSelect?.(symbol)
    setClickedProfile(null)
    setClickedProfileLoading(true)
    screenerApi.symbolProfile(symbol)
      .then(p => setClickedProfile(p))
      .catch(() => setClickedProfile(null))
      .finally(() => setClickedProfileLoading(false))
  }

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const init = Math.floor(el.getBoundingClientRect().width)
    if (init > 0) setTmWidth(init)
    const ro = new ResizeObserver(entries => {
      const w = entries[0]?.contentRect.width
      if (w > 0) setTmWidth(Math.floor(w))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const drilldownEntry = drilldown ? sectors.find(s => s.code === drilldown) ?? null : null

  // 비교 모드: 각 패널 너비 (4px 간격 제외)
  const halfW = compareMode ? Math.floor((tmWidth - 4) / 2) : 0
  // 전체화면 비교 모드: 각 패널 너비 / 레이블 행(28px) 제외한 타일 높이
  const fsHalfW = (compareMode && isFullscreen && fsW > 0) ? Math.floor((fsW - 4) / 2) : 0
  const fsContentH = (compareMode && isFullscreen && fsH > 0) ? Math.max(4, fsH - 28) : fsH

  // 단일 모드 섹터 타일
  const sectorTiles = useMemo((): _TmTile[] => {
    if (compareMode) return []
    return _computeSectorTiles(sectors, period, tmWidth, _TM_H)
  }, [sectors, period, tmWidth, compareMode])

  // 비교 모드 타일 A / B
  const sectorTilesA = useMemo((): _TmTile[] => {
    if (!compareMode) return []
    return _computeSectorTiles(sectors, period, halfW, _TM_H)
  }, [sectors, period, halfW, compareMode])

  const sectorTilesB = useMemo((): _TmTile[] => {
    if (!compareMode) return []
    return _computeSectorTiles(sectors, periodB, halfW, _TM_H)
  }, [sectors, periodB, halfW, compareMode])

  const stockTiles = useMemo((): _TmTile[] => {
    if (tmWidth < 4 || !drilldownEntry) return []
    const crossSet = new Set(drilldownEntry.cross_stocks.map(cs => cs.symbol))
    const items: _TmItem[] = drilldownEntry.stocks
      .map(st => ({
        code: st.symbol,
        name: st.name,
        val: _stockFlowForPeriod(st, period),
        hasCross: crossSet.has(st.symbol),
      }))
      .filter(i => Math.abs(i.val) > 0)
    if (!items.length) return []
    return computeSquarifiedTreemap(items, tmWidth, _TM_H)
  }, [drilldownEntry, tmWidth, period])

  const drilldownTotal = drilldownEntry
    ? drilldownEntry.stocks.reduce((s, st) => s + _stockFlowForPeriod(st, period), 0)
    : 0

  // 전체화면 단일 모드 타일
  const fsSectorTiles = useMemo((): _TmTile[] => {
    if (!isFullscreen || compareMode) return []
    return _computeSectorTiles(sectors, period, fsW, fsH)
  }, [sectors, period, fsW, fsH, isFullscreen, compareMode])

  // 전체화면 비교 모드 타일 A / B
  const fsSectorTilesA = useMemo((): _TmTile[] => {
    if (!isFullscreen || !compareMode) return []
    return _computeSectorTiles(sectors, period, fsHalfW, fsContentH)
  }, [sectors, period, fsHalfW, fsContentH, isFullscreen, compareMode])

  const fsSectorTilesB = useMemo((): _TmTile[] => {
    if (!isFullscreen || !compareMode) return []
    return _computeSectorTiles(sectors, periodB, fsHalfW, fsContentH)
  }, [sectors, periodB, fsHalfW, fsContentH, isFullscreen, compareMode])

  const fsStockTiles = useMemo((): _TmTile[] => {
    if (!isFullscreen || fsW < 4 || fsH < 4 || !drilldownEntry) return []
    const crossSet = new Set(drilldownEntry.cross_stocks.map(cs => cs.symbol))
    const items: _TmItem[] = drilldownEntry.stocks
      .map(st => ({
        code: st.symbol,
        name: st.name,
        val: _stockFlowForPeriod(st, period),
        hasCross: crossSet.has(st.symbol),
      }))
      .filter(i => Math.abs(i.val) > 0)
    if (!items.length) return []
    return computeSquarifiedTreemap(items, fsW, fsH)
  }, [drilldownEntry, period, fsW, fsH, isFullscreen])

  return (
    <div ref={containerRef} className="w-full relative">
      {/* 드릴다운 헤더 (비교 모드에서는 비활성) */}
      {!compareMode && drilldown && drilldownEntry ? (
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <button
            onClick={() => { setDrilldown(null); setClickedProfile(null) }}
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 transition-colors border border-gray-700 hover:border-gray-500 rounded px-2 py-0.5"
          >
            ← 전체 섹터로 돌아가기
          </button>
          <span className="text-sm font-semibold text-gray-100">{drilldownEntry.name}</span>
          <span className="text-xs text-gray-500">{drilldownEntry.stocks.length}종목</span>
          <span className={`text-xs font-medium ${drilldownTotal >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            총 {drilldownTotal >= 0 ? '+' : ''}{Math.round(drilldownTotal).toLocaleString()}억
          </span>
        </div>
      ) : null}

      {/* 트리맵 SVG + 전체화면 버튼 */}
      <div style={{ height: compareMode ? _TM_H + 26 : _TM_H }} className="relative">
        {compareMode ? (
          /* 비교 모드: 2패널 (기간A 왼쪽 / 기간B 오른쪽) */
          tmWidth > 0 && halfW > 0 && (
            <div className="flex gap-1">
              {/* 패널 A */}
              <div style={{ width: halfW }}>
                <div className="flex items-center gap-1 mb-0.5 px-1">
                  <span className="text-[10px] font-semibold text-emerald-400 truncate">기간 A</span>
                </div>
                <svg width={halfW} height={_TM_H} style={{ display: 'block' }}>
                  <_TmSvg
                    tiles={sectorTilesA}
                    onTileClick={() => {}}
                    isOther={code => code === '__other__'}
                    svgW={halfW}
                    svgH={_TM_H}
                  />
                </svg>
              </div>
              {/* 패널 B */}
              <div style={{ width: halfW }}>
                <div className="flex items-center gap-1 mb-0.5 px-1">
                  <span className="text-[10px] font-semibold text-sky-400 truncate">기간 B</span>
                </div>
                <svg width={halfW} height={_TM_H} style={{ display: 'block' }}>
                  <_TmSvg
                    tiles={sectorTilesB}
                    onTileClick={() => {}}
                    isOther={code => code === '__other__'}
                    svgW={halfW}
                    svgH={_TM_H}
                  />
                </svg>
              </div>
            </div>
          )
        ) : (
          /* 단일 모드 */
          tmWidth > 0 && (
            <svg width={tmWidth} height={_TM_H} style={{ display: 'block' }}>
              {drilldown ? (
                stockTiles.length > 0 ? (
                  <_TmSvg
                    tiles={stockTiles}
                    onTileClick={tile => handleDrilldownStockClick(tile.code)}
                    isOther={() => false}
                    svgW={tmWidth}
                    svgH={_TM_H}
                  />
                ) : (
                  <text x={tmWidth / 2} y={_TM_H / 2} textAnchor="middle" dominantBaseline="middle"
                    fontSize={13} fill="rgba(156,163,175,0.8)">
                    수급 데이터가 없습니다
                  </text>
                )
              ) : (
                <_TmSvg
                  tiles={sectorTiles}
                  onTileClick={tile => setDrilldown(tile.code)}
                  isOther={code => code === '__other__'}
                  svgW={tmWidth}
                  svgH={_TM_H}
                />
              )}
            </svg>
          )
        )}
        {/* 전체화면 버튼 */}
        <button
          onClick={() => setIsFullscreen(true)}
          title="전체화면으로 보기"
          className={`absolute ${compareMode ? 'top-8' : 'top-2'} right-2 z-10 flex items-center justify-center w-7 h-7 rounded bg-gray-900/70 hover:bg-gray-700/90 border border-gray-600/50 hover:border-gray-400/60 text-gray-400 hover:text-gray-100 transition-all backdrop-blur-sm`}
          style={{ fontSize: 14, lineHeight: 1 }}
        >
          ⛶
        </button>
      </div>

      {clickedProfile && !clickedProfileLoading && (
        <StockProfileCard
          profile={clickedProfile}
          onClose={() => setClickedProfile(null)}
          onApplyFilters={onApplyFilters ?? (() => {})}
          onAddToCompare={() => {}}
          inCompareList={false}
        />
      )}

      {/* 전체화면 오버레이 */}
      {isFullscreen && (
        <div className="fixed inset-0 z-[300] bg-gray-950 flex flex-col">
          {/* 헤더 바 */}
          <div className="flex items-center gap-3 px-4 shrink-0 border-b border-gray-800 flex-wrap" style={{ minHeight: 56 }}>
            {/* 왼쪽: 제목 / 드릴다운 정보 */}
            <div className="flex items-center gap-2 min-w-0 flex-1">
              {drilldown && drilldownEntry ? (
                <>
                  <button
                    onClick={() => { setDrilldown(null); setClickedProfile(null) }}
                    className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 border border-gray-700 hover:border-gray-500 rounded px-2 py-1 shrink-0 transition-colors"
                  >
                    ← 전체 섹터
                  </button>
                  <span className="text-sm font-semibold text-gray-100 truncate">{drilldownEntry.name}</span>
                  <span className="text-xs text-gray-500 shrink-0">{drilldownEntry.stocks.length}종목</span>
                  <span className={`text-xs font-medium shrink-0 ${drilldownTotal >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                    총 {drilldownTotal >= 0 ? '+' : ''}{Math.round(drilldownTotal).toLocaleString()}억
                  </span>
                </>
              ) : (
                <span className="text-sm font-semibold text-gray-100">섹터 자금흐름 트리맵</span>
              )}
            </div>
            {/* 가운데: 기간 컨트롤 (드릴다운 상태가 아닐 때만) */}
            {!drilldown && onPeriodChange && (
              <div className="flex items-center gap-2 text-xs flex-wrap shrink-0">
                <span className="text-gray-500">{compareMode ? '기간 A:' : '기준 기간:'}</span>
                <select
                  value={period}
                  onChange={e => onPeriodChange(e.target.value as PeriodKey)}
                  className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-gray-300"
                >
                  <option value="combined_5d">최근 5일</option>
                  <option value="combined_20d">최근 20일</option>
                  <option value="combined_60d">최근 60일</option>
                  <option value="combined_120d">최근 120일</option>
                  <option value="combined_250d">최근 1년(250일)</option>
                </select>
                {onToggleCompare && (
                  <button
                    onClick={onToggleCompare}
                    className={`px-2.5 py-1 rounded border text-xs font-medium transition-colors ${
                      compareMode
                        ? 'bg-sky-500/20 border-sky-500/50 text-sky-300 hover:bg-sky-500/30'
                        : 'bg-gray-800 border-gray-700 text-gray-400 hover:bg-gray-700 hover:text-gray-200'
                    }`}
                  >
                    비교
                  </button>
                )}
                {compareMode && onPeriodBChange && (
                  <>
                    <span className="text-gray-500 shrink-0">기간 B:</span>
                    <select
                      value={periodB}
                      onChange={e => onPeriodBChange(e.target.value as PeriodKey)}
                      className="bg-gray-800 border border-sky-700/50 rounded px-2 py-1 text-sky-300"
                    >
                      <option value="combined_5d">최근 5일</option>
                      <option value="combined_20d">최근 20일</option>
                      <option value="combined_60d">최근 60일</option>
                      <option value="combined_120d">최근 120일</option>
                      <option value="combined_250d">최근 1년(250일)</option>
                    </select>
                  </>
                )}
              </div>
            )}
            {/* 오른쪽: 닫기 버튼 */}
            <button
              onClick={() => setIsFullscreen(false)}
              title="전체화면 닫기 (ESC)"
              className="shrink-0 flex items-center justify-center w-8 h-8 rounded hover:bg-gray-800 text-gray-400 hover:text-gray-100 transition-colors text-lg leading-none"
            >
              ✕
            </button>
          </div>

          {/* 전체화면 트리맵 */}
          <div className="flex-1 overflow-hidden relative">
            {fsW > 0 && (
              compareMode ? (
                /* 전체화면 비교 모드: 레이블 행 + 2패널 */
                fsHalfW > 0 && (
                  <div className="flex flex-col h-full">
                    {/* 레이블 행 28px */}
                    <div className="flex shrink-0" style={{ height: 28 }}>
                      <div className="flex items-center gap-1 px-2" style={{ width: fsHalfW }}>
                        <span className="text-xs font-semibold text-emerald-400">기간 A</span>
                        <span className="text-xs text-emerald-300/70">{SECTOR_PERIOD_LABELS[period]}</span>
                      </div>
                      <div className="flex items-center gap-1 px-2" style={{ width: fsHalfW }}>
                        <span className="text-xs font-semibold text-sky-400">기간 B</span>
                        <span className="text-xs text-sky-300/70">{SECTOR_PERIOD_LABELS[periodB]}</span>
                      </div>
                    </div>
                    {/* 트리맵 패널 행 */}
                    <div className="flex gap-1" style={{ height: fsContentH }}>
                      <svg width={fsHalfW} height={fsContentH} style={{ display: 'block' }}>
                        <_TmSvg
                          tiles={fsSectorTilesA}
                          onTileClick={() => {}}
                          isOther={code => code === '__other__'}
                          svgW={fsHalfW}
                          svgH={fsContentH}
                        />
                      </svg>
                      <svg width={fsHalfW} height={fsContentH} style={{ display: 'block' }}>
                        <_TmSvg
                          tiles={fsSectorTilesB}
                          onTileClick={() => {}}
                          isOther={code => code === '__other__'}
                          svgW={fsHalfW}
                          svgH={fsContentH}
                        />
                      </svg>
                    </div>
                  </div>
                )
              ) : (
                <svg width={fsW} height={fsH} style={{ display: 'block' }}>
                  {drilldown ? (
                    fsStockTiles.length > 0 ? (
                      <_TmSvg
                        tiles={fsStockTiles}
                        onTileClick={tile => handleDrilldownStockClick(tile.code)}
                        isOther={() => false}
                        svgW={fsW}
                        svgH={fsH}
                      />
                    ) : (
                      <text x={fsW / 2} y={fsH / 2} textAnchor="middle" dominantBaseline="middle"
                        fontSize={14} fill="rgba(156,163,175,0.8)">
                        수급 데이터가 없습니다
                      </text>
                    )
                  ) : (
                    <_TmSvg
                      tiles={fsSectorTiles}
                      onTileClick={tile => setDrilldown(tile.code)}
                      isOther={code => code === '__other__'}
                      svgW={fsW}
                      svgH={fsH}
                    />
                  )}
                </svg>
              )
            )}
          </div>
        </div>
      )}

    </div>
  )
}

// ── 메인 페이지 ───────────────────────────────────────────────

export default function ScreenerPage({
  embedded = false,
  initialDrilldown = null,
  externalFilters = null,
  onExternalFiltersApplied,
  refetchTick,
  onStockSelect,
  onAddToCompare: externalAddToCompare,
  compareList: externalCompareList,
  watchlist: externalWatchlist,
  onToggleWatchlist: externalToggleWatchlist,
}: {
  embedded?: boolean
  initialDrilldown?: string | null
  externalFilters?: Record<string, boolean> | null
  onExternalFiltersApplied?: () => void
  refetchTick?: number
  onStockSelect?: (symbol: string) => void
  onAddToCompare?: (symbol: string) => void
  compareList?: string[]
  watchlist?: string[]
  onToggleWatchlist?: (symbol: string, name: string) => void
} = {}) {
  const [filters, setFilters] = useState<Record<string, boolean>>({})
  const [sortBy, setSortBy] = useState('symbol')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const [regime, setRegime] = useState<ScreenerRegime | null>(null)
  const [stocks, setStocks] = useState<ScreenerStock[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selfWatchlist, setSelfWatchlist] = useState<string[]>(() => getWatchlist())
  const watchlist = externalWatchlist ?? selfWatchlist
  const [screenerDate, setScreenerDate] = useState<string | null>(null)

  // 교차 분석 모드
  const [crossMode, setCrossMode] = useState(false)
  const [crossData, setCrossData] = useState<CrossAnalysisResponse | null>(null)
  const [crossLoading, setCrossLoading] = useState(false)
  const [crossError, setCrossError] = useState<string | null>(null)
  const [crossSortBy, setCrossSortBy] = useState<'combo_count' | 'score_60d' | 'dividend_yield'>('combo_count')

  // 섹터 흐름 모드
  const [sectorMode, setSectorMode] = useState(false)
  const [sectorData, setSectorData] = useState<SectorFlowResponse | null>(null)
  const [sectorLoading, setSectorLoading] = useState(false)
  const [sectorError, setSectorError] = useState<string | null>(null)
  const [expandedSector, setExpandedSector] = useState<string | null>(null)
  const [expandedSectorStocks, setExpandedSectorStocks] = useState<string | null>(null)
  const [sectorSortPeriod, setSectorSortPeriod] = useState<PeriodKey>('combined_5d')
  const [sectorCompareMode, setSectorCompareMode] = useState(false)
  const [sectorSortPeriodB, setSectorSortPeriodB] = useState<PeriodKey>('combined_60d')
  const sectorTreemapRef = useRef<HTMLDivElement>(null)

  // 매크로에서 전달한 섹터를 기존 섹터 흐름 화면으로 연다.
  useEffect(() => {
    if (!initialDrilldown) return
    setSectorMode(true)
    setCrossMode(false)
  }, [initialDrilldown])

  // 버튼과 외부 섹터 선택이 같은 데이터 로딩 경로를 사용한다.
  useEffect(() => {
    if (!sectorMode || sectorData) return
    let cancelled = false
    setSectorLoading(true)
    setSectorError(null)
    screenerApi.sectorFlow()
      .then(res => { if (!cancelled) setSectorData(res) })
      .catch((e: Error) => { if (!cancelled) setSectorError(e.message) })
      .finally(() => { if (!cancelled) setSectorLoading(false) })
    return () => { cancelled = true }
  }, [sectorMode, sectorData])

  useEffect(() => {
    if (!initialDrilldown || !sectorMode || !sectorData || sectorLoading) return
    sectorTreemapRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [initialDrilldown, sectorMode, sectorData, sectorLoading])

  // ③ 종목 쏠림 설명 토글
  const [showConcentrationInfo, setShowConcentrationInfo] = useState(false)

  // 섹터 흐름 종합 분석
  const [analysisOpen, setAnalysisOpen] = useState(false)
  const analysisSectionRef = useRef<HTMLDivElement>(null)
  const [analysisData, setAnalysisData] = useState<import('../api/aiRecommend').SectorFlowAnalysisResponse | null>(null)
  const [analysisLoading, setAnalysisLoading] = useState(false)
  const [analysisError, setAnalysisError] = useState<string | null>(null)
  const [analysisActiveTrades, setAnalysisActiveTrades] = useState<ActiveTrade[]>([])

  // 종합 분석 패널이 열려 있을 때 기간 변경 시 자동 re-fetch
  useEffect(() => {
    if (!analysisOpen) return
    const p = sectorSortPeriod.replace('combined_', '')
    setAnalysisLoading(true)
    setAnalysisError(null)
    screenerApi.sectorFlowAnalysis(p)
      .then(res => setAnalysisData(res))
      .catch((e: Error) => setAnalysisError(e.message))
      .finally(() => setAnalysisLoading(false))
  }, [analysisOpen, sectorSortPeriod])

  // 종목 프로파일 검색 (standalone 전용 — embedded 시 StockExplorePage가 담당)
  const [profile, setProfile] = useState<SymbolProfile | null>(null)
  const [profileLoading, setProfileLoading] = useState(false)

  // 종목 비교 기능 (내부 상태 — externalAddToCompare 없을 때 사용)
  const [selfCompareList, setSelfCompareList] = useState<string[]>([])
  const [selfCompareNames, setSelfCompareNames] = useState<Record<string, string>>({})
  const [selfCompareProfiles, setSelfCompareProfiles] = useState<Record<string, SymbolProfile>>({})
  const [selfCompareViewOpen, setSelfCompareViewOpen] = useState(false)

  // 실효 비교 상태: 외부 제공 시 외부, 아니면 내부
  const compareList = externalCompareList ?? selfCompareList

  const handleSelfAddToCompare = async (symbol: string) => {
    if (selfCompareList.includes(symbol)) {
      setSelfCompareList(prev => prev.filter(s => s !== symbol))
      return
    }
    if (selfCompareList.length >= 3) return // 최대 3개
    setSelfCompareList(prev => [...prev, symbol])
    if (selfCompareProfiles[symbol]) {
      setSelfCompareNames(prev => ({ ...prev, [symbol]: selfCompareProfiles[symbol].stock.name }))
      return
    }
    const existing = profile?.stock.symbol === symbol ? profile : null
    if (existing) {
      setSelfCompareProfiles(prev => ({ ...prev, [symbol]: existing }))
      setSelfCompareNames(prev => ({ ...prev, [symbol]: existing.stock.name }))
      return
    }
    try {
      const p = await screenerApi.symbolProfile(symbol)
      setSelfCompareProfiles(prev => ({ ...prev, [symbol]: p }))
      setSelfCompareNames(prev => ({ ...prev, [symbol]: p.stock.name }))
    } catch {
      // 실패 시 심볼만 표시
    }
  }

  const handleAddToCompare = externalAddToCompare ?? handleSelfAddToCompare

  const handleRemoveFromCompare = (symbol: string) => {
    setSelfCompareList(prev => prev.filter(s => s !== symbol))
    if (selfCompareList.length <= 1) setSelfCompareViewOpen(false)
  }

  const handleClearCompare = () => {
    setSelfCompareList([])
    setSelfCompareNames({})
    setSelfCompareProfiles({})
    setSelfCompareViewOpen(false)
  }

  // 외부에서 필터를 주입받아 적용 (StockExplorePage 프로파일 카드 → "이 조건으로 검색")
  const prevExternalRef = useRef(externalFilters)
  const scrollAfterSearchRef = useRef(false)
  const screenerResultsRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (externalFilters && externalFilters !== prevExternalRef.current) {
      prevExternalRef.current = externalFilters
      setFilters(externalFilters)
      setProfile(null)
      scrollAfterSearchRef.current = true
      onExternalFiltersApplied?.()
    }
  }, [externalFilters, onExternalFiltersApplied])

  const [selected, setSelected] = useState<{ symbol: string; name: string } | null>(null)
  const [detail, setDetail] = useState<TickerDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    screenerApi.search({ ...filters, sort_by: sortBy, sort_dir: sortDir, limit: 100 })
      .then(res => {
        if (cancelled) return
        setRegime(res.market_regime)
        setStocks(res.stocks)
        setTotal(res.total)
        setScreenerDate(res.date)
      })
      .catch((e: Error) => { if (!cancelled) setError(e.message) })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
          if (scrollAfterSearchRef.current) {
            scrollAfterSearchRef.current = false
            setTimeout(() => {
              screenerResultsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
            }, 100)
          }
        }
      })
    return () => { cancelled = true }
  }, [filters, sortBy, sortDir, refetchTick])

  const toggleFilter = (key: string) => {
    setFilters(prev => ({ ...prev, [key]: !prev[key] }))
  }

  const handleToggleCrossMode = () => {
    if (!crossMode) {
      setCrossMode(true)
      setSectorMode(false)
      if (!crossData) {
        setCrossLoading(true)
        setCrossError(null)
        screenerApi.crossAnalysis()
          .then(res => { setCrossData(res) })
          .catch((e: Error) => { setCrossError(e.message) })
          .finally(() => { setCrossLoading(false) })
      }
    } else {
      setCrossMode(false)
    }
  }

  const handleToggleSectorMode = () => {
    if (!sectorMode) {
      setSectorMode(true)
      setCrossMode(false)
    } else {
      setSectorMode(false)
    }
  }

  const sortedCrossStocks = useMemo(() => {
    if (!crossData) return []
    return [...crossData.stocks].sort((a, b) => {
      // 1차: 조합수 내림차순
      if (b.combo_count !== a.combo_count) return b.combo_count - a.combo_count
      // 2차: 선택한 기준 내림차순 (null은 마지막)
      if (crossSortBy === 'score_60d') {
        const bv = b.score_60d ?? -Infinity
        const av = a.score_60d ?? -Infinity
        return bv - av
      }
      if (crossSortBy === 'dividend_yield') {
        const bv = b.actual_yield ?? b.dividend_yield ?? -Infinity
        const av = a.actual_yield ?? a.dividend_yield ?? -Infinity
        return bv - av
      }
      // combo_count 선택 시 → 배당수익률 내림차순 (백엔드 기본값과 동일)
      const bv = b.dividend_yield ?? -Infinity
      const av = a.dividend_yield ?? -Infinity
      return bv - av
    })
  }, [crossData, crossSortBy])

  const applyPreset = (preset: ComboPreset) => {
    setFilters(prev => {
      const next = { ...prev }
      FILTERS.forEach(f => { next[f.key] = false })
      preset.keys.forEach(k => { next[k] = true })
      return next
    })
    // 3-필터 조합 → 60d 스코어 내림차순, 2-필터 조합 → 배당수익률 내림차순
    setSortBy(preset.keys.length >= 3 ? 'score_60d' : 'dividend_yield')
    setSortDir('desc')
  }

  const handleStockSelect = async (result: StockSearchResult) => {
    setProfile(null)
    setProfileLoading(true)
    try {
      const p = await screenerApi.symbolProfile(result.symbol)
      setProfile(p)
    } catch {
      // 404 등 → 프로파일 없음 (에러 표시 생략, 검색 드롭다운에서 걸러짐)
    } finally {
      setProfileLoading(false)
    }
  }

  const handleSymbolClick = (symbol: string) => {
    if (onStockSelect) {
      onStockSelect(symbol)
    } else {
      handleStockSelect({ symbol, name: '', market: '' })
    }
  }

  const handleApplyProfileFilters = (newFilters: Partial<ScreenerFilters>) => {
    setFilters(_prev => {
      const next: Record<string, boolean> = {}
      FILTERS.forEach(f => { next[f.key] = false })
      Object.entries(newFilters).forEach(([k, v]) => { if (v) next[k] = true })
      return next
    })
    setProfile(null)
  }

  const openAnalysisProfile = (symbol: string) => {
    onStockSelect?.(symbol)
  }

  const handleToggleStar = (symbol: string, name?: string) => {
    if (externalToggleWatchlist) {
      externalToggleWatchlist(symbol, name ?? symbol)
    } else {
      toggleWatchlist(symbol)
      setSelfWatchlist(getWatchlist())
    }
  }

  // 임의 조합 경고 — 검증된 프리셋과 일치하지 않고 시장국면 조건부 필터 2개+ 선택 시
  const activeFilterKeys = Object.entries(filters).filter(([, v]) => v).map(([k]) => k)
  const isPresetMatch = COMBO_PRESETS.some(p =>
    p.keys.length === activeFilterKeys.length &&
    p.keys.every(k => activeFilterKeys.includes(k))
  )
  const conditionalActiveCount = FILTERS.filter(f => f.badge === 'conditional' && !!filters[f.key]).length
  const showArbitraryComboWarning = conditionalActiveCount >= 2 && !isPresetMatch

  const openDetail = async (stock: ScreenerStock) => {
    setSelected({ symbol: stock.symbol, name: stock.name })
    setDetail(null)
    setDetailError(null)
    setDetailLoading(true)
    try {
      const d = await aiApi.ticker(stock.symbol, 60)
      setDetail(d)
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : '조회 실패')
    } finally {
      setDetailLoading(false)
    }
  }

  return (
    <div className={embedded ? '' : 'min-h-screen bg-slate-950'}>
      {/* 상단 내비 — embedded 시 StockExplorePage가 대신 렌더 */}
      {!embedded && (
      <div className="sticky top-0 z-30 bg-slate-950/90 backdrop-blur border-b border-gray-800/60">
        <div className="max-w-6xl mx-auto px-4 h-12 flex items-center gap-3">
          <Link to="/" className="text-gray-400 hover:text-white transition-colors p-1 -ml-1">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </Link>
          <h1 className="text-white font-bold text-sm">종목 스크리너</h1>
          <div className="h-4 w-px bg-gray-700" />
          <span className="text-sky-400 text-xs font-medium">검증된 조건으로 검색</span>
        </div>
      </div>
      )}

      <div className={embedded ? 'max-w-6xl mx-auto px-4 pt-4 pb-6 space-y-5' : 'max-w-6xl mx-auto px-4 py-6 space-y-5'}>
        {/* DataUpdateBanner / RegimeBanner / 검색창 — embedded 시 StockExplorePage가 대신 렌더 */}
        {!embedded && <DataUpdateBanner initialDate={screenerDate} />}
        {!embedded && <RegimeBanner regime={regime} />}

        {error && (
          <div className="bg-gray-900 border border-red-500/30 rounded-xl p-4">
            <p className="text-red-400 text-sm">서버 연결 실패: {error}</p>
          </div>
        )}

        {/* 종목 검색 + 프로파일 카드 — standalone 전용 */}
        {!embedded && <StockSearchBox onSelect={handleStockSelect} />}

        {!embedded && profileLoading && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-6 text-center text-gray-500 text-sm">
            종목 정보 조회 중...
          </div>
        )}
        {!embedded && profile && !profileLoading && (
          <StockProfileCard
            profile={profile}
            onClose={() => setProfile(null)}
            onApplyFilters={handleApplyProfileFilters}
            onAddToCompare={handleAddToCompare}
            inCompareList={compareList.includes(profile.stock.symbol)}
          />
        )}

        {/* 사용 가이드 */}
        <div className="bg-gray-900/50 border border-gray-700/60 rounded-xl p-4 space-y-2">
          <p className="text-gray-300 text-xs font-semibold">📋 스크리너 활용 가이드</p>
          <div className="space-y-1.5 text-xs text-gray-400 leading-relaxed">
            <p>① <span className="text-emerald-400 font-medium">검증된 조합</span>을 기본으로 사용하세요 — 43조합(36 기본+7 60d조합) 워크포워드 OOS 검증에서 단독 필터 대비 유의하게 우수한 6개 조합을 제공합니다. <span className="text-teal-400 font-medium">(3조건)</span> 표시는 60d 모델 스코어를 추가한 3-필터 조합입니다.</p>
            <p>② 조건을 많이 추가할수록 검색 결과가 급감하고 <span className="text-yellow-400">검증 범위를 벗어납니다</span> — 위 프리셋 외의 임의 조합은 성과 예측이 어렵습니다.</p>
            <p>③ 태그 의미: <span className="text-yellow-400">⚠️ 시장국면 조건부</span> = 약세장·횡보장에서만 유효, 강세장에서 역효과 · <span className="text-teal-400">📈 60d 검증</span> = 중장기(60일) 기준 검증 · <span className="text-slate-400">🔧 품질/유동성</span> = 기준값 필터</p>
            <p>④ <span className="text-orange-400 font-medium">배당수익률 10%+</span> 종목은 특별배당·차등배당·주가급락 착시 가능성이 있으므로 개별 확인 권장. '특별'·'병합환산' 뱃지가 없어도 일회성일 수 있습니다.</p>
            <p>⑤ <span className="text-sky-400 font-medium">배당수익률</span>은 배당기준일 주가 대비 실질 수익률입니다. 회색 <span className="text-gray-400">'현재가 X.X%'</span>는 현재 주가 기준 참고값이며, 주가 하락 시 과장될 수 있습니다.</p>
          </div>
        </div>

        {/* 추천 조합 프리셋 */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-gray-500 text-xs">추천 조합 <span className="text-gray-600">(OOS 초과승률 순):</span></span>
          {COMBO_PRESETS.map(preset => (
            <BadgeTooltip key={preset.label} text={preset.tooltip}>
              <button
                onClick={() => applyPreset(preset)}
                className="text-xs px-2.5 py-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors"
              >
                ✅ {preset.label} <span className="text-emerald-300/60 ml-0.5">{preset.oosRate}</span>
              </button>
            </BadgeTooltip>
          ))}
          <BadgeTooltip text="검증된 6개 조합을 동시에 실행해 2개 이상에 공통 포함된 종목을 조합 수 기준 내림차순으로 표시합니다. 여러 조합에서 선택된 종목일수록 다양한 검증 기준을 통과한 종목입니다.">
            <button
              onClick={handleToggleCrossMode}
              className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${crossMode ? 'border-violet-400/60 bg-violet-500/20 text-violet-300' : 'border-violet-500/30 bg-violet-500/10 text-violet-400 hover:bg-violet-500/20'}`}
            >
              🔀 교차 분석{crossMode ? ' ▲' : ''}
            </button>
          </BadgeTooltip>
          <button
            onClick={handleToggleSectorMode}
            className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${sectorMode ? 'border-purple-400/60 bg-purple-500/20 text-purple-300' : 'border-purple-500/30 bg-purple-500/10 text-purple-400 hover:bg-purple-500/20'}`}
          >
            🏭 섹터 흐름{sectorMode ? ' ▲' : ''}
          </button>
        </div>

        {/* 필터 패널 — 교차·섹터 모드 OFF일 때만 표시 */}
        {!crossMode && !sectorMode && (
          <>
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mb-3">
                <p className="text-gray-400 text-xs font-medium">조건 선택 (체크한 조건을 모두 만족하는 종목, AND 조합)</p>
                <span className="text-yellow-400/70 text-xs">⚠️ 시장국면 조건부 = 약세장·횡보장 유효, 강세장 역효과</span>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                {FILTERS.map(f => (
                  <BadgeTooltip key={f.key} text={f.tooltip}>
                    <label className="flex items-center gap-2 px-2.5 py-2 rounded-lg bg-gray-800/50 hover:bg-gray-800 cursor-pointer transition-colors">
                      <input
                        type="checkbox"
                        checked={!!filters[f.key]}
                        onChange={() => toggleFilter(f.key)}
                        className="accent-sky-500"
                      />
                      <span className="text-gray-200 text-xs flex-1">{f.label}</span>
                      <FilterBadge badge={f.badge} />
                    </label>
                  </BadgeTooltip>
                ))}
              </div>
            </div>

            {/* 임의 조합 경고 */}
            {showArbitraryComboWarning && (
              <div className="bg-yellow-500/8 border border-yellow-500/25 rounded-xl px-4 py-3 flex items-start gap-2.5">
                <span className="text-yellow-400 text-sm mt-0.5 shrink-0">⚠️</span>
                <p className="text-yellow-300/80 text-xs leading-relaxed">
                  <span className="font-medium text-yellow-300">검증되지 않은 조합 — 표본 부족 가능.</span>{' '}
                  선택한 조건 조합은 워크포워드 검증을 거치지 않아 성과 예측이 어렵습니다.
                  위의 <span className="text-emerald-400">✅ 검증된 조합</span> 버튼을 사용하는 것을 권장합니다.
                </p>
              </div>
            )}

            {/* 정렬 */}
            <div className="flex items-center gap-3 text-xs">
              <span className="text-gray-500">정렬:</span>
              <select
                value={sortBy} onChange={e => setSortBy(e.target.value)}
                className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-gray-200"
              >
                <option value="symbol">종목코드</option>
                <option value="per">PER</option>
                <option value="pbr">PBR</option>
                <option value="rsi_14">RSI</option>
                <option value="dividend_yield">배당수익률 (실질 기준)</option>
                <option value="vol_ratio_20d">거래량비율</option>
                <option value="ret_20d">20일수익률</option>
                <option value="score_60d">60d 모델 스코어</option>
              </select>
              <button
                onClick={() => setSortDir(d => d === 'asc' ? 'desc' : 'asc')}
                className="px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300 hover:bg-gray-700"
              >
                {sortDir === 'asc' ? '오름차순 ▲' : '내림차순 ▼'}
              </button>
              <span className="text-gray-500 ml-auto">{loading ? '검색 중...' : `${total}개 종목`}</span>
            </div>

            {/* 결과 테이블 */}
            <div ref={screenerResultsRef} className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-gray-800 text-gray-400 text-xs">
                      <th className="px-3 py-2.5"></th>
                      <th className="text-left px-3 py-2.5 font-medium">종목</th>
                      <th className="text-right px-3 py-2.5 font-medium">현재가</th>
                      <th className="text-right px-3 py-2.5 font-medium">PER</th>
                      <th className="text-right px-3 py-2.5 font-medium">PBR</th>
                      <th className="text-right px-3 py-2.5 font-medium">RSI</th>
                      <th className="text-right px-3 py-2.5 font-medium">
                        <span
                          className="cursor-help border-b border-dashed border-gray-500"
                          title={"실질 배당수익률 = 직전 연도 주당배당금(DPS) ÷ 배당기준일 주가.\nDART 사업보고서 기준, biz_year 2024년 이후 최신 연도 적용.\n회색 '현재가 X.X%'는 같은 DPS를 오늘 주가로 나눈 참고값."}
                        >배당 ⓘ</span>
                      </th>
                      <th className="text-right px-3 py-2.5 font-medium">거래량비</th>
                      <th className="text-right px-3 py-2.5 font-medium">20일수익률</th>
                      <th className="text-right px-3 py-2.5 font-medium text-teal-400/80">
                        <span
                          className="cursor-help border-b border-dotted border-teal-400/50"
                          title={"60d 모델이 예측한 60거래일 후 전체 종목 횡단면 중앙값 대비 +7%p 이상 초과수익 달성 확률.\n" +
                            "벤치마크: 당일 전체 상장 종목 수익률 중앙값 (KOSPI 지수 아님).\n" +
                            "CatBoost(80%) + XGBoost(20%) 가중 평균 앙상블 — 보정 없는 raw 확률.\n" +
                            "전체 종목 분포 기준 상위 20%(≥80th percentile) / 상위 10%(≥90th percentile)로 스크리너 필터 판정.\n" +
                            "피처: 기본 45개 + EPS성장·배당성장·ROE·BPS성장·neg_PBR 5개 PIT 팩터."}
                        >60d 스코어 ⓘ</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {!loading && stocks.length === 0 && (
                      <tr><td colSpan={10} className="text-center py-10 text-gray-500 text-sm">조건에 맞는 종목이 없습니다</td></tr>
                    )}
                    {stocks.map(s => (
                      <StockRow
                        key={s.symbol}
                        stock={s}
                        starred={watchlist.some(w => w === s.symbol)}
                        onToggleStar={() => handleToggleStar(s.symbol, s.name)}
                        onClick={() => openDetail(s)}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}

        {/* 교차 분석 결과 — crossMode ON일 때만 표시 */}
        {crossMode && (
          <div className="space-y-3">
            {/* 헤더 */}
            <div className="bg-violet-500/10 border border-violet-500/25 rounded-xl px-4 py-3">
              <p className="text-violet-300 text-xs font-semibold mb-1">🔀 교차 분석 — 검증된 6개 조합 공통 종목</p>
              <p className="text-violet-300/70 text-xs">
                6개 조합을 동시에 실행해 <span className="text-violet-200 font-medium">2개 이상</span>에 포함된 종목만 표시합니다.
                여러 조합에서 선택될수록 서로 다른 검증 기준을 중복 통과한 종목입니다. 필터 체크박스는 이 뷰에 영향을 주지 않습니다.
              </p>
            </div>

            {crossLoading && (
              <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-10 text-center text-gray-500 text-sm">
                교차 분석 중...
              </div>
            )}
            {crossError && (
              <div className="bg-red-500/10 border border-red-500/30 rounded-xl px-4 py-4 text-red-400 text-sm">
                오류: {crossError}
              </div>
            )}
            {crossData && !crossLoading && (
              <>
                <div className="flex items-center justify-between text-xs text-gray-500 px-1">
                  <span>
                    총 <span className="text-violet-300 font-semibold">{crossData.total}개</span> 종목이 2개 이상 조합에 포함됨
                  </span>
                  <span>{crossData.date} 기준</span>
                </div>

                {/* 조합 범례 */}
                <div className="flex flex-wrap gap-1.5 text-xs">
                  {crossData.presets.map((p, i) => (
                    <span key={i} className="px-2 py-0.5 rounded-full bg-gray-800 border border-gray-700 text-gray-300">
                      <span className="text-violet-400 font-medium">{i + 1}번</span> {p.label} <span className="text-gray-500">{p.oos_rate}</span>
                    </span>
                  ))}
                </div>

                {/* 교차 분석 정렬 */}
                <div className="flex items-center gap-2 text-xs px-1">
                  <span className="text-gray-500">정렬:</span>
                  <select
                    value={crossSortBy}
                    onChange={e => setCrossSortBy(e.target.value as 'combo_count' | 'score_60d' | 'dividend_yield')}
                    className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-gray-200"
                  >
                    <option value="combo_count">조합수</option>
                    <option value="score_60d">60d 모델 스코어</option>
                    <option value="dividend_yield">배당수익률(실질)</option>
                  </select>
                  <span className="text-gray-600 ml-1">동일 조합수 내 2차 정렬 기준</span>
                </div>

                {/* 교차 분석 테이블 */}
                <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-gray-800 text-gray-400 text-xs">
                          <th className="text-left px-3 py-2.5 font-medium">종목</th>
                          <th className="text-right px-3 py-2.5 font-medium">현재가</th>
                          <th className="text-right px-3 py-2.5 font-medium">
                            <span className="cursor-help border-b border-dashed border-gray-500" title="배당기준일 주가 대비 DPS 실질 배당수익률">배당수익률</span>
                          </th>
                          <th className="text-right px-3 py-2.5 font-medium">PER</th>
                          <th className="text-right px-3 py-2.5 font-medium">PBR</th>
                          <th className="text-right px-3 py-2.5 font-medium text-teal-400/80">
                            <span className="cursor-help border-b border-dotted border-teal-400/50" title={"60거래일 후 전체 종목 횡단면 중앙값 대비 +7%p 이상 초과수익 달성 확률.\n" + "벤치마크: 당일 전체 상장 종목 수익률 중앙값 (KOSPI 지수 아님).\n" + "CatBoost(80%) + XGBoost(20%) 앙상블 — 보정 없는 raw 확률."}>60d 스코어 ⓘ</span>
                          </th>
                          <th className="text-center px-3 py-2.5 font-medium text-violet-300">
                            <span className="cursor-help border-b border-dashed border-violet-400/50" title="포함된 조합 수 / 6개 전체 조합">조합수</span>
                          </th>
                          <th className="text-left px-3 py-2.5 font-medium text-violet-300/70">포함 조합 (1~6번)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sortedCrossStocks.length === 0 && (
                          <tr>
                            <td colSpan={8} className="text-center py-10 text-gray-500 text-sm">
                              2개 이상 조합에 동시에 포함된 종목이 없습니다
                            </td>
                          </tr>
                        )}
                        {sortedCrossStocks.map(s => (
                          <tr key={s.symbol} className="border-b border-gray-800/60 hover:bg-gray-800/30 transition-colors cursor-pointer" onClick={() => handleSymbolClick(s.symbol)}>
                            <td className="px-3 py-2.5">
                              <div className="font-medium text-gray-100 text-sm hover:underline">{s.name}</div>
                              <div className="text-gray-500 text-xs">{s.symbol} · {s.market}</div>
                            </td>
                            <td className="px-3 py-2.5 text-right text-gray-200 text-xs font-mono">
                              {s.close != null ? s.close.toLocaleString() + '원' : '—'}
                            </td>
                            <td className="px-3 py-2.5 text-right text-xs">
                              {s.actual_yield != null
                                ? <span className="text-emerald-400 font-medium">{s.actual_yield.toFixed(1)}%</span>
                                : s.dividend_yield != null
                                  ? <span className="text-emerald-400 font-medium">{s.dividend_yield.toFixed(1)}%</span>
                                  : <span className="text-gray-600">—</span>}
                            </td>
                            <td className="px-3 py-2.5 text-right text-xs text-gray-300">
                              {s.per != null ? s.per.toFixed(1) : '—'}
                            </td>
                            <td className="px-3 py-2.5 text-right text-xs text-gray-300">
                              {s.pbr != null ? s.pbr.toFixed(2) : '—'}
                            </td>
                            <td className="px-3 py-2.5 text-right text-xs text-teal-400">
                              {s.score_60d != null ? (s.score_60d * 100).toFixed(1) + '%' : '—'}
                            </td>
                            <td className="px-3 py-2.5 text-center">
                              <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${s.combo_count >= 4 ? 'bg-violet-500/30 text-violet-200' : s.combo_count >= 3 ? 'bg-violet-500/20 text-violet-300' : 'bg-violet-500/10 text-violet-400'}`}>
                                {s.combo_count}/6
                              </span>
                            </td>
                            <td className="px-3 py-2.5">
                              <div className="flex gap-1 flex-wrap">
                                {s.combo_flags.map((flag, i) => (
                                  <span key={i} className={`text-xs px-1.5 py-0.5 rounded ${flag ? 'bg-emerald-500/20 text-emerald-400 font-medium' : 'text-gray-600'}`}>
                                    {i + 1}번{flag ? '✅' : '—'}
                                  </span>
                                ))}
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </>
            )}
          </div>
        )}

        {/* 섹터 흐름 결과 — sectorMode ON일 때만 표시 */}
        {sectorMode && (
          <div className="space-y-3">
            {/* 헤더 */}
            <div className="bg-purple-500/10 border border-purple-500/25 rounded-xl px-4 py-3">
              <div className="flex items-start justify-between gap-3 flex-wrap">
                <div>
                  <p className="text-purple-300 text-xs font-semibold">🏭 섹터별 자금흐름 — 외국인·기관 순매수(억원)</p>
                  <p className="text-purple-300/60 text-xs mt-0.5">KSIC 소분류(3자리) 기준 집계 · 표시 전용 — 모델·추천 순위와 무관합니다.</p>
                </div>
                <div className="flex items-center gap-2 text-xs flex-wrap">
                  <button
                    onClick={() => {
                      const opening = !analysisOpen
                      setAnalysisOpen(opening)
                      if (opening) {
                        if (analysisActiveTrades.length === 0) {
                          paperTradingApi.getActive()
                            .then(trades => { setAnalysisActiveTrades(trades) })
                            .catch(() => {})
                        }
                        setTimeout(() => {
                          analysisSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                        }, 50)
                      }
                    }}
                    className="px-2.5 py-1 rounded border border-yellow-500/40 bg-yellow-500/10 text-yellow-300 hover:bg-yellow-500/20 transition-colors"
                  >
                    {analysisOpen ? '📊 종합 분석 접기' : '📊 종합 분석'}
                  </button>
                  <span className="text-gray-500 shrink-0">{sectorCompareMode ? '기간 A:' : '기준 기간:'}</span>
                  <select
                    value={sectorSortPeriod}
                    onChange={e => setSectorSortPeriod(e.target.value as PeriodKey)}
                    className="bg-gray-800 border border-gray-700 rounded px-2 py-1 text-gray-300"
                  >
                    <option value="combined_5d">최근 5일</option>
                    <option value="combined_20d">최근 20일</option>
                    <option value="combined_60d">최근 60일</option>
                    <option value="combined_120d">최근 120일</option>
                    <option value="combined_250d">최근 1년(250일)</option>
                  </select>
                  {/* 비교 모드 토글 */}
                  <button
                    onClick={() => setSectorCompareMode(m => !m)}
                    className={`px-2.5 py-1 rounded border text-xs font-medium transition-colors ${
                      sectorCompareMode
                        ? 'bg-sky-500/20 border-sky-500/50 text-sky-300 hover:bg-sky-500/30'
                        : 'bg-gray-800 border-gray-700 text-gray-400 hover:bg-gray-700 hover:text-gray-200'
                    }`}
                  >
                    비교
                  </button>
                  {sectorCompareMode && (
                    <>
                      <span className="text-gray-500 shrink-0">기간 B:</span>
                      <select
                        value={sectorSortPeriodB}
                        onChange={e => setSectorSortPeriodB(e.target.value as PeriodKey)}
                        className="bg-gray-800 border border-sky-700/50 rounded px-2 py-1 text-sky-300"
                      >
                        <option value="combined_5d">최근 5일</option>
                        <option value="combined_20d">최근 20일</option>
                        <option value="combined_60d">최근 60일</option>
                        <option value="combined_120d">최근 120일</option>
                        <option value="combined_250d">최근 1년(250일)</option>
                      </select>
                    </>
                  )}
                </div>
              </div>
              {sectorData && (
                <>
                  {sectorData.coverage && (
                    <p className="text-gray-500 text-xs mt-1.5">
                      {sectorData.coverage.sector_count}개 섹터,&nbsp;
                      {sectorData.coverage.symbols_in_sectors.toLocaleString()}종목 커버
                      &nbsp;(전체 {sectorData.coverage.total_universe.toLocaleString()}종목 중&nbsp;
                      {Math.round(sectorData.coverage.symbols_in_sectors / sectorData.coverage.total_universe * 100)}%
                      &nbsp;— ETF·우선주 등 제외)
                    </p>
                  )}
                  <p className="text-gray-600 text-xs mt-1">⚠️ {sectorData.pension_note}</p>
                </>
              )}
            </div>

            {sectorData && !sectorLoading && (
              <div ref={sectorTreemapRef} className="scroll-mt-16 rounded-xl overflow-hidden border border-gray-800 bg-gray-900/60 p-2">
                <p className="text-xs text-gray-500 px-1 mb-2">
                  박스 크기 = 자금 규모, 초록 = 유입, 빨강 = 유출. 클릭하면 상세 보기.
                </p>
                <SectorTreemap
                  initialDrilldown={initialDrilldown}
                  sectors={sectorData.sectors}
                  period={sectorSortPeriod}
                  compareMode={sectorCompareMode}
                  periodB={sectorSortPeriodB}
                  onStockSelect={onStockSelect}
                  onPeriodChange={setSectorSortPeriod}
                  onToggleCompare={() => setSectorCompareMode(m => !m)}
                  onPeriodBChange={setSectorSortPeriodB}
                  onApplyFilters={handleApplyProfileFilters}
                />
              </div>
            )}

            {/* 섹터 자금흐름 종합 분析 — 인라인 접이식 섹션 */}
            {analysisOpen && (
              <div ref={analysisSectionRef} className="mt-4 bg-gray-900 border border-gray-700 rounded-2xl p-6 space-y-5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h2 className="text-white font-bold text-base">📊 섹터 자금흐름 종합 분析</h2>
                    {analysisData && (
                      <div className="mt-1">
                        <span
                          className="text-xs bg-blue-900/50 text-blue-300 border border-blue-700/50 rounded px-2 py-0.5 cursor-help"
                          title="KRX 분류 기준 기관계(inst)는 연기금을 포함한 기관 전체 합산입니다. 화면의 연기금(pension) 항목은 기관계 내 세부 내역입니다."
                        >
                          📡 외국인+기관계 순매수 기준
                        </span>
                      </div>
                    )}
                  </div>
                  <button onClick={() => setAnalysisOpen(false)} className="text-gray-500 hover:text-white text-lg leading-none shrink-0 mt-0.5">✕</button>
                </div>

                {/* 분析 본문 */}
                <div>
                  {analysisLoading && (
                    <div className="py-10 text-center text-gray-400 text-sm">분析 중...</div>
                  )}
                  {analysisError && (
                    <div className="py-6 text-center text-red-400 text-sm">{analysisError}</div>
                  )}

                  {analysisData && !analysisLoading && (() => {
                    const ms = analysisData.market_summary
                    const sc = analysisData.sector_classification
                    const cc = analysisData.concentration
                    const ca = analysisData.cross_analysis
                    const colorMap: Record<string, string> = {
                      green: 'text-emerald-400',
                      red:   'text-red-400',
                      orange:'text-orange-400',
                      gray:  'text-gray-400',
                    }
                    const bgMap: Record<string, string> = {
                      green: 'border-emerald-500/30 bg-emerald-500/10',
                      red:   'border-red-500/30 bg-red-500/10',
                      orange:'border-orange-500/30 bg-orange-500/10',
                      gray:  'border-gray-600/30 bg-gray-700/20',
                    }
                    const fmtBillion = (v: number) =>
                      v === 0 ? '0억' : `${v >= 0 ? '+' : ''}${Math.round(v).toLocaleString()}억`
                    const periodKeys = ['5d', '20d', '60d', '120d', '250d'] as const
                    return (
                      <>
                        {/* 1. 시장 전체 방향 */}
                        <div className={`border rounded-xl p-4 ${bgMap[ms.direction_color] ?? bgMap.gray}`}>
                          <p className="text-sm text-gray-400 font-semibold mb-2">① 시장 전체 자금 흐름</p>
                          <p className={`text-lg font-bold mb-3 ${colorMap[ms.direction_color] ?? ''}`}>
                            {ms.direction_label}
                          </p>
                          <div className="space-y-2">
                            {(() => {
                              const maxAbs = Math.max(1, ...periodKeys.map(k => Math.abs(ms.totals[k])))
                              return periodKeys.map(k => {
                                const v = ms.totals[k]
                                const barPct = Math.round(Math.abs(v) / maxAbs * 100)
                                const isPos = v >= 0
                                return (
                                  <div key={k} className="flex items-center gap-2 text-sm">
                                    <span className="text-gray-500 w-14 shrink-0 text-right">{ms.period_labels[k]}</span>
                                    <div className="flex-1 h-3 bg-gray-800 rounded-full overflow-hidden">
                                      <div
                                        className={`h-full rounded-full transition-all ${isPos ? 'bg-emerald-500' : 'bg-red-500'}`}
                                        style={{ width: `${barPct}%` }}
                                      />
                                    </div>
                                    <span className={`font-mono w-28 text-right shrink-0 ${isPos ? 'text-emerald-400' : 'text-red-400'}`}>
                                      {fmtBillion(v)}
                                    </span>
                                  </div>
                                )
                              })
                            })()}
                          </div>
                        </div>

                        {/* 2. 섹터 포지셔닝 — 4사분면 카드 레이아웃 */}
                        <div className="border border-gray-700 rounded-xl p-4">
                          <p className="text-sm text-gray-400 font-semibold mb-3">② 섹터 포지셔닝</p>
                          {(() => {
                            const quadDefs = [
                              { key: 'consistent_inflow'  as const, label: '🟢 일관 유입',  sub: '60d+ · 5d+', fg: '#34d399', bg: 'rgba(52,211,153,0.08)'  },
                              { key: 'short_reversal'     as const, label: '🟠 단기 전환',  sub: '60d− · 5d+', fg: '#fb923c', bg: 'rgba(251,146,60,0.08)'  },
                              { key: 'short_exit'         as const, label: '🟡 단기 이탈',  sub: '60d+ · 5d−', fg: '#fbbf24', bg: 'rgba(251,191,36,0.08)'  },
                              { key: 'consistent_outflow' as const, label: '🔴 일관 유출',  sub: '60d− · 5d−', fg: '#f87171', bg: 'rgba(248,113,113,0.08)' },
                            ]
                            const totalCount = quadDefs.reduce((n, q) => n + sc[q.key].length, 0)
                            if (totalCount === 0) return <p className="text-xs text-gray-600">분류된 섹터 없음</p>
                            const fmt = (v: number) => {
                              const s = v >= 0 ? '+' : ''
                              const a = Math.abs(v)
                              if (a >= 10000) return `${s}${(v / 10000).toFixed(1)}조`
                              if (a >= 1000)  return `${s}${(v / 1000).toFixed(0)}천억`
                              return `${s}${v.toFixed(0)}억`
                            }
                            return (
                              <div className="grid grid-cols-2 gap-3">
                                {quadDefs.map(({ key, label, sub, fg, bg }) => {
                                  const list = [...sc[key]].sort((a, b) => Math.abs(b.combined_5d) - Math.abs(a.combined_5d))
                                  const max5d = Math.max(...list.map(s => Math.abs(s.combined_5d)), 1)
                                  return (
                                    <div key={key} className="rounded-lg p-3"
                                         style={{ background: bg, border: `1px solid ${fg}33` }}>
                                      <div className="flex items-center justify-between mb-0.5">
                                        <span className="text-sm font-semibold" style={{ color: fg }}>{label}</span>
                                        <span className="text-xs text-gray-500">{list.length}개 섹터</span>
                                      </div>
                                      <p className="text-xs text-gray-600 mb-2">{sub}</p>
                                      {list.length === 0 ? (
                                        <p className="text-xs text-gray-600">해당 없음</p>
                                      ) : (
                                        <div className="space-y-1.5 max-h-52 overflow-y-auto pr-0.5">
                                          {list.map(s => {
                                            const barW = Math.round((Math.abs(s.combined_5d) / max5d) * 100)
                                            const pos  = s.combined_5d >= 0
                                            return (
                                              <div key={s.code}
                                                   style={s.has_cross_stocks
                                                     ? { borderLeft: '2px solid #a78bfa', paddingLeft: 5 }
                                                     : {}}>
                                                <div className="flex items-center gap-1 text-xs leading-tight">
                                                  <span className="flex-1 truncate text-gray-300">{s.name}</span>
                                                  <span className="shrink-0 tabular-nums"
                                                        style={{ color: pos ? '#34d399' : '#f87171' }}>
                                                    5d {fmt(s.combined_5d)}
                                                  </span>
                                                  <span className="shrink-0 tabular-nums text-gray-500">
                                                    60d {fmt(s.combined_60d)}
                                                  </span>
                                                </div>
                                                <div className="mt-0.5 h-1 rounded-full bg-gray-800 overflow-hidden">
                                                  <div className="h-full rounded-full"
                                                       style={{ width: `${barW}%`, background: pos ? '#34d399' : '#f87171', opacity: 0.65 }} />
                                                </div>
                                              </div>
                                            )
                                          })}
                                        </div>
                                      )}
                                    </div>
                                  )
                                })}
                              </div>
                            )
                          })()}
                        </div>

                        {/* 3. 종목 쏠림 분析 */}
                        <div className="border border-gray-700 rounded-xl p-4">
                          <div className="flex items-center gap-1.5 mb-2">
                            <p className="text-sm text-gray-400 font-semibold">
                              ③ 특정 종목 쏠림 (시총 대비 이례적 유입 상위)
                            </p>
                            <button
                              onClick={() => setShowConcentrationInfo(v => !v)}
                              className={`text-xs leading-none cursor-pointer transition-colors shrink-0 ${showConcentrationInfo ? 'text-gray-300' : 'text-gray-600 hover:text-gray-400'}`}
                            >ℹ</button>
                          </div>
                          {showConcentrationInfo && (
                            <div className="mb-3 bg-gray-800/60 border border-gray-700 rounded-lg px-3 py-2.5 text-xs text-gray-300 leading-relaxed space-y-1.5">
                              <p>• <span className="text-gray-100 font-medium">시총대비</span>: 현재 선택된 기준 기간(상단 드롭다운, 예: 최근 5일)의 외국인+기관 순매수 금액 ÷ 시가총액</p>
                              <p>• <span className="text-gray-100 font-medium">평소대비</span>: 같은 기간의 순매수 비율 ÷ 직전 60거래일 평균 순매수 비율. 3배 이상이면 <span className="text-orange-300 font-medium">급증</span> 표시</p>
                              <p>• 시총 대비 비율이 높고 평소 대비 급증한 종목을 자동 선별합니다</p>
                            </div>
                          )}
                          {cc.all.length === 0 ? (
                            <p className="text-sm text-gray-600">해당 섹터 없음</p>
                          ) : (
                            <div className="space-y-1.5 max-h-56 overflow-y-auto">
                              {cc.all.slice(0, 20).map(e => (
                                <div key={e.sector_code} className="flex flex-col gap-0.5 bg-gray-800/40 rounded-lg px-3 py-2 text-sm">
                                  {/* 1행: 섹터명 (전체 표시) */}
                                  <span className="text-gray-500 leading-snug">{e.sector_name}</span>
                                  {/* 2행: 종목명 + 수치 */}
                                  <div className="flex items-center gap-2 flex-wrap">
                                    <button
                                      className={`font-medium hover:underline cursor-pointer ${e.is_concentrated ? 'text-orange-300' : 'text-white'}`}
                                      onClick={() => openAnalysisProfile(e.top1_symbol)}
                                    >{e.top1_name}</button>
                                    <span className="text-teal-400 font-mono shrink-0">시총대비 {e.mktcap_ratio_pct}%</span>
                                    {e.surge_label ? (
                                      <span className={`rounded px-1 py-0.5 font-semibold shrink-0 ${e.surge_label === '급증' ? 'bg-orange-500/20 text-orange-300' : 'bg-blue-500/20 text-blue-300'}`}>
                                        {e.surge_ratio !== null ? `평소대비 ${e.surge_ratio}x ` : ''}{e.surge_label}
                                      </span>
                                    ) : e.surge_ratio !== null ? (
                                      <span className="text-gray-500 shrink-0">평소대비 {e.surge_ratio}x</span>
                                    ) : null}
                                    <span className="text-gray-400 font-mono ml-auto shrink-0">{fmtBillion(e.top1_value)}</span>
                                  </div>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>

                        {/* 4. 교차 분析 연결 */}
                        <div className="border border-gray-700 rounded-xl p-4">
                          <p className="text-sm text-gray-400 font-semibold mb-2">④ 교차 분석 연결</p>
                          <p className="text-sm text-gray-500 mb-3">검증된 조합이 전부 선택한 유망 종목(6/6)이 자금 유입 섹터에 있는지 — 유입 섹터에 있으면 모델 판단과 시장 자금 흐름이 같은 방향.</p>
                          <div className="flex flex-wrap gap-3 text-sm">
                            <div className="bg-gray-800/60 rounded-lg px-3 py-2 text-center">
                              <p className="text-gray-500 text-sm mb-0.5">6/6 종목 수</p>
                              <p className="text-white font-bold">{ca.total_cross_stocks}개</p>
                            </div>
                            <div className={`rounded-lg px-3 py-2 text-center ${ca.cross_in_inflow_sectors > 0 ? 'bg-emerald-500/10 border border-emerald-500/20' : 'bg-gray-800/60'}`}>
                              <p className="text-gray-500 text-sm mb-0.5">일관 유입 섹터 내</p>
                              <p className={`font-bold ${ca.cross_in_inflow_sectors > 0 ? 'text-emerald-400' : 'text-gray-400'}`}>
                                {ca.cross_in_inflow_sectors}개
                              </p>
                            </div>
                            <div className={`rounded-lg px-3 py-2 text-center ${ca.cross_as_sector_top1 > 0 ? 'bg-yellow-500/10 border border-yellow-500/20' : 'bg-gray-800/60'}`}>
                              <p className="text-gray-500 text-sm mb-0.5">섹터 내 자금 1위</p>
                              <p className={`font-bold ${ca.cross_as_sector_top1 > 0 ? 'text-yellow-400' : 'text-gray-400'}`}>
                                {ca.cross_as_sector_top1}개
                              </p>
                            </div>
                            <div className="bg-gray-800/60 rounded-lg px-3 py-2 text-center">
                              <p className="text-gray-500 text-sm mb-0.5">일관 유입 섹터</p>
                              <p className="text-white font-bold">{ca.inflow_sector_count}개</p>
                            </div>
                          </div>
                          {ca.total_cross_stocks > 0 && (
                            <p className="text-sm text-gray-500 mt-2">
                              6/6 종목 {ca.total_cross_stocks}개 중 {ca.cross_in_inflow_sectors}개({Math.round(ca.cross_in_inflow_sectors / ca.total_cross_stocks * 100)}%)가 일관 유입 섹터에 속함.
                              {ca.cross_as_sector_top1 > 0 && ` 그중 ${ca.cross_as_sector_top1}개는 해당 섹터 내 5일 순매수 1위 종목.`}
                            </p>
                          )}
                        </div>

                        {/* ⑤ 총평 — 규칙 기반 자동 생성 */}
                        {(() => {
                          // 1. 시장 진단 한 줄
                          const t5 = ms.totals['5d']
                          const t60 = ms.totals['60d']
                          const t120 = ms.totals['120d']
                          let diagnosis: string
                          if (t5 > 0 && t60 < 0) {
                            diagnosis = '장기 유출 속 단기 반등 — 추세 전환 여부 미확인'
                          } else if (t5 < 0 && t60 > 0) {
                            diagnosis = '장기 유입 흐름 속 단기 이탈'
                          } else if (t5 > 0 && t60 > 0 && t120 > 0) {
                            diagnosis = '전면 유입 지속 — 추세 강화'
                          } else if (t5 > 0 && t60 > 0) {
                            diagnosis = '중기 전환 진행 중 (5d·60d 유입, 장기는 유출)'
                          } else if (t5 < 0 && t60 < 0 && t120 < 0) {
                            diagnosis = '전면 유출 지속'
                          } else if (t5 < 0 && t60 < 0) {
                            diagnosis = '최근 이탈 가속 중'
                          } else {
                            diagnosis = ms.direction_label
                          }

                          // 2. 주목 포인트 (최대 3개)
                          const highlights: string[] = []
                          // (a) 단기전환 중 5d 유입 최대 섹터
                          if (sc.short_reversal.length > 0) {
                            const top = [...sc.short_reversal].sort((a, b) => b.combined_5d - a.combined_5d)[0]
                            if (top.combined_5d > 0) {
                              highlights.push(`단기전환 중 5일 유입 최대: ${top.name} (5d ${fmtBillion(top.combined_5d)})`)
                            }
                          }
                          // (b) 일관유입 중 60d 누적 최대 섹터
                          if (sc.consistent_inflow.length > 0) {
                            const top = [...sc.consistent_inflow].sort((a, b) => b.combined_60d - a.combined_60d)[0]
                            highlights.push(`일관유입 중 장기 누적 최대: ${top.name} (60d ${fmtBillion(top.combined_60d)})`)
                          }
                          // (c) 평소 대비 3배+ 집중 종목
                          const surgeTop = cc.all.filter(e => e.surge_ratio != null && e.surge_ratio >= 3)
                            .sort((a, b) => (b.surge_ratio ?? 0) - (a.surge_ratio ?? 0))[0]
                          if (surgeTop) {
                            highlights.push(`단일 종목 집중 급증 (${surgeTop.surge_label}): ${surgeTop.top1_name} / ${surgeTop.sector_name}`)
                          }

                          // 3. 경고 사항
                          const warnings: string[] = []
                          // (a) 일관유출 규모 상위 섹터
                          if (sc.consistent_outflow.length > 0) {
                            const sorted = [...sc.consistent_outflow].sort((a, b) => a.combined_60d - b.combined_60d)
                            const names = sorted.slice(0, 3).map(e => e.name).join(' · ')
                            warnings.push(`일관유출 섹터: ${names}`)
                          }
                          // (b) 상위 업종 내 사분면 분열 감지 (KSIC 앞 2자리 = 상위 그룹)
                          type QLabel = '유입' | '유출' | '단기전환' | '단기이탈'
                          const parentQs = new Map<string, Set<QLabel>>()
                          const addParent = (code: string, q: QLabel) => {
                            const p = code.slice(0, 2)
                            if (!parentQs.has(p)) parentQs.set(p, new Set())
                            parentQs.get(p)!.add(q)
                          }
                          sc.consistent_inflow.forEach(e => addParent(e.code, '유입'))
                          sc.consistent_outflow.forEach(e => addParent(e.code, '유출'))
                          sc.short_reversal.forEach(e => addParent(e.code, '단기전환'))
                          sc.short_exit.forEach(e => addParent(e.code, '단기이탈'))
                          const splitCodes = [...parentQs.entries()]
                            .filter(([, qs]) => qs.size >= 2)
                            .map(([p]) => p)
                          if (splitCodes.length > 0) {
                            warnings.push(`상위 업종 내 사분면 분열 감지 (KSIC ${splitCodes.slice(0, 4).join(', ')})`)
                          }

                          // 4. 포지션 정합성 (60d 포지션 있을 때만)
                          const trades60d = analysisActiveTrades.filter(t => t.horizon === 60)
                          let positionLine: string | null = null
                          if (trades60d.length > 0) {
                            const inflowCodes = new Set([
                              ...sc.consistent_inflow.map(e => e.code),
                              ...sc.short_reversal.map(e => e.code),
                            ])
                            const matched = trades60d.filter(t => t.sector && inflowCodes.has(t.sector))
                            positionLine = `60d 보유 ${trades60d.length}건 중 ${matched.length}건 유입 섹터 (${trades60d.length - matched.length}건 유출·중립 또는 미확인)`
                          }

                          const hasContent = highlights.length > 0 || warnings.length > 0 || positionLine

                          return (
                            <div className="bg-gray-800/40 rounded-xl p-4 border border-gray-700/50 space-y-3">
                              <h3 className="text-white font-semibold text-sm">⑤ 총평</h3>

                              {/* 시장 진단 */}
                              <div>
                                <p className="text-sm text-gray-500 mb-1">시장 진단</p>
                                <p className="text-sm text-white">{diagnosis}</p>
                              </div>

                              {/* 주목 포인트 */}
                              {highlights.length > 0 && (
                                <div>
                                  <p className="text-sm text-gray-500 mb-1">주목 포인트</p>
                                  <ul className="space-y-1">
                                    {highlights.map((h, i) => (
                                      <li key={i} className="text-sm text-emerald-300 flex gap-1.5 items-start">
                                        <span className="text-emerald-500 mt-0.5 shrink-0">▸</span>
                                        <span>{h}</span>
                                      </li>
                                    ))}
                                  </ul>
                                </div>
                              )}

                              {/* 경고 사항 */}
                              {warnings.length > 0 && (
                                <div>
                                  <p className="text-sm text-gray-500 mb-1">경고 사항</p>
                                  <ul className="space-y-1">
                                    {warnings.map((w, i) => (
                                      <li key={i} className="text-sm text-red-300 flex gap-1.5 items-start">
                                        <span className="text-red-400 mt-0.5 shrink-0">⚠</span>
                                        <span>{w}</span>
                                      </li>
                                    ))}
                                  </ul>
                                </div>
                              )}

                              {/* 포지션 정합성 */}
                              {positionLine && (
                                <div>
                                  <p className="text-sm text-gray-500 mb-1">포지션 정합성</p>
                                  <p className="text-sm text-yellow-300">{positionLine}</p>
                                </div>
                              )}

                              {!hasContent && (
                                <p className="text-sm text-gray-500">섹터 분류 데이터 없음 — 유입·유출 섹터가 감지되지 않음</p>
                              )}
                            </div>
                          )
                        })()}

                        <p className="text-sm text-gray-600">기준일: {analysisData.date} · 표시 전용 — 운용 규칙·모델과 무관</p>
                      </>
                    )
                  })()}
                </div>{/* end 분析 본문 */}
              </div>
            )}

            {sectorLoading && (
              <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-10 text-center text-gray-500 text-sm">
                섹터 자금흐름 집계 중...
              </div>
            )}
            {sectorError && (
              <div className="bg-red-500/10 border border-red-500/30 rounded-xl px-4 py-4 text-red-400 text-sm">
                오류: {sectorError}
              </div>
            )}

            {sectorData && !sectorLoading && (() => {
              const maxAbs = Math.max(1, ...sectorData.sectors.map(s => Math.abs(s[sectorSortPeriod])))
              const sorted = [...sectorData.sectors].sort((a, b) => b[sectorSortPeriod] - a[sectorSortPeriod])
              return (
                <div className="space-y-2">
                  <div className="flex items-center justify-between text-xs text-gray-500 px-1">
                    <span>
                      총 <span className="text-purple-300 font-semibold">{sectorData.sectors.length}개</span> 섹터
                      {sectorData.sectors.filter(s => s.cross_stocks.length > 0).length > 0 && (
                        <span className="ml-2 text-purple-400">● 교차 분석 종목 보유 섹터</span>
                      )}
                    </span>
                    <span>{sectorData.date} 기준</span>
                  </div>
                  {sorted.map(sector => {
                    const isExpanded = expandedSector === sector.code
                    const hasCross = sector.cross_stocks.length > 0
                    const val = sector[sectorSortPeriod] as number
                    const isPos = val >= 0
                    const barPct = Math.min(100, Math.abs(val) / maxAbs * 100)
                    const mom = sector.momentum_5d
                    const comb5d = sector.combined_5d

                    // 모멘텀 레이블: 방향 / 강도만, 숫자 없음
                    let momLabel = ''
                    let momColor = ''
                    if (mom != null) {
                      if (mom < 0) {
                        // 방향 전환 (prior와 current 부호 반대)
                        if (comb5d > 0) {
                          momLabel = '유입 전환 🟢'
                          momColor = 'text-emerald-300 font-medium'
                        } else {
                          momLabel = '유출 전환 🔴'
                          momColor = 'text-red-300 font-medium'
                        }
                      } else if (comb5d >= 0) {
                        // 유입 방향
                        if (mom >= 1.0) {
                          momLabel = '5일 연속 유입 ▲'
                          momColor = 'text-emerald-400'
                        } else {
                          momLabel = '유입 둔화 △'
                          momColor = 'text-emerald-600'
                        }
                      } else {
                        // 유출 방향
                        if (mom > 1.0) {
                          momLabel = '5일 연속 유출 ▼'
                          momColor = 'text-red-400'
                        } else {
                          momLabel = '유출 축소 ▽'
                          momColor = 'text-red-600'
                        }
                      }
                    }

                    // 기간별 요약: 부호+색으로 방향 표현, 화살표 없음
                    const periodFmt = (v: number) =>
                      `${v >= 0 ? '+' : ''}${Math.round(v).toLocaleString()}억`

                    // 카드 배경색: 5d 방향 기준
                    const isPos5d = comb5d >= 0
                    const cardBgClass = isExpanded
                      ? (isPos5d ? 'bg-emerald-950/35' : 'bg-red-950/35')
                      : (isPos5d ? 'bg-emerald-950/25 hover:bg-emerald-950/40' : 'bg-red-950/25 hover:bg-red-950/40')
                    const borderClass = hasCross ? 'border-purple-500/40' : (isPos5d ? 'border-emerald-800/30' : 'border-red-800/30')

                    return (
                      <div
                        key={sector.code}
                        id={`sector-card-${sector.code}`}
                        className={`rounded-xl border transition-colors cursor-pointer ${borderClass} ${cardBgClass}`}
                        onClick={() => setExpandedSector(isExpanded ? null : sector.code)}
                      >
                        <div className="px-4 py-3">
                          <div className="flex items-center gap-3">
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-1.5 flex-wrap mb-1.5">
                                {hasCross && <span className="text-purple-400 text-xs">●</span>}
                                <span className={`text-sm font-medium truncate ${hasCross ? 'text-purple-200' : 'text-gray-200'}`}>
                                  {sector.name}
                                </span>
                                <span className="text-gray-600 text-xs shrink-0">{sector.stock_count}종목</span>
                                {hasCross && (
                                  <span className="text-xs px-1.5 py-0.5 rounded-full bg-purple-500/15 text-purple-400 shrink-0">
                                    교차 {sector.cross_stocks.length}
                                  </span>
                                )}
                              </div>
                              {/* 단방향 바 차트 + 금액 + 모멘텀 */}
                              <div className="flex items-center gap-2">
                                <div className="flex-1 h-1.5 bg-gray-800/60 rounded-full overflow-hidden">
                                  <div
                                    className={`h-full rounded-full transition-all ${isPos ? 'bg-emerald-500' : 'bg-red-500'}`}
                                    style={{ width: `${barPct}%` }}
                                  />
                                </div>
                                <span className={`text-xs font-mono shrink-0 ${isPos ? 'text-emerald-400' : 'text-red-400'}`}>
                                  {isPos ? '+' : ''}{Math.round(val).toLocaleString()}억
                                </span>
                                {momLabel && (
                                  <span className={`text-xs shrink-0 ${momColor}`}>
                                    {momLabel}
                                  </span>
                                )}
                              </div>
                              {/* 기간 요약 (5d / 20d / 60d / 120d / 250d) */}
                              <div className="flex items-center gap-3 mt-1.5 flex-wrap">
                                {([
                                  { label: '5일',   v: sector.combined_5d   },
                                  { label: '20일',  v: sector.combined_20d  },
                                  { label: '60일',  v: sector.combined_60d  },
                                  { label: '120일', v: sector.combined_120d },
                                  { label: '1년',   v: sector.combined_250d },
                                ] as const).map(({ label, v }) => (
                                  <span key={label} className={`text-xs font-mono ${v >= 0 ? 'text-emerald-500/80' : 'text-red-500/80'}`}>
                                    <span className="text-gray-500 mr-0.5">{label}</span>{periodFmt(v)}
                                  </span>
                                ))}
                              </div>
                            </div>
                            <span className="text-gray-600 text-xs shrink-0">{isExpanded ? '▲' : '▼'}</span>
                          </div>
                        </div>

                        {isExpanded && (
                          <div className="border-t border-gray-800 px-4 py-3 space-y-3">
                            {/* 기간별 자금흐름 표 */}
                            <table className="w-full text-xs border-collapse">
                              <thead>
                                <tr className="text-gray-500">
                                  <th className="text-left py-1 pr-3 font-medium w-16"></th>
                                  <th className="text-right py-1 px-2 font-medium">5일</th>
                                  <th className="text-right py-1 px-2 font-medium">20일</th>
                                  <th className="text-right py-1 pl-2 font-medium">60일</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-gray-800/60">
                                {([
                                  { label: '외국인', vals: [sector.foreign_5d, sector.foreign_20d, sector.foreign_60d], color: (v: number) => v >= 0 ? 'text-emerald-400' : 'text-red-400' },
                                  { label: '기관', vals: [sector.inst_5d, sector.inst_20d, sector.inst_60d], color: (v: number) => v >= 0 ? 'text-emerald-400' : 'text-red-400' },
                                  { label: '연기금', vals: [sector.pension_5d, sector.pension_20d, sector.pension_60d], color: (v: number | null) => v == null ? '' : v >= 0 ? 'text-blue-400' : 'text-red-400', isPension: true },
                                ] as const).map(row => (
                                  <tr key={row.label} className="hover:bg-gray-800/20">
                                    <td className="py-1.5 pr-3 text-gray-400 font-medium">{row.label}</td>
                                    {row.vals.map((v, i) => (
                                      <td key={i} className={`text-right py-1.5 px-2 font-mono ${v == null ? 'text-gray-600' : (row.color as (v: number | null) => string)(v)}`}>
                                        {v == null
                                          ? <span title="연기금은 최근 30거래일만 제공됩니다">—</span>
                                          : `${v >= 0 ? '+' : ''}${Math.round(v).toLocaleString()}억`
                                        }
                                      </td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                            {sectorData.pension_latest_date && (
                              <p className="text-gray-600 text-xs">* 연기금 최신 데이터: {sectorData.pension_latest_date} (KIS 30거래일 제공)</p>
                            )}
                            {hasCross && (
                              <div>
                                <p className="text-purple-400 text-xs font-medium mb-2">● 교차 분석 종목 (60d 스코어 순)</p>
                                <div className="flex flex-wrap gap-2">
                                  {sector.cross_stocks.map(s => (
                                    <button key={s.symbol} className="bg-purple-500/10 border border-purple-500/20 rounded-lg px-2.5 py-1.5 text-xs hover:bg-purple-500/20 cursor-pointer text-left" onClick={() => handleSymbolClick(s.symbol)}>
                                      <div className="text-purple-200 font-medium hover:underline">{s.name}</div>
                                      <div className="flex gap-2 text-gray-500 mt-0.5">
                                        <span>{s.symbol}</span>
                                        {s.score_60d != null && <span className="text-teal-400">{(s.score_60d * 100).toFixed(1)}%</span>}
                                        <span className="text-emerald-400">{s.combo_count}/6</span>
                                      </div>
                                    </button>
                                  ))}
                                </div>
                              </div>
                            )}
                            {/* 전체 종목 목록 (접기/펼치기) */}
                            {sector.stocks.length > 0 && (
                              <div>
                                <button
                                  onClick={e => {
                                    e.stopPropagation()
                                    setExpandedSectorStocks(expandedSectorStocks === sector.code ? null : sector.code)
                                  }}
                                  className="text-xs text-gray-400 hover:text-gray-200 flex items-center gap-1"
                                >
                                  <span>{expandedSectorStocks === sector.code ? '▲ 닫기' : `▼ 전체 ${sector.stocks.length}종목 보기`}</span>
                                </button>
                                {expandedSectorStocks === sector.code && (
                                  <div className="mt-2 max-h-64 overflow-y-auto overflow-x-auto">
                                    <table className="min-w-full text-xs border-collapse">
                                      <thead className="sticky top-0 bg-gray-900">
                                        <tr className="text-gray-500 border-b border-gray-800">
                                          <th className="text-left py-1 pl-1 pr-2 font-medium min-w-[8rem]">종목</th>
                                          <th className="text-right py-1 px-2 font-medium whitespace-nowrap">60d 스코어</th>
                                          <th className="text-right py-1 px-2 font-medium whitespace-nowrap">
                                            외국인 {sectorSortPeriod === 'combined_250d' ? '1년' : sectorSortPeriod === 'combined_120d' ? '120d' : sectorSortPeriod === 'combined_60d' ? '60d' : sectorSortPeriod === 'combined_20d' ? '20d' : '5d'}
                                          </th>
                                          <th className="text-right py-1 pl-2 font-medium whitespace-nowrap">
                                            기관 {sectorSortPeriod === 'combined_250d' ? '1년' : sectorSortPeriod === 'combined_120d' ? '120d' : sectorSortPeriod === 'combined_60d' ? '60d' : sectorSortPeriod === 'combined_20d' ? '20d' : '5d'}
                                          </th>
                                        </tr>
                                      </thead>
                                      <tbody className="divide-y divide-gray-800/40">
                                        {sector.stocks.map(st => {
                                          const fVal = sectorSortPeriod === 'combined_250d' ? st.foreign_250d
                                            : sectorSortPeriod === 'combined_120d' ? st.foreign_120d
                                            : sectorSortPeriod === 'combined_60d'  ? st.foreign_60d
                                            : sectorSortPeriod === 'combined_20d'  ? st.foreign_20d
                                            : st.foreign_5d
                                          const iVal = sectorSortPeriod === 'combined_250d' ? st.inst_250d
                                            : sectorSortPeriod === 'combined_120d' ? st.inst_120d
                                            : sectorSortPeriod === 'combined_60d'  ? st.inst_60d
                                            : sectorSortPeriod === 'combined_20d'  ? st.inst_20d
                                            : st.inst_5d
                                          const fmt = (v: number) =>
                                            v === 0 ? '—' : `${v >= 0 ? '+' : ''}${Math.round(v).toLocaleString()}억`
                                          return (
                                            <tr key={st.symbol} className="hover:bg-gray-800/30 cursor-pointer" onClick={() => handleSymbolClick(st.symbol)}>
                                              <td className="py-1 pl-1 pr-2">
                                                <span className="text-gray-200 hover:underline">{st.name}</span>
                                                <span className="text-gray-600 ml-1.5">{st.symbol}</span>
                                              </td>
                                              <td className="text-right py-1 px-2 font-mono">
                                                {st.score_60d != null
                                                  ? <span className="text-teal-400">{(st.score_60d * 100).toFixed(1)}%</span>
                                                  : <span className="text-gray-600">—</span>
                                                }
                                              </td>
                                              <td className={`text-right py-1 px-2 font-mono ${fVal >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                                {fmt(fVal)}
                                              </td>
                                              <td className={`text-right py-1 pl-2 font-mono ${iVal >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                                                {fmt(iVal)}
                                              </td>
                                            </tr>
                                          )
                                        })}
                                      </tbody>
                                    </table>
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )
            })()}
          </div>
        )}

      </div>

      {selected && (
        <DetailModal
          symbol={selected.symbol}
          name={selected.name}
          detail={detail}
          loading={detailLoading}
          error={detailError}
          onClose={() => setSelected(null)}
        />
      )}

      {/* 비교 플로팅 바 — 외부에서 비교 상태를 관리할 때는 렌더하지 않음 */}
      {!externalAddToCompare && !selfCompareViewOpen && selfCompareList.length >= 1 && (
        <CompareFloatingBar
          symbols={selfCompareList}
          names={selfCompareNames}
          onOpen={() => setSelfCompareViewOpen(true)}
          onRemove={handleRemoveFromCompare}
          onClear={handleClearCompare}
        />
      )}

      {/* 비교 뷰 */}
      {!externalAddToCompare && selfCompareViewOpen && (
        <StockCompareView
          symbols={selfCompareList}
          profiles={selfCompareProfiles}
          onClose={() => setSelfCompareViewOpen(false)}
          onRemove={handleRemoveFromCompare}
          onApplyFilters={(f) => { handleApplyProfileFilters(f) }}
          onAddToCompare={handleAddToCompare}
        />
      )}
    </div>
  )
}
