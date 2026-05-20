# 잔소리 코치 (Nagging Coach)

PC에서 백그라운드로 돌며, 텔레그램으로 대화하고, 사용자의 활동을 지켜보다가
목표·습관을 향해 **먼저 개입하는 능동형 개인 AI 에이전트.**

명령어로 조작하는 챗봇이 아니다. 대화 속에서 사용자를 알아가고, PC 딴짓을
감지해 잔소리하고, 일정·알람·습관을 직접 관리하며, "완료했다"는 말은 사진으로
검증한다.

## 에이전트 구조

기능을 하나씩 붙인 게 아니라 **닫힌 루프**로 동작한다:

```
지각(Perception) → 추론(Reasoning) → 행동(Action) → 기억(Memory) ↺
```

| 단계 | 담당 | 방식 |
|---|---|---|
| 지각 | `tracker.py` + 상태 추출 | 결정론적 PC 센서 + focused LLM 추출 |
| 추론 | 에이전트 루프 (`ai_engine.py`) | LLM이 스스로 도구 선택 (ReAct 방식) |
| 행동 | 도구 (`agent_tools.py`) | 캘린더·알람·습관 등록 |
| 기억 | `store.py` | 목표·습관·프로필 영속화 |

설계 원칙: **센싱은 결정론적 코드, 추론·판단은 LLM.** 새 능력은 `agent_tools.py`
에 도구를 추가하면 에이전트가 알아서 쓴다.

## 주요 기능

- **대화 코치** — 친구 같은 톤으로 대화하며 사용자의 성격·취미·직업·약점을 학습
- **PC 딴짓 감지 (8종)** — 도파민 좀비 / 산만함 / 가짜 일하기 / 과몰입 /
  능동적 스크롤 / 늦은 밤 / 과로 / 개인 약점 앱
- **목표 관리** — 단기 목표(여러 개) + 장기 목표 + 스몰스텝 분해
- **습관 시스템** — 난이도 레벨 진도제 + 연속일수(streak) 추적
- **Google 캘린더** — 대화로 일정 등록, 시작 전 리마인드
- **예약 알람** — 일회성·매일 반복
- **사진 완료 검증** — 완료 증거 사진을 AI 비전이 판독해 인정 여부 판단
- **능동 개입** — 오래 조용하면 미룬 목표를 콕 집어 먼저 말 걸고,
  잔소리를 무시할수록 강도를 올림

## 설치

1. 의존성 설치
   ```
   pip install -r requirements.txt
   ```
2. 텔레그램 봇 생성 — 텔레그램에서 **@BotFather** → `/newbot` → 토큰 발급
3. `.env` 작성 (`.env.example` 참고)
   ```
   GEMINI_API_KEY=...        # https://aistudio.google.com/apikey
   TELEGRAM_BOT_TOKEN=...    # @BotFather 에서 발급
   ```
4. (선택) Google 캘린더 연동
   - Google Cloud Console에서 OAuth 데스크톱 클라이언트를 만들어 `credentials.json` 저장
   - `python calendar_setup.py` 를 1회 실행해 인증
   - 생략하면 캘린더 기능만 꺼지고 나머지는 정상 동작한다

## 실행

```
python app.py
```

- 또는 `start_coach.bat` 더블클릭 (콘솔 최소화 실행)
- Windows 시작프로그램에 `start_coach.bat` 바로가기를 넣으면 부팅 시 자동 실행

실행 후 텔레그램에서 봇에게 **`/start`** 를 보내면 연결된다.

## 사용법

- 그냥 대화하면 된다 — 코치가 알아서 목표·습관·일정을 챙긴다.
- **사진 전송** — 할 일을 끝냈으면 인증샷을 보내면 AI가 검증한다.
- `/start` — 봇 연결 (최초 1회)
- `/reset` — 대화 기록·목표·습관 초기화

## 파일 구조

| 파일 | 역할 |
|---|---|
| `app.py` | 헤드리스 오케스트레이터 — 6개 스레드 조율 |
| `ai_engine.py` | `CoachAgent` — 에이전트 루프 · 상태 추출 · 비전 판독 |
| `agent_tools.py` | 도구 레지스트리 (캘린더·알람·습관) |
| `tracker.py` | PC 활동 감시 + 8종 딴짓 트리거 |
| `calendar_client.py` | Google 캘린더 클라이언트 |
| `calendar_setup.py` | 캘린더 최초 OAuth 인증 (1회 실행) |
| `telegram_client.py` | 텔레그램 Bot API 클라이언트 (long-polling) |
| `store.py` | 상태 영속화 (`state.json`) |
| `smoke_test.py` | 핵심 기능 라이브 검증 스크립트 |

## 기술 스택

- Python 3.10+
- Gemini (`gemini-3-flash-preview`, 멀티모달) — `google-genai` SDK
- 텔레그램 Bot API (long-polling)
- `pygetwindow` · `pynput` · `psutil` — PC 활동 감시
- Google Calendar API (OAuth)

## 비고

- `.env` · `credentials.json` · `token.json` · `state.json` 은 비밀·개인정보이므로
  저장소에 커밋하지 않는다 (`.gitignore` 에 등록됨).
