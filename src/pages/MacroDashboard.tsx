import { useEffect, useMemo, useState } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine,
} from 'recharts'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) throw new Error(`API error ${res.status}`)
  return res.json() as Promise<T>
}

// ── 타입 ─────────────────────────────────────────────────────────────────────

interface MacroItem {
  label: string
  value: number
  date: string
  change: number | null
  change_pct: number | null
}

type MacroSummary = Record<string, MacroItem>

interface SeriesPoint {
  date: string
  value: number
}

// ── 상수 ─────────────────────────────────────────────────────────────────────

const INDICATORS = [
  { key: 'usd_krw',     label: 'USD/KRW',         unit: '원',   color: '#60a5fa' },
  { key: 'jpy_krw',     label: 'JPY/KRW (100엔)',  unit: '원',   color: '#818cf8' },
  { key: 'vix',         label: 'VIX 공포지수',      unit: '',     color: '#f87171' },
  { key: 'us10y',       label: '미국 10Y 금리',     unit: '%',    color: '#fb923c' },
  { key: 'wti',         label: 'WTI 원유',          unit: 'USD',  color: '#a3e635' },
  { key: 'kospi',       label: 'KOSPI',             unit: '',     color: '#34d399' },
  { key: 'kosdaq',      label: 'KOSDAQ',            unit: '',     color: '#22d3ee' },
  { key: 'foreign_net', label: '외국인 순매수',      unit: '억원', color: '#f472b6' },
  { key: 'inst_net',    label: '기관 순매수',        unit: '억원', color: '#c084fc' },
]

// 항상 표시하는 4개 미니 차트
const MINI_KEYS = ['usd_krw', 'vix', 'kospi', 'wti']

const PERIOD_DAYS: Record<string, number> = {
  '1M': 30, '3M': 90, '6M': 180, '1Y': 260,
}

// 규칙 기반 시장 영향 해설
const IMPACT_RULES = [
  {
    id: 'usd_rise',
    condition: (s: MacroSummary) => (s['usd_krw']?.change_pct ?? 0) > 1,
    title: '🔴 원화 약세 (달러 강세)',
    text: '수출주(반도체·자동차·조선) 유리, 내수·여행·항공 불리. 환차손 부담 기업 주의.',
    color: 'red',
  },
  {
    id: 'usd_fall',
    condition: (s: MacroSummary) => (s['usd_krw']?.change_pct ?? 0) < -1,
    title: '🟢 원화 강세 (달러 약세)',
    text: '내수·수입 업체 유리, 수출주 환차익 감소 가능성. 외국인 매수 유입 기대.',
    color: 'green',
  },
  {
    id: 'vix_high',
    condition: (s: MacroSummary) => (s['vix']?.value ?? 0) > 25,
    title: '⚠️ 고변동성 국면 (VIX>25)',
    text: '글로벌 불확실성 고조. 방어주·배당주 상대 선호, 성장주 조정 압력 가능.',
    color: 'yellow',
  },
  {
    id: 'rate_rise',
    condition: (s: MacroSummary) => (s['us10y']?.change_pct ?? 0) > 3,
    title: '🔴 미국 금리 급등',
    text: '성장주·부동산주 밸류에이션 압박. 금융주(은행·보험) 단기 유리.',
    color: 'red',
  },
  {
    id: 'foreign_buy',
    // foreign_net 값은 백만원 단위 → 1000억 = 100,000백만원
    condition: (s: MacroSummary) => (s['foreign_net']?.value ?? 0) > 100_000,
    title: '🟢 외국인 대규모 순매수',
    text: '외국인 자금 유입 신호. 대형주·코스피200 종목 상대 강세 가능성.',
    color: 'green',
  },
  {
    id: 'foreign_sell',
    condition: (s: MacroSummary) => (s['foreign_net']?.value ?? 0) < -100_000,
    title: '🔴 외국인 대규모 순매도',
    text: '수급 경보 발령. 인버스/달러 헷지 고려, 개별 종목 스크리닝 강화 권장.',
    color: 'red',
  },
]

// ── 유틸 ─────────────────────────────────────────────────────────────────────

/** 외국인/기관: DB값이 백만원 단위 → /100 = 억원 */
function fmtValue(key: string, val: number): string {
  if (key === 'foreign_net' || key === 'inst_net') {
    const eok = val / 100  // 억원
    const sign = eok >= 0 ? '+' : ''
    if (Math.abs(eok) >= 10000) {
      return `${sign}${(eok / 10000).toFixed(2)}조원`
    }
    if (Math.abs(eok) >= 1) {
      return `${sign}${eok.toFixed(0)}억원`
    }
    // 1억 미만 → 백만원 단위
    const man = val / 100 * 100  // 백만원 = val / 1 (이미 백만원)
    return `${man >= 0 ? '+' : ''}${man.toFixed(0)}백만원`
  }
  if (key === 'us10y') return `${val.toFixed(2)}%`
  if (key === 'usd_krw' || key === 'jpy_krw') return `${val.toFixed(1)}원`
  if (key === 'vix') return val.toFixed(2)
  if (key === 'wti') return `$${val.toFixed(2)}`
  return val.toLocaleString('ko-KR', { maximumFractionDigits: 2 })
}

function fmtChange(item: MacroItem, key: string): string {
  if (item.change === null || item.change_pct === null) return '—'
  // 값이 거의 0이거나 변화율이 비정상적으로 크면 의미 없음
  if (Math.abs(item.change_pct) > 300) return '—'
  // 수급 지표: 기준값이 작아 %가 의미 없는 경우
  if ((key === 'foreign_net' || key === 'inst_net') && Math.abs(item.change_pct) > 200) return '—'
  const sign = item.change >= 0 ? '+' : ''
  return `${sign}${item.change_pct.toFixed(2)}%`
}

/** 수급 지표용 변화량(억원) 표시 */
function fmtChangeAbs(item: MacroItem, key: string): string | null {
  if (key !== 'foreign_net' && key !== 'inst_net') return null
  if (item.change === null) return null
  const eok = item.change / 100
  const sign = eok >= 0 ? '+' : ''
  if (Math.abs(eok) >= 1) return `${sign}${eok.toFixed(0)}억`
  return null
}

function changeCls(item: MacroItem): string {
  if (item.change === null) return 'text-gray-500'
  return item.change >= 0 ? 'text-red-400' : 'text-blue-400'
}

function fmtDate(dt: string): string {
  if (dt.length !== 8) return dt
  return `${dt.slice(0, 4)}-${dt.slice(4, 6)}-${dt.slice(6, 8)}`
}

function fmtAxis(dt: string): string {
  return `${dt.slice(4, 6)}/${dt.slice(6, 8)}`
}

// ── 차트 컴포넌트 ─────────────────────────────────────────────────────────────

interface MacroChartProps {
  indicator: typeof INDICATORS[0]
  period: number
  mini?: boolean
  color?: string
}

function MacroChart({ indicator, period, mini = false, color }: MacroChartProps) {
  const lineColor = color ?? indicator.color
  const [data, setData] = useState<SeriesPoint[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    apiFetch<{ data: SeriesPoint[] }>(`/api/macro/series?indicator=${indicator.key}&days=${period}`)
      .then(r => setData(r.data ?? []))
      .catch(() => setData([]))
      .finally(() => setLoading(false))
  }, [indicator.key, period])

  const height = mini ? 80 : 120

  if (loading) return (
    <div className={`flex items-center justify-center text-gray-600 text-xs`} style={{ height }}>로딩 중…</div>
  )
  if (!data.length) return (
    <div className={`flex items-center justify-center text-gray-600 text-xs`} style={{ height }}>데이터 없음</div>
  )

  const vals = data.map(d => d.value)
  const minV = Math.min(...vals)
  const maxV = Math.max(...vals)
  const mid = (minV + maxV) / 2

  const isFlow = indicator.key === 'foreign_net' || indicator.key === 'inst_net'

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="2 2" stroke="#1e293b" />
        <XAxis
          dataKey="date"
          tickFormatter={fmtAxis}
          tick={{ fill: '#64748b', fontSize: mini ? 8 : 9 }}
          interval="preserveStartEnd"
          tickLine={false}
          axisLine={false}
        />
        <YAxis
          domain={['auto', 'auto']}
          tick={{ fill: '#64748b', fontSize: mini ? 8 : 9 }}
          width={mini ? 36 : 48}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: number) => {
            if (isFlow) {
              const eok = v / 100
              if (Math.abs(eok) >= 1000) return `${(eok / 1000).toFixed(0)}천억`
              return `${eok.toFixed(0)}억`
            }
            if (indicator.key === 'us10y') return v.toFixed(2)
            return v.toLocaleString('ko-KR', { maximumFractionDigits: 0 })
          }}
        />
        <Tooltip
          contentStyle={{ background: '#0f172a', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }}
          labelFormatter={(l: unknown) => fmtDate(String(l))}
          formatter={(v: unknown) => [fmtValue(indicator.key, Number(v)), indicator.label]}
        />
        {isFlow && <ReferenceLine y={0} stroke="#475569" strokeDasharray="3 3" />}
        {!isFlow && <ReferenceLine y={mid} stroke="#334155" strokeDasharray="2 2" />}
        <Line
          type="monotone"
          dataKey="value"
          stroke={lineColor}
          strokeWidth={mini ? 1 : 1.5}
          dot={false}
          activeDot={{ r: 3 }}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

// ── 요약 카드 ─────────────────────────────────────────────────────────────────

function SummaryCard({
  ind, item, selected, onSelect, stale,
}: {
  ind: typeof INDICATORS[0]
  item: MacroItem | undefined
  selected: boolean
  onSelect: () => void
  stale?: boolean
}) {
  const absChange = item ? fmtChangeAbs(item, ind.key) : null

  return (
    <button
      onClick={onSelect}
      className={`text-left p-3 rounded-xl border transition-all ${
        selected
          ? 'border-opacity-60 bg-slate-800/80'
          : 'border-gray-800 bg-slate-900/60 hover:bg-slate-800/50'
      }`}
      style={selected ? { borderColor: ind.color } : {}}
    >
      <div className="flex items-center justify-between mb-1 gap-1">
        <span className="text-xs text-gray-500 truncate">{ind.label}</span>
        {stale && (
          <span className="text-[9px] text-amber-500 bg-amber-900/30 px-1 rounded shrink-0">지연</span>
        )}
      </div>
      {item ? (
        <>
          <div className="text-sm font-semibold text-white">{fmtValue(ind.key, item.value)}</div>
          <div className={`text-xs mt-0.5 ${changeCls(item)}`}>
            {absChange ?? fmtChange(item, ind.key)}
          </div>
          <div className="text-xs text-gray-600 mt-0.5">{fmtDate(item.date)}</div>
        </>
      ) : (
        <div className="text-xs text-gray-600">데이터 없음</div>
      )}
    </button>
  )
}

// ── 메인 컴포넌트 ─────────────────────────────────────────────────────────────

export default function MacroDashboard() {
  const [summary, setSummary] = useState<MacroSummary>({})
  const [loading, setLoading] = useState(true)
  const [selectedKey, setSelectedKey] = useState<string>('usd_krw')
  const [period, setPeriod] = useState<string>('3M')

  useEffect(() => {
    setLoading(true)
    apiFetch<MacroSummary>('/api/macro/summary')
      .then(r => setSummary(r))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // 전체 지표 중 최신 날짜 → 이보다 오래된 지표는 "지연" 표시
  const maxDate = useMemo(() =>
    Object.values(summary).reduce((m, item) => item.date > m ? item.date : m, '00000000'),
    [summary]
  )

  const selectedInd = INDICATORS.find(i => i.key === selectedKey) ?? INDICATORS[0]
  const miniInds = INDICATORS.filter(i => MINI_KEYS.includes(i.key))
  const activeRules = IMPACT_RULES.filter(r => r.condition(summary))

  return (
    <div className="max-w-7xl mx-auto px-4 pb-10 space-y-6">
      {/* 헤더 */}
      <div className="pt-4">
        <h2 className="text-base font-semibold text-white">매크로 지표</h2>
        <p className="text-xs text-gray-500 mt-0.5">시장 환경 파악용 — AI 추천·스크리너 결과와 무관한 순수 시각화</p>
      </div>

      {/* 요약 카드 그리드 */}
      {loading ? (
        <div className="text-center text-gray-500 text-sm py-8">지표 로딩 중…</div>
      ) : (
        <div className="grid grid-cols-3 sm:grid-cols-5 gap-2">
          {INDICATORS.map(ind => {
            const item = summary[ind.key]
            const stale = !!item && item.date < maxDate
            return (
              <SummaryCard
                key={ind.key}
                ind={ind}
                item={item}
                selected={selectedKey === ind.key}
                onSelect={() => setSelectedKey(ind.key)}
                stale={stale}
              />
            )
          })}
        </div>
      )}

      {/* 주요 4개 지표 미니 차트 */}
      <div>
        <h3 className="text-xs font-medium text-gray-400 uppercase tracking-wider mb-2">주요 지표 추이</h3>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {miniInds.map(ind => (
            <button
              key={ind.key}
              onClick={() => setSelectedKey(ind.key)}
              className={`bg-slate-900/70 border rounded-xl p-3 text-left transition-all hover:bg-slate-800/60 ${
                selectedKey === ind.key ? 'border-opacity-60' : 'border-gray-800'
              }`}
              style={selectedKey === ind.key ? { borderColor: ind.color } : {}}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs text-gray-400">{ind.label}</span>
                {summary[ind.key] && (
                  <span className={`text-xs ${changeCls(summary[ind.key])}`}>
                    {fmtChange(summary[ind.key], ind.key)}
                  </span>
                )}
              </div>
              <MacroChart
                indicator={ind}
                period={PERIOD_DAYS[period]}
                mini
              />
            </button>
          ))}
        </div>
      </div>

      {/* 선택 지표 상세 차트 */}
      <div className="bg-slate-900/70 border border-gray-800 rounded-xl p-4">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-white">{selectedInd.label} 상세</span>
            {summary[selectedKey] && (
              <span className="text-xs text-gray-400">
                {fmtValue(selectedKey, summary[selectedKey].value)}
                <span className={`ml-1 ${changeCls(summary[selectedKey])}`}>
                  ({fmtChange(summary[selectedKey], selectedKey)})
                </span>
              </span>
            )}
          </div>
          <div className="flex gap-1">
            {Object.keys(PERIOD_DAYS).map(p => (
              <button
                key={p}
                onClick={() => setPeriod(p)}
                className={`text-xs px-2 py-1 rounded transition-colors ${
                  period === p
                    ? 'bg-slate-700 text-white'
                    : 'text-gray-500 hover:text-gray-300'
                }`}
              >
                {p}
              </button>
            ))}
          </div>
        </div>
        {/* 전체 9개 지표 선택 탭 */}
        <div className="flex flex-wrap gap-1 mb-3">
          {INDICATORS.map(ind => (
            <button
              key={ind.key}
              onClick={() => setSelectedKey(ind.key)}
              className={`text-xs px-2 py-0.5 rounded-full border transition-colors ${
                selectedKey === ind.key
                  ? 'text-white border-opacity-60'
                  : 'text-gray-500 border-gray-700 hover:text-gray-300'
              }`}
              style={selectedKey === ind.key ? { borderColor: ind.color, color: ind.color } : {}}
            >
              {ind.label}
            </button>
          ))}
        </div>
        <MacroChart
          indicator={selectedInd}
          color={selectedInd.color}
          period={PERIOD_DAYS[period]}
        />
      </div>

      {/* 규칙 기반 영향 분석 */}
      {activeRules.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-xs font-medium text-gray-400 uppercase tracking-wider">현재 국면 해석</h3>
          {activeRules.map(rule => (
            <div
              key={rule.id}
              className={`rounded-xl border px-4 py-3 text-sm ${
                rule.color === 'red'
                  ? 'border-red-800/50 bg-red-900/10'
                  : rule.color === 'green'
                  ? 'border-emerald-800/50 bg-emerald-900/10'
                  : 'border-yellow-800/50 bg-yellow-900/10'
              }`}
            >
              <div className={`font-medium mb-0.5 ${
                rule.color === 'red' ? 'text-red-400'
                  : rule.color === 'green' ? 'text-emerald-400'
                  : 'text-yellow-400'
              }`}>{rule.title}</div>
              <div className="text-gray-300 text-xs">{rule.text}</div>
            </div>
          ))}
        </div>
      )}

      {/* 비고 */}
      <div className="text-xs text-gray-600 pb-2">
        외부 데이터: yfinance (USD/KRW, JPY/KRW, VIX, 미국 10Y, WTI). 내부 데이터: KOSPI/KOSDAQ — pykrx, 외국인/기관 순매수 — KIS API (백만원 단위). 매일 16:30 자동 갱신. "지연" 배지: 다른 지표보다 날짜가 뒤처진 지표.
      </div>
    </div>
  )
}
