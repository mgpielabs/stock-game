import { describe, it, expect } from 'vitest'
import { expectedLatestTradingDay, tradingDayDiff, calcDataFreshness } from './ScreenerPage'

// KST 시각으로 Date를 만드는 헬퍼 (UTC 보정 포함)
function kst(year: number, month: number, day: number, hour = 0, minute = 0): Date {
  // month: 1-based
  const utcMs = Date.UTC(year, month - 1, day, hour - 9, minute)
  return new Date(utcMs)
}

// ─── expectedLatestTradingDay ────────────────────────────────────

describe('expectedLatestTradingDay', () => {
  it('평일 16:30 이후 → 당일', () => {
    // 2026-08-12 (수) 17:00 KST
    const result = expectedLatestTradingDay(kst(2026, 8, 12, 17, 0))
    expect(result).toBe('20260812')
  })

  it('평일 16:30 이전 → 전날 (전날이 평일)', () => {
    // 2026-08-12 (수) 09:00 KST
    const result = expectedLatestTradingDay(kst(2026, 8, 12, 9, 0))
    expect(result).toBe('20260811')
  })

  it('정확히 16:30 → 당일', () => {
    // 2026-08-12 (수) 16:30 KST
    const result = expectedLatestTradingDay(kst(2026, 8, 12, 16, 30))
    expect(result).toBe('20260812')
  })

  it('16:29 → 전날', () => {
    const result = expectedLatestTradingDay(kst(2026, 8, 12, 16, 29))
    expect(result).toBe('20260811')
  })

  it('월요일 09:00 (16:30 이전) → 직전 금요일', () => {
    // 2026-08-10 (월) 09:00 KST, 전날=일요일 → 금요일로
    const result = expectedLatestTradingDay(kst(2026, 8, 10, 9, 0))
    expect(result).toBe('20260807')
  })

  it('월요일 17:00 (16:30 이후) → 월요일', () => {
    // 2026-08-10 (월) 17:00 KST
    const result = expectedLatestTradingDay(kst(2026, 8, 10, 17, 0))
    expect(result).toBe('20260810')
  })

  it('토요일 → 금요일', () => {
    // 2026-08-08 (토) 12:00 KST (16:30 이전)
    const result = expectedLatestTradingDay(kst(2026, 8, 8, 12, 0))
    expect(result).toBe('20260807')
  })

  it('토요일 18:00 → 금요일 (16:30 이후여도 주말)', () => {
    const result = expectedLatestTradingDay(kst(2026, 8, 8, 18, 0))
    expect(result).toBe('20260807')
  })

  it('일요일 → 금요일', () => {
    // 2026-08-09 (일)
    const result = expectedLatestTradingDay(kst(2026, 8, 9, 15, 0))
    expect(result).toBe('20260807')
  })

  it('화요일 아침 → 월요일', () => {
    // 2026-08-11 (화) 08:00 KST
    const result = expectedLatestTradingDay(kst(2026, 8, 11, 8, 0))
    expect(result).toBe('20260810')
  })
})

// ─── tradingDayDiff ─────────────────────────────────────────────

describe('tradingDayDiff', () => {
  it('같은 날 → 0', () => {
    expect(tradingDayDiff('20260812', '20260812')).toBe(0)
  })

  it('연속 평일 하루 차이 → 1', () => {
    expect(tradingDayDiff('20260811', '20260812')).toBe(1)
  })

  it('금→월 (주말 건너뜀) → 1', () => {
    expect(tradingDayDiff('20260807', '20260810')).toBe(1)
  })

  it('목→월 (금+주말) → 2', () => {
    expect(tradingDayDiff('20260806', '20260810')).toBe(2)
  })

  it('수→수 (한 주 뒤) → 5', () => {
    // 2026-08-05(수) ~ 2026-08-12(수)
    expect(tradingDayDiff('20260805', '20260812')).toBe(5)
  })

  it('newer가 older 이하 → 0', () => {
    expect(tradingDayDiff('20260812', '20260811')).toBe(0)
  })
})

// ─── calcDataFreshness ───────────────────────────────────────────

describe('calcDataFreshness', () => {
  it('null 입력 → null 반환', () => {
    expect(calcDataFreshness(null)).toBeNull()
  })

  it('데이터 날짜 = 기대 최신일 → green, ✅ 최신', () => {
    // 2026-08-12(수) 17:00 KST → expected = 20260812
    const result = calcDataFreshness('20260812', kst(2026, 8, 12, 17, 0))
    expect(result).not.toBeNull()
    expect(result!.color).toBe('green')
    expect(result!.lag).toBe(0)
    expect(result!.label).toContain('최신')
  })

  it('1 거래일 뒤처짐 → orange', () => {
    // expected = 20260812(수), data = 20260811(화)
    const result = calcDataFreshness('20260811', kst(2026, 8, 12, 17, 0))
    expect(result!.color).toBe('orange')
    expect(result!.lag).toBe(1)
    expect(result!.label).toContain('1일 전')
  })

  it('2 거래일 뒤처짐 → red', () => {
    // expected = 20260812(수), data = 20260810(월)
    const result = calcDataFreshness('20260810', kst(2026, 8, 12, 17, 0))
    expect(result!.color).toBe('red')
    expect(result!.lag).toBe(2)
    expect(result!.label).toContain('2일 전')
  })

  it('금요일 데이터, 주말 시각 → green', () => {
    // 2026-08-08(토) 12:00 KST → expected = 20260807
    const result = calcDataFreshness('20260807', kst(2026, 8, 8, 12, 0))
    expect(result!.color).toBe('green')
    expect(result!.lag).toBe(0)
  })

  it('금요일 데이터, 월요일 16:30 이전 → green (월요일 아직 최신 아님)', () => {
    // 2026-08-10(월) 09:00 → expected = 20260807
    const result = calcDataFreshness('20260807', kst(2026, 8, 10, 9, 0))
    expect(result!.color).toBe('green')
    expect(result!.lag).toBe(0)
  })

  it('금요일 데이터, 월요일 17:00 → orange (1거래일 지연)', () => {
    // 2026-08-10(월) 17:00 → expected = 20260810
    const result = calcDataFreshness('20260807', kst(2026, 8, 10, 17, 0))
    expect(result!.color).toBe('orange')
    expect(result!.lag).toBe(1)
  })

  it('3일 이상 → red, 레이블에 일수 포함', () => {
    // expected = 20260812, data = 20260807 → 3거래일(월/화/수)
    const result = calcDataFreshness('20260807', kst(2026, 8, 12, 17, 0))
    expect(result!.color).toBe('red')
    expect(result!.lag).toBe(3)
    expect(result!.label).toContain('3일 전')
  })
})
