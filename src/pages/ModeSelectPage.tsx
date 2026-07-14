import { Link } from 'react-router-dom'
import { useHistoryStore } from '../store/historyStore'
import { formatKRW, formatChangePercent, changeColor } from '../utils/format'
import { INITIAL_CASH } from '../api/constants'
import { useEffect, useState } from 'react'

function useAiServerStatus() {
  const [online, setOnline] = useState<boolean | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    fetch(`${import.meta.env.VITE_API_BASE ?? ''}/health`, { signal: controller.signal })
      .then((r) => setOnline(r.ok))
      .catch((e) => { if (e.name !== 'AbortError') setOnline(false) })
    return () => controller.abort()
  }, [])
  return online
}

export default function ModeSelectPage() {
  const history = useHistoryStore()
  const aiOnline = useAiServerStatus()

  const historyProfitRate = ((history.cash + Object.values(history.holdings).reduce(
    (s, h) => s + h.avgPrice * h.quantity, 0
  ) - INITIAL_CASH) / INITIAL_CASH) * 100

  const historyDateMs = Object.values(history.stockPositions).length > 0
    ? Math.max(...Object.values(history.stockPositions))
    : history.startDate
  const historyDateStr = new Date(historyDateMs).toLocaleDateString('ko-KR', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })

  const historyTradeCount = history.tradeHistory.length
  const today = Date.now()
  const historyEnded = historyDateMs >= today

  return (
    <div className="min-h-screen bg-slate-950 flex flex-col items-center justify-center px-4">
      <div className="w-full max-w-2xl space-y-8">
        <div className="text-center space-y-2">
          <h1 className="text-white text-3xl font-bold tracking-tight">주식 모의투자</h1>
          <p className="text-gray-400 text-sm">플레이할 모드를 선택하세요</p>
        </div>

        <div className="grid grid-cols-1 gap-4">
          {/* 역사 시뮬레이션 카드 */}
          <Link
            to="/history"
            className="group block bg-gray-900 border border-gray-800 hover:border-amber-500/50 rounded-2xl p-6 transition-all hover:shadow-lg hover:shadow-amber-500/10"
          >
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-amber-500/20 flex items-center justify-center text-xl">
                ⏱️
              </div>
              <div>
                <h2 className="text-white font-bold text-lg">역사 시뮬레이션</h2>
                <p className="text-gray-400 text-xs">6개월 전부터 현재까지</p>
              </div>
            </div>

            <p className="text-gray-400 text-sm mb-5 leading-relaxed">
              6개월 전 과거로 돌아가 매수·매도·홀드를 결정하며 하루씩 진행합니다. 당신의 선택이 맞았는지 확인해보세요.
            </p>

            {historyTradeCount > 0 ? (
              <div className="bg-gray-800/60 rounded-xl px-4 py-3 space-y-1">
                <p className="text-gray-400 text-xs">
                  {historyEnded ? '시뮬레이션 종료' : `${historyDateStr} 진행 중`}
                </p>
                <div className="flex items-center justify-between">
                  <span className="text-gray-300 text-sm">{historyTradeCount}회 거래</span>
                  <span className={`text-sm font-semibold ${changeColor(historyProfitRate)}`}>
                    {formatChangePercent(historyProfitRate)}
                  </span>
                </div>
                <p className="text-gray-400 text-xs">이어하기 →</p>
              </div>
            ) : (
              <div className="bg-gray-800/60 rounded-xl px-4 py-3 space-y-1">
                <p className="text-gray-400 text-xs">시작 전 · 초기 자본 {formatKRW(INITIAL_CASH)}</p>
                <p className="text-amber-500/70 text-xs">{historyDateStr}부터 시작</p>
              </div>
            )}
          </Link>
        </div>

        {/* AI 추천 카드 */}
        <Link
          to="/recommend"
          className="group block bg-gray-900 border border-gray-800 hover:border-emerald-500/50 rounded-2xl p-6 transition-all hover:shadow-lg hover:shadow-emerald-500/10"
        >
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-emerald-500/20 flex items-center justify-center text-xl">
              🤖
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <h2 className="text-white font-bold text-lg">AI 추천 종목</h2>
                <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${
                  aiOnline === null
                    ? 'bg-gray-700 text-gray-400'
                    : aiOnline
                    ? 'bg-emerald-500/20 text-emerald-400'
                    : 'bg-gray-700/60 text-gray-500'
                }`}>
                  {aiOnline === null ? '확인 중' : aiOnline ? '서버 ON' : '서버 OFF'}
                </span>
              </div>
              <p className="text-gray-400 text-xs">CatBoost+XGBoost · SHAP 분석</p>
            </div>
          </div>
          <p className="text-gray-400 text-sm mb-5 leading-relaxed">
            머신러닝 모델이 내일 +3% 이상 상승 가능성이 높은 종목을 예측합니다.
            SHAP 기여도 분석으로 예측 근거를 확인하고, 백테스트 성과를 검증하세요.
          </p>
          <div className={`rounded-xl px-4 py-3 ${aiOnline ? 'bg-emerald-500/10 border border-emerald-500/20' : 'bg-gray-800/60'}`}>
            {aiOnline
              ? <p className="text-emerald-400 text-xs">서버 연결됨 — 지금 바로 예측 결과를 확인하세요 →</p>
              : <p className="text-gray-500 text-xs">
                  서버 오프라인 · <code className="text-gray-400">uvicorn main:app --port 8000</code> 실행 후 접속
                </p>
            }
          </div>
        </Link>

        {/* 종목 스크리너 카드 */}
        <Link
          to="/screener"
          className="group block bg-gray-900 border border-gray-800 hover:border-sky-500/50 rounded-2xl p-6 transition-all hover:shadow-lg hover:shadow-sky-500/10"
        >
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-sky-500/20 flex items-center justify-center text-xl">
              🔍
            </div>
            <div>
              <h2 className="text-white font-bold text-lg">종목 스크리너</h2>
              <p className="text-gray-400 text-xs">검증된 조건으로 직접 검색</p>
            </div>
          </div>
          <p className="text-gray-400 text-sm mb-5 leading-relaxed">
            워크포워드로 검증된 조건(고배당, RSI 과매도, 볼린저 하단)으로 종목을 검색하고
            최종 선택은 직접 합니다. 어떤 조건이 검증됐는지/안 됐는지 배지로 명확히 표시됩니다.
          </p>
          <div className="bg-sky-500/10 border border-sky-500/20 rounded-xl px-4 py-3">
            <p className="text-sky-400 text-xs">조건 검색하러 가기 →</p>
          </div>
        </Link>

        {/* AI 모의투자 추적 카드 */}
        <Link
          to="/paper-trading"
          className="group block bg-gray-900 border border-gray-800 hover:border-purple-500/50 rounded-2xl p-6 transition-all hover:shadow-lg hover:shadow-purple-500/10"
        >
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-purple-500/20 flex items-center justify-center text-xl">
              📊
            </div>
            <div>
              <h2 className="text-white font-bold text-lg">AI 모의투자 추적</h2>
              <p className="text-gray-400 text-xs">Paper Trading · 5거래일 자동 청산</p>
            </div>
          </div>
          <p className="text-gray-400 text-sm mb-5 leading-relaxed">
            AI가 추천한 종목의 실제 성과를 5거래일 단위로 추적합니다.
            승률, 누적 수익률, 베스트/워스트 거래를 한눈에 확인하세요.
          </p>
          <div className="bg-purple-500/10 border border-purple-500/20 rounded-xl px-4 py-3">
            <p className="text-purple-400 text-xs">성과 추적 확인 →</p>
          </div>
        </Link>

        <div className="flex items-center justify-between">
          <p className="text-gray-400 text-xs">
            각 모드의 자산과 거래 내역은 독립적으로 저장됩니다
          </p>
          <Link to="/help" className="text-gray-500 hover:text-gray-300 text-xs transition-colors">
            사용 안내 →
          </Link>
        </div>
      </div>
    </div>
  )
}
