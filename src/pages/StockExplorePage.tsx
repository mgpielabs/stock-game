import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import {
  aiApi, screenerApi,
  type HealthResponse, type ScreenerFilters, type ScreenerRegime,
  type StockSearchResult, type SymbolProfile, type TickerDetail,
} from '../api/aiRecommend'
import StatusBanner from '../components/StatusBanner'
import { DataUpdateBanner, RegimeBanner, StockSearchBox, StockProfileCard } from './ScreenerPage'
import AIRecommendPage from './AIRecommendPage'
import ScreenerPage from './ScreenerPage'

export default function StockExplorePage() {
  const location = useLocation()
  const navigate = useNavigate()

  const activeTab = location.pathname === '/screener' ? 'screener' : 'recommend'

  // 탭별 lazy mount — 첫 방문 시 마운트, 이후 hidden으로 유지 (상태 보존)
  const [aiMounted, setAiMounted] = useState(activeTab === 'recommend')
  const [screenerMounted, setScreenerMounted] = useState(activeTab === 'screener')
  useEffect(() => {
    if (activeTab === 'recommend') setAiMounted(true)
    else setScreenerMounted(true)
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

  // 스크리너 외부 필터 주입 (프로파일 카드 "이 조건으로 검색" → 스크리너 탭)
  const [screenerExternalFilters, setScreenerExternalFilters] = useState<Record<string, boolean> | null>(null)

  // 최초 마운트: health + regime 로드
  useEffect(() => {
    aiApi.health().then(h => setHealth(h)).catch(() => {})
    screenerApi.search({ limit: 1 })
      .then(res => setRegime(res.market_regime))
      .catch(() => {})
  }, [])

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
          <h1 className="text-white font-bold text-sm">종목 탐색</h1>
          <div className="h-4 w-px bg-gray-700" />

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
              AI 추천
            </Link>
            <Link
              to="/screener"
              className={`text-xs px-3 py-1 rounded-md font-medium transition-colors ${
                activeTab === 'screener'
                  ? 'bg-sky-500/15 text-sky-400 border border-sky-500/30'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              필터 검색
            </Link>
          </div>

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
        <DataUpdateBanner />
        <RegimeBanner regime={regime} />

        {/* 종목 검색 */}
        <StockSearchBox onSelect={handleStockSelect} />

        {/* 프로파일 카드 */}
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
          />
        )}
      </div>

      {/* 탭 콘텐츠 — lazy mount + hidden으로 상태 보존 */}
      {aiMounted && (
        <div className={activeTab !== 'recommend' ? 'hidden' : ''}>
          <AIRecommendPage embedded />
        </div>
      )}
      {screenerMounted && (
        <div className={activeTab !== 'screener' ? 'hidden' : ''}>
          <ScreenerPage
            embedded
            externalFilters={screenerExternalFilters}
            onExternalFiltersApplied={() => setScreenerExternalFilters(null)}
          />
        </div>
      )}
    </div>
  )
}
