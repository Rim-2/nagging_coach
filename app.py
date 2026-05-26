"""
app.py — 잔소리 코치 (헤드리스 텔레그램 봇) 메인 진입점

데스크톱 GUI 없이 백그라운드에서 동작한다:
    [1] PC 활동을 Tracker로 감시 (ENABLE_PC_TRACKER 일 때만)
    [2] 사용자와 텔레그램으로 대화 (CoachAgent — 대화로 목표를 끌어냄)
    [3] 딴짓이 감지되면 대화 흐름에 맞춰 잔소리 발송
    [4] 한동안 조용하면 코치가 먼저 말을 걺 (프로액티브)
    [5] Google 캘린더 일정을 읽고, 시작 전에 미리 알림
    [6] 완료 증거 사진을 받으면 AI 비전으로 판독해 인정 여부 판단

스레드 구성:
    메인 스레드        살아있기만 함 (종료 신호 대기)
    Tracker 스레드     PC 감시 → _on_trigger  (ENABLE_PC_TRACKER 일 때만)
    텔레그램 폴링 스레드  getUpdates → _on_update
    프로액티브 스레드    일정 시간 조용하면 먼저 말 걸기
    리마인더 스레드     캘린더 일정이 곧 시작하면 미리 알림
모든 AI 호출은 CoachAgent 내부 락으로 직렬화된다.
"""

from __future__ import annotations

import datetime
import http.server
import json
import os
import sys
import threading
import time
from typing import Optional

from dotenv import load_dotenv

from agent_tools import FOCUS_END_MARKER
from ai_engine import AIGenerationError, CoachAgent
from calendar_client import CalendarClient
from store import Store
from telegram_client import TelegramClient

load_dotenv()

# PC 활동 감시(Tracker)는 ENABLE_PC_TRACKER 환경변수로 켜고 끈다 (기본: 꺼짐).
# 꺼져 있으면 tracker 모듈을 아예 import 하지 않는다 — 화면·입력 감시에 필요한
# OS 의존 패키지(pygetwindow, pynput)가 없는 환경에서도 봇이 동작하도록.
ENABLE_PC_TRACKER = os.getenv("ENABLE_PC_TRACKER", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
if ENABLE_PC_TRACKER:
    from tracker import Snapshot, State, Tracker, TriggerType

# Windows 콘솔(cp949)에서 한글·특수문자 출력이 깨지거나 죽지 않도록 UTF-8로 전환.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# state.json 경로. STATE_PATH 환경변수로 덮어쓸 수 있다 (Docker 볼륨 등).
STATE_PATH = os.getenv("STATE_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state.json"
)

WARNING_TIMEOUT_SEC = 600.0       # 잔소리 후 응답 없으면 10분 뒤 감시 자동 정상화
PROACTIVE_IDLE_SEC = 3600.0       # 사용자가 1시간 답 없으면 먼저 말 걸기
PROACTIVE_CHECK_INTERVAL = 120.0  # 프로액티브 조건 점검 주기
AI_FAILURE_WARN_THRESHOLD = 3     # 연속 N회 실패 시 사용자에게 시스템 경고
REMINDER_LEAD_MIN = 15.0          # 캘린더 일정 N분 전에 리마인드
REMINDER_CHECK_INTERVAL = 120.0   # 일정 리마인더 점검 주기
REMINDER_GRACE_SEC = 600.0        # 폴링 miss·재시작으로 늦은 알림 — N초 안이면 발사 (10분)
ALARM_ONCE_MAX_RETRY = 3          # once 알람 발송 실패 시 최대 재시도 횟수


_HELP_TEXT = (
    "👋 잔소리 코치 사용법\n\n"
    "그냥 친구처럼 떠들면 돼 — 명령어 외울 필요 X. 코치가 알아서 챙겨:\n"
    "• 오늘 할 일·습관 얘기하면 자동 기억\n"
    "• 큰 과제는 작은 step 으로 자동 쪼개기\n"
    "• 일정 잡으면 N분 전 자동 미리 알림\n"
    "• 시간 알람 (\"1시간 뒤 물 마셔\") 도 가능\n"
    "• \"다 했어\" 보다 사진 인증샷 — AI 가 검증\n"
    "• 톤이 빡세면 \"살살 해줘\", 약하면 \"더 세게\"\n"
    "• 일요일 21시에 한 주 회고 자동 시작\n\n"
    "명령어:\n"
    "/start — 봇 연결 (최초 1회)\n"
    "/help — 이 도움말\n"
    "/export — 내 데이터 (목표·습관·일정·통계) 한 번에 보기\n"
    "/reset — 대화·목표·습관·일정 다 비우기 (설정·통계는 유지)\n"
)
ALARM_CHECK_INTERVAL = 30.0       # 예약 알람 점검 주기
WEEKLY_REVIEW_HOUR = 21           # 매주 일요일 N시에 주간 회고 ritual
WEEKLY_REVIEW_CHECK_INTERVAL = 300.0  # 5분 주기로 시각 체크 (21:00~21:59 사이 1회)

# 과부하 자체 점검 — 잔소리가 N회 연속 무시되고 사용자가 한참 답 없을 때,
# 코치가 '톤이 빡센가, 목표가 무거운가' 한 번 물어봐 자기결정권 돌려주기.
# 자기인식 부재로 strict 톤을 고집하거나 캐파 오버 목표로 반복 실패하는
# 케이스의 안전망. 발사 후 3일은 쿨다운.
OVERLOAD_IGNORED_THRESHOLD = 5
OVERLOAD_SILENCE_SEC = 86400.0       # 사용자가 24시간 응답 없음
OVERLOAD_COOLDOWN_SEC = 86400.0 * 3  # 같은 사용자에게 3일 안엔 안 띄움

# ── 자동 복구 (Railway가 컨테이너를 자동 재시작하는 걸 이용) ─────────────
# A) 텔레그램 발송이 연속 N회 실패하면 컨테이너 자살 → Railway가 재시작.
#    네트워크 누적 문제로 send가 막힐 때 사람 개입 없이 복구하기 위함.
TG_FAILURE_EXIT_THRESHOLD = 20
# C) 매일 새벽 한 번 깨끗한 상태로 재시작 (장기 누적 문제 예방).
DAILY_RESTART_HOUR = 4
DAILY_RESTART_MIN_UPTIME_HOURS = 23
DAILY_RESTART_CHECK_INTERVAL = 300.0  # 5분 주기로 시각 체크

# 원격 위성 트리거 쿨다운. 위성은 자체 60초 쿨다운만 가져서, idle/dwell 기반 트리거
# (도파민 좀비, 가짜 일하기, 개인 약점 앱)는 조건이 계속 참이면 1~2분마다 또 발사될
# 수 있음. 같은 트리거 종류는 클라우드가 권위 있게 N분 막아 잔소리 폭주를 차단.
REMOTE_TRIGGER_COOLDOWN_SEC = 600.0

# 폰 위성이 보내는 Android 패키지명을 사람이 읽는 라벨로. 매핑에 없으면 그대로.
_PHONE_APP_LABELS = {
    "com.instagram.android": "인스타그램",
    "com.google.android.youtube": "유튜브",
    "com.zhiliaoapp.musically": "틱톡",
    "com.ss.android.ugc.trill": "틱톡",
    "com.facebook.katana": "페이스북",
    "com.twitter.android": "X(트위터)",
    "com.reddit.frontpage": "레딧",
    "com.snapchat.android": "스냅챗",
    "com.kakao.talk": "카카오톡",
    "com.discord": "디스코드",
    "com.nhn.android.band": "밴드",
    "com.linecorp.linelite": "라인",
    "phone-screen": "휴대폰",
}


class _TriggerHTTPHandler(http.server.BaseHTTPRequestHandler):
    """원격 PC 트래커 위성(trigger_satellite.py)에서 보내는 트리거를 받는
    미니 HTTP 핸들러. Bearer 토큰으로 인증하고, 검증된 요청만 CoachApp.
    handle_remote_trigger 로 위임한다.

    같은 봇 토큰으로 두 인스턴스가 텔레그램 폴링을 동시에 하면 409 충돌이
    나기 때문에, '클라우드 24/7 봇 + 로컬 PC 감시'를 같이 운영하려면 로컬은
    텔레그램에 손대지 않고 트리거만 여기로 쏴야 한다."""

    # 서브클래스 팩토리(_start_http_server)에서 주입된다.
    coach_app: Optional["CoachApp"] = None
    trigger_secret: str = ""

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 인터페이스
        if self.path != "/trigger":
            self._respond(404, {"ok": False, "error": "not found"})
            return
        if not self.trigger_secret:
            self._respond(503, {"ok": False, "error": "secret not configured"})
            return
        if self.headers.get("Authorization", "") != f"Bearer {self.trigger_secret}":
            self._respond(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            self._respond(400, {"ok": False, "error": f"bad json: {exc}"})
            return
        try:
            result = self.coach_app.handle_remote_trigger(body)
        except Exception as exc:
            print(f"[App] 원격 트리거 처리 오류: {exc}")
            self._respond(500, {"ok": False, "error": "internal"})
            return
        self._respond(200, result)

    def do_GET(self) -> None:  # noqa: N802
        # 외부에서 curl 로 살아있는지 찔러볼 수 있는 헬스체크용 엔드포인트.
        if self.path in ("/", "/health"):
            self._respond(200, {"ok": True, "service": "nagging_coach"})
            return
        self._respond(404, {"ok": False, "error": "not found"})

    def log_message(self, format, *args) -> None:  # noqa: A002
        # 기본 access log 는 stderr 로 시끄러움 — 우리 print 만 남긴다.
        return

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CoachApp:
    def __init__(self) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "환경변수 TELEGRAM_BOT_TOKEN 이 비어있어. "
                "텔레그램 @BotFather 로 봇을 만들고 토큰을 .env 에 넣어줘."
            )

        self._store = Store(STATE_PATH)
        self._tg = TelegramClient(token)
        self._calendar = self._init_calendar()
        self._agent = CoachAgent(
            self._store,
            calendar=self._calendar,
            on_goal_set=self._on_goal_set,
            on_today_complete=self._on_today_complete,
            on_back_on_track=self._on_back_on_track,
        )
        self._tracker = self._init_tracker()

        self._stop = threading.Event()
        self._timer_lock = threading.Lock()
        self._warning_timer: Optional[threading.Timer] = None
        self._last_user_msg = time.time()    # 사용자가 마지막으로 보낸 시각
        self._last_proactive = time.time()   # 봇이 마지막으로 먼저 말 건 시각
        self._consecutive_ai_failures = 0
        self._consecutive_tg_failures = 0    # 텔레그램 발송 연속 실패 카운터 (자동 재시작용)
        self._started_at = time.time()       # 컨테이너 시작 시각 (매일 재시작 판단용)
        self._ignored_nags = 0               # 연속으로 무시당한 잔소리 횟수
        self._remote_cooldowns: dict[str, float] = {}  # trigger_value → 다음 발동 가능 시각
        self._remote_cooldown_lock = threading.Lock()

    @staticmethod
    def _init_calendar() -> Optional[CalendarClient]:
        """캘린더 클라이언트를 준비한다. 인증(token.json)이 없으면 None —
        캘린더 기능만 꺼지고 봇의 나머지는 정상 동작한다."""
        try:
            cal = CalendarClient()
        except Exception as exc:
            print(f"[App] 캘린더 초기화 실패 — 캘린더 기능 비활성: {exc}")
            return None
        if not cal.has_token():
            print(
                "[App] 캘린더 미인증 — 캘린더 기능 비활성. "
                "calendar_setup.py 를 먼저 실행하면 켜진다."
            )
            return None
        print("[App] Google 캘린더 연동 활성화됨")
        return cal

    def _init_tracker(self) -> Optional["Tracker"]:
        """PC 활동 감시(Tracker)를 준비한다. ENABLE_PC_TRACKER 가 꺼져 있으면
        None — 감시 기능만 빠지고 텔레그램 코치 기능은 그대로 동작한다."""
        if not ENABLE_PC_TRACKER:
            print("[App] PC 활동 감시 비활성화 (ENABLE_PC_TRACKER=false)")
            return None
        print("[App] PC 활동 감시 활성화됨 (ENABLE_PC_TRACKER=true)")
        return Tracker(
            on_trigger=self._on_trigger,
            get_weak_spots=lambda: self._store.weak_spots,
        )

    # =============================================================== run
    def run(self) -> None:
        me = self._tg.get_me()
        print(f"[App] 텔레그램 봇 연결됨: @{me.get('username')}")

        if self._store.chat_id is not None:
            self._start_tracking()
            mode = "추적 시작" if self._tracker is not None else "감시 없이 대기"
            print(f"[App] 기존 사용자(chat_id={self._store.chat_id}) — {mode}")
        else:
            print("[App] 등록된 사용자 없음 — 텔레그램에서 봇에게 /start 를 보내줘.")

        self._tg.start_polling(self._on_update)
        threading.Thread(
            target=self._proactive_loop, name="Proactive", daemon=True
        ).start()
        # 리마인더 루프는 Google 캘린더와 봇 자체 일정 둘 다 챙기므로 항상 시작.
        threading.Thread(
            target=self._reminder_loop, name="Reminder", daemon=True
        ).start()
        threading.Thread(
            target=self._alarm_loop, name="Alarm", daemon=True
        ).start()
        threading.Thread(
            target=self._daily_restart_loop, name="DailyRestart", daemon=True
        ).start()
        threading.Thread(
            target=self._weekly_review_loop, name="WeeklyReview", daemon=True
        ).start()
        self._start_http_server()

        print("[App] 실행 중. Ctrl+C 로 종료.")
        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        print("\n[App] 종료 중…")
        self._stop.set()
        self._cancel_warning_timeout()
        if self._tracker is not None:
            self._tracker.stop()
        self._tg.stop()

    # ==================================================== 텔레그램 수신
    def _on_update(self, update: dict) -> None:
        """텔레그램 폴링 스레드에서 호출됨."""
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        caption = (message.get("caption") or "").strip()
        photo = message.get("photo")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or (not text and not photo):
            return

        if text.startswith("/start"):
            self._handle_start(chat_id)
            return

        registered = self._store.chat_id
        if registered is None:
            self._send(
                chat_id, "먼저 /start 를 보내줘. 그래야 너랑 짝이 될 수 있어."
            )
            return
        if chat_id != registered:
            return  # 1인용 봇 — 등록된 사용자만 응대

        if text.startswith("/help"):
            self._send(chat_id, _HELP_TEXT)
            return

        if text.startswith("/export"):
            self._send(chat_id, self._build_export())
            return

        if text.startswith("/reset"):
            self._agent.reset()
            self._send(
                chat_id, "기억 싹 비웠어. 자, 다시 시작하자 — 오늘 뭐 할 거야?"
            )
            return

        if photo:
            self._handle_photo(chat_id, photo, caption)
            return
        self._handle_chat(chat_id, text)

    def _handle_start(self, chat_id: int) -> None:
        self._store.chat_id = chat_id
        self._start_tracking()
        self._last_user_msg = time.time()
        pc_note = (
            "지금부터 PC도 슬쩍 지켜본다 — 딴짓하면 바로 잡으러 간다.\n"
            if self._tracker is not None
            else ""
        )
        self._send(
            chat_id,
            "안녕! 난 너의 잔소리 코치야 (AI 챗봇임 😅)\n"
            "여기서 친구처럼 떠들면 돼 — 명령어 외울 필요 X. 같이 할 수 있는 거:\n"
            "• 오늘 할 일·습관 챙기기 (대화로)\n"
            "• 일정·알람 잡기\n"
            "• 막히면 작은 행동으로 쪼개기\n"
            "• 사진으로 완료 인증\n"
            "자세한 사용법은 /help.\n"
            + pc_note
            + "\n자, 일단 가볍게 — 요즘 어떻게 지내?",
        )

    def _build_export(self) -> str:
        """사용자가 /export 로 자기 상태 전체를 한 통에 요약 받는다."""
        s = self._store
        lines: list = ["📊 너의 데이터 요약\n"]

        goals = s.today_goals_detailed
        if goals:
            lines.append("【오늘 목표】")
            for g in goals:
                sub = g.get("sub_steps") or []
                if sub:
                    cur = int(g.get("current", 0) or 0)
                    lines.append(f"  • {g['name']} ({cur}/{len(sub)})")
                else:
                    lines.append(f"  • {g['name']}")

        if s.long_term_goal:
            lines.append(f"\n【장기 목표】\n  • {s.long_term_goal}")

        habits = s.habits
        if habits:
            lines.append("\n【습관】")
            for h in habits:
                levels = h.get("levels") or []
                idx = h.get("level_idx", 0)
                cur = levels[idx] if 0 <= idx < len(levels) else h["name"]
                lines.append(
                    f"  • {h['name']} (현재 목표: {cur}, 연속 {h.get('streak', 0)}일)"
                )

        upcoming_events = sorted(
            [e for e in s.events if (e.get("start_ts") or 0) >= time.time()],
            key=lambda e: e.get("start_ts", 0),
        )[:5]
        if upcoming_events:
            lines.append("\n【다가오는 일정】")
            for e in upcoming_events:
                when = datetime.datetime.fromtimestamp(e["start_ts"]).strftime(
                    "%m-%d %H:%M"
                )
                lines.append(f"  • {when} — {e.get('summary')}")

        alarms = s.alarms
        if alarms:
            lines.append("\n【예약 알람】")
            for a in alarms:
                kind = "매일" if a.get("repeat") == "daily" else "한 번"
                lines.append(f"  • ({kind}) {a.get('label')} — {a.get('text')}")

        intentions = s.implementation_intentions
        if intentions:
            lines.append("\n【if-then plan】")
            for p in intentions[:10]:
                lines.append(f"  • '{p['situation']}' → '{p['response']}'")

        summary = s.weekly_summary(days=7)
        rec = summary["recent"]
        lines.append(
            f"\n【최근 7일】\n"
            f"  • 목표 완료 {rec['goals_completed']}회\n"
            f"  • 습관 수행 {rec['habit_dones']}회\n"
            f"  • 잔소리 트리거 {rec['trigger_total']}회"
        )
        if rec.get("mood_count", 0) > 0:
            avg = rec.get("mood_avg") or 0
            lines.append(
                f"  • mood 평균 {avg:.1f}/5 ({rec['mood_count']}회 기록)"
            )
        rate = rec.get("completion_rate")
        if rate is not None:
            lines.append(f"  • 완료율 {int(rate * 100)}%")

        lines.append(f"\n【설정】\n  • 잔소리 강도: {s.nag_policy}")
        profile = s.profile
        if profile:
            facts = ", ".join(
                f"{k}={v}" for k, v in profile.items() if k != "core_values"
            )
            if facts:
                lines.append(f"  • 알아낸 정보: {facts}")
            if profile.get("core_values"):
                lines.append(f"  • core values: {profile['core_values']}")
        weak = s.weak_spots
        if weak:
            lines.append(f"  • 약점 앱: {', '.join(weak)}")

        return "\n".join(lines)

    def _handle_chat(self, chat_id: int, text: str) -> None:
        self._last_user_msg = time.time()
        try:
            reply = self._agent.chat(text)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            return
        self._consecutive_ai_failures = 0
        self._send(chat_id, reply)

    def _handle_photo(self, chat_id: int, photo: list, caption: str) -> None:
        """완료 증거 사진을 받아 AI 비전 판독으로 처리한다."""
        self._last_user_msg = time.time()
        try:
            file_id = photo[-1]["file_id"]  # 마지막 = 가장 큰 해상도
            image = self._tg.download_file(file_id)
        except Exception as exc:
            print(f"[App] 사진 다운로드 실패: {exc}")
            self._send(chat_id, "사진을 못 받았어 — 다시 보내줄래?")
            return
        try:
            reply = self._agent.verify_completion(image, caption)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            return
        self._consecutive_ai_failures = 0
        self._send(chat_id, reply)

    # ==================================================== Tracker 콜백
    def _on_trigger(self, trigger: TriggerType, snap: Snapshot) -> None:
        """⚠️ Tracker 데몬 스레드에서 호출됨."""
        chat_id = self._store.chat_id
        if chat_id is None:
            self._tracker.resume_normal()
            return

        goals = self._store.today_goals
        goal = ", ".join(goals) if goals else None

        # 산만함 트리거는 LLM에게 '진짜 딴짓인지' 한 번 더 확인
        if trigger == TriggerType.DISTRACTED_SWITCHING:
            freq = self._tracker.get_recent_window_freq()
            try:
                if not self._agent.judge_distracted(goal, freq):
                    print(f"[App] 산만함 판독: 업무 중 — 스킵 ({freq})")
                    self._tracker.resume_normal()
                    return
            except AIGenerationError as exc:
                print(f"[App] 산만함 판독 실패 — 스킵: {exc}")
                self._tracker.resume_normal()
                return
            print(f"[App] 산만함 판독: 딴짓 확정 ({freq})")

        snap_dict = {
            "active_window": snap.active_window,
            "idle_time": snap.idle_time,
            "switch_count": snap.switch_count,
        }
        description = self._describe_trigger(trigger.value, snap_dict, goal)
        description += self._escalation_note()
        try:
            reply = self._agent.handle_event(description)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            self._tracker.resume_normal()
            return

        self._consecutive_ai_failures = 0
        print(f"[App] 트리거 [{trigger.value}] → 잔소리 발송")
        sent_ok = self._send(chat_id, reply)
        self._arm_warning_timeout()
        if sent_ok:
            self._store.bump_trigger_fire(trigger.value)
            # 자기학습 weak_spot 후보 — sanitized 라벨 (PC 환경)
            from tracker import sanitize_window_title
            label = sanitize_window_title(snap.active_window)
            if label and label != "other":
                self._store.bump_weak_spot_candidate(label)
            if not self._store.nag_policy_asked:
                self._store.nag_policy_asked = True

    @staticmethod
    def _describe_trigger(
        trigger_value: str, snap: dict, goal: Optional[str]
    ) -> str:
        """트리거를 사람·LLM 이 읽을 자연어 설명으로 변환. 로컬 Tracker 콜백과
        원격 위성(/trigger HTTP) 둘 다 같은 함수로 통일.

        본문은 관찰 사실 위주(중립 어휘) — "도파민 좀비"·"가짜 일하기" 같은
        진단적 라벨은 코드 식별자(enum)에만 남기고 LLM 컨텍스트엔 넣지 않는다.
        라벨이 노출되면 코치 답장 톤이 진단·낙인 쪽으로 끌려가서 무너져 있는
        사용자한테 역효과."""
        raw_win = snap.get("active_window") or "(알 수 없는 창)"
        # 폰 위성이 보낸 Android 패키지명이면 사람이 읽는 이름으로 (PC 의 sanitize
        # 라벨이나 raw 창 제목은 그대로 통과).
        win = _PHONE_APP_LABELS.get(raw_win, raw_win)
        idle_min = int((snap.get("idle_time") or 0) // 60)
        switch_count = snap.get("switch_count") or 0
        # 폰 위성은 실제 세션 분을 보냄. PC 위성은 안 보냄 → None.
        # None 일 때만 각 트리거의 알려진 default (PC 의 임계치) 로 fall-back.
        session_min = snap.get("session_minutes")

        # 수면·휴식 잔소리는 목표와 무관 — 본문만 돌려준다.
        if trigger_value == "늦은 밤":
            hour = datetime.datetime.now().hour
            return (
                f"지금 새벽 {hour}시인데 사용자가 아직 화면 앞에서 안 자고 있어. "
                f"수면 챙기라고 한마디 해줘."
            )
        if trigger_value == "휴식 없는 과로":
            mins = session_min if session_min and session_min > 0 else 120
            return (
                f"사용자가 {mins}분 넘게 쉬는 틈도 없이 계속 화면 앞에 붙어 있어. "
                "눈도 몸도 지칠 텐데 — 잠깐 쉬라고 챙겨줘."
            )

        goal_note = (
            f" 참고로 오늘 목표는 '{goal}'."
            if goal
            else " 오늘 목표는 아직 안 정했어."
        )
        if trigger_value == "도파민 좀비":
            body = (
                f"사용자가 '{win}'을 {idle_min}분째 입력 없이 보고 있어."
            )
        elif trigger_value == "능동적 도파민 스크롤":
            mins = session_min if session_min and session_min > 0 else 15
            body = (
                f"사용자가 '{win}'을 {mins}분 넘게 손 안 떼고 계속 보고 있어."
            )
        elif trigger_value == "산만함/널뛰기":
            body = (
                f"사용자가 5분 사이 창을 {switch_count}번 옮겨다니고 있어. "
                f"지금 창은 '{win}'."
            )
        elif trigger_value == "가짜 일하기":
            body = (
                f"사용자가 업무앱 '{win}'을 켜놓고 {idle_min}분째 손을 "
                f"안 대고 있어."
            )
        elif trigger_value == "개인 약점 앱":
            body = (
                f"사용자가 평소 자주 빠진다고 했던 '{win}'에 다시 들어왔어."
            )
        else:  # "과몰입 딴짓" 또는 알 수 없는 신규 트리거
            mins = session_min if session_min and session_min > 0 else 30
            body = (
                f"사용자가 '{win}'(게임/메신저류)에 {mins}분 넘게 집중하고 있어."
            )
        return body + goal_note

    def _escalation_note(self) -> str:
        """잔소리 무시 누적 시 description 끝에 붙일 코치 가이드. nag_policy 별
        방향이 다르다 — strict 만 압박을 올리고, gentle/balanced(3회 이상) 은
        오히려 톤다운으로 전환해서 압박이 안 먹히는 사용자에게 역효과를 막는다."""
        n = self._ignored_nags
        if n <= 0:
            return ""
        policy = self._store.nag_policy
        if policy == "gentle":
            return (
                f" (사용자가 잔소리를 {n}번 답 없이 지나갔어 — 더 다그치지 말고 "
                f"가만히 옆에 있어주거나, 행동을 잘게 쪼개 한 발만 권해. '좀 "
                f"힘들어?' 같은 직접 질문은 피해 — 컨디션 안 좋을 땐 그것도 부담.)"
            )
        if policy == "strict":
            return (
                f" (사용자가 잔소리를 이미 {n}번 무시하고 또 딴짓 중 — 점점 더 "
                f"세게, 매번 다른 각도로 쪼아라.)"
            )
        # balanced — 2회까지 escalate, 3회 이상은 톤다운으로 전환
        if n <= 2:
            return (
                f" (사용자가 잔소리를 {n}번 답 없이 지나갔어 — 한 번 더 각도를 "
                f"바꿔 단호하게 쪼아라.)"
            )
        return (
            f" (사용자가 잔소리를 이미 {n}번 답 없이 지나갔어 — 압박이 안 먹히는 "
            f"것 같아. 다그치지 말고 한 발 빠져서 안부를 살피거나, 행동을 더 "
            f"잘게 쪼개서 부담 없이 권해.)"
        )

    # ============================================== 원격 트리거 (위성)
    def handle_remote_trigger(self, body: dict) -> dict:
        """로컬 PC 트래커 위성(trigger_satellite.py)에서 보낸 트리거를 처리.
        HTTP /trigger 핸들러에서 호출됨. _on_trigger 의 클라우드 버전 —
        Tracker 상태 머신은 위성 쪽이 갖고 있고, 여기선 잔소리만 만들어 발송."""
        trigger_value = str(body.get("trigger", "")).strip()
        snap = body.get("snapshot") or {}
        freq = body.get("window_freq")
        if not trigger_value:
            return {"ok": False, "action": "skipped", "reason": "missing_trigger"}

        # 같은 트리거 종류는 N분 쿨다운 — 위성 60초 쿨다운으론 idle/dwell 기반
        # 트리거가 연발되는 걸 못 막아서, 여기서 권위 있게 한 번 더 걸러낸다.
        now = time.time()
        with self._remote_cooldown_lock:
            next_ok = self._remote_cooldowns.get(trigger_value, 0.0)
        if now < next_ok:
            remaining = int(next_ok - now)
            print(
                f"[App] (원격) 트리거 [{trigger_value}] 쿨다운 중 "
                f"({remaining}s 남음) — 스킵"
            )
            return {"ok": True, "action": "skipped", "reason": "cooldown"}

        chat_id = self._store.chat_id
        if chat_id is None:
            return {"ok": False, "action": "skipped", "reason": "no_user_registered"}

        goals = self._store.today_goals
        goal = ", ".join(goals) if goals else None

        # 산만함 트리거는 LLM 에 한 번 더 '진짜 딴짓인지' 확인 (로컬 _on_trigger 동일 로직)
        if trigger_value == "산만함/널뛰기":
            if not freq:
                print("[App] (원격) 산만함 트리거에 window_freq 누락 — 스킵")
                return {"ok": True, "action": "skipped", "reason": "no_freq"}
            try:
                if not self._agent.judge_distracted(goal, freq):
                    print(f"[App] (원격) 산만함 판독: 업무 중 — 스킵 ({freq})")
                    return {"ok": True, "action": "skipped", "reason": "judged_focused"}
            except AIGenerationError as exc:
                print(f"[App] (원격) 산만함 판독 실패 — 스킵: {exc}")
                return {"ok": True, "action": "skipped", "reason": "ai_error"}
            print(f"[App] (원격) 산만함 판독: 딴짓 확정 ({freq})")

        description = self._describe_trigger(trigger_value, snap, goal)
        description += self._escalation_note()
        try:
            reply = self._agent.handle_event(description)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            return {"ok": False, "action": "failed", "reason": "ai_error"}

        self._consecutive_ai_failures = 0
        print(f"[App] (원격) 트리거 [{trigger_value}] → 잔소리 발송")
        sent_ok = self._send(chat_id, reply)
        self._arm_warning_timeout()
        with self._remote_cooldown_lock:
            self._remote_cooldowns[trigger_value] = (
                time.time() + REMOTE_TRIGGER_COOLDOWN_SEC
            )
        if sent_ok:
            self._store.bump_trigger_fire(trigger_value)
            # 자기학습 weak_spot 후보 — 폰 위성이 보낸 active_window (앱 패키지명)
            app_label = snap.get("active_window") or ""
            if app_label:
                self._store.bump_weak_spot_candidate(app_label)
            if not self._store.nag_policy_asked:
                self._store.nag_policy_asked = True
        return {"ok": True, "action": "nag_sent"}

    # ====================================================== 도구 콜백
    # (CoachAgent의 function calling이 대화 중 호출 → 여기로 들어옴)
    def _on_goal_set(self, goal: str) -> None:
        print(f"[App] 오늘 목표 저장: {goal}")
        self._start_tracking()

    def _on_today_complete(self) -> None:
        print("[App] 오늘 목표 완료")
        self._ignored_nags = 0
        self._cancel_warning_timeout()
        if self._tracker is not None:
            self._tracker.sleep()

    def _on_back_on_track(self) -> None:
        print("[App] 사용자 복귀")
        self._ignored_nags = 0
        self._cancel_warning_timeout()
        if self._tracker is not None:
            self._tracker.resume_normal()

    # =================================================== Warning 타임아웃
    def _arm_warning_timeout(self) -> None:
        """잔소리 후 사용자가 끝내 반응 없으면 감시가 멈춰버리지 않도록 안전장치."""
        with self._timer_lock:
            if self._warning_timer:
                self._warning_timer.cancel()
            self._warning_timer = threading.Timer(
                WARNING_TIMEOUT_SEC, self._on_warning_timeout
            )
            self._warning_timer.daemon = True
            self._warning_timer.start()

    def _cancel_warning_timeout(self) -> None:
        with self._timer_lock:
            if self._warning_timer:
                self._warning_timer.cancel()
                self._warning_timer = None

    def _on_warning_timeout(self) -> None:
        self._ignored_nags += 1
        print(
            f"[App] 잔소리 응답 없음 (누적 무시 {self._ignored_nags}회) "
            f"— 감시 정상화"
        )
        if self._tracker is not None:
            self._tracker.resume_normal()

    # ====================================================== 프로액티브
    def _proactive_loop(self) -> None:
        while not self._stop.wait(PROACTIVE_CHECK_INTERVAL):
            chat_id = self._store.chat_id
            if chat_id is None:
                continue
            # SLEEP(완료) 또는 WARNING(잔소리 중)이면 먼저 말 걸지 않는다.
            # Tracker 가 꺼져 있으면 상태 개념이 없으므로 그대로 진행한다.
            if self._tracker is not None and self._tracker.state != State.NORMAL:
                continue
            now = time.time()
            # 사용자가 오래 답이 없을 때만 — 봇이 보낸 메시지는 침묵 타이머에
            # 영향을 주지 않는다 (봇이 떠들어도 사용자가 잠잠하면 먼저 말 건다).
            if now - self._last_user_msg < PROACTIVE_IDLE_SEC:
                continue
            # 직전에 이미 먼저 말 걸었으면 또 보채지 않는다.
            if now - self._last_proactive < PROACTIVE_IDLE_SEC:
                continue
            self._last_proactive = now  # 성공·실패 무관하게 한동안 재시도 안 함
            # 과부하 자체 점검 우선 — 시그널 충족하면 평소 프로액티브 대신
            # '톤·목표 점검' 메시지를 띄운다 (자기결정권 보존, 자동 적용 X).
            if self._should_check_overload(now):
                try:
                    reply = self._agent.overload_checkin()
                except AIGenerationError as exc:
                    print(f"[App] 과부하 점검 생성 실패: {exc}")
                    continue
                print("[App] 과부하 점검 메시지 발송")
                if self._send(chat_id, reply):
                    self._store.last_overload_checkin = now
                continue
            try:
                reply = self._agent.proactive_checkin()
            except AIGenerationError as exc:
                print(f"[App] 프로액티브 생성 실패: {exc}")
                continue
            print("[App] 프로액티브 메시지 발송")
            self._send(chat_id, reply)

    def _should_check_overload(self, now: float) -> bool:
        """과부하 점검 메시지를 띄울 시그널 충족 여부.
        조건: 잔소리 누적 무시 ≥ N회 + 사용자가 24h 응답 없음 + 3일 쿨다운."""
        if self._ignored_nags < OVERLOAD_IGNORED_THRESHOLD:
            return False
        if now - self._last_user_msg < OVERLOAD_SILENCE_SEC:
            return False
        last = self._store.last_overload_checkin
        if last is not None and (now - last) < OVERLOAD_COOLDOWN_SEC:
            return False
        return True

    # ====================================================== 일정 리마인더
    def _reminder_loop(self) -> None:
        """Google 캘린더 + 봇 자체 일정 (store.events) 둘 다 챙긴다.
        곧 시작할 일정의 reminder_lead 분 전에 미리 알림을 띄운다."""
        reminded_google: set = set()  # Google 캘린더 이벤트 id 누적
        while not self._stop.wait(REMINDER_CHECK_INTERVAL):
            chat_id = self._store.chat_id
            if chat_id is None:
                continue

            # --- Google 캘린더 (인증된 경우만) ---
            if self._calendar is not None:
                try:
                    events = self._calendar.list_upcoming(max_results=10)
                except Exception as exc:
                    print(f"[App] 일정 조회 실패: {exc}")
                    events = []
                now_utc = datetime.datetime.now(datetime.timezone.utc)
                for ev in events:
                    eid = ev.get("id")
                    start = self._parse_event_start(ev.get("start"))
                    if not eid or start is None or eid in reminded_google:
                        continue
                    minutes = (start - now_utc).total_seconds() / 60.0
                    if 0 < minutes <= REMINDER_LEAD_MIN:
                        sent = self._send_event_reminder(
                            chat_id, ev.get("summary", "일정"), int(minutes)
                        )
                        if sent:  # 발송 성공 시에만 마크 — 실패 시 다음 폴링 재시도
                            reminded_google.add(eid)

            # --- 봇 자체 일정 (Google 인증 무관) ---
            # 발사 윈도우: [start_ts - lead_sec, start_ts + GRACE].
            # 폴링 간격(2분)보다 짧은 lead_min 또는 컨테이너 재시작으로 인한
            # 폴링 miss 를 보완하기 위해 시작 시각 후 일정 시간까지 grace.
            now_ts = time.time()
            for ev in self._store.events:
                if ev.get("reminded"):
                    continue
                start_ts = ev.get("start_ts")
                if start_ts is None:
                    continue
                lead_sec = (ev.get("reminder_lead_min", 15) or 15) * 60
                window_start = start_ts - lead_sec
                window_end = start_ts + REMINDER_GRACE_SEC
                if now_ts < window_start or now_ts > window_end:
                    continue
                minutes_left = max(0, int((start_ts - now_ts) / 60))
                sent = self._send_event_reminder(
                    chat_id, ev.get("summary", "일정"), minutes_left
                )
                if sent:  # 발송 성공 시에만 영구 마크 — 실패 시 다음 폴링 재시도
                    self._store.mark_event_reminded(ev["id"])

            # 지난 일정은 자동 정리 (메모리·저장 비대 방지)
            self._store.prune_past_events()

    def _send_event_reminder(
        self, chat_id: int, summary: str, minutes: int
    ) -> bool:
        """일정 미리 알림 발송. 발송 성공 여부를 반환한다 — 호출자는 이걸
        보고 reminded 마크 여부를 결정 (실패 시 마크 X → 다음 폴링 재시도)."""
        try:
            msg = self._agent.event_reminder(summary, minutes)
        except AIGenerationError as exc:
            print(f"[App] 리마인더 생성 실패 — 기본 문구 사용: {exc}")
            msg = f"곧 '{summary}' 시작이야 — {minutes}분 남았어!"
        print(f"[App] 일정 리마인더 발송: {summary} ({minutes}분 전)")
        return self._send(chat_id, msg)

    @staticmethod
    def _parse_event_start(value: Optional[str]):
        """캘린더 start 값을 datetime 으로. 종일 일정(날짜만)은 None."""
        if not value or "T" not in value:
            return None
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    # ====================================================== 예약 알람
    def _alarm_loop(self) -> None:
        """예약된 알람을 주기적으로 보고, 시간이 된 알람을 발송한다.
        매일 알람은 다음 날로 재예약. 일회성은 발송 성공 시 삭제, 실패 시
        retry_count 누적 후 ALARM_ONCE_MAX_RETRY 까지 다음 폴링에 재시도."""
        while not self._stop.wait(ALARM_CHECK_INTERVAL):
            chat_id = self._store.chat_id
            if chat_id is None:
                continue
            alarms = self._store.alarms
            now = time.time()
            if not any(a.get("next_ts", 0) <= now for a in alarms):
                continue
            kept = []
            for a in alarms:
                if a.get("next_ts", 0) > now:
                    kept.append(a)
                    continue
                sent_ok = self._fire_alarm(chat_id, a)
                if a.get("repeat") == "daily":
                    nt = a.get("next_ts", now)
                    while nt <= now:
                        nt += 86400.0
                    a["next_ts"] = nt
                    a["retry_count"] = 0  # 매일 알람은 발송 성공/실패 무관 다음 날로
                    kept.append(a)
                elif not sent_ok:
                    # once 알람 발송 실패 — 재시도 (cap 까지)
                    a["retry_count"] = (a.get("retry_count") or 0) + 1
                    if a["retry_count"] < ALARM_ONCE_MAX_RETRY:
                        kept.append(a)
                    else:
                        print(
                            f"[App] 알람 발송 {a['retry_count']}회 연속 실패 — "
                            f"포기: {a.get('text')}"
                        )
                # once 발송 성공 시 kept 에 안 넣어 삭제됨
            self._store.replace_alarms(kept)

    def _fire_alarm(self, chat_id: int, alarm: dict) -> bool:
        """알람 메시지 생성 후 발송. 발송 성공 여부를 반환 — 호출자가 보고
        once 알람의 retry/삭제 여부를 결정."""
        text = alarm.get("text", "알람")
        if text.startswith(FOCUS_END_MARKER):
            what = text[len(FOCUS_END_MARKER):].strip() or "집중 작업"
            try:
                msg = self._agent.focus_session_end(what)
            except AIGenerationError as exc:
                print(f"[App] focus 세션 종료 메시지 생성 실패: {exc}")
                msg = f"⏰ '{what}' 집중 세션 끝났어! 어땠어? 더 갈래, 쉴래, 끝낼래?"
            print(f"[App] focus 세션 종료 발송: {what}")
        else:
            try:
                msg = self._agent.deliver_alarm(text)
            except AIGenerationError as exc:
                print(f"[App] 알람 메시지 생성 실패 — 기본 문구 사용: {exc}")
                msg = f"⏰ 알람: {text}"
            print(f"[App] 알람 발송: {text}")
        return self._send(chat_id, msg)

    # ============================================== 원격 트리거 HTTP 서버
    def _start_http_server(self) -> None:
        """원격 트리거 HTTP 서버를 데몬 스레드로 띄운다. TRIGGER_SECRET 이
        설정돼 있을 때만 활성 — 시크릿 없이 띄우면 누구나 트리거를 발사할 수
        있으므로 안전을 위해 비활성. PORT 환경변수는 Railway 가 자동 주입한다."""
        secret = os.getenv("TRIGGER_SECRET", "").strip()
        if not secret:
            print("[App] TRIGGER_SECRET 미설정 — 원격 트리거 endpoint 비활성")
            return
        port = int(os.getenv("PORT", "8080"))
        handler_cls = type(
            "TriggerHandler",
            (_TriggerHTTPHandler,),
            {"coach_app": self, "trigger_secret": secret},
        )
        try:
            server = http.server.ThreadingHTTPServer(
                ("0.0.0.0", port), handler_cls
            )
        except Exception as exc:
            print(f"[App] HTTP 서버 시작 실패 (포트 {port}): {exc}")
            return
        threading.Thread(
            target=server.serve_forever, name="TriggerHTTP", daemon=True
        ).start()
        print(f"[App] 원격 트리거 HTTP 서버 시작 — 포트 {port}, POST /trigger")

    # ====================================================== 주간 회고
    def _weekly_review_loop(self) -> None:
        """매주 일요일 21시 한 번 주간 회고 대화를 시작한다 (이번 주 활동 데이터를
        끼워서). 컨테이너 재시작·PC 켜짐 등으로 슬롯을 놓치지 않게 5분 주기로
        체크하되, last_weekly_review 일자로 같은 일요일 중복 발송 방지."""
        while not self._stop.wait(WEEKLY_REVIEW_CHECK_INTERVAL):
            chat_id = self._store.chat_id
            if chat_id is None:
                continue
            now = datetime.datetime.now()
            if now.weekday() != 6:  # 0=월, 6=일
                continue
            if now.hour != WEEKLY_REVIEW_HOUR:
                continue
            today_s = now.date().isoformat()
            if self._store.last_weekly_review == today_s:
                continue  # 오늘 이미 발송됨
            try:
                reply = self._agent.weekly_review()
            except AIGenerationError as exc:
                print(f"[App] 주간 회고 생성 실패: {exc}")
                continue
            print("[App] 주간 회고 발송")
            if self._send(chat_id, reply):
                self._store.last_weekly_review = today_s
                # 자기학습 weak_spot 후보 카운터 reset — 새 주 시작
                self._store.reset_weak_spot_candidates()

    # ====================================================== 매일 재시작
    def _daily_restart_loop(self) -> None:
        """매일 새벽 한 번 컨테이너를 자살시켜 Railway가 다시 띄우게 한다.
        장기 실행 시 누적되는 알 수 없는 문제(네트워크 세션, FD, 메모리 등)를
        깨끗이 리셋하는 안전망. 사용자가 자고 있을 새벽 시간에만 동작."""
        while not self._stop.wait(DAILY_RESTART_CHECK_INTERVAL):
            uptime_hours = (time.time() - self._started_at) / 3600.0
            if uptime_hours < DAILY_RESTART_MIN_UPTIME_HOURS:
                continue
            if datetime.datetime.now().hour == DAILY_RESTART_HOUR:
                print(
                    f"[App] 매일 재시작 시각({DAILY_RESTART_HOUR}시) 도달 "
                    f"(uptime {uptime_hours:.1f}h) — 컨테이너 재시작 유도 (exit 1)",
                    flush=True,
                )
                os._exit(1)

    # ========================================================= helpers
    def _start_tracking(self) -> None:
        """추적 시작. 이미 돌고 있으면 무시되고, SLEEP 상태면 NORMAL 로 깨운다.
        ENABLE_PC_TRACKER 가 꺼져 있으면 아무것도 하지 않는다."""
        if self._tracker is None:
            return
        self._tracker.start()
        self._tracker.wake()

    def _note_ai_failure(self, chat_id: int, exc: Exception) -> None:
        self._consecutive_ai_failures += 1
        print(f"[App] AI 실패 #{self._consecutive_ai_failures}: {exc}")
        if self._consecutive_ai_failures >= AI_FAILURE_WARN_THRESHOLD:
            self._send(
                chat_id,
                "(시스템) AI 응답이 계속 실패하고 있어. "
                "GEMINI_API_KEY·쿼터·네트워크를 확인해줘.",
            )
            self._consecutive_ai_failures = 0

    def _send(self, chat_id: int, text: str) -> bool:
        """텔레그램 발송 wrapper. 연속 실패가 임계치를 넘으면 컨테이너 자살 →
        Railway가 자동 재시작 (사람 개입 없이 누적 네트워크 문제 복구)."""
        ok = self._tg.send_message(chat_id, text)
        if ok:
            self._consecutive_tg_failures = 0
            return True
        self._consecutive_tg_failures += 1
        if self._consecutive_tg_failures >= TG_FAILURE_EXIT_THRESHOLD:
            print(
                f"[App] 텔레그램 발송 {self._consecutive_tg_failures}회 연속 실패 "
                f"— 컨테이너 재시작 유도 (exit 1)",
                flush=True,
            )
            os._exit(1)
        return False


def main() -> None:
    try:
        CoachApp().run()
    except Exception as exc:
        print(f"[App] 시작 실패: {exc}")


if __name__ == "__main__":
    main()
