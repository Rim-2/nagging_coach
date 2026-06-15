"""스몰스텝 보상 — 진척 바 + 마일스톤 헬퍼 단위 테스트.

agent_tools 의 순수 헬퍼만 검증 (LLM·store 의존 없음).
"""

from __future__ import annotations

import agent_tools as at


class TestProgressBar:
    def test_empty(self):
        assert at._progress_bar(0, 5) == "▱▱▱▱▱"

    def test_partial(self):
        assert at._progress_bar(3, 5) == "▰▰▰▱▱"

    def test_full(self):
        assert at._progress_bar(5, 5) == "▰▰▰▰▰"

    def test_zero_total_safe(self):
        assert at._progress_bar(0, 0) == ""

    def test_large_total_scaled_to_ten(self):
        bar = at._progress_bar(10, 20)
        assert len(bar) == 10            # 20단계 → 10칸으로 스케일
        assert bar.count("▰") == 5       # 절반

    def test_never_overflows(self):
        bar = at._progress_bar(7, 7)
        assert bar.count("▱") == 0


class TestStepMilestone:
    def test_first_at_ten(self):
        assert at._step_milestone(10) is True

    def test_below_ten_false(self):
        assert at._step_milestone(9) is False
        assert at._step_milestone(5) is False

    def test_twenty_five_multiples(self):
        for v in (25, 50, 75, 100, 200):
            assert at._step_milestone(v) is True

    def test_non_milestone_values(self):
        for v in (11, 24, 26, 49, 99):
            assert at._step_milestone(v) is False
