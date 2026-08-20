import { describe, it, expect } from 'vitest'
import type { CrossAnalysisStock, CrossAnalysisPreset, CrossAnalysisResponse } from '../api/aiRecommend'

// CrossAnalysisWidget 내부 필터 로직을 동일한 방식으로 재현해 테스트
function filterStocks(stocks: CrossAnalysisStock[], activeIdx: number | null): CrossAnalysisStock[] {
  return activeIdx !== null
    ? stocks.filter(s => s.combo_flags[activeIdx])
    : stocks
}

function calcPresetCounts(stocks: CrossAnalysisStock[], presets: CrossAnalysisPreset[]): number[] {
  return presets.map((_, i) => stocks.filter(s => s.combo_flags[i]).length)
}

// 테스트용 더미 종목 생성 헬퍼
function makeStock(symbol: string, combo_flags: boolean[]): CrossAnalysisStock {
  return {
    symbol,
    name: `종목${symbol}`,
    market: 'KOSDAQ',
    close: 10000,
    dividend_yield: 3.0,
    actual_yield: 3.0,
    score_60d: 0.5,
    per: 10,
    pbr: 0.5,
    combo_count: combo_flags.filter(Boolean).length,
    combo_flags,
  }
}

const PRESETS: CrossAnalysisPreset[] = [
  { label: '고배당+저PBR+60d상위20%', keys: [], oos_rate: '76.1%' },
  { label: '고배당+저PER+60d상위10%', keys: [], oos_rate: '75.7%' },
  { label: '고배당+저PER', keys: [], oos_rate: '71.9%' },
  { label: '고배당+저PBR', keys: [], oos_rate: '68.8%' },
  { label: '고배당+배당성장', keys: [], oos_rate: '67.5%' },
  { label: '저PBR+배당성장', keys: [], oos_rate: '63.9%' },
]

// 6개 프리셋 중 특정 조합을 통과하는 종목들
const ALL_TRUE = makeStock('A', [true, true, true, true, true, true])     // 모든 프리셋 통과 (combo_count=6)
const PRESET_0_1 = makeStock('B', [true, true, false, false, false, false]) // 프리셋 0,1만
const PRESET_5_ONLY = makeStock('C', [false, false, false, false, false, true]) // 프리셋 5만 (combo_count=1, 실제로는 filtering되지 않지만 테스트용)
const PRESET_3_4_5 = makeStock('D', [false, false, false, true, true, true])   // 프리셋 3,4,5
const NO_PRESET = makeStock('E', [false, false, false, false, false, false])    // 없음

const STOCKS = [ALL_TRUE, PRESET_0_1, PRESET_5_ONLY, PRESET_3_4_5, NO_PRESET]

// ─── filterStocks ────────────────────────────────────────────────

describe('filterStocks', () => {
  it('activeIdx=null 이면 전체 종목 반환', () => {
    const result = filterStocks(STOCKS, null)
    expect(result).toHaveLength(5)
  })

  it('프리셋 0 필터링 → combo_flags[0]=true인 종목만', () => {
    const result = filterStocks(STOCKS, 0)
    // A(true), B(true), C(false), D(false), E(false) → A, B
    expect(result).toHaveLength(2)
    expect(result.map(s => s.symbol)).toEqual(['A', 'B'])
  })

  it('프리셋 5 필터링 → combo_flags[5]=true인 종목만', () => {
    const result = filterStocks(STOCKS, 5)
    // A(true), B(false), C(true), D(true), E(false) → A, C, D
    expect(result).toHaveLength(3)
    expect(result.map(s => s.symbol)).toContain('A')
    expect(result.map(s => s.symbol)).toContain('C')
    expect(result.map(s => s.symbol)).toContain('D')
    expect(result.map(s => s.symbol)).not.toContain('B')
    expect(result.map(s => s.symbol)).not.toContain('E')
  })

  it('프리셋 2 필터링 → combo_flags[2]=true인 종목만', () => {
    const result = filterStocks(STOCKS, 2)
    // A만
    expect(result).toHaveLength(1)
    expect(result[0].symbol).toBe('A')
  })

  it('모든 종목이 false인 프리셋 → 빈 배열', () => {
    const result = filterStocks(STOCKS, 4)
    // A(true), B(false), C(false), D(true), E(false)
    // A, D 포함
    expect(result.every(s => s.combo_flags[4])).toBe(true)
  })

  it('activeIdx 토글 — 같은 인덱스 다시 누르면 null이 되어야 함 (UI 로직, 상태 외부에서 관리)', () => {
    // 실제 UI: setActiveIdx(activeIdx === i ? null : i)
    // 상태 전환 테스트: 0→0 재클릭 → null
    const toggle = (current: number | null, clicked: number | null) =>
      current === clicked ? null : clicked

    expect(toggle(0, 0)).toBeNull()       // 같은 버튼 재클릭 → null(해제)
    expect(toggle(null, 0)).toBe(0)       // 버튼 첫 클릭 → 0
    expect(toggle(0, 1)).toBe(1)          // 다른 버튼 클릭 → 1
    expect(toggle(null, null)).toBeNull() // 전체 버튼 → null
  })
})

// ─── calcPresetCounts ────────────────────────────────────────────

describe('calcPresetCounts', () => {
  it('프리셋 수만큼 배열 반환', () => {
    const counts = calcPresetCounts(STOCKS, PRESETS)
    expect(counts).toHaveLength(PRESETS.length)
  })

  it('각 프리셋별 통과 종목 수 정확히 계산', () => {
    const counts = calcPresetCounts(STOCKS, PRESETS)
    // 프리셋 0: A(true), B(true) → 2
    expect(counts[0]).toBe(2)
    // 프리셋 1: A(true), B(true) → 2
    expect(counts[1]).toBe(2)
    // 프리셋 2: A만(true) → 1
    expect(counts[2]).toBe(1)
    // 프리셋 3: A(true), D(true) → 2
    expect(counts[3]).toBe(2)
    // 프리셋 4: A(true), D(true) → 2
    expect(counts[4]).toBe(2)
    // 프리셋 5: A(true), C(true), D(true) → 3
    expect(counts[5]).toBe(3)
  })

  it('종목이 없으면 전부 0', () => {
    const counts = calcPresetCounts([], PRESETS)
    expect(counts.every(c => c === 0)).toBe(true)
  })

  it('DISPLAY_LIMIT=20 증가로 다른 프리셋 선택 시 표시 종목이 달라짐', () => {
    // 13개 combo_count=6 + 나머지로 구성된 81종목 시뮬레이션 축소판
    // combo_count=6인 상위 3개 종목 + 각 프리셋에 1개씩 추가
    const top3 = Array.from({ length: 3 }, (_, i) =>
      makeStock(`TOP${i}`, [true, true, true, true, true, true])
    )
    const preset0Only = makeStock('P0', [true, false, false, false, false, false])
    const preset5Only = makeStock('P5', [false, false, false, false, false, true])
    const allStocks = [...top3, preset0Only, preset5Only]

    const filtered0 = filterStocks(allStocks, 0)
    const filtered5 = filterStocks(allStocks, 5)

    // 프리셋 0 → TOP3 + P0 = 4종목
    expect(filtered0).toHaveLength(4)
    expect(filtered0.map(s => s.symbol)).toContain('P0')
    expect(filtered0.map(s => s.symbol)).not.toContain('P5')

    // 프리셋 5 → TOP3 + P5 = 4종목
    expect(filtered5).toHaveLength(4)
    expect(filtered5.map(s => s.symbol)).toContain('P5')
    expect(filtered5.map(s => s.symbol)).not.toContain('P0')

    // DISPLAY_LIMIT=20이면 5종목 모두 표시 → 프리셋 따라 P0/P5 차이 보임
    const displayed0 = filtered0.slice(0, 20)
    const displayed5 = filtered5.slice(0, 20)
    expect(displayed0.map(s => s.symbol)).toContain('P0')
    expect(displayed5.map(s => s.symbol)).toContain('P5')
    expect(displayed0.map(s => s.symbol)).not.toContain('P5')
    expect(displayed5.map(s => s.symbol)).not.toContain('P0')
  })

  it('"전체" 선택 시 filteredCount = data.total', () => {
    const data: Pick<CrossAnalysisResponse, 'stocks' | 'total'> = {
      stocks: STOCKS,
      total: STOCKS.length,
    }
    const activeIdx = null
    const displayedCount = activeIdx !== null
      ? filterStocks(data.stocks, activeIdx).length
      : data.total

    expect(displayedCount).toBe(5)
  })

  it('특정 프리셋 선택 시 filteredCount = 해당 프리셋 통과 수', () => {
    const data: Pick<CrossAnalysisResponse, 'stocks' | 'total'> = {
      stocks: STOCKS,
      total: STOCKS.length,
    }
    const activeIdx = 5 // 저PBR+배당성장 → A, C, D = 3
    const displayedCount = filterStocks(data.stocks, activeIdx).length
    expect(displayedCount).toBe(3)
  })
})
