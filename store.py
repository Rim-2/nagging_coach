"""
store.py — 앱 상태 영속화 (state.json)

여러 스레드(텔레그램 폴링 / 트래커 / 프로액티브)가 공유하므로
모든 접근을 RLock으로 보호하고, 변경 시마다 원자적으로 파일에 쓴다.

저장 항목:
    chat_id           등록된 텔레그램 사용자
    today_goals       오늘의 단기 목표들 (여러 개 가능)
    long_term_goal    장기 목표
    progress          최근 진행 상황 메모
    next_step         지금 할 다음 구체 행동
    profile           코치가 알아낸 사용자 정보 {항목: 내용, ...}
    weak_spots        자주 무너지는 앱·사이트 키워드 목록
    alarms            예약된 시간 기반 알람 [{text, repeat, next_ts, label}, ...]
    events            봇 자체에 등록된 일정 [{id, summary, start_ts, end_ts, reminder_lead_min, reminded}]
    habits            추적 중인 습관 [{name, levels, level_idx, streak, ...}, ...]
    history           최근 대화 턴 [{role, text}, ...]
    nag_policy        잔소리 강도 — "gentle"|"balanced"|"strict" (사용자 발화로만 변경)
    nag_policy_asked  첫 잔소리 직후 톤 조절 안내 노출 여부 — 한 번만 띄움
    daily_stats       날짜별 자가 격려용 누적 카운터
                      {YYYY-MM-DD: {triggers: {label: n}, goals_completed: n, habit_dones: n}}
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional


def _aggregate_buckets(buckets: List[dict]) -> dict:
    """daily_stats 의 N일치 버킷을 합쳐 한 요약 dict 으로. weekly_summary 가 씀."""
    triggers: Dict[str, int] = {}
    goals_completed = 0
    goals_registered = 0
    habit_dones = 0
    mood_ratings: List[int] = []
    mood_notes: List[str] = []
    for b in buckets:
        for label, cnt in (b.get("triggers") or {}).items():
            triggers[label] = triggers.get(label, 0) + int(cnt or 0)
        goals_completed += int(b.get("goals_completed") or 0)
        goals_registered += int(b.get("goals_registered") or 0)
        habit_dones += int(b.get("habit_dones") or 0)
        for log in (b.get("mood_logs") or []):
            try:
                mood_ratings.append(int(log.get("rating", 0)))
                note = str(log.get("note", "")).strip()
                if note:
                    mood_notes.append(note)
            except Exception:
                continue
    trigger_total = sum(triggers.values())
    top_trigger = max(triggers.items(), key=lambda x: x[1])[0] if triggers else ""
    # 완료율은 분모(등록) 가 0 이면 None — '데이터 부족'으로 표시할 수 있게.
    completion_rate: Optional[float] = (
        goals_completed / goals_registered if goals_registered > 0 else None
    )
    mood_avg: Optional[float] = (
        sum(mood_ratings) / len(mood_ratings) if mood_ratings else None
    )
    return {
        "trigger_total": trigger_total,
        "top_trigger": top_trigger,
        "trigger_counts": triggers,
        "goals_completed": goals_completed,
        "goals_registered": goals_registered,
        "completion_rate": completion_rate,
        "habit_dones": habit_dones,
        "mood_avg": mood_avg,
        "mood_count": len(mood_ratings),
        "mood_notes_recent": mood_notes[-3:],  # 가장 최근 3개 메모
    }


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
            "events": [],
            "habits": [],
            "history": [],
            "nag_policy": "balanced",
            "nag_policy_asked": False,
            "daily_stats": {},
            "last_weekly_review": None,
            "last_overload_checkin": None,
            "last_late_night_fired": None,  # YYYY-MM-DD — 하루 한 번 제약 영속화
            "implementation_intentions": [],
            "weak_spot_candidates": {},
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
            # today_goals 자동 마이그레이션: 옛 List[str] → 새 List[Dict].
            # 옛 형식으로 저장된 production state 도 깨지지 않게 한 번 변환해 둔다.
            migrated: List[dict] = []
            for g in self._data.get("today_goals", []):
                if isinstance(g, str):
                    name = g.strip()
                    if name:
                        migrated.append(
                            {"name": name, "sub_steps": [], "current": 0}
                        )
                elif isinstance(g, dict):
                    name = str(g.get("name", "")).strip()
                    if not name:
                        continue
                    migrated.append({
                        "name": name,
                        "sub_steps": [
                            str(s).strip()
                            for s in (g.get("sub_steps") or [])
                            if str(s).strip()
                        ],
                        "current": int(g.get("current", 0) or 0),
                    })
            self._data["today_goals"] = migrated
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
        """오늘 목표 이름 목록 (복사본) — 기존 호출자 호환 유지를 위해 이름만."""
        with self._lock:
            return [g["name"] for g in self._data["today_goals"]]

    @property
    def today_goals_detailed(self) -> List[dict]:
        """오늘 목표 전체 정보 (sub_steps 와 current 포함). 진척도 표시용."""
        with self._lock:
            return [dict(g) for g in self._data["today_goals"]]

    def add_today_goal(
        self, goal: str, sub_steps: Optional[List[str]] = None
    ) -> bool:
        """오늘 목표를 추가한다. 이미 같은 이름이 있으면 False.
        sub_steps 가 있으면 단계별로 등록 — 시스템이 current 로 진척 관리."""
        goal = goal.strip()
        if not goal:
            return False
        with self._lock:
            for g in self._data["today_goals"]:
                if g["name"].lower() == goal.lower():
                    return False
            self._data["today_goals"].append({
                "name": goal,
                "sub_steps": [
                    str(s).strip() for s in (sub_steps or []) if str(s).strip()
                ],
                "current": 0,
            })
            # 캐파 분모 — '오늘 시도한 일'이 얼마나 되는지. 완료/시도 비율을
            # weekly_summary 가 보고 코치가 사용자 캐파에 맞게 분량을 조절.
            bucket = self._today_bucket()
            bucket["goals_registered"] = bucket.get("goals_registered", 0) + 1
            self._persist()
            return True

    def advance_today_goal(self, goal: str) -> Optional[dict]:
        """sub_step 하나 완료로 표시. current += 1, 마지막을 넘기면 goal 자체를
        완료 처리(목록에서 제거 + goals_completed 카운터 +1). sub_steps 가 없는
        goal 이거나 일치하는 goal 이 없으면 None.
        반환: {next_step: 다음 sub_step 또는 None, current, total, completed: bool}"""
        key = goal.strip().lower()
        if not key:
            return None
        with self._lock:
            target = None
            for g in self._data["today_goals"]:
                n = g["name"].lower()
                if n == key or key in n or n in key:
                    target = g
                    break
            if target is None:
                return None
            sub = target.get("sub_steps") or []
            if not sub:
                return None
            target["current"] = min(target.get("current", 0) + 1, len(sub))
            completed = target["current"] >= len(sub)
            result = {
                "next_step": (
                    sub[target["current"]] if not completed else None
                ),
                "current": target["current"],
                "total": len(sub),
                "completed": completed,
            }
            if completed:
                bucket = self._today_bucket()
                bucket["goals_completed"] = bucket.get("goals_completed", 0) + 1
                self._data["today_goals"] = [
                    g for g in self._data["today_goals"] if g is not target
                ]
            self._persist()
            return result

    def complete_today_goal(self, goal: str) -> bool:
        """끝낸 오늘 목표를 목록에서 제거한다 (부분 일치, name 기준). 제거했으면
        True — 일별 통계의 goals_completed 도 함께 +1."""
        key = goal.strip().lower()
        if not key:
            return False
        with self._lock:
            before = len(self._data["today_goals"])
            self._data["today_goals"] = [
                g for g in self._data["today_goals"]
                if key not in g["name"].lower() and g["name"].lower() not in key
            ]
            changed = len(self._data["today_goals"]) != before
            if changed:
                bucket = self._today_bucket()
                bucket["goals_completed"] = bucket.get("goals_completed", 0) + 1
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

    # ------------------------------------------- 약점 후보 (자기학습)
    def bump_weak_spot_candidate(self, app_label: str) -> None:
        """트리거가 잡힌 앱/사이트 라벨을 후보 카운터에 +1. 주간 회고 때
        가장 자주 잡힌 후보를 사용자한테 '추가할까?' 확인 받는 데 쓴다."""
        label = (app_label or "").strip()
        if not label:
            return
        with self._lock:
            cands = self._data.setdefault("weak_spot_candidates", {})
            cands[label] = cands.get(label, 0) + 1
            # 비대 방지 — 100개 넘으면 빈도 낮은 절반 정리
            if len(cands) > 100:
                ranked = sorted(cands.items(), key=lambda kv: kv[1])
                for k, _ in ranked[:50]:
                    cands.pop(k, None)
            self._persist()

    def top_weak_spot_candidates(self, n: int = 5) -> List[tuple]:
        """가장 자주 잡힌 후보 N개 — 이미 weak_spots 에 있는 건 제외.
        반환: [(label, count), ...] 내림차순."""
        with self._lock:
            existing = {s.lower() for s in self._data["weak_spots"]}
            items = [
                (k, v)
                for k, v in self._data.get("weak_spot_candidates", {}).items()
                if k.lower() not in existing
            ]
            items.sort(key=lambda kv: -kv[1])
            return items[:n]

    def reset_weak_spot_candidates(self) -> None:
        """후보 카운터 초기화 — 주간 회고 발송 성공 시 호출 (새 주 시작)."""
        with self._lock:
            self._data["weak_spot_candidates"] = {}
            self._persist()

    # --------------------------------------------------------- 일별 통계
    DAILY_STATS_MAX_DAYS = 60     # 너무 오래된 날짜는 자동 정리 (메모리·저장 비대 방지)

    def _today_bucket(self) -> dict:
        """오늘 날짜의 통계 버킷을 (RLock 잡힌 상태에서) 가져온다 — 없으면 생성."""
        today = datetime.date.today().isoformat()
        stats = self._data["daily_stats"]
        bucket = stats.get(today)
        if bucket is None:
            bucket = {
                "triggers": {},
                "goals_completed": 0,
                "goals_registered": 0,
                "habit_dones": 0,
                "mood_logs": [],
            }
            stats[today] = bucket
            # 오래된 날짜 자동 정리
            if len(stats) > self.DAILY_STATS_MAX_DAYS:
                for k in sorted(stats.keys())[: len(stats) - self.DAILY_STATS_MAX_DAYS]:
                    stats.pop(k, None)
        return bucket

    def add_mood_log(self, rating: int, note: str = "") -> None:
        """오늘 자 통계에 가벼운 mood 기록 (1~5) + 메모. behavioral activation
        의 핵심 — 활동-기분 연결을 weekly_summary 에서 사용자에게 돌려준다."""
        try:
            r = int(rating)
        except Exception:
            return
        r = max(1, min(5, r))
        with self._lock:
            bucket = self._today_bucket()
            logs = bucket.setdefault("mood_logs", [])
            logs.append({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "rating": r,
                "note": (note or "").strip()[:200],
            })
            self._persist()

    def bump_trigger_fire(self, trigger_value: str) -> None:
        """잔소리가 실제 발송된 트리거를 오늘 자 통계에 +1. 쿨다운으로 스킵된
        건 카운트하지 않는다 — 사용자 체감 패턴만 잡기 위함."""
        label = (trigger_value or "").strip()
        if not label:
            return
        with self._lock:
            bucket = self._today_bucket()
            bucket["triggers"][label] = bucket["triggers"].get(label, 0) + 1
            self._persist()

    @property
    def daily_stats(self) -> Dict[str, dict]:
        """일별 통계 (얕은 복사본)."""
        with self._lock:
            return {k: dict(v) for k, v in self._data["daily_stats"].items()}

    def weekly_summary(self, days: int = 7) -> dict:
        """최근 N일 + 그 전 N일 누적치를 비교용으로 요약.
        반환 형태:
            {"window_days": N,
             "recent": {trigger_total, top_trigger, goals_completed, habit_dones},
             "previous": {...같은 키...}}
        """
        today = datetime.date.today()
        recent_dates = {
            (today - datetime.timedelta(days=i)).isoformat() for i in range(days)
        }
        previous_dates = {
            (today - datetime.timedelta(days=i)).isoformat()
            for i in range(days, days * 2)
        }
        with self._lock:
            stats = self._data["daily_stats"]
            recent_buckets = [stats[d] for d in recent_dates if d in stats]
            previous_buckets = [stats[d] for d in previous_dates if d in stats]
        return {
            "window_days": days,
            "recent": _aggregate_buckets(recent_buckets),
            "previous": _aggregate_buckets(previous_buckets),
        }

    def mood_correlation(self, days: int = 14) -> dict:
        """일별 mood 평균을 activity 그룹별로 비교 — '목표 완료한 날 / 안 한 날'
        등으로 mood 가 어떻게 다른지 결정론적으로 계산.
        충분한 mood 기록이 있는 날 4일 미만이면 insufficient_data=True.
        반환 dict 의 각 비교 결과는 mood 평균 차이가 0.5 미만이면 None — 노이즈로 본다."""
        today = datetime.date.today()
        target_dates = [
            (today - datetime.timedelta(days=i)).isoformat()
            for i in range(days)
        ]
        with self._lock:
            stats_data = self._data["daily_stats"]
            day_data: List[Dict[str, Any]] = []
            for d in target_dates:
                b = stats_data.get(d)
                if not b:
                    continue
                moods = b.get("mood_logs") or []
                if not moods:
                    continue
                ratings = [int(m.get("rating", 0) or 0) for m in moods]
                if not ratings:
                    continue
                day_data.append({
                    "date": d,
                    "mood_avg": sum(ratings) / len(ratings),
                    "goals_completed": int(b.get("goals_completed", 0) or 0),
                    "habit_dones": int(b.get("habit_dones", 0) or 0),
                    "trigger_total": sum(
                        int(v or 0) for v in (b.get("triggers") or {}).values()
                    ),
                })

        if len(day_data) < 4:
            return {"insufficient_data": True, "log_days": len(day_data)}

        def _avg(arr: list, key: str = "mood_avg") -> Optional[float]:
            if not arr:
                return None
            return sum(d[key] for d in arr) / len(arr)

        def _compare(with_arr: list, without_arr: list) -> Optional[dict]:
            with_avg = _avg(with_arr)
            without_avg = _avg(without_arr)
            if with_avg is None or without_avg is None:
                return None
            diff = with_avg - without_avg
            if abs(diff) < 0.5:
                return None  # 노이즈
            return {
                "with": round(with_avg, 2),
                "without": round(without_avg, 2),
                "diff": round(diff, 2),
                "n_with": len(with_arr),
                "n_without": len(without_arr),
            }

        return {
            "log_days": len(day_data),
            "goal_completion": _compare(
                [d for d in day_data if d["goals_completed"] >= 1],
                [d for d in day_data if d["goals_completed"] == 0],
            ),
            "habit": _compare(
                [d for d in day_data if d["habit_dones"] >= 1],
                [d for d in day_data if d["habit_dones"] == 0],
            ),
            "low_trigger": _compare(
                [d for d in day_data if d["trigger_total"] <= 1],
                [d for d in day_data if d["trigger_total"] >= 3],
            ),
        }

    # --------------------------------------------------- WOOP if-then plan
    @property
    def implementation_intentions(self) -> List[dict]:
        """미리 정해둔 if-then plan 목록 (얕은 복사본).
        WOOP/MCII 의 Plan 단계 — '상황 X 가 오면 행동 Y 한다' 식 자동 반응."""
        with self._lock:
            return [dict(p) for p in self._data["implementation_intentions"]]

    def add_implementation_intention(
        self, situation: str, response: str, related_goal: Optional[str] = None
    ) -> bool:
        sit = (situation or "").strip()
        resp = (response or "").strip()
        if not sit or not resp:
            return False
        with self._lock:
            for p in self._data["implementation_intentions"]:
                if p["situation"].lower() == sit.lower():
                    return False  # 같은 situation 중복 X
            self._data["implementation_intentions"].append({
                "situation": sit,
                "response": resp,
                "related_goal": (related_goal or "").strip() or None,
                "created": datetime.date.today().isoformat(),
            })
            self._persist()
            return True

    def remove_implementation_intention(self, key_substring: str) -> int:
        """situation 또는 response 부분 일치하는 plan 제거. 제거된 갯수 반환."""
        key = (key_substring or "").strip().lower()
        if not key:
            return 0
        with self._lock:
            before = len(self._data["implementation_intentions"])
            self._data["implementation_intentions"] = [
                p for p in self._data["implementation_intentions"]
                if key not in p["situation"].lower()
                and key not in p["response"].lower()
            ]
            removed = before - len(self._data["implementation_intentions"])
            if removed:
                self._persist()
            return removed

    # --------------------------------------------------------- 주간 회고
    @property
    def last_weekly_review(self) -> Optional[str]:
        return self._get("last_weekly_review")

    @last_weekly_review.setter
    def last_weekly_review(self, value: Optional[str]) -> None:
        self._set("last_weekly_review", value)

    # ----------------------------------------------------- 늦은 밤 하루 1회
    @property
    def last_late_night_fired(self) -> Optional[str]:
        """가장 최근 '늦은 밤' 트리거가 발사된 날짜 (YYYY-MM-DD).
        위성이 재시작될 때 메모리 reset 되어 또 발사 시도해도 백엔드가 막도록
        영속화. 클라우드 trigger_value 별 10분 쿨다운보다 강한 제약."""
        return self._get("last_late_night_fired")

    @last_late_night_fired.setter
    def last_late_night_fired(self, value: Optional[str]) -> None:
        self._set("last_late_night_fired", value)

    # ----------------------------------------------------- 과부하 자체 점검
    @property
    def last_overload_checkin(self) -> Optional[float]:
        """가장 최근 'overload checkin'(코치가 톤·목표 크기 의사 확인) 발사 시각."""
        return self._get("last_overload_checkin")

    @last_overload_checkin.setter
    def last_overload_checkin(self, value: Optional[float]) -> None:
        self._set("last_overload_checkin", value)

    # --------------------------------------------------------- 잔소리 강도
    @property
    def nag_policy(self) -> str:
        val = self._get("nag_policy")
        return val if val in ("gentle", "balanced", "strict") else "balanced"

    @nag_policy.setter
    def nag_policy(self, value: str) -> None:
        if value in ("gentle", "balanced", "strict"):
            self._set("nag_policy", value)

    @property
    def nag_policy_asked(self) -> bool:
        return bool(self._get("nag_policy_asked"))

    @nag_policy_asked.setter
    def nag_policy_asked(self, value: bool) -> None:
        self._set("nag_policy_asked", bool(value))

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

    # ------------------------------------------------------- 봇 자체 일정
    @property
    def events(self) -> List[dict]:
        """봇 자체에 등록된 일정 목록 (각 항목의 복사본). Google 캘린더와
        별개 — OAuth 없이 봇 안에서 일정 관리가 가능하도록."""
        with self._lock:
            return [dict(e) for e in self._data["events"]]

    def add_event(
        self,
        summary: str,
        start_ts: float,
        end_ts: Optional[float] = None,
        reminder_lead_min: int = 15,
    ) -> Optional[dict]:
        """일정 등록. 반환된 dict 에 id 가 들어있어 mark/cancel 시 쓸 수 있다."""
        summary = (summary or "").strip()
        if not summary:
            return None
        with self._lock:
            ev = {
                "id": int(time.time() * 1000),  # ms 단위 unique
                "summary": summary,
                "start_ts": float(start_ts),
                "end_ts": float(end_ts) if end_ts else None,
                "reminder_lead_min": max(0, int(reminder_lead_min)),
                "reminded": False,
            }
            self._data["events"].append(ev)
            self._persist()
            return dict(ev)

    def mark_event_reminded(self, event_id: int) -> None:
        with self._lock:
            for ev in self._data["events"]:
                if ev.get("id") == event_id:
                    ev["reminded"] = True
                    self._persist()
                    return

    def cancel_event(self, key_substring: str) -> int:
        """summary 부분 일치하는 일정 제거. 반환: 제거된 갯수."""
        key = (key_substring or "").strip().lower()
        if not key:
            return 0
        with self._lock:
            before = len(self._data["events"])
            self._data["events"] = [
                e for e in self._data["events"]
                if key not in str(e.get("summary", "")).lower()
            ]
            removed = before - len(self._data["events"])
            if removed:
                self._persist()
            return removed

    def prune_past_events(self, keep_seconds_after: float = 3600.0) -> int:
        """일정이 끝나고 N초 이상 지난 것 자동 정리. end_ts 가 있으면 그 기준,
        없으면 start_ts 기준 — 진행 중인 긴 일정이 prune 되지 않도록.
        반환: 제거된 갯수."""
        cutoff = time.time() - keep_seconds_after
        with self._lock:
            before = len(self._data["events"])
            def _is_kept(e: dict) -> bool:
                effective_end = e.get("end_ts") or e.get("start_ts") or 0
                return effective_end >= cutoff
            self._data["events"] = [
                e for e in self._data["events"] if _is_kept(e)
            ]
            removed = before - len(self._data["events"])
            if removed:
                self._persist()
            return removed

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
                bucket = self._today_bucket()
                bucket["habit_dones"] = bucket.get("habit_dones", 0) + 1
                self._persist()

            levels = habit.get("levels") or []
            idx = habit.get("level_idx", 0)
            cur = levels[idx] if 0 <= idx < len(levels) else None
            return {
                "streak": habit["streak"],
                "level": cur,
                "leveled_up": leveled_up,
            }

    def cancel_habit(self, name: str) -> int:
        """습관 제거 (부분 일치). 반환: 제거 갯수."""
        key = (name or "").strip().lower()
        if not key:
            return 0
        with self._lock:
            before = len(self._data["habits"])
            self._data["habits"] = [
                h for h in self._data["habits"]
                if key not in str(h.get("name", "")).lower()
            ]
            removed = before - len(self._data["habits"])
            if removed:
                self._persist()
            return removed

    def cancel_today_goal(self, name: str) -> bool:
        """오늘 목표를 *취소* (완료가 아닌 단순 삭제). complete_today_goal 과
        다름 — daily_stats.goals_completed 카운터 안 올림."""
        key = (name or "").strip().lower()
        if not key:
            return False
        with self._lock:
            before = len(self._data["today_goals"])
            self._data["today_goals"] = [
                g for g in self._data["today_goals"]
                if key not in g["name"].lower() and g["name"].lower() not in key
            ]
            changed = len(self._data["today_goals"]) != before
            if changed:
                self._persist()
            return changed

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
        """대화 기록·목표·진행 메모를 비운다. chat_id 와 nag_policy/daily_stats
        같은 사용자 설정·누적 데이터는 유지한다."""
        with self._lock:
            self._data["history"] = []
            self._data["today_goals"] = []
            self._data["long_term_goal"] = None
            self._data["progress"] = None
            self._data["next_step"] = None
            self._data["profile"] = {}
            self._data["weak_spots"] = []
            self._data["alarms"] = []
            self._data["events"] = []
            self._data["habits"] = []
            self._data["implementation_intentions"] = []
            self._persist()
