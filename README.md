# 잔소리 코치 (Nagging Coach)

PC·폰 활동을 지켜보다 텔레그램으로 먼저 말 걸어 잔소리·격려·회고까지 챙기는
**능동형 개인 AI 에이전트**. 미루기 습관을 가진 사람이 의지력 대신 *시스템* 의
힘으로 작은 행동을 쌓아가도록 설계됐다.

명령어로 조작하는 챗봇이 아니다. 평소엔 친구처럼 떠들면서 정보를 모으고,
딴짓이 잡히면 잔소리하고, 무시당하면 톤을 조절하고, 캐파 안 맞으면 분량을
줄이고, 일요일 저녁이면 한 주를 돌아본다.

> **타겟**: 미루기 습관 / 시작 장벽이 큰 사람.

---

## 한눈에 — 뭐가 다른가

| | 보통의 to-do 앱 / 챗봇 | 잔소리 코치 |
|---|---|---|
| 개입 방식 | 사용자가 열어야 동작 | **먼저** 말 걸고 PC·폰을 보고 잔소리 |
| 톤 | 고정 | 사용자 발화 신호로 자동 조절 (gentle/balanced/strict) |
| 실패 대응 | 책망 / 무한 알림 | 캐파 점검·분량 축소·자기연민 톤 |
| 목표 분해 | 사용자가 직접 | 코치가 5~15분짜리 행동으로 자동 분해 |
| 시작 장벽 | 의지력 의존 | Pomodoro·if-then plan 자동 권유 |
| 회고 | 없음 | 매주 일요일 21시 자동 시작 |
| 자기학습 | 없음 | 자주 잡힌 앱을 약점 후보로 누적, 회고 때 확인 |
| 활동 감지 | 단일 환경 | **PC + 폰 두 위성**, 한 백엔드로 통합 |

---

## 아키텍처 — 분산 감지 / 중앙 두뇌 / 통합 인터페이스

```
┌──────────────────────────┐    ┌──────────────────────────┐
│  PC 위성                 │    │  폰 위성 (Android)        │
│  trigger_satellite.py    │    │  android-satellite/       │
│  - pygetwindow + pynput  │    │  - UsageEvents (연속 세션) │
│  - 8종 트리거 감지       │    │  - 4종 트리거 감지        │
│  - 트리거만 발사         │    │  - Foreground Service     │
└────────┬─────────────────┘    └────────┬──────────────────┘
         │ HTTP POST /trigger            │ HTTP POST /trigger
         │ Bearer 인증                   │ Bearer 인증
         └───────────────┬───────────────┘
                         ▼
            ┌──────────────────────────────────┐
            │  Railway 봇 (24/7, app.py)       │
            │  AI 모델: Gemini 3 Flash         │
            │    (gemini-3-flash-preview)      │
            │  - 텔레그램 폴링                 │
            │  - HTTP /trigger 수신·검증       │
            │  - AI 잔소리 생성 + 도구 호출    │
            │  - 일정·알람·습관·plan·mood      │
            │  - 사진 비전 판독 (멀티모달)     │
            │  - 주간 회고·overload checkin    │
            │  - 자기학습 weak_spots           │
            └──────────────┬───────────────────┘
                           │
                           │ 텔레그램 메시지
                           ▼
                    ┌──────────────┐
                    │  사용자       │
                    │  (텔레그램)   │
                    │  PC · 폰 · 웹 │
                    │  다 동기화    │
                    └──────────────┘
```

- 같은 봇 토큰으로 두 인스턴스 폴링하면 409 충돌 → **텔레그램·AI 는 클라우드, 감시는 위성**으로 분리
- PC 꺼져 있어도 Railway 봇은 24/7 — 일정 리마인드·알람·프로액티브·회고 계속
- 위성·클라우드 양쪽이 자체 쿨다운으로 폭주 방지 (PC 60초 · 폰 90초~5분 · 클라우드 trigger_value 별 10분)
- 새 클라이언트 추가는 *HTTP /trigger 로 POST 하는 코드만* 더하면 됨 (백엔드 수정 X)

---

## 닫힌 루프 — perception → reasoning → action → memory

| 단계 | 담당 | 방식 |
|---|---|---|
| 지각 (Perception) | `tracker.py` (PC) + `TrackerService.java` (폰) + EXTRACT | 결정론적 센서 + focused LLM 추출 (Gemini 3 Flash) |
| 추론 (Reasoning) | 에이전트 루프 (`ai_engine.py`) | Gemini 3 Flash 가 스스로 도구 선택 (ReAct 방식) |
| 행동 (Action) | 도구 레지스트리 (`agent_tools.py`) | 17종 도구 (캘린더 인증 시 19종) |
| 기억 (Memory) | `store.py` | state.json — 목표·습관·프로필·values·plans·daily_stats·mood·weak_spot 후보 |

**설계 원칙**: 센싱은 결정론적 코드, 추론·판단은 LLM (Gemini 3 Flash, 멀티모달).
새 능력은 `agent_tools.py` 에 도구를 추가하면 에이전트가 알아서 쓴다. 모델은
`GEMINI_MODEL` 환경변수로 교체 가능.

---

## 주요 기능

### 대화 & 학습
- 친구 톤으로 대화하며 **성격·취미·직업·약점·core_values** (건강·성장 같은 추상 가치) 자동 추출
- 잡담·하소연이면 코치 모드 끄고 들어주기 — 분위기 자동 감지
- **사용자 알아가기 적극성** — profile 빈 영역(취미·요즘 관심사·강점·스트레스원 등)을 LLM 컨텍스트에 실시간 hint 로 노출해 코치가 흐름 맞춰 한 가지씩 슬쩍 묻기. 한 turn 1 질문, 답 안 하면 다른 영역으로
- **chat debounce** — 사용자가 짧은 간격에 여러 메시지를 우다다 보내면 1.5초 안에 묶어서 한 번에 답장 (사람처럼 모아 읽고 응답)
- **자기학습 weak_spots**: 자주 잡힌 앱을 후보 카운터에 누적, 주간 회고 때 사용자한테 "이것도 약점으로 등록할까?" 확인

### 활동 감지 — 두 위성, 13종 트리거 + 1종 복합 룰

| | 트리거 | 감지 조건 |
|---|---|---|
| 🖥️ PC | 도파민 좀비 | 엔터테인먼트 앱 + 10분 무입력 (생산성 컨텍스트 가드) |
| 🖥️ PC | 산만함·널뛰기 | 5분 안 창 12번 변경 (+ LLM 한 번 더 판독) |
| 🖥️ PC | 가짜 일하기 | 업무 앱 + 5분 무입력 |
| 🖥️ PC | 과몰입 | 중독 앱 + 분당 60회+ 입력 + 30분 연속 (생산성 컨텍스트 가드) |
| 🖥️ PC | 능동 도파민 스크롤 | 엔터테인먼트 앱 활발 사용 15분 연속 (생산성 컨텍스트 가드) |
| 🖥️ PC | 늦은 밤 | 새벽 1~5시 (하루 1회) |
| 🖥️ PC | 휴식 없는 과로 | 5분 휴식 없이 2시간 연속 |
| 🖥️ PC | 개인 약점 앱 | 사용자 등록 약점 키워드 3분 연속 (한·영 변형 매핑, 백엔드 학습 결과 5분 fetch) |
| 🖥️ PC | **Pomodoro 휴식** | 5분 휴식 없이 50분 연속 — 한 세션 1회만, 짧은 환기 권유 |
| 📱 폰 | 능동 도파민 스크롤 | 인스타·유튜브 등 15분 *연속 세션* |
| 📱 폰 | 과몰입 | 카톡·디스코드 30분 *연속 세션* |
| 📱 폰 | 늦은 밤 | 새벽 1~5시 + 화면 ON |
| 📱 폰 | 휴식 없는 과로 | 화면 ON 2시간 누적 (5분 휴식 = 리셋) |
| 🔁 결합 | **회피 패턴** | PC 가짜 일하기·과로 ↔ 폰 도파민·과몰입 이 10분 내 잡히면 라벨 승격 |

**폰 위성 컨텍스트 (트리거 페이로드에 동봉)**:
- DND·충전·화면 상태 — 잔소리 가드. 사용자가 방해 금지 켜놨으면 경고성 트리거 보류. 새벽 + 충전 + 화면 OFF = 진짜 잠든 걸로 판단해 늦은 밤 트리거 skip
- 만보계 (오늘 걸음 수) + 헤드폰 연결 — 활동 컨텍스트. 좌식 시간 + mood 상관 분석에 활용
- 장소 카테고리 (집·회사·카페) — 사용자가 `/place 라벨` + 위치 메시지로 등록. *정확 좌표는 폰 안에서만* 매칭, 백엔드엔 라벨만 전송 (`GET /places` 로 폰이 좌표 fetch)

**감지 방식**: 결정론적 코드 (`pygetwindow` · `pynput` · `UsageEvents`),
1초~1분 폴링. *판단* 영역만 LLM (예: 산만함이 진짜 딴짓인지 `judge_distracted`).

**폰은 누적이 아닌 *연속 세션***: `ACTIVITY_RESUMED` ~ `ACTIVITY_PAUSED` 페어로
한 번에 N분 연속해야 발사. 알림 답장 같은 짧은 사용은 무시.

**이중 안전장치**: 위성 자체 쿨다운 (PC 60초 / 폰 90초~5분) + 클라우드
`trigger_value` 별 10분 쿨다운 — 잔소리 폭주를 시스템 차원에서 차단.

### 톤 정책 (`nag_policy`)
- **gentle / balanced / strict** — 명시적 요청에서만 *base* 가 변경 ("살살 해줘" → gentle / "더 세게" → strict)
- **컨디션 토로** ("지쳤어", "힘들어") 는 base 안 건드림 — *24시간 일시* gentle 적용 → 만료 시 base 로 자동 복귀, 다음 잔소리에 자연스러운 톤 전환 코멘트
- 첫 잔소리 직후 한 번 옵션 안내 → 사용자가 체감 후 조정 가능
- gentle: 압박 뺀 부드러운 톤 / strict: 박력 ↑ (인격 모독 금지)

### Quiet mode (외출·취침)
- 사용자가 *"나갈게"·"잘게"* 같이 발화하면 EXTRACT 가 잡아 자동 진입 (또는 `/away`·`/sleep` 명령)
- 활성 중: 모든 자동 발사 보류 (위성 트리거·프로액티브·일지·주간 회고·위험 예측). 사용자 답장·알람·일정 알림은 정상
- 자동 해제: **취침** = 사용자 메시지 보내면 (깨어남) / **외출** = 폰 `place_category=="집"` 자동 감지 또는 *"왔어"·"들어왔어"* 발화
- 안전 fallback: **24시간 지나면 자동 해제** (영영 잠수 방지)
- 해제 시: 보류 건수 있으면 *한 줄* 안내 ("외출 동안 자동 잔소리 N건 보류했어")

### 과부하 안전망
- 잔소리 5회 무시 + 24h 응답 없음 → **Overload checkin** 자동 발사
- "톤이 빡센가? 목표가 무겁나?" 한 번 물어봐 사용자에게 자기결정권 돌려줌 (자동 톤 변경 X)
- 3일 쿨다운

### 목표 분해 — 행동 트리
- 큰 단발 과제("코딩 1시간") → `register_today_goal_with_steps` 로 3~5개 '시동 거는 행동' 자동 분해
- 시스템이 sub_step 진척 추적 → 매번 일관된 다음 행동 제시
- 지난주 완료율 40% 미만 시 자동으로 분량 축소 권장 (캐파 가이드)
- 반복 실패에도 책망 금지, 더 작게 다시 잡아주기 (self-compassion 톤)

### 시작 장벽 도구
- `start_focus_session(분, 작업)` — Pomodoro 식 타이머. 끝나면 자동으로 "어땠어? 더 갈래/쉴래/끝낼래?" 물음
- `register_implementation_intention(situation, response)` — WOOP/MCII 의 if-then plan 저장. 매칭 상황에서 코치가 가볍게 상기

### 자기 격려 시스템
- `log_mood(rating 1-5)` — 사용자가 기분 꺼낼 때 가볍게 기록 + **mood 1~5 inline keyboard 버튼** (하루 마무리 일지·주간 회고에 자동 첨부, 탭 한 번 응답)
- 자유 텍스트로 기분 묻어나면 EXTRACT 가 자동 log_mood. 버튼·텍스트 둘 다 같은 통계
- **mood 가치 되돌려주기** — 데이터 충분(7일 5회+)이면 LLM 컨텍스트에 추세 hint 노출, 답장에 짧게 끌어와 동기 부여
- `get_weekly_insight` — 지난 7일 vs 그 전 7일 비교 (목표 완료·습관·트리거·mood·완료율·**mood 상관**·**시간대 패턴**)
- `analyze_my_patterns(days?)` — 최근 N일 활동 로그를 AI 가 종합 분석해 의미 있는 패턴 3~5개 자연어로
- mood ↔ 활동 인과 통계: "목표 완료한 날 mood +0.7" 같은 결정론적 상관 (LLM X)
- **시간대 매핑** — 시간(hour) 별 트리거·목표 완료·습관 카운트로 골든타임·위험 시간대 도출
- **도파민 trail 학습** — 위성이 트리거 직전 sanitized 라벨 시퀀스 동봉, 백엔드 N-gram 카운트 → if-then plan 제안 후보

### 습관 시스템
- `register_habit` — 3~4단계 점진적 레벨 (예: 독서 10→20→30분)
- 한 레벨에서 3회 성공 시 자동 레벨업, streak 추적

### 일정·알람
- **봇 자체 일정** (`add_event`) — OAuth 없이 시작 N분 전 자동 미리 알림
- **Google 캘린더** (`add_calendar_event`) — 인증 시 활성, 자체 일정과 병존
- **시간 알람** (`set_alarm`) — 1회 / 매일 반복. 발송 실패 시 3회 재시도 후 포기
- `_reminder_loop` 가 두 일정 소스를 한 루프에서 처리, 폴링 miss 보완 (10분 grace)

### 사진 완료 검증
- "다 했어" 만으론 안 쳐줌 — 사진 인증 요구
- Gemini Vision 이 사진 판독해 인정 여부 결정

---

## 자동 발사 시나리오 (사용자 입력 없이)

| 트리거 | 동작 | 빈도 |
|---|---|---|
| PC 딴짓 (9종, Pomodoro 포함) | 잔소리 / 환기 권유 | 트리거 조건 + 정책별 쿨다운 |
| 폰 약점 앱 *연속 세션* (4종) | 잔소리 발송 | 임계치 + 5분 로컬 쿨다운 |
| PC ↔ 폰 결합 (회피 패턴) | 라벨 승격 + 잔소리 | 10분 윈도우 내 두 위성 신호 매칭 시 |
| 1시간 침묵 | 프로액티브 안부 + 미룬 목표·다가오는 일정 슬쩍 상기 | 1시간 |
| 24h 무응답 + 잔소리 5회 무시 | **Overload checkin** | 3일 쿨다운 |
| 위험 시간대 *직전* 15분 | **선제 알림** — 과거 패턴 기반 (gentle 톤일 땐 스킵) | 일 1회 |
| 매일 22시 | **하루 마무리 일지** — "오늘 어땠어?" 한 줄 회고 유도 | 일 1회 |
| 일요일 21시 | **주간 회고** — 7일 데이터 + 자기학습 weak_spot 후보 확인 | 주 1회 |
| 캘린더 / 봇 일정 N분 전 | 미리 알림 | 일정별 |
| Focus session 종료 | "어땠어?" 자동 묻기 | 세션별 |
| 텔레그램 전송 실패 | **영구 retry 큐 적재** → 백오프 1m→2m→…→30m 로 도달까지 재시도 | 1주일 후 만료 |
| retry 큐 5개+ / 1시간+ 묵음 | 사용자에게 시스템 알림 1회 (3시간 쿨다운) | 큐 상태 기반 |
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

## 도구 목록

모델이 호출하는 행동, 카테고리별:

| 카테고리 | 도구 |
|---|---|
| **목표** | `register_today_goal_with_steps` · `advance_today_goal_step` · `cancel_today_goal` |
| **습관** | `register_habit` · `cancel_habit` |
| **일정 (봇 자체)** | `add_event` · `list_events` · `cancel_event` |
| **일정 (Google 캘린더, 인증 시)** | `add_calendar_event` · `list_calendar_events` |
| **알람** | `set_alarm` · `list_alarms` · `cancel_alarm` |
| **시작 장벽** | `start_focus_session` · `register_implementation_intention` · `cancel_implementation_intention` |
| **자기 격려·분석** | `log_mood` · `get_weekly_insight` · `analyze_my_patterns` |

활성 도구 목록은 매 turn `_state_summary` 에 동적으로 노출 — 환경에 따라 빠지는 도구 (예: 캘린더 미인증) 를 LLM 이 호출·언급하지 않도록.

---

## 실행

### A. Railway 클라우드 봇 (24/7 텔레그램 + AI)

이미 배포됨: `https://naggingcoach-production.up.railway.app`
재배포는 `git push origin main` 만 하면 자동.

Railway Variables:
```
GEMINI_API_KEY        Gemini API 키
TELEGRAM_BOT_TOKEN    @BotFather 에서 발급
TRIGGER_SECRET        위성과 공유하는 Bearer 토큰
STATE_PATH            /data/state.json (영속 볼륨)
ENABLE_PC_TRACKER     false (컨테이너엔 데스크톱 X)
```

### B. PC 위성 (Python)

```
python trigger_satellite.py
```

또는 Windows 자동 시작 (이미 등록됨):
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\nagging_coach_satellite.vbs`

로컬 `.env`:
```
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
NAGGING_COACH_URL=https://naggingcoach-production.up.railway.app
TRIGGER_SECRET=...
ENABLE_PC_TRACKER=false
```

> ⚠️ 로컬에서 `python app.py` 띄우지 말 것 — Railway 봇과 409 충돌. 로컬은 위성만.

### C. 폰 위성 (Android)

`android-satellite/` 디렉토리에 Kotlin/Java 앱. **Android Studio 없이도 GitHub Actions 가 자동 빌드** → APK artifact 다운로드만.

1. GitHub Actions 페이지에서 최신 ✓ run 클릭 → 최하단 Artifacts → `nagging-coach-satellite-debug.zip` 다운로드
2. zip 풀면 `app-debug.apk`
3. 폰에 옮겨 설치 (출처 불명 앱 허용 필요)
4. 앱 실행 → 1단계 (Usage Access 권한) → 2단계 (위성 시작)
5. 알림창에 "잔소리 코치 폰 활동 감시 중" 보이면 가동

`android-satellite/app/build.gradle.kts` 의 `buildConfigField` 로 백엔드 URL·TRIGGER_SECRET 빌드 시 주입.

### D. (선택) Google 캘린더 연동

1. Google Cloud Console 에서 OAuth 데스크톱 클라이언트 → `credentials.json`
2. `python calendar_setup.py` 1회 실행
3. 생략하면 캘린더 도구만 비활성, 봇 자체 일정 (`add_event`) 은 그대로 작동

### 사용

텔레그램에서 봇한테 `/start` 보내면 연결됨. 그 후엔 그냥 대화하면 코치가 알아서 챙김.

| 명령어 | 동작 |
|---|---|
| `/start` | 봇 연결 (최초 1회) |
| `/help` | 사용법 안내 |
| `/export` | 내 데이터 (목표·습관·일정·통계·설정) 한 번에 보기 |
| `/reset` | 대화·목표·습관·일정 비우기 (`nag_policy` · 누적 통계는 유지). **첫 명령은 안내, 60초 안에 한 번 더 보내야 실행** (다른 메시지 도착 시 자동 취소) |
| `/reset all` | 통계·설정·자기학습까지 전부 비우기 (처음 만난 상태로). 동일한 확인 쿠션 적용 |
| `/place 라벨` | 장소 등록 — 5분 안에 텔레그램 *내 위치* 메시지 보내면 그 좌표로 등록. `/place list`·`/place remove 라벨` 도 가능 |
| `/away` · `/sleep` | 외출·취침 모드 — 자동 잔소리 보류 (24h 안전 한도) |
| `/wake` · `/back` | 모드 해제. 단, 발화로도 자동 해제됨 ("왔어"·"일어났어" 또는 메시지 보내면) |
| 사진 전송 | 할 일 인증샷 |
| 위치 전송 | `/place 라벨` 직후 5분 안에 등록, 그 외엔 무시 |

---

## 파일 구조

| 파일 | 역할 |
|---|---|
| `app.py` (601줄) | `CoachApp` 본체 — `__init__` · `_on_update` · chat/photo 핸들러 · /reset 확인 쿠션 · chat debounce · main 진입점 |
| `app_http.py` (143줄) | `TriggerHTTPHandler` + `HttpServerMixin` — `POST /trigger` · `GET /health` · `GET /weak_spots` |
| `app_messaging.py` (281줄) | `MessagingMixin` — `_send` (짧은 backoff) · `_send_or_enqueue` · `_apply_message_side_effects` · retry 큐 워커 · 큐 모니터링 알림 · 톤 노트 |
| `app_triggers.py` (307줄) | `TriggersMixin` — `_on_trigger` (로컬) · `handle_remote_trigger` (원격) · `_describe_trigger` · 복합 룰 평가 |
| `app_loops.py` (363줄) | `LoopsMixin` — proactive · reminder · alarm · risk_predict · daily_journal · weekly_review · daily_restart 데몬 + 헬퍼 |
| `trigger_satellite.py` | PC 위성 — Python Tracker + HTTP POST + 5분 간격 `/weak_spots` 동기화 |
| `android-satellite/` | 폰 위성 — Android (Kotlin/Java), UsageEvents 기반 연속 세션 측정 |
| `ai_engine.py` | `CoachAgent` — 에이전트 루프 · EXTRACT (별도 모델 분리 가능) · 비전 판독 · WOOP/회고/overload/risk_predict/daily_journal/pattern 분석 · `_call_genai` retry wrapper |
| `agent_tools.py` | 도구 레지스트리 (17~19종) |
| `tracker.py` | PC 활동 감시 + 9종 딴짓 트리거 (Pomodoro 포함) + 생산성 가드 + 화면 라벨 sanitize |
| `store.py` | 상태 영속화 — 목표·습관·프로필·values·if-then·daily_stats (시간대 매핑 포함)·mood·events·weak_spot 후보·pending_messages 큐·dopamine_trails·nag_policy_temp |
| `tests/` | pytest 단위 테스트 (33개) — store/tracker/복합 트리거 |
| `calendar_client.py` / `calendar_setup.py` | Google 캘린더 클라이언트 · OAuth 인증 |
| `telegram_client.py` | 텔레그램 Bot API (long-polling) |
| `Dockerfile` / `requirements-docker.txt` | Railway 클라우드 봇 빌드 |
| `.github/workflows/android-build.yml` | GitHub Actions Android 빌드 — APK artifact |
| `nagging_coach_satellite.vbs` | Windows 시작프로그램 — PC 위성 자동 실행 |

---

## 기술 스택

- Python 3.10+ (PC 위성), 3.12-slim (Railway 컨테이너)
- Java 17 + Android SDK 34 (폰 위성, AGP 8.2.2 + Gradle 8.5)
- Gemini (`gemini-3-flash-preview`, 멀티모달) — `google-genai` SDK
- 텔레그램 Bot API (long-polling)
- `pygetwindow` · `pynput` — PC 활동 감시
- Android `UsageStatsManager.queryEvents` — 폰 연속 세션 측정
- Google Calendar API (OAuth, 선택)
- Railway (클라우드 호스팅, 영속 볼륨)
- GitHub Actions (Android APK 자동 빌드)

---

## 한계 / Future work

**현재 한계**:
- 폰 위성은 **Android 전용** — iOS 는 Screen Time API 정책상 일반 앱 접근 불가
- 멀티 사용자 지원 X — 1인 1봇 구조 (chat_id 1개만 등록)

**부분 구현 — 데이터·분석 레이어 통합은 이미 완료**:
- PC + 폰 트리거가 같은 `daily_stats` / `weak_spot_candidates` 에 누적 — 데이터 통합 ✓
- `analyze_my_patterns` 가 두 위성 활동을 같이 분석 — LLM 통합 분석 ✓
- 주간 회고·인사이트가 통합 기준으로 집계 ✓

**구현 완료 (Phase B/C 일괄 + 후속 운영 개선)**:
- 시간대별 생산성 매핑 — `hourly_breakdown` 으로 골든타임·위험 시간대 도출, `get_weekly_insight`·`analyze_my_patterns` 노출
- 위험 예측 선제 알림 — 위험 시간대 *직전* 1일 1회 발사 (gentle 톤일 땐 스킵)
- 실시간 복합 트리거 — PC + 폰 결합 룰 (`회피 패턴`), `_recent_triggers` 메모리 기반
- 도파민 trail 학습 — 위성이 트리거 직전 sanitized 시퀀스 동봉, 백엔드 N-gram 카운트, `analyze_my_patterns` 가 if-then plan 권유에 활용
- 생산성 앱 가드 — `PRODUCTIVITY_KEYWORDS` 매칭 시 도파민·과몰입·도파민 스크롤 트리거 억제 (약점·산만·과로·늦은 밤은 그대로)
- 위성↔백엔드 weak_spots 동기화 — `GET /weak_spots` 엔드포인트, 위성 5분 간격 fetch + 캐싱
- EXTRACT 모델 분리 — `GEMINI_EXTRACT_MODEL` 환경변수, Lite 등 cheap 모델 사용 시 비용 절감
- Gemini API retry — 일시 503/504/timeout 에 1s→2s backoff, 영구 오류는 즉시 fail-fast
- 텔레그램 영구 retry 큐 — 전송 실패 시 큐 적재 → backoff 1m→30m 으로 도달까지 재시도, 부수효과는 *도달 시점*에 적용
- chat debounce — 사용자 우다다 메시지 묶어 한 번에 답장
- 사용자 알아가기 적극성 — 빈 profile 영역 hint + 시스템 프롬프트 강화
- 하루 마무리 일지 (매일 22시) + Pomodoro 휴식 (50분 한 세션 1회)
- `/reset` · `/reset all` 두 단계 확인 쿠션 + `/reset all` 분기
- **mood 시스템 능동화** — 자가 보고 의존 → inline keyboard 빠른 버튼 + 일지/주간 회고 자동 첨부 + 추세 피드백 동기 부여
- 단위 테스트 33개 (store · tracker · 복합 트리거)
- `app.py` god class → `HttpServerMixin` · `MessagingMixin` · `TriggersMixin` · `LoopsMixin` 모듈 분리 (1583 → 601줄)

백엔드는 이미 `POST /trigger` 한 엔드포인트로 모든 클라이언트를 받는 구조라, 새 디바이스 (스마트워치 등) 추가도 같은 패턴으로 가능.

---

## 비고

`.env` · `credentials.json` · `token.json` · `state.json` · `satellite.log` 는
비밀·개인정보이므로 저장소에 커밋하지 않는다 (`.gitignore` 등록됨).
APK 안에 박힌 `TRIGGER_SECRET` 은 GitHub repo 가 public 이면 노출됨 — private repo 권장 또는 GitHub Secrets 로 분리.
