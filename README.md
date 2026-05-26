# 잔소리 코치 (Nagging Coach)

PC 활동을 지켜보다 텔레그램으로 먼저 말 걸어 잔소리·격려·회고까지 챙기는
**능동형 개인 AI 에이전트**. 미루기 습관을 가진 사람이 의지력 대신 *시스템* 의
힘으로 작은 행동을 쌓아가도록 설계됐다.

명령어로 조작하는 챗봇이 아니다. 평소엔 친구처럼 떠들면서 정보를 모으고,
PC 딴짓이 잡히면 잔소리하고, 무시당하면 톤을 조절하고, 캐파 안 맞으면 분량을
줄이고, 일요일 저녁이면 한 주를 돌아본다.

> **타겟**: 미루기 습관 / 시작 장벽이 큰 사람.

---

## 한눈에 — 뭐가 다른가

| | 보통의 to-do 앱 / 챗봇 | 잔소리 코치 |
|---|---|---|
| 개입 방식 | 사용자가 열어야 동작 | **먼저** 말 걸고 PC를 보고 잔소리 |
| 톤 | 고정 | 사용자 발화 신호로 자동 조절 (gentle/balanced/strict) |
| 실패 대응 | 책망 / 무한 알림 | 캐파 점검·분량 축소·자기연민 톤 |
| 목표 분해 | 사용자가 직접 | 코치가 5~15분짜리 행동으로 자동 분해 |
| 시작 장벽 | 의지력 의존 | Pomodoro·if-then plan 자동 권유 |
| 회고 | 없음 | 매주 일요일 21시 자동 시작 |
| 동작 환경 | 단일 인스턴스 | **클라우드 24/7 봇 + 로컬 PC 감시 분리** |

---

## 아키텍처 — 클라우드/로컬 분리

같은 봇 토큰으로 두 인스턴스가 텔레그램을 동시에 폴링하면 409 충돌. 그래서
**텔레그램·AI 는 Railway 클라우드**, **PC 감시는 로컬 위성** 으로 분리.

```
┌──────────────────────────┐                ┌──────────────────────────┐
│  로컬 PC (위성)          │  POST /trigger │  Railway 봇 (24/7)       │
│  trigger_satellite.py    │ ─────────────▶ │  app.py                  │
│  - Tracker (PC 감시)     │  Bearer 인증   │  - 텔레그램 폴링         │
│  - 트리거만 발사         │                │  - HTTP /trigger 수신    │
│                          │                │  - AI 잔소리 생성·발송  │
└──────────────────────────┘                └──────────────────────────┘
        ↑                                              ↓
        PC 켜져 있을 때만                    텔레그램 (사용자) ◀──┐
                                                                  │
                                              일정·알람·회고 ─────┘
```

- 로컬 위성이 살아 있으면 PC 감시 + 텔레그램 대화 다 됨
- PC 꺼져 있어도 Railway 봇이 24/7 폴링 — 일정 리마인드·알람·프로액티브·
  주간 회고는 계속 작동
- 위성·클라우드 양쪽이 자체 쿨다운 (위성 60초 / 클라우드 10분, trigger_value별)
  으로 잔소리 폭주 차단

---

## 닫힌 루프 — perception → reasoning → action → memory

| 단계 | 담당 | 방식 |
|---|---|---|
| 지각 (Perception) | `tracker.py` + 상태 추출 | 결정론적 PC 센서 + focused LLM 추출 |
| 추론 (Reasoning) | 에이전트 루프 (`ai_engine.py`) | LLM 이 스스로 도구 선택 (ReAct 방식) |
| 행동 (Action) | 도구 (`agent_tools.py`) | 11개 도구로 캘린더·알람·습관·plan·mood 등 |
| 기억 (Memory) | `store.py` | 목표·습관·프로필·core_values·if-then·daily_stats 영속화 |

**설계 원칙**: 센싱은 결정론적 코드, 추론·판단은 LLM. 새 능력은
`agent_tools.py` 에 도구를 추가하면 에이전트가 알아서 쓴다.

---

## 주요 기능

### 대화 & 학습
- 친구 톤으로 대화하며 사용자의 **성격·취미·직업·약점·core_values** (건강·성장·
  가족 같은 추상 가치) 자동 추출
- 잡담·하소연이면 코치 모드 끄고 들어주기 — 분위기 자동 감지

### PC 딴짓 감지 (8종)
도파민 좀비 / 산만함·널뛰기 / 가짜 일하기 / 과몰입 / 능동 스크롤 /
늦은 밤 / 휴식 없는 과로 / 개인 약점 앱
- "산만함" 은 한 번 더 LLM 판독으로 *진짜 딴짓인지* 확인 후 발사

### 톤 정책 (`nag_policy`)
- **gentle / balanced / strict** — 사용자 발화에서 자동 추출
  ("살살 해줘" → gentle / "더 세게" → strict)
- 첫 잔소리 직후 한 번 옵션 안내 → 사용자가 체감 후 조정 가능
- gentle: 압박 뺀 부드러운 톤 / strict: 박력 ↑ (인격 모독 금지)

### 목표 분해 — 행동 트리
- 큰 단발 과제("코딩 1시간") → `register_today_goal_with_steps` 로 3~5개
  '시동 거는 행동' 자동 분해
- 시스템이 sub_step 진척 추적 → 매번 일관된 다음 행동 제시
- 지난주 완료율 40% 미만 시 자동으로 분량 축소 권장 (캐파 가이드)

### 시작 장벽 도구
- `start_focus_session(분, 작업)` — Pomodoro 식 타이머. 끝나면 자동으로
  "어땠어? 더 갈래/쉴래/끝낼래?" 물음
- `register_implementation_intention(situation, response)` — WOOP/MCII 의
  if-then plan 저장. 매칭 상황에서 코치가 가볍게 상기

### 자기 격려 시스템
- `log_mood(rating 1-5)` — 사용자가 기분 꺼낼 때 가볍게 기록
- `get_weekly_insight` — 지난 7일 vs 그 전 7일 비교 (목표 완료·습관·트리거·
  mood). 코치가 칭찬·격려에 구체 숫자로 뒷받침

### 습관 시스템
- `register_habit` — 3~4단계 점진적 레벨 (예: 독서 10→20→30분)
- 한 레벨에서 3회 성공 시 자동 레벨업, streak 추적

### 캘린더·알람
- Google 캘린더 통합 (대화로 등록, 15분 전 리마인드)
- 시간 알람 (1회 / 매일 반복)

### 사진 완료 검증
- "다 했어" 만으론 안 쳐줌 — 사진 인증 요구
- Gemini Vision 이 사진 판독해 인정 여부 결정

---

## 자동 발사 시나리오 (사용자 입력 없이)

| 트리거 | 동작 | 빈도 |
|---|---|---|
| PC 딴짓 (8종) | 잔소리 발송 | 트리거 조건 + 정책별 쿨다운 |
| 1시간 침묵 | 프로액티브 안부 / 미룬 목표 챙김 | 1시간 |
| 24h 무응답 + 잔소리 5회 무시 | **Overload checkin** — "톤이 빡센가? 목표가 무겁나?" | 3일 쿨다운 |
| 일요일 21시 | **주간 회고** — 7일 데이터로 회고 대화 시작 | 주 1회 |
| 캘린더 일정 15분 전 | 리마인드 | 일정별 |
| Focus session 종료 | "어땠어?" 자동 묻기 | 세션별 |
| 매일 새벽 4시 | 컨테이너 자체 재시작 (안정성) | 일 1회 |
| 텔레그램 발송 누적 실패 20회 | 자체 재시작 | 자동 복구 |

---

## 안전 가드

사용자가 자해·자살 의도를 직접 표현하는 발화가 감지되면, 잔소리·도구 호출을
멈추고 공인 위기 채널 (**1393 자살예방상담** · **1577-0199 정신건강위기상담**)
을 안내한다. 시스템이 사용자 상태를 임의로 분류·라벨링하지 않는다.

---

## 참고한 연구

코드 곳곳에 차용한 행동과학·심리학 기법들:

| 기능 | 참고 연구 |
|---|---|
| Implementation Intention (WOOP) | Meta-analysis 24 trials, g=0.34 (Cohen's d, small-to-medium) |
| Behavioral activation + mood 트래킹 | Moodivate RCT (2025) — 1차 진료 환자 증상 개선 효과 |
| Pomodoro / time-boxing | task completion 25-30%↑ (focus·attention 어려움 케이스) |
| Self-compassion → procrastination ↓ | Wohl 2010 — 자기 용서가 다음 시도 회복 가속 |
| Procrastination = emotion regulation | Pychyl & Sirois — 압박 ↑ 가 미루기 강화 (회피 reinforcement) |
| AI 챗봇 효과 | Therabot RCT (Dartmouth, 2025) |
| AI 명시 / 위기 채널 안내 | CA SB243 / NY S3008 (2025) 트렌드 부합 |

---

## 도구 목록 (모델이 호출하는 행동 11종)

`add_calendar_event` · `list_calendar_events` · `set_alarm` · `list_alarms` ·
`cancel_alarm` · `register_habit` · `register_today_goal_with_steps` ·
`advance_today_goal_step` · `get_weekly_insight` · `log_mood` ·
`start_focus_session` · `register_implementation_intention`

---

## 실행

### A. Railway 클라우드 봇 (24/7 텔레그램 + AI)

이미 배포되어 있음: `https://naggingcoach-production.up.railway.app`
재배포는 `git push origin main` 만 하면 자동.

환경변수 (Railway Variables):
```
GEMINI_API_KEY        Gemini API 키
TELEGRAM_BOT_TOKEN    @BotFather 에서 발급
TRIGGER_SECRET        위성과 공유하는 Bearer 토큰
STATE_PATH            /data/state.json (영속 볼륨)
ENABLE_PC_TRACKER     false (컨테이너엔 데스크톱 X)
```

### B. 로컬 PC 위성 (PC 감시 → Railway 로 트리거)

```
python trigger_satellite.py
```

또는 Windows 자동 시작 (이미 등록됨):
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\nagging_coach_satellite.vbs`
— 로그온 시 콘솔 없이 백그라운드, 출력은 `satellite.log` 누적.

로컬 `.env`:
```
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
NAGGING_COACH_URL=https://naggingcoach-production.up.railway.app
TRIGGER_SECRET=...   # Railway 와 같은 값
ENABLE_PC_TRACKER=false  # 위성에서는 안 쓰임, app.py 가 봤을 때 충돌 방지용
```

> ⚠️ **로컬에서 `python app.py` 를 띄우지 말 것** — Railway 봇과 같은 토큰
> 으로 두 인스턴스가 폴링하면 409 충돌. 로컬은 위성만.

### C. (선택) Google 캘린더 연동

1. Google Cloud Console 에서 OAuth 데스크톱 클라이언트 생성 → `credentials.json` 저장
2. `python calendar_setup.py` 1회 실행
3. 생략하면 캘린더 기능만 꺼지고 나머지는 정상 동작

### 사용

텔레그램에서 봇한테 `/start` 보내면 연결됨. 그 후엔 그냥 대화하면 코치가
알아서 챙김.

- `/start` — 봇 연결 (최초 1회)
- `/reset` — 대화 기록·목표·습관 초기화 (`nag_policy` 같은 설정·누적 데이터는 유지)
- 사진 전송 — 할 일 인증샷

---

## 파일 구조

| 파일 | 역할 |
|---|---|
| `app.py` | 헤드리스 오케스트레이터 — 텔레그램 폴링·프로액티브·리마인더·알람·주간 회고·HTTP 서버 7개 스레드 |
| `trigger_satellite.py` | 로컬 PC 위성 — Tracker 만 띄우고 트리거를 Railway 로 POST |
| `ai_engine.py` | `CoachAgent` — 에이전트 루프 · 상태 추출 · 비전 판독 · WOOP/회고/overload checkin |
| `agent_tools.py` | 도구 레지스트리 (11종) |
| `tracker.py` | PC 활동 감시 + 8종 딴짓 트리거 + 화면 라벨 sanitize |
| `store.py` | 상태 영속화 — 목표·습관·프로필·core_values·if-then plan·daily_stats·mood |
| `calendar_client.py` / `calendar_setup.py` | Google 캘린더 클라이언트 · OAuth 인증 |
| `telegram_client.py` | 텔레그램 Bot API 클라이언트 (long-polling) |
| `smoke_test.py` | 핵심 기능 라이브 검증 스크립트 |
| `Dockerfile` / `requirements-docker.txt` | Railway 클라우드 봇 빌드 |
| `nagging_coach_satellite.vbs` | Windows 시작프로그램 — 위성 자동 실행 |

---

## 기술 스택

- Python 3.10+ (위성), 3.12-slim (Railway 컨테이너)
- Gemini (`gemini-3-flash-preview`, 멀티모달) — `google-genai` SDK
- 텔레그램 Bot API (long-polling)
- `pygetwindow` · `pynput` — PC 활동 감시 (위성 전용)
- Google Calendar API (OAuth, 선택)
- Railway (클라우드 호스팅, 영속 볼륨)

---

## 한계 / Future work

- 모바일(스마트폰) 활동 감시 없음 — PC 한정
- mood 트래킹은 사용자 자가 보고에만 의존 (능동 권유는 코치 판단)
- 멀티 사용자 지원 X — 1인 1봇 구조 (chat_id 1개만 등록)

---

## 비고

`.env` · `credentials.json` · `token.json` · `state.json` · `satellite.log` 는
비밀·개인정보이므로 저장소에 커밋하지 않는다 (`.gitignore` 등록됨).
