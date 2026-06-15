"""presence.py — 사용자가 자나/깼나/깨어있나 추론의 단일 주인.

폰은 화면 OFF 동안 아무 신호도 안 보내 백엔드가 수면을 직접 못 본다. 그래서
세 가지 신호로 추론한다:
  - 마지막 사용자 *메시지* 시각 (store.last_user_message_at, 영속)
  - 마지막 *기기 활동*(폰/PC 트리거) 시각 (in-memory)
  - 폰이 트리거에 동봉한 screen_off_sec (직전까지 화면 OFF 지속)

이전엔 이 로직과 임계 상수가 app.py·app_loops.py·app_triggers.py·ai_engine.py 에
흩어져 '밤/아침 시간대' 정의가 3벌로 갈라져 있었다. 여기로 모아 단일 진실원으로 둔다.

의도 단위 질의:
  - note_user_message()  : 메시지 도착 → '자다 깸?' 판정 후 last_user_message_at 갱신
  - note_device_activity(): 트리거 도착 → *직전까지* 기기 침묵 반환 + 시각 갱신
  - should_skip_late_night(): '늦은 밤' 수면 잔소리를 보류할지 (자다 깬 직후면 True)
  - is_quiet_hours() / has_awake_evidence(): 야간 proactive 가드용
"""

from __future__ import annotations

import time
from typing import Optional


class Presence:
    # --- 임계·시간대 (단일 진실원) ---
    WAKE_GAP_MIN_SEC = 3 * 3600.0       # 이 이상 메시지 침묵 후 첫 메시지여야 밤잠 후보
    WAKE_GAP_MAX_SEC = 16 * 3600.0      # 너무 길면(며칠) 밤잠으로 안 봄
    WAKE_DETECTED_GRACE_SEC = 2 * 3600.0  # 깸 감지 후 '늦은 밤' 수면 잔소리 억제 창
    SLEEP_SCREEN_OFF_SEC = 90 * 60.0    # 폰 보고 화면 OFF 지속이 이 이상이면 자다 깸
    MORNING_WAKE_START_HOUR = 4         # 자다 깬 걸로 볼 아침 시간대 [start, end)
    MORNING_WAKE_END_HOUR = 12
    QUIET_HOURS_START = 0               # 야간 proactive 보류 시간대 [start, end)
    QUIET_HOURS_END = 7
    AWAKE_EVIDENCE_SEC = 3600.0         # 이 안에 기기 활동 있으면 '깨어있음'으로 봄

    def __init__(self, store) -> None:
        self._store = store
        self._last_device_activity_at = 0.0  # 마지막 폰/PC 트리거 시각
        self._last_wake_detected_at = 0.0    # 마지막 '자다 깸' 감지 시각

    # ============================================================ 질의
    def user_silence_sec(self) -> float:
        """마지막 사용자 메시지 이후 경과(초). 미설정이면 0."""
        last = self._store.last_user_message_at
        return max(0.0, time.time() - last) if last > 0 else 0.0

    def device_silence_sec(self) -> float:
        """마지막 기기 활동(트리거) 이후 경과(초). 기록 없으면 inf.
        '기기가 한참 잠잠 = 화면 OFF = 자는 중', 짧으면 '폰 하느라 깨어 있음'."""
        last = self._last_device_activity_at
        return (time.time() - last) if last > 0 else float("inf")

    def is_quiet_hours(self) -> bool:
        """지금이 야간(자는 시간대)인지 — proactive 기본 보류 창."""
        hour = time.localtime().tm_hour
        return self.QUIET_HOURS_START <= hour < self.QUIET_HOURS_END

    def has_awake_evidence(self) -> bool:
        """최근 기기 활동으로 '깨어있음'이 명백한지 (폰 하느라 안 자는 중)."""
        return self.device_silence_sec() < self.AWAKE_EVIDENCE_SEC

    def should_skip_late_night(
        self, device_silence_sec: float, screen_off_sec: Optional[float] = None
    ) -> bool:
        """'늦은 밤' 수면 잔소리 직전 호출. 방금 깸을 감지했거나 자다 깬 직후면
        True → 보류 (자는 사람한테 '안 자고 뭐해' 는 역효과). 계속 폰 하던 중이면
        False → 발사 (밤샘은 잡아야 함).

        우선순위: ①아침 인사로 막 깸 감지 → ②폰이 준 screen_off_sec 권위 판단 →
        ③폰 신호 없으면(구버전 APK) 기기 침묵 휴리스틱 폴백.
        device_silence_sec 은 *이번 트리거 직전까지* 의 기기 침묵 (note_device_activity 반환값)."""
        if time.time() - self._last_wake_detected_at < self.WAKE_DETECTED_GRACE_SEC:
            return True
        if screen_off_sec is not None:
            return screen_off_sec >= self.SLEEP_SCREEN_OFF_SEC
        return device_silence_sec > self.WAKE_GAP_MIN_SEC

    # ============================================================ 기록(상태 갱신)
    def note_user_message(self) -> Optional[int]:
        """사용자 메시지 도착 시 호출. 갱신 *전* 침묵 기준으로 '자다 깸?' 을 먼저
        판정한 뒤 last_user_message_at 을 갱신한다. 깸 후보면 경과 시간(시) 반환,
        아니면 None (호출자는 None 아니면 '자다 일어났어?' 를 물어볼 수 있다)."""
        woke = self._detect_wake()
        self._store.last_user_message_at = time.time()
        return woke

    def note_device_activity(self) -> float:
        """폰/PC 트리거 도착 시 호출. *직전까지* 의 기기 침묵(초)을 반환하고 활동
        시각을 갱신한다. 반환값은 늦은 밤 판단(should_skip_late_night)에 쓰인다."""
        silence = self.device_silence_sec()
        self._last_device_activity_at = time.time()
        return silence

    # ============================================================ 내부
    def _detect_wake(self) -> Optional[int]:
        """메시지 침묵이 밤잠 범위 + 아침 시간대 + 기기도 한참 잠잠(딴짓 중 아님)
        이면 '자다 깸' 으로 보고 경과(시) 반환 + 깸 시각 기록. 아니면 None."""
        gap = self.user_silence_sec()
        if gap <= 0 or not (self.WAKE_GAP_MIN_SEC <= gap <= self.WAKE_GAP_MAX_SEC):
            return None
        hour = time.localtime().tm_hour
        if not (self.MORNING_WAKE_START_HOUR <= hour < self.MORNING_WAKE_END_HOUR):
            return None
        # 폰을 계속 쓰고 있었으면(딴짓) 자다 깬 게 아니다.
        if self.device_silence_sec() <= self.WAKE_GAP_MIN_SEC:
            return None
        self._last_wake_detected_at = time.time()
        return int(gap // 3600)
