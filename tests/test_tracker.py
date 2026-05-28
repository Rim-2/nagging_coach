"""tracker.py 트리거 분기 단위 테스트.

직접 Tracker 인스턴스를 만들고 _evaluate() 를 호출. OS·키보드 후킹은 일절
시작하지 않으므로 안전. weak_spots 콜백·_input.recent_events 만 주입.
"""

from __future__ import annotations

import time
from collections import deque

import pytest

import tracker as tracker_mod


@pytest.fixture
def mk_tracker():
    """간단 Tracker 팩토리 — start() 호출 X (스레드·후킹 X)."""
    def _make(weak_spots=()):
        return tracker_mod.Tracker(
            on_trigger=lambda *_a, **_kw: None,
            get_weak_spots=lambda: list(weak_spots),
        )
    return _make


def _snap(active_window: str, *, idle: float = 0.0, switches: int = 0):
    return tracker_mod.Snapshot(
        active_window=active_window,
        idle_time=idle,
        switch_count=switches,
        state=tracker_mod.State.NORMAL,
    )


# --------------------------------------------------------- 생산성 컨텍스트 가드
class TestProductivityGuard:
    def test_vscode_is_productive(self):
        assert tracker_mod.Tracker._is_productive_context(
            "main.py - visual studio code"
        )

    def test_youtube_is_not_productive(self):
        assert not tracker_mod.Tracker._is_productive_context(
            "youtube - google chrome"
        )

    def test_notion_is_productive(self):
        assert tracker_mod.Tracker._is_productive_context("notion - docs")

    def test_no_keyword_falls_to_not_productive(self):
        assert not tracker_mod.Tracker._is_productive_context("untitled")


# ------------------------------------------------------------ 약점 키워드 매칭
class TestWeakKeywordMatching:
    def test_korean_input_matches_english_title(self):
        # store 에 "유튜브" 등록 → "youtube" 가 포함된 창과 매칭되어야
        assert tracker_mod.matches_weak_keyword("유튜브", "youtube - chrome")

    def test_english_input_matches_korean_title(self):
        assert tracker_mod.matches_weak_keyword("youtube", "유튜브 - 브라우저")

    def test_no_match_returns_false(self):
        assert not tracker_mod.matches_weak_keyword(
            "유튜브", "main.py - vscode"
        )


# ------------------------------------------------------- 약점 앱 트리거 분기
class TestWeaknessTrigger:
    def test_weakness_fires_after_dwell(self, mk_tracker):
        t = mk_tracker(weak_spots=["youtube"])
        snap = _snap("youtube - chrome", idle=0)
        # 1차 호출: 카운터만 시작
        assert t._evaluate(snap) is None
        # dwell 만큼 시간이 지났다고 위장
        t._weakness_started -= t.WEAKNESS_DWELL_SEC + 1.0
        result = t._evaluate(snap)
        assert result is tracker_mod.TriggerType.PERSONAL_WEAKNESS

    def test_weakness_resets_when_window_changes(self, mk_tracker):
        t = mk_tracker(weak_spots=["youtube"])
        t._evaluate(_snap("youtube - chrome"))
        assert t._weakness_started is not None
        # 다른 창으로 이동 — 카운터 리셋되어야
        t._evaluate(_snap("notepad - 메모"))
        assert t._weakness_started is None


# ------------------------------------------------------ Pomodoro 휴식 1세션 1회
class TestPomodoroTrigger:
    def test_fires_after_duration(self, mk_tracker):
        t = mk_tracker()
        snap = _snap("etc-app", idle=0)
        t._evaluate(snap)
        assert t._pomodoro_started is not None
        # 50분 경과 위장
        t._pomodoro_started -= t.POMODORO_DURATION + 1.0
        # idle 0 + 도파민 아닌 창 → 다른 트리거 안 잡힘
        result = t._evaluate(snap)
        assert result is tracker_mod.TriggerType.POMODORO_BREAK
        assert t._pomodoro_fired is True

    def test_one_per_session(self, mk_tracker):
        t = mk_tracker()
        t._evaluate(_snap("etc-app"))
        t._pomodoro_started -= t.POMODORO_DURATION + 1.0
        t._evaluate(_snap("etc-app"))  # 첫 발사
        # 같은 세션에서 또 50분 경과 위장 — 한 번 더 발사하면 안 됨
        t._pomodoro_started = time.time() - (t.POMODORO_DURATION + 1.0)
        result = t._evaluate(_snap("etc-app"))
        # _pomodoro_fired 가 True 라 다시 발사 안 됨 → 다른 트리거도 매칭 X 면 None
        assert result is not tracker_mod.TriggerType.POMODORO_BREAK

    def test_idle_reset_allows_new_session(self, mk_tracker):
        t = mk_tracker()
        t._evaluate(_snap("etc-app"))
        t._pomodoro_started -= t.POMODORO_DURATION + 1.0
        t._evaluate(_snap("etc-app"))  # 첫 발사
        # idle 5분+ → 휴식으로 인정 → 카운터·플래그 리셋
        t._evaluate(_snap("etc-app", idle=t.BREAK_IDLE_SEC + 1.0))
        assert t._pomodoro_fired is False
        assert t._pomodoro_started is None
