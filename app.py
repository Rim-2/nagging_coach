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
from typing import List, Optional

from dotenv import load_dotenv

from agent_tools import FOCUS_END_MARKER
from ai_engine import AIGenerationError, CoachAgent
from app_http import PHONE_APP_LABELS, HttpServerMixin
from app_loops import LoopsMixin
from app_messaging import MessagingMixin
from app_triggers import TriggersMixin
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
AI_FAILURE_WARN_THRESHOLD = 3     # 연속 N회 실패 시 사용자에게 시스템 경고
RESET_CONFIRM_TTL = 60.0          # /reset 첫 명령 후 N초 안에 한 번 더 보내야 실행
CHAT_DEBOUNCE_SEC = 1.5           # 사용자 chat 짧은 간격 우다다 → 묶어서 한 번에 답장
# 그 외 루프 관련 상수 (PROACTIVE_*·REMINDER_*·ALARM_*·OVERLOAD_*·WEEKLY_REVIEW_*·
# DAILY_RESTART_*·DAILY_JOURNAL_* 등) 는 app_loops.LoopsMixin 의 class attribute 로 묶임.


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
    "/reset all — 통계·설정·약점 학습까지 전부 비우기 (처음 만난 상태로)\n"
)
# 그 외 자동 복구·루프 관련 상수는 mixin 모듈로 분리:
#   - 텔레그램 연속 실패 시 컨테이너 자살: app_messaging (TG_FAILURE_EXIT_THRESHOLD)
#   - 매일 새벽 재시작: app_loops (DAILY_RESTART_*)
#   - 원격 트리거 쿨다운: app_messaging (REMOTE_TRIGGER_COOLDOWN_SEC)
#   - 과부하 자체 점검: app_loops (OVERLOAD_*)

class CoachApp(HttpServerMixin, MessagingMixin, TriggersMixin, LoopsMixin):
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
        # /reset 확인 쿠션 — 첫 명령은 안내 메시지, 60초 안에 같은 명령 한 번 더
        # 받아야 실제 실행. 사용자 사고 방지용. None 또는 {"kind", "at"}.
        self._pending_reset: Optional[dict] = None
        # 최근 트리거 이력 (복합 트리거 룰 평가용). 메모리 deque, 봇 재시작 시 리셋.
        # 항목: {"device": "pc"|"phone", "trigger": str, "at": float}
        from collections import deque
        self._recent_triggers = deque(maxlen=20)
        self._recent_triggers_lock = threading.Lock()
        # Chat debounce — 사용자가 짧은 간격에 여러 메시지 보내면 묶어서 한 번에
        # LLM 호출 + 답장. 사람처럼 자연스럽게.
        self._chat_buffer: List[str] = []
        self._chat_buffer_lock = threading.Lock()
        self._chat_debounce_timer: Optional[threading.Timer] = None
        # retry 큐 모니터링 — 임계 초과 시 시스템 알림, 쿨다운으로 도배 방지
        self._last_retry_queue_alert: float = 0.0

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
        threading.Thread(
            target=self._retry_loop, name="Retry", daemon=True
        ).start()
        threading.Thread(
            target=self._risk_predict_loop, name="RiskPredict", daemon=True
        ).start()
        threading.Thread(
            target=self._daily_journal_loop, name="DailyJournal", daemon=True
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

        # /reset 펜딩 자동 취소 — 사용자가 같은 명령을 다시 보내지 않고 다른
        # 메시지를 보내면 그 자체가 '취소' 의도. 짧게 알려주고 흐름 이어감.
        if self._pending_reset is not None and not text.startswith("/reset"):
            kind = self._pending_reset.get("kind")
            self._pending_reset = None
            cmd = "/reset all" if kind == "hard" else "/reset"
            self._send(chat_id, f"({cmd} 취소 — 다른 메시지를 받았어)")

        # 사진·명령 등 *별도 흐름*이 들어오기 전에 chat debounce 버퍼를 비운다.
        # 우다다 채팅 후 곧이은 사진/명령이 chat 답변보다 먼저 가서 순서가
        # 뒤집히지 않도록 명시적 flush.
        if (photo or text.startswith("/")) and self._chat_buffer:
            self._flush_chat_buffer(chat_id, immediate=True)

        if text.startswith("/help"):
            self._send(chat_id, _HELP_TEXT)
            return

        if text.startswith("/export"):
            self._send(chat_id, self._build_export())
            return

        if text.startswith("/reset"):
            # `/reset all` 은 통계·설정·자기학습까지 전부 비움 — 위험 작업이라
            # 명시 인자 필수. `/reset` 만 쓰면 기존 동작 (대화·목표·습관만).
            # 두 단계 확인: 첫 명령은 안내, 60초 안에 같은 명령 한 번 더 받아야 실행.
            hard = text.strip().lower() in ("/reset all", "/reset hard", "/resetall")
            kind = "hard" if hard else "soft"
            now = time.time()
            pending = self._pending_reset
            confirmed = (
                pending is not None
                and pending.get("kind") == kind
                and (now - float(pending.get("at", 0.0))) < RESET_CONFIRM_TTL
            )
            if confirmed:
                self._pending_reset = None
                self._agent.reset(hard=hard)
                if hard:
                    msg = (
                        "전부 초기화했어 — 통계·설정·약점 학습까지 다 비웠어. "
                        "처음 만난 것처럼 시작하자."
                    )
                else:
                    msg = "기억 싹 비웠어. 자, 다시 시작하자 — 오늘 뭐 할 거야?"
            else:
                self._pending_reset = {"kind": kind, "at": now}
                if hard:
                    msg = (
                        "⚠️ 정말 *전부* 비우려고? 통계·설정·약점 학습까지 다 날아가. "
                        "확실하면 60초 안에 `/reset all` 을 한 번 더 보내. "
                        "그 사이에 다른 메시지를 보내면 자동 취소돼."
                    )
                else:
                    msg = (
                        "정말 비울거야? 대화·목표·습관·일정이 사라져 "
                        "(설정·통계는 유지). 확실하면 60초 안에 `/reset` 을 "
                        "한 번 더 보내. 다른 메시지를 보내면 자동 취소돼."
                    )
            self._send(chat_id, msg)
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
        """사용자 chat 메시지 도착 → 짧은 debounce 후 LLM 호출. 같은 debounce
        창 안에 추가 메시지가 오면 버퍼에 누적되어 *한 번에* 묶여 답장된다."""
        self._last_user_msg = time.time()
        with self._chat_buffer_lock:
            self._chat_buffer.append(text)
            # 기존 타이머 취소 후 새 타이머 — 우다다 도착 시 매번 만료 시각 갱신
            if self._chat_debounce_timer is not None:
                self._chat_debounce_timer.cancel()
            timer = threading.Timer(
                CHAT_DEBOUNCE_SEC,
                self._flush_chat_buffer,
                args=(chat_id,),
            )
            timer.daemon = True
            self._chat_debounce_timer = timer
            timer.start()

    def _flush_chat_buffer(self, chat_id: int, *, immediate: bool = False) -> None:
        """버퍼에 쌓인 사용자 메시지들을 합쳐 한 번에 LLM 호출 + 답장.
        immediate=True 면 photo·command 등 *별도 흐름*이 들어오기 직전 호출돼서
        타이머 만료를 기다리지 않고 즉시 비운다."""
        with self._chat_buffer_lock:
            if self._chat_debounce_timer is not None:
                self._chat_debounce_timer.cancel()
                self._chat_debounce_timer = None
            if not self._chat_buffer:
                return
            messages = list(self._chat_buffer)
            self._chat_buffer = []
        if immediate:
            print(f"[App] chat buffer 즉시 flush ({len(messages)}건)")
        elif len(messages) > 1:
            print(f"[App] chat buffer flush — {len(messages)}건 묶어서 처리")
        # 여러 통이면 줄바꿈으로 합쳐 한 컨텍스트로 전달. LLM 은 한 사람이 연달아
        # 보낸 메시지 묶음으로 자연스럽게 인식한다.
        combined = "\n".join(messages) if len(messages) > 1 else messages[0]
        try:
            reply = self._agent.chat(combined)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            return
        self._consecutive_ai_failures = 0
        if self._send(chat_id, reply):
            # 직전 turn 에 못 보낸 답장이 있었다면 새 답장에 통합되어 갔으니 클리어.
            self._store.clear_pending_chat_reply()
        else:
            # chat 답장은 retry 큐 X — 시간 지나서 따로 도착하면 어색.
            # 대신 다음 사용자 메시지 때 LLM 이 합쳐 답하도록 보관.
            print("[App] chat 답장 전송 실패 → pending_chat_reply 보관")
            self._store.set_pending_chat_reply(reply)

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
        if self._send(chat_id, reply):
            self._store.clear_pending_chat_reply()
        else:
            print("[App] 사진 답장 전송 실패 → pending_chat_reply 보관")
            self._store.set_pending_chat_reply(reply)

    # _on_trigger / handle_remote_trigger / _describe_trigger /
    # _evaluate_compound_trigger / _record_recent_trigger / _COMPOUND_* 는
    # app_triggers.TriggersMixin 으로 분리됨.


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

    # 모든 백그라운드 루프 (_proactive_loop, _reminder_loop, _alarm_loop,
    # _risk_predict_loop, _daily_journal_loop, _weekly_review_loop,
    # _daily_restart_loop) 와 관련 헬퍼 (_should_check_overload, _fire_alarm,
    # _send_event_reminder, _parse_event_start) 는 app_loops.LoopsMixin 으로 분리됨.


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

    # _send 는 app_messaging.MessagingMixin 으로 분리됨.


def main() -> None:
    try:
        CoachApp().run()
    except Exception as exc:
        print(f"[App] 시작 실패: {exc}")


if __name__ == "__main__":
    main()
