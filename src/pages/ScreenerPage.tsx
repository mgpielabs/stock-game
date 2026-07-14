import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  screenerApi, aiApi, updateApi, getWatchlist, toggleWatchlist,
  type ScreenerStock, type ScreenerRegime, type ScreenerFilters,
  type TickerDetail, type UpdateStatus,
  type StockSearchResult, type SymbolProfile, type FilterFlags,
} from '../api/aiRecommend'
import { DetailModal, BadgeTooltip } from './AIRecommendPage'

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

// ── 추천 조합 프리셋 — 검증된 필터 2개를 동시에 켬 (combo_validation.py 검증 통과) ──

export interface ComboPreset {
  label: string
  keys: (keyof ScreenerFilters)[]
  tooltip: string
}

export const COMBO_PRESETS: ComboPreset[] = [
  {
    label: '고배당 + 저PER',
    keys: ['high_dividend', 'low_per'],
    tooltip: '36조합 전수 검증(2026-07-14) — 초과승률 71.9%(단독 최고 대비 +9.2%p, p<0.001) + 절대 20d 수익률도 개선(OOS +4.18% vs 단독 최고 +2.05%, p<0.001). IS/OOS 방향 일치. Jaccard <0.7(다른 조합과 중복 없음). 36개 중 Δ 최고.',
  },
  {
    label: '저PBR + 배당성장',
    keys: ['low_pbr', 'div_growth'],
    tooltip: '36조합 전수 검증(2026-07-14) — 초과승률 63.9%(단독 최고 대비 +6.1%p, p<0.001) + 절대 20d 수익률도 개선(OOS +2.71% vs 단독 최고 +2.36%, p<0.001). IS/OOS 방향 일치. n=11,737.',
  },
  {
    label: '고배당 + 배당성장',
    keys: ['high_dividend', 'div_growth'],
    tooltip: '36조합 전수 검증(2026-07-14) — 초과승률 67.5%(단독 최고 대비 +5.2%p, p<0.001) + 절대 20d 수익률도 개선(OOS +3.06% vs 단독 최고 +2.25%, p<0.001). IS/OOS 방향 일치. n=11,577.',
  },
  {
    label: '고배당 + 저PBR',
    keys: ['high_dividend', 'low_pbr'],
    tooltip: '고배당을 DPS÷실제종가로 재계산한 기준으로 최종 재검증(2026-06-23)해도 20일 기준 단독보다 유의하게 나음(OOS p=0.0001). 약세장 승률 71.3%. 2026-07-14 전수 36조합 검증에서도 재확인(OOS 초과승률 68.8% +5.1%p, 절대수익 +3.25% vs +2.37%, p<0.001).',
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

export function StockProfileCard({
  profile,
  onClose,
  onApplyFilters,
  aiRank,
  aiProb,
}: {
  profile: SymbolProfile
  onClose: () => void
  onApplyFilters: (filters: Partial<ScreenerFilters>) => void
  aiRank?: number | null
  aiProb?: number | null
}) {
  const { stock: s, filter_flags: flags } = profile
  const passedFlags = FLAG_FILTER_MAP.filter(f => flags[f.key])

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
        </div>
        <button onClick={onClose} className="text-gray-500 hover:text-gray-300 transition-colors p-1 mt-0.5 shrink-0">
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      {/* 주요 수치 */}
      <div className="grid grid-cols-4 sm:grid-cols-7 gap-2">
        {[
          { label: 'PER', value: s.per_pit != null ? s.per_pit.toFixed(1) : '—' },
          { label: 'PBR', value: s.pbr_pit != null ? s.pbr_pit.toFixed(2) : '—' },
          { label: 'RSI', value: s.rsi_14 != null ? s.rsi_14.toFixed(0) : '—' },
          { label: '배당', value: s.dividend_yield != null ? `${s.dividend_yield.toFixed(1)}%` : '—' },
          { label: '거래량비', value: s.vol_ratio_20d != null ? `${s.vol_ratio_20d.toFixed(1)}x` : '—' },
          { label: '20일수익', value: s.ret_20d != null ? `${(s.ret_20d >= 0 ? '+' : '')}${(s.ret_20d * 100).toFixed(1)}%` : '—' },
          { label: 'ATR%', value: s.atr_pct != null ? `${(s.atr_pct * 100).toFixed(1)}%` : '—' },
        ].map(item => (
          <div key={item.label} className="bg-gray-800/60 rounded-lg px-2 py-2 text-center">
            <p className="text-gray-500 text-xs">{item.label}</p>
            <p className="text-gray-200 text-sm font-medium mt-0.5">{item.value}</p>
          </div>
        ))}
      </div>

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

export function DataUpdateBanner({ initialDate }: { initialDate?: string | null }) {
  const [status, setStatus] = useState<UpdateStatus | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)

  const poll = useCallback(async (): Promise<UpdateStatus | null> => {
    try {
      const s = await updateApi.getStatus()
      setStatus(s)
      return s
    } catch {
      return null
    }
  }, [])

  // 실행 중일 때만 3초마다 polling — idle/done/error면 멈춤(불필요한 요청 방지)
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
  }, [poll])

  const handleClick = async () => {
    setStartError(null)
    setStarting(true)
    try {
      await updateApi.start()
      await poll()
    } catch (e) {
      setStartError(e instanceof Error ? e.message : '시작 실패')
    } finally {
      setStarting(false)
    }
  }

  const running = status?.status === 'running'
  const lastLog = status?.log?.[status.log.length - 1]

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-3 space-y-2">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-gray-400 text-xs">
            데이터 기준: <span className="text-gray-200 font-medium">{fmtUpdateDate(status?.latest_feature_date ?? initialDate ?? null)}</span>
          </span>
          {status?.foreign_rate_warning && (
            <BadgeTooltip text="외국인비율은 업데이트를 안 돌린 날만큼 영구히 공백이 생기는 구조입니다 — 업데이트를 눌러 채우세요. (PER/PBR은 point-in-time 방식으로 자동 보정되어 더 이상 해당 없음)">
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

        <div className="flex items-center gap-2">
          {running && lastLog && (
            <span className="text-gray-500 text-xs truncate max-w-[14rem] hidden sm:inline">{lastLog}</span>
          )}
          {status?.status === 'error' && !running && (
            <span className="text-red-400 text-xs">업데이트 실패 — 서버는 정상, 아래 로그 확인</span>
          )}
          {status?.status === 'done' && !running && (
            <span className="text-emerald-400 text-xs">✓ 완료</span>
          )}
          <button
            onClick={handleClick}
            disabled={running || starting}
            className={`text-xs font-medium px-3 py-1.5 rounded-lg border transition-colors whitespace-nowrap ${
              running || starting
                ? 'border-gray-700 text-gray-500 cursor-not-allowed'
                : 'border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10'
            }`}
          >
            {running ? `업데이트 중... ${status?.elapsed_sec ?? 0}s` : starting ? '시작 중...' : '데이터 업데이트'}
          </button>
        </div>
      </div>
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
      <td className="px-3 py-2.5 text-right text-gray-400 text-xs">{stock.dividend_yield != null ? `${stock.dividend_yield.toFixed(1)}%` : '—'}</td>
      <td className="px-3 py-2.5 text-right text-gray-400 text-xs">{stock.vol_ratio_20d != null ? `${stock.vol_ratio_20d.toFixed(1)}x` : '—'}</td>
      <td className="px-3 py-2.5 text-right text-xs font-medium">{fmtPct(stock.ret_20d)}</td>
    </tr>
  )
}

// ── 메인 페이지 ───────────────────────────────────────────────

export default function ScreenerPage({
  embedded = false,
  externalFilters = null,
  onExternalFiltersApplied,
}: {
  embedded?: boolean
  externalFilters?: Record<string, boolean> | null
  onExternalFiltersApplied?: () => void
} = {}) {
  const [filters, setFilters] = useState<Record<string, boolean>>({})
  const [sortBy, setSortBy] = useState('symbol')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const [regime, setRegime] = useState<ScreenerRegime | null>(null)
  const [stocks, setStocks] = useState<ScreenerStock[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [watchlist, setWatchlist] = useState<string[]>(() => getWatchlist())
  const [screenerDate, setScreenerDate] = useState<string | null>(null)

  // 종목 프로파일 검색 (standalone 전용 — embedded 시 StockExplorePage가 담당)
  const [profile, setProfile] = useState<SymbolProfile | null>(null)
  const [profileLoading, setProfileLoading] = useState(false)

  // 외부에서 필터를 주입받아 적용 (StockExplorePage 프로파일 카드 → "이 조건으로 검색")
  const prevExternalRef = useRef(externalFilters)
  useEffect(() => {
    if (externalFilters && externalFilters !== prevExternalRef.current) {
      prevExternalRef.current = externalFilters
      setFilters(externalFilters)
      setProfile(null)
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
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [filters, sortBy, sortDir])

  const toggleFilter = (key: string) => {
    setFilters(prev => ({ ...prev, [key]: !prev[key] }))
  }

  const applyPreset = (preset: ComboPreset) => {
    setFilters(prev => {
      const next = { ...prev }
      FILTERS.forEach(f => { next[f.key] = false })
      preset.keys.forEach(k => { next[k] = true })
      return next
    })
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

  const handleApplyProfileFilters = (newFilters: Partial<ScreenerFilters>) => {
    setFilters(_prev => {
      const next: Record<string, boolean> = {}
      FILTERS.forEach(f => { next[f.key] = false })
      Object.entries(newFilters).forEach(([k, v]) => { if (v) next[k] = true })
      return next
    })
    setProfile(null)
  }

  const handleToggleStar = (symbol: string) => {
    const next = toggleWatchlist(symbol)
    setWatchlist(getWatchlist())
    void next
  }

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
          />
        )}

        {/* 추천 조합 프리셋 */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-gray-500 text-xs">추천 조합(검증됨):</span>
          {COMBO_PRESETS.map(preset => (
            <BadgeTooltip key={preset.label} text={preset.tooltip}>
              <button
                onClick={() => applyPreset(preset)}
                className="text-xs px-2.5 py-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 transition-colors"
              >
                ✅ {preset.label}
              </button>
            </BadgeTooltip>
          ))}
        </div>

        {/* 필터 패널 */}
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
          <p className="text-gray-400 text-xs font-medium mb-3">조건 선택 (체크한 조건을 모두 만족하는 종목, AND 조합)</p>
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
            <option value="dividend_yield">배당수익률</option>
            <option value="vol_ratio_20d">거래량비율</option>
            <option value="ret_20d">20일수익률</option>
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
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
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
                  <th className="text-right px-3 py-2.5 font-medium">배당</th>
                  <th className="text-right px-3 py-2.5 font-medium">거래량비</th>
                  <th className="text-right px-3 py-2.5 font-medium">20일수익률</th>
                </tr>
              </thead>
              <tbody>
                {!loading && stocks.length === 0 && (
                  <tr><td colSpan={9} className="text-center py-10 text-gray-500 text-sm">조건에 맞는 종목이 없습니다</td></tr>
                )}
                {stocks.map(s => (
                  <StockRow
                    key={s.symbol}
                    stock={s}
                    starred={watchlist.some(w => w === s.symbol)}
                    onToggleStar={() => handleToggleStar(s.symbol)}
                    onClick={() => openDetail(s)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
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
    </div>
  )
}
