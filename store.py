"""
store.py — 앱 상태 영속화 (state.json)

여러 스레드(텔레그램 폴링 / 트래커 / 프로액티브)가 공유하므로
모든 접근을 RLock으로 보호하고, 변경 시마다 원자적으로 파일에 쓴다.

저장 항목:
    chat_id         등록된 텔레그램 사용자
    today_goals     오늘의 단기 목표들 (여러 개 가능)
    long_term_goal  장기 목표
    progress        최근 진행 상황 메모
    next_step       지금 할 다음 구체 행동
    profile         코치가 알아낸 사용자 정보 {항목: 내용, ...}
    weak_spots      자주 무너지는 앱·사이트 키워드 목록
    alarms          예약된 시간 기반 알람 [{text, repeat, next_ts, label}, ...]
    habits          추적 중인 습관 [{name, levels, level_idx, streak, ...}, ...]
    history         최근 대화 턴 [{role, text}, ...]
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Any, Dict, List, Optional


class Store:
    MAX_HISTORY = 40              # 최근 N개 턴만 유지 (user/model 합산)
    HABIT_LEVELUP_THRESHOLD = 3   # 한 레벨에서 N회 성공하면 다음 레벨로

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {
            "chat_id": None,
            "today_goals": [],
            "long_term_goal": None,
            "progress": None,
            "next_step": None,
            "profile": {},
            "weak_spots": [],
            "alarms": [],
            "habits": [],
            "history": [],
        }
        self._load()

    # ----------------------------------------------------------------- I/O
    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fp:
                saved = json.load(fp)
            for key in self._data:
                if key in saved:
                    self._data[key] = saved[key]
            print(f"[Store] 상태 복원 완료: {self._path}")
        except Exception as exc:
            print(f"[Store] 상태 로드 실패 (기본값 사용): {exc}")

    def _persist(self) -> None:
        """RLock을 잡은 상태에서 호출. 임시 파일에 쓴 뒤 원자적으로 교체."""
        tmp = f"{self._path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            print(f"[Store] 상태 저장 실패: {exc}")

    def _set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._persist()

    def _get(self, key: str) -> Any:
        with self._lock:
            return self._data[key]

    # -------------------------------------------------------------- 속성
    @property
    def chat_id(self) -> Optional[int]:
        return self._get("chat_id")

    @chat_id.setter
    def chat_id(self, value: Optional[int]) -> None:
        self._set("chat_id", value)

    @property
    def today_goals(self) -> List[str]:
        """오늘의 단기 목표 목록 (복사본)."""
        with self._lock:
            return list(self._data["today_goals"])

    def add_today_goal(self, goal: str) -> bool:
        """오늘 목표를 추가한다. 이미 같은 게 있으면 False."""
        goal = goal.strip()
        if not goal:
            return False
        with self._lock:
            for g in self._data["today_goals"]:
                if g.lower() == goal.lower():
                    return False
            self._data["today_goals"].append(goal)
            self._persist()
            return True

    def complete_today_goal(self, goal: str) -> bool:
        """끝낸 오늘 목표를 목록에서 제거한다 (부분 일치). 제거했으면 True."""
        key = goal.strip().lower()
        if not key:
            return False
        with self._lock:
            before = len(self._data["today_goals"])
            self._data["today_goals"] = [
                g for g in self._data["today_goals"]
                if key not in g.lower() and g.lower() not in key
            ]
            changed = len(self._data["today_goals"]) != before
            if changed:
                self._persist()
            return changed

    @property
    def long_term_goal(self) -> Optional[str]:
        return self._get("long_term_goal")

    @long_term_goal.setter
    def long_term_goal(self, value: Optional[str]) -> None:
        self._set("long_term_goal", value)

    @property
    def progress(self) -> Optional[str]:
        return self._get("progress")

    @progress.setter
    def progress(self, value: Optional[str]) -> None:
        self._set("progress", value)

    @property
    def next_step(self) -> Optional[str]:
        return self._get("next_step")

    @next_step.setter
    def next_step(self, value: Optional[str]) -> None:
        self._set("next_step", value)

    # --------------------------------------------------------- 사용자 프로필
    @property
    def profile(self) -> Dict[str, str]:
        """코치가 대화에서 알아낸 사용자 정보 (나이·직업·취미·성격 등)."""
        with self._lock:
            return dict(self._data["profile"])

    def update_profile(self, updates: Dict[str, str]) -> None:
        """알아낸 사용자 정보를 프로필에 병합한다 (기존 항목은 덮어씀)."""
        with self._lock:
            self._data["profile"].update(updates)
            self._persist()

    @property
    def weak_spots(self) -> List[str]:
        """코치가 학습한, 사용자가 자주 무너지는 앱·사이트 키워드 목록."""
        with self._lock:
            return list(self._data["weak_spots"])

    def add_weak_spots(self, items: List[str]) -> None:
        """학습한 약점 키워드를 목록에 추가한다 (중복은 건너뜀)."""
        with self._lock:
            seen = {s.lower() for s in self._data["weak_spots"]}
            for item in items:
                item = item.strip()
                if item and item.lower() not in seen:
                    self._data["weak_spots"].append(item)
                    seen.add(item.lower())
            self._persist()

    # --------------------------------------------------------- 예약 알람
    @property
    def alarms(self) -> List[dict]:
        """예약된 시간 기반 알람 목록 (각 항목의 복사본)."""
        with self._lock:
            return [dict(a) for a in self._data["alarms"]]

    def add_alarm(self, alarm: dict) -> None:
        """알람 하나를 예약 목록에 추가한다."""
        with self._lock:
            self._data["alarms"].append(alarm)
            self._persist()

    def replace_alarms(self, alarms: List[dict]) -> None:
        """알람 목록을 통째로 교체한다 (스케줄러가 발송·재예약·삭제 후 기록)."""
        with self._lock:
            self._data["alarms"] = list(alarms)
            self._persist()

    # --------------------------------------------------------- 습관 추적
    @property
    def habits(self) -> List[dict]:
        """추적 중인 습관 목록 (각 항목의 복사본)."""
        with self._lock:
            return [dict(h) for h in self._data["habits"]]

    def add_habit(self, name: str, levels: List[str]) -> bool:
        """새 습관을 등록한다 (난이도 레벨 단계 포함). 이미 있으면 False."""
        name = name.strip()
        if not name:
            return False
        with self._lock:
            for h in self._data["habits"]:
                if h["name"].lower() == name.lower():
                    return False
            self._data["habits"].append({
                "name": name,
                "levels": [str(lv) for lv in levels],
                "level_idx": 0,
                "level_progress": 0,
                "streak": 0,
                "last_done": None,
                "created": datetime.date.today().isoformat(),
            })
            self._persist()
            return True

    def mark_habit_done(self, name: str) -> Optional[dict]:
        """습관을 '오늘 수행'으로 기록한다. 연속 일수(streak)와 레벨 진척을
        갱신하고, 한 레벨에서 충분히 성공하면 다음 레벨로 올린다 (마지막
        레벨이 상한). 등록 안 된 습관이면 새로 만든다.
        {streak, level, leveled_up} 를 돌려준다."""
        name = name.strip()
        if not name:
            return None
        today = datetime.date.today()
        today_s = today.isoformat()
        yesterday_s = (today - datetime.timedelta(days=1)).isoformat()
        with self._lock:
            habit = None
            for h in self._data["habits"]:
                hn = h["name"].lower()
                if hn == name.lower() or name.lower() in hn or hn in name.lower():
                    habit = h
                    break
            if habit is None:
                habit = {
                    "name": name, "levels": [], "level_idx": 0,
                    "level_progress": 0, "streak": 0,
                    "last_done": None, "created": today_s,
                }
                self._data["habits"].append(habit)

            leveled_up = False
            if habit["last_done"] != today_s:
                if habit["last_done"] == yesterday_s:
                    habit["streak"] = habit.get("streak", 0) + 1
                else:
                    habit["streak"] = 1  # 처음이거나 연속이 끊김
                habit["last_done"] = today_s

                levels = habit.get("levels") or []
                idx = habit.get("level_idx", 0)
                habit["level_progress"] = habit.get("level_progress", 0) + 1
                if (habit["level_progress"] >= self.HABIT_LEVELUP_THRESHOLD
                        and idx < len(levels) - 1):
                    habit["level_idx"] = idx + 1
                    habit["level_progress"] = 0
                    leveled_up = True
                self._persist()

            levels = habit.get("levels") or []
            idx = habit.get("level_idx", 0)
            cur = levels[idx] if 0 <= idx < len(levels) else None
            return {
                "streak": habit["streak"],
                "level": cur,
                "leveled_up": leveled_up,
            }

    # --------------------------------------------------------- 대화 기록
    @property
    def history(self) -> List[Dict[str, str]]:
        with self._lock:
            return list(self._data["history"])

    def append_turn(self, role: str, text: str) -> None:
        with self._lock:
            self._data["history"].append({"role": role, "text": text})
            overflow = len(self._data["history"]) - self.MAX_HISTORY
            if overflow > 0:
                # 가장 오래된 것부터 제거. user/model 짝이 어긋나지 않도록 짝수로.
                if overflow % 2:
                    overflow += 1
                del self._data["history"][:overflow]
            self._persist()

    def clear_conversation(self) -> None:
        """대화 기록·목표·진행 메모를 비운다. chat_id는 유지한다."""
        with self._lock:
            self._data["history"] = []
            self._data["today_goals"] = []
            self._data["long_term_goal"] = None
            self._data["progress"] = None
            self._data["next_step"] = None
            self._data["profile"] = {}
            self._data["weak_spots"] = []
            self._data["alarms"] = []
            self._data["habits"] = []
            self._persist()
