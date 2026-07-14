import { Link } from 'react-router-dom'

// ── 섹션 컴포넌트 ──────────────────────────────────────────────

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section id={id} className="bg-gray-900 border border-gray-800 rounded-2xl p-6 space-y-4 scroll-mt-16">
      <h2 className="text-white font-bold text-base">{title}</h2>
      <div className="space-y-3 text-gray-300 text-sm leading-relaxed">
        {children}
      </div>
    </section>
  )
}

function Q({ q, children }: { q: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <p className="text-white font-medium">{q}</p>
      <div className="text-gray-400 text-sm leading-relaxed pl-3 border-l border-gray-700 space-y-2">
        {children}
      </div>
    </div>
  )
}

function Tag({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <span className={`inline-block text-xs px-2 py-0.5 rounded font-medium ${color}`}>
      {children}
    </span>
  )
}

function Table({ headers, rows }: { headers: string[]; rows: (string | React.ReactNode)[][] }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-gray-800">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-800 text-gray-400 text-xs">
            {headers.map(h => <th key={h} className="text-left px-4 py-2.5 font-medium">{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-gray-800/50 last:border-0">
              {row.map((cell, j) => (
                <td key={j} className="px-4 py-2.5 text-gray-300">{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── 목차 ──────────────────────────────────────────────────────

const TOC = [
  { id: 'market-mode',    label: '시장 국면 필터' },
  { id: 'cooldown',       label: '쿨다운 필터' },
  { id: 'disclosure',     label: '공시 필터' },
  { id: 'paper-trading',  label: '모의투자 성과 추적' },
  { id: 'model',          label: 'AI 모델 & 업데이트' },
  { id: 'faq',            label: '자주 묻는 질문' },
]

// ── 메인 페이지 ───────────────────────────────────────────────

export default function HelpPage() {
  return (
    <div className="min-h-screen bg-slate-950">
      {/* 헤더 */}
      <div className="sticky top-0 z-30 bg-slate-950/90 backdrop-blur border-b border-gray-800/60">
        <div className="max-w-3xl mx-auto px-4 h-12 flex items-center gap-3">
          <Link to="/" className="text-gray-400 hover:text-white transition-colors p-1 -ml-1">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
            </svg>
          </Link>
          <h1 className="text-white font-bold text-sm">사용 안내</h1>
          <div className="h-4 w-px bg-gray-700" />
          <span className="text-gray-400 text-xs">AI 주식 추천 & 모의투자 시스템</span>
        </div>
      </div>

      <div className="max-w-3xl mx-auto px-4 py-8 space-y-6">
        {/* 목차 */}
        <nav className="bg-gray-900 border border-gray-800 rounded-2xl p-5">
          <p className="text-gray-400 text-xs font-medium mb-3 uppercase tracking-wider">목차</p>
          <div className="grid grid-cols-2 gap-1.5">
            {TOC.map(item => (
              <a
                key={item.id}
                href={`#${item.id}`}
                className="text-indigo-400 hover:text-indigo-300 text-sm transition-colors py-0.5"
              >
                {item.label}
              </a>
            ))}
          </div>
        </nav>

        {/* 1. 시장 국면 필터 */}
        <Section id="market-mode" title="시장 국면 필터 — 왜 종목이 적게 나오나요?">
          <p>
            AI 추천 페이지는 단순히 확률 높은 종목을 나열하는 게 아니라,
            <strong className="text-white"> 현재 시장 분위기에 맞게 추천 강도를 자동 조절</strong>합니다.
            최근 20거래일 유동성 종목의 중앙값 수익률로 시장 추세를 계산합니다.
          </p>

          <Table
            headers={['국면', '판단 기준', '확률 임계값', '최대 종목 수', '의미']}
            rows={[
              [
                <Tag color="bg-emerald-500/10 text-emerald-400">강세장 bull</Tag>,
                '최근 수익률 상위',
                '제한 없음',
                'top_n 그대로',
                '공격 모드 — 정상 추천',
              ],
              [
                <Tag color="bg-yellow-500/10 text-yellow-400">횡보장 sideways</Tag>,
                '중립 구간',
                '60% 이상만',
                '최대 15개',
                '주의 모드 — 강한 신호만',
              ],
              [
                <Tag color="bg-red-500/10 text-red-400">약세장 bear</Tag>,
                '최근 수익률 하위',
                '70% 이상만',
                '최대 5개',
                '방어 모드 — 최고 확신만',
              ],
            ]}
          />

          <Q q="횡보장에서 추천 종목이 0개인 이유는?">
            <p>
              횡보장에서는 <strong className="text-white">확률 60% 이상인 종목만</strong> 표시합니다.
              모델이 계산한 확률이 모두 60% 미만이면 추천 리스트가 비어 보입니다.
              이는 버그가 아니라 <strong className="text-white">잘못된 신호를 걸러내는 의도된 동작</strong>입니다.
            </p>
            <p>
              횡보장에서는 어떤 종목을 사도 수익이 불확실합니다.
              이 필터 덕분에 "애매한 신호"를 추천받아 손실 보는 상황을 예방합니다.
            </p>
          </Q>

          <Q q="약세장에서 확률 70%를 넘는 종목이 없으면 어떻게 하나요?">
            <p>
              추천 없음이 정답입니다. 약세장에서는 현금을 보유하거나,
              이미 보유한 종목의 손절 기준을 미리 설정해두는 것이 더 중요합니다.
              모델의 정확도 자체가 시장 국면에 따라 크게 달라집니다.
            </p>
          </Q>
        </Section>

        {/* 2. 쿨다운 필터 */}
        <Section id="cooldown" title="쿨다운 필터 — 최근 추천 종목은 왜 제외되나요?">
          <p>
            최근 <strong className="text-white">5거래일 이내에 추천된 종목</strong>은 쿨다운 기간으로 분류되어
            재추천에서 제외됩니다. 추천 페이지 하단 "쿨다운 제외" 항목에 표시됩니다.
          </p>

          <Q q="왜 최근 추천 종목을 다시 추천하지 않나요?">
            <p>두 가지 이유입니다.</p>
            <p>
              <strong className="text-white">① 중복 매수 방지:</strong> 같은 종목을 반복 추천하면
              실질적으로 같은 정보를 계속 보여주는 것입니다.
              신선한 기회를 발굴하는 게 더 유용합니다.
            </p>
            <p>
              <strong className="text-white">② 과집중 방지:</strong> 특정 종목에 자금이 몰리는 것을 막습니다.
              분산 투자 관점에서 이미 포지션을 가진 종목을 또 추천하는 건 의미가 없습니다.
            </p>
          </Q>

          <Q q="쿨다운이 풀리면 자동으로 다시 추천되나요?">
            <p>
              네. 5거래일이 지나면 쿨다운이 해제되고, 모델 확률이 높으면 다시 추천 리스트에 올라올 수 있습니다.
              쿨다운 이력은 서버가 재시작돼도 파일로 유지됩니다.
            </p>
          </Q>
        </Section>

        {/* 3. 공시 필터 */}
        <Section id="disclosure" title="공시 필터 — 리스크 종목 자동 제외">
          <p>
            DART 공시 기반으로 최근 14일 이내에 다음 키워드를 포함한 공시가 있는 종목은
            추천에서 자동 제외됩니다.
          </p>

          <div className="flex flex-wrap gap-2">
            {['유상증자', '주식분할', '액면변경', '거래정지', '상장폐지', '횡령', '배임', '감사의견', '권리락'].map(kw => (
              <Tag key={kw} color="bg-red-500/10 text-red-400">{kw}</Tag>
            ))}
          </div>

          <Q q="제외된 종목은 어디서 확인하나요?">
            <p>
              AI 추천 페이지 하단 <strong className="text-white">"공시 리스크 제외"</strong> 섹션에서
              어떤 공시 때문에 제외됐는지 이유와 함께 확인할 수 있습니다.
            </p>
          </Q>
        </Section>

        {/* 4. 모의투자 성과 */}
        <Section id="paper-trading" title="모의투자 성과 추적 — 무엇이고 어떻게 활용하나요?">
          <p>
            AI가 추천한 종목을 실제로 매수했다면 어떤 성과가 났을지 자동으로 추적합니다.
            실제 돈을 쓰지 않고, 추천일 종가에 매수해 <strong className="text-white">5거래일 후 종가에 자동 청산</strong>하는
            가상 거래 기록입니다.
          </p>

          <Q q="어떻게 기록되나요?">
            <p>
              매일 16:30 자동 파이프라인이 실행될 때:
            </p>
            <div className="space-y-1 text-xs font-mono bg-gray-800/60 rounded-lg p-3">
              <p>1. 서버 재시작 → 오늘 추천 top 10 자동 기록 (추천일 종가로 매수가 설정)</p>
              <p>2. 5거래일 경과 시 자동 청산 → 청산일 종가로 수익률 계산</p>
            </div>
          </Q>

          <Q q="승률과 누적 수익률은 어떻게 해석하나요?">
            <Table
              headers={['지표', '의미', '참고 기준']}
              rows={[
                ['승률', '청산 거래 중 수익(+)으로 끝난 비율', '50% 이상이면 양호'],
                ['누적 수익률', '모든 청산 거래의 수익률 합산', '분산 투자 가정, 복리 미반영'],
                ['평균 보유일', '청산까지 평균 거래일', '5거래일 고정 청산'],
                ['베스트/워스트', '가장 수익/손실이 컸던 단일 거래', '극단값 참고용'],
              ]}
            />
          </Q>

          <Q q="데이터가 없을 때 '—'로 표시되는 이유는?">
            <p>
              아직 5거래일이 경과한 거래가 없어서입니다.
              첫 추천일(2026.05.12) 기준으로 약 <strong className="text-white">2026.05.20 이후</strong>부터
              청산 데이터가 채워지기 시작합니다.
            </p>
          </Q>

          <Q q="모의투자 성과가 낮으면 AI를 믿지 말아야 하나요?">
            <p>
              단순히 성과 숫자만 보지 않는 것이 중요합니다. 다음을 함께 확인하세요:
            </p>
            <p>• 시장 국면: 약세장에서는 AI도 어렵습니다</p>
            <p>• 기간: 최소 30건 이상 쌓인 후 통계가 의미 있습니다</p>
            <p>• AUC: 모델 성능 지표 (0.63 이상이면 시장 평균 대비 우위)</p>
          </Q>
        </Section>

        {/* 5. 모델 & 업데이트 */}
        <Section id="model" title="AI 모델 & 업데이트 주기">
          <Q q="어떤 모델을 사용하나요?">
            <p>
              <strong className="text-white">LightGBM</strong> 분류 모델입니다.
              5거래일 내 최고가 +10% 또는 5일 종가 +5% 이상을 달성할 확률을 예측합니다.
              43개 피처를 사용합니다.
            </p>
            <Table
              headers={['피처 카테고리', '예시']}
              rows={[
                ['기술적 지표', 'RSI, MACD, 볼린저밴드, 이동평균 이격도'],
                ['거래량 지표', '거래대금 5일/20일 평균, 거래량 급등'],
                ['시장 상대 강도', '코스피/코스닥 대비 초과 수익률'],
                ['수급 지표', '외국인 보유비율 변화'],
                ['시장 국면', 'KOSPI 200일선 비율, 변동성, 등락 비율'],
                ['펀더멘털', 'PER, PBR'],
              ]}
            />
          </Q>

          <Q q="데이터와 모델은 언제 업데이트되나요?">
            <Table
              headers={['항목', '주기', '시각']}
              rows={[
                ['주가 데이터 수집', '매일 평일', '16:30 자동'],
                ['피처 업데이트', '매일 평일', '16:30 자동'],
                ['서버 재시작 (새 피처 반영)', '매일 평일', '16:30 자동'],
                ['모델 재학습', '수동 또는 필요 시', 'AI 추천 페이지 "재학습" 버튼'],
                ['모의투자 청산 처리', '매일 평일', '16:30 자동'],
              ]}
            />
          </Q>

          <Q q="기준일이 오늘 날짜가 아닌 이유는?">
            <p>
              장 마감(15:30) 후 데이터 확정까지 시간이 필요합니다.
              파이프라인이 16:30에 실행되므로, 오늘 데이터가 반영된 피처는
              <strong className="text-white"> 당일 16:30 이후</strong>부터 기준일이 오늘로 표시됩니다.
              평일 16:30 이전 접속 시 전날 기준일로 표시되는 것이 정상입니다.
            </p>
          </Q>
        </Section>

        {/* 6. FAQ */}
        <Section id="faq" title="자주 묻는 질문">
          <Q q="추천 종목을 그대로 사도 되나요?">
            <p>
              이 시스템은 <strong className="text-white">투자 참고용</strong>이며 실제 투자 결론은 본인이 내려야 합니다.
              SHAP 분석으로 추천 근거를 확인하고, 재무제표·뉴스와 함께 판단하세요.
              과거 성과가 미래 수익을 보장하지 않습니다.
            </p>
          </Q>

          <Q q="ETF, 레버리지 상품은 왜 추천 안 하나요?">
            <p>
              유니버스 필터에서 ETF, ETN, 레버리지, 인버스, 선물 상품은 자동 제외됩니다.
              개별 종목 분석 모델이라 지수 추종 상품에는 적합하지 않습니다.
            </p>
          </Q>

          <Q q="서버가 꺼져 있으면 어떻게 하나요?">
            <p>
              <code className="bg-gray-800 px-1.5 py-0.5 rounded text-xs text-gray-300">StockGame-Start.bat</code>을
              실행하거나, 터미널에서 아래 명령을 실행하세요:
            </p>
            <div className="bg-gray-800/60 rounded-lg p-3 font-mono text-xs text-gray-300">
              cd C:\01coding\stock-game\backend\server<br />
              ..\data\.venv\Scripts\python.exe -m uvicorn main:app --port 8001
            </div>
          </Q>

          <Q q="모의투자 기록을 초기화하고 싶어요.">
            <p>
              SQLite DB에서 직접 삭제할 수 있습니다:
            </p>
            <div className="bg-gray-800/60 rounded-lg p-3 font-mono text-xs text-gray-300">
              python -c "import sqlite3; conn=sqlite3.connect('backend/data/stocks.db'); conn.execute('DELETE FROM paper_trades'); conn.commit()"
            </div>
          </Q>

          <Q q="모델 AUC가 무엇인가요?">
            <p>
              AUC(Area Under Curve)는 모델이 실제 상승 종목과 하락 종목을 얼마나 잘 구분하는지 나타냅니다.
              0.5 = 랜덤 수준, 1.0 = 완벽한 예측.
              현재 모델 AUC는 약 <strong className="text-white">0.62~0.63</strong>으로,
              무작위 선택 대비 유의미하게 높지만 완벽하진 않습니다.
            </p>
          </Q>
        </Section>

        {/* 하단 링크 */}
        <div className="flex gap-3 flex-wrap">
          <Link
            to="/recommend"
            className="flex-1 min-w-fit bg-gray-900 border border-gray-800 hover:border-emerald-500/50 rounded-xl px-5 py-4 text-center transition-all"
          >
            <p className="text-emerald-400 font-medium text-sm">AI 추천 종목</p>
            <p className="text-gray-500 text-xs mt-0.5">오늘 추천 보기</p>
          </Link>
          <Link
            to="/paper-trading"
            className="flex-1 min-w-fit bg-gray-900 border border-gray-800 hover:border-purple-500/50 rounded-xl px-5 py-4 text-center transition-all"
          >
            <p className="text-purple-400 font-medium text-sm">모의투자 성과</p>
            <p className="text-gray-500 text-xs mt-0.5">누적 성과 확인</p>
          </Link>
          <Link
            to="/"
            className="flex-1 min-w-fit bg-gray-900 border border-gray-800 hover:border-gray-600 rounded-xl px-5 py-4 text-center transition-all"
          >
            <p className="text-gray-300 font-medium text-sm">모드 선택</p>
            <p className="text-gray-500 text-xs mt-0.5">홈으로</p>
          </Link>
        </div>
      </div>
    </div>
  )
}
