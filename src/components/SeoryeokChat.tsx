import { useEffect, useRef } from 'react'

export interface ChatMessage {
  from: 'boss' | 'deputy' | 'spy'
  text: string
  ts: number
}

const NAMES = { boss: '팀장', deputy: '부팀장', spy: '감시자' }
const COLORS = { boss: 'text-red-400', deputy: 'text-orange-400', spy: 'text-gray-400' }
const AVATARS = { boss: '👴', deputy: '😈', spy: '👁' }

interface Props {
  messages: ChatMessage[]
  compact?: boolean
}

export default function SeoryeokChat({ messages, compact = false }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages.length])

  if (messages.length === 0) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 text-center text-gray-600 text-xs">
        세력 감시 대기 중...
      </div>
    )
  }

  return (
    <div className={`bg-gray-950 border border-gray-800 rounded-xl flex flex-col ${compact ? 'max-h-64' : 'max-h-96'}`}>
      <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-800 shrink-0">
        <div className="flex gap-1">
          <span className="w-2.5 h-2.5 rounded-full bg-red-500" />
          <span className="w-2.5 h-2.5 rounded-full bg-yellow-500" />
          <span className="w-2.5 h-2.5 rounded-full bg-green-500" />
        </div>
        <span className="text-gray-500 text-xs font-medium ml-1">세력 작전방 🔒</span>
        <span className="ml-auto text-gray-700 text-xs">{messages.length}개</span>
      </div>
      <div className="overflow-y-auto flex-1 px-3 py-2 space-y-2">
        {messages.map((msg, i) => (
          <div key={i} className="flex items-start gap-2">
            <span className="text-base shrink-0 mt-0.5">{AVATARS[msg.from]}</span>
            <div className="flex-1 min-w-0">
              <span className={`text-xs font-semibold ${COLORS[msg.from]}`}>{NAMES[msg.from]}</span>
              <p className="text-gray-200 text-xs leading-relaxed mt-0.5 whitespace-pre-wrap break-words">{msg.text}</p>
            </div>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

// ── 유틸 ─────────────────────────────────────────────────────────

function pick<T>(arr: T[], seed: number): T {
  return arr[Math.abs(seed) % arr.length]
}

function fmt(n: number) { return n.toLocaleString() }
function pct(n: number) { return `${n >= 0 ? '+' : ''}${n.toFixed(1)}%` }

// ── 매수 메시지 ───────────────────────────────────────────────────
// context:
//   seed       - 거래 인덱스 (메시지 선택 다양성)
//   buyCount   - 이전 매수 횟수 (0이면 첫 진입)
//   sellCount  - 이전 매도 횟수 (>0이면 재매수)
//   avgHeld    - 현재 보유 평균단가 (undefined면 신규)
//   priceVsAvg - 매수가 vs 평균단가 차이 % (물타기 여부)

export function makeBuyMessages(
  price: number,
  qty: number,
  seed = 0,
  buyCount = 0,
  sellCount = 0,
  avgHeld?: number,
): ChatMessage[] {
  const ts = Date.now()
  const kr = fmt(price)
  const q = qty

  // 재매수 (이미 팔았다가 다시 사는 경우)
  if (sellCount > 0 && buyCount === 0) {
    return pick([
      [
        { from: 'spy',    text: `팔았다가 다시 들어옵니다\n${kr}원 ${q}주`, ts },
        { from: 'deputy', text: '못 참고 재매수ㅋ', ts: ts + 100 },
        { from: 'boss',   text: '그럴 줄 알았어. 반갑다', ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `재진입 확인. ${kr}원 ${q}주`, ts },
        { from: 'boss',   text: '이 개미 중독됐네', ts: ts + 100 },
      ],
      [
        { from: 'deputy', text: `또 왔습니다. ${kr}원`, ts },
        { from: 'spy',    text: '손 못 떼는 타입', ts: ts + 100 },
        { from: 'boss',   text: '환영해', ts: ts + 200 },
      ],
    ] as ChatMessage[][], seed)
  }

  // 물타기 (이미 보유 중에 추가 매수, 평단보다 낮은 가격)
  const isAverageDown = avgHeld !== undefined && price < avgHeld * 0.98
  if (buyCount > 0 && isAverageDown) {
    const diffPct = avgHeld ? ((price - avgHeld) / avgHeld * 100) : 0
    return pick([
      [
        { from: 'spy',    text: `물타기. ${kr}원 ${q}주\n평단 대비 ${diffPct.toFixed(1)}%`, ts },
        { from: 'deputy', text: '개미가 물을 탑니다', ts: ts + 100 },
        { from: 'boss',   text: '더 눌러줘. 계속 먹히잖아', ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `${kr}원 추가 매수. ${buyCount + 1}번째`, ts },
        { from: 'boss',   text: '물타기 패턴이네\n계속 눌러', ts: ts + 100 },
        { from: 'deputy', text: '네 팀장님', ts: ts + 200 },
      ],
      [
        { from: 'deputy', text: `또 탑니다. ${kr}원 ${q}주`, ts },
        { from: 'spy',    text: `평단 ${fmt(avgHeld ?? 0)}원에서 물 탔습니다`, ts: ts + 100 },
        { from: 'boss',   text: '고마워. 더 눌러줄게', ts: ts + 200 },
      ],
    ] as ChatMessage[][], seed)
  }

  // 추가 매수 (이미 보유 중, 평단 근처에서 추가)
  if (buyCount > 0) {
    return pick([
      [
        { from: 'spy',    text: `추가 매수 ${kr}원 ${q}주\n총 ${buyCount + 1}번째 진입`, ts },
        { from: 'deputy', text: '자꾸 들어오네', ts: ts + 100 },
        { from: 'boss',   text: '그래 더 담아라', ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `${kr}원 ${q}주 추가`, ts },
        { from: 'boss',   text: '욕심 있는 개미네', ts: ts + 100 },
      ],
      [
        { from: 'deputy', text: `또 삽니다 ${kr}원`, ts },
        { from: 'spy',    text: '계속 담고 있습니다', ts: ts + 100 },
        { from: 'boss',   text: '...좋아', ts: ts + 200 },
      ],
    ] as ChatMessage[][], seed)
  }

  // 첫 진입 (신규 매수)
  const bigQty = qty >= 100
  if (bigQty) {
    return pick([
      [
        { from: 'spy',    text: `큰 손 개미 포착\n${kr}원 ${q}주 진입`, ts },
        { from: 'boss',   text: '오. 제법인데\n흔들어줘', ts: ts + 100 },
        { from: 'deputy', text: '네', ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `${q}주 대량 매수\n${kr}원`, ts },
        { from: 'boss',   text: '이 개미 배짱 있네', ts: ts + 100 },
        { from: 'deputy', text: '재미있겠는데요', ts: ts + 200 },
      ],
    ] as ChatMessage[][], seed)
  }

  return pick([
    [
      { from: 'spy',    text: `개미 포착\n${kr}원 ${q}주 매수`, ts },
      { from: 'boss',   text: '흔들어', ts: ts + 100 },
    ],
    [
      { from: 'spy',    text: `신규 진입 확인\n${kr}원 ${q}주`, ts },
      { from: 'boss',   text: '반갑다 개미야', ts: ts + 100 },
      { from: 'deputy', text: '잘 왔어요 ^^', ts: ts + 200 },
    ],
    [
      { from: 'deputy', text: `${kr}원 ${q}주 들어왔습니다`, ts },
      { from: 'boss',   text: '이제 시작이야', ts: ts + 100 },
    ],
    [
      { from: 'spy',    text: `${kr}원 매수. ${q}주`, ts },
      { from: 'deputy', text: '눌러드릴까요?', ts: ts + 100 },
      { from: 'boss',   text: '아직. 지켜봐', ts: ts + 200 },
    ],
    [
      { from: 'spy',    text: `개미 진입\n${kr}원짜리 ${q}주`, ts },
      { from: 'boss',   text: '잘 왔어', ts: ts + 100 },
    ],
  ] as ChatMessage[][], seed)
}

// ── 매도 메시지 ───────────────────────────────────────────────────
// context:
//   seed        - 거래 인덱스
//   sellCount   - 이번이 몇 번째 매도인지
//   holdDays    - 보유 일수 (짧으면 단타, 길면 장기)
//   isPartial   - 일부 매도 여부 (남은 수량 있으면 true)

export function makeSellMessages(
  buyPrice: number,
  sellPrice: number,
  nextDayChangePct: number | null,
  seed = 0,
  _sellCount = 0,
  holdDays = 0,
  isPartial = false,
): ChatMessage[] {
  const ts = Date.now()
  const pnlPct = (sellPrice - buyPrice) / buyPrice * 100
  const kr = fmt(sellPrice)
  const p = pct(pnlPct)
  const shortHold = holdDays <= 3
  const longHold  = holdDays >= 30

  // ── 대박 익절 (≥30%)
  if (pnlPct >= 30) {
    return pick([
      [
        { from: 'spy',    text: `${kr}원 매도\n${p} 챙겨갔습니다`, ts },
        { from: 'deputy', text: '팀장님...', ts: ts + 100 },
        { from: 'boss',   text: '다음 판 준비해', ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `${p} 먹고 나갔습니다`, ts },
        { from: 'boss',   text: '이 개미 뭐가 다른 거야', ts: ts + 100 },
        { from: 'deputy', text: '죄송합니다 팀장님...', ts: ts + 200 },
      ],
      [
        { from: 'deputy', text: `${kr}원 ${p}...`, ts },
        { from: 'boss',   text: '작전 다시 짜', ts: ts + 100 },
        { from: 'spy',    text: '네', ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `${p} 수익 실현\n${kr}원에 탈출`, ts },
        { from: 'boss',   text: '운인지 실력인지', ts: ts + 100 },
        { from: 'deputy', text: '아무튼 짜증나네요', ts: ts + 200 },
      ],
    ] as ChatMessage[][], seed)
  }

  // ── 좋은 익절 (10~30%)
  if (pnlPct >= 10) {
    const nextMsg = nextDayChangePct !== null && nextDayChangePct < -2
      ? `내일 ${nextDayChangePct.toFixed(1)}% 줘\n아프게`
      : longHold ? '오래 버텼네. 더 눌러' : '올려. 더 올려'

    return pick([
      [
        { from: 'spy',    text: `${kr}원 매도`, ts },
        { from: 'deputy', text: `${p} 뒤통수 맞았습니다`, ts: ts + 100 },
        { from: 'boss',   text: nextMsg, ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `${p} 익절\n${kr}원`, ts },
        { from: 'deputy', text: longHold ? `${holdDays}일이나 버텼네요` : '아까운데...', ts: ts + 100 },
        { from: 'boss',   text: nextMsg, ts: ts + 200 },
      ],
      [
        { from: 'deputy', text: `${kr}원에 튀었습니다`, ts },
        { from: 'spy',    text: `${p} 먹고 나갔네요`, ts: ts + 100 },
        { from: 'boss',   text: '개미가 공부를 했나', ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `${p} 수익\n${shortHold ? '단타로 뺐습니다' : `${holdDays}일 보유 후 매도`}`, ts },
        { from: 'boss',   text: nextMsg, ts: ts + 100 },
      ],
    ] as ChatMessage[][], seed)
  }

  // ── 소폭 익절 (0~10%)
  if (pnlPct >= 0) {
    const partialNote = isPartial ? '일부만 팔았네' : '소심한 놈'
    return pick([
      [
        { from: 'spy',    text: `${kr}원 매도\n${p}`, ts },
        { from: 'deputy', text: nextDayChangePct !== null && nextDayChangePct < 0
            ? `내일 ${nextDayChangePct.toFixed(1)}% 예정 ㅋ`
            : '적게 먹고 나갔네', ts: ts + 100 },
      ],
      [
        { from: 'spy',    text: `${p} 익절`, ts },
        { from: 'boss',   text: partialNote, ts: ts + 100 },
      ],
      [
        { from: 'deputy', text: `겨우 ${p}만 먹고 나가네`, ts },
        { from: 'boss',   text: shortHold ? '단타쟁이네' : '그럼 더 올려줄게', ts: ts + 100 },
      ],
      [
        { from: 'spy',    text: `${kr}원 탈출\n${p}`, ts },
        { from: 'deputy', text: '더 줄 수도 있었는데', ts: ts + 100 },
        { from: 'boss',   text: '...다음엔 더 조여', ts: ts + 200 },
      ],
    ] as ChatMessage[][], seed)
  }

  // ── 소폭 손절 (-10%~0%)
  if (pnlPct >= -10) {
    return pick([
      [
        { from: 'deputy', text: `손절 ${Math.abs(pnlPct).toFixed(1)}% ㅋㅋ`, ts },
        { from: 'boss',   text: '올려. 더 아프게', ts: ts + 100 },
      ],
      [
        { from: 'spy',    text: `${Math.abs(pnlPct).toFixed(1)}% 물고 나갔습니다`, ts },
        { from: 'boss',   text: '이걸로 끝이야?', ts: ts + 100 },
        { from: 'deputy', text: '더 태워드릴까요?', ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `${kr}원 손절\n${p}`, ts },
        { from: 'deputy', text: shortHold ? '빨리 포기했네' : `${holdDays}일이나 버티다가 손절`, ts: ts + 100 },
        { from: 'boss',   text: '수고', ts: ts + 200 },
      ],
      [
        { from: 'deputy', text: `못 버티고 나갑니다. ${p}`, ts },
        { from: 'boss',   text: '역시. 다음엔 더 눌러줄게', ts: ts + 100 },
      ],
    ] as ChatMessage[][], seed)
  }

  // ── 대손절 (<-10%)
  return pick([
    [
      { from: 'deputy', text: `${p} 손절ㅋㅋ 잘 가라`, ts },
      { from: 'spy',    text: '저항 못 하고 나갔네요', ts: ts + 100 },
      { from: 'boss',   text: '다음 것도 준비해놔', ts: ts + 200 },
    ],
    [
      { from: 'spy',    text: `${p}... 크게 물렸습니다`, ts },
      { from: 'boss',   text: '뒤통수 제대로 쳤네', ts: ts + 100 },
      { from: 'deputy', text: '다음 개미 올게요', ts: ts + 200 },
    ],
    [
      { from: 'deputy', text: `${Math.abs(pnlPct).toFixed(1)}% 손실로 탈출`, ts },
      { from: 'boss',   text: longHold
          ? `${holdDays}일 버티다가 이렇게 끝나네`
          : '맞아도 싸다', ts: ts + 100 },
    ],
    [
      { from: 'spy',    text: `${kr}원 손절\n원금 ${Math.abs(pnlPct).toFixed(1)}% 손실`, ts },
      { from: 'boss',   text: '이 정도면 공황이네', ts: ts + 100 },
      { from: 'deputy', text: '재미있었습니다 ^^', ts: ts + 200 },
    ],
  ] as ChatMessage[][], seed)
}

// ── 최종 결과 메시지 ──────────────────────────────────────────────
// context:
//   tradeCount - 총 거래 횟수
//   buyCount   - 총 매수 횟수
//   sellCount  - 총 매도 횟수

export function makeCompleteMessages(
  totalPnlPct: number,
  tradeCount = 0,
  seed = 0,
  _sellCount = 0,
): ChatMessage[] {
  const ts = Date.now()
  const p = pct(totalPnlPct)
  const manyTrades = tradeCount >= 10

  if (totalPnlPct >= 30) {
    return pick([
      [
        { from: 'boss',   text: '이 개미 뭐야', ts },
        { from: 'deputy', text: `전체 ${p} 챙겼습니다`, ts: ts + 100 },
        { from: 'boss',   text: '작전 다시 짜', ts: ts + 200 },
      ],
      [
        { from: 'spy',    text: `종목 정리. ${p}`, ts },
        { from: 'boss',   text: '실력인지 운인지 모르겠네', ts: ts + 100 },
        { from: 'deputy', text: '다음엔 더 아프게 해드릴게요', ts: ts + 200 },
      ],
      [
        { from: 'deputy', text: `${p}... 팀장님 죄송합니다`, ts },
        { from: 'boss',   text: '이 개미는 다시 안 들어와', ts: ts + 100 },
        { from: 'spy',    text: '그럴 것 같습니다', ts: ts + 200 },
      ],
    ] as ChatMessage[][], seed)
  }

  if (totalPnlPct >= 10) {
    return pick([
      [
        { from: 'spy',    text: `종목 정리됨. 수익 ${p}`, ts },
        { from: 'boss',   text: '다음 개미 대기시켜', ts: ts + 100 },
      ],
      [
        { from: 'deputy', text: `${p} 챙겨갔습니다`, ts },
        { from: 'boss',   text: manyTrades ? '거래를 많이 했네. 운이야' : '운 좋았네', ts: ts + 100 },
      ],
      [
        { from: 'spy',    text: `${p} 수익으로 마무리`, ts },
        { from: 'boss',   text: '어쩌다 됐네', ts: ts + 100 },
        { from: 'deputy', text: '다음엔 안 그럴 거예요', ts: ts + 200 },
      ],
    ] as ChatMessage[][], seed)
  }

  if (totalPnlPct >= 0) {
    return pick([
      [
        { from: 'spy',    text: `종목 정리. ${p}`, ts },
        { from: 'boss',   text: '겨우 본전이네', ts: ts + 100 },
      ],
      [
        { from: 'deputy', text: `${p} 수익... 아깝네요`, ts },
        { from: 'boss',   text: '다음엔 더 조여', ts: ts + 100 },
      ],
      [
        { from: 'spy',    text: `${p} 마무리`, ts },
        { from: 'deputy', text: manyTrades ? '거래는 많이 했는데...' : '아슬아슬했네', ts: ts + 100 },
        { from: 'boss',   text: '그래도 수고', ts: ts + 200 },
      ],
    ] as ChatMessage[][], seed)
  }

  // 손실
  return pick([
    [
      { from: 'deputy', text: `손실 ${Math.abs(totalPnlPct).toFixed(1)}% ㅋㅋ`, ts },
      { from: 'boss',   text: '수고', ts: ts + 100 },
    ],
    [
      { from: 'spy',    text: `${p} 손실로 정리`, ts },
      { from: 'boss',   text: '그럴 줄 알았어', ts: ts + 100 },
      { from: 'deputy', text: '다음 판엔 더 세게 털죠', ts: ts + 200 },
    ],
    [
      { from: 'deputy', text: `${Math.abs(totalPnlPct).toFixed(1)}% 물렸습니다`, ts },
      { from: 'boss',   text: manyTrades ? '거래는 열심히 했는데 결과가...' : '처음부터 답 없었어', ts: ts + 100 },
    ],
  ] as ChatMessage[][], seed)
}
