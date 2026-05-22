# Docker 실행 가이드

잔소리 코치를 헤드리스 텔레그램 봇으로 컨테이너에서 실행한다.

## 구성 파일

| 파일 | 역할 |
|------|------|
| `Dockerfile` | Python 3.12-slim 기반 이미지 |
| `docker-compose.yml` | 볼륨·환경변수·재시작 정책 |
| `requirements-docker.txt` | 컨테이너용 의존성 (트래커 패키지 제외) |
| `.dockerignore` | 비밀·상태 파일을 이미지에서 제외 |

## 사전 준비

프로젝트 루트에 `.env` 가 있어야 한다 (`.env.example` 참고). 컨테이너는
`.env` 의 키를 환경변수로 주입받는다 — `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`GEMINI_MODEL`, `ENABLE_PC_TRACKER`.

## 실행

```bash
docker compose up -d --build   # 빌드 후 백그라운드 실행
docker compose logs -f         # 로그 확인
docker compose down            # 중지
```

## state.json 영속화

상태 파일은 호스트 `./data/` 와 컨테이너 `/data` 를 연결해 영속화된다
(`STATE_PATH=/data/state.json`). 컨테이너를 지워도 `./data/state.json` 은 남는다.

기존 상태(텔레그램 등록·대화 기록)를 이어가려면 기존 `state.json` 을
`./data/` 로 복사해 둔다. 새로 시작하려면 `./data/state.json` 을 지운다.

## PC 활동 감시(Tracker)

컨테이너에는 데스크톱이 없어 트래커가 동작하지 않는다. `.env` 의
`ENABLE_PC_TRACKER` 는 `false` 로 둔다. PC 감시까지 쓰려면 컨테이너가 아니라
호스트에서 직접 실행해야 한다 (`start_coach.bat`).

## Google 캘린더 (선택)

캘린더를 쓰려면 `docker-compose.yml` 에서 `credentials.json`·`token.json`
마운트 2줄의 주석을 해제한다. 없으면 캘린더 기능만 비활성화되고 봇은 정상 동작한다.

## 주의: 인스턴스는 하나만

같은 텔레그램 봇 토큰으로 인스턴스를 둘 이상 띄우면 폴링이 충돌한다(409).
호스트에서 `start_coach.bat` 으로 돌고 있다면, 컨테이너를 켜기 전에 호스트
인스턴스를 종료한다 — 한 번에 하나만.
