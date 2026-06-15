"""잔소리 응답 버튼 + 메타 체크인 단위 테스트.

CoachApp 전체(텔레그램·AI·캘린더)를 띄우지 않고, 새로 추가한 메서드만
*bound method* 처럼 더미 객체에 붙여 검증한다 (test_compound_trigger 와 동일 패턴).
"""

from __future__ import annotations

import threading
import time
import types

import pytest

import app as app_mod


# ----------------------------------------------------------- nag 버튼 응답
class _NagApp:
    """_handle_nag_action 흐름만 흉내. tracker 는 None, 타이머는 실제 lock 사용."""

    # CoachApp 메서드를 그대로 가져와 self 에 bind
    _handle_nag_action = app_mod.CoachApp._handle_nag_action
    _reset_ignore_streak = app_mod.CoachApp._reset_ignore_streak
    _cancel_warning_timeout = app_mod.CoachApp._cancel_warning_timeout
    _arm_snooze = app_mod.CoachApp._arm_snooze
    _cancel_snooze = app_mod.CoachApp._cancel_snooze
    _fire_snooze_nudge = app_mod.CoachApp._fire_snooze_nudge

    def __init__(self):
        self._timer_lock = threading.Lock()
        self._warning_timer = None
        self._snooze_timer = None
        self._tracker = None
        self._ignored_nags = 4
        self._meta_checkin_sent = True
        self._last_user_msg = 0.0


@pytest.fixture
def nag_app():
    app = _NagApp()
    yield app
    app._cancel_snooze()   # 테스트 중 켜진 타이머 정리


class TestNagAction:
    def test_ack_resets_streak(self, nag_app):
        toast = nag_app._handle_nag_action("ack", chat_id=1)
        assert nag_app._ignored_nags == 0
        assert nag_app._meta_checkin_sent is False
        assert nag_app._last_user_msg > 0
        assert nag_app._snooze_timer is None
        assert toast  # 토스트 문구 존재

    def test_pass_resets_streak_and_no_snooze(self, nag_app):
        nag_app._handle_nag_action("pass", chat_id=1)
        assert nag_app._ignored_nags == 0
        assert nag_app._meta_checkin_sent is False
        assert nag_app._snooze_timer is None

    def test_snooze_arms_timer_and_resets(self, nag_app):
        nag_app._handle_nag_action("snooze", chat_id=1)
        assert nag_app._ignored_nags == 0
        # 스누즈는 재알림 타이머를 건다
        assert nag_app._snooze_timer is not None
        assert nag_app._snooze_timer.is_alive()

    def test_unknown_action_returns_none(self, nag_app):
        assert nag_app._handle_nag_action("bogus", chat_id=1) is None


# ----------------------------------------------------------- 메타 체크인
class _MetaApp:
    """_maybe_meta_checkin 만 흉내. store·agent·send 는 페이크."""

    _maybe_meta_checkin = app_mod.CoachApp._maybe_meta_checkin
    _META_CHECKIN_IGNORED_THRESHOLD = app_mod.CoachApp._META_CHECKIN_IGNORED_THRESHOLD
    _META_CHECKIN_COOLDOWN_SEC = app_mod.CoachApp._META_CHECKIN_COOLDOWN_SEC

    def __init__(self, ignored, sent=False, last_overload=None):
        self._ignored_nags = ignored
        self._meta_checkin_sent = sent
        self._store = types.SimpleNamespace(last_overload_checkin=last_overload)
        self._agent = types.SimpleNamespace(
            overload_checkin=lambda: "요즘 답이 잘 안 오네 — 톤이 빡센가?"
        )
        self.sent_msgs = []

    def _send_or_enqueue(self, chat_id, text, *, kind, side_effects=None, reply_markup=None):
        self.sent_msgs.append({"text": text, "kind": kind})
        return True


# ----------------------------------------------------------- escalation note
class _EscApp:
    """_escalation_note 만 흉내 — store.active_nag_policy 페이크."""

    _escalation_note = app_mod.CoachApp._escalation_note

    def __init__(self, ignored, policy):
        self._ignored_nags = ignored
        self._store = types.SimpleNamespace(active_nag_policy=policy)


class TestEscalationNote:
    def test_no_note_when_not_ignored(self):
        assert _EscApp(0, "balanced")._escalation_note() == ""

    @pytest.mark.parametrize("policy", ["gentle", "balanced", "strict"])
    def test_first_ignore_leads_to_small_step_all_policies(self, policy):
        note = _EscApp(1, policy)._escalation_note()
        # 첫 무시부터 분해 방향 — 압박(쪼아라)이 아니라 쪼개기
        assert "작게" in note
        assert "register_today_goal_with_steps" in note
        assert "쪼아라" not in note   # 압박 어휘는 더 이상 안 씀

    def test_high_ignore_removes_pressure(self):
        note = _EscApp(4, "balanced")._escalation_note()
        assert "압박은 완전히 빼" in note or "안부" in note


# ----------------------------------------------------------- 밤잠 추론
class _WakeApp:
    """밤잠 추론 헬퍼만 흉내 — store.last_user_message_at + 기기 활동 시각 페이크."""

    _user_silence_sec = app_mod.CoachApp._user_silence_sec
    _device_silence_sec = app_mod.CoachApp._device_silence_sec
    _detect_wake_on_message = app_mod.CoachApp._detect_wake_on_message
    _should_skip_late_night_as_woke = app_mod.CoachApp._should_skip_late_night_as_woke

    def __init__(self, last_msg_at, wake_detected_at=0.0, last_device_at=0.0):
        self._store = types.SimpleNamespace(last_user_message_at=last_msg_at)
        self._last_wake_detected_at = wake_detected_at
        self._last_device_activity_at = last_device_at


class TestWakeInference:
    def test_silence_zero_when_unset(self):
        assert _WakeApp(0.0)._user_silence_sec() == 0.0

    def test_silence_measures_gap(self):
        app = _WakeApp(time.time() - 100.0)
        assert 90.0 <= app._user_silence_sec() <= 200.0

    def test_device_silence_inf_when_unset(self):
        assert _WakeApp(0.0)._device_silence_sec() == float("inf")

    def test_detect_wake_none_when_unset(self):
        assert _WakeApp(0.0)._detect_wake_on_message() is None

    def test_detect_wake_none_when_gap_too_long(self):
        # 며칠(>MAX)이면 밤잠으로 안 봄 — 시간대와 무관하게 None
        old = time.time() - (app_mod.WAKE_GAP_MAX_SEC + 3600.0)
        assert _WakeApp(old)._detect_wake_on_message() is None

    def test_late_night_skipped_when_device_quiet_long(self):
        # 기기가 한참 잠잠하다 막 신호 = 자다 깸 → 늦은 밤 잔소리 보류
        big = app_mod.WAKE_GAP_MIN_SEC + 600.0
        assert _WakeApp(0.0)._should_skip_late_night_as_woke(big) is True

    def test_late_night_fires_when_device_recently_active(self):
        # 계속 폰 하던 중(기기 침묵 짧음) + 깸 감지 없음 → 밤새운 것 → 발사
        app = _WakeApp(0.0, wake_detected_at=0.0)
        assert app._should_skip_late_night_as_woke(120.0) is False

    def test_late_night_skipped_right_after_wake_detected(self):
        # 메시지가 트리거보다 먼저 와 깸이 감지됐으면, 기기 침묵이 짧아도 보류
        app = _WakeApp(0.0, wake_detected_at=time.time())
        assert app._should_skip_late_night_as_woke(120.0) is True

    def test_phone_screen_off_long_skips_even_if_device_active(self):
        # 폰이 '직전까지 화면 5시간 OFF' 보고 → 자다 깸(권위 신호) → 보류
        app = _WakeApp(0.0)
        big = app_mod.SLEEP_SCREEN_OFF_SEC + 600.0
        assert app._should_skip_late_night_as_woke(0.0, screen_off_sec=big) is True

    def test_phone_screen_off_short_fires_even_if_message_silent(self):
        # 폰이 '화면 OFF 0초'(계속 켜둠) 보고 → 밤샘 → 발사. 메시지 침묵이 길어도
        # 폰 신호가 권위라 휴리스틱 폴백 안 함.
        app = _WakeApp(time.time() - 6 * 3600.0)
        assert app._should_skip_late_night_as_woke(6 * 3600.0, screen_off_sec=0.0) is False


class TestMetaCheckin:
    def test_below_threshold_does_not_fire(self):
        app = _MetaApp(ignored=2)
        assert app._maybe_meta_checkin(chat_id=1) is False
        assert app.sent_msgs == []

    def test_fires_at_threshold(self):
        app = _MetaApp(ignored=3)
        assert app._maybe_meta_checkin(chat_id=1) is True
        assert app._meta_checkin_sent is True
        assert len(app.sent_msgs) == 1
        # overload_checkin 쿨다운을 공유하도록 같은 kind 로 발송
        assert app.sent_msgs[0]["kind"] == "overload_checkin"

    def test_does_not_fire_twice_in_streak(self):
        app = _MetaApp(ignored=5, sent=True)
        assert app._maybe_meta_checkin(chat_id=1) is False
        assert app.sent_msgs == []

    def test_respects_cooldown(self):
        # 최근에 이미 overload/meta 를 보냈으면 쿨다운 동안 다시 안 보냄
        app = _MetaApp(ignored=5, last_overload=time.time())
        assert app._maybe_meta_checkin(chat_id=1) is False

    def test_fires_after_cooldown_expires(self):
        old = time.time() - (app_mod.CoachApp._META_CHECKIN_COOLDOWN_SEC + 60.0)
        app = _MetaApp(ignored=5, last_overload=old)
        assert app._maybe_meta_checkin(chat_id=1) is True
