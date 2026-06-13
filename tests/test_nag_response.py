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
