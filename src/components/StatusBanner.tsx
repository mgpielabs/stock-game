import { useEffect, useState } from 'react'
import { aiApi, type HealthResponse } from '../api/aiRecommend'

interface Props {
  /** 테스트용 mock — 제공되면 /health 호출 스킵 */
  mock?: HealthResponse | 'error'
}

export default function StatusBanner({ mock }: Props) {
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [fetchError, setFetchError] = useState(false)

  useEffect(() => {
    if (mock !== undefined) {
      if (mock === 'error') setFetchError(true)
      else setHealth(mock)
      return
    }
    aiApi.health()
      .then(h => { setHealth(h); setFetchError(false) })
      .catch(() => setFetchError(true))
  }, [mock])

  if (fetchError) {
    return (
      <div className="flex items-center gap-2 px-4 py-2 rounded-lg bg-gray-700/60 text-gray-300 text-sm mb-3">
        <span>⚠</span>
        <span>서버 상태 확인 불가 — /health 응답 없음</span>
      </div>
    )
  }

  if (!health) return null

  const banners: React.ReactNode[] = []

  if (health.prices_stale) {
    const dateStr = health.prices_latest_date
      ? `${health.prices_latest_date.slice(4, 6)}/${health.prices_latest_date.slice(6, 8)}`
      : '?'
    banners.push(
      <div key="stale" className="flex items-center gap-2 px-4 py-2 rounded-lg bg-yellow-900/60 border border-yellow-600/50 text-yellow-300 text-sm">
        <span>⚠</span>
        <span>
          데이터 지연 {health.prices_stale_trading_days}거래일
          {' '}(기준일 {dateStr}) — 데이터_수동업데이트.bat 실행 필요
        </span>
      </div>
    )
  }

  if (health.retrain_due) {
    const parts = Object.entries(health.model_ages_days ?? {})
      .filter(([, days]) => days >= 90)
      .map(([model, days]) => `${model} ${days}일`)
      .join(' / ')
    banners.push(
      <div key="retrain" className="flex items-center gap-2 px-4 py-2 rounded-lg bg-orange-900/60 border border-orange-600/50 text-orange-300 text-sm">
        <span>🔄</span>
        <span>모델 재학습 기한 도래 ({parts || '90일+'})</span>
      </div>
    )
  }

  if (banners.length === 0) return null

  return <div className="flex flex-col gap-2 mb-3">{banners}</div>
}
