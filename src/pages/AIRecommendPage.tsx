import { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate, Link } from 'react-router-dom'
import {
  createChart,
  CandlestickSeries,
  ColorType,
  LineSeries,
  HistogramSeries,
  type UTCTimestamp,
} from 'lightweight-charts'
import { fetchQuote, fetchCandlesCached, searchSymbols, type SearchResult } from '../api/yahooFinance'
import {
  aiApi,
  getWatchlist, toggleWatchlist,
  getMemo, saveMemo,
  getVirtualTrades, addVirtualTrade, deleteVirtualTrade,
  type Prediction, type ShapEntry, type TickerDetail,
  type PerformanceResponse, type PriceBar, type CostsApplied,
  type MarketTrend, type DailySummary, type VirtualTrade,
  type RetrainStatus, type ExcludedStock,
  type TradeStrategy, type VolumeAnomalyResponse,
  type VolatilityRegime,
  type Prediction60d, type HealthResponse,
} from '../api/aiRecommend'
import StatusBanner from '../components/StatusBanner'

// ── 유틸 ──────────────────────────────────────────────────────

function yyyymmddToTs(s: string): UTCTimestamp {
  const y = +s.slice(0, 4), m = +s.slice(4, 6) - 1, d = +s.slice(6, 8)
  return Math.floor(new Date(y, m, d, 9, 0, 0).getTime() / 1000) as UTCTimestamp
}
function fmtDate(s: string): string {
  return s.length === 8 ? `${s.slice(0, 4)}.${s.slice(4, 6)}.${s.slice(6, 8)}` : s
}
const normalizeSymbol = (s: string) => s.replace(/\.(KS|KQ)$/, '')
function todayStr(): string {
  const d = new Date()
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`
}

const CHART_OPTS = {
  layout: { background: { type: ColorType.Solid, color: '#111827' }, textColor: '#9CA3AF' },
  grid: { vertLines: { color: '#1F2937' }, horzLines: { color: '#1F2937' } },
  crosshair: {
    vertLine: { color: '#6366F1', labelBackgroundColor: '#6366F1' },
    horzLine: { color: '#6366F1', labelBackgroundColor: '#6366F1' },
  },
  rightPriceScale: { borderColor: '#1F2937' },
  timeScale: {
    borderColor: '#1F2937',
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
}

// ── 재학습 버튼 ───────────────────────────────────────────────

function fmtElapsed(sec: number | null): string {
  if (sec == null) return ''
  const m = Math.floor(sec / 60), s = sec % 60
  return m > 0 ? `${m}분 ${s}초` : `${s}초`
}

function RetrainButton({ onDone }: { onDone?: () => void }) {
  const [status, setStatus] = useState<RetrainStatus | null>(null)
  const [open, setOpen] = useState(false)
  const [starting, setStarting] = useState(false)
  const [tick, setTick] = useState(0)
  const logRef = useRef<HTMLDivElement>(null)
  const prevStatusRef = useRef<string | null>(null)

  const isRunning = status?.status === 'running'
  const isDone = status?.status === 'done'
  const isError = status?.status === 'error'

  // 재학습 완료 감지 → onDone 콜백
  useEffect(() => {
    if (prevStatusRef.current === 'running' && status?.status === 'done') {
      onDone?.()
    }
    prevStatusRef.current = status?.status ?? null
  }, [status?.status, onDone])

  // 폴링: 모달 열려 있거나 running일 때만
  useEffect(() => {
    if (!open && !isRunning) return
    const id = setInterval(async () => {
      try { setStatus(await aiApi.retrainStatus()) } catch { /* ignore */ }
    }, 3000)
    return () => clearInterval(id)
  }, [open, isRunning])

  // 실행 중 경과시간 1초 tick
  useEffect(() => {
    if (!isRunning) return
    const id = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [isRunning])

  // 로그 자동 스크롤
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [status?.log?.length])

  const handleOpen = async () => {
    setOpen(true)
    try { setStatus(await aiApi.retrainStatus()) } catch { /* ignore */ }
  }

  const handleStart = async (quick: boolean) => {
    const msg = quick
      ? '빠른 재학습을 시작합니다.\n• 데이터 수집 스킵 (이미 최신)\n• 15 Optuna trials (~2-3분)\n계속할까요?'
      : '전체 재학습을 시작합니다.\n• 3년치 데이터 재수집\n• 25 Optuna trials (~30분)\n계속할까요?'
    if (!confirm(msg)) return
    setStarting(true)
    try {
      await aiApi.startRetrain(quick)
      setStatus({ status: 'running', elapsed_sec: 0, log: [`[시작] ${quick ? '빠른' : '전체'} 재학습 파이프라인 실행 중...`] })
      const s = await aiApi.retrainStatus()
      setStatus(s)
    } catch (e) {
      // 이미 실행 중이면 현재 상태를 가져와서 모달에 표시 (alert 대신)
      const msg = (e as Error).message
      if (msg.includes('실행 중')) {
        try { setStatus(await aiApi.retrainStatus()) } catch { /* ignore */ }
      } else {
        alert(msg)
      }
    } finally {
      setStarting(false)
    }
  }

  const displayedElapsed = isRunning && status?.elapsed_sec != null
    ? status.elapsed_sec + tick
    : status?.elapsed_sec ?? null

  const statusLabel = !status || status.status === 'idle' ? '대기 중'
    : status.status === 'running' ? `실행 중 — ${fmtElapsed(displayedElapsed)}`
    : status.status === 'done' ? `완료 (${fmtElapsed(status.elapsed_sec)})`
    : `오류 (exit=${status.return_code})`

  return (
    <>
      <button
        onClick={handleOpen}
        className={`text-xs px-3 py-1.5 rounded-lg border transition-colors ${
          isRunning
            ? 'bg-yellow-500/10 border-yellow-500/30 text-yellow-400'
            : isDone
            ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'
            : isError
            ? 'bg-red-500/10 border-red-500/30 text-red-400'
            : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-white hover:border-gray-600'
        }`}
      >
        {isRunning
          ? <span className="flex items-center gap-1.5">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-yellow-400 animate-ping" />
              재학습 중 {fmtElapsed(displayedElapsed)}
            </span>
          : isDone
          ? <span className="flex items-center gap-1">✓ 학습완료</span>
          : isError
          ? <span className="flex items-center gap-1">✕ 학습실패</span>
          : '재학습'}
      </button>

      {open && createPortal(
        <div
          style={{ position: 'fixed', inset: 0, zIndex: 9999, background: 'rgba(0,0,0,0.75)',
                   display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '16px' }}
          onClick={() => setOpen(false)}
        >
          <div
            style={{ background: '#111827', border: '1px solid #374151', borderRadius: '16px',
                     width: '100%', maxWidth: '512px', maxHeight: 'calc(100vh - 32px)',
                     display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
            onClick={e => e.stopPropagation()}
          >
            {/* 헤더 — 고정 */}
            <div style={{ flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                          padding: '16px 20px', borderBottom: '1px solid #1f2937' }}>
              <div>
                <p style={{ color: '#fff', fontWeight: 600, fontSize: '14px', margin: 0 }}>모델 재학습</p>
                <p style={{ color: '#6b7280', fontSize: '12px', margin: '2px 0 0' }}>target_5d · val_days=252</p>
              </div>
              <button onClick={() => setOpen(false)} style={{ color: '#9ca3af', background: 'none', border: 'none', cursor: 'pointer', padding: '4px' }}>
                <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {/* 본문 — 스크롤 */}
            <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {/* 상태 */}
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                            background: 'rgba(31,41,55,0.6)', borderRadius: '8px', padding: '10px 16px' }}>
                <span style={{ color: '#9ca3af', fontSize: '14px' }}>상태</span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  {isRunning && (
                    <div style={{ width: 14, height: 14, border: '2px solid #facc15', borderTopColor: 'transparent',
                                  borderRadius: '50%', animation: 'spin 0.7s linear infinite' }} />
                  )}
                  <span style={{ fontSize: '14px', fontWeight: 500,
                                 color: !status || status.status === 'idle' ? '#9ca3af'
                                      : status.status === 'running' ? '#facc15'
                                      : status.status === 'done' ? '#34d399' : '#f87171' }}>
                    {statusLabel}
                  </span>
                </div>
              </div>

              {/* 완료 배너 */}
              {isDone && (
                <div style={{ background: 'rgba(16,185,129,0.12)', border: '1px solid rgba(16,185,129,0.35)',
                              borderRadius: '10px', padding: '12px 16px', display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <span style={{ fontSize: '20px', lineHeight: 1 }}>✅</span>
                  <div>
                    <p style={{ margin: 0, color: '#34d399', fontWeight: 600, fontSize: '14px' }}>재학습 완료</p>
                    <p style={{ margin: '2px 0 0', color: '#6ee7b7', fontSize: '12px' }}>
                      소요 시간 {fmtElapsed(status?.elapsed_sec)} · 서버가 새 모델로 자동 전환됐습니다
                    </p>
                  </div>
                </div>
              )}

              {/* 오류 배너 */}
              {isError && (
                <div style={{ background: 'rgba(239,68,68,0.12)', border: '1px solid rgba(239,68,68,0.35)',
                              borderRadius: '10px', padding: '12px 16px', display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <span style={{ fontSize: '20px', lineHeight: 1 }}>❌</span>
                  <div>
                    <p style={{ margin: 0, color: '#f87171', fontWeight: 600, fontSize: '14px' }}>재학습 실패</p>
                    <p style={{ margin: '2px 0 0', color: '#fca5a5', fontSize: '12px' }}>
                      종료 코드 {status?.return_code ?? '?'} · 아래 로그에서 오류를 확인하세요
                    </p>
                  </div>
                </div>
              )}

              {/* 로그 */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ color: '#6b7280', fontSize: '12px' }}>로그</span>
                  {isRunning && <span style={{ color: '#facc15', fontSize: '12px' }}>● 실행 중</span>}
                </div>
                <div ref={logRef}
                  style={{ background: '#030712', border: '1px solid #1f2937', borderRadius: '8px',
                           padding: '10px 12px', height: '200px', overflowY: 'auto',
                           fontFamily: 'monospace', fontSize: '12px' }}>
                  {(!status || status.log.length === 0)
                    ? <span style={{ color: '#374151' }}>로그 없음 — 재학습을 시작하면 여기에 출력됩니다</span>
                    : status.log.map((line, i) => (
                        <div key={i} style={{
                          color: line.includes('완료') || line.includes('Completed') ? '#34d399'
                               : line.includes('오류') || line.includes('Error') || line.includes('실패') ? '#f87171'
                               : line.includes('단계') || line.includes('===') ? '#fde68a'
                               : '#9ca3af',
                          marginBottom: '2px', wordBreak: 'break-all'
                        }}>{line}</div>
                      ))
                  }
                </div>
              </div>

              {/* 안내 */}
              <div style={{ background: 'rgba(31,41,55,0.4)', borderRadius: '8px', padding: '10px 12px',
                            fontSize: '12px', color: '#6b7280', lineHeight: '1.6' }}>
                <p style={{ margin: '0 0 6px', color: '#9ca3af', fontWeight: 500 }}>빠른 재학습 (~2-3분)</p>
                <p style={{ margin: '0 0 2px' }}>• 데이터 수집 스킵 (일일 업데이트가 이미 최신 유지)</p>
                <p style={{ margin: '0 0 8px' }}>• 15 Optuna trials — 정확도 소폭 감소, 빠른 모델 갱신</p>
                <p style={{ margin: '0 0 6px', color: '#9ca3af', fontWeight: 500 }}>전체 재학습 (~30분)</p>
                <p style={{ margin: '0 0 2px' }}>• 3년치 전종목 데이터 재수집 → 피처 → 25 trials 학습</p>
                <p style={{ margin: 0 }}>• 완료 후 서버가 자동으로 새 모델로 전환됩니다</p>
              </div>
            </div>

            {/* 버튼 — 고정 */}
            <div style={{ flexShrink: 0, padding: '16px 20px', borderTop: '1px solid #1f2937', display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {isRunning ? (
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px',
                              background: 'rgba(234,179,8,0.1)', border: '1px solid rgba(234,179,8,0.2)',
                              borderRadius: '8px', padding: '10px', color: '#facc15', fontSize: '14px' }}>
                  <div style={{ width: 14, height: 14, border: '2px solid #facc15', borderTopColor: 'transparent',
                                borderRadius: '50%', animation: 'spin 0.7s linear infinite' }} />
                  학습 진행 중... {fmtElapsed(displayedElapsed)} 경과
                </div>
              ) : (
                <div style={{ display: 'flex', gap: '8px' }}>
                  <button onClick={() => handleStart(true)} disabled={starting}
                    style={{ flex: 1, background: starting ? '#065f46' : '#047857', border: 'none', borderRadius: '8px',
                             padding: '9px 8px', color: '#fff', fontSize: '13px', cursor: starting ? 'not-allowed' : 'pointer',
                             opacity: starting ? 0.7 : 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '5px' }}>
                    {starting && <div style={{ width: 12, height: 12, border: '2px solid #fff', borderTopColor: 'transparent',
                                              borderRadius: '50%', animation: 'spin 0.7s linear infinite' }} />}
                    ⚡ 빠른 재학습
                    <span style={{ fontSize: '11px', opacity: 0.8 }}>~2분</span>
                  </button>
                  <button onClick={() => handleStart(false)} disabled={starting}
                    style={{ flex: 1, background: starting ? '#4338ca' : '#4f46e5', border: 'none', borderRadius: '8px',
                             padding: '9px 8px', color: '#fff', fontSize: '13px', cursor: starting ? 'not-allowed' : 'pointer',
                             opacity: starting ? 0.7 : 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '5px' }}>
                    전체 재학습
                    <span style={{ fontSize: '11px', opacity: 0.8 }}>~30분</span>
                  </button>
                </div>
              )}
              <button onClick={() => setOpen(false)}
                style={{ width: '100%', padding: '7px 16px', background: '#1f2937', border: 'none', borderRadius: '8px',
                         color: '#d1d5db', fontSize: '14px', cursor: 'pointer' }}>
                닫기
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </>
  )
}

// ── 확률 표시값 ────────────────────────────────────────────────
// 보정확률(calibration, 2026-06-22) — 화면에 보이는 숫자는 항상 이 값을 써야 함.
// 순위/추천 선정은 백엔드가 이미 raw probability로 끝낸 뒤 응답이 오므로 여기서는 절대
// 정렬·필터링 기준으로 쓰지 말 것(과거 모델 등 calibrator 없는 응답은 raw로 폴백).
function displayProb(p: { probability: number; probability_calibrated?: number }): number {
  return p.probability_calibrated ?? p.probability
}

// ── 신호 강도 ──────────────────────────────────────────────────

function signalInfo(prob: number): { label: string; stars: number; cls: string } {
  if (prob >= 0.75) return { label: '강한 매수', stars: 5, cls: 'text-emerald-400' }
  if (prob >= 0.65) return { label: '매수', stars: 4, cls: 'text-green-400' }
  if (prob >= 0.55) return { label: '주목', stars: 3, cls: 'text-yellow-400' }
  if (prob >= 0.45) return { label: '중립', stars: 2, cls: 'text-gray-400' }
  return { label: '관망', stars: 1, cls: 'text-gray-500' }
}

function SignalStrength({ prob, size = 'sm' }: { prob: number; size?: 'sm' | 'md' }) {
  const { label, stars, cls } = signalInfo(prob)
  const starEl = Array.from({ length: 5 }, (_, i) => (
    <span key={i} className={i < stars ? cls : 'text-gray-700'}>★</span>
  ))
  return (
    <div className={`flex items-center gap-1.5 ${size === 'md' ? 'text-sm' : 'text-xs'}`}>
      <span className={`font-medium ${cls}`}>{label}</span>
      <span className="tracking-tight">{starEl}</span>
    </div>
  )
}

// ── 시장 배너 ─────────────────────────────────────────────────

function MarketBanner({ market }: { market: MarketTrend }) {
  const trendColor = market.trend === 'bull' ? 'emerald' : market.trend === 'bear' ? 'red' : 'yellow'
  const borderCls = `border-${trendColor}-500/30`
  const bgCls = `bg-${trendColor}-500/5`
  const textCls = `text-${trendColor}-400`
  const ret20Sign = market.ret_20d_pct >= 0 ? '+' : ''
  const ret5Sign  = market.ret_5d_pct  >= 0 ? '+' : ''

  return (
    <div className={`rounded-xl border ${borderCls} ${bgCls} px-4 py-3 flex flex-wrap items-center gap-x-5 gap-y-2`}>
      <div className="flex items-center gap-2">
        <span className={`text-base font-bold ${textCls}`}>{market.label}</span>
        {market.badge && (
          <span className="text-xs font-semibold bg-red-500/20 text-red-400 border border-red-500/30 px-2 py-0.5 rounded-full">
            {market.badge}
          </span>
        )}
      </div>
      <div className="flex items-center gap-4 text-xs">
        <span className="text-gray-400">5일 수익률: <span className={textCls}>{ret5Sign}{market.ret_5d_pct}%</span></span>
        <span className="text-gray-400">20일 수익률: <span className={textCls}>{ret20Sign}{market.ret_20d_pct}%</span></span>
        <span className="text-gray-400">모델 신뢰도:
          <span className={`ml-1 font-medium ${
            market.confidence === 'high' ? 'text-emerald-400' :
            market.confidence === 'low' ? 'text-red-400' : 'text-yellow-400'
          }`}>{
            market.confidence === 'high' ? '높음' :
            market.confidence === 'low' ? '낮음' : '보통'
          }</span>
        </span>
      </div>
    </div>
  )
}

// ── 일일 요약 ─────────────────────────────────────────────────

function DailySummaryCard({ summary, names, onClickSymbol, volSurgeSymbols }: { summary: DailySummary; names: Record<string, string>; onClickSymbol?: (sym: string, name: string) => void; volSurgeSymbols?: Set<string> }) {
  const [open, setOpen] = useState(true)
  const { market, top3, caution, model_confidence } = summary

  return (
    <div className="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-5 py-3 hover:bg-gray-800/40 transition-colors"
      >
        <div className="flex items-center gap-2">
          <span className="text-white font-semibold text-sm">📋 오늘의 아침 요약</span>
          <span className="text-gray-400 text-xs">{fmtDate(summary.date)}</span>
        </div>
        <svg className={`w-4 h-4 text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="px-5 pb-4 space-y-3 border-t border-gray-800">
          {/* 시장 상황 */}
          <div className="pt-3">
            <p className="text-gray-400 text-xs mb-1.5">시장 상황</p>
            <div className="flex items-center gap-3 flex-wrap">
              <span className={`font-bold text-sm ${
                market.trend === 'bull' ? 'text-emerald-400' :
                market.trend === 'bear' ? 'text-red-400' : 'text-yellow-400'
              }`}>{market.label}</span>
              <span className="text-gray-400 text-xs">20일: {market.ret_20d_pct >= 0 ? '+' : ''}{market.ret_20d_pct}%</span>
              <span className="text-gray-300 text-xs bg-gray-800 px-2 py-0.5 rounded">{caution}</span>
            </div>
          </div>

          {/* 핵심 3종목 */}
          <div>
            <p className="text-gray-400 text-xs mb-2">핵심 추천 3종목</p>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
              {top3.map((p, i) => {
                const { label, stars, cls } = signalInfo(displayProb(p))
                const code = normalizeSymbol(p.symbol)
                const name = p.name || names[code] || names[p.symbol] || code
                const isVolSurge = volSurgeSymbols?.has(p.symbol)
                return (
                  <div key={p.symbol}
                    className={`bg-gray-800/60 rounded-lg px-3 py-2 space-y-1 ${onClickSymbol ? 'cursor-pointer hover:bg-gray-700/60 transition-colors' : ''}`}
                    onClick={() => onClickSymbol?.(p.symbol, name)}
                  >
                    <div className="flex items-center gap-1.5">
                      <span className="text-gray-500 text-xs">#{i + 1}</span>
                      <span className="text-white font-medium text-sm truncate">{name}</span>
                      {isVolSurge && (
                        <span className="bg-orange-500/20 text-orange-400 text-[10px] px-1.5 py-0.5 rounded-full border border-orange-500/30 shrink-0">
                          ⚡ 거래량급등 섹션
                        </span>
                      )}
                    </div>
                    <div className="flex items-center justify-between">
                      <span className={`text-xs font-medium ${cls}`}>{label} {'★'.repeat(stars)}</span>
                      <span className={`text-xs font-bold ${cls}`}>{Math.round(displayProb(p) * 100)}%</span>
                    </div>
                    <p className="text-gray-500 text-xs truncate">
                      {p.shap_top[0] ? `${p.shap_top[0].direction === 'up' ? '▲' : '▼'} ${p.shap_top[0].label}` : ''}
                    </p>
                  </div>
                )
              })}
            </div>
          </div>

          {/* 주의사항 */}
          <div className="text-xs text-gray-500 border-t border-gray-800 pt-2">
            모델 신뢰도 <span className={`font-medium ${
              model_confidence === '높음' ? 'text-emerald-400' :
              model_confidence === '낮음' ? 'text-red-400' : 'text-yellow-400'
            }`}>{model_confidence}</span>
            &nbsp;·&nbsp;백테스트 기준 5일 보유 전략 · 개인 판단 후 실행
          </div>
        </div>
      )}
    </div>
  )
}

// ── Sparkline ──────────────────────────────────────────────────

function Sparkline({ prices }: { prices: number[] }) {
  if (prices.length < 2) return <div className="h-9 bg-gray-800 rounded animate-pulse" />
  const min = Math.min(...prices), max = Math.max(...prices)
  const range = max - min || 1
  const W = 100, H = 36
  const pts = prices.map((v, i) => `${(i / (prices.length - 1)) * W},${H - ((v - min) / range) * H}`).join(' ')
  const isUp = prices[prices.length - 1] >= prices[0]
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="w-full h-9">
      <polyline points={pts} fill="none" stroke={isUp ? '#EF4444' : '#3B82F6'} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

// ── 지표 용어 설명 ────────────────────────────────────────────

const FEATURE_HINTS: Record<string, string> = {
  'ATR 비율':       'Average True Range를 주가로 나눈 변동성 지표. 클수록 가격 등락폭이 큼',
  '볼린저 폭':      '볼린저밴드 상·하단 간격 ÷ 중간선. 수축은 큰 움직임 예고, 확장은 추세 확인',
  'PBR':            '주가순자산비율. 주가 ÷ 주당순자산. 1 미만이면 장부가 이하 거래 중',
  'PER':            '주가수익비율. 주가 ÷ 주당순이익. 낮을수록 이익 대비 저평가 가능성',
  'RSI(14)':        '14일 상대강도지수(0~100). 30 이하 과매도(반등 기대), 70 이상 과매수(조정 경계)',
  'RSI(7)':         '7일 상대강도지수. RSI(14)보다 민감해 단기 과매수·과매도를 빠르게 포착',
  'MA5 이격':       '현재가 ÷ 5일 이동평균 − 1. 양수 = 단기 평균선 위, 클수록 단기 과열',
  'MA20 이격':      '현재가 ÷ 20일 이동평균 − 1. 20일선은 단기 추세의 기준선',
  'MA60 이격':      '현재가 ÷ 60일 이동평균 − 1. 중기 추세 이탈 정도',
  '5일MA 이격도':   '현재가 ÷ 5일 이동평균 − 1. 단기 과열·침체 판단',
  '20일MA 이격도':  '현재가 ÷ 20일 이동평균 − 1',
  '60일MA 이격도':  '현재가 ÷ 60일 이동평균 − 1. 중기 추세',
  '120일MA 이격도': '현재가 ÷ 120일 이동평균 − 1. 장기 추세 이탈 정도. 양수 = 장기 평균선 위',
  '볼린저 위치':    '볼린저밴드 내 현재가 위치(0=하단, 1=상단). 0.1 이하 과매도권, 0.9 이상 과매수권',
  '거래량비(5일)':  '최근 5일 평균 거래량 ÷ 이전 기간 평균. 1 초과 = 거래 증가, 2 이상 = 급등 신호',
  '거래량비(20일)': '최근 20일 평균 거래량 ÷ 이전 기간 평균. 중기 매매 활발도',
  '시장대비(5일)':  '종목 5일 수익률 − KOSPI/KOSDAQ 5일 수익률. 양수 = 시장 초과 성과',
  '시장대비(20일)': '종목 20일 수익률 − KOSPI/KOSDAQ 20일 수익률. 양수 = 시장 초과 성과',
  '섹터대비(20일)': '종목 20일 수익률 − 동일 섹터 평균 수익률. 업종 내 상대적 강도',
  '5일 수익률':     '최근 5거래일 종가 기준 수익률',
  '60일 수익률':    '최근 60거래일 종가 기준 수익률. 중장기 모멘텀 확인',
  'MACD Hist':      'MACD − 신호선의 차. 양수 확대 = 상승 모멘텀 강화, 음수 확대 = 하락 가속',
  '외인 보유율':    '외국인 투자자 보유 비율(%). 높을수록 안정적 수요 기반',
  '외인 1일 변화':  '외국인 보유율의 전일 대비 변화(%p). 양수 = 순매수, 음수 = 순매도',
  '1일 수익률':     '전일 대비 당일 등락률(%). AI는 단기 모멘텀 신호로 활용',
  '5일 변동성':     '5일 일별 수익률의 표준편차. 단기 급등락 위험 지표. 클수록 변동성 높음',
  '20일 변동성':    '20일 일별 수익률의 표준편차. 클수록 가격 변동이 심한 고변동성 종목',
  '섹터대비(5일)':  '종목 5일 수익률 − 동일 섹터 5일 평균 수익률. 양수 = 업종 대비 단기 강세',
  '거래대금(5일평균)': '최근 5거래일 평균 거래대금. 클수록 시장 참여자 관심·유동성이 높음',
  'kospi_ma200_ratio': 'KOSPI 지수의 200일 이동평균 대비 현재 비율. 시장 전체 장기 추세 반영. 1 이상 = 장기 상승 추세',
  'market_breadth':    '시장 내 상승 종목 수 비율. 높을수록 시장 전반이 강세. AI가 시장 환경 판단에 사용',
}

// ── 배지 툴팁 ─────────────────────────────────────────────────

export function BadgeTooltip({ text, children, placement = 'top', className = '' }: {
  text: string
  children: React.ReactNode
  placement?: 'top' | 'top-right' | 'bottom'
  className?: string
}) {
  const boxCls = placement === 'top-right'
    ? 'bottom-full left-0 mb-1.5'
    : placement === 'bottom'
    ? 'top-full left-1/2 -translate-x-1/2 mt-1.5'
    : 'bottom-full left-1/2 -translate-x-1/2 mb-1.5'
  const arrowCls = placement === 'top-right'
    ? 'top-full left-4 border-t-gray-700'
    : placement === 'bottom'
    ? 'bottom-full left-1/2 -translate-x-1/2 border-b-gray-700'
    : 'top-full left-1/2 -translate-x-1/2 border-t-gray-700'
  return (
    <span className={`relative group/tip inline-flex ${className}`}>
      {children}
      <span className={`pointer-events-none absolute z-50 w-56 rounded-lg bg-gray-950 border border-gray-700 px-2.5 py-2
                       text-xs text-gray-300 leading-relaxed whitespace-normal
                       opacity-0 group-hover/tip:opacity-100 transition-opacity duration-150 shadow-2xl ${boxCls}`}>
        {text}
        <span className={`absolute border-4 border-transparent ${arrowCls}`} />
      </span>
    </span>
  )
}

// ── ShapChip ───────────────────────────────────────────────────

function ShapChip({ entry }: { entry: ShapEntry }) {
  const isUp = entry.direction === 'up'
  const hint = FEATURE_HINTS[entry.label]
  const valStr = entry.value != null ? ` (${typeof entry.value === 'number' ? entry.value.toFixed(2) : entry.value})` : ''
  const tooltipText = hint
    ? `${entry.label}${valStr}\n${hint}`
    : `${entry.label}${valStr} · AI가 ${isUp ? '상승' : '하락'} 신호로 판단한 지표`

  return (
    <BadgeTooltip text={tooltipText}>
      <span className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-medium whitespace-nowrap cursor-default ${
        isUp ? 'bg-red-500/10 text-red-400' : 'bg-blue-500/10 text-blue-400'
      }`}>
        {isUp ? '▲' : '▼'} <span className="truncate max-w-[5rem]">{entry.label}</span>
      </span>
    </BadgeTooltip>
  )
}

// ── 전략 배지 툴팁 텍스트 ──────────────────────────────────────

const ACTION_HINTS: Record<string, string> = {
  '관망 권장': 'ATR(14일 평균 변동폭) > 4% + 위험 경고 2개 이상. 변동성·리스크가 높아 추격 매수보다 관망을 권장합니다.',
  '신중 진입': 'ATR > 2% 또는 위험 경고 1개 이상. 분할 매수와 손절가 설정을 권장합니다.',
  '적극 고려': 'ATR < 2% + 위험 경고 없음. 변동성이 낮고 리스크 요인이 적어 적극 진입을 고려할 수 있습니다.',
}

const RISK_LEVEL_HINTS: Record<string, string> = {
  '높음': '고위험: 고변동성(20일 변동성 상위 30%) · 단기급등(5일 +20% 이상) · 코스닥 중 2가지 이상 해당. 포지션을 줄이고 손절 기준을 명확히 하세요.',
  '중간': '중위험: 고변동성 · 단기급등 · 코스닥 중 1가지 해당. 분할 매수와 리스크 관리를 권장합니다.',
  '낮음': '저위험: 위험 요인 없음. 상대적으로 안정적이나 절대적 안전을 보장하지는 않습니다.',
}

// ── 전략 바 (카드 하단) ─────────────────────────────────────────

function StrategyBar({ strategy }: { strategy: TradeStrategy }) {
  const actionColor =
    strategy.action_label === '관망 권장' ? 'text-red-400' :
    strategy.action_label === '신중 진입' ? 'text-yellow-400' :
    'text-emerald-400'

  const riskColor =
    strategy.risk_level === '높음' ? 'text-red-400/70' :
    strategy.risk_level === '중간' ? 'text-yellow-400/70' :
    'text-emerald-400/70'

  const t = strategy.exit_targets
  return (
    <div className="space-y-1 text-xs">
      <div className="flex items-center justify-between gap-1">
        <BadgeTooltip text={ACTION_HINTS[strategy.action_label] ?? strategy.action_label}>
          <span className={`font-semibold shrink-0 cursor-default ${actionColor}`}>{strategy.action_label}</span>
        </BadgeTooltip>
        <BadgeTooltip text={RISK_LEVEL_HINTS[strategy.risk_level] ?? strategy.risk_level}>
          <span className={`shrink-0 cursor-default ${riskColor}`}>{strategy.risk_level}위험</span>
        </BadgeTooltip>
      </div>
      <div className="text-gray-500">
        목표 <span className="text-red-400">{t.target1_pct}</span>
        {' '}/ 손절 <span className="text-blue-400">{t.stop_loss_pct}</span>
      </div>
    </div>
  )
}

// ── PredictionCard ─────────────────────────────────────────────

interface PredictionWithMeta extends Prediction {
  sparkline: number[]
}

function PredictionCard({
  pred, onClick, onCompareToggle, inCompare, watchlist, onStarChange, lowVolRegime,
}: {
  pred: PredictionWithMeta
  onClick: () => void
  onCompareToggle: (sym: string) => void
  inCompare: boolean
  watchlist: string[]
  onStarChange?: () => void
  /** 저변동성 구간 — 신뢰도 HIGH가 아닌 카드는 살짝 비강조 처리 (추천 자체는 그대로 유지) */
  lowVolRegime?: boolean
}) {
  const [starred, setStarred] = useState(watchlist.some(w => normalizeSymbol(w) === normalizeSymbol(pred.symbol)))
  const pct = Math.round(displayProb(pred) * 100)

  const handleStar = (e: React.MouseEvent) => {
    e.stopPropagation()
    const next = toggleWatchlist(normalizeSymbol(pred.symbol))
    setStarred(next)
    onStarChange?.()
  }
  const handleCompare = (e: React.MouseEvent) => {
    e.stopPropagation()
    onCompareToggle(pred.symbol)
  }

  const deemphasize = lowVolRegime && pred.confidence_level !== 'HIGH'

  return (
    <div className={`relative bg-gray-900 border rounded-xl p-4 transition-all space-y-3 ${
      inCompare
        ? 'border-indigo-500/60 shadow-lg shadow-indigo-500/10'
        : pred.risk_level === 'HIGH'
          ? 'border-red-500/30 hover:border-red-500/50'
          : 'border-gray-800 hover:border-emerald-500/40'
    } ${deemphasize ? 'opacity-60' : ''}`}>
      {/* 상단 버튼 */}
      <div className="absolute top-3 right-3 flex items-center gap-1.5">
        <button onClick={handleCompare} title="비교에 추가"
          className={`text-xs px-1.5 py-0.5 rounded transition-colors ${
            inCompare ? 'bg-indigo-500/30 text-indigo-300' : 'bg-gray-800 text-gray-500 hover:text-gray-300'
          }`}>비교</button>
        <button onClick={handleStar} title="관심 종목"
          className={`text-base transition-colors ${starred ? 'text-yellow-400' : 'text-gray-600 hover:text-gray-400'}`}>
          ★
        </button>
      </div>

      <button onClick={onClick} className="w-full text-left group space-y-3">
        <div className="flex items-start gap-2 pr-16">
          <span className="shrink-0 text-xs font-bold text-gray-400 bg-gray-800 rounded px-1.5 py-0.5 group-hover:text-white transition-colors">
            #{pred.rank}
          </span>
          <div className="min-w-0">
            <p className="text-white font-semibold text-sm leading-tight truncate">
              {pred.name !== pred.symbol ? pred.name : '—'}
            </p>
            <p className="text-gray-400 text-xs">{pred.symbol}</p>
          </div>
        </div>

        <div className="space-y-1">
          <SignalStrength prob={displayProb(pred)} />
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">상승 확률</span>
            <span className={`text-sm font-bold ${pct >= 75 ? 'text-emerald-400' : pct >= 60 ? 'text-green-400' : 'text-gray-300'}`}>{pct}%</span>
          </div>
          <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
            <div className={`h-full rounded-full ${pct >= 75 ? 'bg-emerald-500' : pct >= 60 ? 'bg-green-500/80' : 'bg-emerald-700/60'}`}
              style={{ width: `${pct}%` }} />
          </div>
        </div>

        <div className="flex flex-wrap gap-1">
          {pred.shap_top.slice(0, 3).map((s, i) => <ShapChip key={i} entry={s} />)}
          {(pred.vol_ratio_20d ?? 0) >= 1.5 && (
            <BadgeTooltip text={`20일 평균 거래량 대비 ${pred.vol_ratio_20d?.toFixed(1)}배 거래 중. 단기 변동성이 크고 급등락 가능성이 높습니다.`}>
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-orange-500/15 text-orange-400 border border-orange-500/30 whitespace-nowrap cursor-default">
                🔥 거래량 {pred.vol_ratio_20d?.toFixed(1)}×
              </span>
            </BadgeTooltip>
          )}
          {pred.price_surge_warning && (
            <BadgeTooltip text={`5일 수익률 ${((pred.ret_5d ?? 0) * 100).toFixed(1)}% / 당일 ${((pred.ret_1d ?? 0) * 100).toFixed(1)}% 급등. 추격 매수 시 단기 조정 위험이 있습니다.`}>
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-red-500/15 text-red-400 border border-red-500/30 whitespace-nowrap cursor-default">
                ⚠ 단기급등
              </span>
            </BadgeTooltip>
          )}
          {pred.investor_sell_warning && (
            <BadgeTooltip text="외국인+기관이 동시에 순매도하는데 개인만 순매수 중입니다(전일 기준). 룰 기반 참고 신호로, AI 모델 점수에는 반영되지 않습니다.">
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-amber-500/15 text-amber-400 border border-amber-500/30 whitespace-nowrap cursor-default">
                ⚠ 수급경고
              </span>
            </BadgeTooltip>
          )}
          {pred.high_vol_warning && (
            <BadgeTooltip text="전일 장중 변동폭(고가-저가/종가)이 전체 종목 상위 5%였습니다. 전일 변동성이 높았던 종목은 통계적으로 이후 5일 수익률이 평균보다 낮은 경향이 있습니다(IS/OOS p=0.0000). AI 모델 점수에는 반영되지 않는 참고 정보입니다.">
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-orange-500/15 text-orange-400 border border-orange-500/30 whitespace-nowrap cursor-default">
                ⚡ 변동성↑
              </span>
            </BadgeTooltip>
          )}
          {pred.dps_growth_flag && (
            <BadgeTooltip text="전년 대비 주당배당금(DPS)이 배당 종목 중 상위 25% 증가한 종목입니다. 배당 성장 상위 종목은 OOS 기준 5일~60일 전구간에서 초과수익이 관찰됐습니다(+0.35~+2.64%p, p≤0.003). AI 모델 점수에는 반영되지 않으며, 배당이 없는 종목엔 표시되지 않습니다.">
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 whitespace-nowrap cursor-default">
                📈 배당성장
              </span>
            </BadgeTooltip>
          )}
          {pred.gap_high && (
            <BadgeTooltip text={`최근 20거래일 평균 시가갭이 ${pred.trail_gap20d_pct?.toFixed(1) ?? '?'}%로 높습니다(기준 2%). 갭이 큰 종목은 장 시작 시 이미 상승분이 반영돼 실전 수익이 줄어드는 경향이 있습니다. AI 모델 추천 순위에서 자동으로 하위로 배치됩니다. 매수 시 시가를 직접 확인 후 결정하세요(C모드 갭 필터).`}>
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-yellow-500/15 text-yellow-400 border border-yellow-500/30 whitespace-nowrap cursor-default">
                ↑ 갭주의
              </span>
            </BadgeTooltip>
          )}
          {pred.confidence_level === 'HIGH' && (
            <BadgeTooltip text="신뢰도 HIGH: AI 확률 65% 이상 + 거래량 증가 + 상승 모멘텀이 모두 확인된 종목입니다.">
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 whitespace-nowrap cursor-default">
                ✓ 신뢰HIGH
              </span>
            </BadgeTooltip>
          )}
          {pred.confidence_level === 'MEDIUM' && (
            <BadgeTooltip text="신뢰도 MED: AI 확률 60% 이상이나 거래량·모멘텀 조건 중 일부가 미충족입니다.">
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-yellow-500/15 text-yellow-400 border border-yellow-500/30 whitespace-nowrap cursor-default">
                ~ 신뢰MED
              </span>
            </BadgeTooltip>
          )}
          {pred.risk_level === 'HIGH' && (
            <BadgeTooltip text={`위험 HIGH: ${pred.risk_factors?.join(', ') || '복합 위험 요인'}. 포지션 크기를 줄이고 손절 기준을 명확히 하세요.`}>
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-red-500/15 text-red-400 border border-red-500/30 whitespace-nowrap cursor-default">
                ⚠ 고위험
              </span>
            </BadgeTooltip>
          )}
          {pred.risk_level === 'MEDIUM' && (
            <BadgeTooltip text={`위험 MED: ${pred.risk_factors?.join(', ') || '일부 위험 요인 존재'}. 분할 매수 및 손절 설정을 권장합니다.`}>
              <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-xs font-semibold bg-orange-500/15 text-orange-400 border border-orange-500/30 whitespace-nowrap cursor-default">
                △ 중위험
              </span>
            </BadgeTooltip>
          )}
        </div>

        <div className="pt-1 border-t border-gray-800/60">
          {pred.sparkline.length > 0
            ? <Sparkline prices={pred.sparkline} />
            : <div className="h-9 bg-gray-800/50 rounded animate-pulse" />}
        </div>

        {pred.strategy && (
          <div className="pt-1 border-t border-gray-800/60">
            <StrategyBar strategy={pred.strategy} />
          </div>
        )}
      </button>

      {pred.top_reasons && pred.top_reasons.length > 0 && (() => {
        const maxImpact = Math.max(...pred.top_reasons.map(r => r.impact), 0.001)
        return (
          <details className="group border-t border-gray-800/60 pt-2">
            <summary className="cursor-pointer text-xs text-gray-500 hover:text-gray-300 list-none flex items-center gap-1 select-none">
              <svg className="w-3 h-3 transition-transform group-open:rotate-90 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
              </svg>
              추천 이유 보기
            </summary>
            <div className="mt-2 space-y-1.5">
              {pred.top_reasons.map((r, i) => (
                <div key={i}>
                  <div className="flex items-center justify-between text-xs mb-0.5">
                    <span className="text-gray-400 truncate pr-2">{r.label}</span>
                    <span className="text-emerald-400 shrink-0 font-medium">+{r.impact.toFixed(3)}</span>
                  </div>
                  <div className="h-1 bg-gray-800 rounded-full overflow-hidden">
                    <div className="h-full bg-emerald-500/50 rounded-full transition-all" style={{ width: `${(r.impact / maxImpact) * 100}%` }} />
                  </div>
                </div>
              ))}
            </div>
          </details>
        )
      })()}
    </div>
  )
}

// ── 비교 패널 ─────────────────────────────────────────────────

function ComparePanel({
  symbols, names, details, onClose,
}: {
  symbols: string[]
  names: Record<string, string>
  details: Record<string, TickerDetail | null>
  onClose: () => void
}) {
  // lowerIsBetter: true면 낮은 값이 초록색 (저평가 지표)
  const KEY_ROWS = [
    { key: 'probability',  label: '상승 확률',    fmt: (v: number) => `${Math.round(v * 100)}%`,  lowerIsBetter: false,
      hint: 'CatBoost 모델이 예측한 5거래일 내 상승 확률. 높을수록 AI가 상승 가능성을 높게 판단한 종목.' },
    { key: 'rank',         label: '순위',         fmt: (v: number) => `#${v}`,                    lowerIsBetter: true,
      hint: '오늘 전체 추천 종목 중 상승 확률 순위. 낮을수록(#1에 가까울수록) AI가 더 유망하게 본 종목.' },
    { key: 'rsi_14',       label: 'RSI(14)',      fmt: (v: number) => v.toFixed(1),                lowerIsBetter: false,
      hint: '14일 상대강도지수(0~100). 30 이하 → 과매도(반등 기대), 70 이상 → 과매수(조정 경계). 높을수록 단기 모멘텀 강함.' },
    { key: 'ma20_dev',     label: 'MA20 이격',    fmt: (v: number) => `${(v * 100).toFixed(1)}%`, lowerIsBetter: false,
      hint: '현재가 ÷ 20일 이동평균 − 1. 양수면 20일선 위에서 거래 중(단기 강세), 음수면 20일선 아래(단기 약세).' },
    { key: 'bb_pct',       label: '볼린저 위치',  fmt: (v: number) => v.toFixed(2),                lowerIsBetter: false,
      hint: '볼린저밴드 내 현재가 위치(0=하단, 1=상단). 0.1 이하 과매도권, 0.9 이상 과매수권. 높을수록 밴드 상단에 근접.' },
    { key: 'vol_ratio_5d', label: '거래량비(5일)', fmt: (v: number) => `${v.toFixed(2)}×`,         lowerIsBetter: false,
      hint: '최근 5일 평균 거래량 ÷ 이전 기간 평균. 1.0 = 평소 수준, 2.0× 이상이면 거래량 급등 신호.' },
    { key: 'foreign_rate', label: '외인 보유율',  fmt: (v: number) => `${v.toFixed(1)}%`,          lowerIsBetter: false,
      hint: '외국인 투자자 보유 비율(%). 높을수록 안정적 수요 기반. 외인 비중이 높은 종목은 급락 시 완충 역할.' },
    { key: 'per',          label: 'PER',          fmt: (v: number) => v.toFixed(1),                lowerIsBetter: true,
      hint: '주가수익비율 = 주가 ÷ 주당순이익. 낮을수록 이익 대비 저평가. 단, 성장주는 미래 기대치로 고PER이 정상일 수 있음.' },
    { key: 'pbr',          label: 'PBR',          fmt: (v: number) => v.toFixed(2),                lowerIsBetter: true,
      hint: '주가순자산비율 = 주가 ÷ 주당순자산. 낮을수록 장부가 대비 저평가. 1 미만이면 청산가치 이하 거래 중.' },
  ]

  return (
    <div className="bg-gray-900 border border-indigo-500/30 rounded-xl overflow-hidden">
      <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800">
        <span className="text-white font-semibold text-sm">종목 비교</span>
        <button onClick={onClose} className="text-gray-400 hover:text-white text-sm">닫기</button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-gray-800">
              <th className="text-left text-gray-500 px-4 py-2 font-normal w-28">지표</th>
              {symbols.map(s => {
                const code = normalizeSymbol(s)
                const displayName = names[code] || names[s] || code
                return (
                  <th key={s} className="text-center px-4 py-2 font-medium text-white">
                    <div>{displayName}</div>
                    <div className="text-gray-400 font-normal">{code}</div>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {KEY_ROWS.map(({ key, label, fmt, lowerIsBetter, hint }) => {
              const vals = symbols.map(s => {
                const d = details[s]
                if (!d) return null
                if (key === 'probability') return displayProb(d)
                if (key === 'rank') return d.rank
                return d.recent_features[key]
              })
              const nums = vals.filter((v): v is number => v != null)
              const best = nums.length
                ? (lowerIsBetter ? Math.min(...nums) : Math.max(...nums))
                : null

              return (
                <tr key={key} className="border-b border-gray-800/50 hover:bg-gray-800/20">
                  <td className="text-gray-400 px-4 py-2">
                    <BadgeTooltip text={hint} placement="top-right">
                      <span className="cursor-help border-b border-dashed border-gray-700">
                        {label}
                        {lowerIsBetter && <span className="ml-1 text-gray-600 text-xs">↓좋음</span>}
                      </span>
                    </BadgeTooltip>
                  </td>
                  {vals.map((v, i) => (
                    <td key={i} className={`text-center px-4 py-2 font-mono ${
                      v != null && v === best ? 'text-emerald-400 font-bold' : 'text-gray-300'
                    }`}>
                      {v == null ? '—' : fmt(v)}
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── SHAP Bar ───────────────────────────────────────────────────

function ShapBarChart({ shaps }: { shaps: ShapEntry[] }) {
  const visible = shaps.slice(0, 14)
  const maxAbs = Math.max(...visible.map(s => Math.abs(s.shap)), 0.001)
  return (
    <div className="space-y-2">
      {visible.map((s, i) => {
        const pct = (Math.abs(s.shap) / maxAbs) * 100
        const isUp = s.direction === 'up'
        const hint = FEATURE_HINTS[s.label]
        return (
          <div key={i} className="flex items-center gap-2 text-xs">
            {hint ? (
              <BadgeTooltip text={hint} placement="top-right" className="w-28 shrink-0 justify-end">
                <span className="text-gray-300 truncate cursor-help text-right w-full">{s.label}</span>
              </BadgeTooltip>
            ) : (
              <div className="w-28 text-gray-300 text-right truncate shrink-0">{s.label}</div>
            )}
            <div className="flex-1 relative h-3 bg-gray-800 rounded overflow-hidden">
              <div className={`absolute inset-y-0 left-0 rounded ${isUp ? 'bg-red-500/50' : 'bg-blue-500/50'}`}
                style={{ width: `${pct}%` }} />
            </div>
            <div className={`w-14 text-right font-mono tabular-nums ${isUp ? 'text-red-400' : 'text-blue-400'}`}>
              {isUp ? '+' : ''}{s.shap.toFixed(4)}
            </div>
          </div>
        )
      })}
      <p className="text-gray-400 text-xs pt-1">▲ 빨간색: 상승 기여 · ▼ 파란색: 하락 기여</p>
    </div>
  )
}

// ── Feature Grid ───────────────────────────────────────────────

const KEY_FEATURES: { key: string; label: string; fmt: (v: number) => string; hint?: (v: number) => { text: string; cls: string } | null }[] = [
  { key: 'rsi_14', label: 'RSI(14)', fmt: v => v.toFixed(1), hint: v => v < 30 ? { text: '과매도', cls: 'text-red-400' } : v > 70 ? { text: '과매수', cls: 'text-blue-400' } : null },
  { key: 'rsi_7', label: 'RSI(7)', fmt: v => v.toFixed(1) },
  { key: 'ma5_dev', label: 'MA5 이격', fmt: v => `${(v * 100).toFixed(2)}%` },
  { key: 'ma20_dev', label: 'MA20 이격', fmt: v => `${(v * 100).toFixed(2)}%` },
  { key: 'ma60_dev', label: 'MA60 이격', fmt: v => `${(v * 100).toFixed(2)}%` },
  { key: 'bb_pct', label: '볼린저 위치', fmt: v => v.toFixed(2), hint: v => v < 0.1 ? { text: '하단', cls: 'text-red-400' } : v > 0.9 ? { text: '상단', cls: 'text-blue-400' } : null },
  { key: 'macd_hist', label: 'MACD Hist', fmt: v => v.toFixed(4) },
  { key: 'vol_ratio_5d', label: '거래량비(5일)', fmt: v => `${v.toFixed(2)}×` },
  { key: 'foreign_rate', label: '외인 보유율', fmt: v => `${v.toFixed(1)}%` },
  { key: 'foreign_1d_chg', label: '외인 1일 변화', fmt: v => `${v > 0 ? '+' : ''}${v.toFixed(2)}%` },
  { key: 'per', label: 'PER', fmt: v => v.toFixed(1) },
  { key: 'pbr', label: 'PBR', fmt: v => v.toFixed(2) },
]

function FeatureGrid({ features }: { features: Record<string, number | null> }) {
  const items = KEY_FEATURES.filter(({ key }) => features[key] != null)
  if (items.length === 0) return null
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
      {items.map(({ key, label, fmt, hint }) => {
        const val = features[key] as number
        const h = hint?.(val)
        return (
          <div key={key} className="bg-gray-800/60 rounded-lg px-3 py-2">
            {FEATURE_HINTS[label] ? (
              <BadgeTooltip text={FEATURE_HINTS[label]} placement="top-right" className="mb-0.5">
                <span className="text-gray-400 text-xs cursor-help border-b border-dashed border-gray-600">{label}</span>
              </BadgeTooltip>
            ) : (
              <p className="text-gray-400 text-xs mb-0.5">{label}</p>
            )}
            <div className="flex items-baseline gap-1.5">
              <span className="text-white font-mono text-sm font-medium">{fmt(val)}</span>
              {h && <span className={`text-xs ${h.cls}`}>{h.text}</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ── 차트 ──────────────────────────────────────────────────────

type ModalTF = 'D' | 'W' | 'M'

function aggregatePriceBars(bars: PriceBar[], tf: ModalTF): PriceBar[] {
  if (tf === 'D') return bars
  const groups = new Map<string, PriceBar>()
  for (const b of bars) {
    const y = +b.time.slice(0, 4), mo = +b.time.slice(4, 6) - 1, day = +b.time.slice(6, 8)
    const d = new Date(Date.UTC(y, mo, day))
    let key: string
    let t: string
    if (tf === 'W') {
      const dow = d.getUTCDay()
      const diff = dow === 0 ? -6 : 1 - dow
      const mon = new Date(Date.UTC(y, mo, day + diff))
      key = mon.toISOString().slice(0, 10).replace(/-/g, '')
      t = key
    } else {
      key = `${y}${String(mo + 1).padStart(2, '0')}01`
      t = key
    }
    const g = groups.get(key)
    if (!g) groups.set(key, { time: t, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume })
    else { g.high = Math.max(g.high, b.high); g.low = Math.min(g.low, b.low); g.close = b.close; g.volume += b.volume }
  }
  return Array.from(groups.values()).sort((a, b) => a.time.localeCompare(b.time))
}

function ModalChart({ priceHistory, fullscreen = false }: { priceHistory: PriceBar[]; fullscreen?: boolean }) {
  const wrapRef   = useRef<HTMLDivElement>(null)
  const candleRef = useRef<HTMLDivElement>(null)
  const volRef    = useRef<HTMLDivElement>(null)
  const divRef    = useRef<HTMLDivElement>(null)

  const [tf, setTf] = useState<ModalTF>('D')

  // 거래량 패널 높이 (px). fullscreen에서 드래그 조절 가능
  const VOL_DEFAULT = fullscreen ? 100 : 60
  const VOL_MIN     = 40
  const VOL_MAX     = fullscreen ? 300 : 120
  const [volH, setVolH] = useState(VOL_DEFAULT)

  // 차트 인스턴스 ref (리사이즈·높이 변경 시 재사용)
  const candleChartRef = useRef<ReturnType<typeof createChart> | null>(null)
  const volChartRef    = useRef<ReturnType<typeof createChart> | null>(null)

  // 차트 생성
  useEffect(() => {
    if (!candleRef.current || !volRef.current || !wrapRef.current || priceHistory.length === 0) return
    const candleEl = candleRef.current
    const volEl    = volRef.current
    const w        = wrapRef.current.clientWidth

    const totalH   = fullscreen
      ? (wrapRef.current.clientHeight || window.innerHeight - 56)
      : 300
    const candleH  = totalH - volH - 6

    const candleChart = createChart(candleEl, {
      ...CHART_OPTS, height: candleH, width: w,
    })
    const candle = candleChart.addSeries(CandlestickSeries, {
      upColor: '#EF4444', downColor: '#3B82F6',
      borderUpColor: '#EF4444', borderDownColor: '#3B82F6',
      wickUpColor: '#EF4444', wickDownColor: '#3B82F6',
    })
    const agg = aggregatePriceBars(
      priceHistory.filter(p => p.close > 0 && p.open > 0 && p.high > 0),
      tf,
    )
    candle.setData(agg.map(p => ({
      time: yyyymmddToTs(p.time), open: p.open, high: p.high, low: p.low, close: p.close,
    })))
    candleChart.timeScale().fitContent()
    candleChartRef.current = candleChart

    const volChart = createChart(volEl, {
      ...CHART_OPTS, height: volH, width: w,
      rightPriceScale: { ...CHART_OPTS.rightPriceScale, scaleMargins: { top: 0.05, bottom: 0 } },
      timeScale: { ...CHART_OPTS.timeScale, visible: false },
    })
    const vol = volChart.addSeries(HistogramSeries, { priceScaleId: 'right' })
    vol.setData(agg.map(p => ({
      time: yyyymmddToTs(p.time),
      value: p.volume,
      color: p.close >= p.open ? 'rgba(239,68,68,0.5)' : 'rgba(59,130,246,0.5)',
    })))
    volChart.timeScale().fitContent()
    volChartRef.current = volChart

    candleChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (range) volChart.timeScale().setVisibleLogicalRange(range)
    })
    volChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (range) candleChart.timeScale().setVisibleLogicalRange(range)
    })

    const ro = new ResizeObserver(() => {
      if (!wrapRef.current || !candleRef.current) return
      const nw = wrapRef.current.clientWidth
      volChart.applyOptions({ width: nw })
      const h = candleRef.current.clientHeight
      if (h > 50) candleChart.applyOptions({ width: nw, height: h })
      else candleChart.applyOptions({ width: nw })
    })
    ro.observe(wrapRef.current)

    return () => { candleChart.remove(); volChart.remove(); ro.disconnect(); candleChartRef.current = null; volChartRef.current = null }
  }, [priceHistory, fullscreen, tf]) // eslint-disable-line react-hooks/exhaustive-deps

  // 거래량 높이 변경 시 차트 크기만 조정
  useEffect(() => {
    if (!candleChartRef.current || !volChartRef.current) return
    volChartRef.current.applyOptions({ height: volH })
    // DOM이 업데이트된 후 candleRef 실제 높이 읽기
    requestAnimationFrame(() => {
      if (!candleRef.current || !candleChartRef.current) return
      const h = candleRef.current.clientHeight
      if (h > 50) candleChartRef.current.applyOptions({ height: h })
    })
  }, [volH])

  // 드래그 핸들러 (fullscreen 전용)
  const onDividerMouseDown = (e: React.MouseEvent) => {
    if (!fullscreen) return
    e.preventDefault()
    const startY   = e.clientY
    const startVolH = volH
    const onMove = (me: MouseEvent) => {
      const delta = startY - me.clientY  // 위로 드래그 → 거래량 패널 커짐
      setVolH(Math.min(VOL_MAX, Math.max(VOL_MIN, startVolH + delta)))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  const tfButtons = (
    <div style={{ display: 'flex', gap: 4, marginBottom: 4 }}>
      {(['D', 'W', 'M'] as ModalTF[]).map(t => (
        <button key={t} onClick={() => setTf(t)} style={{
          fontSize: 11, padding: '2px 8px', borderRadius: 6, border: 'none', cursor: 'pointer',
          background: tf === t ? '#4f46e5' : '#1f2937', color: tf === t ? '#fff' : '#9ca3af',
        }}>{t === 'D' ? '일봉' : t === 'W' ? '주봉' : '월봉'}</button>
      ))}
    </div>
  )

  if (!fullscreen) {
    return (
      <div ref={wrapRef} style={{ width: '100%' }}>
        {tfButtons}
        <div ref={candleRef} />
        <div ref={divRef} style={{ height: 2, background: '#1f2937' }} />
        <div ref={volRef} />
      </div>
    )
  }

  return (
    <div ref={wrapRef} style={{ display: 'flex', flexDirection: 'column', width: '100%', height: '100%' }}>
      <div style={{ flexShrink: 0, padding: '4px 8px' }}>{tfButtons}</div>
      <div ref={candleRef} style={{ flex: 1, minHeight: 0 }} />
      <div
        ref={divRef}
        onMouseDown={onDividerMouseDown}
        style={{
          height: 6, flexShrink: 0,
          background: '#374151',
          cursor: 'ns-resize',
          borderTop: '1px solid #4b5563',
          borderBottom: '1px solid #4b5563',
        }}
      />
      <div ref={volRef} style={{ height: volH, flexShrink: 0 }} />
    </div>
  )
}

function BacktestChart({ dates, values }: { dates: string[]; values: number[] }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!ref.current || dates.length === 0) return
    const el = ref.current
    const isPos = values[values.length - 1] >= 0
    const chart = createChart(el, { ...CHART_OPTS, height: 200, width: el.clientWidth })
    const series = chart.addSeries(LineSeries, { color: isPos ? '#10B981' : '#EF4444', lineWidth: 2 })
    series.setData(dates.map((d, i) => ({ time: yyyymmddToTs(d), value: values[i] })))
    series.createPriceLine({ price: 0, color: '#374151', lineWidth: 1, lineStyle: 2, axisLabelVisible: false })
    chart.timeScale().fitContent()
    const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
    ro.observe(el)
    return () => { chart.remove(); ro.disconnect() }
  }, [dates, values])
  return <div ref={ref} />
}

// ── 메모 패널 ─────────────────────────────────────────────────

function MemoPanel({ symbol }: { symbol: string }) {
  const [text, setText] = useState(() => getMemo(symbol))
  const [saved, setSaved] = useState(false)
  const save = () => { saveMemo(symbol, text); setSaved(true); setTimeout(() => setSaved(false), 1500) }
  return (
    <div className="space-y-2">
      <textarea
        value={text}
        onChange={e => setText(e.target.value)}
        placeholder="이 종목에 대한 본인 분석, 매수 이유, 주의사항 등을 기록하세요..."
        className="w-full h-28 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-indigo-500 resize-none"
      />
      <div className="flex items-center justify-end gap-2">
        {saved && <span className="text-emerald-400 text-xs">저장됨</span>}
        <button onClick={save} className="text-xs bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-1.5 rounded-lg transition-colors">
          저장
        </button>
      </div>
    </div>
  )
}

// ── 가상 매매 패널 ────────────────────────────────────────────

function VirtualTradePanel({ symbol }: { symbol: string }) {
  const [trades, setTrades] = useState<VirtualTrade[]>(() => getVirtualTrades(symbol))
  const [form, setForm] = useState({ action: 'buy' as 'buy' | 'sell', date: todayStr(), price: '', shares: '', note: '' })

  const addTrade = () => {
    if (!form.price || !form.shares) return
    addVirtualTrade({ symbol, ...form, price: +form.price, shares: +form.shares })
    setTrades(getVirtualTrades(symbol))
    setForm(f => ({ ...f, price: '', shares: '', note: '' }))
  }
  const delTrade = (id: string) => { deleteVirtualTrade(id); setTrades(getVirtualTrades(symbol)) }

  const totalBuy = trades.filter(t => t.action === 'buy').reduce((s, t) => s + t.price * t.shares, 0)
  const totalSell = trades.filter(t => t.action === 'sell').reduce((s, t) => s + t.price * t.shares, 0)
  const pnl = totalSell - totalBuy

  return (
    <div className="space-y-3">
      {/* 입력 폼 */}
      <div className="bg-gray-800/60 rounded-lg p-3 space-y-2">
        <p className="text-gray-300 text-xs font-medium">거래 기록 추가</p>
        <div className="grid grid-cols-2 gap-2">
          <select value={form.action} onChange={e => setForm(f => ({ ...f, action: e.target.value as 'buy' | 'sell' }))}
            className="bg-gray-700 text-white text-xs rounded px-2 py-1.5 focus:outline-none">
            <option value="buy">매수</option>
            <option value="sell">매도</option>
          </select>
          <input type="text" value={form.date} onChange={e => setForm(f => ({ ...f, date: e.target.value }))}
            placeholder="날짜 YYYYMMDD" className="bg-gray-700 text-white text-xs rounded px-2 py-1.5 focus:outline-none placeholder:text-gray-500" />
          <input type="number" value={form.price} onChange={e => setForm(f => ({ ...f, price: e.target.value }))}
            placeholder="가격 (원)" className="bg-gray-700 text-white text-xs rounded px-2 py-1.5 focus:outline-none placeholder:text-gray-500" />
          <input type="number" value={form.shares} onChange={e => setForm(f => ({ ...f, shares: e.target.value }))}
            placeholder="수량 (주)" className="bg-gray-700 text-white text-xs rounded px-2 py-1.5 focus:outline-none placeholder:text-gray-500" />
        </div>
        <input type="text" value={form.note} onChange={e => setForm(f => ({ ...f, note: e.target.value }))}
          placeholder="메모 (선택)" className="w-full bg-gray-700 text-white text-xs rounded px-2 py-1.5 focus:outline-none placeholder:text-gray-500" />
        <button onClick={addTrade} className="w-full text-xs bg-indigo-600 hover:bg-indigo-500 text-white py-1.5 rounded transition-colors">
          기록 추가
        </button>
      </div>

      {/* 손익 요약 */}
      {trades.length > 0 && (
        <div className="flex items-center justify-between bg-gray-800/40 rounded-lg px-3 py-2 text-xs">
          <span className="text-gray-400">가상 손익</span>
          <span className={`font-bold ${pnl >= 0 ? 'text-red-400' : 'text-blue-400'}`}>
            {pnl >= 0 ? '+' : ''}{pnl.toLocaleString()}원
          </span>
        </div>
      )}

      {/* 거래 목록 */}
      <div className="space-y-1.5 max-h-40 overflow-y-auto">
        {trades.length === 0 && <p className="text-gray-500 text-xs text-center py-3">거래 기록 없음</p>}
        {trades.map(t => (
          <div key={t.id} className="flex items-center justify-between bg-gray-800/50 rounded-lg px-3 py-2 text-xs">
            <div className="flex items-center gap-2">
              <span className={`font-bold ${t.action === 'buy' ? 'text-red-400' : 'text-blue-400'}`}>
                {t.action === 'buy' ? '매수' : '매도'}
              </span>
              <span className="text-gray-400">{fmtDate(t.date)}</span>
              <span className="text-white">{t.price.toLocaleString()}원 × {t.shares}주</span>
              {t.note && <span className="text-gray-500 truncate max-w-[8rem]">{t.note}</span>}
            </div>
            <button onClick={() => delTrade(t.id)} className="text-gray-600 hover:text-red-400 transition-colors ml-2">✕</button>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── 매매 전략 패널 ────────────────────────────────────────────

function StrategyPanel({ strategy }: { strategy?: TradeStrategy }) {
  if (!strategy) {
    return <p className="text-gray-500 text-sm py-8 text-center">전략 데이터 없음</p>
  }

  const actionColor =
    strategy.action_label === '관망 권장' ? { bg: 'bg-red-500/10', border: 'border-red-500/30', text: 'text-red-400' } :
    strategy.action_label === '신중 진입' ? { bg: 'bg-yellow-500/10', border: 'border-yellow-500/30', text: 'text-yellow-400' } :
    { bg: 'bg-emerald-500/10', border: 'border-emerald-500/30', text: 'text-emerald-400' }

  const riskColor =
    strategy.risk_level === '높음' ? 'text-red-400' :
    strategy.risk_level === '중간' ? 'text-yellow-400' :
    'text-emerald-400'

  const t = strategy.exit_targets
  const e = strategy.entry

  return (
    <div className="space-y-4">
      {/* 진입 판단 */}
      <div className={`rounded-xl border p-4 ${actionColor.bg} ${actionColor.border}`}>
        <div className="flex items-center gap-3 mb-2">
          <BadgeTooltip text={ACTION_HINTS[strategy.action_label] ?? strategy.action_label}>
            <span className={`text-base font-bold cursor-default ${actionColor.text}`}>{strategy.action_label}</span>
          </BadgeTooltip>
          <BadgeTooltip text={RISK_LEVEL_HINTS[strategy.risk_level] ?? strategy.risk_level}>
            <span className={`text-xs px-2 py-0.5 rounded-full border cursor-default ${actionColor.border} ${actionColor.text}`}>
              위험 {strategy.risk_level}
            </span>
          </BadgeTooltip>
        </div>
        <p className="text-gray-300 text-xs leading-relaxed">{strategy.action_reason}</p>
      </div>

      {/* 경고 */}
      {strategy.warnings.length > 0 && (
        <div className="space-y-1.5">
          {strategy.warnings.map((w, i) => (
            <div key={i} className="flex items-start gap-2 bg-yellow-500/5 border border-yellow-500/20 rounded-lg px-3 py-2 text-xs text-yellow-300">
              <span className="shrink-0 mt-0.5">⚠</span>
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {/* 2열 레이아웃: 진입가 + 목표/손절 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {/* 진입 가격대 */}
        <div className="bg-gray-800/60 rounded-xl p-4 space-y-3">
          <p className="text-gray-300 text-xs font-semibold uppercase tracking-wide">진입 가격대</p>
          {e.caution && (
            <p className="text-red-400 text-xs">{e.caution}</p>
          )}
          {e.warning && (
            <p className="text-yellow-400 text-xs">{e.warning}</p>
          )}
          <div className="space-y-2">
            <div className="flex justify-between items-center text-xs">
              <span className="text-gray-400">매수 하한</span>
              <span className="text-white font-mono font-medium">
                {e.buy_price_low.toLocaleString()}원
                <span className="text-blue-400 ml-1">({e.buy_low_pct})</span>
              </span>
            </div>
            <div className="flex justify-between items-center text-xs">
              <span className="text-gray-400">매수 상한</span>
              <span className="text-white font-mono font-medium">
                {e.buy_price_high.toLocaleString()}원
                <span className="text-red-400 ml-1">({e.buy_high_pct})</span>
              </span>
            </div>
          </div>
        </div>

        {/* 목표가 / 손절가 */}
        <div className="bg-gray-800/60 rounded-xl p-4 space-y-3">
          <div className="flex items-baseline gap-2 flex-wrap">
            <p className="text-gray-300 text-xs font-semibold uppercase tracking-wide">목표가 / 손절가</p>
            <span className="text-gray-500 text-xs">참고치 (ATR 기반) — 백테스트 검증 규칙 아님</span>
          </div>
          <div className="space-y-2">
            <div className="flex justify-between items-start text-xs gap-2">
              <span className="text-red-400 shrink-0">목표 1</span>
              <div className="text-right">
                <p className="text-white font-mono">{t.target1_price.toLocaleString()}원 <span className="text-red-400">({t.target1_pct})</span></p>
                <p className="text-gray-500">{t.target1_action}</p>
              </div>
            </div>
            <div className="flex justify-between items-start text-xs gap-2">
              <span className="text-red-300 shrink-0">목표 2</span>
              <div className="text-right">
                <p className="text-white font-mono">{t.target2_price.toLocaleString()}원 <span className="text-red-300">({t.target2_pct})</span></p>
                <p className="text-gray-500">{t.target2_action}</p>
              </div>
            </div>
            <div className="border-t border-gray-700 pt-2 flex justify-between items-start text-xs gap-2">
              <span className="text-blue-400 shrink-0">손절</span>
              <div className="text-right">
                <p className="text-white font-mono">{t.stop_loss_price.toLocaleString()}원 <span className="text-blue-400">({t.stop_loss_pct})</span></p>
                <p className="text-gray-500">{t.stop_loss_action}</p>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 포지션 크기 + 보유 전략 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="bg-gray-800/60 rounded-xl p-4 space-y-2">
          <p className="text-gray-300 text-xs font-semibold uppercase tracking-wide">포지션 비중</p>
          <div className="flex items-baseline gap-2">
            <span className={`text-2xl font-bold ${riskColor}`}>{strategy.position.weight_pct}%</span>
            <span className="text-gray-400 text-xs">변동성 {strategy.position.weight_label}</span>
          </div>
          <p className="text-gray-500 text-xs">총 투자금 대비 이 종목 권장 비중</p>
        </div>
        <div className="bg-gray-800/60 rounded-xl p-4 space-y-2">
          <p className="text-gray-300 text-xs font-semibold uppercase tracking-wide">보유 전략</p>
          <p className="text-white text-xs font-medium">최대 {strategy.holding.max_days}거래일</p>
          <p className="text-gray-400 text-xs leading-relaxed">{strategy.holding.strategy}</p>
        </div>
      </div>
    </div>
  )
}

// ── 상세 모달 ─────────────────────────────────────────────────

type ModalTab = 'analysis' | 'strategy' | 'memo' | 'trade'

export function DetailModal({
  symbol, name, detail, loading, error, onClose,
}: {
  symbol: string; name: string; detail: TickerDetail | null
  loading: boolean; error: string | null; onClose: () => void
}) {
  const [tab, setTab] = useState<ModalTab>('analysis')
  const [starred, setStarred] = useState(() => getWatchlist().includes(symbol))
  const [chartFullscreen, setChartFullscreen] = useState(false)
  const [fullHistory, setFullHistory] = useState<PriceBar[] | null>(null)
  const [fullHistoryLoading, setFullHistoryLoading] = useState(false)

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') { if (chartFullscreen) { setChartFullscreen(false) } else { onClose() } } }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, chartFullscreen])

  const openFullscreen = async () => {
    setChartFullscreen(true)
    if (!fullHistory && !fullHistoryLoading) {
      setFullHistoryLoading(true)
      try {
        const d = await aiApi.ticker(symbol, 250)
        setFullHistory(d.price_history)
      } catch { /* 실패 시 기존 60일 데이터 사용 */ }
      finally { setFullHistoryLoading(false) }
    }
  }

  const handleStar = () => { const next = toggleWatchlist(normalizeSymbol(symbol)); setStarred(next) }

  const TABS: { id: ModalTab; label: string }[] = [
    { id: 'analysis', label: 'AI 분석' },
    { id: 'strategy', label: '매매 전략' },
    { id: 'memo', label: '메모' },
    { id: 'trade', label: '가상 매매' },
  ]

  return (
    <>
    {chartFullscreen && detail && createPortal(
      <div
        className="fixed inset-0 z-[200] bg-black/95 flex flex-col"
        onClick={() => setChartFullscreen(false)}
      >
        <div className="flex items-center justify-between px-6 py-3 border-b border-gray-800" onClick={e => e.stopPropagation()}>
          <div>
            <span className="text-white font-semibold">{name && name !== symbol ? name : symbol}</span>
            <span className="text-gray-400 text-sm ml-2">{symbol}</span>
          </div>
          <button onClick={() => setChartFullscreen(false)} className="text-gray-400 hover:text-white text-sm px-3 py-1 rounded bg-gray-800 hover:bg-gray-700 transition-colors">
            ✕ 닫기
          </button>
        </div>
        <div className="flex-1 relative overflow-hidden" onClick={e => e.stopPropagation()}>
          {fullHistoryLoading && (
            <div className="absolute inset-0 flex items-center justify-center z-10 bg-black/50">
              <div className="w-8 h-8 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin" />
            </div>
          )}
          <ModalChart priceHistory={fullHistory ?? detail.price_history} fullscreen />
        </div>
      </div>,
      document.body
    )}
    <div className="fixed inset-0 z-50 bg-black/80 flex items-start justify-center p-4 overflow-y-auto" onClick={onClose}>
      <div className="bg-gray-900 border border-gray-800 rounded-2xl w-full max-w-4xl my-8" onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-800 sticky top-0 bg-gray-900 rounded-t-2xl z-10">
          <div className="flex items-center gap-4 min-w-0">
            <button onClick={handleStar} className={`text-xl transition-colors shrink-0 ${starred ? 'text-yellow-400' : 'text-gray-600 hover:text-gray-300'}`}>★</button>
            <div className="min-w-0">
              <h2 className="text-white font-bold text-base leading-tight truncate">{name && name !== symbol ? name : symbol}</h2>
              <p className="text-gray-400 text-xs">{symbol}</p>
            </div>
            {detail && (
              <>
                <div className="h-8 w-px bg-gray-700 shrink-0" />
                <div className="shrink-0 space-y-0.5">
                  <SignalStrength prob={displayProb(detail)} size="md" />
                  <p className={`text-lg font-bold leading-none ${displayProb(detail) >= 0.7 ? 'text-emerald-400' : 'text-gray-200'}`}>
                    {Math.round(displayProb(detail) * 100)}%
                  </p>
                </div>
                {detail.rank != null && (
                  <>
                    <div className="h-8 w-px bg-gray-700 shrink-0" />
                    <div className="text-center shrink-0">
                      <p className="text-lg font-bold text-gray-200">#{detail.rank}</p>
                      <p className="text-gray-400 text-xs">전체 순위</p>
                    </div>
                  </>
                )}
                <div className="h-8 w-px bg-gray-700 shrink-0" />
                <p className="text-gray-400 text-xs shrink-0">{fmtDate(detail.date)} 기준</p>
              </>
            )}
          </div>
          <button onClick={onClose} className="shrink-0 ml-3 text-gray-400 hover:text-white transition-colors p-1">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-gray-800">
          {TABS.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`px-6 py-2.5 text-sm font-medium transition-colors ${
                tab === t.id ? 'text-white border-b-2 border-indigo-500' : 'text-gray-400 hover:text-gray-200'
              }`}>{t.label}</button>
          ))}
        </div>

        {/* Body */}
        <div className="p-6">
          {loading && (
            <div className="flex items-center justify-center h-64">
              <div className="space-y-2 text-center">
                <div className="w-8 h-8 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin mx-auto" />
                <p className="text-gray-500 text-sm">분석 데이터 로딩 중...</p>
              </div>
            </div>
          )}
          {error && <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-4 text-red-400 text-sm">{error}</div>}

          {detail && !loading && (
            <>
              {tab === 'analysis' && (
                <div className="space-y-6">
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    <div className="space-y-2">
                      <h3 className="text-white font-medium text-sm">AI 예측 근거 (SHAP 기여도)</h3>
                      <ShapBarChart shaps={detail.shap_full} />
                    </div>
                    <div className="space-y-2">
                      <div className="flex items-center justify-between">
                        <h3 className="text-white font-medium text-sm">최근 {detail.price_history.length}일 주가</h3>
                        <button
                          onClick={openFullscreen}
                          className="text-xs text-gray-400 hover:text-white transition-colors px-2 py-1 rounded bg-gray-800 hover:bg-gray-700"
                          title="차트 확대 (1년)"
                        >⛶ 확대</button>
                      </div>
                      <ModalChart priceHistory={detail.price_history} />
                    </div>
                  </div>
                  <div>
                    <h3 className="text-white font-medium text-sm mb-3">주요 기술 지표</h3>
                    <FeatureGrid features={detail.recent_features} />
                  </div>
                  <details className="group">
                    <summary className="cursor-pointer text-xs text-gray-400 hover:text-gray-200 transition-colors select-none list-none flex items-center gap-1">
                      <svg className="w-3 h-3 transition-transform group-open:rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                      </svg>
                      지표 용어 설명
                    </summary>
                    <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2">
                      {Object.entries(FEATURE_HINTS).map(([term, desc]) => (
                        <div key={term} className="bg-gray-800/40 rounded-lg px-3 py-2 space-y-0.5">
                          <p className="text-gray-200 text-xs font-semibold">{term}</p>
                          <p className="text-gray-400 text-xs leading-relaxed">{desc}</p>
                        </div>
                      ))}
                    </div>
                  </details>
                </div>
              )}
              {tab === 'strategy' && (
                <StrategyPanel strategy={detail.strategy} />
              )}
              {tab === 'memo' && (
                <div>
                  <p className="text-gray-400 text-xs mb-3">개인 분석 메모 (기기 로컬 저장)</p>
                  <MemoPanel symbol={symbol} />
                </div>
              )}
              {tab === 'trade' && (
                <div>
                  <p className="text-gray-400 text-xs mb-3">가상 매수/매도 기록 (실제 거래 아님, 기기 로컬 저장)</p>
                  <VirtualTradePanel symbol={symbol} />
                </div>
              )}
            </>
          )}
          {!detail && !loading && !error && tab !== 'analysis' && (
            <div className="space-y-4">
              {tab === 'strategy' && <StrategyPanel />}
              {tab === 'memo' && <MemoPanel symbol={symbol} />}
              {tab === 'trade' && <VirtualTradePanel symbol={symbol} />}
            </div>
          )}
        </div>
      </div>
    </div>
    </>
  )
}

// ── 모의투자 재분석 리포트 배너 ─────────────────────────────────

const REANALYSIS_DISMISS_KEY = 'ai_reanalysis_dismissed_at'

function ReanalysisBanner({ info }: { info: { available: boolean; generated_at?: string } | null }) {
  const [dismissed, setDismissed] = useState(false)
  if (!info?.available || dismissed) return null
  if (typeof localStorage !== 'undefined' && localStorage.getItem(REANALYSIS_DISMISS_KEY) === info.generated_at) {
    return null
  }
  const handleDismiss = () => {
    if (info.generated_at) localStorage.setItem(REANALYSIS_DISMISS_KEY, info.generated_at)
    setDismissed(true)
  }
  return (
    <div className="rounded-xl border border-indigo-500/30 bg-indigo-500/10 px-4 py-3 flex items-center justify-between gap-3 flex-wrap">
      <span className="text-indigo-300 text-sm font-medium">
        📊 모델 재분석 리포트가 준비됐습니다 — backend/reanalysis_report.md 확인
      </span>
      <button
        onClick={handleDismiss}
        className="text-xs text-indigo-400 hover:text-indigo-200 shrink-0"
      >닫기</button>
    </div>
  )
}

// ── 시장 국면 정보 배너 ─────────────────────────────────────────
// 2026-07-12: 기각된 필터 로직(probability threshold, max_count 축소) 제거.
// 이 배너는 순수 정보 표시만 — 추천 종목 수·확률 필터에 영향 없음.

function MarketModeBanner({ mode, message }: { mode: string; message: string }) {
  if (mode === 'aggressive' || !message) return null

  const isDefensive = mode === 'defensive'

  return (
    <div className={`rounded-xl border px-4 py-3 ${
      isDefensive
        ? 'border-red-500/30 bg-red-500/10'
        : 'border-yellow-500/30 bg-yellow-500/10'
    }`}>
      <div className="flex items-center gap-2">
        <span className="text-base">{isDefensive ? '🔴' : '🟡'}</span>
        <span className={`font-semibold text-sm ${isDefensive ? 'text-red-300' : 'text-yellow-300'}`}>
          {message}
        </span>
        <span className={`text-xs ml-1 ${isDefensive ? 'text-red-400/70' : 'text-yellow-400/70'}`}>
          (정보 표시 — 추천 결과 미변경)
        </span>
      </div>
    </div>
  )
}

// 약세장 가드(방향성 신호)와는 독립적인 신호라 동시에 뜰 수 있음 — 의도된 동작.
function VolatilityRegimeBanner({ regime }: { regime: VolatilityRegime | null }) {
  const [expanded, setExpanded] = useState(false)
  if (!regime || !regime.low_vol_warning) return null

  const precisionPct = regime.historical_precision_at_10 != null
    ? Math.round(regime.historical_precision_at_10 * 100)
    : null

  return (
    <div className="rounded-xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 space-y-2">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-base">💧</span>
          <span className="font-semibold text-sm text-sky-300">
            저변동성 장 — 모델 신뢰도 낮음{precisionPct != null ? ` (이 구간 과거 정밀도 약 ${precisionPct}%)` : ''}
          </span>
        </div>
        <button
          onClick={() => setExpanded(e => !e)}
          className="text-xs shrink-0 text-sky-400 hover:text-sky-200 transition-colors"
        >
          {expanded ? '닫기 ▲' : '자세히 보기 ▼'}
        </button>
      </div>
      {expanded && (
        <p className="text-xs leading-relaxed border-t border-sky-500/20 pt-2 text-sky-300/80">
          시장 변동성이 과거 분포 기준 하위 구간({regime.quartile})입니다. 진단 결과 이 구간에서는
          모델의 top10 정밀도가 베이스레이트 수준까지 떨어지는 경향이 있었습니다(단, 실제 5일
          평균 수익률 자체는 손실로 이어지지 않아 추천 개수는 줄이지 않았습니다 — 확신도 HIGH
          종목 위주로 참고하세요).
        </p>
      )}
    </div>
  )
}

// ── 공시 위험 제외 종목 ────────────────────────────────────────

const SEVERITY_STYLES = {
  critical: {
    badge: 'bg-red-500/20 text-red-300 border border-red-500/40',
    panel: 'bg-red-500/5 border border-red-500/20',
    accent: 'text-red-400',
    dot: 'bg-red-400',
  },
  high: {
    badge: 'bg-orange-500/20 text-orange-300 border border-orange-500/40',
    panel: 'bg-orange-500/5 border border-orange-500/20',
    accent: 'text-orange-400',
    dot: 'bg-orange-400',
  },
  medium: {
    badge: 'bg-yellow-500/20 text-yellow-300 border border-yellow-500/40',
    panel: 'bg-yellow-500/5 border border-yellow-500/20',
    accent: 'text-yellow-400',
    dot: 'bg-yellow-400',
  },
} as const

function ExcludedStockRow({
  ex, name, code, onClickSymbol,
}: {
  ex: ExcludedStock
  name: string
  code: string
  onClickSymbol: (symbol: string, name: string) => void
}) {
  const [analysisOpen, setAnalysisOpen] = useState(false)
  const a = ex.analysis
  const sty = a ? SEVERITY_STYLES[a.severity] ?? SEVERITY_STYLES.high : SEVERITY_STYLES.high

  return (
    <div className="border-b border-gray-800/50 last:border-0">
      {/* 종목 행 */}
      <div
        onClick={() => onClickSymbol(code, name)}
        className="flex flex-col sm:flex-row sm:items-start gap-3 px-5 py-3 hover:bg-gray-800/30 transition-colors cursor-pointer"
      >
        <div className="sm:w-40 shrink-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            {ex.rank != null && (
              <span className="text-gray-500 text-xs font-mono">#{ex.rank}</span>
            )}
            <p className="text-white text-sm font-medium truncate">{name}</p>
            {a && (
              <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${sty.badge}`}>
                {a.severity_label}
              </span>
            )}
          </div>
          <p className="text-gray-400 text-xs mt-0.5">{code}</p>
        </div>

        {/* 공시 목록 */}
        <div className="flex-1 space-y-1.5">
          {ex.risks.map((risk, i) => (
            <div key={i} className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs">
              <span className="shrink-0 bg-orange-500/15 text-orange-300 border border-orange-500/30 px-1.5 py-0.5 rounded font-medium">
                {risk.matched_keyword}
              </span>
              {risk.rcept_no ? (
                <a
                  href={`https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${risk.rcept_no}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={e => e.stopPropagation()}
                  className="text-orange-300/80 hover:text-orange-200 underline underline-offset-2 flex-1 min-w-0 truncate"
                >
                  {risk.report_nm}
                </a>
              ) : (
                <span className="text-gray-300 flex-1 min-w-0 truncate">{risk.report_nm}</span>
              )}
              <span className="shrink-0 text-gray-500">{fmtDate(risk.rcept_dt)}</span>
            </div>
          ))}
        </div>

        {/* 분석 토글 버튼 */}
        {a && (
          <button
            onClick={e => { e.stopPropagation(); setAnalysisOpen(o => !o) }}
            className={`shrink-0 text-xs px-2.5 py-1 rounded-lg border transition-colors ${
              analysisOpen
                ? `${sty.badge} opacity-100`
                : 'border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500'
            }`}
          >
            {analysisOpen ? '분석 닫기' : '영향 분석 ▾'}
          </button>
        )}
      </div>

      {/* 분석 패널 */}
      {a && analysisOpen && (
        <div className={`mx-4 mb-3 rounded-xl p-4 ${sty.panel}`}>
          {/* 헤더 */}
          <div className="flex flex-wrap items-center gap-3 mb-3">
            <span className={`text-xs font-semibold uppercase tracking-wide ${sty.accent}`}>
              주가 영향 분석
            </span>
            <div className="flex items-center gap-2 text-xs text-gray-300">
              <span className="text-gray-500">방향</span>
              <span className={`font-medium ${sty.accent}`}>{a.direction}</span>
            </div>
            <div className="flex items-center gap-2 text-xs text-gray-300">
              <span className="text-gray-500">예상 등락</span>
              <span className="font-medium text-white">{a.range}</span>
            </div>
            <div className="flex items-center gap-2 text-xs text-gray-300">
              <span className="text-gray-500">반응 시점</span>
              <span className="text-gray-300">{a.timing}</span>
            </div>
          </div>

          {/* 분석 본문 */}
          <p className="text-gray-300 text-xs leading-relaxed">{a.analysis}</p>

          {/* 재순위 가능 여부 */}
          <div className={`mt-3 pt-3 border-t ${
            a.severity === 'critical' ? 'border-red-500/20' :
            a.severity === 'high' ? 'border-orange-500/20' : 'border-yellow-500/20'
          }`}>
            {a.rerank_eligible ? (
              <p className="text-xs text-yellow-300/80">
                ⚡ 아래 재순위 결과에 주의 등급으로 포함됩니다 — 공시 원문 확인 후 판단하세요.
              </p>
            ) : (
              <p className={`text-xs ${sty.accent}`}>
                ✕ 위험도가 높아 재순위에서도 제외됩니다.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function ExcludedSection({
  excluded, names, onClickSymbol,
}: {
  excluded: ExcludedStock[]
  names: Record<string, string>
  onClickSymbol: (symbol: string, name: string) => void
}) {
  const [open, setOpen] = useState(false)
  if (excluded.length === 0) return null

  return (
    <div className="bg-gray-900 border border-yellow-500/20 rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-5 py-3 hover:bg-gray-800/40 transition-colors"
      >
        <div className="flex items-center gap-2">
          <span className="text-yellow-400">⚠️</span>
          <span className="text-yellow-300 font-medium text-sm">
            공시 위험으로 제외된 {excluded.length}종목 — 영향 분석 보기
          </span>
          <span className="text-gray-500 text-xs hidden sm:block">— AI 추천에서 제외됨</span>
        </div>
        <svg className={`w-4 h-4 text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="border-t border-gray-800">
          {excluded.map(ex => {
            const code = normalizeSymbol(ex.symbol)
            const name = names[code] ?? '—'
            return (
              <ExcludedStockRow
                key={ex.symbol}
                ex={ex} name={name} code={code}
                onClickSymbol={onClickSymbol}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── 거래량 이상 급등 스캐너 ───────────────────────────────────

function VolumeAnomalySection({
  onClickSymbol, watchlist, onStarChange,
}: {
  onClickSymbol: (sym: string, name: string) => void
  watchlist: string[]
  onStarChange?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<VolumeAnomalyResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const fetchedRef = useRef(false)

  useEffect(() => {
    if (!open || fetchedRef.current) return
    fetchedRef.current = true
    setLoading(true)
    aiApi.volumeAnomaly().then(d => { setData(d); setLoading(false) }).catch(() => setLoading(false))
  }, [open])

  return (
    <div className="bg-gray-900 border border-gray-700/50 rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-5 py-3 hover:bg-gray-800/40 transition-colors"
      >
        <div className="flex items-center gap-2">
          <span className="text-orange-400 text-sm">📡</span>
          <span className="text-gray-200 font-medium text-sm">거래량 이상 급등 스캐너</span>
          <span className="text-gray-500 text-xs hidden sm:block">— 거래량 폭등 + 가격 소폭 (축적 신호 탐지)</span>
          {data && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-400 border border-orange-500/30">
              {data.count}종목
            </span>
          )}
        </div>
        <svg className={`w-4 h-4 text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="border-t border-gray-800">
          {/* 설명 */}
          <div className="px-5 py-3 bg-orange-500/5 border-b border-gray-800 text-xs text-gray-400 space-y-1">
            <p>거래량이 20일 평균의 <span className="text-orange-400 font-medium">5배 이상</span> 급등했지만 당일 가격 변동은 <span className="text-orange-400 font-medium">5% 이하</span>인 종목.</p>
            <p className="text-gray-500">현대오토에버 패턴: 스마트머니 축적 가능성 → 주시 후 조정 시 진입 전략에 활용.</p>
          </div>

          {loading ? (
            <div className="flex items-center justify-center py-10 text-gray-500 text-sm">스캔 중...</div>
          ) : data && data.stocks.length === 0 ? (
            <div className="flex items-center justify-center py-10 text-gray-600 text-sm">오늘은 해당 종목 없음</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-gray-800">
                    <th className="text-left px-4 py-2 min-w-[9rem]">종목</th>
                    <th className="text-right px-3 py-2">
                      <BadgeTooltip text="오늘 거래량 ÷ 최근 20일 평균 거래량. 5× = 평소의 5배. 숫자가 클수록 강한 이상 신호." placement="bottom">
                        <span className="cursor-help border-b border-dashed border-gray-600">거래량(20일비)</span>
                      </BadgeTooltip>
                    </th>
                    <th className="text-right px-3 py-2">
                      <BadgeTooltip text="최근 5일 평균 거래량 ÷ 그 이전 20일 평균. 최근 며칠간 거래량이 지속적으로 높은지 확인." placement="bottom">
                        <span className="cursor-help border-b border-dashed border-gray-600">5일비</span>
                      </BadgeTooltip>
                    </th>
                    <th className="text-right px-3 py-2">
                      <BadgeTooltip text="어제(전일) 거래량 ÷ 20일 평균. 2× 이상이면 어제도 거래량이 높아 '최신↑' 신호로 표시됨." placement="bottom">
                        <span className="cursor-help border-b border-dashed border-gray-600">전일비</span>
                      </BadgeTooltip>
                    </th>
                    <th className="text-right px-3 py-2">
                      <BadgeTooltip text="오늘 하루 주가 등락률(%). 5% 이하인 종목만 표시 — 거래량이 많은데 가격이 안 올랐다는 게 핵심." placement="bottom">
                        <span className="cursor-help border-b border-dashed border-gray-600">당일등락</span>
                      </BadgeTooltip>
                    </th>
                    <th className="text-right px-3 py-2">
                      <BadgeTooltip text="최근 5거래일 누적 수익률. 이미 많이 올랐으면(25% 초과) 이 목록에서 제외됨." placement="bottom">
                        <span className="cursor-help border-b border-dashed border-gray-600">5일등락</span>
                      </BadgeTooltip>
                    </th>
                    <th className="text-right px-3 py-2">
                      <BadgeTooltip text="최근 20거래일 누적 수익률. 한 달 기준 주가 흐름 확인용." placement="bottom">
                        <span className="cursor-help border-b border-dashed border-gray-600">20일등락</span>
                      </BadgeTooltip>
                    </th>
                    <th className="text-center px-3 py-2">
                      <BadgeTooltip text="'최신↑': 어제도 거래량이 2배 이상 — 이상 신호가 지금도 진행 중. '누적': 과거 20일 안에 급등했고 현재는 평소 수준으로 돌아온 상태." placement="bottom">
                        <span className="cursor-help border-b border-dashed border-gray-600">신호</span>
                      </BadgeTooltip>
                    </th>
                    <th className="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {data?.stocks.map(s => {
                    const code = s.symbol
                    const starred = watchlist.some(w => normalizeSymbol(w) === code)
                    const volColor = (s.vol_ratio_20d ?? 0) >= 20 ? 'text-red-400'
                      : (s.vol_ratio_20d ?? 0) >= 10 ? 'text-orange-400' : 'text-yellow-400'
                    const retColor = (v: number | null) =>
                      v == null ? 'text-gray-500'
                        : v > 0.02 ? 'text-red-400' : v < -0.02 ? 'text-blue-400' : 'text-gray-400'
                    return (
                      <tr key={code}
                        className="border-b border-gray-800/60 hover:bg-gray-800/40 cursor-pointer transition-colors"
                        onClick={() => onClickSymbol(code, s.name)}
                      >
                        <td className="px-4 py-2.5">
                          <div className="font-medium text-white">{s.name}</div>
                          <div className="text-gray-500 font-mono">{code} <span className="text-gray-600">{s.market}</span></div>
                        </td>
                        <td className={`text-right px-3 py-2.5 font-bold ${volColor}`}>
                          {s.vol_ratio_20d != null ? `${s.vol_ratio_20d}x` : '-'}
                        </td>
                        <td className={`text-right px-3 py-2.5 ${(s.vol_ratio_5d ?? 0) >= 3 ? 'text-orange-400' : 'text-gray-400'}`}>
                          {s.vol_ratio_5d != null ? `${s.vol_ratio_5d}x` : '-'}
                        </td>
                        <td className={`text-right px-3 py-2.5 ${(s.vol_ratio_lag_1 ?? 0) >= 2 ? 'text-orange-400' : 'text-gray-500'}`}>
                          {s.vol_ratio_lag_1 != null ? `${s.vol_ratio_lag_1}x` : '-'}
                        </td>
                        <td className={`text-right px-3 py-2.5 ${retColor(s.ret_1d)}`}>
                          {s.ret_1d != null ? `${(s.ret_1d * 100).toFixed(1)}%` : '-'}
                        </td>
                        <td className={`text-right px-3 py-2.5 ${retColor(s.ret_5d)}`}>
                          {s.ret_5d != null ? `${(s.ret_5d * 100).toFixed(1)}%` : '-'}
                        </td>
                        <td className={`text-right px-3 py-2.5 ${retColor(s.ret_20d)}`}>
                          {s.ret_20d != null ? `${(s.ret_20d * 100).toFixed(1)}%` : '-'}
                        </td>
                        <td className="text-center px-3 py-2.5">
                          {s.signal === 'fresh' ? (
                            <BadgeTooltip text="어제도 거래량이 평소의 2배 이상 — 이상 신호가 지금 진행 중. 가장 신선한 패턴으로, 조정 시 진입 타이밍을 주시할 필요가 있음." placement="top">
                              <span className="px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-400 border border-orange-500/30 font-medium cursor-help">
                                최신↑
                              </span>
                            </BadgeTooltip>
                          ) : (
                            <BadgeTooltip text="최근 20일 안에 거래량 급등이 있었지만 지금은 평소 수준으로 돌아온 상태. 급등 후 누군가 조용히 매집했을 가능성 — 이후 가격 반응을 관찰 중." placement="top">
                              <span className="px-1.5 py-0.5 rounded bg-gray-700/60 text-gray-500 text-xs cursor-help">
                                누적
                              </span>
                            </BadgeTooltip>
                          )}
                        </td>
                        <td className="px-3 py-2.5" onClick={e => e.stopPropagation()}>
                          <button
                            onClick={() => { toggleWatchlist(code); onStarChange?.() }}
                            className={`text-base transition-colors ${starred ? 'text-yellow-400' : 'text-gray-600 hover:text-gray-400'}`}
                          >★</button>
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
  )
}

// ── 중장기 추천 (60일) ────────────────────────────────────────

function Prediction60dCard({ pred, is5dOverlap, onClickSymbol }: {
  pred: Prediction60d
  is5dOverlap: boolean
  onClickSymbol: (sym: string, name: string) => void
}) {
  const pct = Math.round(pred.probability * 100)

  return (
    <button
      onClick={() => onClickSymbol(pred.symbol, pred.name)}
      className="w-full text-left bg-gray-800/60 border border-gray-700/50 hover:border-teal-500/40 rounded-xl p-4 space-y-3 transition-colors"
    >
      {/* 헤더 */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-gray-500 text-xs font-mono shrink-0">#{pred.rank}</span>
        <p className="text-white font-semibold text-sm flex-1 truncate">{pred.name}</p>
        <span className="text-xs px-1 py-0.5 rounded bg-gray-700/60 text-gray-400 shrink-0">
          {pred.market === 'KOSPI' ? '코스피' : '코스닥'}
        </span>
      </div>

      {is5dOverlap && (
        <span className="inline-flex items-center text-xs px-2 py-0.5 rounded-full bg-teal-500/15 text-teal-400 border border-teal-500/30 font-medium">
          ★ 단기·중장기 모두 상위
        </span>
      )}

      {/* 초과수익 기대 스코어 */}
      <div className="space-y-1">
        <div className="flex items-center justify-between text-xs">
          <span className="text-gray-500">초과수익 기대 점수</span>
          <span className={`font-bold ${pct >= 65 ? 'text-teal-400' : pct >= 50 ? 'text-green-400' : 'text-gray-300'}`}>{pct}%</span>
        </div>
        <div className="h-1.5 bg-gray-700 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${pct >= 65 ? 'bg-teal-500' : pct >= 50 ? 'bg-green-500/80' : 'bg-teal-700/60'}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>

      {/* 팩터 지표 */}
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
        {pred.eps_growth_yoy != null && (
          <div className="flex items-center justify-between">
            <span className="text-gray-500">EPS 성장</span>
            <span className={`font-medium ${pred.eps_growth_yoy > 0 ? 'text-emerald-400' : 'text-red-400/70'}`}>
              {pred.eps_growth_yoy > 0 ? '+' : ''}{(pred.eps_growth_yoy * 100).toFixed(0)}%
            </span>
          </div>
        )}
        {pred.dps_growth_yoy != null && (
          <div className="flex items-center justify-between">
            <span className="text-gray-500">배당 성장</span>
            <span className={`font-medium ${pred.dps_growth_yoy > 0 ? 'text-emerald-400' : 'text-gray-400'}`}>
              {pred.dps_growth_yoy > 0 ? '+' : ''}{(pred.dps_growth_yoy * 100).toFixed(0)}%
            </span>
          </div>
        )}
        {pred.roe_level != null && (
          <div className="flex items-center justify-between">
            <span className="text-gray-500">ROE</span>
            <span className="font-medium text-gray-300">{(pred.roe_level * 100).toFixed(1)}%</span>
          </div>
        )}
        {pred.neg_pbr != null && (
          <div className="flex items-center justify-between">
            <span className="text-gray-500">PBR</span>
            <span className="font-medium text-gray-300">{(-pred.neg_pbr).toFixed(2)}×</span>
          </div>
        )}
      </div>

      <p className="text-xs text-teal-400/50 text-right">~60일 관점</p>
    </button>
  )
}

function Predictions60dSection({
  predictions5d,
  onClickSymbol,
}: {
  predictions5d: PredictionWithMeta[]
  onClickSymbol: (sym: string, name: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<Prediction60d[] | null>(null)
  const [loading60d, setLoading60d] = useState(false)
  const [error60d, setError60d] = useState<string | null>(null)
  const fetchedRef = useRef(false)

  useEffect(() => {
    if (!open || fetchedRef.current) return
    fetchedRef.current = true
    setLoading60d(true)
    aiApi.predictions60d(10)
      .then(d => { setData(d.predictions); setLoading60d(false) })
      .catch(e => { setError60d((e as Error).message); setLoading60d(false) })
  }, [open])

  const symbols5d = useMemo(
    () => new Set(predictions5d.map(p => normalizeSymbol(p.symbol))),
    [predictions5d]
  )
  const overlapCount = useMemo(
    () => data?.filter(p => symbols5d.has(normalizeSymbol(p.symbol))).length ?? 0,
    [data, symbols5d]
  )

  return (
    <div className="bg-gray-900 border border-teal-500/20 rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-5 py-3 hover:bg-gray-800/40 transition-colors"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-teal-400 text-sm">📈</span>
          <span className="text-gray-200 font-medium text-sm">중장기 추천 (60일)</span>
          <span className="text-gray-500 text-xs hidden sm:block">— 60일 시장 대비 초과수익 기대 기준</span>
          {data && (
            <>
              <span className="text-xs px-1.5 py-0.5 rounded bg-teal-500/20 text-teal-400 border border-teal-500/30">
                {data.length}종목
              </span>
              {overlapCount > 0 && (
                <span className="text-xs px-1.5 py-0.5 rounded bg-teal-500/10 text-teal-300/60 border border-teal-500/20">
                  단기+중장기 {overlapCount}
                </span>
              )}
            </>
          )}
        </div>
        <svg className={`w-4 h-4 text-gray-400 transition-transform shrink-0 ${open ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="border-t border-gray-800">
          <div className="px-5 py-3 bg-teal-500/5 border-b border-gray-800 text-xs text-gray-400 space-y-1">
            <p>
              CatBoost 모델이 <strong className="text-teal-300">60일 후 시장 초과수익 경향</strong>을 높게 평가한 종목입니다.
            </p>
            <p className="text-gray-500">
              • 5일 단기 추천과 완전히 독립된 별도 모델 &nbsp;·&nbsp;
              EPS 성장·ROE·배당 성장·저PBR 팩터 기반 &nbsp;·&nbsp;
              예측 경향을 나타내며 수익을 보장하지 않습니다
            </p>
            <p className="text-gray-500/70 text-xs">
              purge gap(60거래일) 적용 재학습 완료 — val top-10 평균 초과수익 K10=+0.195 / K20=+0.170.
              하락장 진입 구간 4건 평균은 시장 대비 -8.1%p로 하락장에서는 시장 열위 경향이 확인됩니다.
            </p>
          </div>

          {loading60d && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3 p-4">
              {Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className="h-44 bg-gray-800/50 rounded-xl animate-pulse" />
              ))}
            </div>
          )}

          {error60d && !loading60d && (
            <div className="px-5 py-4 text-sm text-red-400">
              60일 모델 로드 실패 (단기 추천에는 영향 없음): {error60d}
            </div>
          )}

          {!loading60d && !error60d && data && (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3 p-4">
                {data.map(pred => (
                  <Prediction60dCard
                    key={pred.symbol}
                    pred={pred}
                    is5dOverlap={symbols5d.has(normalizeSymbol(pred.symbol))}
                    onClickSymbol={onClickSymbol}
                  />
                ))}
              </div>
              <p className="text-center text-gray-600 text-xs pb-3">
                중장기 추천은 참고용입니다. 투자 손실에 대한 책임은 투자자 본인에게 있습니다.
              </p>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ── 코스피 시총 상위 10 분석 ──────────────────────────────────

const KOSPI_TOP10 = [
  { symbol: '005930', name: '삼성전자',         rank: 1 },
  { symbol: '000660', name: 'SK하이닉스',       rank: 2 },
  { symbol: '373220', name: 'LG에너지솔루션',   rank: 3 },
  { symbol: '207940', name: '삼성바이오로직스', rank: 4 },
  { symbol: '005380', name: '현대차',           rank: 5 },
  { symbol: '000270', name: '기아',             rank: 6 },
  { symbol: '068270', name: '셀트리온',         rank: 7 },
  { symbol: '105560', name: 'KB금융',           rank: 8 },
  { symbol: '055550', name: '신한지주',         rank: 9 },
  { symbol: '005490', name: 'POSCO홀딩스',      rank: 10 },
]

function KospiStockCard({
  stock, detail, inCmp, compareList, watchlist,
  onClickSymbol, onCompareToggle, onStarChange,
  aiRank, isVolSurge,
}: {
  stock: typeof KOSPI_TOP10[number]
  detail: TickerDetail | null | undefined
  inCmp: boolean
  compareList: string[]
  watchlist: string[]
  onClickSymbol: (sym: string, name: string) => void
  onCompareToggle: (sym: string) => void
  onStarChange?: () => void
  aiRank?: number | null
  isVolSurge?: boolean
}) {
  const code = normalizeSymbol(stock.symbol)
  const d = detail
  const pct = d ? Math.round(displayProb(d) * 100) : null
  const sparkline = d ? (d.price_history ?? []).slice(-20).map(b => b.close) : []
  const [starred, setStarred] = useState(() => watchlist.some(w => normalizeSymbol(w) === code))

  return (
    <div className="relative bg-gray-800/60 border border-gray-700/50 rounded-xl p-4 space-y-3 hover:border-gray-600 transition-colors">
      {/* 우상단 버튼 그룹 */}
      <div className="absolute top-3 right-3 flex items-center gap-1.5">
        <button
          onClick={e => { e.stopPropagation(); onCompareToggle(stock.symbol) }}
          className={`text-xs px-2 py-0.5 rounded border transition-colors ${
            inCmp ? 'bg-indigo-500/30 border-indigo-500/40 text-indigo-300'
                  : compareList.length >= 6 ? 'border-gray-700 text-gray-700 cursor-not-allowed'
                  : 'border-gray-700 text-gray-500 hover:border-gray-500 hover:text-gray-300'
          }`}
          disabled={!inCmp && compareList.length >= 6}
        >{inCmp ? '비교 ✓' : '비교'}</button>
        <button
          onClick={e => {
            e.stopPropagation()
            const next = toggleWatchlist(code)
            setStarred(next)
            onStarChange?.()
          }}
          title="관심 종목"
          className={`text-base transition-colors ${starred ? 'text-yellow-400' : 'text-gray-600 hover:text-gray-400'}`}
        >★</button>
      </div>

      {/* 헤더 */}
      <button onClick={() => onClickSymbol(stock.symbol, stock.name)} className="w-full text-left">
        <div className="flex items-center gap-1.5 pr-20">
          <span className="text-gray-500 text-xs font-mono shrink-0">#{stock.rank}</span>
          <p className="text-white font-semibold text-sm">{stock.name}</p>
          {aiRank != null && (
            <span className="ml-auto shrink-0 text-xs px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 font-medium">
              AI #{aiRank}
            </span>
          )}
          {isVolSurge && aiRank == null && (
            <span className="ml-auto shrink-0 text-xs px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-400 border border-orange-500/30 font-medium">
              거래량↑
            </span>
          )}
        </div>
        <p className="text-gray-500 text-xs mt-0.5">{code}</p>
      </button>

      {d === null ? (
        <p className="text-gray-600 text-xs">데이터 없음</p>
      ) : d === undefined ? (
        <div className="h-16 bg-gray-700/40 rounded animate-pulse" />
      ) : (
        <button onClick={() => onClickSymbol(stock.symbol, stock.name)} className="w-full text-left space-y-2.5">
          {/* 확률 */}
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <SignalStrength prob={displayProb(d)} />
              <span className={`text-sm font-bold ${pct! >= 75 ? 'text-emerald-400' : pct! >= 60 ? 'text-green-400' : 'text-gray-300'}`}>
                {pct}%
              </span>
            </div>
            <div className="h-1.5 bg-gray-700 rounded-full overflow-hidden">
              <div className={`h-full rounded-full ${pct! >= 75 ? 'bg-emerald-500' : pct! >= 60 ? 'bg-green-500/80' : 'bg-emerald-700/60'}`}
                style={{ width: `${pct}%` }} />
            </div>
          </div>

          {/* SHAP 칩 */}
          <div className="flex flex-wrap gap-1">
            {d.shap_full.slice(0, 3).map((s, i) => <ShapChip key={i} entry={s} />)}
          </div>

          {/* 스파크라인 */}
          {sparkline.length > 1 && <Sparkline prices={sparkline} />}

          {/* 전략 — 2줄 컴팩트 */}
          {d.strategy && (() => {
            const st = d.strategy!
            const actionColor = st.action_label === '관망 권장' ? 'text-red-400'
              : st.action_label === '신중 진입' ? 'text-yellow-400' : 'text-emerald-400'
            const riskColor = st.risk_level === '높음' ? 'text-red-400/70'
              : st.risk_level === '중간' ? 'text-yellow-400/70' : 'text-emerald-400/70'
            return (
              <div className="pt-2 border-t border-gray-700/60 space-y-0.5">
                <div className="flex items-center justify-between text-xs">
                  <BadgeTooltip text={ACTION_HINTS[st.action_label] ?? st.action_label}>
                    <span className={`font-semibold cursor-default ${actionColor}`}>{st.action_label}</span>
                  </BadgeTooltip>
                  <BadgeTooltip text={RISK_LEVEL_HINTS[st.risk_level] ?? st.risk_level}>
                    <span className={`cursor-default ${riskColor}`}>{st.risk_level}위험</span>
                  </BadgeTooltip>
                </div>
                <div className="flex items-center gap-2 text-xs text-gray-500">
                  <span>목표 <span className="text-red-400">{st.exit_targets.target1_pct}</span></span>
                  <span>손절 <span className="text-blue-400">{st.exit_targets.stop_loss_pct}</span></span>
                </div>
              </div>
            )
          })()}
        </button>
      )}
    </div>
  )
}

function KospiTop10Section({
  onClickSymbol, onCompareToggle, compareList, watchlist, onStarChange,
  predictions, volSurge,
}: {
  onClickSymbol: (sym: string, name: string) => void
  onCompareToggle: (sym: string) => void
  compareList: string[]
  watchlist: string[]
  onStarChange?: () => void
  predictions: PredictionWithMeta[]
  volSurge: PredictionWithMeta[]
}) {
  const [open, setOpen] = useState(false)
  const [details, setDetails] = useState<Record<string, TickerDetail | null>>({})
  const fetchedRef = useRef(false)

  // 페이지 마운트 시 백그라운드 프리패치 — 카드별로 순차 표시
  useEffect(() => {
    if (fetchedRef.current) return
    fetchedRef.current = true
    KOSPI_TOP10.forEach(s => {
      aiApi.ticker(s.symbol, 30)
        .then(d => setDetails(prev => ({ ...prev, [s.symbol]: d })))
        .catch(() => setDetails(prev => ({ ...prev, [s.symbol]: null })))
    })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="bg-gray-900 border border-gray-700/50 rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-5 py-3 hover:bg-gray-800/40 transition-colors"
      >
        <div className="flex items-center gap-2">
          <span className="text-gray-300 text-sm">🏢</span>
          <span className="text-gray-200 font-medium text-sm">코스피 시총 상위 10 — AI 분석</span>
          <span className="text-gray-500 text-xs hidden sm:block">— 오늘 모델 기준 상승 확률</span>
        </div>
        <svg className={`w-4 h-4 text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="border-t border-gray-800">
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4 p-4">
            {KOSPI_TOP10.map((stock) => {
              const code = normalizeSymbol(stock.symbol)
              const predEntry = predictions.find(p => normalizeSymbol(p.symbol) === code)
              const volEntry = volSurge.find(p => normalizeSymbol(p.symbol) === code)
              const aiRank = predEntry ? (predEntry.rank ?? predictions.indexOf(predEntry) + 1) : null
              const isVolSurge = !!volEntry
              return (
              <KospiStockCard
                key={stock.symbol}
                stock={stock}
                detail={details[stock.symbol]}
                inCmp={compareList.includes(stock.symbol)}
                compareList={compareList}
                watchlist={watchlist}
                onClickSymbol={onClickSymbol}
                onCompareToggle={onCompareToggle}
                onStarChange={onStarChange}
                aiRank={aiRank}
                isVolSurge={isVolSurge}
              />
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Backtest 섹션 ─────────────────────────────────────────────

const METRIC_GUIDE = [
  { term: 'AUC', desc: '모델이 상승/비상승 종목을 얼마나 잘 구별하는지. 0.5 = 동전 던지기, 1.0 = 완벽. 0.7 이상이면 실용적 수준.' },
  { term: 'P@K (라벨 기준)', desc: '상위 K개 추천 종목 중 라벨 기준(5일내 최고가+10% 또는 종가+5%)을 달성한 비율. ★ 5일 보유 후 종가 청산 시 실현율은 이보다 낮음(하단 "실거래 실현율" 참고).' },
  { term: '실거래 실현율', desc: '5일 후 종가 청산 기준 +5% 달성 비율. P@K보다 보수적인 실거래 기준. val 기간이 강세장에 쏠려 있어 실거래보다 높을 수 있음.' },
  { term: '누적 수익률', desc: '수수료·거래세·슬리피지·유동성 필터 반영 후 현실적 가상 수익률.' },
  { term: 'Sharpe Ratio', desc: '위험 1단위당 초과수익. 1 이상 양호, 2 이상 우수.' },
  { term: 'MDD', desc: '고점 대비 최대 낙폭. 작을수록 안정적.' },
  { term: '승률', desc: '추천 포트폴리오가 해당 보유 기간 동안 플러스 수익을 낸 비율.' },
]

function StatCard({ label, value, cls, hint }: { label: string; value: string; cls: string; hint?: string }) {
  return (
    <div className="bg-gray-800/60 rounded-lg px-3 py-2.5 text-center space-y-0.5">
      <p className="text-gray-400 text-xs">{label}</p>
      <p className={`font-bold text-lg ${cls}`}>{value}</p>
      {hint && <p className="text-gray-400 text-xs leading-tight">{hint}</p>}
    </div>
  )
}

function CostsBadge({ costs }: { costs: CostsApplied }) {
  return (
    <div className="bg-gray-800/50 rounded-lg px-4 py-3 space-y-2">
      <p className="text-gray-300 text-xs font-medium">반영된 매매 비용 및 필터</p>
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-x-4 gap-y-2">
        <div><p className="text-gray-500 text-xs">수수료 (양방향)</p><p className="text-gray-200 text-sm font-medium">{costs.commission_pct}% × 2</p></div>
        <div><p className="text-gray-500 text-xs">증권거래세 (매도)</p><p className="text-gray-200 text-sm font-medium">{costs.sell_tax_pct}%</p></div>
        <div><p className="text-gray-500 text-xs">슬리피지 (양방향)</p><p className="text-gray-200 text-sm font-medium">{costs.slippage_pct}% × 2</p></div>
        <div><p className="text-gray-500 text-xs">유동성 기준</p><p className="text-gray-200 text-sm font-medium">일 {costs.min_volume_bn}억 이상</p></div>
        <div><p className="text-gray-500 text-xs">매일 평균 제외</p><p className="text-yellow-400 text-sm font-medium">{Math.round(costs.avg_excluded_per_day)}개</p></div>
      </div>
    </div>
  )
}

function BacktestSection({ perf }: { perf: PerformanceResponse }) {
  const bt = perf.backtest
  const topk = perf.precision_at_topk
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h2 className="text-white font-semibold text-sm">백테스트 성과</h2>
          <p className="text-gray-400 text-xs mt-0.5">
            {bt?.costs_applied ? '수수료·거래세·슬리피지·유동성 필터 반영 현실적 성과' : '검증 기간 기준 · 가상 성과'}
          </p>
        </div>
        <div className="flex items-center gap-3 text-xs flex-wrap">
          {perf.val_auc != null && (
            <div className="text-center">
              <span className="block text-xs text-gray-400">AUC</span>
              <span className="text-white font-bold text-base">{perf.val_auc.toFixed(3)}</span>
              <span className="block text-xs text-gray-400">{perf.val_auc >= 0.75 ? '우수' : perf.val_auc >= 0.65 ? '양호' : '보통'}</span>
            </div>
          )}
          {topk && Object.entries(topk).map(([model, val]) => {
            const pct = typeof val === 'object' && val !== null
              ? (val['10'] ?? Object.values(val)[0]) as number
              : val as number
            if (isNaN(pct)) return null
            return (
              <div key={model} className="text-center">
                <span className="block text-xs text-gray-400">P@{model}</span>
                <span className="text-white font-bold text-base">{(pct * 100).toFixed(1)}%</span>
                <span className="block text-xs text-yellow-400/80">라벨 기준 적중</span>
              </div>
            )
          })}
          {perf.close_hit5_rate_pct != null && (
            <div className="text-center border-l border-gray-700 pl-3">
              <span className="block text-xs text-gray-400">실거래 실현율</span>
              <span className="text-amber-400 font-bold text-base">{perf.close_hit5_rate_pct.toFixed(1)}%</span>
              <span className="block text-xs text-gray-500">5d종가+5% 기준</span>
            </div>
          )}
        </div>
      </div>
      {perf.label_basis && (
        <p className="text-xs text-yellow-400/60 bg-yellow-400/5 rounded px-3 py-1.5">
          ⚠ P@K 라벨 기준: {perf.label_basis} — 5일 보유 후 종가 청산 시 실현율(오른쪽)과 차이가 날 수 있음
        </p>
      )}
      <p className="text-xs text-orange-400/70 bg-orange-400/5 rounded px-3 py-1.5 leading-relaxed">
        ⚠ <strong>레짐 편향 주의</strong> — val 252일이 강세장(2025-26)에만 쏠려 있습니다.
        갭필터 실전수익: 강세장 <span className="text-emerald-400 font-semibold">+0.52%</span> /
        하락장 <span className="text-blue-400 font-semibold">-0.22%</span> (개선하나 손실).
        하락장에선 방어 효과만, 수익 아님. 이론 P@K 격차(-2.32%p 갭·-0.23%p 비용)도 강세장 기준.
      </p>

      {bt ? (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <StatCard label="누적 수익률 (비용 후)" value={`${bt.total_return_pct > 0 ? '+' : ''}${bt.total_return_pct.toFixed(1)}%`}
              cls={bt.total_return_pct > 0 ? 'text-red-400' : 'text-blue-400'}
              hint={bt.gross_total_return_pct != null ? `비용 전 ${bt.gross_total_return_pct > 0 ? '+' : ''}${bt.gross_total_return_pct.toFixed(1)}%` : undefined} />
            <StatCard label="Sharpe (비용 후)" value={bt.sharpe_ratio.toFixed(2)}
              cls={bt.sharpe_ratio > 1 ? 'text-emerald-400' : bt.sharpe_ratio > 0 ? 'text-gray-300' : 'text-blue-400'}
              hint={bt.gross_sharpe_ratio != null ? `비용 전 ${bt.gross_sharpe_ratio.toFixed(2)}` : undefined} />
            <StatCard label="최대 낙폭 MDD" value={`${bt.max_drawdown_pct.toFixed(1)}%`} cls="text-blue-400" hint="고점 대비 최대 손실" />
            <StatCard label="승률 (비용 후)" value={`${(bt.win_rate * 100).toFixed(1)}%`}
              cls={bt.win_rate > 0.5 ? 'text-red-400' : 'text-gray-300'}
              hint={bt.gross_win_rate != null ? `비용 전 ${(bt.gross_win_rate * 100).toFixed(1)}%` : undefined} />
          </div>
          {bt.costs_applied && <CostsBadge costs={bt.costs_applied} />}
          <BacktestChart dates={bt.dates} values={bt.cumulative_return_pct} />
        </>
      ) : (
        <p className="text-gray-500 text-sm py-4">백테스트 데이터 없음</p>
      )}

      <details className="group">
        <summary className="cursor-pointer text-xs text-gray-400 hover:text-gray-200 transition-colors select-none list-none flex items-center gap-1">
          <svg className="w-3 h-3 transition-transform group-open:rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
          지표 용어 설명
        </summary>
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2">
          {METRIC_GUIDE.map(({ term, desc }) => (
            <div key={term} className="bg-gray-800/40 rounded-lg px-3 py-2 space-y-0.5">
              <p className="text-gray-200 text-xs font-semibold">{term}</p>
              <p className="text-gray-400 text-xs leading-relaxed">{desc}</p>
            </div>
          ))}
        </div>
      </details>
    </div>
  )
}

// ── 메인 페이지 ───────────────────────────────────────────────

export default function AIRecommendPage({ embedded = false }: { embedded?: boolean } = {}) {
  const navigate = useNavigate()
  const [predictions, setPredictions] = useState<PredictionWithMeta[]>([])
  const [volSurge, setVolSurge] = useState<PredictionWithMeta[]>([])
  const [perf, setPerf] = useState<PerformanceResponse | null>(null)
  const [market, setMarket] = useState<MarketTrend | null>(null)
  const [summary, setSummary] = useState<DailySummary | null>(null)
  const [date, setDate] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [names, setNames] = useState<Record<string, string>>({})

  const [searchQuery, setSearchQuery] = useState('')
  const [searchHits, setSearchHits] = useState<SearchResult[]>([])
  const [searchLoading, setSearchLoading] = useState(false)

  const [showWatchlistOnly, setShowWatchlistOnly] = useState(false)
  const [watchlist, setWatchlist] = useState<string[]>(() => getWatchlist())
  const [watchlistExtras, setWatchlistExtras] = useState<PredictionWithMeta[]>([])
  const [watchlistLoading, setWatchlistLoading] = useState(false)
  const [excluded, setExcluded] = useState<ExcludedStock[]>([])
  const [marketMode, setMarketMode] = useState<string>('aggressive')
  const [marketMessage, setMarketMessage] = useState<string>('')
  const [volatilityRegime, setVolatilityRegime] = useState<VolatilityRegime | null>(null)
  const [reanalysisInfo, setReanalysisInfo] = useState<{ available: boolean; generated_at?: string } | null>(null)
  const [health, setHealth] = useState<HealthResponse | null>(null)

  const [showRerank, setShowRerank] = useState(false)
  const [confDist, setConfDist] = useState<Record<string, number>>({})
  const [riskDist, setRiskDist] = useState<Record<string, number>>({})

  const [compareList, setCompareList] = useState<string[]>([])
  const [compareDetails, setCompareDetails] = useState<Record<string, TickerDetail | null>>({})


  const [modalSymbol, setModalSymbol] = useState<string | null>(null)
  const [modalName, setModalName] = useState('')
  const [detail, setDetail] = useState<TickerDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  // ── 초기 로드 ──
  const loadedRef = useRef(false)
  useEffect(() => {
    if (loadedRef.current) return
    loadedRef.current = true

    let cancelled = false
    const load = async () => {
      setLoading(true); setError(null)
      try {
        // 종목명은 predict_today()가 로컬 stocks 테이블과 조인해서 이미 채워서 내려줌
        // (예전엔 종목당 외부 시세 API(fetchQuote)를 따로 호출해서 30초씩 걸렸음).
        const [todayResp, perfResp, marketResp, summaryResp] = await Promise.all([
          aiApi.predictions(30), aiApi.performance(), aiApi.marketTrend(),
          aiApi.dailySummary().catch(() => null),
        ])
        if (cancelled) return
        setPerf(perfResp); setDate(todayResp.date); setMarket(marketResp)
        setMarketMode(todayResp.market_mode ?? 'aggressive')
        setMarketMessage(todayResp.market_message ?? '')
        setVolatilityRegime(todayResp.volatility_regime ?? null)
        setConfDist(todayResp.confidence_distribution ?? {})
        setRiskDist(todayResp.risk_distribution ?? {})
        if (summaryResp) setSummary(summaryResp)

        // 모의투자 재분석 리포트 준비 상태 (배너용, 비차단)
        aiApi.reanalysisStatus().then(s => { if (!cancelled) setReanalysisInfo(s) }).catch(() => {})
        // 서버 상태 배너용 (비차단)
        aiApi.health().then(h => { if (!cancelled) setHealth(h) }).catch(() => {})

        const initial: PredictionWithMeta[] = todayResp.predictions.map(p => ({ ...p, sparkline: [] }))
        setPredictions(initial)
        const initialVol: PredictionWithMeta[] = (todayResp.vol_surge ?? []).map(p => ({ ...p, sparkline: [] }))
        setVolSurge(initialVol)
        setExcluded(todayResp.excluded ?? [])

        // names 맵도 응답에 포함된 이름으로 즉시 채움 (검색·비교·요약 등에서 재사용)
        setNames(prev => {
          const next = { ...prev }
          for (const p of [...todayResp.predictions, ...(todayResp.vol_surge ?? [])]) {
            next[p.symbol] = p.name
            next[normalizeSymbol(p.symbol)] = p.name
          }
          return next
        })

        // 스파크라인만 비동기 병렬 로드 (이름은 이미 응답에 포함돼 있어 별도 조회 불필요)
        todayResp.predictions.forEach((p, i) => {
          fetchCandlesCached(p.symbol, '1mo').then(candles => {
            if (!cancelled)
              setPredictions(prev => prev.map((x, j) => j === i ? { ...x, sparkline: candles.slice(-30).map(c => c.close) } : x))
          }).catch(() => {})
        })
        ;(todayResp.vol_surge ?? []).forEach((p, i) => {
          fetchCandlesCached(p.symbol, '1mo').then(candles => {
            if (!cancelled)
              setVolSurge(prev => prev.map((x, j) => j === i ? { ...x, sparkline: candles.slice(-30).map(c => c.close) } : x))
          }).catch(() => {})
        })

        // excluded 종목 회사명 비동기 로드 (같은 names 상태에 합산)
        ;(todayResp.excluded ?? []).map(e => e.symbol).forEach(sym => {
          const code = normalizeSymbol(sym)
          fetchQuote(code).then(q => {
            if (!cancelled) setNames(prev => ({ ...prev, [code]: q.shortName }))
          }).catch(() => {})
        })

      } catch (e) {
        if (!cancelled) setError((e as Error).message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [])

  // ── 비교 종목 상세 로드 ──
  const toggleCompare = useCallback(async (sym: string) => {
    const code = normalizeSymbol(sym)
    setCompareList(prev => {
      if (prev.includes(code)) return prev.filter(s => s !== code)
      if (prev.length >= 6) return prev
      return [...prev, code]
    })
    // 이름이 없거나 code와 동일(미로드)이면 다시 fetch → 실패 시 검색 API fallback
    if (!names[code] || names[code] === code || names[code] === sym) {
      fetchQuote(code)
        .then(q => {
          const n = q.shortName && q.shortName !== code && q.shortName !== sym ? q.shortName : null
          if (n) { setNames(prev => ({ ...prev, [code]: n })); return }
          return searchSymbols(code).then(hits => {
            const hit = hits.find(h => normalizeSymbol(h.symbol) === code)
            if (hit?.shortname) setNames(prev => ({ ...prev, [code]: hit.shortname }))
          })
        })
        .catch(() =>
          searchSymbols(code).then(hits => {
            const hit = hits.find(h => normalizeSymbol(h.symbol) === code)
            if (hit?.shortname) setNames(prev => ({ ...prev, [code]: hit.shortname }))
          }).catch(() => {})
        )
    }
    if (!compareDetails[code]) {
      try {
        const d = await aiApi.ticker(code)
        setCompareDetails(prev => ({ ...prev, [code]: d }))
      } catch {
        setCompareDetails(prev => ({ ...prev, [code]: null }))
      }
    }
  }, [compareDetails, names])

  // ── 검색 ──
  // AI 추천 풀(일반 + 거래량급등 하위 그리드)을 하나의 관심종목 목록으로 통합.
  // predictions/volSurge는 각각 내부적으로는 확률 내림차순이지만 두 배열을 단순
  // 이어붙이면 전체 순서가 깨지므로(예: #1,#3,#5... 다음에 #2,#4...) 합친 뒤
  // 확률 기준으로 재정렬 — 아침 요약 top3(원본 캐시 기준 확률 내림차순)와 동일한 순서 유지.
  const filteredPredictions = useMemo(() => {
    const aiPool = [...predictions, ...volSurge].sort((a, b) => b.probability - a.probability)
    let list: PredictionWithMeta[]
    if (showWatchlistOnly) {
      const inPreds = aiPool.filter(p => watchlist.some(w => normalizeSymbol(w) === normalizeSymbol(p.symbol)))
      const inPredSymbols = new Set(inPreds.map(p => normalizeSymbol(p.symbol)))
      const extras = watchlistExtras.filter(p => !inPredSymbols.has(normalizeSymbol(p.symbol)))
      list = [...inPreds, ...extras]
    } else {
      list = aiPool
    }
    if (searchQuery.trim()) {
      list = list.filter(p =>
        p.name.includes(searchQuery) || normalizeSymbol(p.symbol).includes(searchQuery)
      )
    }
    return list
  }, [predictions, volSurge, watchlist, watchlistExtras, showWatchlistOnly, searchQuery])

  useEffect(() => {
    const q = searchQuery.trim()
    if (!q || filteredPredictions.length > 0) { setSearchHits([]); return }
    const timer = setTimeout(async () => {
      setSearchLoading(true)
      try { setSearchHits((await searchSymbols(q)).slice(0, 6)) }
      catch { setSearchHits([]) }
      finally { setSearchLoading(false) }
    }, 350)
    return () => clearTimeout(timer)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery, filteredPredictions.length])

  // ── 모달 ──
  const openModalForSymbol = async (symbol: string, name: string) => {
    setModalSymbol(symbol); setModalName(name)
    setDetail(null); setDetailError(null); setDetailLoading(true)
    try { setDetail(await aiApi.ticker(symbol, 250)) }
    catch (e) { setDetailError((e as Error).message) }
    finally { setDetailLoading(false) }
  }
  const openModal = (pred: PredictionWithMeta) => openModalForSymbol(pred.symbol, pred.name)
  const closeModal = () => { setModalSymbol(null); setDetail(null); setDetailError(null) }

  const refreshWatchlist = () => setWatchlist(getWatchlist())

  const handleWatchlistToggle = useCallback(async () => {
    const turningOn = !showWatchlistOnly
    setShowWatchlistOnly(turningOn)
    const wl = getWatchlist()
    setWatchlist(wl)

    if (!turningOn) { setWatchlistExtras([]); return }

    // 관심종목 목록(AI 추천 풀)에 없는 종목만 별도 API 호출
    const predSymbols = new Set(
      [...predictions, ...volSurge].map(p => normalizeSymbol(p.symbol))
    )
    const missing = wl.filter(s => !predSymbols.has(normalizeSymbol(s)))
    if (missing.length === 0) return

    setWatchlistLoading(true)
    const settled = await Promise.allSettled(missing.map(s => aiApi.ticker(s)))
    const extras: PredictionWithMeta[] = []
    settled.forEach((r, i) => {
      if (r.status !== 'fulfilled') return
      const d = r.value
      extras.push({
        rank: d.rank ?? 9999,
        symbol: missing[i],
        probability: d.probability,
        probability_calibrated: d.probability_calibrated,
        shap_top: d.shap_full.slice(0, 6),
        name: missing[i],
        sparkline: d.price_history.slice(-30).map(p => p.close),
      })
    })
    setWatchlistExtras(extras)
    setWatchlistLoading(false)

    extras.forEach(p => {
      fetchQuote(p.symbol).then(q => {
        setWatchlistExtras(prev => prev.map(x => x.symbol === p.symbol ? { ...x, name: q.shortName } : x))
      }).catch(() => {})
    })
  }, [showWatchlistOnly, predictions, volSurge])

  // ── 렌더 ──
  return (
    <div className={embedded ? '' : 'min-h-screen bg-slate-950'}>
      {/* 상단 내비 — embedded 시 StockExplorePage가 대신 렌더 */}
      {!embedded && (
      <div className="sticky top-0 z-30 bg-slate-950/90 backdrop-blur border-b border-gray-800/60">
        <div className="max-w-7xl mx-auto px-4 h-12 flex items-center gap-3">
          <button onClick={() => navigate('/')} className="text-gray-400 hover:text-white transition-colors p-1 -ml-1">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </button>
          <h1 className="text-white font-bold text-sm">AI 추천 종목</h1>
          <div className="h-4 w-px bg-gray-700" />
          <span className="text-emerald-400 text-xs font-medium">CatBoost+XGBoost · SHAP</span>
          {date && (<><div className="h-4 w-px bg-gray-700" /><span className="text-gray-400 text-xs">기준 {fmtDate(date)}</span></>)}
          {perf?.val_auc != null && (<><div className="h-4 w-px bg-gray-700" /><span className="text-gray-400 text-xs">AUC <span className="text-white">{perf.val_auc.toFixed(3)}</span></span></>)}
          {perf?.trained_at && <span className="text-gray-400 text-xs hidden sm:block">학습일 {perf.trained_at}</span>}
          <div className="ml-auto flex items-center gap-2">
            <Link
              to="/paper-trading"
              className="text-purple-400 hover:text-purple-300 text-xs transition-colors hidden sm:block"
            >
              모의투자 성과 →
            </Link>
            <Link
              to="/help"
              className="text-gray-500 hover:text-gray-300 text-xs transition-colors hidden sm:block"
            >
              사용 안내
            </Link>
            <RetrainButton onDone={() => aiApi.performance().then(setPerf).catch(() => {})} />
          </div>
        </div>
      </div>
      )}

      <div className="max-w-7xl mx-auto px-4 py-6 space-y-5">
        {/* 에러 */}
        {error && (
          <div className="bg-gray-900 border border-red-500/30 rounded-xl p-5 space-y-2">
            <p className="text-red-400 font-medium text-sm">AI 서버에 연결할 수 없습니다</p>
            <p className="text-gray-400 text-xs">{error}</p>
            <div className="bg-gray-800/60 rounded-lg p-3 font-mono text-xs text-gray-300 space-y-1">
              <p className="text-gray-500"># FastAPI 서버 실행</p>
              <p>cd backend/server</p>
              <p>uvicorn main:app --reload --port 8000</p>
            </div>
          </div>
        )}

        {/* 로딩 스켈레톤 */}
        {loading && !error && (
          <div className="space-y-5">
            <div className="h-12 bg-gray-900 border border-gray-800 rounded-xl animate-pulse" />
            <div className="h-44 bg-gray-900 border border-gray-800 rounded-xl animate-pulse" />
            <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className="h-52 bg-gray-900 border border-gray-800 rounded-xl animate-pulse" />
              ))}
            </div>
          </div>
        )}

        {!loading && !error && (
          <>
            {/* 서버 상태 배너 — embedded 시 StockExplorePage가 대신 렌더 */}
            {!embedded && health != null && <StatusBanner mock={health} />}

            {/* 시장 배너 */}
            {market && <MarketBanner market={market} />}

            {/* 일일 요약 */}
            {summary && <DailySummaryCard summary={summary} names={names} onClickSymbol={openModalForSymbol} volSurgeSymbols={new Set(volSurge.map(v => v.symbol))} />}

            {/* 백테스트 */}
            {perf && <BacktestSection perf={perf} />}

            {/* 공시 위험 제외 종목 */}
            <ExcludedSection excluded={excluded} names={names} onClickSymbol={openModalForSymbol} />

            {/* 공시 재순위 */}
            {excluded.length > 0 && (() => {
              const eligible = excluded.filter(ex => ex.analysis?.rerank_eligible && ex.rank != null)
              if (eligible.length === 0) return null

              const merged = [
                ...predictions.map(p => ({ type: 'safe' as const, rank: p.rank, pred: p, ex: null as ExcludedStock | null })),
                ...eligible.map(ex => ({ type: 'risky' as const, rank: ex.rank!, pred: null as PredictionWithMeta | null, ex })),
              ].sort((a, b) => a.rank - b.rank)

              return (
                <div className="bg-gray-900 border border-gray-700/50 rounded-xl overflow-hidden">
                  <button
                    onClick={() => setShowRerank(o => !o)}
                    className="w-full flex items-center justify-between px-5 py-3 hover:bg-gray-800/40 transition-colors"
                  >
                    <div className="flex items-center gap-2">
                      <span className="text-gray-400 text-sm">⚡</span>
                      <span className="text-gray-200 font-medium text-sm">
                        공시 포함 재순위 — {eligible.length}종목 주의 등급 편입
                      </span>
                      <span className="text-gray-500 text-xs hidden sm:block">— 위험 감수 시 참고용</span>
                    </div>
                    <svg className={`w-4 h-4 text-gray-400 transition-transform ${showRerank ? 'rotate-180' : ''}`}
                      fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </button>

                  {showRerank && (
                    <div className="border-t border-gray-800">
                      <p className="px-5 py-2 text-xs text-gray-500">
                        공시 필터를 제외하고 AI 확률 순위만 기준으로 재배열한 결과입니다. 매수 전 반드시 공시 원문을 확인하세요.
                      </p>
                      <div className="divide-y divide-gray-800/40">
                      {merged.map(item => {
                        if (item.type === 'safe' && item.pred) {
                          const p = item.pred
                          const pct = Math.round(displayProb(p) * 100)
                          const { label, stars, cls } = signalInfo(displayProb(p))
                          const safeCode = normalizeSymbol(p.symbol)
                          const safeInCmp = compareList.includes(safeCode)
                          return (
                            <div
                              key={p.symbol}
                              onClick={() => openModalForSymbol(p.symbol, p.name)}
                              className="px-5 py-3 hover:bg-gray-800/30 transition-colors cursor-pointer space-y-2"
                            >
                              {/* 1행: 순위 + 이름 + 정상 배지 + 확률 + 비교 */}
                              <div className="flex items-center gap-2">
                                <span className="text-gray-500 text-xs font-mono shrink-0">#{item.rank}</span>
                                <span className="text-white text-sm font-semibold truncate flex-1">{p.name}</span>
                                <span className="text-gray-500 text-xs shrink-0">{safeCode}</span>
                                <span className="text-xs px-1.5 py-0.5 rounded font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 shrink-0">✓ 정상 추천</span>
                                <span className={`text-sm font-bold shrink-0 ${pct >= 75 ? 'text-emerald-400' : pct >= 60 ? 'text-green-400' : 'text-gray-300'}`}>{pct}%</span>
                                <button
                                  onClick={e => { e.stopPropagation(); toggleCompare(safeCode) }}
                                  disabled={!safeInCmp && compareList.length >= 6}
                                  className={`shrink-0 text-xs px-2 py-0.5 rounded border transition-colors ${
                                    safeInCmp ? 'bg-indigo-500/30 border-indigo-500/40 text-indigo-300'
                                    : compareList.length >= 6 ? 'border-gray-700 text-gray-700 cursor-not-allowed'
                                    : 'border-gray-700 text-gray-500 hover:border-gray-500 hover:text-gray-300'
                                  }`}
                                >{safeInCmp ? '비교 ✓' : '비교'}</button>
                              </div>
                              {/* 2행: 신호강도 + 확률 바 */}
                              <div className="flex items-center gap-3">
                                <span className={`text-xs font-medium shrink-0 ${cls}`}>{label} {'★'.repeat(stars)}{'☆'.repeat(5 - stars)}</span>
                                <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
                                  <div className={`h-full rounded-full ${pct >= 75 ? 'bg-emerald-500' : pct >= 60 ? 'bg-green-500/80' : 'bg-emerald-700/60'}`}
                                    style={{ width: `${pct}%` }} />
                                </div>
                              </div>
                              {/* 3행: SHAP 칩 + 전략 */}
                              <div className="flex items-center gap-2 flex-wrap">
                                {p.shap_top.slice(0, 3).map((s, i) => <ShapChip key={i} entry={s} />)}
                                {p.strategy && (
                                  <div className="ml-auto shrink-0">
                                    <BadgeTooltip text={ACTION_HINTS[p.strategy.action_label] ?? p.strategy.action_label}>
                                      <span className={`text-xs font-semibold cursor-default ${
                                        p.strategy.action_label === '관망 권장' ? 'text-red-400' :
                                        p.strategy.action_label === '신중 진입' ? 'text-yellow-400' : 'text-emerald-400'
                                      }`}>{p.strategy.action_label}</span>
                                    </BadgeTooltip>
                                  </div>
                                )}
                              </div>
                            </div>
                          )
                        }
                        const ex = item.ex!
                        const code = normalizeSymbol(ex.symbol)
                        const exName = names[code] ?? '—'
                        const a = ex.analysis!
                        const sty = SEVERITY_STYLES[a.severity] ?? SEVERITY_STYLES.high
                        const riskyInCmp = compareList.includes(code)
                        return (
                          <div
                            key={ex.symbol}
                            onClick={() => openModalForSymbol(code, exName)}
                            className="px-5 py-3 hover:bg-gray-800/30 transition-colors cursor-pointer space-y-2"
                          >
                            {/* 1행: 순위 + 이름 + 심각도 배지 + 비교 */}
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-gray-500 text-xs font-mono shrink-0">#{item.rank}</span>
                              <span className="text-white text-sm font-semibold truncate flex-1">{exName}</span>
                              <span className="text-gray-500 text-xs shrink-0">{code}</span>
                              <span className={`text-xs px-1.5 py-0.5 rounded font-semibold shrink-0 ${sty.badge}`}>
                                {a.severity_label}
                              </span>
                              <button
                                onClick={e => { e.stopPropagation(); toggleCompare(code) }}
                                disabled={!riskyInCmp && compareList.length >= 6}
                                className={`shrink-0 text-xs px-2 py-0.5 rounded border transition-colors ${
                                  riskyInCmp ? 'bg-indigo-500/30 border-indigo-500/40 text-indigo-300'
                                  : compareList.length >= 6 ? 'border-gray-700 text-gray-700 cursor-not-allowed'
                                  : 'border-gray-700 text-gray-500 hover:border-gray-500 hover:text-gray-300'
                                }`}
                              >{riskyInCmp ? '비교 ✓' : '비교'}</button>
                            </div>
                            {/* 2행: 키워드 + 방향/등락 */}
                            <div className="flex items-center gap-2 flex-wrap">
                              {ex.risks.map((r, i) => (
                                <span key={i} className="text-xs bg-orange-500/15 text-orange-300 border border-orange-500/30 px-1.5 py-0.5 rounded font-medium">
                                  {r.matched_keyword}
                                </span>
                              ))}
                              <span className={`text-xs font-medium shrink-0 ${sty.accent}`}>{a.direction}</span>
                              <span className="text-xs text-gray-400 shrink-0">{a.range}</span>
                              <span className="text-xs text-gray-500 shrink-0">· {a.timing}</span>
                            </div>
                            {/* 3행: 분석 요약 */}
                            <p className="text-xs text-gray-400 leading-relaxed line-clamp-2">{a.analysis}</p>
                          </div>
                        )
                      })}
                      </div>
                    </div>
                  )}
                </div>
              )
            })()}


            {/* 거래량 이상 급등 스캐너 */}
            <VolumeAnomalySection
              onClickSymbol={openModalForSymbol}
              watchlist={watchlist}
              onStarChange={refreshWatchlist}
            />

            {/* 중장기 추천 (60일) */}
            <Predictions60dSection
              predictions5d={predictions}
              onClickSymbol={openModalForSymbol}
            />

            {/* 코스피 시총 상위 10 */}
            <KospiTop10Section
              onClickSymbol={openModalForSymbol}
              onCompareToggle={toggleCompare}
              compareList={compareList}
              watchlist={watchlist}
              onStarChange={refreshWatchlist}
              predictions={predictions}
              volSurge={volSurge}
            />

            {/* 추천 그리드 */}
            <div>
              <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                <div>
                  <h2 className="text-white font-semibold">관심 종목 {predictions.length + volSurge.length}종목</h2>
                  <p className="text-gray-400 text-xs mt-0.5">5일 보유 전략 · 클릭하면 상세 분석</p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleWatchlistToggle}
                    className={`text-xs px-3 py-1.5 rounded-lg border transition-colors flex items-center gap-1.5 ${
                      showWatchlistOnly ? 'bg-yellow-500/20 border-yellow-500/40 text-yellow-300' : 'bg-gray-800 border-gray-700 text-gray-400 hover:text-white'
                    }`}
                  >
                    {watchlistLoading
                      ? <><div className="w-3 h-3 border border-yellow-400 border-t-transparent rounded-full animate-spin" />불러오는 중...</>
                      : '★ 관심 종목만'}
                  </button>
                  {compareList.length > 0 && (
                    <button onClick={() => setCompareList([])} className="text-xs bg-indigo-600/30 border border-indigo-500/30 text-indigo-300 px-3 py-1.5 rounded-lg hover:bg-indigo-600/50 transition-colors">
                      비교 ({compareList.length}) 초기화
                    </button>
                  )}
                  <span className="text-emerald-500 text-xs font-medium bg-emerald-500/10 px-2.5 py-1 rounded-full">{date ? fmtDate(date) : ''}</span>
                </div>
              </div>

              {/* 검색 */}
              <div className="relative mb-4">
                <div className="flex items-center bg-gray-900 border border-gray-700 rounded-xl px-4 py-2.5 gap-2 focus-within:border-emerald-500/60 transition-colors">
                  <svg className="w-4 h-4 text-gray-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-4.35-4.35M17 11A6 6 0 1 1 5 11a6 6 0 0 1 12 0z" />
                  </svg>
                  <input type="text" value={searchQuery} onChange={e => setSearchQuery(e.target.value)}
                    placeholder="종목명 또는 코드 검색 (예: 삼성전자, 005930)"
                    className="flex-1 bg-transparent text-sm text-white placeholder:text-gray-500 focus:outline-none" />
                  {searchQuery && (
                    <button onClick={() => { setSearchQuery(''); setSearchHits([]) }} className="text-gray-500 hover:text-white transition-colors">
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  )}
                </div>
                {searchQuery.trim() && filteredPredictions.length === 0 && (
                  <div className="absolute top-full mt-1 w-full bg-gray-900 border border-gray-700 rounded-xl overflow-hidden z-20 shadow-xl">
                    {searchLoading && (
                      <div className="flex items-center gap-2 px-4 py-3 text-gray-400 text-sm">
                        <div className="w-3.5 h-3.5 border border-emerald-500 border-t-transparent rounded-full animate-spin" />검색 중...
                      </div>
                    )}
                    {!searchLoading && searchHits.length === 0 && <p className="text-gray-500 text-sm px-4 py-3">검색 결과 없음</p>}
                    {searchHits.map(hit => {
                      const code = hit.symbol.replace(/\.(KS|KQ)$/, '')
                      const inCmp = compareList.includes(code)
                      return (
                        <div key={hit.symbol} className="flex items-center border-t border-gray-800 first:border-0 hover:bg-gray-800 transition-colors">
                          <button
                            onClick={() => { setSearchQuery(''); setSearchHits([]); openModalForSymbol(code, hit.shortname) }}
                            className="flex-1 flex items-center justify-between px-4 py-2.5 text-left"
                          >
                            <span className="text-white text-sm font-medium">{hit.shortname}</span>
                            <div className="flex items-center gap-2 text-xs text-gray-400">
                              <span>{code}</span>
                              <span className="bg-gray-700 px-1.5 py-0.5 rounded">{hit.exchDisp}</span>
                              <span className="text-emerald-400">AI 분석 →</span>
                            </div>
                          </button>
                          <button
                            onClick={e => {
                              e.stopPropagation()
                              setNames(prev => ({ ...prev, [code]: hit.shortname }))
                              toggleCompare(code)
                            }}
                            className={`shrink-0 text-xs px-3 py-1.5 mr-3 rounded transition-colors ${
                              inCmp
                                ? 'bg-indigo-500/30 text-indigo-300'
                                : compareList.length >= 6
                                ? 'bg-gray-700 text-gray-500 cursor-not-allowed'
                                : 'bg-gray-700 text-gray-300 hover:bg-indigo-500/20 hover:text-indigo-300'
                            }`}
                            disabled={!inCmp && compareList.length >= 6}
                          >
                            {inCmp ? '비교 ✓' : '비교'}
                          </button>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>

              {/* 모의투자 재분석 리포트 배너 (1회성) */}
              <ReanalysisBanner info={reanalysisInfo} />

              {/* 시장 국면 정보 배너 (순수 표시 — 추천 결과 미변경) */}
              <MarketModeBanner mode={marketMode} message={marketMessage} />

              {/* 저변동성 신뢰도 경고 배너 (크기 — 약세장 가드와 독립적, 동시 표시 가능) */}
              <VolatilityRegimeBanner regime={volatilityRegime} />

              {/* 신뢰도·위험도 요약 */}
              {(confDist.HIGH || confDist.MEDIUM || riskDist.HIGH || riskDist.MEDIUM) ? (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-400 px-1">
                  <span>오늘 <span className="text-white font-medium">{predictions.length}</span>종목 추천</span>
                  {health?.prices_latest_date && (() => {
                    const d = health.prices_latest_date!
                    const label = `${d.slice(4,6)}/${d.slice(6,8)} 기준 예측`
                    return health.prices_stale
                      ? <span className="text-yellow-400 font-medium">{label}</span>
                      : <span className="text-gray-500">{label}</span>
                  })()}
                  {(confDist.HIGH || confDist.MEDIUM) ? (
                    <span className="flex items-center gap-2">
                      <span>신뢰도</span>
                      {confDist.HIGH > 0 && <span className="text-emerald-400 font-medium">HIGH {confDist.HIGH}</span>}
                      {confDist.MEDIUM > 0 && <span className="text-yellow-400 font-medium">MED {confDist.MEDIUM}</span>}
                    </span>
                  ) : null}
                  {(riskDist.HIGH > 0 || riskDist.MEDIUM > 0) ? (
                    <span className="flex items-center gap-2">
                      <span>위험</span>
                      {riskDist.HIGH > 0 && <span className="text-red-400 font-medium">HIGH {riskDist.HIGH}</span>}
                      {riskDist.MEDIUM > 0 && <span className="text-orange-400 font-medium">MED {riskDist.MEDIUM}</span>}
                    </span>
                  ) : null}
                </div>
              ) : null}

              {/* 카드 그리드 — AI 추천 목록 */}
              {showWatchlistOnly && watchlistLoading ? (
                <div className="text-center py-16 text-gray-500 text-sm">관심 종목 불러오는 중...</div>
              ) : showWatchlistOnly && filteredPredictions.length === 0 ? (
                <div className="text-center py-16 text-gray-500 text-sm">관심 종목이 없습니다. 카드의 ★를 눌러 추가하세요.</div>
              ) : !showWatchlistOnly && predictions.length === 0 && volSurge.length === 0 ? (
                <div className="text-center py-16 text-gray-500 text-sm">예측 데이터가 없습니다.</div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                  {filteredPredictions.map(pred => (
                    <PredictionCard key={pred.symbol} pred={pred}
                      onClick={() => openModal(pred)}
                      onCompareToggle={toggleCompare}
                      inCompare={compareList.includes(pred.symbol)}
                      watchlist={watchlist}
                      onStarChange={refreshWatchlist}
                      lowVolRegime={volatilityRegime?.low_vol_warning}
                    />
                  ))}
                </div>
              )}

              <p className="text-center text-gray-600 text-xs pt-1">
                AI 추천은 참고용입니다. 투자 손실에 대한 책임은 투자자 본인에게 있습니다.
              </p>
            </div>

            {/* 비교 패널 */}
            {compareList.length >= 2 && (
              <ComparePanel
                symbols={compareList}
                names={names}
                details={compareDetails}
                onClose={() => setCompareList([])}
              />
            )}
          </>
        )}
      </div>

      {/* 상세 모달 */}
      {modalSymbol && (
        <DetailModal
          symbol={modalSymbol} name={modalName}
          detail={detail} loading={detailLoading} error={detailError}
          onClose={closeModal}
        />
      )}
    </div>
  )
}
