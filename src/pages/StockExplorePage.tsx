import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import {
  aiApi, screenerApi, watchlistApi,
  type HealthResponse, type ScreenerFilters, type ScreenerRegime,
  type StockSearchResult, type SymbolProfile, type TickerDetail,
  type WatchlistItem,
} from '../api/aiRecommend'
import StatusBanner from '../components/StatusBanner'
import { DataUpdateBanner, RegimeBanner, StockSearchBox, StockProfileCard, CompareFloatingBar, StockCompareView } from './ScreenerPage'
import AIRecommendPage from './AIRecommendPage'
import ScreenerPage from './ScreenerPage'
import MacroDashboard from './MacroDashboard'

export default function StockExplorePage() {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  const activeTab = location.pathname === '/screener' ? 'screener'
    : location.pathname === '/macro' ? 'macro'
    : 'recommend'

  // 탭별 lazy mount — 첫 방문 시 마운트, 이후 hidden으로 유지 (상태 보존)
  const [aiMounted, setAiMounted] = useState(activeTab === 'recommend')
  const [screenerMounted, setScreenerMounted] = useState(activeTab === 'screener')
  const [macroMounted, setMacroMounted] = useState(activeTab === 'macro')
  useEffect(() => {
    if (activeTab === 'recommend') setAiMounted(true)
    else if (activeTab === 'screener') setScreenerMounted(true)
    else if (activeTab === 'macro') setMacroMounted(true)
  }, [activeTab])

  // 공통 상태
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [regime, setRegime] = useState<ScreenerRegime | null>(null)

  // 공유 종목 프로파일 카드 상태
  const [profile, setProfile] = useState<SymbolProfile | null>(null)
  const [profileAiRank, setProfileAiRank] = useState<number | null>(null)
  const [profileAiProb, setProfileAiProb] = useState<number | null>(null)
  const [profileLoading, setProfileLoading] = useState(false)
  const profileAbortRef = useRef<AbortController | null>(null)
  const profileCardRef = useRef<HTMLDivElement>(null)

  // 스크리너 외부 필터 주입 (프로파일 카드 "이 조건으로 검색" → 스크리너 탭)
  const [screenerExternalFilters, setScreenerExternalFilters] = useState<Record<string, boolean> | null>(null)

  // 종목 비교 상태 (StockExplorePage 레벨에서 관리 — ScreenerPage + 공유 프로파일 카드 모두 공유)
  const [compareList, setCompareList] = useState<string[]>([])
  const [compareNames, setCompareNames] = useState<Record<string, string>>({})
  const [compareProfiles, setCompareProfiles] = useState<Record<string, SymbolProfile>>({})
  const [compareViewOpen, setCompareViewOpen] = useState(false)

  const handleAddToCompare = async (symbol: string) => {
    if (compareList.includes(symbol)) {
      setCompareList(prev => prev.filter(s => s !== symbol))
      return
    }
    if (compareList.length >= 3) return
    setCompareList(prev => [...prev, symbol])
    if (compareProfiles[symbol]) {
      setCompareNames(prev => ({ ...prev, [symbol]: compareProfiles[symbol].stock.name }))
      return
    }
    // 현재 열려 있는 프로파일 카드 재활용
    const existing = profile?.stock.symbol === symbol ? profile : null
    if (existing) {
      setCompareProfiles(prev => ({ ...prev, [symbol]: existing }))
      setCompareNames(prev => ({ ...prev, [symbol]: existing.stock.name }))
      return
    }
    try {
      const p = await screenerApi.symbolProfile(symbol)
      setCompareProfiles(prev => ({ ...prev, [symbol]: p }))
      setCompareNames(prev => ({ ...prev, [symbol]: p.stock.name }))
    } catch { }
  }

  const handleRemoveFromCompare = (symbol: string) => {
    setCompareList(prev => {
      const next = prev.filter(s => s !== symbol)
      if (next.length === 0) setCompareViewOpen(false)
      return next
    })
  }

  const handleClearCompare = () => {
    setCompareList([])
    setCompareNames({})
    setCompareProfiles({})
    setCompareViewOpen(false)
  }

  // 관심 종목 상태 (StockExplorePage 레벨에서 관리)
  const [watchlistItems, setWatchlistItems] = useState<WatchlistItem[]>([])
  const [undoToast, setUndoToast] = useState<{ symbol: string; name: string; timeoutId: ReturnType<typeof setTimeout> } | null>(null)

  const watchlistSymbols = watchlistItems.map(w => w.symbol)

  useEffect(() => {
    watchlistApi.list().then(items => setWatchlistItems(items)).catch(() => {})
  }, [])

  const handleToggleWatchlist = async (symbol: string, name: string) => {
    if (watchlistSymbols.includes(symbol)) {
      // 즉시 UI에서 제거 + undo toast
      setWatchlistItems(prev => prev.filter(w => w.symbol !== symbol))
      const prevItems = watchlistItems
      if (undoToast) {
        clearTimeout(undoToast.timeoutId)
        // 이전 제거 확정
        await watchlistApi.remove(undoToast.symbol).catch(() => {})
      }
      const timeoutId = setTimeout(async () => {
        await watchlistApi.remove(symbol).catch(() => {})
        setUndoToast(null)
      }, 4000)
      setUndoToast({ symbol, name, timeoutId })
      void prevItems
    } else {
      // 추가
      if (undoToast && undoToast.symbol === symbol) {
        // undo: 다시 추가
        clearTimeout(undoToast.timeoutId)
        setUndoToast(null)
        return
      }
      await watchlistApi.add(symbol, name).catch(() => {})
      setWatchlistItems(prev => [{ symbol, name, added_at: new Date().toISOString(), memo: null }, ...prev])
    }
  }

  const handleUndoRemove = () => {
    if (!undoToast) return
    clearTimeout(undoToast.timeoutId)
    setWatchlistItems(prev => [{ symbol: undoToast.symbol, name: undoToast.name, added_at: new Date().toISOString(), memo: null }, ...prev])
    setUndoToast(null)
  }

  // 업데이트 파이프라인 완료 시 refetch 트리거
  const [updateDoneTick, setUpdateDoneTick] = useState(0)
  const handleUpdateComplete = useCallback(() => {
    setUpdateDoneTick(t => t + 1)
  }, [])

  // 최초 마운트: health + regime 로드
  useEffect(() => {
    aiApi.health().then(h => setHealth(h)).catch(() => {})
    screenerApi.search({ limit: 1 })
      .then(res => setRegime(res.market_regime))
      .catch(() => {})
  }, [])

  // ?ticker= URL 파라미터로 프로파일 자동 열기 (새 창으로 열기 기능)
  useEffect(() => {
    const ticker = searchParams.get('ticker')
    if (!ticker) return
    handleStockSelect({ symbol: ticker, name: '', market: '' })
  }, [])

  // 업데이트 완료 시 regime 재조회
  useEffect(() => {
    if (updateDoneTick === 0) return
    screenerApi.search({ limit: 1 }).then(res => setRegime(res.market_regime)).catch(() => {})
  }, [updateDoneTick])

  // 프로파일 카드가 열릴 때 해당 영역으로 스크롤
  // scrollIntoView(block:'start')는 탭 바 + 시장국면 배너에 종목명이 가려지므로
  // getBoundingClientRect()로 절대 위치를 계산한 뒤 96px 여백을 확보해 scrollTo.
  useEffect(() => {
    if ((profile || profileLoading) && profileCardRef.current) {
      const el = profileCardRef.current
      const y = el.getBoundingClientRect().top + window.scrollY - 120
      window.scrollTo({ top: y, behavior: 'smooth' })
    }
  }, [profile, profileLoading])

  const handleStockSelect = async (result: StockSearchResult) => {
    // 이전 요청 취소
    if (profileAbortRef.current) profileAbortRef.current.abort()
    const ac = new AbortController()
    profileAbortRef.current = ac

    setProfile(null)
    setProfileAiRank(null)
    setProfileAiProb(null)
    setProfileLoading(true)

    try {
      const [profileResult, tickerResult] = await Promise.allSettled([
        screenerApi.symbolProfile(result.symbol),
        aiApi.ticker(result.symbol, 7).catch(() => null),
      ])
      if (ac.signal.aborted) return

      if (profileResult.status === 'fulfilled') {
        setProfile(profileResult.value)
      }
      if (tickerResult.status === 'fulfilled' && tickerResult.value) {
        const d = tickerResult.value as TickerDetail
        setProfileAiRank(d.rank ?? null)
        setProfileAiProb(
          (d as unknown as Record<string, number | null>).probability_calibrated
          ?? d.probability
          ?? null
        )
      }
    } catch {
      // 에러 무시 (프로파일 표시 생략)
    } finally {
      if (!ac.signal.aborted) setProfileLoading(false)
    }
  }

  const handleApplyProfileFilters = (newFilters: Partial<ScreenerFilters>) => {
    const next: Record<string, boolean> = {}
    Object.entries(newFilters).forEach(([k, v]) => { if (v) next[k] = true })
    setScreenerExternalFilters(next)
    setProfile(null)
    navigate('/screener')
  }

  return (
    <div className="min-h-screen bg-slate-950">
      {/* 공유 상단 내비 + 탭 */}
      <div className="sticky top-0 z-30 bg-slate-950/90 backdrop-blur border-b border-gray-800/60">
        <div className="max-w-7xl mx-auto px-4 h-12 flex items-center gap-3">
          <Link to="/" className="text-gray-400 hover:text-white transition-colors p-1 -ml-1">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </Link>

          {/* 탭 버튼 */}
          <div className="flex items-center gap-1">
            <Link
              to="/recommend"
              className={`text-xs px-3 py-1 rounded-md font-medium transition-colors ${
                activeTab === 'recommend'
                  ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              대시보드
            </Link>
            <Link
              to="/screener"
              className={`text-xs px-3 py-1 rounded-md font-medium transition-colors ${
                activeTab === 'screener'
                  ? 'bg-sky-500/15 text-sky-400 border border-sky-500/30'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              스크리너
            </Link>
            <Link
              to="/macro"
              className={`text-xs px-3 py-1 rounded-md font-medium transition-colors ${
                activeTab === 'macro'
                  ? 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              매크로
            </Link>
          </div>

          {/* 프로파일 열림 시 위치 표시 */}
          {(profile || profileLoading) && (
            <div className="flex items-center gap-1 text-xs text-gray-500">
              <span>{activeTab === 'screener' ? '스크리너' : '대시보드'}</span>
              <span>›</span>
              <span className="text-gray-300 font-medium truncate max-w-[120px]">
                {profile?.stock.name ?? '조회 중...'}
              </span>
            </div>
          )}

          <div className="ml-auto flex items-center gap-3">
            <Link to="/paper-trading" className="text-purple-400 hover:text-purple-300 text-xs transition-colors hidden sm:block">
              모의투자 →
            </Link>
            <Link to="/help" className="text-gray-500 hover:text-gray-300 text-xs transition-colors hidden sm:block">
              도움말
            </Link>
          </div>
        </div>
      </div>

      {/* 공유 콘텐츠 영역: 서버상태, 데이터기준일, 시장국면, 검색 */}
      <div className="max-w-7xl mx-auto px-4 pt-4 pb-2 space-y-3">
        {health != null && <StatusBanner mock={health} />}
        <DataUpdateBanner onComplete={handleUpdateComplete} />
        <RegimeBanner regime={regime} />

        {/* 종목 검색 */}
        <StockSearchBox onSelect={handleStockSelect} />

        {/* 프로파일 카드 */}
        <div ref={profileCardRef} />
        {profileLoading && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl px-4 py-6 text-center text-gray-500 text-sm">
            종목 정보 조회 중...
          </div>
        )}
        {profile && !profileLoading && (
          <StockProfileCard
            profile={profile}
            aiRank={profileAiRank}
            aiProb={profileAiProb}
            onClose={() => setProfile(null)}
            onApplyFilters={handleApplyProfileFilters}
            onAddToCompare={handleAddToCompare}
            inCompareList={compareList.includes(profile.stock.symbol)}
            onToggleWatchlist={handleToggleWatchlist}
            inWatchlist={watchlistSymbols.includes(profile.stock.symbol)}
          />
        )}
      </div>

      {/* 탭 콘텐츠 — lazy mount + hidden으로 상태 보존 */}
      {aiMounted && (
        <div className={activeTab !== 'recommend' ? 'hidden' : ''}>
          <AIRecommendPage
            embedded
            onPresetSelect={(keys) => {
              const next: Record<string, boolean> = {}
              keys.forEach(k => { next[k] = true })
              setScreenerExternalFilters(next)
              navigate('/screener')
            }}
            watchlistItems={watchlistItems}
            onToggleWatchlist={handleToggleWatchlist}
            onWatchlistStockSelect={(sym) => handleStockSelect({ symbol: sym, name: '', market: '' })}
          />
        </div>
      )}
      {screenerMounted && (
        <div className={activeTab !== 'screener' ? 'hidden' : ''}>
          <ScreenerPage
            embedded
            externalFilters={screenerExternalFilters}
            onExternalFiltersApplied={() => setScreenerExternalFilters(null)}
            refetchTick={updateDoneTick}
            onStockSelect={sym => handleStockSelect({ symbol: sym, name: '', market: '' })}
            onAddToCompare={handleAddToCompare}
            compareList={compareList}
            watchlist={watchlistSymbols}
            onToggleWatchlist={handleToggleWatchlist}
          />
        </div>
      )}
      {macroMounted && (
        <div className={activeTab !== 'macro' ? 'hidden' : ''}>
          <MacroDashboard />
        </div>
      )}
      {/* 비교 플로팅 바 */}
      {!compareViewOpen && compareList.length >= 1 && (
        <CompareFloatingBar
          symbols={compareList}
          names={compareNames}
          onOpen={() => setCompareViewOpen(true)}
          onRemove={handleRemoveFromCompare}
          onClear={handleClearCompare}
        />
      )}

      {/* 비교 뷰 */}
      {compareViewOpen && (
        <StockCompareView
          symbols={compareList}
          profiles={compareProfiles}
          onClose={() => setCompareViewOpen(false)}
          onRemove={handleRemoveFromCompare}
          onApplyFilters={handleApplyProfileFilters}
          onAddToCompare={handleAddToCompare}
          onToggleWatchlist={handleToggleWatchlist}
          watchlist={watchlistSymbols}
        />
      )}

      {/* Undo toast — 관심 종목 제거 후 되돌리기 */}
      {undoToast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 bg-gray-800 border border-gray-600 rounded-xl px-4 py-3 shadow-xl text-sm">
          <span className="text-gray-300">
            <span className="text-white font-medium">{undoToast.name}</span> 관심 종목에서 제거됨
          </span>
          <button
            onClick={handleUndoRemove}
            className="text-yellow-400 hover:text-yellow-300 font-semibold transition-colors"
          >
            되돌리기
          </button>
        </div>
      )}
    </div>
  )
}
