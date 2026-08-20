import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  aiApi, paperTradingApi,
  type ActiveTrade, type HealthResponse, type RegimeGate,
  type LivePerformanceResponse,
  type PerformanceSummary,
  type PerformanceTimeline,
  type PerformanceByModel,
  type ModelPerformance,
  type TimelineEntry,
  type TimelineGroupStat,
  type TradeRecord,
} from '../api/aiRecommend'
import StatusBanner from '../components/StatusBanner'
import { fmtDate } from '../utils/format'

// ── 유틸 ──────────────────────────────────────────────────────

function fmtPct(v: number | null | undefined): string {
  if (v == null) return '—'
  return (v >= 0 ? '+' : '') + v.toFixed(1) + '%'
}

function pctColor(v: number | null | undefined): string {
  if (v == null) return 'text-gray-400'
  return v > 0 ? 'text-emerald-400' : v < 0 ? 'text-red-400' : 'text-gray-400'
}

function winRateColor(v: number | null | undefined): string {
  if (v == null) return 'text-gray-400'
  return v >= 0.6 ? 'text-emerald-400' : v >= 0.4 ? 'text-yellow-400' : 'text-red-400'
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '—'
  return v.toLocaleString() + '원'
}

// ── 라이브 성과 카드 ───────────────────────────────────────────

function LivePerfCard({ livePerf }: { livePerf: LivePerformanceResponse | null }) {
  if (!livePerf) return null

  const slot5d  = livePerf['5d']
  const slot60d = livePerf['60d']

  const render5d = () => {
    if (slot5d.status === 'collecting') {
      return (
        <div className="space-y-1">
          <p className="text-gray-300 text-xs font-medium">5d · prediction_log 기준 P@10</p>
          <p className="text-gray-500 text-xs">수집 중 ({slot5d.n}/{slot5d.min_sample ?? 30}건)</p>
        </div>
      )
    }
    const val = slot5d.value
    // 2026-07-28 이전 기록은 ETF/인버스가 포함된 정제 전 데이터
    const preCleanupNote = slot5d.n > 0
    return (
      <div className="space-y-1">
        <p className="text-gray-300 text-xs font-medium">5d · P@10 <span className="text-gray-600">(종가+5% 달성률)</span></p>
        <p className={`text-xl font-bold tabular-nums ${pctColor(val ?? null)}`}>
          {val != null ? `${val.toFixed(1)}%` : '—'}
        </p>
        <p className="text-gray-500 text-xs">n={slot5d.n}건 · 기준 25% 이상 목표</p>
        {preCleanupNote && (
          <p className="text-amber-500/70 text-xs" title="2026-07-28 ETF/인버스 필터 적용 이전 기록 포함. 이후 신규 기록은 ETF 제외 기준.">
            ※ ETF 정제 이전(~7/27) 기록 포함
          </p>
        )}
      </div>
    )
  }

  const render60d = () => {
    if (slot60d.status === 'collecting') {
      return (
        <div className="space-y-1">
          <p className="text-gray-300 text-xs font-medium">60d · 시장초과수익 (avg)</p>
          <p className="text-gray-500 text-xs">수집 중 ({slot60d.n}/{slot60d.min_sample ?? 30}건)</p>
        </div>
      )
    }
    const val = slot60d.value  // fraction (e.g. 0.0245)
    const pct = val != null ? val * 100 : null
    return (
      <div className="space-y-1">
        <p className="text-gray-300 text-xs font-medium">60d · 시장초과수익 <span className="text-gray-600">(avg)</span></p>
        <p className={`text-xl font-bold tabular-nums ${pctColor(pct)}`}>
          {pct != null ? (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%' : '—'}
        </p>
        <p className="text-gray-500 text-xs">n={slot60d.n}건</p>
      </div>
    )
  }

  return (
    <div className="bg-gray-900 border border-purple-500/20 rounded-xl p-4">
      <div className="flex items-center gap-2 mb-3">
        <span className="text-purple-400 text-xs font-semibold">📊 라이브 성과</span>
        {livePerf.data_as_of && (
          <span className="text-gray-600 text-xs">데이터 기준: {fmtDate(livePerf.data_as_of)}</span>
        )}
        <span className="text-gray-700 text-xs ml-auto" title="prediction_log — 실제 API 추천 기준, concentration_filter 통과분">prediction_log 기반</span>
      </div>
      <div className="grid grid-cols-2 gap-4">
        {render5d()}
        {render60d()}
      </div>
    </div>
  )
}

// ── 누적 수익률 라인 차트 (SVG) ────────────────────────────────

function TimelineChart({ entries }: { entries: TimelineEntry[] }) {
  const closed = entries.filter(e => e.closed_count > 0)
  if (closed.length < 2) return (
    <div className="h-28 flex items-center justify-center text-gray-600 text-xs">
      청산 데이터 2개 이상 필요
    </div>
  )

  const W = 600, H = 100, PAD = { t: 10, r: 10, b: 20, l: 44 }
  const iW = W - PAD.l - PAD.r
  const iH = H - PAD.t - PAD.b

  const vals = closed.map(e => e.cumulative_return_pct)
  const minV = Math.min(0, ...vals)
  const maxV = Math.max(0, ...vals)
  const span = maxV - minV || 1

  const xOf = (i: number) => PAD.l + (i / (closed.length - 1)) * iW
  const yOf = (v: number) => PAD.t + (1 - (v - minV) / span) * iH
  const y0 = yOf(0)

  const pts = closed.map((e, i) => `${xOf(i)},${yOf(e.cumulative_return_pct)}`).join(' ')
  const last = closed[closed.length - 1]
  const lastColor = (last.cumulative_return_pct ?? 0) >= 0 ? '#34d399' : '#f87171'

  // 날짜 레이블: 첫·중간·마지막만
  const labelIdxs = [0, Math.floor(closed.length / 2), closed.length - 1]
    .filter((v, i, a) => a.indexOf(v) === i)

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 120 }}>
      {/* 0선 */}
      <line x1={PAD.l} y1={y0} x2={W - PAD.r} y2={y0} stroke="#374151" strokeWidth={1} strokeDasharray="3 3" />
      {/* 영역 채우기 */}
      <polygon
        points={`${xOf(0)},${y0} ${pts} ${xOf(closed.length - 1)},${y0}`}
        fill={(last.cumulative_return_pct ?? 0) >= 0 ? '#34d39920' : '#f8717120'}
      />
      {/* 라인 */}
      <polyline points={pts} fill="none" stroke={lastColor} strokeWidth={1.5} strokeLinejoin="round" />
      {/* 마지막 점 */}
      <circle cx={xOf(closed.length - 1)} cy={yOf(last.cumulative_return_pct)} r={3} fill={lastColor} />
      {/* Y 레이블 */}
      {[minV, 0, maxV].filter((v, i, a) => a.indexOf(v) === i).map(v => (
        <text key={v} x={PAD.l - 4} y={yOf(v) + 3} textAnchor="end"
          fontSize={8} fill="#6b7280">
          {v >= 0 ? '+' : ''}{v.toFixed(1)}%
        </text>
      ))}
      {/* X 레이블 */}
      {labelIdxs.map(i => (
        <text key={i} x={xOf(i)} y={H - 2} textAnchor="middle"
          fontSize={8} fill="#6b7280">
          {closed[i].date.slice(4, 6)}/{closed[i].date.slice(6, 8)}
        </text>
      ))}
    </svg>
  )
}

// ── 그룹별 성과 가로 막대 ──────────────────────────────────────

function GroupBarChart({
  title, data, colorMap,
}: {
  title: string
  data: Record<string, TimelineGroupStat>
  colorMap: Record<string, string>
}) {
  const entries = Object.entries(data).filter(([, v]) => v.closed > 0)
  if (entries.length === 0) return null

  const maxAbs = Math.max(...entries.map(([, v]) => Math.abs(v.avg_return ?? 0)), 0.1)

  return (
    <div className="space-y-2">
      <p className="text-gray-400 text-xs font-medium">{title}</p>
      {entries.map(([key, stat]) => {
        const val = stat.avg_return ?? 0
        const pct = (Math.abs(val) / maxAbs) * 100
        const isPos = val >= 0
        const color = colorMap[key] ?? (isPos ? '#34d399' : '#f87171')
        return (
          <div key={key} className="space-y-0.5">
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-300 w-20 truncate">{key}</span>
              <span className="text-gray-500 text-xs">({stat.closed}건 청산)</span>
              <span style={{ color: isPos ? '#34d399' : '#f87171' }} className="font-medium tabular-nums">
                {isPos ? '+' : ''}{val.toFixed(1)}%
              </span>
            </div>
            <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
              <div
                className="h-full rounded-full transition-all"
                style={{ width: `${pct}%`, backgroundColor: color + 'cc' }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ── 성과 요약 카드 ─────────────────────────────────────────────

interface SummaryCardProps {
  label: string
  value: string
  valueClass: string
  sub: string
  tooltip?: string
}

function SummaryCard({ label, value, valueClass, sub, tooltip }: SummaryCardProps) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4" title={tooltip}>
      <p className="text-gray-400 text-xs mb-1">{label}</p>
      <p className={`text-2xl font-bold tabular-nums ${valueClass}`}>{value}</p>
      <p className="text-gray-500 text-xs mt-1">{sub}</p>
    </div>
  )
}

// ── 단일 모델 성과 패널 ───────────────────────────────────────

function ModelStatPanel({
  label, version, cur, borderClass
}: {
  label: string
  version: string | null
  cur: ModelPerformance | null
  borderClass: string
}) {
  return (
    <div className={`bg-gray-900 border ${borderClass} rounded-xl p-4 space-y-3`}>
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h3 className="text-white font-semibold text-sm">{label}</h3>
        <span className="text-xs font-mono text-gray-500 truncate max-w-[200px]">{version ?? '—'}</span>
      </div>
      {!cur ? (
        <p className="text-gray-500 text-xs py-2 text-center">아직 기록된 추천 없음 — 다음 추천부터 기록됩니다</p>
      ) : cur.open === 0 && cur.closed === 0 ? (
        <p className="text-gray-500 text-xs py-2 text-center">기록 없음</p>
      ) : (
        <>
          {cur.insufficient_sample && cur.closed > 0 && (
            <p className="text-yellow-400 text-xs bg-yellow-500/10 border border-yellow-500/20 rounded-lg px-3 py-1">
              ⚠ 표본 부족 (청산 {cur.closed}건) — 성과 참고용
            </p>
          )}
          <div className="grid grid-cols-2 gap-2">
            <SummaryCard
              label="보유 중"
              value={String(cur.open)}
              valueClass="text-white"
              sub={`전체 ${cur.total}건`}
            />
            <SummaryCard
              label="청산 건수"
              value={String(cur.closed)}
              valueClass="text-gray-300"
              sub={cur.closed > 0 ? `승 ${cur.win_count}건` : '미청산'}
              tooltip="closed 상태 기준 — expired(가격없음)·open(보유중) 제외"
            />
            {cur.closed > 0 && <>
              <SummaryCard
                label="승률"
                value={cur.win_rate != null ? `${(cur.win_rate * 100).toFixed(1)}%` : '—'}
                valueClass={winRateColor(cur.win_rate)}
                sub={`${cur.win_count}/${cur.closed}`}
              />
              <SummaryCard
                label="거래당 평균"
                value={fmtPct(cur.avg_return_pct)}
                valueClass={pctColor(cur.avg_return_pct)}
                sub="청산 기준"
              />
            </>}
          </div>
          {cur.closed > 0 && cur.cumulative_return_pct != null && (
            <div className="border-t border-gray-800 pt-2 flex items-baseline gap-2">
              <span className="text-gray-500 text-xs">누적 수익률</span>
              <span className={`text-base font-bold ${pctColor(cur.cumulative_return_pct)}`}>
                {fmtPct(cur.cumulative_return_pct)}
              </span>
              <span className="text-gray-600 text-xs">(배치 평균 누적합)</span>
            </div>
          )}
        </>
      )}
    </div>
  )
}

// ── 현재 운영 모델 성과 (상단, 가장 중요) ───────────────────────

function CurrentModelCard({ byModel }: { byModel: PerformanceByModel | null }) {
  if (!byModel) return null
  const cur5d  = byModel.current_model_stats_5d
  const cur60d = byModel.current_model_stats_60d
  const v5d    = byModel.active_model_version_5d
  const v60d   = byModel.active_model_version_60d

  return (
    <section className="space-y-3">
      <h2 className="text-white font-bold text-base">🟢 현재 운영 모델 성과</h2>
      <div className="grid sm:grid-cols-2 gap-3">
        <ModelStatPanel
          label="5d 단기 모델"
          version={v5d}
          cur={cur5d}
          borderClass="border-emerald-500/30"
        />
        <ModelStatPanel
          label="60d 중장기 모델"
          version={v60d}
          cur={cur60d}
          borderClass="border-teal-500/30"
        />
      </div>
    </section>
  )
}

// ── 모델별 성과 비교 테이블 (중단) ──────────────────────────────

function ModelBadge({ m }: { m: ModelPerformance }) {
  if (m.is_current_5d)
    return <span className="text-xs px-2 py-0.5 rounded font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 whitespace-nowrap">현재 운영 (5d)</span>
  if (m.is_current_60d)
    return <span className="text-xs px-2 py-0.5 rounded font-medium bg-teal-500/15 text-teal-400 border border-teal-500/30 whitespace-nowrap">현재 운영 (60d)</span>
  if (m.is_regime_bear)
    return <span className="text-xs px-2 py-0.5 rounded font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20 whitespace-nowrap">약세장 보조</span>
  if (m.is_rejected)
    return <span className="text-xs px-2 py-0.5 rounded font-medium bg-red-500/10 text-red-400/70 border border-red-500/20 whitespace-nowrap">폐기</span>
  if (m.is_pending)
    return <span className="text-xs px-2 py-0.5 rounded font-medium bg-purple-500/10 text-purple-400 border border-purple-500/30 whitespace-nowrap">검증대기</span>
  if (m.is_untracked)
    return <span className="text-xs px-2 py-0.5 rounded font-medium bg-gray-700 text-gray-400 whitespace-nowrap">레거시</span>
  return <span className="text-xs px-2 py-0.5 rounded font-medium bg-blue-500/10 text-blue-400 border border-blue-500/20 whitespace-nowrap">과거 모델</span>
}

function ModelTableRows({ models, dimExtra }: { models: ModelPerformance[]; dimExtra?: boolean }) {
  return (
    <>
      {models.map(m => {
        const dim = dimExtra || m.is_rejected
        const rowBg = m.is_current_5d
          ? 'bg-emerald-500/5'
          : m.is_current_60d
          ? 'bg-teal-500/5'
          : ''
        return (
          <tr
            key={m.model_version ?? 'untracked'}
            className={`border-b border-gray-800/50 ${dim ? 'opacity-50' : ''} ${rowBg}`}
          >
            <td className="px-4 py-3 text-gray-300 text-xs font-mono truncate max-w-[180px]">
              {m.model_version ?? '(NULL)'}
            </td>
            <td className="px-4 py-3"><ModelBadge m={m} /></td>
            <td className="px-4 py-3 text-right text-gray-300 text-xs">{m.total}</td>
            <td className="px-4 py-3 text-right text-gray-300 text-xs">
              {m.closed}
              {m.insufficient_sample && m.closed > 0 && (
                <span className="ml-1 text-yellow-400" title="표본 부족 (5건 미만)">⚠</span>
              )}
            </td>
            <td className={`px-4 py-3 text-right text-xs font-medium ${winRateColor(m.win_rate)}`}>
              {m.win_rate != null ? `${(m.win_rate * 100).toFixed(1)}%` : '—'}
            </td>
            <td className={`px-4 py-3 text-right text-xs font-medium ${pctColor(m.avg_return_pct)}`}>
              {fmtPct(m.avg_return_pct)}
            </td>
            <td className={`px-4 py-3 text-right text-xs font-medium ${pctColor(m.cumulative_return_pct)}`}>
              {fmtPct(m.cumulative_return_pct)}
            </td>
          </tr>
        )
      })}
    </>
  )
}

function ModelComparisonTable({ models }: { models: ModelPerformance[] }) {
  if (models.length === 0) return null

  const tracked  = models.filter(m => !m.is_untracked && !m.is_regime_bear)
  const legacy   = models.filter(m => m.is_untracked || m.is_regime_bear)
  const hasInsufficient = tracked.some(m => m.insufficient_sample && m.closed > 0)

  const thead = (
    <thead>
      <tr className="border-b border-gray-800 text-gray-400 text-xs">
        <th className="text-left px-4 py-3 font-medium">모델</th>
        <th className="text-left px-4 py-3 font-medium">구분</th>
        <th className="text-right px-4 py-3 font-medium">건수</th>
        <th className="text-right px-4 py-3 font-medium">청산</th>
        <th className="text-right px-4 py-3 font-medium">승률</th>
        <th className="text-right px-4 py-3 font-medium">평균수익</th>
        <th className="text-right px-4 py-3 font-medium">누적수익</th>
      </tr>
    </thead>
  )

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-white font-semibold text-sm mb-3">모델별 성과 비교</h2>
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              {thead}
              <tbody>
                <ModelTableRows models={tracked} />
              </tbody>
            </table>
          </div>
        </div>
        {hasInsufficient && (
          <p className="text-gray-500 text-xs mt-2">⚠ 청산 5건 미만 모델은 표본 부족 — 성과로 단정하지 마세요</p>
        )}
      </div>

      {legacy.length > 0 && (
        <div className="opacity-60">
          <h2 className="text-gray-500 font-semibold text-sm mb-3">
            비운영 모델 (성과 집계 제외)
          </h2>
          <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                {thead}
                <tbody>
                  <ModelTableRows models={legacy} dimExtra />
                </tbody>
              </table>
            </div>
          </div>
          <p className="text-gray-600 text-xs mt-1">구운용규칙(NULL) 및 regime 보조 모델 기록 — 현재 운용 기준과 다름, 성과 비교 제외</p>
        </div>
      )}
    </section>
  )
}

// ── 모델 전환 이력 (타임라인용) ──────────────────────────────────

interface ModelSwitch { date: string; from: string | null; to: string }

function findModelSwitches(entries: TimelineEntry[]): ModelSwitch[] {
  const switches: ModelSwitch[] = []
  let prev: string | null = null
  for (const e of entries) {
    const v = e.model_version
    if (!v) continue
    if (v !== prev) {
      switches.push({ date: e.date, from: prev, to: v })
      prev = v
    }
  }
  return switches
}

function ModelSwitchHistory({ entries }: { entries: TimelineEntry[] }) {
  const switches = findModelSwitches(entries)
  if (switches.length === 0) return null

  return (
    <div className="text-xs space-y-1 border-t border-gray-800 pt-3">
      <p className="text-gray-400 font-medium mb-1">모델 전환 시점</p>
      {switches.map((s, i) => (
        <p key={i} className="text-gray-500">
          {fmtDate(s.date)}:{' '}
          <span className="font-mono text-gray-500">{s.from ?? '(추적없음)'}</span>
          {' → '}
          <span className="font-mono text-gray-300">{s.to === '혼재' ? '혼재(같은 날 여러 버전)' : s.to}</span>
        </p>
      ))}
    </div>
  )
}

// ── 보유 종목 행 ───────────────────────────────────────────────

function SleeveBadge({ horizon }: { horizon?: number }) {
  const is60d = (horizon ?? 5) >= 60
  return is60d
    ? <span className="text-xs px-1.5 py-0.5 rounded font-medium bg-teal-500/15 text-teal-400 border border-teal-500/30">60d</span>
    : <span className="text-xs px-1.5 py-0.5 rounded font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">5d</span>
}

function ActiveRow({ t }: { t: ActiveTrade }) {
  const horizon = t.horizon ?? 5
  const daysLeft = horizon - t.holding_days
  const daysLeftColor = daysLeft <= 1 ? 'text-amber-400' : 'text-gray-400'

  // 진입가: entry_price(익일시가) 우선, 없으면 recommended_price(종가, 레거시)
  const displayEntryPrice = t.entry_price ?? t.recommended_price
  const entryLabel = t.entry_price == null && t.recommended_price != null ? '(종가)' : null

  return (
    <tr className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors">
      <td className="px-4 py-3"><SleeveBadge horizon={horizon} /></td>
      <td className="px-4 py-3 text-gray-400 text-xs">{t.recommended_rank ?? '—'}</td>
      <td className="px-4 py-3">
        <span className="text-white font-mono text-xs">{t.symbol}</span>
        {t.name && <span className="text-gray-400 text-xs ml-1.5">{t.name}</span>}
      </td>
      <td className="px-4 py-3 text-gray-400 text-xs">{fmtDate(t.recommended_date)}</td>
      <td className="px-4 py-3 text-right text-gray-300 text-xs">
        {fmtPrice(displayEntryPrice)}
        {entryLabel && <span className="text-gray-600 text-xs ml-1">{entryLabel}</span>}
      </td>
      <td className="px-4 py-3 text-right text-gray-300 text-xs">{fmtPrice(t.current_price)}</td>
      <td className={`px-4 py-3 text-right text-xs font-medium ${pctColor(t.unrealized_pct)}`}
          title="비용(0.63%) 미반영 — 실현 시 차감됨">
        {fmtPct(t.unrealized_pct)}*
      </td>
      <td className="px-4 py-3 text-right text-gray-400 text-xs">{t.holding_days}일</td>
      <td className={`px-4 py-3 text-right text-xs ${daysLeftColor}`} title="만기까지 남은 거래일">
        {daysLeft}일
      </td>
      <td className="px-4 py-3 text-right text-gray-500 text-xs">
        {t.recommended_prob != null ? `${(t.recommended_prob * 100).toFixed(1)}%` : '—'}
      </td>
    </tr>
  )
}

// ── 거래 이력 행 ───────────────────────────────────────────────

function TradeRow({ t }: { t: TradeRecord }) {
  const statusBadge =
    t.status === 'closed'
      ? 'bg-emerald-500/10 text-emerald-400'
      : t.status === 'expired'
      ? 'bg-gray-700 text-gray-400'
      : 'bg-blue-500/10 text-blue-400'

  // 폐기 모델/추적 불가(NULL) 거래는 흐리게 표시 — 신구 모델 혼재로 인한 착시 방지
  const isRejected = !!t.model_version && t.model_version.startsWith('rejected_')
  const isUntracked = !t.model_version
  const dim = isRejected || isUntracked

  // 진입가: entry_price(익일시가) 우선, 없으면 recommended_price(종가, 레거시)
  const displayEntryPrice = t.entry_price ?? t.recommended_price
  const entryLabel = t.entry_price == null && t.recommended_price != null ? '(종가)' : null

  return (
    <tr className={`border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors ${dim ? 'opacity-50' : ''}`}>
      <td className="px-4 py-3"><SleeveBadge horizon={t.horizon} /></td>
      <td className="px-4 py-3">
        <span className="text-white font-mono text-xs">{t.symbol}</span>
        {t.name && <span className="text-gray-400 text-xs ml-1.5">{t.name}</span>}
        {isRejected && <span className="ml-1.5 text-xs text-red-400/70" title={t.model_version ?? undefined}>폐기모델</span>}
      </td>
      <td className="px-4 py-3">
        <span className={`text-xs px-2 py-0.5 rounded font-medium ${statusBadge}`}>
          {t.status}
        </span>
      </td>
      <td className="px-4 py-3 text-gray-400 text-xs">{fmtDate(t.recommended_date)}</td>
      <td className="px-4 py-3 text-gray-400 text-xs">
        {t.close_date ? fmtDate(t.close_date) : '—'}
      </td>
      <td className="px-4 py-3 text-right text-gray-300 text-xs">
        {fmtPrice(displayEntryPrice)}
        {entryLabel && <span className="text-gray-600 text-xs ml-1">{entryLabel}</span>}
      </td>
      <td className="px-4 py-3 text-right text-gray-300 text-xs">{fmtPrice(t.close_price)}</td>
      <td className={`px-4 py-3 text-right text-xs font-medium ${pctColor(t.return_pct)}`}
          title="비용(0.63%) 포함 — (1+r)×(1-0.215%)×(1-0.415%)-1">
        {fmtPct(t.return_pct)}
      </td>
      <td className="px-4 py-3 text-right text-gray-400 text-xs">
        {t.holding_days != null ? `${t.holding_days}일` : '—'}
      </td>
    </tr>
  )
}

// ── 메인 페이지 ───────────────────────────────────────────────

export default function PaperTradingPage() {
  const [active, setActive] = useState<ActiveTrade[]>([])
  const [perf, setPerf] = useState<PerformanceSummary | null>(null)
  const [timeline, setTimeline] = useState<PerformanceTimeline | null>(null)
  const [byModel, setByModel] = useState<PerformanceByModel | null>(null)
  const [livePerf, setLivePerf] = useState<LivePerformanceResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [regimeGate, setRegimeGate] = useState<RegimeGate | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const loadedRef = useRef(false)

  useEffect(() => {
    if (loadedRef.current) return
    loadedRef.current = true

    let cancelled = false
    Promise.all([
      paperTradingApi.getActive(),
      paperTradingApi.getPerformance(),
      paperTradingApi.getTimeline(365),
      paperTradingApi.getPerformanceByModel(),
    ])
      .then(([activeData, perfData, timelineData, byModelData]) => {
        if (cancelled) return
        setActive(activeData)
        setPerf(perfData)
        setTimeline(timelineData)
        setByModel(byModelData)
      })
      .catch((e: Error) => {
        if (cancelled) return
        setError(e.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    // 비차단 — 서버 상태 배너 + 게이트 상태 + 라이브 성과
    aiApi.health().then(h => { if (!cancelled) setHealth(h) }).catch(() => {})
    aiApi.predictions(1).then(r => { if (!cancelled) setRegimeGate(r.regime_gate ?? null) }).catch(() => {})
    aiApi.livePerformance().then(lp => { if (!cancelled) setLivePerf(lp) }).catch(() => {})

    return () => { cancelled = true }
  }, [])

  // 타임라인 기반 누적 수익률 (배치별 평균의 누적합 — 합산 SUM과 다름)
  const lastCumReturn = timeline?.timeline.filter(e => e.closed_count > 0).at(-1)?.cumulative_return_pct ?? null
  const totalReturnClass = pctColor(lastCumReturn)

  const winRateClass = winRateColor(perf?.win_rate)

  const closedTrades = perf?.closed_trades ?? 0

  return (
    <div className="min-h-screen bg-slate-950">
      {/* 상단 내비 */}
      <div className="sticky top-0 z-30 bg-slate-950/90 backdrop-blur border-b border-gray-800/60">
        <div className="max-w-5xl mx-auto px-4 h-12 flex items-center gap-3">
          <Link
            to="/"
            className="text-gray-400 hover:text-white transition-colors p-1 -ml-1"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </Link>
          <h1 className="text-white font-bold text-sm">AI 모의투자 추적</h1>
          <div className="h-4 w-px bg-gray-700" />
          <span className="text-purple-400 text-xs font-medium">Paper Trading</span>
          <div className="ml-auto">
            <span className="text-gray-500 text-xs">5d/60d 자동 청산</span>
          </div>
        </div>
      </div>

      <div className="max-w-5xl mx-auto px-4 py-6 space-y-6">
        {/* 에러 */}
        {error && (
          <div className="bg-gray-900 border border-red-500/30 rounded-xl p-5">
            <p className="text-red-400 font-medium text-sm">서버 연결 실패</p>
            <p className="text-gray-400 text-xs mt-1">{error}</p>
          </div>
        )}

        {loading && (
          <div className="flex items-center justify-center py-24">
            <p className="text-gray-500 text-sm">불러오는 중...</p>
          </div>
        )}

        {!loading && !error && (
          <>
            {/* 서버 상태 배너 */}
            {health != null && <StatusBanner mock={health} />}

            {/* G2 레짐 게이트 상태 */}
            {regimeGate && (
              <div
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm ${
                  regimeGate.gate_blocked
                    ? 'bg-amber-900/40 border border-amber-600/40 text-amber-300'
                    : 'bg-emerald-900/20 border border-emerald-700/30 text-emerald-400'
                }`}
                title="G2 게이트: KOSPI ma60 vs ma120 장기추세 기준 (2연속 확인). 종목탐색의 '시장국면'은 별도 지표(KOSPI 20일 수익률 기준)."
              >
                <span>{regimeGate.gate_blocked ? '🔒' : '🟢'}</span>
                <span>
                  G2 게이트 <span className="text-xs opacity-60">(ma60/ma120 장기추세)</span>:{' '}
                  {regimeGate.gate_blocked
                    ? `차단 중 (bear ${regimeGate.bear_streak}일째) — 신규 5d 진입 없음, shadow 기록 중`
                    : `정상 진입 중 (bull ${regimeGate.bull_streak}일째)`
                  }
                </span>
              </div>
            )}

            {/* 0. 현재 운영 모델 성과 (가장 중요 — 최상단) */}
            <CurrentModelCard byModel={byModel} />

            {/* 0.5 라이브 성과 */}
            <LivePerfCard livePerf={livePerf} />

            {/* 1. 현재 보유 종목 */}
            <section>
              <h2 className="text-white font-semibold text-sm mb-3">
                현재 보유 종목
                <span className="ml-2 text-gray-500 font-normal text-xs">{active.length}개</span>
              </h2>
              {active.length === 0 ? (
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
                  <p className="text-gray-500 text-sm">보유 중인 종목 없음</p>
                </div>
              ) : (
                <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-gray-800 text-gray-400 text-xs">
                          <th className="text-left px-4 py-3 font-medium">슬리브</th>
                          <th className="text-left px-4 py-3 font-medium">순위</th>
                          <th className="text-left px-4 py-3 font-medium">종목</th>
                          <th className="text-left px-4 py-3 font-medium">추천일</th>
                          <th className="text-right px-4 py-3 font-medium">진입가</th>
                          <th className="text-right px-4 py-3 font-medium">현재가</th>
                          <th className="text-right px-4 py-3 font-medium" title="비용 미반영 (매도 시 -0.63% 추가 차감)">손익률*</th>
                          <th className="text-right px-4 py-3 font-medium">보유일</th>
                          <th className="text-right px-4 py-3 font-medium">만기</th>
                          <th className="text-right px-4 py-3 font-medium">확률</th>
                        </tr>
                      </thead>
                      <tbody>
                        {active.map(t => <ActiveRow key={t.id} t={t} />)}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </section>

            {/* 2. 베스트 / 워스트 거래 */}
            {(perf?.best_trade || perf?.worst_trade) && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {perf.best_trade && (
                  <div className="bg-gray-900 border border-emerald-500/20 rounded-xl p-4">
                    <p className="text-emerald-400 text-xs font-medium mb-2">베스트 거래</p>
                    <p className="text-white font-mono text-sm">{perf.best_trade.symbol}</p>
                    {perf.best_trade.name && (
                      <p className="text-gray-400 text-xs">{perf.best_trade.name}</p>
                    )}
                    <p className="text-emerald-400 text-2xl font-bold mt-2 tabular-nums">
                      {fmtPct(perf.best_trade.return_pct)}
                    </p>
                  </div>
                )}
                {perf.worst_trade && (
                  <div className="bg-gray-900 border border-red-500/20 rounded-xl p-4">
                    <p className="text-red-400 text-xs font-medium mb-2">워스트 거래</p>
                    <p className="text-white font-mono text-sm">{perf.worst_trade.symbol}</p>
                    {perf.worst_trade.name && (
                      <p className="text-gray-400 text-xs">{perf.worst_trade.name}</p>
                    )}
                    <p className="text-red-400 text-2xl font-bold mt-2 tabular-nums">
                      {fmtPct(perf.worst_trade.return_pct)}
                    </p>
                  </div>
                )}
              </div>
            )}

            {/* 3. 최근 청산 이력 */}
            <section>
              <h2 className="text-white font-semibold text-sm mb-3">최근 청산 이력</h2>
              {!perf?.recent_trades || perf.recent_trades.length === 0 ? (
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center space-y-1">
                  <p className="text-gray-500 text-sm">아직 청산된 거래 없음</p>
                  <p className="text-gray-600 text-xs">
                    5d: 약 5거래일 후, 60d: 약 60거래일 후 첫 결과가 표시됩니다
                  </p>
                </div>
              ) : (
                <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-gray-800 text-gray-400 text-xs">
                          <th className="text-left px-4 py-3 font-medium">슬리브</th>
                          <th className="text-left px-4 py-3 font-medium">종목</th>
                          <th className="text-left px-4 py-3 font-medium">상태</th>
                          <th className="text-left px-4 py-3 font-medium">추천일</th>
                          <th className="text-left px-4 py-3 font-medium">청산일</th>
                          <th className="text-right px-4 py-3 font-medium">진입가</th>
                          <th className="text-right px-4 py-3 font-medium">매도가</th>
                          <th className="text-right px-4 py-3 font-medium" title="비용(0.63%) 포함 — (1+r)×(1-0.215%)×(1-0.415%)-1">수익률</th>
                          <th className="text-right px-4 py-3 font-medium">보유일</th>
                        </tr>
                      </thead>
                      <tbody>
                        {perf.recent_trades.map(t => <TradeRow key={t.id} t={t} />)}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </section>

            {/* 과거 기록 (참고용) — 기본 접힘 */}
            <div className="border border-gray-700/50 rounded-xl overflow-hidden">
              <button
                className="w-full flex items-center gap-3 px-5 py-3 text-left bg-gray-900/60 hover:bg-gray-800/50 transition-colors"
                onClick={() => setHistoryOpen(v => !v)}
              >
                <span className="text-gray-300 text-sm font-semibold">📚 과거 기록 (참고용)</span>
                <span className="text-gray-600 text-xs flex-1">신구 모델 혼재 — 정확한 수치는 위 "현재 운영 모델" 카드 참고</span>
                <span className={`text-gray-500 text-sm transition-transform duration-200 ${historyOpen ? 'rotate-180' : ''}`}>▾</span>
              </button>

              {historyOpen && (
                <div className="px-5 pb-6 pt-4 space-y-6 border-t border-gray-700/50 bg-gray-900/20">

                  {/* 전체 누적 성과 */}
                  <div>
                    <p className="text-gray-500 text-xs mb-2">전체 누적 성과 — 신구/폐기 모델 전부 합산이라 왜곡 가능</p>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                      <SummaryCard
                        label="누적 수익률"
                        value={lastCumReturn != null ? fmtPct(lastCumReturn) : '—'}
                        valueClass={totalReturnClass}
                        sub="배치 평균 누적합"
                        tooltip="추천일 배치별 평균 수익률을 누적합한 값. 신구 모델 전체 포함."
                      />
                      <SummaryCard
                        label="거래당 평균"
                        value={fmtPct(perf?.avg_return_pct)}
                        valueClass={pctColor(perf?.avg_return_pct)}
                        sub={closedTrades > 0 ? `${closedTrades}건 청산 기준` : '청산 없음'}
                        tooltip="비용(왕복 0.63%) 포함 청산 거래의 평균 수익률. 신구 모델 전체 혼합."
                      />
                      <SummaryCard
                        label="승률"
                        value={
                          perf?.win_rate != null
                            ? `${(perf.win_rate * 100).toFixed(1)}%`
                            : '—'
                        }
                        valueClass={winRateClass}
                        sub={
                          perf?.win_count != null
                            ? `${perf.win_count}승 / ${closedTrades}건`
                            : '데이터 없음'
                        }
                        tooltip="closed 청산 거래 중 수익률 > 0인 거래 비율."
                      />
                      <SummaryCard
                        label="총 거래"
                        value={perf ? String(perf.total_trades) : '—'}
                        valueClass="text-white"
                        sub={`보유 ${perf?.open_trades ?? 0} · 만료 ${perf?.expired_trades ?? 0}`}
                        tooltip="전체 추천 기록 수 (보유중 + 청산 + 만료 합산)."
                      />
                    </div>
                  </div>

                  {/* 누적 수익률 차트 + 그룹별 성과 */}
                  {timeline && timeline.timeline.length > 0 && (
                    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-5">
                      <div>
                        <h2 className="text-white font-semibold text-sm mb-1">누적 수익률 추이</h2>
                        <p className="text-gray-500 text-xs mb-3">
                          추천일 배치별 평균 수익률 누적합 (신구 모델 전체)
                        </p>
                        <TimelineChart entries={timeline.timeline} />
                        <ModelSwitchHistory entries={timeline.timeline} />
                      </div>

                      {(Object.keys(timeline.by_confidence).length > 0 ||
                        Object.keys(timeline.by_market_mode).length > 0 ||
                        Object.keys(timeline.by_risk).length > 0) && (
                        <div className="grid grid-cols-1 sm:grid-cols-3 gap-5 pt-3 border-t border-gray-800">
                          <GroupBarChart
                            title="신뢰도별 평균 수익률"
                            data={timeline.by_confidence}
                            colorMap={{ HIGH: '#34d399', MEDIUM: '#fbbf24', LOW: '#9ca3af' }}
                          />
                          <GroupBarChart
                            title="시장 국면별 평균 수익률"
                            data={timeline.by_market_mode}
                            colorMap={{ aggressive: '#34d399', cautious: '#fbbf24', defensive: '#f87171' }}
                          />
                          <GroupBarChart
                            title="위험도별 평균 수익률"
                            data={timeline.by_risk}
                            colorMap={{ LOW: '#34d399', MEDIUM: '#fb923c', HIGH: '#f87171' }}
                          />
                        </div>
                      )}
                    </div>
                  )}

                  {/* 모델별 성과 비교 */}
                  {byModel && <ModelComparisonTable models={byModel.models} />}

                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
