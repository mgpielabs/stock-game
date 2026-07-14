import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { useEffect, useState } from 'react'
import HistoryHeader from './components/HistoryHeader'
import ModeSelectPage from './pages/ModeSelectPage'
import HistoryHomePage from './pages/HistoryHomePage'
import HistoryStockPage from './pages/HistoryStockPage'
import HistoryPortfolioPage from './pages/HistoryPortfolioPage'
import HistoryResultsPage from './pages/HistoryResultsPage'
import PaperTradingPage from './pages/PaperTradingPage'
import HelpPage from './pages/HelpPage'
import StockExplorePage from './pages/StockExplorePage'
import { useHistoryStore } from './store/historyStore'
import { fetchCandlesCached, getPriceAt } from './api/yahooFinance'
import { getSyntheticCandles, isSynthetic } from './api/syntheticStocks'

// 역사 시뮬레이션 레이아웃
function HistoryLayout() {
  const { cash, holdings, stockPositions, startDate } = useHistoryStore()
  const [stockValue, setStockValue] = useState(0)

  useEffect(() => {
    const symbols = Object.keys(holdings)
    if (symbols.length === 0) {
      setStockValue(0)
      return
    }
    let cancelled = false
    Promise.all(
      symbols.map(async (sym) => {
        const date = stockPositions[sym] ?? startDate
        if (isSynthetic(sym)) {
          const candles = getSyntheticCandles(sym)
          return getPriceAt(candles, date) ?? 0
        }
        const candles = await fetchCandlesCached(sym, '1y')
        return getPriceAt(candles, date) ?? 0
      })
    )
      .then((prices) => {
        if (cancelled) return
        const val = symbols.reduce((sum, sym, i) => {
          const h = holdings[sym]
          return sum + (h ? prices[i] * h.quantity : 0)
        }, 0)
        setStockValue(val)
      })
      .catch(() => {})
    return () => { cancelled = true }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [holdings, JSON.stringify(stockPositions)])

  const totalAsset = cash + stockValue

  return (
    <div className="min-h-screen bg-slate-950">
      <HistoryHeader totalAsset={totalAsset} />
      <Routes>
        <Route index element={<HistoryHomePage />} />
        <Route path="stock/:symbol" element={<HistoryStockPage />} />
        <Route path="portfolio" element={<HistoryPortfolioPage />} />
        <Route path="results/:symbol" element={<HistoryResultsPage />} />
      </Routes>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<ModeSelectPage />} />
        <Route path="/history/*" element={<HistoryLayout />} />
        <Route path="/recommend" element={<StockExplorePage />} />
        <Route path="/screener" element={<StockExplorePage />} />
        <Route path="/ai-recommend" element={<StockExplorePage />} />
        <Route path="/paper-trading" element={<PaperTradingPage />} />
        <Route path="/help" element={<HelpPage />} />
      </Routes>
    </BrowserRouter>
  )
}
