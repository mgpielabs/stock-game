// FastAPI 예측 서버 클라이언트

const AI_BASE = import.meta.env.VITE_API_BASE ?? ''

export interface ShapEntry {
  feature: string
  label: string
  value: number | null
  shap: number
  direction: 'up' | 'down'
}

export interface TopReason {
  feature: string
  value: number | null
  impact: number
  label: string
}

export interface Prediction {
  rank: number
  symbol: string
  name: string
  probability: number
  // 보정확률(calibration, 2026-06-22) — 실제 적중률에 맞춰 보정된 표시용 값.
  // 랭킹은 항상 probability(raw)로 이미 결정돼 있고 이 필드는 화면 표시에만 씀.
  // 과거 모델(calibrator 없음)은 undefined일 수 있음 — 그 경우 probability로 폴백.
  probability_calibrated?: number
  shap_top: ShapEntry[]
  strategy?: TradeStrategy
  vol_ratio_5d?: number | null
  vol_ratio_20d?: number | null
  ret_1d?: number | null
  ret_5d?: number | null
  price_surge_warning?: boolean
  // 외국인+기관 동시 순매도 & 개인 순매수 경고(룰 기반, 모델 점수와 무관, 2026-06-27)
  investor_sell_warning?: boolean
  // 전일 intraday range 횡단면 상위5% 경고(룰 기반, 모델 점수와 무관, 2026-07-02)
  high_vol_warning?: boolean
  // YoY DPS 성장률 상위 25% 플래그(룰 기반, 모델 점수와 무관, 2026-07-03)
  dps_growth_flag?: boolean
  // C모드 갭 필터: trailing 20d 평균갭(%), >2%이면 gap_high=true(후순위 배치, 2026-07-04)
  trail_gap20d_pct?: number | null
  gap_high?: boolean
  top_reasons?: TopReason[]
  confidence_level?: 'HIGH' | 'MEDIUM' | 'LOW'
  risk_level?: 'HIGH' | 'MEDIUM' | 'LOW'
  risk_factors?: string[]
}

export interface DisclosureRisk {
  report_nm: string
  rcept_dt: string
  matched_keyword: string
  rcept_no?: string
}

export interface DisclosureAnalysis {
  severity: 'critical' | 'high' | 'medium'
  severity_label: string
  color: 'red' | 'orange' | 'yellow'
  direction: string
  range: string
  timing: string
  analysis: string
  rerank_eligible: boolean
  matched_keyword: string
}

export interface ExcludedStock {
  symbol: string
  risks: DisclosureRisk[]
  rank?: number
  analysis?: DisclosureAnalysis
}

export interface RegimeGate {
  regime: 'bull' | 'bear'
  bear_streak: number
  bull_streak: number
  gate_blocked: boolean
}

export interface TodayResponse {
  date: string
  total_stocks: number
  predictions: Prediction[]
  vol_surge?: Prediction[]
  excluded: ExcludedStock[]
  market_mode?: 'aggressive' | 'cautious' | 'defensive'
  market_message?: string
  threshold_applied?: number
  explanation_version?: string
  confidence_distribution?: Record<string, number>
  risk_distribution?: Record<string, number>
  bear_market_guard_active?: boolean
  bear_market_guard_reason?: string | null
  kospi_5d_ret_pct?: number | null
  model_regime?: 'bear' | 'unified'
  volatility_regime?: VolatilityRegime
  // C모드 갭 필터 메타데이터 (2026-07-04)
  gap_filter_mode?: string
  gap_filter_threshold_pct?: number
  gap_filter_high_count?: number
  gap_filter_expected_return_pct?: number
  gap_filter_note?: string
  // G2 레짐 게이트 상태 (2026-07-08)
  regime_gate?: RegimeGate
}

export interface HealthResponse {
  status: string
  latest_date: string | null
  model_dir: string | null
  prices_latest_date: string | null
  prices_stale_trading_days: number
  prices_stale: boolean
  retrain_due: boolean
  retrain_due_models: string[]
  model_ages_days: Record<string, number>
}

export interface VolatilityRegime {
  quartile: 'Q1' | 'Q2' | 'Q3' | 'Q4' | null
  current_value: number | null
  thresholds: { q1: number; q2: number; q3: number } | null
  low_vol_warning: boolean
  historical_precision_at_10: number | null
  reason: string
}

export interface TradeEntry {
  buy_price_low: number
  buy_price_high: number
  buy_low_pct: string
  buy_high_pct: string
  warning: string | null
  caution?: string
}

export interface ExitTargets {
  target1_price: number
  target1_pct: string
  target1_action: string
  target2_price: number
  target2_pct: string
  target2_action: string
  stop_loss_price: number
  stop_loss_pct: string
  stop_loss_action: string
}

export interface TradeStrategy {
  action_label: '관망 권장' | '신중 진입' | '적극 고려'
  action_reason: string
  entry: TradeEntry
  exit_targets: ExitTargets
  position: { weight_pct: number; weight_label: string }
  holding: { max_days: number; strategy: string }
  warnings: string[]
  risk_level: '낮음' | '중간' | '높음'
}

export interface PriceBar {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface TickerDetail {
  symbol: string
  date: string
  probability: number
  probability_calibrated?: number
  rank: number | null
  shap_full: ShapEntry[]
  price_history: PriceBar[]
  recent_features: Record<string, number | null>
  strategy?: TradeStrategy
}

export interface CostsApplied {
  commission_pct: number
  sell_tax_pct: number
  slippage_pct: number
  min_volume_bn: number
  avg_excluded_per_day: number
}

export interface BacktestData {
  dates: string[]
  cumulative_return_pct: number[]
  daily_return_pct: number[]
  total_return_pct: number
  sharpe_ratio: number
  max_drawdown_pct: number
  win_rate: number
  gross_total_return_pct?: number
  gross_sharpe_ratio?: number
  gross_win_rate?: number
  costs_applied?: CostsApplied
}

export interface PerformanceResponse {
  model_version: string | null
  target_col: string | null
  trained_at: string | null
  val_auc: number | null
  precision_at_topk: Record<string, Record<string, number> | number> | null
  label_basis: string | null
  close_win_rate_pct: number | null
  close_hit5_rate_pct: number | null
  backtest: BacktestData | null
}

export interface MarketTrend {
  trend: 'bull' | 'sideways' | 'bear' | 'unknown'
  label: string
  ret_5d_pct: number
  ret_20d_pct: number
  confidence: 'high' | 'medium' | 'low'
  badge: string | null
  dates: string[]
  returns: number[]
}

export interface DailySummary {
  date: string
  market: MarketTrend
  top3: Prediction[]
  model_confidence: string
  caution: string
}

// ── localStorage helpers ───────────────────────────────────────

const WL_KEY = 'ai_watchlist'
export const getWatchlist = (): string[] => JSON.parse(localStorage.getItem(WL_KEY) ?? '[]')
export const toggleWatchlist = (symbol: string): boolean => {
  const list = getWatchlist()
  const idx = list.indexOf(symbol)
  if (idx >= 0) list.splice(idx, 1); else list.push(symbol)
  localStorage.setItem(WL_KEY, JSON.stringify(list))
  return idx < 0
}

export const getMemo = (symbol: string): string => localStorage.getItem(`ai_memo_${symbol}`) ?? ''
export const saveMemo = (symbol: string, text: string): void =>
  localStorage.setItem(`ai_memo_${symbol}`, text)

export interface VirtualTrade {
  id: string
  symbol: string
  action: 'buy' | 'sell'
  date: string
  price: number
  shares: number
  note: string
}

const VT_KEY = 'ai_vtrades'
export const getVirtualTrades = (symbol?: string): VirtualTrade[] => {
  const all: VirtualTrade[] = JSON.parse(localStorage.getItem(VT_KEY) ?? '[]')
  return symbol ? all.filter(t => t.symbol === symbol) : all
}
export const addVirtualTrade = (t: Omit<VirtualTrade, 'id'>): void => {
  const all = getVirtualTrades()
  all.unshift({ ...t, id: `${Date.now()}` })
  localStorage.setItem(VT_KEY, JSON.stringify(all))
}
export const deleteVirtualTrade = (id: string): void => {
  const all = getVirtualTrades().filter(t => t.id !== id)
  localStorage.setItem(VT_KEY, JSON.stringify(all))
}

// ── Paper Trading 인터페이스 ──────────────────────────────────

export interface ActiveTrade {
  id: number
  symbol: string
  name?: string
  recommended_date: string
  recommended_rank: number | null
  recommended_prob: number | null
  recommended_price: number | null
  current_price: number | null
  unrealized_pct: number | null
  holding_days: number
  status: string
}

export interface TradeRecord {
  id: number
  symbol: string
  name?: string
  recommended_date: string
  recommended_rank: number | null
  recommended_prob: number | null
  recommended_price: number | null
  status: string
  close_date: string | null
  close_price: number | null
  return_pct: number | null
  holding_days: number | null
  model_version?: string | null
}

export interface TimelineEntry {
  date: string
  recommended_count: number
  closed_count: number
  avg_return_pct: number | null
  win_rate: number | null
  cumulative_return_pct: number
  /** 그 날 배치의 model_version. 한 배치에 여러 버전이 섞이면 '혼재'. */
  model_version?: string | null
}

export interface ModelPerformance {
  model_version: string | null
  label: string
  is_current: boolean
  is_current_5d: boolean
  is_current_60d: boolean
  is_regime_bear: boolean
  is_rejected: boolean
  is_pending: boolean
  is_untracked: boolean
  total: number
  open: number
  closed: number
  expired: number
  win_count: number
  win_rate: number | null
  avg_return_pct: number | null
  cumulative_return_pct: number | null
  insufficient_sample: boolean
}

export interface PerformanceByModel {
  active_model_version: string | null
  active_model_version_5d: string | null
  active_model_version_60d: string | null
  current_model_stats: ModelPerformance | null
  current_model_stats_5d: ModelPerformance | null
  current_model_stats_60d: ModelPerformance | null
  models: ModelPerformance[]
}

export interface TimelineGroupStat {
  count: number
  closed: number
  avg_return: number | null
  win_rate: number | null
}

export interface PerformanceTimeline {
  cutoff_date: string
  new_model_start: string
  new_model_count: number
  timeline: TimelineEntry[]
  by_confidence: Record<string, TimelineGroupStat>
  by_market_mode: Record<string, TimelineGroupStat>
  by_risk: Record<string, TimelineGroupStat>
}

export interface PerformanceSummary {
  total_trades: number
  closed_trades: number
  open_trades: number
  expired_trades: number
  win_count: number
  win_rate: number | null
  avg_return_pct: number | null
  total_return_pct: number | null
  avg_holding_days: number | null
  best_trade: { symbol: string; name?: string; return_pct: number } | null
  worst_trade: { symbol: string; name?: string; return_pct: number } | null
  recent_trades: TradeRecord[]
}

// ── API ────────────────────────────────────────────────────────

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${AI_BASE}${path}`)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(err.detail ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export interface RetrainStatus {
  status: 'idle' | 'running' | 'done' | 'error'
  elapsed_sec: number | null
  return_code?: number | null
  log: string[]
}

export interface ReanalysisStatus {
  available: boolean
  generated_at?: string
}

export interface UpdateStatus {
  status: 'idle' | 'running' | 'done' | 'error'
  elapsed_sec: number | null
  log: string[]
  dart_partial: boolean
  latest_feature_date: string | null
  latest_foreign_rate_date: string | null
  foreign_rate_stale_days: number | null
  foreign_rate_warning: boolean
}

export interface VolumeAnomalyStock {
  symbol: string
  name: string
  market: string
  sector: string | null
  vol_ratio_20d: number | null
  vol_ratio_5d: number | null
  vol_ratio_lag_1: number | null
  vol_ratio_lag_3: number | null
  ret_1d: number | null
  ret_5d: number | null
  ret_20d: number | null
  signal: 'fresh' | 'normal'
}

export interface VolumeAnomalyResponse {
  date: string
  count: number
  stocks: VolumeAnomalyStock[]
}

async function apiPost<T>(path: string): Promise<T> {
  const res = await fetch(`${AI_BASE}${path}`, { method: 'POST' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(err.detail ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export const paperTradingApi = {
  recordToday: (date?: string) =>
    apiPost<{ date: string; inserted: number; skipped: number }>(
      `/api/paper/record${date ? `?date=${date}` : ''}`
    ),
  closeExpired: () =>
    apiPost<{ closed: number; expired: number }>('/api/paper/close-expired'),
  getActive: () =>
    apiFetch<ActiveTrade[]>('/api/paper/active'),
  getPerformance: () =>
    apiFetch<PerformanceSummary>('/api/paper/performance'),
  getTimeline: (days = 30, since?: string) =>
    apiFetch<PerformanceTimeline>(
      `/api/paper/performance-timeline?days=${days}${since ? `&since=${since}` : ''}`
    ),
  getPerformanceByModel: () =>
    apiFetch<PerformanceByModel>('/api/paper/performance-by-model'),
}

// ── 스크리너 ──────────────────────────────────────────────────

export interface ScreenerStock {
  symbol: string
  name: string
  market: string
  close: number
  per: number | null
  per_pit: number | null
  pbr: number | null
  pbr_pit: number | null
  rsi_14: number | null
  bb_pct: number | null
  vol_ratio_20d: number | null
  ret_20d: number | null
  atr_pct: number | null
  dividend_yield: number | null
  dps_growth: boolean
  // 60d 모델 PIT 팩터
  eps_growth_yoy: number | null
  eps_growth_accel: number | null
  roe_level: number | null
  bps_growth_yoy: number | null
  // 유동성 / 60d 점수
  vol_krw_20d: number | null
  score_60d: number | null
}

export interface ScreenerRegime {
  trend: 'bull' | 'sideways' | 'bear' | 'unknown'
  label: string
  ret_20d_pct: number | null
  mean_reversion_valid: boolean
  mean_reversion_reason: string
}

export interface ScreenerResponse {
  date: string
  market_regime: ScreenerRegime
  total: number
  stocks: ScreenerStock[]
}

export interface ScreenerFilters {
  high_dividend?: boolean
  rsi_oversold?: boolean
  bb_lower?: boolean
  low_per?: boolean
  low_pbr?: boolean
  div_growth?: boolean
  eps_growth_top?: boolean
  eps_accel?: boolean
  roe_top?: boolean
  bps_growth_top?: boolean
  score_60d_top20?: boolean
  score_60d_top10?: boolean
  min_vol20d?: boolean
  exclude_high_atr?: boolean
  sort_by?: string
  sort_dir?: 'asc' | 'desc'
  limit?: number
}

export interface StockSearchResult {
  symbol: string
  name: string
  market: string
}

export interface FilterFlags {
  high_dividend: boolean
  rsi_oversold: boolean
  bb_lower: boolean
  low_per: boolean
  low_pbr: boolean
  div_growth: boolean
  eps_growth_top: boolean
  eps_accel: boolean
  roe_top: boolean
  bps_growth_top: boolean
  score_60d_top20: boolean
  score_60d_top10: boolean
  min_vol20d: boolean
  exclude_high_atr: boolean
}

export interface SymbolProfile {
  date: string
  market_regime: ScreenerRegime
  stock: ScreenerStock
  filter_flags: FilterFlags
}

export const screenerApi = {
  search: (filters: ScreenerFilters) => {
    const params = new URLSearchParams()
    Object.entries(filters).forEach(([k, v]) => { if (v !== undefined) params.set(k, String(v)) })
    return apiFetch<ScreenerResponse>(`/api/screener?${params.toString()}`)
  },
  stocksSearch: (q: string) =>
    apiFetch<StockSearchResult[]>(`/api/stocks/search?q=${encodeURIComponent(q)}`),
  symbolProfile: (symbol: string) =>
    apiFetch<SymbolProfile>(`/api/screener?symbol=${encodeURIComponent(symbol)}`),
}

export const updateApi = {
  start: () => apiPost<{ status: string; pid: number }>('/api/update'),
  getStatus: () => apiFetch<UpdateStatus>('/api/update/status'),
}

export interface Prediction60d {
  rank: number
  symbol: string
  name: string
  market: string
  probability: number
  close: number | null
  eps_growth_yoy: number | null
  dps_growth_yoy: number | null
  roe_level: number | null
  neg_pbr: number | null
}

export interface Predictions60dResponse {
  date: string
  predictions: Prediction60d[]
  count: number
  model: string
  note: string
}

export const aiApi = {
  health: () =>
    apiFetch<HealthResponse>('/health'),
  predictions: (topN = 30) =>
    apiFetch<TodayResponse>(`/api/predictions/today?top_n=${topN}`),
  predictions60d: (topN = 10) =>
    apiFetch<Predictions60dResponse>(`/api/predictions/60d?top_n=${topN}`),
  ticker: (symbol: string, priceDays = 60) =>
    apiFetch<TickerDetail>(`/api/predictions/${symbol}?price_days=${priceDays}`),
  performance: () =>
    apiFetch<PerformanceResponse>('/api/backtest/performance'),
  marketTrend: () =>
    apiFetch<MarketTrend>('/api/market/trend'),
  dailySummary: () =>
    apiFetch<DailySummary>('/api/daily/summary'),
  startRetrain: (quick = false) =>
    fetch(`${AI_BASE}/api/admin/retrain?quick=${quick}`, { method: 'POST' }).then(async r => {
      if (!r.ok) { const e = await r.json().catch(() => ({ detail: `HTTP ${r.status}` })); throw new Error(e.detail) }
      return r.json()
    }),
  retrainStatus: () =>
    apiFetch<RetrainStatus>('/api/admin/retrain/status'),
  reanalysisStatus: () =>
    apiFetch<ReanalysisStatus>('/api/admin/reanalysis-status'),
  volumeAnomaly: (volMin = 5.0, priceMax = 0.05, ret5dMax = 0.25) =>
    apiFetch<VolumeAnomalyResponse>(
      `/api/volume-anomaly?vol_min=${volMin}&price_max=${priceMax}&ret5d_max=${ret5dMax}`
    ),
  alltimeVolumeSurge: (rankMax = 10, retMin = 0.0, retMax = 0.15, lookback = 5) =>
    apiFetch<AlltimeVolumeSurgeResponse>(
      `/api/alltime-volume-surge?rank_max=${rankMax}&ret_min=${retMin}&ret_max=${retMax}&lookback=${lookback}`
    ),
}

export interface AlltimeVolumeSurgeStock {
  symbol: string
  name: string
  market: string
  sector: string | null
  vol_rank: number
  total_days: number
  surge_date: string
  days_ago: number
  surge_volume: number
  surge_close: number
  surge_ret: number
  latest_close: number | null
  ret_since_surge: number
  signal_strength: '극강' | '강' | '유의'
  per: number | null
}

export interface AlltimeVolumeSurgeResponse {
  date: string
  lookback: number
  count: number
  stocks: AlltimeVolumeSurgeStock[]
}
