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
  kospi_20d?: number[]
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
  sector?: string | null
  recommended_date: string
  recommended_rank: number | null
  recommended_prob: number | null
  recommended_price: number | null
  entry_price: number | null
  current_price: number | null
  unrealized_pct: number | null
  holding_days: number
  status: string
  horizon?: number
}

export interface TradeRecord {
  id: number
  symbol: string
  name?: string
  recommended_date: string
  recommended_rank: number | null
  recommended_prob: number | null
  recommended_price: number | null
  entry_price?: number | null
  status: string
  close_date: string | null
  close_price: number | null
  return_pct: number | null
  holding_days: number | null
  model_version?: string | null
  horizon?: number
}

export interface LivePerformanceSlot {
  status: 'collecting' | 'ready'
  n: number
  min_sample?: number
  metric?: 'P@10' | 'avg_excess_return'
  value?: number | null
  unit?: string
  description?: string
}

export interface LivePerformanceResponse {
  data_as_of?: string | null
  '5d': LivePerformanceSlot
  '60d': LivePerformanceSlot
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
  status: 'idle' | 'running' | 'done' | 'error' | 'skipped'
  reason?: string          // 스킵 이유: 'weekend' | 'already_current' (status='skipped' 일 때)
  elapsed_sec: number | null
  log: string[]
  dart_partial: boolean
  latest_feature_date: string | null
  latest_foreign_rate_date: string | null
  foreign_rate_stale_days: number | null
  foreign_rate_warning: boolean
  // 진행 단계 / 경과시간 앵커 / 하트비트 (running 상태에서만 채워짐)
  current_stage: number | null
  current_stage_name: string | null
  total_stages: number | null
  started_at: number | null      // Unix timestamp (초, float) — 클라이언트 경과시간 계산용
  last_heartbeat: number | null  // Unix timestamp (초, float) — 300s 초과 시 응답없음 경고
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
  shadowVsLive: () =>
    apiFetch<ShadowVsLiveResponse>('/api/paper/shadow-vs-live'),
}

// ── Shadow vs Live ────────────────────────────────────────────

export interface ShadowSlot {
  status: 'collecting' | 'ready'
  n: number
  avg_return_pct?: number
  hit_rate_pct?: number
  label: string
}

export interface ShadowVsLiveResponse {
  live: ShadowSlot
  shadow_blocked: ShadowSlot
  live_60d: ShadowSlot
  shadow_blocked_60d: ShadowSlot
}

// ── 스크리너 ──────────────────────────────────────────────────

export interface ScreenerStock {
  symbol: string
  name: string
  market: string
  sector?: string | null
  sector_name?: string | null
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
  dividend_yield: number | null    // trailing yield = DPS ÷ 현재가
  actual_yield: number | null      // 실질 배당수익률 = 보정DPS ÷ 배당기준일 주가
  dps_growth: boolean
  has_special_dividend: boolean
  has_split_adjusted: boolean
  has_div_suspended: boolean
  has_unverified_yield: boolean
  // 60d 모델 PIT 팩터
  eps_growth_yoy: number | null
  eps_growth_accel: number | null
  roe_level: number | null
  bps_growth_yoy: number | null
  // 유동성 / 60d 점수
  vol_krw_20d: number | null
  score_60d: number | null
  // 외국인 보유비율 (flows.foreign_net 최신값, 표시 전용)
  foreign_rate: number | null
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

export interface CrossAnalysisPreset {
  label: string
  keys: string[]
  oos_rate: string
}

export interface CrossAnalysisStock {
  symbol: string
  name: string
  market: string
  close: number
  dividend_yield: number | null
  actual_yield: number | null
  score_60d: number | null
  per: number | null
  pbr: number | null
  combo_count: number
  combo_flags: boolean[]
}

export interface CrossAnalysisResponse {
  date: string
  presets: CrossAnalysisPreset[]
  total: number
  stocks: CrossAnalysisStock[]
}

export interface SectorFlowStock {
  symbol: string
  name: string
  score_60d: number | null   // raw(0-1) — 표시 시 ×100
  combo_count: number
}

export interface SectorFlowStockItem {
  symbol: string
  name: string
  score_60d: number | null   // raw(0-1) — 표시 시 ×100
  foreign_5d: number         // 억원 (외국인 5d 순매수)
  inst_5d: number            // 억원 (기관 5d 순매수)
  foreign_20d: number        // 억원 (외국인 20d 순매수)
  inst_20d: number           // 억원 (기관 20d 순매수)
  foreign_60d: number        // 억원 (외국인 60d 순매수)
  inst_60d: number           // 억원 (기관 60d 순매수)
  foreign_120d: number       // 억원 (외국인 120d 순매수)
  inst_120d: number          // 억원 (기관 120d 순매수)
  foreign_250d: number       // 억원 (외국인 250d 순매수)
  inst_250d: number          // 억원 (기관 250d 순매수)
}

export interface SectorFlowEntry {
  code: string
  name: string
  stock_count: number
  foreign_5d: number
  inst_5d: number
  pension_5d: number | null
  combined_5d: number
  foreign_20d: number
  inst_20d: number
  pension_20d: number | null
  combined_20d: number
  foreign_60d: number
  inst_60d: number
  pension_60d: number | null
  combined_60d: number
  foreign_120d: number
  inst_120d: number
  pension_120d: number | null
  combined_120d: number
  foreign_250d: number
  inst_250d: number
  pension_250d: number | null
  combined_250d: number
  momentum_5d: number | null
  pension_available: boolean
  cross_stocks: SectorFlowStock[]
  stocks: SectorFlowStockItem[]
}

export interface SectorFlowResponse {
  date: string
  pension_note: string
  pension_latest_date: string | null
  cutoffs: { '5d': string; '20d': string; '60d': string; '120d': string; '250d': string }
  coverage: {
    sector_count: number
    symbols_in_sectors: number
    total_universe: number
  }
  sectors: SectorFlowEntry[]
}

// ── 섹터 흐름 종합 분석 ─────────────────────────────────────────────────────
export interface SectorFlowAnalysisMarketSummary {
  totals: { '5d': number; '20d': number; '60d': number; '120d': number; '250d': number }
  direction_label: string
  direction_color: 'green' | 'red' | 'orange' | 'gray'
  period_labels: { '5d': string; '20d': string; '60d': string; '120d': string; '250d': string }
}

export interface SectorFlowAnalysisRankEntry {
  code: string
  name: string
  value: number
}

export interface SectorFlowAnalysisRankPeriod {
  top: SectorFlowAnalysisRankEntry[]
  bottom: SectorFlowAnalysisRankEntry[]
}

export interface SectorFlowAnalysisClassEntry {
  code: string
  name: string
  combined_5d: number
  combined_20d: number
  combined_60d: number
  has_cross_stocks?: boolean
}

export interface SectorFlowAnalysisConcentrationEntry {
  sector_code: string
  sector_name: string
  sector_total: number
  top1_symbol: string
  top1_name: string
  top1_value: number
  concentration_pct: number
  is_concentrated: boolean
  mktcap_ratio_pct: number
  market_cap_억: number
  surge_ratio: number | null
  surge_label: string | null
}

export interface SectorFlowAnalysisCrossAnalysis {
  total_cross_stocks: number
  cross_in_inflow_sectors: number
  cross_as_sector_top1: number
  inflow_sector_count: number
}

export interface SectorFlowAnalysisResponse {
  date: string
  data_source: {
    label: string
    detail: string
    pension_latest_date: string | null
  }
  market_summary: SectorFlowAnalysisMarketSummary
  sector_rankings: {
    '5d': SectorFlowAnalysisRankPeriod
    '20d': SectorFlowAnalysisRankPeriod
    '60d': SectorFlowAnalysisRankPeriod
    '120d': SectorFlowAnalysisRankPeriod
    '250d': SectorFlowAnalysisRankPeriod
  }
  sector_classification: {
    consistent_inflow: SectorFlowAnalysisClassEntry[]
    consistent_outflow: SectorFlowAnalysisClassEntry[]
    short_reversal: SectorFlowAnalysisClassEntry[]
    short_exit: SectorFlowAnalysisClassEntry[]
  }
  concentration: {
    all: SectorFlowAnalysisConcentrationEntry[]
    concentrated: SectorFlowAnalysisConcentrationEntry[]
  }
  cross_analysis: SectorFlowAnalysisCrossAnalysis
}

export interface StockChartPoint {
  date: string                 // YYYYMMDD
  open: number | null          // 시가
  high: number | null          // 고가
  low: number | null           // 저가
  close: number                // 종가
  volume: number | null        // 거래량 (주)
  foreign_net: number | null   // 외국인 순매수 (억원)
  inst_net: number | null      // 기관계 순매수 (억원)
  indiv_net: number | null     // 개인 순매수 (억원)
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
  crossAnalysis: () =>
    apiFetch<CrossAnalysisResponse>('/api/screener/cross-analysis'),
  sectorFlow: () =>
    apiFetch<SectorFlowResponse>('/api/screener/sector-flow'),
  sectorFlowAnalysis: (period = '5d') =>
    apiFetch<SectorFlowAnalysisResponse>(`/api/screener/sector-flow/analysis?period=${encodeURIComponent(period)}`),
  stockChart: (symbol: string, period: '60d' | '120d' | '1y' | '2y' | '3y' | 'all' = '1y') =>
    apiFetch<StockChartPoint[]>(`/api/stock/${encodeURIComponent(symbol)}/chart?period=${period}`),
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

export interface OpenPosition {
  symbol: string
  name: string
  recommended_date: string
  horizon: number
  elapsed_days: number
  remaining_days: number
  recommended_price: number
  current_price: number | null
  pnl_pct: number | null
}

export interface OpenSummaryResponse {
  count: number
  avg_pnl_pct: number | null
  best: OpenPosition | null
  worst: OpenPosition | null
  positions: OpenPosition[]
}

export const aiApi = {
  health: () =>
    apiFetch<HealthResponse>('/health'),
  predictions: (topN = 30) =>
    apiFetch<TodayResponse>(`/api/predictions/today?top_n=${topN}`),
  predictions60d: (topN = 10) =>
    apiFetch<Predictions60dResponse>(`/api/predictions/60d?top_n=${topN}`),
  openSummary: () =>
    apiFetch<OpenSummaryResponse>('/api/paper/open-summary'),
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
  livePerformance: () =>
    apiFetch<LivePerformanceResponse>('/api/live-performance'),
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

export interface WatchlistItem {
  symbol: string
  name: string | null
  added_at: string
  memo: string | null
}

async function apiFetchWithOptions<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${AI_BASE}${path}`, options)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(err.detail ?? `HTTP ${res.status}`)
  }
  return res.json() as Promise<T>
}

export const watchlistApi = {
  list: () => apiFetch<WatchlistItem[]>('/api/watchlist'),
  add: (symbol: string, name?: string) =>
    apiFetchWithOptions<WatchlistItem>('/api/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, name }),
    }),
  remove: (symbol: string) =>
    apiFetchWithOptions<{ symbol: string; removed: boolean }>(`/api/watchlist/${symbol}`, { method: 'DELETE' }),
}

export interface SignalLog {
  id: number
  event_type: 'foreign_surge' | 'score60d_entry' | 'watchlist_price_jump' | 'sector_quadrant' | 'position_event'
  ticker: string | null
  sector: string | null
  message: string
  data: Record<string, unknown> | null
  created_at: string
}

export const signalsApi = {
  list: (days = 7) => apiFetch<SignalLog[]>(`/api/signals?days=${days}`),
}

export interface CalendarEvent {
  date: string  // YYYYMMDD
  type:
    | 'position_entry' | 'position_expiry'
    | 'dividend_record' | 'dividend_exdate' | 'dividend_payment'
    | 'signal_event' | 'system_schedule'
    | 'earnings_season'
    | 'options_expiry' | 'quadruple_witching'
    | 'fomc' | 'bok_rate'
    | 'msci_rebalance'
    | 'market_holiday'
  color:
    | 'blue' | 'orange' | 'green' | 'teal' | 'red' | 'purple'
    | 'yellow' | 'amber' | 'rose' | 'slate' | 'violet' | 'gray'
  label: string
  ticker: string | null
  related_stocks?: Array<{ symbol: string; name: string }>
}

export const calendarApi = {
  events: (year: number, month: number) =>
    apiFetch<CalendarEvent[]>(`/api/calendar?year=${year}&month=${month}`),
}

export interface CorrelationResponse {
  symbols: string[]
  names: Record<string, string>
  matrix: number[][]
  avg_correlation: number | null
  trading_days_used?: number
  message?: string
}

export const portfolioApi = {
  correlation: () => apiFetch<CorrelationResponse>('/api/portfolio/correlation'),
}

// ── 내 포트폴리오 ────────────────────────────────────────────────────────────

export interface MyPortfolioItem {
  id: number
  ticker: string
  name: string
  buy_price: number
  quantity: number
  buy_date: string
  memo?: string | null
  created_at?: string
}

export interface MyPortfolioAnalysisItem extends MyPortfolioItem {
  current_price: number
  return_pct: number
  current_value: number
  pnl: number
  sector: string
  weight_pct: number
}

export interface MyPortfolioSectorWeight {
  sector: string
  value: number
  weight_pct: number
}

export interface MyPortfolioFlowAlignment {
  ticker: string
  name: string
  sector: string
  quadrant: 'consistent_inflow' | 'consistent_outflow' | 'short_reversal' | 'neutral'
  combined_5d: number
  combined_20d: number
  combined_60d: number
}

export interface MyPortfolioAnalysis {
  date: string
  summary: {
    total_invested: number
    total_value: number
    total_return_pct: number
    total_pnl: number
    stock_count: number
  }
  items: MyPortfolioAnalysisItem[]
  sector_weights: MyPortfolioSectorWeight[]
  correlation: CorrelationResponse
  sector_flow_alignment: MyPortfolioFlowAlignment[]
  event_impact: Record<string, number>
  error?: string
}

export const myPortfolioApi = {
  list: () => apiFetch<MyPortfolioItem[]>('/api/my-portfolio'),
  add: (body: Omit<MyPortfolioItem, 'id' | 'created_at'>) =>
    apiFetchWithOptions<MyPortfolioItem>('/api/my-portfolio', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  update: (id: number, body: Partial<Omit<MyPortfolioItem, 'id' | 'ticker' | 'created_at'>>) =>
    apiFetchWithOptions<MyPortfolioItem>(`/api/my-portfolio/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  remove: (id: number) =>
    apiFetchWithOptions<{ deleted: number }>(`/api/my-portfolio/${id}`, { method: 'DELETE' }),
  analysis: () => apiFetch<MyPortfolioAnalysis>('/api/my-portfolio/analysis'),
}
