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
import os
import sys
import threading
import time
from typing import Optional

from dotenv import load_dotenv

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
ALARM_CHECK_INTERVAL = 30.0       # 예약 알람 점검 주기


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
        self._ignored_nags = 0               # 연속으로 무시당한 잔소리 횟수

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
        if self._calendar is not None:
            threading.Thread(
                target=self._reminder_loop, name="Reminder", daemon=True
            ).start()
        threading.Thread(
            target=self._alarm_loop, name="Alarm", daemon=True
        ).start()

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
            self._tg.send_message(
                chat_id, "먼저 /start 를 보내줘. 그래야 너랑 짝이 될 수 있어."
            )
            return
        if chat_id != registered:
            return  # 1인용 봇 — 등록된 사용자만 응대

        if text.startswith("/reset"):
            self._agent.reset()
            self._tg.send_message(
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
        self._tg.send_message(
            chat_id,
            "안녕! 난 너의 잔소리 코치야 😎\n"
            "앞으로 여기서 같이 떠들면서 오늘 할 일 챙겨줄게. "
            + pc_note
            + "자, 오늘 뭐 할 거야?",
        )

    def _handle_chat(self, chat_id: int, text: str) -> None:
        self._last_user_msg = time.time()
        try:
            reply = self._agent.chat(text)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            return
        self._consecutive_ai_failures = 0
        self._tg.send_message(chat_id, reply)

    def _handle_photo(self, chat_id: int, photo: list, caption: str) -> None:
        """완료 증거 사진을 받아 AI 비전 판독으로 처리한다."""
        self._last_user_msg = time.time()
        try:
            file_id = photo[-1]["file_id"]  # 마지막 = 가장 큰 해상도
            image = self._tg.download_file(file_id)
        except Exception as exc:
            print(f"[App] 사진 다운로드 실패: {exc}")
            self._tg.send_message(chat_id, "사진을 못 받았어 — 다시 보내줄래?")
            return
        try:
            reply = self._agent.verify_completion(image, caption)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            return
        self._consecutive_ai_failures = 0
        self._tg.send_message(chat_id, reply)

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

        description = self._describe_trigger(trigger, snap, goal)
        if self._ignored_nags >= 1:
            description += (
                f" (사용자가 잔소리를 이미 {self._ignored_nags}번 무시하고 "
                f"또 딴짓 중 — 점점 더 세게, 매번 다른 각도로 쪼아라.)"
            )
        try:
            reply = self._agent.handle_event(description)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            self._tracker.resume_normal()
            return

        self._consecutive_ai_failures = 0
        print(f"[App] 트리거 [{trigger.value}] → 잔소리 발송")
        self._tg.send_message(chat_id, reply)
        self._arm_warning_timeout()

    @staticmethod
    def _describe_trigger(
        trigger: TriggerType, snap: Snapshot, goal: Optional[str]
    ) -> str:
        win = snap.active_window or "(알 수 없는 창)"
        idle_min = int(snap.idle_time // 60)

        # 수면·휴식 잔소리는 목표와 무관 — 본문만 돌려준다.
        if trigger == TriggerType.LATE_NIGHT:
            hour = datetime.datetime.now().hour
            return (
                f"지금 새벽 {hour}시인데 사용자가 아직 PC 앞에서 안 자고 있어. "
                f"수면 챙기라고 한마디 해줘."
            )
        if trigger == TriggerType.OVERWORK:
            return (
                "사용자가 2시간 넘게 쉬는 틈도 없이 계속 화면 앞에 붙어 있어. "
                "눈도 몸도 지칠 텐데 — 잠깐 쉬라고 챙겨줘."
            )

        goal_note = (
            f" 참고로 오늘 목표는 '{goal}'."
            if goal
            else " 심지어 오늘 목표도 아직 안 정했어."
        )
        if trigger == TriggerType.DOPAMINE_ZOMBIE:
            body = (
                f"사용자가 '{win}' 화면을 {idle_min}분째 입력도 없이 "
                f"멍하니 보고 있어 (도파민 좀비)."
            )
        elif trigger == TriggerType.ACTIVE_SCROLL:
            body = (
                f"사용자가 '{win}'을 15분 넘게 손 안 떼고 계속 스크롤하며 "
                f"도파민 서핑 중이야 (능동적 딴짓)."
            )
        elif trigger == TriggerType.DISTRACTED_SWITCHING:
            body = (
                f"사용자가 5분 사이 창을 {snap.switch_count}번이나 정신없이 "
                f"옮겨다니고 있어 (산만함/널뛰기). 지금 창은 '{win}'."
            )
        elif trigger == TriggerType.FAKE_WORKING:
            body = (
                f"사용자가 업무앱 '{win}'을 켜놓고 {idle_min}분째 아무 입력도 "
                f"없이 가만히 있어 (가짜 일하기)."
            )
        elif trigger == TriggerType.PERSONAL_WEAKNESS:
            body = (
                f"사용자가 평소 자주 무너진다던 '{win}'에 또 빠졌어 "
                f"(개인 약점 앱) — 약점인 거 콕 집어서 잔소리해."
            )
        else:  # OVER_IMMERSION
            body = (
                f"사용자가 '{win}'(게임/메신저류)에 30분 넘게 고강도로 "
                f"빠져 있어 (과몰입 딴짓)."
            )
        return body + goal_note

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
            try:
                reply = self._agent.proactive_checkin()
            except AIGenerationError as exc:
                print(f"[App] 프로액티브 생성 실패: {exc}")
                continue
            print("[App] 프로액티브 메시지 발송")
            self._tg.send_message(chat_id, reply)

    # ====================================================== 일정 리마인더
    def _reminder_loop(self) -> None:
        """캘린더를 주기적으로 보고, 곧 시작할 일정을 미리 알려준다."""
        reminded = set()
        while not self._stop.wait(REMINDER_CHECK_INTERVAL):
            chat_id = self._store.chat_id
            if chat_id is None or self._calendar is None:
                continue
            try:
                events = self._calendar.list_upcoming(max_results=10)
            except Exception as exc:
                print(f"[App] 일정 조회 실패: {exc}")
                continue
            now = datetime.datetime.now(datetime.timezone.utc)
            for ev in events:
                eid = ev.get("id")
                start = self._parse_event_start(ev.get("start"))
                if not eid or start is None or eid in reminded:
                    continue
                minutes = (start - now).total_seconds() / 60.0
                if 0 < minutes <= REMINDER_LEAD_MIN:
                    reminded.add(eid)
                    self._send_event_reminder(
                        chat_id, ev.get("summary", "일정"), int(minutes)
                    )

    def _send_event_reminder(
        self, chat_id: int, summary: str, minutes: int
    ) -> None:
        try:
            msg = self._agent.event_reminder(summary, minutes)
        except AIGenerationError as exc:
            print(f"[App] 리마인더 생성 실패 — 기본 문구 사용: {exc}")
            msg = f"곧 '{summary}' 시작이야 — {minutes}분 남았어!"
        print(f"[App] 일정 리마인더 발송: {summary} ({minutes}분 전)")
        self._tg.send_message(chat_id, msg)

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
        일회성은 발송 후 삭제, 매일 알람은 다음 날로 재예약한다."""
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
                self._fire_alarm(chat_id, a)
                if a.get("repeat") == "daily":
                    nt = a.get("next_ts", now)
                    while nt <= now:
                        nt += 86400.0
                    a["next_ts"] = nt
                    kept.append(a)
                # 일회성(once) 은 kept 에 안 넣어 발송 후 삭제됨
            self._store.replace_alarms(kept)

    def _fire_alarm(self, chat_id: int, alarm: dict) -> None:
        text = alarm.get("text", "알람")
        try:
            msg = self._agent.deliver_alarm(text)
        except AIGenerationError as exc:
            print(f"[App] 알람 메시지 생성 실패 — 기본 문구 사용: {exc}")
            msg = f"⏰ 알람: {text}"
        print(f"[App] 알람 발송: {text}")
        self._tg.send_message(chat_id, msg)

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
            self._tg.send_message(
                chat_id,
                "(시스템) AI 응답이 계속 실패하고 있어. "
                "GEMINI_API_KEY·쿼터·네트워크를 확인해줘.",
            )
            self._consecutive_ai_failures = 0


def main() -> None:
    try:
        CoachApp().run()
    except Exception as exc:
        print(f"[App] 시작 실패: {exc}")


if __name__ == "__main__":
    main()
