import { useEffect, useState } from 'react'
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
  { key: 'usd_krw',    label: 'USD/KRW', unit: '원', color: '#60a5fa' },
  { key: 'jpy_krw',    label: 'JPY/KRW (100엔)', unit: '원', color: '#818cf8' },
  { key: 'vix',        label: 'VIX 공포지수', unit: '', color: '#f87171' },
  { key: 'us10y',      label: '미국 10Y 금리', unit: '%', color: '#fb923c' },
  { key: 'wti',        label: 'WTI 원유', unit: 'USD', color: '#a3e635' },
  { key: 'kospi',      label: 'KOSPI', unit: '', color: '#34d399' },
  { key: 'kosdaq',     label: 'KOSDAQ', unit: '', color: '#22d3ee' },
  { key: 'foreign_net',label: '외국인 순매수', unit: '억원', color: '#f472b6' },
  { key: 'inst_net',   label: '기관 순매수', unit: '억원', color: '#c084fc' },
]

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
    condition: (s: MacroSummary) => (s['foreign_net']?.value ?? 0) > 500e8,
    title: '🟢 외국인 대규모 순매수',
    text: '외국인 자금 유입 신호. 대형주·코스피200 종목 상대 강세 가능성.',
    color: 'green',
  },
  {
    id: 'foreign_sell',
    condition: (s: MacroSummary) => (s['foreign_net']?.value ?? 0) < -500e8,
    title: '🔴 외국인 대규모 순매도',
    text: '수급 경보 발령. 인버스/달러 헷지 고려, 개별 종목 스크리닝 강화 권장.',
    color: 'red',
  },
]

// ── 유틸 ─────────────────────────────────────────────────────────────────────

function fmtValue(key: string, val: number): string {
  if (key === 'foreign_net' || key === 'inst_net') {
    const hundredM = val / 1e8
    return `${hundredM >= 0 ? '+' : ''}${hundredM.toFixed(0)}억원`
  }
  if (key === 'us10y') return `${val.toFixed(2)}%`
  if (key === 'usd_krw' || key === 'jpy_krw') return `${val.toFixed(1)}원`
  if (key === 'vix') return val.toFixed(2)
  if (key === 'wti') return `$${val.toFixed(2)}`
  return val.toLocaleString('ko-KR', { maximumFractionDigits: 2 })
}

function fmtChange(item: MacroItem): string {
  if (item.change === null || item.change_pct === null) return '—'
  const sign = item.change >= 0 ? '+' : ''
  return `${sign}${item.change_pct.toFixed(2)}%`
}

function changeCls(item: MacroItem): string {
  if (item.change === null) return 'text-gray-500'
  return item.change >= 0 ? 'text-red-400' : 'text-blue-400'
}

function fmtDate(dt: string): string {
  return `${dt.slice(0, 4)}-${dt.slice(4, 6)}-${dt.slice(6, 8)}`
}

function fmtAxis(dt: string): string {
  return `${dt.slice(4, 6)}/${dt.slice(6, 8)}`
}

// ── 차트 컴포넌트 ─────────────────────────────────────────────────────────────

function MacroChart({
  indicator, color, period,
}: { indicator: typeof INDICATORS[0]; color: string; period: number }) {
  const [data, setData] = useState<SeriesPoint[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    apiFetch(`/api/macro/series?indicator=${indicator.key}&days=${period}`)
      .then((r: any) => setData(r.data ?? []))
      .catch(() => setData([]))
      .finally(() => setLoading(false))
  }, [indicator.key, period])

  if (loading) return (
    <div className="h-28 flex items-center justify-center text-gray-600 text-xs">로딩 중…</div>
  )
  if (!data.length) return (
    <div className="h-28 flex items-center justify-center text-gray-600 text-xs">데이터 없음</div>
  )

  const vals = data.map(d => d.value)
  const minV = Math.min(...vals)
  const maxV = Math.max(...vals)
  const mid = (minV + maxV) / 2

  return (
    <ResponsiveContainer width="100%" height={112}>
      <LineChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="2 2" stroke="#1e293b" />
        <XAxis
          dataKey="date"
          tickFormatter={fmtAxis}
          tick={{ fill: '#64748b', fontSize: 9 }}
          interval="preserveStartEnd"
          tickLine={false}
          axisLine={false}
        />
        <YAxis
          domain={['auto', 'auto']}
          tick={{ fill: '#64748b', fontSize: 9 }}
          width={48}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: number) => {
            if (indicator.key === 'foreign_net' || indicator.key === 'inst_net') {
              return `${(v / 1e8).toFixed(0)}억`
            }
            return v.toFixed(indicator.key === 'us10y' ? 2 : 0)
          }}
        />
        <Tooltip
          contentStyle={{ background: '#0f172a', border: '1px solid #334155', borderRadius: 8, fontSize: 11 }}
          labelFormatter={(l: unknown) => fmtDate(String(l))}
          formatter={(v: unknown) => [fmtValue(indicator.key, Number(v)), indicator.label]}
        />
        <ReferenceLine y={mid} stroke="#334155" strokeDasharray="2 2" />
        <Line
          type="monotone"
          dataKey="value"
          stroke={color}
          strokeWidth={1.5}
          dot={false}
          activeDot={{ r: 3 }}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

// ── 요약 카드 ─────────────────────────────────────────────────────────────────

function SummaryCard({
  ind, item, selected, onSelect,
}: {
  ind: typeof INDICATORS[0]
  item: MacroItem | undefined
  selected: boolean
  onSelect: () => void
}) {
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
      <div className="text-xs text-gray-500 mb-1">{ind.label}</div>
      {item ? (
        <>
          <div className="text-sm font-semibold text-white">{fmtValue(ind.key, item.value)}</div>
          <div className={`text-xs mt-0.5 ${changeCls(item)}`}>{fmtChange(item)}</div>
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
    apiFetch('/api/macro/summary')
      .then((r: any) => setSummary(r))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const selectedInd = INDICATORS.find(i => i.key === selectedKey) ?? INDICATORS[0]
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
          {INDICATORS.map(ind => (
            <SummaryCard
              key={ind.key}
              ind={ind}
              item={summary[ind.key]}
              selected={selectedKey === ind.key}
              onSelect={() => setSelectedKey(ind.key)}
            />
          ))}
        </div>
      )}

      {/* 시계열 차트 */}
      <div className="bg-slate-900/70 border border-gray-800 rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <span className="text-sm font-medium text-white">{selectedInd.label} 추이</span>
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
        외부 데이터: yfinance (USD/KRW, JPY/KRW, VIX, 미국 10Y, WTI). 내부 데이터: KOSPI/KOSDAQ — pykrx, 외국인/기관 순매수 — KIS API. 매일 16:30 자동 갱신.
      </div>
    </div>
  )
}
