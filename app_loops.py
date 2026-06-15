"""app_loops — 백그라운드 루프 책임 분리.

CoachApp 의 데몬 스레드들을 한 mixin 으로 모은다:
  - _proactive_loop:     1시간 침묵 시 코치가 먼저 말 걸기 (+overload checkin)
  - _reminder_loop:      Google 캘린더 + 봇 자체 일정 시작 직전 알림
  - _alarm_loop:         예약 알람 발사 (매일/일회성)
  - _risk_predict_loop:  위험 시간대 직전 선제 알림 (1일 1회)
  - _daily_journal_loop: 매일 22시 한 줄 회고 유도
  - _weekly_review_loop: 매주 일요일 21시 주간 회고 ritual
  - _daily_restart_loop: 매일 새벽 컨테이너 자살 → Railway 자동 재시작
  - _fire_alarm, _send_event_reminder, _should_check_overload (헬퍼)

루프 관련 상수는 모두 mixin class attribute 로 묶어 *책임 응집* 을 명시한다.
의존: self._store, self._tracker, self._agent, self._calendar,
      self._stop, self._last_user_msg, self._last_proactive, self._ignored_nags,
      self._started_at + MessagingMixin 의 _send_or_enqueue.
"""

from __future__ import annotations

import datetime
import os
import time
from typing import Optional

from agent_tools import FOCUS_END_MARKER
from ai_engine import AIGenerationError
from tracker import State


# inline keyboard — mood 1~5 빠른 응답 버튼. callback_data="mood:N"
# 사용자가 탭하면 app._handle_callback_query 가 store.add_mood_log(N) 호출.
MOOD_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "😞 1", "callback_data": "mood:1"},
        {"text": "😕 2", "callback_data": "mood:2"},
        {"text": "😐 3", "callback_data": "mood:3"},
        {"text": "🙂 4", "callback_data": "mood:4"},
        {"text": "😄 5", "callback_data": "mood:5"},
    ]]
}


class LoopsMixin:
    """모든 백그라운드 데몬 루프를 담는 mixin."""

    # ============================================ Loops 전용 상수 (class attribute)
    # 프로액티브 — 1시간 침묵 시 먼저 말 걸기
    PROACTIVE_IDLE_SEC = 3600.0
    PROACTIVE_CHECK_INTERVAL = 120.0
    # 야간엔 먼저 말 걸지 않는다 (자는 시간 — 새벽 '뭐해' 핑 방지) [start, end)
    PROACTIVE_NIGHT_START_HOUR = 0
    PROACTIVE_NIGHT_END_HOUR = 7
    # 일정 리마인더
    REMINDER_LEAD_MIN = 15.0
    REMINDER_CHECK_INTERVAL = 120.0
    REMINDER_GRACE_SEC = 600.0
    # 알람
    ALARM_CHECK_INTERVAL = 30.0
    ALARM_ONCE_MAX_RETRY = 3
    # 과부하 자체 점검 — 잔소리 5회 무시 + 24h 무응답 + 3일 쿨다운
    OVERLOAD_IGNORED_THRESHOLD = 5
    OVERLOAD_SILENCE_SEC = 86400.0
    OVERLOAD_COOLDOWN_SEC = 86400.0 * 3
    # 매일 재시작 (Railway 자동 재시작 이용)
    DAILY_RESTART_HOUR = 4
    DAILY_RESTART_MIN_UPTIME_HOURS = 23
    DAILY_RESTART_CHECK_INTERVAL = 300.0
    # 주간 회고 (일요일 21시)
    WEEKLY_REVIEW_HOUR = 21
    WEEKLY_REVIEW_CHECK_INTERVAL = 300.0
    # 위험 시간대 예측 — 1일 1회, 직전 15분, 임계 3회+
    _RISK_PREDICT_CHECK_INTERVAL = 300.0
    _RISK_PREDICT_LEAD_MIN = 15
    _RISK_PREDICT_MIN_FIRES = 3
    # 하루 마무리 일지 — 매일 22시
    DAILY_JOURNAL_HOUR = 22
    DAILY_JOURNAL_CHECK_INTERVAL = 300.0

    # ====================================================== 프로액티브
    def _proactive_loop(self) -> None:
        while not self._stop.wait(self.PROACTIVE_CHECK_INTERVAL):
            chat_id = self._store.chat_id
            if chat_id is None:
                continue
            # 야간(자는 시간)엔 먼저 말 걸지 않는다 — 새벽 '뭐해 조용하네' 핑은
            # 무의미하고 수면 방해. 늦은 밤 '자라' 잔소리는 별도 트리거가 담당.
            hour = datetime.datetime.now().hour
            if self.PROACTIVE_NIGHT_START_HOUR <= hour < self.PROACTIVE_NIGHT_END_HOUR:
                continue
            # SLEEP(완료) 또는 WARNING(잔소리 중)이면 먼저 말 걸지 않는다.
            # Tracker 가 꺼져 있으면 상태 개념이 없으므로 그대로 진행한다.
            if self._tracker is not None and self._tracker.state != State.NORMAL:
                continue
            now = time.time()
            # 사용자가 오래 답이 없을 때만 — 봇이 보낸 메시지는 침묵 타이머에
            # 영향을 주지 않는다 (봇이 떠들어도 사용자가 잠잠하면 먼저 말 건다).
            if now - self._last_user_msg < self.PROACTIVE_IDLE_SEC:
                continue
            # 직전에 이미 먼저 말 걸었으면 또 보채지 않는다.
            if now - self._last_proactive < self.PROACTIVE_IDLE_SEC:
                continue
            self._last_proactive = now
            # quiet_mode 체크는 idle gate 를 통과한 *여기서* — 실제로 먼저 말 걸
            # 시점에만 보류 카운트를 올린다 (루프 tick 마다 세면 시간당 30건씩
            # 폭증). last_proactive 를 이미 갱신했으니 보류해도 다음은 1시간 뒤.
            if self._quiet_mode_block():
                continue
            # 과부하 자체 점검 우선 — 시그널 충족하면 평소 프로액티브 대신
            # '톤·목표 점검' 메시지를 띄운다 (자기결정권 보존, 자동 적용 X).
            if self._should_check_overload(now):
                try:
                    reply = self._agent.overload_checkin()
                except AIGenerationError as exc:
                    print(f"[App] 과부하 점검 생성 실패: {exc}")
                    continue
                print("[App] 과부하 점검 메시지 발송")
                self._send_or_enqueue(chat_id, reply, kind="overload_checkin")
                continue
            try:
                reply = self._agent.proactive_checkin()
            except AIGenerationError as exc:
                print(f"[App] 프로액티브 생성 실패: {exc}")
                continue
            print("[App] 프로액티브 메시지 발송")
            self._send_or_enqueue(chat_id, reply, kind="proactive_checkin")

    def _should_check_overload(self, now: float) -> bool:
        """과부하 점검 메시지를 띄울 시그널 충족 여부.
        조건: 잔소리 누적 무시 ≥ N회 + 사용자가 24h 응답 없음 + 3일 쿨다운."""
        if self._ignored_nags < self.OVERLOAD_IGNORED_THRESHOLD:
            return False
        if now - self._last_user_msg < self.OVERLOAD_SILENCE_SEC:
            return False
        last = self._store.last_overload_checkin
        if last is not None and (now - last) < self.OVERLOAD_COOLDOWN_SEC:
            return False
        return True

    # ====================================================== 일정 리마인더
    def _reminder_loop(self) -> None:
        """Google 캘린더 + 봇 자체 일정 (store.events) 둘 다 챙긴다.
        곧 시작할 일정의 reminder_lead 분 전에 미리 알림을 띄운다."""
        reminded_google: set = set()  # Google 캘린더 이벤트 id 누적
        while not self._stop.wait(self.REMINDER_CHECK_INTERVAL):
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
                    if 0 < minutes <= self.REMINDER_LEAD_MIN:
                        self._send_event_reminder(
                            chat_id, ev.get("summary", "일정"), int(minutes)
                        )
                        reminded_google.add(eid)

            # --- 봇 자체 일정 (Google 인증 무관) ---
            now_ts = time.time()
            for ev in self._store.events:
                if ev.get("reminded"):
                    continue
                start_ts = ev.get("start_ts")
                if start_ts is None:
                    continue
                lead_sec = (ev.get("reminder_lead_min", 15) or 15) * 60
                window_start = start_ts - lead_sec
                window_end = start_ts + self.REMINDER_GRACE_SEC
                if now_ts < window_start or now_ts > window_end:
                    continue
                minutes_left = max(0, int((start_ts - now_ts) / 60))
                self._send_event_reminder(
                    chat_id,
                    ev.get("summary", "일정"),
                    minutes_left,
                    event_id=ev["id"],
                )

            self._store.prune_past_events()

    def _send_event_reminder(
        self,
        chat_id: int,
        summary: str,
        minutes: int,
        *,
        event_id: Optional[str] = None,
    ) -> bool:
        """일정 미리 알림 발송. 첫 시도 성공 또는 큐 적재 둘 다 True 로 간주
        (큐가 도달까지 책임짐). event_id 가 주어지면 큐 도달 시점에 자체 일정
        영구 마크가 자동 적용된다."""
        try:
            msg = self._agent.event_reminder(summary, minutes)
        except AIGenerationError as exc:
            print(f"[App] 리마인더 생성 실패 — 기본 문구 사용: {exc}")
            msg = f"곧 '{summary}' 시작이야 — {minutes}분 남았어!"
        print(f"[App] 일정 리마인더 발송: {summary} ({minutes}분 전)")
        se = {"event_id": event_id} if event_id else None
        self._send_or_enqueue(chat_id, msg, kind="event_reminder", side_effects=se)
        return True

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
        while not self._stop.wait(self.ALARM_CHECK_INTERVAL):
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
                    a["retry_count"] = 0
                    kept.append(a)
                elif not sent_ok:
                    a["retry_count"] = (a.get("retry_count") or 0) + 1
                    if a["retry_count"] < self.ALARM_ONCE_MAX_RETRY:
                        kept.append(a)
                    else:
                        print(
                            f"[App] 알람 발송 {a['retry_count']}회 연속 실패 — "
                            f"포기: {a.get('text')}"
                        )
            self._store.replace_alarms(kept)

    def _fire_alarm(self, chat_id: int, alarm: dict) -> bool:
        """알람 메시지 생성 후 발송. retry 큐가 도달 책임을 지므로 항상 True 를
        리턴 — '한 번은 보냈다'고 마킹해 once 알람은 큐에 위임 후 즉시 삭제."""
        text = alarm.get("text", "알람")
        if text.startswith(FOCUS_END_MARKER):
            what = text[len(FOCUS_END_MARKER):].strip() or "집중 작업"
            try:
                msg = self._agent.focus_session_end(what)
            except AIGenerationError as exc:
                print(f"[App] focus 세션 종료 메시지 생성 실패: {exc}")
                msg = f"⏰ '{what}' 집중 세션 끝났어! 어땠어? 더 갈래, 쉴래, 끝낼래?"
            print(f"[App] focus 세션 종료 발송: {what}")
            self._send_or_enqueue(chat_id, msg, kind="focus_session_end")
        else:
            try:
                msg = self._agent.deliver_alarm(text)
            except AIGenerationError as exc:
                print(f"[App] 알람 메시지 생성 실패 — 기본 문구 사용: {exc}")
                msg = f"⏰ 알람: {text}"
            print(f"[App] 알람 발송: {text}")
            self._send_or_enqueue(chat_id, msg, kind="alarm")
        return True

    # ====================================================== 위험 시간대 예측
    def _risk_predict_loop(self) -> None:
        """다음 시각이 시간대 매핑 상 위험 시간대면 직전에 선제 알림. 부담 완화
        위해 1일 1회 + 최소 임계치 + 활성 톤이 gentle 이 아닐 때만."""
        while not self._stop.wait(self._RISK_PREDICT_CHECK_INTERVAL):
            chat_id = self._store.chat_id
            if chat_id is None:
                continue
            if self._store.active_nag_policy == "gentle":
                continue
            if self._store.risk_predict_already_fired_today():
                continue
            now = datetime.datetime.now()
            next_hour = (now.hour + 1) % 24
            minutes_to_next = 60 - now.minute
            if minutes_to_next > self._RISK_PREDICT_LEAD_MIN:
                continue
            try:
                hb = self._store.hourly_breakdown(days=14)
            except Exception as exc:
                print(f"[App] 위험 예측 hourly_breakdown 실패: {exc}")
                continue
            if hb.get("total_days_with_data", 0) < 3:
                continue
            risk_hours = hb.get("risk_hours") or []
            target = None
            for h, c in risk_hours:
                if int(h) == int(next_hour) and int(c) >= self._RISK_PREDICT_MIN_FIRES:
                    target = (int(h), int(c))
                    break
            if target is None:
                continue
            # quiet_mode 체크는 '실제로 발사할' 이 시점에서 — 보류해도 오늘 1회
            # 발사한 걸로 마크해 5분마다 재시도/중복 카운트되는 걸 막는다.
            if self._quiet_mode_block():
                self._store.mark_risk_predict_fired(target[0])
                continue
            weekly = self._store.weekly_summary(days=14)
            top_label = (weekly.get("recent") or {}).get("top_trigger") or "딴짓"
            try:
                reply = self._agent.risk_predict(target[0], top_label, target[1])
            except AIGenerationError as exc:
                print(f"[App] 위험 예측 메시지 생성 실패: {exc}")
                continue
            print(f"[App] 위험 예측 발송: {target[0]}시 ({target[1]}회 누적)")
            self._send_or_enqueue(chat_id, reply, kind="risk_predict")
            self._store.mark_risk_predict_fired(target[0])

    # ===================================================== 하루 마무리 일지
    def _daily_journal_loop(self) -> None:
        """매일 정해진 시간에 한 줄 회고 유도 메시지 발사. 같은 날 중복 발송
        가드 + retry 큐가 도달 책임짐."""
        while not self._stop.wait(self.DAILY_JOURNAL_CHECK_INTERVAL):
            chat_id = self._store.chat_id
            if chat_id is None:
                continue
            now = datetime.datetime.now()
            if now.hour != self.DAILY_JOURNAL_HOUR:
                continue
            today_s = now.date().isoformat()
            if self._store.last_daily_journal == today_s:
                continue
            if self._quiet_mode_block():
                # 보류해도 오늘은 처리한 걸로 — 안 그러면 그 시각 내내 5분마다
                # 다시 와서 보류 카운트가 ~12배로 부풀고, 해제 후 늦게 발사됨.
                self._store.last_daily_journal = today_s
                continue
            try:
                reply = self._agent.daily_journal()
            except AIGenerationError as exc:
                print(f"[App] 하루 마무리 일지 생성 실패: {exc}")
                continue
            print("[App] 하루 마무리 일지 발송")
            self._store.last_daily_journal = today_s
            self._send_or_enqueue(
                chat_id, reply, kind="daily_journal", reply_markup=MOOD_KEYBOARD,
            )

    # ====================================================== 주간 회고
    def _weekly_review_loop(self) -> None:
        """매주 일요일 21시 한 번 주간 회고 대화를 시작한다. 같은 일요일 중복
        발송은 last_weekly_review (날짜 문자열) 로 차단."""
        while not self._stop.wait(self.WEEKLY_REVIEW_CHECK_INTERVAL):
            chat_id = self._store.chat_id
            if chat_id is None:
                continue
            now = datetime.datetime.now()
            if now.weekday() != 6:  # 0=월, 6=일
                continue
            if now.hour != self.WEEKLY_REVIEW_HOUR:
                continue
            today_s = now.date().isoformat()
            if self._store.last_weekly_review == today_s:
                continue
            if self._quiet_mode_block():
                # 보류해도 오늘은 처리한 걸로 — 그 시각 내내 5분마다 재카운트 방지.
                self._store.last_weekly_review = today_s
                continue
            try:
                reply = self._agent.weekly_review()
            except AIGenerationError as exc:
                print(f"[App] 주간 회고 생성 실패: {exc}")
                continue
            print("[App] 주간 회고 발송")
            self._send_or_enqueue(
                chat_id, reply, kind="weekly_review", reply_markup=MOOD_KEYBOARD,
            )

    # ====================================================== 매일 재시작
    def _daily_restart_loop(self) -> None:
        """매일 새벽 한 번 컨테이너를 자살시켜 Railway가 다시 띄우게 한다.
        장기 실행 시 누적되는 알 수 없는 문제(네트워크 세션, FD, 메모리 등)를
        깨끗이 리셋하는 안전망. 사용자가 자고 있을 새벽 시간에만 동작."""
        while not self._stop.wait(self.DAILY_RESTART_CHECK_INTERVAL):
            uptime_hours = (time.time() - self._started_at) / 3600.0
            if uptime_hours < self.DAILY_RESTART_MIN_UPTIME_HOURS:
                continue
            if datetime.datetime.now().hour == self.DAILY_RESTART_HOUR:
                print(
                    f"[App] 매일 재시작 시각({self.DAILY_RESTART_HOUR}시) 도달 "
                    f"(uptime {uptime_hours:.1f}h) — 컨테이너 재시작 유도 (exit 1)",
                    flush=True,
                )
                os._exit(1)
