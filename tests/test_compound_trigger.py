"""복합 트리거 룰 평가 단위 테스트.

CoachApp 전체를 띄우지 않고, _evaluate_compound_trigger / _record_recent_trigger
만 *bound method* 처럼 사용하기 위해 더미 객체에 deque + lock 만 붙인다.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import pytest

import app as app_mod


class _DummyApp:
    """CoachApp 의 복합 룰 평가 부분만 흉내. 실 인스턴스 의존 X."""

    _COMPOUND_WINDOW_SEC = app_mod.CoachApp._COMPOUND_WINDOW_SEC
    _COMPOUND_RULES = app_mod.CoachApp._COMPOUND_RULES

    def __init__(self):
        self._recent_triggers = deque(maxlen=20)
        self._recent_triggers_lock = threading.Lock()

    # CoachApp 의 메서드를 그대로 가져와 self 에 bind
    _evaluate_compound_trigger = app_mod.CoachApp._evaluate_compound_trigger
    _record_recent_trigger = app_mod.CoachApp._record_recent_trigger


@pytest.fixture
def dummy():
    return _DummyApp()


class TestCompoundTrigger:
    def test_pc_fake_then_phone_dopamine_is_avoidance(self, dummy):
        dummy._record_recent_trigger("pc", "가짜 일하기")
        label = dummy._evaluate_compound_trigger("phone", "능동적 도파민 스크롤")
        assert label == "회피 패턴"

    def test_same_device_does_not_combine(self, dummy):
        dummy._record_recent_trigger("pc", "가짜 일하기")
        # PC 끼리는 룰 대상 아님
        label = dummy._evaluate_compound_trigger("pc", "능동적 도파민 스크롤")
        assert label is None

    def test_outside_window_does_not_combine(self, dummy):
        # 윈도우 초과한 옛 트리거는 결합 안 됨
        dummy._recent_triggers.append({
            "device": "pc",
            "trigger": "가짜 일하기",
            "at": time.time() - (dummy._COMPOUND_WINDOW_SEC + 60.0),
        })
        label = dummy._evaluate_compound_trigger("phone", "능동적 도파민 스크롤")
        assert label is None

    def test_phone_dopamine_then_pc_fake_also_combines(self, dummy):
        dummy._record_recent_trigger("phone", "능동적 도파민 스크롤")
        label = dummy._evaluate_compound_trigger("pc", "가짜 일하기")
        assert label == "회피 패턴"

    def test_unknown_combo_returns_none(self, dummy):
        dummy._record_recent_trigger("pc", "도파민 좀비")
        label = dummy._evaluate_compound_trigger("phone", "늦은 밤")
        assert label is None
