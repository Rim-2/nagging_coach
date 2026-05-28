"""store.py 핵심 동작 단위 테스트.

격리: 각 테스트가 tmp_path 에 별도 state.json 을 두므로 서로 영향 없음.
실 LLM·텔레그램 호출은 일절 없다 (store 는 순수 데이터 레이어).
"""

from __future__ import annotations

import time

import pytest

import store as store_mod


@pytest.fixture
def fresh_store(tmp_path):
    """비어있는 새 Store — 매 테스트 격리."""
    return store_mod.Store(str(tmp_path / "state.json"))


# ----------------------------------------------------------------- nag_policy
class TestNagPolicy:
    def test_default_balanced(self, fresh_store):
        assert fresh_store.nag_policy == "balanced"
        assert fresh_store.active_nag_policy == "balanced"

    def test_explicit_change(self, fresh_store):
        fresh_store.nag_policy = "strict"
        assert fresh_store.nag_policy == "strict"
        assert fresh_store.active_nag_policy == "strict"

    def test_invalid_value_ignored(self, fresh_store):
        fresh_store.nag_policy = "bogus"  # type: ignore[assignment]
        assert fresh_store.nag_policy == "balanced"

    def test_temp_overrides_base(self, fresh_store):
        fresh_store.nag_policy = "strict"
        fresh_store.apply_temporary_policy("gentle", duration_sec=10.0)
        assert fresh_store.active_nag_policy == "gentle"
        # base 는 보존
        assert fresh_store.nag_policy == "strict"

    def test_temp_expires_to_base(self, fresh_store):
        fresh_store.apply_temporary_policy("gentle", duration_sec=0.01)
        time.sleep(0.05)
        assert fresh_store.active_nag_policy == "balanced"

    def test_recovery_flag_consumed_once(self, fresh_store):
        fresh_store.apply_temporary_policy("gentle", duration_sec=0.01)
        time.sleep(0.05)
        # active_nag_policy 한 번 호출하면 만료 감지 + 플래그 set
        assert fresh_store.active_nag_policy == "balanced"
        assert fresh_store.consume_policy_recovery_note() is True
        # 두 번째는 False
        assert fresh_store.consume_policy_recovery_note() is False


# ----------------------------------------------------------------- hard_reset
class TestHardReset:
    def test_preserves_chat_id_clears_rest(self, fresh_store):
        fresh_store.chat_id = 42
        fresh_store.nag_policy = "strict"
        fresh_store.nag_policy_asked = True
        fresh_store.add_pending_message(
            kind="alarm", chat_id=42, text="x", side_effects={}
        )
        fresh_store.set_pending_chat_reply("foo")

        fresh_store.hard_reset()

        assert fresh_store.chat_id == 42  # 봇 등록 자체는 살림
        assert fresh_store.nag_policy == "balanced"
        assert fresh_store.nag_policy_asked is False
        assert fresh_store.list_pending_messages() == []
        assert fresh_store.get_pending_chat_reply() is None


# --------------------------------------------------------- pending_messages 큐
class TestPendingMessageQueue:
    def test_add_list_remove(self, fresh_store):
        mid = fresh_store.add_pending_message(
            kind="remote_trigger", chat_id=1, text="hi",
            side_effects={"trigger_value": "개인 약점 앱"},
        )
        items = fresh_store.list_pending_messages()
        assert len(items) == 1
        assert items[0]["id"] == mid
        assert items[0]["attempts"] == 0
        removed = fresh_store.remove_pending_message(mid)
        assert removed and removed["text"] == "hi"
        assert fresh_store.list_pending_messages() == []

    def test_update_attempts_persists(self, fresh_store):
        mid = fresh_store.add_pending_message(
            kind="alarm", chat_id=1, text="x", side_effects={},
        )
        fresh_store.update_pending_attempt(mid, time.time() + 60.0)
        items = fresh_store.list_pending_messages()
        assert items[0]["attempts"] == 1
        # 두 번째 update → 2 로
        fresh_store.update_pending_attempt(mid, time.time() + 120.0)
        items = fresh_store.list_pending_messages()
        assert items[0]["attempts"] == 2

    def test_order_preserved(self, fresh_store):
        a = fresh_store.add_pending_message(
            kind="alarm", chat_id=1, text="first", side_effects={}
        )
        time.sleep(0.01)
        b = fresh_store.add_pending_message(
            kind="alarm", chat_id=1, text="second", side_effects={}
        )
        items = sorted(
            fresh_store.list_pending_messages(),
            key=lambda it: it["created_at"],
        )
        assert items[0]["id"] == a
        assert items[1]["id"] == b


# ------------------------------------------------------------ hourly_breakdown
class TestHourlyBreakdown:
    def test_empty_state(self, fresh_store):
        hb = fresh_store.hourly_breakdown(days=7)
        assert hb["total_days_with_data"] == 0
        assert hb["golden_hours"] == []
        assert hb["risk_hours"] == []

    def test_triggers_count_toward_risk(self, fresh_store):
        fresh_store.bump_trigger_fire("개인 약점 앱")
        fresh_store.bump_trigger_fire("개인 약점 앱")
        hb = fresh_store.hourly_breakdown(days=7)
        assert hb["total_days_with_data"] == 1
        assert len(hb["risk_hours"]) == 1
        hour, count = hb["risk_hours"][0]
        assert count == 2

    def test_invalid_trigger_label_ignored(self, fresh_store):
        # 빈 라벨은 카운터에 안 들어감
        fresh_store.bump_trigger_fire("")
        fresh_store.bump_trigger_fire("   ")
        hb = fresh_store.hourly_breakdown(days=7)
        assert hb["risk_hours"] == []


# ------------------------------------------------------------ dopamine_trails
class TestDopamineTrails:
    def test_short_sequence_ignored(self, fresh_store):
        fresh_store.bump_dopamine_trail(["a"])
        fresh_store.bump_dopamine_trail(["a", "b"])
        assert fresh_store.top_dopamine_trails(min_count=1) == []

    def test_repeated_trail_aggregates(self, fresh_store):
        for _ in range(3):
            fresh_store.bump_dopamine_trail(["mail", "twitter", "youtube"])
        top = fresh_store.top_dopamine_trails(n=5, min_count=2)
        assert len(top) == 1
        seq, count = top[0]
        assert seq == ["mail", "twitter", "youtube"]
        assert count == 3

    def test_min_count_filter(self, fresh_store):
        fresh_store.bump_dopamine_trail(["a", "b", "c"])  # count=1
        fresh_store.bump_dopamine_trail(["a", "b", "d"])  # count=1
        # min_count=2 면 둘 다 노이즈로 제외
        assert fresh_store.top_dopamine_trails(min_count=2) == []


# -------------------------------------------------------- 목표 카운트 정밀화
class TestTodayGoalsCounting:
    def test_add_increments_registered(self, fresh_store):
        assert fresh_store.add_today_goal("발표 자료")
        from datetime import date
        bucket = fresh_store.daily_stats[date.today().isoformat()]
        assert bucket["goals_registered"] == 1
        assert bucket.get("goals_completed", 0) == 0

    def test_exact_match_preferred_over_substring(self, fresh_store):
        # 짧은 이름이 긴 이름의 substring 인 케이스 — 정확 매칭 우선
        fresh_store.add_today_goal("A")
        fresh_store.add_today_goal("AB")
        # "A" 완료 → "A" 만 사라져야 (정확 매칭)
        assert fresh_store.complete_today_goal("A") is True
        names = [g["name"] for g in fresh_store.today_goals_detailed]
        assert names == ["AB"]

    def test_ambiguous_substring_blocks(self, fresh_store):
        # 두 목표 모두에 매칭되는 모호한 키워드 → False, 둘 다 그대로
        fresh_store.add_today_goal("발표 자료 만들기")
        fresh_store.add_today_goal("발표 자료 검토")
        # "발표 자료" 만 보내면 둘 다 매칭 → 모호 → 처리 안 함
        assert fresh_store.complete_today_goal("발표 자료") is False
        assert len(fresh_store.today_goals_detailed) == 2

    def test_cancel_same_day_decrements_registered(self, fresh_store):
        fresh_store.add_today_goal("취소될 목표")
        assert fresh_store.cancel_today_goal("취소될 목표") is True
        from datetime import date
        bucket = fresh_store.daily_stats[date.today().isoformat()]
        # 같은 날 등록·취소 → 분모도 -1 (시도조차 안 한 것)
        assert bucket["goals_registered"] == 0
        # 완료가 아니므로 goals_completed 는 0
        assert bucket.get("goals_completed", 0) == 0

    def test_advance_records_sub_step_stat(self, fresh_store):
        fresh_store.add_today_goal("발표", sub_steps=["골격", "본문", "리허설"])
        result = fresh_store.advance_today_goal("발표")
        assert result is not None
        assert result["current"] == 1
        assert result["completed"] is False
        from datetime import date
        bucket = fresh_store.daily_stats[date.today().isoformat()]
        assert bucket.get("sub_step_advances", 0) == 1
        # 아직 마지막 단계 X — goals_completed 안 올라감
        assert bucket.get("goals_completed", 0) == 0

    def test_advance_last_step_completes_goal(self, fresh_store):
        fresh_store.add_today_goal("발표", sub_steps=["A", "B"])
        fresh_store.advance_today_goal("발표")
        result = fresh_store.advance_today_goal("발표")
        assert result["completed"] is True
        from datetime import date
        bucket = fresh_store.daily_stats[date.today().isoformat()]
        assert bucket["goals_completed"] == 1
        assert bucket["sub_step_advances"] == 2
        assert fresh_store.today_goals_detailed == []

    def test_ambiguous_advance_blocked(self, fresh_store):
        # 모호한 매칭이면 advance 도 None — 의도치 않은 다중 진척 방지
        fresh_store.add_today_goal("발표 자료", sub_steps=["A", "B"])
        fresh_store.add_today_goal("발표 연습", sub_steps=["C", "D"])
        result = fresh_store.advance_today_goal("발표")
        # 둘 다 substring 매칭 → 모호 → None
        assert result is None
