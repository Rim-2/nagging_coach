"""app_messaging — 텔레그램 전송과 retry 큐 워커 책임 분리.

이 mixin 은 CoachApp 에 mix-in 되어 다음을 담당한다:
  - _send: 짧은 동기 backoff (1s→2s→4s) 가 있는 텔레그램 발송 wrapper
  - _send_or_enqueue: 잔소리·자동 메시지 전용. 실패 시 영구 retry 큐에 적재
  - _apply_message_side_effects: 메시지가 *실제 도달했을 때* 적용되는 부수효과
    (쿨다운·통계·자기학습)
  - _retry_loop: 큐 워커. exponential backoff 로 도달할 때까지 재시도
  - _maybe_alert_retry_queue: 큐가 막혔으면 시스템 알림 1회 (쿨다운 보호)
  - _policy_recovery_note / _escalation_note: 잔소리 description 에 끼우는 가이드

의존: self._store, self._tg, self._stop, self._consecutive_tg_failures,
      self._remote_cooldowns, self._remote_cooldown_lock,
      self._last_retry_queue_alert, self._ignored_nags
"""

from __future__ import annotations

import datetime
import os
import time
from typing import Optional


# 텔레그램 발송이 연속 N회 실패하면 컨테이너 자살 → Railway 가 자동 재시작.
# 네트워크 누적 문제로 send 가 막힐 때 사람 개입 없이 복구하기 위함.
TG_FAILURE_EXIT_THRESHOLD = 20

# 같은 trigger_value 는 백엔드에서 권위 있게 N분 막아 잔소리 폭주를 차단.
# 위성 60초 쿨다운만으로는 idle/dwell 기반 트리거가 매 분 다시 발사될 수 있음.
REMOTE_TRIGGER_COOLDOWN_SEC = 600.0


class MessagingMixin:
    """텔레그램 전송·retry 큐·부수효과 적용을 한 곳에 모은 mixin."""

    # 텔레그램 일시 장애(API timeout 등)에 대응하는 *짧은* 동기 retry 백오프.
    # 합계 약 7초. 이걸로 못 잡는 더 긴 장애는 호출자가 retry 큐로 위임한다.
    _SEND_BACKOFF_SEC = (1.0, 2.0, 4.0)

    # exponential backoff (분 단위). 도달 못한 메시지는 점점 긴 간격으로 시도.
    # cap = 30분: 며칠짜리 장애도 그 사이에 풀리면 도달 가능.
    _RETRY_BACKOFF_MIN = (1, 2, 4, 8, 16, 30)
    _RETRY_CHECK_INTERVAL = 30.0          # 큐 폴링 주기 (초)
    _RETRY_MAX_AGE_SEC = 7 * 24 * 3600.0  # 일주일 지나도 못 보내면 폐기
    # 큐 모니터링 — 임계치 넘으면 사용자에게 1회 시스템 메시지 (다시 풀릴 때까지 X)
    _RETRY_QUEUE_ALERT_SIZE = 5             # 동시에 N개 이상 큐에 있으면 경고
    _RETRY_QUEUE_ALERT_AGE_SEC = 3600.0     # 가장 오래된 항목이 N초 이상이면 경고
    _RETRY_QUEUE_ALERT_COOLDOWN = 3 * 3600.0  # 같은 경고 N초 안에 또 보내지 않음

    # =================================================== 텔레그램 발송 wrapper
    def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """텔레그램 발송 wrapper. 일시적 실패에 짧은 backoff retry. 그래도
        실패하면 False — 잔소리/자동 메시지 경로는 영구 retry 큐에 적재하고,
        대화 답장은 pending_chat_reply 에 보관해 다음 turn 에 합쳐 답하도록.
        연속 실패가 임계치를 넘으면 컨테이너 자살 → Railway 자동 재시작.
        reply_markup: inline keyboard 등 첨부 (옵션)."""
        ok = self._tg.send_message(chat_id, text, reply_markup=reply_markup)
        for delay in self._SEND_BACKOFF_SEC:
            if ok:
                break
            time.sleep(delay)
            ok = self._tg.send_message(chat_id, text, reply_markup=reply_markup)
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

    # ============================================ 큐 적재 헬퍼 (잔소리/자동)
    def _send_or_enqueue(
        self,
        chat_id: int,
        text: str,
        *,
        kind: str,
        side_effects: Optional[dict] = None,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """잔소리·자동 메시지 전용 발송. 첫 시도 성공 시 부수효과 즉시 적용,
        실패 시 큐에 적재해 워커가 도달할 때까지 retry. reply_markup 도 큐에
        같이 보관되어 retry 도달 시점에도 버튼이 유지된다.

        반환: True = 첫 시도 도달 / False = 큐에 적재됨 (호출자는 추가 부수
        효과 코드를 두지 마라 — 모두 _apply_message_side_effects 에서)."""
        se = side_effects or {}
        if self._send(chat_id, text, reply_markup=reply_markup):
            self._apply_message_side_effects(kind, se)
            return True
        msg_id = self._store.add_pending_message(
            kind=kind,
            chat_id=chat_id,
            text=text,
            side_effects=se,
            reply_markup=reply_markup,
        )
        print(f"[App] 전송 실패 → retry 큐 적재 (kind={kind}, id={msg_id[:8]})")
        return False

    # ====================================================== 부수효과 단일 진입점
    def _apply_message_side_effects(self, kind: str, se: dict) -> None:
        """잔소리·자동 메시지가 사용자에게 *실제 도달했을 때* 적용되는 부수효과."""
        if kind == "remote_trigger":
            tv = se.get("trigger_value")
            if tv:
                with self._remote_cooldown_lock:
                    self._remote_cooldowns[tv] = time.time() + REMOTE_TRIGGER_COOLDOWN_SEC
                self._store.bump_trigger_fire(tv)
            weak = se.get("weak_spot_candidate")
            if weak:
                self._store.bump_weak_spot_candidate(weak)
            if se.get("is_late_night"):
                self._store.last_late_night_fired = (
                    datetime.datetime.now().date().isoformat()
                )
            if se.get("mark_policy_asked") and not self._store.nag_policy_asked:
                self._store.nag_policy_asked = True
        elif kind == "local_trigger":
            tv = se.get("trigger_value")
            if tv:
                self._store.bump_trigger_fire(tv)
            weak = se.get("weak_spot_candidate")
            if weak:
                self._store.bump_weak_spot_candidate(weak)
            if se.get("mark_policy_asked") and not self._store.nag_policy_asked:
                self._store.nag_policy_asked = True
        elif kind == "weekly_review":
            # last_weekly_review 는 날짜 문자열 (YYYY-MM-DD) — 같은 일요일 중복 발송 가드.
            self._store.last_weekly_review = datetime.date.today().isoformat()
            self._store.reset_weak_spot_candidates()
        elif kind == "overload_checkin":
            self._store.last_overload_checkin = time.time()
        elif kind == "event_reminder":
            eid = se.get("event_id")
            if eid:
                self._store.mark_event_reminded(eid)
        # alarm·focus_session_end·proactive_checkin·system_notice·daily_journal·
        # risk_predict·snooze_nudge 는 부수효과 없음.

    # ================================================== description 가이드 노트
    def _policy_recovery_note(self) -> str:
        """일시 톤다운 (컨디션 신호로 적용된 24h gentle) 이 막 만료되었을 때,
        다음 잔소리에 자연스러운 복귀 코멘트를 끼우게 가이드. 한 번만 발동."""
        if not self._store.consume_policy_recovery_note():
            return ""
        base = self._store.nag_policy
        return (
            f" (어제 컨디션 신호 때문에 하루 톤이 부드러웠어. 이제 평소 톤"
            f"({base})으로 돌아갈 차례 — 답장 자연스러운 한 곳에 '오늘은 좀 "
            f"빡세게 가볼까' 같은 톤 전환을 한 줄 끼워.)"
        )

    def _escalation_note(self) -> str:
        """잔소리 무시 누적 시 description 끝에 붙일 코치 가이드.

        핵심 원칙: 무시·미루기는 대개 '할 일이 너무 커서 시작 장벽이 높다'는 신호다.
        그래서 압박을 *올리는* 게 아니라, 다음 잔소리는 행동을 *더 잘게 쪼개*
        시작 장벽을 낮춘다 (activation energy ↓). '요구 크기를 줄인다'는 방향은
        정책과 무관하게 공통이고, nag_policy 는 톤 세기만 조절한다."""
        n = self._ignored_nags
        if n <= 0:
            return ""
        policy = self._store.active_nag_policy
        # 공통 코어 — 무시 = 부담 신호로 읽고, 다음 한 걸음을 더 작게.
        core = (
            f"사용자가 잔소리를 {n}번 답 없이 지나갔어 — 할 일이 부담스러워 "
            "시작을 못 하는 거일 수 있어. 압박을 더 주지 말고 다음 한 걸음을 더 "
            "*작게* 쪼개라. 지금 당장 2~5분이면 끝낼 구체적인 한 조각 하나만 콕 "
            "집어 권하고(예: '딱 한 줄만', '2분만 책상 정리', '파일만 열기'), "
            "오늘 목표가 있으면 register_today_goal_with_steps 로 더 잘게 분해하거나 "
            "다음 sub_step 하나만 가리켜."
        )
        if policy == "gentle":
            tone = " 톤은 아주 부드럽게 — 안 해도 괜찮다는 안전감을 먼저 주고 한 발만."
        elif policy == "strict":
            tone = " 톤은 단호해도 되지만, 요구하는 '크기'는 반드시 더 작게 가라."
        else:  # balanced
            tone = " 톤은 담담하게, 부담만 덜어주는 방향으로."
        # 여러 번 흘려보냈으면 압박을 완전히 빼 — 더 작게, 혹은 그냥 안부만.
        if n >= 3:
            tone += (
                " 이미 여러 번 그냥 지나갔으니 압박은 완전히 빼 — 더 작게 쪼개거나, "
                "정 안 되면 행동 권유 대신 가볍게 안부만 물어."
            )
        return f" ({core}{tone})"

    # =================================================== retry 큐 워커
    def _retry_backoff_sec(self, attempts: int) -> float:
        idx = min(attempts, len(self._RETRY_BACKOFF_MIN) - 1)
        return self._RETRY_BACKOFF_MIN[idx] * 60.0

    def _retry_loop(self) -> None:
        """텔레그램 전송 실패로 큐에 적재된 잔소리·자동 메시지를 도착할 때까지
        재시도. 도착 시점에 부수효과(쿨다운·통계·자기학습)도 적용.
        주의: 같은 chat 에 여러 메시지가 큐에 있으면 *발사 순서대로* 처리해
        시간 역순 도착을 막는다 (먼저 적재된 메시지가 먼저 도달)."""
        while not self._stop.wait(self._RETRY_CHECK_INTERVAL):
            pending = self._store.list_pending_messages()
            if not pending:
                continue
            # 발사 순서 (created_at) 오름차순 — 시간 역순 도착 방지
            pending.sort(key=lambda it: it.get("created_at", 0.0))
            now = time.time()
            for item in pending:
                if self._stop.is_set():
                    break
                # 너무 오래 묵은 항목은 폐기 (적재된 지 일주일 이상)
                age = now - float(item.get("created_at", now))
                if age > self._RETRY_MAX_AGE_SEC:
                    print(
                        f"[App] retry 큐 항목 만료 폐기 "
                        f"(kind={item.get('kind')}, age={age/3600:.1f}h)"
                    )
                    self._store.remove_pending_message(item["id"])
                    continue
                # next_attempt_at 안 된 항목은 통과
                if now < float(item.get("next_attempt_at", 0.0)):
                    continue
                # 시도 — reply_markup 도 같이 보존되어야 큐 도달 시점에도 버튼 유지
                ok = self._tg.send_message(
                    item["chat_id"],
                    item["text"],
                    reply_markup=item.get("reply_markup"),
                )
                if ok:
                    self._consecutive_tg_failures = 0
                    print(
                        f"[App] retry 도달 "
                        f"(kind={item.get('kind')}, "
                        f"attempts={item.get('attempts', 0) + 1})"
                    )
                    try:
                        self._apply_message_side_effects(
                            item.get("kind", ""),
                            item.get("side_effects", {}) or {},
                        )
                    except Exception as exc:
                        print(f"[App] retry 도달 후 부수효과 적용 실패: {exc}")
                    self._store.remove_pending_message(item["id"])
                else:
                    delay = self._retry_backoff_sec(
                        int(item.get("attempts", 0)) + 1
                    )
                    self._store.update_pending_attempt(item["id"], now + delay)
                    print(
                        f"[App] retry 실패 → {delay/60:.0f}분 후 재시도 "
                        f"(kind={item.get('kind')}, "
                        f"attempts={item.get('attempts', 0) + 1})"
                    )
                # 한 사이클에 한 메시지씩만 — Telegram API rate-limit 회피
                break
            self._maybe_alert_retry_queue(pending, now)

    def _maybe_alert_retry_queue(self, pending: list, now: float) -> None:
        """큐 크기 또는 가장 오래된 항목 age 가 임계 초과 시 1회 알림.
        같은 알림은 _RETRY_QUEUE_ALERT_COOLDOWN 동안 다시 보내지 않는다."""
        if not pending:
            return
        chat_id = self._store.chat_id
        if chat_id is None:
            return
        if now - self._last_retry_queue_alert < self._RETRY_QUEUE_ALERT_COOLDOWN:
            return
        size_alert = len(pending) >= self._RETRY_QUEUE_ALERT_SIZE
        oldest_age = now - min(float(it.get("created_at", now)) for it in pending)
        age_alert = oldest_age >= self._RETRY_QUEUE_ALERT_AGE_SEC
        if not (size_alert or age_alert):
            return
        reasons = []
        if size_alert:
            reasons.append(f"큐에 {len(pending)}개 누적")
        if age_alert:
            reasons.append(f"가장 오래된 항목 {int(oldest_age / 60)}분째 미도달")
        msg = (
            "(시스템) 텔레그램 발송 retry 큐가 막혀 있어 — "
            + ", ".join(reasons)
            + ". 네트워크나 봇 토큰을 확인해줘. 큐가 풀리는 대로 누적된 "
            "잔소리가 한꺼번에 도착할 수 있어."
        )
        # _send 는 retry 큐가 도와주는 wrapper. 이 알림 자체가 전송 실패하면
        # 또 큐에 들어가도 의미 없으니 raw send_message 로 한 번만 시도.
        try:
            self._tg.send_message(chat_id, msg)
        except Exception as exc:
            print(f"[App] retry 큐 경고 전송 실패: {exc}")
        self._last_retry_queue_alert = now
        print(f"[App] retry 큐 경고 알림: {reasons}")
