import { describe, it, expect } from 'vitest'

type ChartPeriod = '60d' | '120d' | '1y' | '2y' | '3y' | 'all'

function periodDays(p: ChartPeriod): number {
  if (p === '60d')  return 60
  if (p === '120d') return 120
  if (p === '2y')   return 504
  if (p === '3y')   return 756
  if (p === 'all')  return 9999
  return 252 // '1y'
}

function visibleRange(dataLen: number, period: ChartPeriod) {
  const days = periodDays(period)
  const n = Math.min(days, dataLen)
  return { fromIdx: dataLen - n, toIdx: dataLen - 1 }
}

describe('periodDays', () => {
  it('60d → 60', () => expect(periodDays('60d')).toBe(60))
  it('120d → 120', () => expect(periodDays('120d')).toBe(120))
  it('1y → 252', () => expect(periodDays('1y')).toBe(252))
  it('2y → 504', () => expect(periodDays('2y')).toBe(504))
  it('3y → 756', () => expect(periodDays('3y')).toBe(756))
  it('all → 9999', () => expect(periodDays('all')).toBe(9999))
})

describe('visibleRange', () => {
  it('데이터가 충분할 때 요청 기간만큼 범위 선택', () => {
    const r = visibleRange(1000, '1y')
    expect(r.fromIdx).toBe(1000 - 252)
    expect(r.toIdx).toBe(999)
  })

  it('데이터가 기간보다 짧으면 전체 표시', () => {
    const r = visibleRange(50, '1y')
    expect(r.fromIdx).toBe(0)
    expect(r.toIdx).toBe(49)
  })

  it('2년 기간 — 데이터 충분', () => {
    const r = visibleRange(1000, '2y')
    expect(r.fromIdx).toBe(1000 - 504)
    expect(r.toIdx).toBe(999)
  })

  it('3년 기간 — 데이터 충분', () => {
    const r = visibleRange(1000, '3y')
    expect(r.fromIdx).toBe(1000 - 756)
    expect(r.toIdx).toBe(999)
  })

  it('전체 기간 — 항상 처음부터', () => {
    const r = visibleRange(1500, 'all')
    expect(r.fromIdx).toBe(0)
    expect(r.toIdx).toBe(1499)
  })

  it('데이터 1건 edge case', () => {
    const r = visibleRange(1, '60d')
    expect(r.fromIdx).toBe(0)
    expect(r.toIdx).toBe(0)
  })
})
