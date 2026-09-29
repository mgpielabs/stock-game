import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { screenerApi, type SymbolProfile, type StockChartPoint } from '../api/aiRecommend'
import { StockInvestorChart, getTrendInfo, getSupplyInfo } from './ScreenerPage'

export default function StockChartPage() {
  const [searchParams] = useSearchParams()
  const symbol = searchParams.get('ticker') ?? ''

  const [profile, setProfile] = useState<SymbolProfile | null>(null)
  const [profileLoading, setProfileLoading] = useState(true)
  const [chartData, setChartData] = useState<StockChartPoint[] | null>(null)

  useEffect(() => {
    if (!symbol) return
    setProfileLoading(true)
    screenerApi.symbolProfile(symbol)
      .then(p => setProfile(p))
      .catch(() => {})
      .finally(() => setProfileLoading(false))
  }, [symbol])

  if (!symbol) {
    return (
      <div className="h-screen bg-gray-950 flex items-center justify-center text-gray-500 text-sm">
        ticker 파라미터가 없습니다 (?ticker=005930)
      </div>
    )
  }

  const s = profile?.stock
  const trendInfo  = chartData ? getTrendInfo(chartData)  : null
  const supplyInfo = chartData ? getSupplyInfo(chartData) : null

  return (
    <div className="h-screen overflow-hidden bg-gray-950 flex flex-col">

      {/* ── 헤더 (컴팩트) ── */}
      <div className="shrink-0 bg-gray-900 border-b border-gray-800 px-4 py-2 flex items-center gap-3">
        <span className="font-mono text-sky-400 font-bold text-sm">{symbol}</span>
        {profileLoading ? (
          <span className="text-gray-500 text-xs">조회 중…</span>
        ) : s ? (
          <>
            <span className="text-white font-semibold text-sm truncate">{s.name}</span>
            <span className="text-[11px] px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 border border-gray-700">
              {s.market}
            </span>
            {s.sector_name && (
              <span className="text-[11px] text-gray-500 hidden sm:inline truncate">· {s.sector_name}</span>
            )}
            <span className="ml-auto text-lg font-bold text-white tabular-nums">
              {s.close.toLocaleString()}원
            </span>
          </>
        ) : (
          <span className="text-gray-500 text-xs">프로파일 없음</span>
        )}
      </div>

      {/* ── 지표 행 (컴팩트) ── */}
      {s && (
        <div className="shrink-0 bg-gray-900/50 border-b border-gray-800/50 px-4 py-1.5 flex items-center gap-5 text-xs overflow-x-auto">
          {[
            { label: 'PER',     value: s.per_pit       != null ? s.per_pit.toFixed(1)        : '—' },
            { label: 'PBR',     value: s.pbr_pit       != null ? s.pbr_pit.toFixed(2)        : '—' },
            { label: 'RSI',     value: s.rsi_14        != null ? s.rsi_14.toFixed(0)         : '—' },
            { label: '거래량비',  value: s.vol_ratio_20d != null ? `${s.vol_ratio_20d.toFixed(1)}x` : '—' },
            { label: '배당수익률', value: s.dividend_yield != null ? `${s.dividend_yield.toFixed(1)}%` : '—' },
          ].map(({ label, value }) => (
            <div key={label} className="flex items-center gap-1.5 shrink-0">
              <span className="text-gray-500">{label}</span>
              <span className="text-gray-200 font-medium">{value}</span>
            </div>
          ))}

          {/* 추세·수급 요약을 같은 행 오른쪽에 */}
          {(trendInfo || supplyInfo) && (
            <div className="ml-auto flex items-center gap-4 shrink-0">
              {trendInfo && (
                <span className={`font-medium ${trendInfo.color}`}>
                  {trendInfo.label}
                  <span className="text-gray-500 font-normal ml-1 hidden sm:inline">({trendInfo.detail})</span>
                </span>
              )}
              {supplyInfo && (
                <span className={supplyInfo.foreignColor}>{supplyInfo.line}</span>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── 차트 영역 — 남은 공간 전부 ── */}
      <div className="flex-1 min-h-0 p-2">
        <div className="h-full bg-gray-900 rounded-xl border border-gray-800 flex flex-col p-2">
          <StockInvestorChart
            key={symbol}
            symbol={symbol}
            name={s?.name}
            onDataLoaded={setChartData}
            flex
            investHeight={240}
          />
        </div>
      </div>

    </div>
  )
}
