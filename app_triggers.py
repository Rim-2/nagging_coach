"""app_triggers — 트리거 처리 책임 분리.

이 mixin 은 CoachApp 에 mix-in 되어 다음을 담당한다:
  - _on_trigger: 로컬 PC 트래커 콜백 (Tracker 데몬 스레드에서 호출)
  - handle_remote_trigger: 원격 위성(POST /trigger)에서 보낸 트리거 처리
  - _describe_trigger: trigger_value → LLM 컨텍스트용 자연어 설명 (static)
  - _evaluate_compound_trigger / _record_recent_trigger: PC + 폰 신호 결합 룰

의존: self._store, self._tracker, self._agent, self._remote_cooldowns,
      self._remote_cooldown_lock, self._recent_triggers,
      self._recent_triggers_lock, self._consecutive_ai_failures
      그리고 MessagingMixin 의 _send_or_enqueue·_escalation_note·_policy_recovery_note,
      CoachApp 본체의 _note_ai_failure·_arm_warning_timeout.
"""

from __future__ import annotations

import datetime
import time
from typing import Optional

from ai_engine import AIGenerationError
from app_http import PHONE_APP_LABELS
from tracker import Snapshot, TriggerType, sanitize_window_title


# 도파민 trail 학습 대상 — 딴짓 카테고리만. 늦은 밤·과로 등은 학습 의미 X.
_TRAIL_LEARN_TRIGGERS = {
    "개인 약점 앱", "도파민 좀비", "능동적 도파민 스크롤", "과몰입 딴짓",
}

# inline keyboard — 잔소리에 붙는 pattern-interrupt 버튼. 텍스트 알림을 스와이프로
# 흘려보내는 습관을 깨고, 침묵 대신 *명시적 신호* (수락/미룸/거절)를 받는다.
# callback_data="nag:action" → app._handle_nag_action 이 처리.
NAG_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "알겠어 👍", "callback_data": "nag:ack"},
        {"text": "좀따 ⏰", "callback_data": "nag:snooze"},
        {"text": "패스 🙅", "callback_data": "nag:pass"},
    ]]
}


class TriggersMixin:
    """로컬·원격 트리거 처리 + 복합 룰 평가 + description 생성."""

    # 두 위성 신호를 시간축에서 결합해 단일 위성 룰로는 못 잡는 패턴 감지.
    _COMPOUND_WINDOW_SEC = 10 * 60.0   # 두 트리거가 이 시간 내에 있어야 결합 평가
    # 결합 룰 — (이전 device, 이전 트리거, 현재 device, 현재 트리거) → 복합 라벨
    _COMPOUND_RULES = {
        # PC에서 일하는 척(idle/과로) → 곧이어 폰에서 도파민 = 회피
        ("pc", "가짜 일하기", "phone", "능동적 도파민 스크롤"): "회피 패턴",
        ("pc", "가짜 일하기", "phone", "과몰입 딴짓"): "회피 패턴",
        ("pc", "휴식 없는 과로", "phone", "능동적 도파민 스크롤"): "회피 패턴",
        ("pc", "휴식 없는 과로", "phone", "과몰입 딴짓"): "회피 패턴",
        # 반대 방향: 폰에서 SNS 보다가 PC로 가서 일하는 척 = 또 다른 회피
        ("phone", "능동적 도파민 스크롤", "pc", "가짜 일하기"): "회피 패턴",
        ("phone", "과몰입 딴짓", "pc", "가짜 일하기"): "회피 패턴",
    }

    # 메타 체크인 — 잔소리를 연속 N회 답 없이 흘려보내면, 또 잔소리하는 대신
    # 한 발 물러나 '내 방식이 안 맞나?' 하고 직접 물어본다 (알림 습관화 대응).
    _META_CHECKIN_IGNORED_THRESHOLD = 3
    # 메타 메시지 자체가 또 다른 잔소리가 되지 않게, overload 점검과 같은 3일
    # 쿨다운을 공유한다 (last_overload_checkin 기준).
    _META_CHECKIN_COOLDOWN_SEC = 86400.0 * 3

    # ====================================================== 메타 체크인
    def _maybe_meta_checkin(self, chat_id: int) -> bool:
        """무시 누적이 임계치를 넘었고 streak 안에서 아직 안 물어봤으면, 평소
        잔소리 대신 메타 체크인(톤·목표 적합성 질문)을 보낸다. 보냈으면 True —
        호출자는 이번 트리거의 잔소리를 생략한다. overload_checkin 생성기를 재사용
        (메시지 내용이 정확히 '톤 빡센가/목표 무거운가' 질문)."""
        if self._ignored_nags < self._META_CHECKIN_IGNORED_THRESHOLD:
            return False
        if self._meta_checkin_sent:
            return False
        last = self._store.last_overload_checkin
        if last is not None and (time.time() - last) < self._META_CHECKIN_COOLDOWN_SEC:
            return False
        try:
            reply = self._agent.overload_checkin()
        except AIGenerationError as exc:
            print(f"[App] 메타 체크인 생성 실패: {exc}")
            return False
        self._meta_checkin_sent = True
        print(f"[App] 메타 체크인 발송 (누적 무시 {self._ignored_nags}회)")
        # kind="overload_checkin" → 도달 시 last_overload_checkin 갱신 (쿨다운 공유).
        self._send_or_enqueue(chat_id, reply, kind="overload_checkin")
        return True

    # ====================================================== 로컬 트리거 (PC Tracker 콜백)
    def _on_trigger(self, trigger: TriggerType, snap: Snapshot) -> None:
        """⚠️ Tracker 데몬 스레드에서 호출됨."""
        chat_id = self._store.chat_id
        if chat_id is None:
            self._tracker.resume_normal()
            return
        # quiet_mode 활성 시 잔소리 보류 — 트래커 상태는 normal 로 돌려 다음
        # 트리거에도 평가가 이어지게.
        if self._quiet_mode_block():
            print(f"[App] 트리거 [{trigger.value}] quiet_mode 활성 — 보류")
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

        # 잔소리를 연속으로 흘려보낸 흔적이 임계치를 넘으면, 또 잔소리하지 말고
        # 한 발 물러나 메타로 물어본다 (보냈으면 이번 트리거는 여기서 종료).
        if self._maybe_meta_checkin(chat_id):
            self._tracker.resume_normal()
            return

        snap_dict = {
            "active_window": snap.active_window,
            "idle_time": snap.idle_time,
            "switch_count": snap.switch_count,
        }
        description = self._describe_trigger(trigger.value, snap_dict, goal)
        description += self._escalation_note()
        description += self._policy_recovery_note()
        try:
            reply = self._agent.handle_event(description)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            self._tracker.resume_normal()
            return

        self._consecutive_ai_failures = 0
        print(f"[App] 트리거 [{trigger.value}] → 잔소리 발송")
        # 자기학습 weak_spot 후보 — sanitized 라벨 (PC 환경)
        label = sanitize_window_title(snap.active_window)
        se = {
            "trigger_value": trigger.value,
            "weak_spot_candidate": label if (label and label != "other") else "",
            "mark_policy_asked": True,
        }
        self._send_or_enqueue(
            chat_id, reply, kind="local_trigger", side_effects=se,
            reply_markup=NAG_KEYBOARD,
        )
        self._arm_warning_timeout()

    # ====================================================== description (LLM 컨텍스트)
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
        win = PHONE_APP_LABELS.get(raw_win, raw_win)
        idle_min = int((snap.get("idle_time") or 0) // 60)
        switch_count = snap.get("switch_count") or 0
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
        if trigger_value == "Pomodoro 휴식":
            # 한 세션 첫 50분 — 짧은 환기 권유. 다그치지 말고 가볍게.
            return (
                "사용자가 50분 가까이 한 흐름으로 작업했어. Pomodoro 식으로 "
                "5~10분 가볍게 환기하라고 권해 — 다그치지 말고 '한 사이클 끝났네, "
                "잠깐 일어나서 물 한 모금?' 같이 부드러운 톤. 강요 X."
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
        elif trigger_value == "회피 패턴":
            body = (
                f"사용자가 PC에서 업무앱을 켜둔 채 폰으로 SNS·메신저로 넘어가서 한참 머무는, "
                f"혹은 그 역방향의 회피 흐름이 잡혔어. 지금 보고 있는 창: '{win}'. "
                "다그치지 말고 패턴을 객관적으로 짚어 주고, 작은 한 걸음을 권해."
            )
        else:  # "과몰입 딴짓" 또는 알 수 없는 신규 트리거
            mins = session_min if session_min and session_min > 0 else 30
            body = (
                f"사용자가 '{win}'(게임/메신저류)에 {mins}분 넘게 집중하고 있어."
            )
        return body + goal_note

    # ====================================================== 복합 트리거 룰 평가
    def _evaluate_compound_trigger(
        self, device: str, trigger_value: str
    ) -> Optional[str]:
        """현재 도착한 트리거와 _recent_triggers 의 *다른 device* 이력을 비교해
        복합 룰이 매칭되면 승격 라벨을 돌려준다. 매칭 없으면 None."""
        now = time.time()
        with self._recent_triggers_lock:
            for prev in reversed(self._recent_triggers):
                if now - prev["at"] > self._COMPOUND_WINDOW_SEC:
                    break    # 시간순 deque — 더 오래된 건 더 이상 의미 X
                if prev["device"] == device:
                    continue  # 같은 device 끼리는 결합 룰 대상 아님
                key = (prev["device"], prev["trigger"], device, trigger_value)
                label = self._COMPOUND_RULES.get(key)
                if label:
                    return label
        return None

    def _record_recent_trigger(self, device: str, trigger_value: str) -> None:
        with self._recent_triggers_lock:
            self._recent_triggers.append(
                {"device": device, "trigger": trigger_value, "at": time.time()}
            )

    # ====================================================== 원격 트리거 (위성 → 백엔드)
    def handle_remote_trigger(self, body: dict) -> dict:
        """로컬 PC 트래커 위성(trigger_satellite.py)에서 보낸 트리거를 처리.
        HTTP /trigger 핸들러에서 호출됨. _on_trigger 의 클라우드 버전 —
        Tracker 상태 머신은 위성 쪽이 갖고 있고, 여기선 잔소리만 만들어 발송."""
        trigger_value = str(body.get("trigger", "")).strip()
        snap = body.get("snapshot") or {}
        freq = body.get("window_freq")
        device = str(body.get("device", "")).strip().lower() or "pc"
        trail = body.get("trail")
        if not trigger_value:
            return {"ok": False, "action": "skipped", "reason": "missing_trigger"}

        # 폰 위성이 같이 보낸 디바이스 status (DND·충전·화면·걸음·헤드폰) 을
        # 최신값으로 store 에 보관 — state_summary·자기 격려 메시지에서 활용.
        if device == "phone":
            try:
                self._store.update_phone_context(snap)
                # place=="집" 자동 감지 → away 모드 해제. exit_quiet_mode 가 dict
                # 반환하므로 보류 건수 있으면 안내 메시지 발사.
                if (
                    snap.get("place_category") == "집"
                    and self._store.quiet_mode == "away"
                ):
                    info = self._store.exit_quiet_mode()
                    if info is not None:
                        self._on_quiet_exit(info)
            except Exception as exc:
                print(f"[App] phone_context 갱신 실패: {exc}")

        # quiet_mode 활성 시 모든 잔소리 보류 (사용자 명시 선언 → 자동 발사 X)
        if self._quiet_mode_block():
            print(
                f"[App] (원격) 트리거 [{trigger_value}] quiet_mode 활성 — 보류"
            )
            return {"ok": True, "action": "skipped", "reason": "quiet_mode"}

        # 디바이스 상태 가드 (폰 위성 페이로드에 같이 옴). 사용자가 명시적으로
        # 방해 차단(DND)을 걸어둔 상태에선 *낮은 우선순위* 트리거는 잔소리 보류 —
        # 약점·산만함 같은 *사용자가 등록한 진짜 약점*은 그대로 보내고, 도파민/
        # 과몰입/도파민 스크롤 같은 *경고성* 트리거만 가드.
        dnd_active = bool(snap.get("dnd_active"))
        charging = bool(snap.get("charging"))
        screen_on = snap.get("screen_on")
        if dnd_active and trigger_value in {
            "도파민 좀비", "능동적 도파민 스크롤", "과몰입 딴짓", "Pomodoro 휴식",
        }:
            print(
                f"[App] (원격) 트리거 [{trigger_value}] DND 활성 — 잔소리 보류"
            )
            return {"ok": True, "action": "skipped", "reason": "dnd_active"}
        # 늦은 밤 + 충전 중 + 화면 OFF = 진짜 잠든 신호 → 잔소리 안 보냄.
        # screen_on 정보가 명시적으로 False 일 때만 (None 은 알 수 없음으로 처리).
        if (
            trigger_value == "늦은 밤"
            and charging
            and screen_on is False
        ):
            print(
                "[App] (원격) 늦은 밤 트리거 — 충전 중 + 화면 OFF (잠든 걸로 판단) → 스킵"
            )
            return {"ok": True, "action": "skipped", "reason": "asleep_signal"}

        # 도파민 trail 학습 — 딴짓 카테고리 트리거 직전 라벨 시퀀스 누적.
        if (
            isinstance(trail, list)
            and trigger_value in _TRAIL_LEARN_TRIGGERS
        ):
            try:
                self._store.bump_dopamine_trail([str(x) for x in trail])
            except Exception as exc:
                print(f"[App] trail 학습 실패: {exc}")

        # 복합 룰 평가 — 다른 device 의 최근 트리거와 결합 가능하면 라벨 승격.
        compound_label = self._evaluate_compound_trigger(device, trigger_value)
        self._record_recent_trigger(device, trigger_value)
        if compound_label and compound_label != trigger_value:
            print(
                f"[App] (원격) 복합 트리거 승격: {device}/{trigger_value} → "
                f"{compound_label}"
            )
            trigger_value = compound_label

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

        # 늦은 밤 트리거 — 하루 한 번만. 위성 메모리 변수의 재시작 리셋을 막기
        # 위해 백엔드가 영속 마크로 차단 (트리거 종류별 10분 쿨다운보다 강한 제약).
        if trigger_value == "늦은 밤":
            today_s = datetime.datetime.now().date().isoformat()
            if self._store.last_late_night_fired == today_s:
                print(f"[App] (원격) 늦은 밤 트리거 — 오늘 이미 발사됨, 스킵")
                return {"ok": True, "action": "skipped", "reason": "late_night_already_fired_today"}

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

        # 잔소리 누적 무시가 임계치를 넘으면 메타 체크인으로 대체 (한 발 물러남).
        if self._maybe_meta_checkin(chat_id):
            return {"ok": True, "action": "meta_checkin"}

        description = self._describe_trigger(trigger_value, snap, goal)
        description += self._escalation_note()
        description += self._policy_recovery_note()
        try:
            reply = self._agent.handle_event(description)
        except AIGenerationError as exc:
            self._note_ai_failure(chat_id, exc)
            return {"ok": False, "action": "failed", "reason": "ai_error"}

        self._consecutive_ai_failures = 0
        print(f"[App] (원격) 트리거 [{trigger_value}] → 잔소리 발송")
        se = {
            "trigger_value": trigger_value,
            "weak_spot_candidate": snap.get("active_window") or "",
            "is_late_night": trigger_value == "늦은 밤",
            "mark_policy_asked": True,
        }
        delivered = self._send_or_enqueue(
            chat_id, reply, kind="remote_trigger", side_effects=se,
            reply_markup=NAG_KEYBOARD,
        )
        self._arm_warning_timeout()
        if not delivered:
            return {"ok": True, "action": "queued", "reason": "telegram_retry_queue"}
        return {"ok": True, "action": "nag_sent"}
