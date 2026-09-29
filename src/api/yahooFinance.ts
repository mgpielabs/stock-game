// 캔들 데이터: 백엔드 DB (GET {CANDLES_BASE}/api/candles/{code}?days=N)
// 검색/재무:   네이버 증권 API (Vite 프록시 경유)

const CANDLES_BASE = import.meta.env.VITE_API_BASE ?? ''

function toCode(symbol: string): string {
  return symbol.split('.')[0]
}

export interface QuoteResult {
  symbol: string
  shortName: string
  regularMarketPrice: number
  regularMarketChange: number
  regularMarketChangePercent: number
  regularMarketPreviousClose: number
  regularMarketOpen: number
  regularMarketDayHigh: number
  regularMarketDayLow: number
  regularMarketVolume: number
  currency: string
}

export interface CandleBar {
  time: number // Unix seconds
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface SearchResult {
  symbol: string
  shortname: string
  exchDisp: string
  typeDisp: string
}

function parseNum(val: string | number | undefined): number {
  if (val == null) return 0
  if (typeof val === 'number') return val
  return Number(String(val).replace(/,/g, '')) || 0
}

function findInfo(totalInfos: { code: string; value: string }[], code: string): number {
  const item = totalInfos?.find((i) => i.code === code)
  return item ? parseNum(item.value) : 0
}

// 현재가·등락 전용 (watchlist, 빠른 조회)
async function fetchBasic(symbol: string): Promise<Omit<QuoteResult, 'regularMarketOpen' | 'regularMarketDayHigh' | 'regularMarketDayLow' | 'regularMarketVolume'>> {
  const code = toCode(symbol)
  const res = await fetch(`/api/naver-stock/api/stock/${code}/basic`)
  if (!res.ok) throw new Error(`basic ${res.status}`)
  const d = await res.json()
  const price = parseNum(d.closePrice ?? d.stockEndPrice)
  const change = parseNum(d.compareToPreviousClosePrice)
  const pct = parseNum(d.fluctuationsRatio) * (change < 0 ? -1 : 1)
  return {
    symbol,
    shortName: d.stockName ?? symbol,
    regularMarketPrice: price,
    regularMarketChange: change,
    regularMarketChangePercent: pct,
    regularMarketPreviousClose: price - change,
    currency: 'KRW',
  }
}

// 시가·고가·저가·거래량 추가 (상세 페이지용)
async function fetchIntegration(symbol: string): Promise<{ open: number; high: number; low: number; volume: number }> {
  const code = toCode(symbol)
  const res = await fetch(`/api/naver-stock/api/stock/${code}/integration`)
  if (!res.ok) throw new Error(`integration ${res.status}`)
  const d = await res.json()
  const ti: { code: string; value: string }[] = d.totalInfos ?? []
  return {
    open: findInfo(ti, 'openPrice'),
    high: findInfo(ti, 'highPrice'),
    low: findInfo(ti, 'lowPrice'),
    volume: findInfo(ti, 'accumulatedTradingVolume'),
  }
}

export async function fetchQuote(symbol: string): Promise<QuoteResult> {
  const basic = await fetchBasic(symbol)
  return { ...basic, regularMarketOpen: 0, regularMarketDayHigh: 0, regularMarketDayLow: 0, regularMarketVolume: 0 }
}

export async function fetchQuoteDetail(symbol: string): Promise<QuoteResult> {
  const [basic, ohlcv] = await Promise.all([fetchBasic(symbol), fetchIntegration(symbol)])
  return {
    ...basic,
    regularMarketOpen: ohlcv.open,
    regularMarketDayHigh: ohlcv.high,
    regularMarketDayLow: ohlcv.low,
    regularMarketVolume: ohlcv.volume,
  }
}

export async function fetchQuotes(symbols: string[]): Promise<QuoteResult[]> {
  const results = await Promise.allSettled(symbols.map(fetchQuote))
  return results
    .filter((r): r is PromiseFulfilledResult<QuoteResult> => r.status === 'fulfilled')
    .map((r) => r.value)
}

const RANGE_DAYS: Record<string, number> = {
  '1mo': 30,
  '3mo': 90,
  '6mo': 180,
  '1y': 365,
  '2y': 730,
  '3y': 1095,
  '5y': 1825,
}

export async function fetchCandles(
  symbol: string,
  range: '1mo' | '3mo' | '6mo' | '1y' | '2y' | '3y' | '5y' = '3mo',
): Promise<CandleBar[]> {
  const code = toCode(symbol)
  const days = RANGE_DAYS[range] ?? 90
  const res = await fetch(`${CANDLES_BASE}/api/candles/${code}?days=${days}`)
  if (!res.ok) throw new Error(`candles ${res.status}`)
  return res.json() as Promise<CandleBar[]>
}

// 캔들 배열에서 dateMs(Unix ms) 이전 마지막 캔들의 close 반환 (주말/휴장일 처리 포함)
export function getPriceAt(candles: CandleBar[], dateMs: number): number | null {
  const dateSec = Math.floor(dateMs / 1000)
  let result: number | null = null
  for (const candle of candles) {
    if (candle.time <= dateSec) result = candle.close
    else break
  }
  return result
}

// 캔들 배열에서 dateMs 이전 마지막 캔들 반환
export function getCandleAt(candles: CandleBar[], dateMs: number): CandleBar | null {
  const dateSec = Math.floor(dateMs / 1000)
  let result: CandleBar | null = null
  for (const candle of candles) {
    if (candle.time <= dateSec) result = candle
    else break
  }
  return result
}

// 간단한 모듈 레벨 캔들 캐시 (역사 시뮬레이션에서 중복 요청 방지)
const _candleCache = new Map<string, CandleBar[]>()

export async function fetchCandlesCached(
  symbol: string,
  range: '1mo' | '3mo' | '6mo' | '1y' | '2y' | '3y' | '5y' = '1y',
): Promise<CandleBar[]> {
  const key = `${symbol}-${range}`
  if (_candleCache.has(key)) return _candleCache.get(key)!
  const candles = await fetchCandles(symbol, range)
  _candleCache.set(key, candles)
  return candles
}

export interface FinanceSummary {
  profit: number  // 영업이익 (억원), 음수면 적자
  year: string    // e.g. "2024"
}

const _financeCache = new Map<string, FinanceSummary | null>()

export async function fetchFinanceSummary(symbol: string): Promise<FinanceSummary | null> {
  const code = toCode(symbol)
  if (_financeCache.has(code)) return _financeCache.get(code)!
  try {
    const res = await fetch(`/api/naver-stock/api/stock/${code}/finance/summary`)
    if (!res.ok) { _financeCache.set(code, null); return null }
    const d = await res.json()
    const annual = d?.chartIncomeStatement?.annual
    if (!annual) { _financeCache.set(code, null); return null }
    const titleList: { isConsensus: string; title: string }[] = annual.trTitleList ?? []
    const columns: string[][] = annual.columns ?? []
    const actuals = titleList.map((t, i) => ({ ...t, i })).filter((t) => t.isConsensus === 'N')
    if (actuals.length === 0) { _financeCache.set(code, null); return null }
    const last = actuals[actuals.length - 1]
    const profitRow = columns.find((r) => r[0] === '영업이익')
    if (!profitRow) { _financeCache.set(code, null); return null }
    const profit = Number(profitRow[last.i + 1]) || 0
    const year = last.title.slice(0, 4)
    const result: FinanceSummary = { profit, year }
    _financeCache.set(code, result)
    return result
  } catch {
    _financeCache.set(code, null)
    return null
  }
}

export async function searchSymbols(query: string): Promise<SearchResult[]> {
  const res = await fetch(`/api/naver-search/ac?q=${encodeURIComponent(query)}&target=stock,index&lang=ko`)
  if (!res.ok) throw new Error(`search ${res.status}`)
  const data = await res.json()

  // 실제 응답: { items: [{ code, name, typeCode: "KOSPI"/"KOSDAQ", ... }] }
  const items: { code: string; name: string; typeCode: string }[] = data?.items ?? []
  return items
    .filter((item) => item.typeCode === 'KOSPI' || item.typeCode === 'KOSDAQ')
    .map((item) => ({
      symbol: `${item.code}${item.typeCode === 'KOSPI' ? '.KS' : '.KQ'}`,
      shortname: item.name,
      exchDisp: item.typeCode,
      typeDisp: 'Equity',
    }))
}
