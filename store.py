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
    sub_step_advances = 0
    mood_ratings: List[int] = []
    mood_notes: List[str] = []
    for b in buckets:
        for label, cnt in (b.get("triggers") or {}).items():
            triggers[label] = triggers.get(label, 0) + int(cnt or 0)
        goals_completed += int(b.get("goals_completed") or 0)
        goals_registered += int(b.get("goals_registered") or 0)
        habit_dones += int(b.get("habit_dones") or 0)
        sub_step_advances += int(b.get("sub_step_advances") or 0)
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
        "sub_step_advances": sub_step_advances,
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
            # 컨디션 토로로 일시 적용된 톤. 만료되면 base(nag_policy) 로 복귀.
            "nag_policy_temp": "",
            "nag_policy_temp_until": 0.0,     # unix timestamp
            # 직전 호출에서 temp 가 막 만료되었으면 True — 다음 잔소리에 자연스러운
            # '톤 복귀' 코멘트를 끼우기 위한 1회용 플래그. 소비 후 자동 False.
            "nag_policy_recovery_pending": False,
            "daily_stats": {},
            # 누적 '걸음' — sub_step 진척·목표 완료·습관 수행을 한 발씩 누적.
            # 절대 줄지 않는다 (성취감 보상; streak 처럼 끊겨 0 되는 부담 없음).
            "lifetime_steps": 0,
            "last_weekly_review": None,
            "last_overload_checkin": None,
            "last_late_night_fired": None,  # YYYY-MM-DD — 하루 한 번 제약 영속화
            "last_risk_predict": None,      # {"date": YYYY-MM-DD, "hour": int} — 1일 1회 가드
            "last_daily_journal": None,     # YYYY-MM-DD — 하루 마무리 일지 1일 1회 가드
            "last_user_message_at": 0.0,    # epoch — 마지막 사용자 메시지 시각 (밤잠 추론용, 재시작 영속)
            "implementation_intentions": [],
            "weak_spot_candidates": {},
            # 도파민 trail 학습 — 딴짓 트리거 직전 sanitized 라벨 시퀀스 N-gram
            # 카운터. 키: "label1|label2|label3" (길이 3 trail). 값: int 카운트.
            "dopamine_trails": {},
            # 텔레그램 전송 실패 시 영구 retry 큐. 잔소리·자동 메시지 전용.
            # 항목: {id, kind, chat_id, text, side_effects, attempts,
            #        next_attempt_at, first_attempt_at, created_at}
            "pending_messages": [],
            # 대화 답장 (사용자 chat 응답) 이 전송 실패했을 때 *한 개*만 보관.
            # 다음 사용자 메시지 도착 시 LLM 이 합쳐 답하도록 state_summary 에 노출.
            "pending_chat_reply": None,
            # 폰 위성이 최근 트리거에 동봉해 보낸 디바이스 컨텍스트 — daily_stats
            # 같은 누적은 폰이 들고 있고, 백엔드는 *최신값* 만 보관해 description·
            # 자기 격려 메시지에 활용.
            "phone_context": {
                "steps_today": None,
                "headphones_connected": None,
                "dnd_active": None,
                "charging": None,
                "screen_on": None,
                "place_category": None,
                "at": 0.0,    # unix timestamp
            },
            # 사용자가 라벨링한 장소 — 좌표는 *매칭 전용*. 폰 위성이 GET /places
            # 로 받아 자기 GPS와 비교 → category 만 백엔드에 보고. 좌표 자체는
            # 트리거 페이로드에 안 들어옴 (프라이버시 보호).
            # 항목: {label, lat, lng, radius_m}
            "places": [],
            # `/place 라벨` 직후 사용자가 위치 메시지를 5분 안에 보내야 등록된다.
            "pending_place_label": None,    # str 또는 None
            "pending_place_at": 0.0,        # unix timestamp
            # quiet_mode — 사용자가 외출·취침 같은 *조용히 해야 할 상황*을 명시
            # 선언했을 때 모든 자동 발사를 보류. None 이면 정상.
            # kind: "away" | "sleep" 또는 None.
            "quiet_mode": None,             # str 또는 None
            "quiet_mode_started_at": 0.0,
            "quiet_mode_suppressed_count": 0,  # 모드 활성 중 보류된 발사 건수
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

    @staticmethod
    def _match_today_goals(goals: List[dict], key: str) -> List[dict]:
        """주어진 키와 매칭되는 today_goals 항목들을 우선순위 단계로 추려낸다:
        1) 정확 매칭 (name == key) → 단 한 개만 매칭되어도 그것만
        2) 한 이름이 key 의 *접두/접미* 인 경우 (사용자 표현이 풍성한 경우)
        3) 그것도 없으면 *유일한 substring* 매칭만 (다중 substring 이면 모호 → 빈 list)

        sub_step 부분 매칭의 모호함은 LLM 에 다시 묻게 해 의도치 않은 다중 삭제
        를 차단. *시도한 목표만* 정확히 처리되도록."""
        if not key:
            return []
        key = key.strip().lower()
        if not key:
            return []
        # 1) 정확 매칭
        exact = [g for g in goals if g["name"].lower() == key]
        if exact:
            return exact
        # 2) 접두/접미 (key가 길고 name이 짧을 때 — "유튜브 정리하기 끝" → name="유튜브 정리하기")
        prefix_suffix = [
            g for g in goals
            if key.startswith(g["name"].lower()) or key.endswith(g["name"].lower())
        ]
        if len(prefix_suffix) == 1:
            return prefix_suffix
        # 3) 유일한 substring 매칭만 — 다중이면 모호로 보고 빈 list (호출자가 LLM 에 재질의)
        substring = [g for g in goals if key in g["name"].lower() or g["name"].lower() in key]
        if len(substring) == 1:
            return substring
        return []

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
                # 등록 날짜 — 같은 날 등록·취소를 깨끗하게 분모에서 빼기 위함.
                "registered_date": datetime.date.today().isoformat(),
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
        goal 이거나 일치하는 goal 이 없거나 *모호한 다중 매칭*이면 None.
        반환: {next_step, current, total, completed} 또는 None.

        sub_step 단계 한 칸 전진할 때마다 bucket['sub_step_advances'] 도 +1 —
        '오늘 작은 단계 N번 진척' 같은 *세분화된* 진척 통계용 (기존 goals_completed
        는 *목표 단위* 그대로 유지)."""
        with self._lock:
            matches = self._match_today_goals(self._data["today_goals"], goal)
            if len(matches) != 1:
                return None
            target = matches[0]
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
            bucket = self._today_bucket()
            # 단계별 진척 카운터 — 매 advance 마다 +1
            bucket["sub_step_advances"] = bucket.get("sub_step_advances", 0) + 1
            hk = self._hour_key()
            bucket.setdefault("hourly_sub_step_advances", {})
            bucket["hourly_sub_step_advances"][hk] = (
                bucket["hourly_sub_step_advances"].get(hk, 0) + 1
            )
            if completed:
                bucket["goals_completed"] = bucket.get("goals_completed", 0) + 1
                bucket["hourly_goals_completed"][hk] = (
                    bucket["hourly_goals_completed"].get(hk, 0) + 1
                )
                self._data["today_goals"] = [
                    g for g in self._data["today_goals"] if g is not target
                ]
            # 한 발 전진 = 누적 걸음 +1 (마지막 단계로 완료돼도 이 호출은 1걸음).
            self._data["lifetime_steps"] = int(self._data.get("lifetime_steps", 0)) + 1
            self._persist()
            return result

    def complete_today_goal(self, goal: str) -> bool:
        """끝낸 오늘 목표를 목록에서 제거. 매칭 헬퍼로 *유일한* 항목만 처리하며,
        모호한 다중 매칭이면 False (호출자가 LLM 에 재질의). 제거 시 일별 통계의
        goals_completed +1."""
        with self._lock:
            matches = self._match_today_goals(self._data["today_goals"], goal)
            if len(matches) != 1:
                return False
            target = matches[0]
            self._data["today_goals"] = [
                g for g in self._data["today_goals"] if g is not target
            ]
            bucket = self._today_bucket()
            bucket["goals_completed"] = bucket.get("goals_completed", 0) + 1
            hk = self._hour_key()
            bucket["hourly_goals_completed"][hk] = (
                bucket["hourly_goals_completed"].get(hk, 0) + 1
            )
            # 목표 완료 = 누적 걸음 +1 (sub_step 없는 목표를 바로 완료한 경로).
            self._data["lifetime_steps"] = int(self._data.get("lifetime_steps", 0)) + 1
            self._persist()
            return True

    @property
    def lifetime_steps(self) -> int:
        """누적 '걸음' 수 — sub_step 진척·목표 완료·습관 수행의 총합. 절대 줄지 않음."""
        return int(self._get("lifetime_steps") or 0)

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

    # ----------------------------------------------------- 도파민 trail 학습
    TRAIL_LEN = 3  # 트리거 직전 N개 라벨을 1개 trail 키로 묶는다

    def bump_dopamine_trail(self, sequence: List[str]) -> None:
        """딴짓 트리거 직전 라벨 시퀀스에서 길이 TRAIL_LEN prefix·suffix 를 추출해
        카운터에 누적. 마지막 N개가 가장 의미 있음 (트리거 직전 흐름)."""
        seq = [str(s).strip() for s in sequence if str(s).strip()]
        if len(seq) < self.TRAIL_LEN:
            return
        tail = seq[-self.TRAIL_LEN:]
        key = "|".join(tail)
        with self._lock:
            trails = self._data.setdefault("dopamine_trails", {})
            trails[key] = trails.get(key, 0) + 1
            # 비대 방지 — 200개 넘으면 빈도 낮은 절반 정리
            if len(trails) > 200:
                ranked = sorted(trails.items(), key=lambda kv: kv[1])
                for k, _ in ranked[: len(trails) // 2]:
                    trails.pop(k, None)
            self._persist()

    def top_dopamine_trails(self, n: int = 5, min_count: int = 2) -> List[tuple]:
        """가장 자주 등장한 trail. min_count 미만은 노이즈로 보고 제외.
        반환: [(["a","b","c"], count), ...] 내림차순."""
        with self._lock:
            items = [
                (k.split("|"), v)
                for k, v in self._data.get("dopamine_trails", {}).items()
                if v >= min_count
            ]
        items.sort(key=lambda kv: -kv[1])
        return items[:n]

    def reset_weak_spot_candidates(self) -> None:
        """후보 카운터 초기화 — 주간 회고 발송 성공 시 호출 (새 주 시작)."""
        with self._lock:
            self._data["weak_spot_candidates"] = {}
            self._persist()

    # ----------------------------------------------------- 텔레그램 retry 큐
    # 텔레그램 전송 실패 시 잔소리·자동 메시지는 영구 큐에 적재되어 백그라운드
    # 워커가 backoff 로 retry 한다. 최종 도달 시점에 부수효과(쿨다운·통계·자기학습)
    # 가 적용되도록, 부수효과 메타도 큐 항목에 함께 보관한다.

    def add_pending_message(
        self,
        *,
        kind: str,
        chat_id: int,
        text: str,
        side_effects: Optional[dict] = None,
        reply_markup: Optional[dict] = None,
    ) -> str:
        """새 retry 항목을 큐에 적재. 반환: 새 항목 id.
        reply_markup 이 있으면 함께 보관해 retry 도달 시점에도 inline keyboard 유지."""
        import uuid
        with self._lock:
            now = time.time()
            item = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "chat_id": chat_id,
                "text": text,
                "side_effects": side_effects or {},
                "reply_markup": reply_markup,
                "attempts": 0,
                "first_attempt_at": now,
                "next_attempt_at": now,   # 즉시 첫 워커 시도
                "created_at": now,
            }
            self._data["pending_messages"].append(item)
            self._persist()
            return item["id"]

    def list_pending_messages(self) -> List[dict]:
        """워커 폴링용 — 큐 사본을 돌려준다 (외부 mutation 보호)."""
        with self._lock:
            return [dict(it) for it in self._data["pending_messages"]]

    def update_pending_attempt(self, msg_id: str, next_attempt_at: float) -> None:
        """retry 실패 후 다음 시도 시각을 갱신 + attempts 증가."""
        with self._lock:
            for it in self._data["pending_messages"]:
                if it["id"] == msg_id:
                    it["attempts"] = int(it.get("attempts", 0)) + 1
                    it["next_attempt_at"] = float(next_attempt_at)
                    self._persist()
                    return

    def remove_pending_message(self, msg_id: str) -> Optional[dict]:
        """retry 성공·만료 시 큐에서 제거. 제거된 항목 반환."""
        with self._lock:
            queue = self._data["pending_messages"]
            for i, it in enumerate(queue):
                if it["id"] == msg_id:
                    removed = queue.pop(i)
                    self._persist()
                    return removed
        return None

    # --------------------------------------------------------- 장소 라벨
    PLACE_DEFAULT_RADIUS_M = 200
    PENDING_PLACE_TTL_SEC = 300.0   # /place 명령 후 5분 안에 위치 메시지 받아야

    def begin_place_registration(self, label: str) -> None:
        """/place 라벨 → 다음 위치 메시지를 받아 그 좌표로 등록 예약."""
        with self._lock:
            self._data["pending_place_label"] = (label or "").strip() or None
            self._data["pending_place_at"] = time.time()
            self._persist()

    def consume_pending_place_label(self) -> Optional[str]:
        """위치 메시지 도착 시 호출. 펜딩 라벨이 살아있으면 그걸 반환 + 클리어.
        만료(5분)됐으면 None."""
        with self._lock:
            label = self._data.get("pending_place_label")
            at = float(self._data.get("pending_place_at") or 0.0)
            if not label or time.time() - at > self.PENDING_PLACE_TTL_SEC:
                self._data["pending_place_label"] = None
                self._data["pending_place_at"] = 0.0
                return None
            self._data["pending_place_label"] = None
            self._data["pending_place_at"] = 0.0
            self._persist()
            return label

    def add_place(self, label: str, lat: float, lng: float, radius_m: int = 0) -> None:
        """라벨이 같은 기존 항목이 있으면 좌표만 덮어쓰기."""
        label = (label or "").strip()
        if not label:
            return
        if radius_m <= 0:
            radius_m = self.PLACE_DEFAULT_RADIUS_M
        with self._lock:
            places = self._data.setdefault("places", [])
            for p in places:
                if p.get("label") == label:
                    p["lat"] = float(lat)
                    p["lng"] = float(lng)
                    p["radius_m"] = int(radius_m)
                    self._persist()
                    return
            places.append({
                "label": label,
                "lat": float(lat),
                "lng": float(lng),
                "radius_m": int(radius_m),
            })
            self._persist()

    def remove_place(self, label: str) -> bool:
        label = (label or "").strip()
        if not label:
            return False
        with self._lock:
            places = self._data.setdefault("places", [])
            before = len(places)
            self._data["places"] = [p for p in places if p.get("label") != label]
            changed = len(self._data["places"]) != before
            if changed:
                self._persist()
            return changed

    @property
    def places(self) -> List[dict]:
        with self._lock:
            return [dict(p) for p in (self._data.get("places") or [])]

    # --------------------------------------------------------- quiet_mode
    QUIET_MODE_TTL_SEC = 24 * 3600.0     # 24h 안전 fallback — 영영 잠수 방지

    def enter_quiet_mode(self, kind: str) -> bool:
        """외출·취침 같은 *조용 모드* 진입. 이미 같은 종류로 활성이면 False
        (재진입 무의미). 다른 종류면 갱신."""
        kind = (kind or "").strip().lower()
        if kind not in ("away", "sleep"):
            return False
        with self._lock:
            current = self._data.get("quiet_mode")
            if current == kind:
                return False
            self._data["quiet_mode"] = kind
            self._data["quiet_mode_started_at"] = time.time()
            self._data["quiet_mode_suppressed_count"] = 0
            self._persist()
            return True

    def exit_quiet_mode(self) -> Optional[dict]:
        """모드 해제. 활성이었으면 dict({kind, suppressed_count, duration_sec})
        를 반환해 호출자가 안내 메시지 만들 수 있게. 비활성이었으면 None."""
        with self._lock:
            current = self._data.get("quiet_mode")
            if not current:
                return None
            info = {
                "kind": current,
                "suppressed_count": int(self._data.get("quiet_mode_suppressed_count") or 0),
                "duration_sec": time.time() - float(
                    self._data.get("quiet_mode_started_at") or time.time()
                ),
            }
            self._data["quiet_mode"] = None
            self._data["quiet_mode_started_at"] = 0.0
            self._data["quiet_mode_suppressed_count"] = 0
            self._persist()
            return info

    @property
    def quiet_mode(self) -> Optional[str]:
        """현재 활성 모드 — 'away'/'sleep'/None. 24h 초과 시 자동 만료."""
        with self._lock:
            kind = self._data.get("quiet_mode")
            if not kind:
                return None
            started = float(self._data.get("quiet_mode_started_at") or 0.0)
            if time.time() - started > self.QUIET_MODE_TTL_SEC:
                # 만료 — None 으로 정리 (호출자가 별도 안내 X)
                self._data["quiet_mode"] = None
                self._data["quiet_mode_started_at"] = 0.0
                self._data["quiet_mode_suppressed_count"] = 0
                self._persist()
                return None
            return kind

    def bump_quiet_suppressed(self) -> None:
        """모드 활성 중 자동 발사가 보류될 때 호출. 해제 시 안내에 사용."""
        with self._lock:
            n = int(self._data.get("quiet_mode_suppressed_count") or 0)
            self._data["quiet_mode_suppressed_count"] = n + 1
            self._persist()

    # --------------------------------------------------------- 폰 컨텍스트
    def update_phone_context(self, snap: dict) -> None:
        """폰 위성이 트리거 페이로드에 같이 보낸 디바이스 status 를 최신값으로 갱신.
        snap 안에 키가 없으면 기존값 유지 — 부분 갱신 안전."""
        if not isinstance(snap, dict):
            return
        with self._lock:
            ctx = self._data.setdefault("phone_context", {})
            for key in (
                "steps_today", "headphones_connected",
                "dnd_active", "charging", "screen_on",
            ):
                if key in snap:
                    ctx[key] = snap[key]
            ctx["at"] = time.time()
            self._persist()

    @property
    def phone_context(self) -> dict:
        with self._lock:
            return dict(self._data.get("phone_context") or {})

    # --------------------------------------- 대화 답장 (chat reply) 펜딩 박스
    # 잔소리는 *재발사*가 자연스럽지만, 사용자 chat 에 대한 답장은 시간이 지나서
    # 따로 도착하면 어색하다. 그래서 chat 답장은 retry 하지 않고 한 개만 보관 →
    # 사용자가 다음 메시지를 보낼 때 LLM 이 이전 답장 못 전달한 사실을 알고
    # 자연스럽게 합쳐서 답하도록 _state_summary 에 노출한다.

    def set_pending_chat_reply(self, text: str) -> None:
        with self._lock:
            self._data["pending_chat_reply"] = {
                "text": text,
                "at": time.time(),
            }
            self._persist()

    def get_pending_chat_reply(self) -> Optional[dict]:
        with self._lock:
            data = self._data.get("pending_chat_reply")
            return dict(data) if data else None

    def clear_pending_chat_reply(self) -> None:
        with self._lock:
            if self._data.get("pending_chat_reply") is not None:
                self._data["pending_chat_reply"] = None
                self._persist()

    # --------------------------------------------------------- 일별 통계
    DAILY_STATS_MAX_DAYS = 60     # 너무 오래된 날짜는 자동 정리 (메모리·저장 비대 방지)

    def _today_bucket(self) -> dict:
        """오늘 날짜의 통계 버킷을 (RLock 잡힌 상태에서) 가져온다 — 없으면 생성.
        hourly_* 필드는 시간대별 생산성 매핑용 (0~23 시 키)."""
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
                # 시간대별 (hour 0~23) 카운터 — 기존 일별 버킷에는 영향 없음
                "hourly_triggers": {},        # {"14": N, ...}
                "hourly_goals_completed": {},
                "hourly_habit_dones": {},
            }
            stats[today] = bucket
            # 오래된 날짜 자동 정리
            if len(stats) > self.DAILY_STATS_MAX_DAYS:
                for k in sorted(stats.keys())[: len(stats) - self.DAILY_STATS_MAX_DAYS]:
                    stats.pop(k, None)
        # 기존 버킷에 hourly 필드 누락 시 보강 (마이그레이션)
        bucket.setdefault("hourly_triggers", {})
        bucket.setdefault("hourly_goals_completed", {})
        bucket.setdefault("hourly_habit_dones", {})
        return bucket

    @staticmethod
    def _hour_key() -> str:
        return str(datetime.datetime.now().hour)

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
        건 카운트하지 않는다 — 사용자 체감 패턴만 잡기 위함. 시간대 카운터도 같이."""
        label = (trigger_value or "").strip()
        if not label:
            return
        with self._lock:
            bucket = self._today_bucket()
            bucket["triggers"][label] = bucket["triggers"].get(label, 0) + 1
            hk = self._hour_key()
            bucket["hourly_triggers"][hk] = bucket["hourly_triggers"].get(hk, 0) + 1
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

    def hourly_breakdown(self, days: int = 14) -> dict:
        """최근 N일 활동을 시간(hour) 별로 집계. 사용자 골든타임(완료가 가장 많은
        시간대)과 위험 시간대(트리거가 가장 많이 잡힌 시간대) 도출에 사용.

        반환 형태:
            {
              "window_days": N,
              "total_days_with_data": ...,
              "hours": {
                "0": {"triggers": N, "goals_completed": N, "habit_dones": N},
                ...
              },
              "golden_hours": [(hour, completed_count), ...],   # 상위 3개
              "risk_hours":   [(hour, trigger_count),   ...],   # 상위 3개
            }
        """
        today = datetime.date.today()
        target_dates = [
            (today - datetime.timedelta(days=i)).isoformat() for i in range(days)
        ]
        hours: Dict[int, Dict[str, int]] = {
            h: {"triggers": 0, "goals_completed": 0, "habit_dones": 0}
            for h in range(24)
        }
        days_with_data = 0
        with self._lock:
            stats = self._data["daily_stats"]
            for d in target_dates:
                b = stats.get(d)
                if not b:
                    continue
                ht = b.get("hourly_triggers") or {}
                hg = b.get("hourly_goals_completed") or {}
                hh = b.get("hourly_habit_dones") or {}
                if ht or hg or hh:
                    days_with_data += 1
                for hk, v in ht.items():
                    try:
                        hours[int(hk)]["triggers"] += int(v or 0)
                    except Exception:
                        pass
                for hk, v in hg.items():
                    try:
                        hours[int(hk)]["goals_completed"] += int(v or 0)
                    except Exception:
                        pass
                for hk, v in hh.items():
                    try:
                        hours[int(hk)]["habit_dones"] += int(v or 0)
                    except Exception:
                        pass
        # 정렬: 골든타임 = goals_completed + habit_dones 합산 상위 / 위험 = triggers 상위
        scored_gold = sorted(
            ((h, hours[h]["goals_completed"] + hours[h]["habit_dones"]) for h in hours),
            key=lambda kv: -kv[1],
        )
        scored_risk = sorted(
            ((h, hours[h]["triggers"]) for h in hours),
            key=lambda kv: -kv[1],
        )
        golden = [(h, c) for h, c in scored_gold if c > 0][:3]
        risk = [(h, c) for h, c in scored_risk if c > 0][:3]
        return {
            "window_days": days,
            "total_days_with_data": days_with_data,
            "hours": {str(h): hours[h] for h in hours},
            "golden_hours": golden,
            "risk_hours": risk,
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

    # ----------------------------------------------------- 하루 마무리 일지 1일 1회
    @property
    def last_daily_journal(self) -> Optional[str]:
        return self._get("last_daily_journal")

    @last_daily_journal.setter
    def last_daily_journal(self, value: Optional[str]) -> None:
        self._set("last_daily_journal", value)

    # ----------------------------------------------------- 마지막 사용자 메시지 시각
    @property
    def last_user_message_at(self) -> float:
        """마지막으로 사용자가 메시지를 보낸 epoch 시각. 0 이면 미설정.
        밤잠 추론(긴 침묵 후 첫 메시지 = 자다 깸)에 쓰며, 4시 재시작에도
        살아남도록 영속화한다."""
        return float(self._get("last_user_message_at") or 0.0)

    @last_user_message_at.setter
    def last_user_message_at(self, value: float) -> None:
        self._set("last_user_message_at", float(value))

    # ----------------------------------------------------- 위험 예측 1일 1회
    def risk_predict_already_fired_today(self) -> bool:
        """오늘 위험 예측 알림이 이미 발사되었는지. 부담 완화 위해 1일 1회."""
        rec = self._get("last_risk_predict") or {}
        return rec.get("date") == datetime.date.today().isoformat()

    def mark_risk_predict_fired(self, hour: int) -> None:
        self._set("last_risk_predict", {
            "date": datetime.date.today().isoformat(),
            "hour": int(hour),
        })

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
        """사용자가 명시 발화로 설정한 *base* 톤. 컨디션 토로로 인한 일시 톤다운은
        `active_nag_policy` 가 별도로 합쳐 돌려준다."""
        val = self._get("nag_policy")
        return val if val in ("gentle", "balanced", "strict") else "balanced"

    @nag_policy.setter
    def nag_policy(self, value: str) -> None:
        if value in ("gentle", "balanced", "strict"):
            self._set("nag_policy", value)

    @property
    def active_nag_policy(self) -> str:
        """현재 활성 톤. temp 가 살아있으면 temp, 만료됐거나 없으면 base.
        만료 순간을 잡아 `nag_policy_recovery_pending` 플래그를 세워둔다 — 다음
        잔소리 발사 시 자연스러운 복귀 코멘트를 끼울 수 있게."""
        with self._lock:
            temp = self._data.get("nag_policy_temp") or ""
            until = float(self._data.get("nag_policy_temp_until") or 0.0)
            if temp and time.time() < until:
                return temp if temp in ("gentle", "balanced", "strict") else self.nag_policy
            # temp 가 있었는데 만료됐으면 — 정리 + 복귀 플래그 세움.
            if temp:
                self._data["nag_policy_temp"] = ""
                self._data["nag_policy_temp_until"] = 0.0
                self._data["nag_policy_recovery_pending"] = True
                self._persist()
            return self.nag_policy

    def apply_temporary_policy(self, policy: str, duration_sec: float = 86400.0) -> None:
        """컨디션 신호로 일시 톤다운을 적용. base 는 안 건드림."""
        if policy not in ("gentle", "balanced", "strict"):
            return
        with self._lock:
            self._data["nag_policy_temp"] = policy
            self._data["nag_policy_temp_until"] = time.time() + duration_sec
            # 활성 톤이 다시 바뀌었으니 이전에 세워진 복귀 플래그는 무효.
            self._data["nag_policy_recovery_pending"] = False
            self._persist()

    def consume_policy_recovery_note(self) -> bool:
        """복귀가 막 일어났으면 True 한 번. 잔소리 답장에 자연스러운 코멘트
        끼우는 용도로 한 번 소비하면 자동 False 로 내려간다."""
        with self._lock:
            if self._data.get("nag_policy_recovery_pending"):
                self._data["nag_policy_recovery_pending"] = False
                self._persist()
                return True
            return False

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
        """알람 하나를 예약 목록에 추가한다. 같은 (text, repeat, label) 알람이
        이미 있으면 *중복 추가 대신 교체* — 같은 알람을 두 번 등록해도 1개만
        남아 같은 시각에 두 번 울리는 걸 막는다 (멱등). 기존에 중복이 쌓여 있어도
        한 번 더 등록하면 하나로 합쳐진다."""
        text = alarm.get("text")
        repeat = alarm.get("repeat")
        label = alarm.get("label")
        with self._lock:
            self._data["alarms"] = [
                a for a in self._data["alarms"]
                if not (
                    a.get("text") == text
                    and a.get("repeat") == repeat
                    and a.get("label") == label
                )
            ]
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
                hk = self._hour_key()
                bucket["hourly_habit_dones"][hk] = (
                    bucket["hourly_habit_dones"].get(hk, 0) + 1
                )
                # 습관 수행 = 누적 걸음 +1 (하루 1회 분기 안 → 같은 날 중복 증가 X).
                self._data["lifetime_steps"] = int(self._data.get("lifetime_steps", 0)) + 1
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
        """오늘 목표를 *취소* (완료가 아닌 단순 삭제). 매칭 헬퍼로 유일한 항목만.
        등록일이 오늘이면 *분모(goals_registered)*도 같이 차감 — 시도조차 안 한
        목표가 완료율을 낮춰 캐파 가이드를 왜곡시키지 않게."""
        with self._lock:
            matches = self._match_today_goals(self._data["today_goals"], name)
            if len(matches) != 1:
                return False
            target = matches[0]
            self._data["today_goals"] = [
                g for g in self._data["today_goals"] if g is not target
            ]
            # 오늘 등록·오늘 취소면 분모도 -1. 어제 등록된 거 오늘 취소는 분모
            # 유지 (어제 자 통계는 그대로). registered_date 마이그레이션 안 된
            # 옛 항목은 *오늘 자*로 간주해 차감 (안전한 쪽으로).
            today_s = datetime.date.today().isoformat()
            reg_date = target.get("registered_date") or today_s
            if reg_date == today_s:
                bucket = self._today_bucket()
                current = int(bucket.get("goals_registered", 0) or 0)
                if current > 0:
                    bucket["goals_registered"] = current - 1
            self._persist()
            return True

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

    def hard_reset(self) -> None:
        """`/reset all` — 통계·설정·자기학습까지 *전부* 비운다. chat_id 는 유지
        (봇 등록 자체는 살려두는 게 자연스러움). 위험한 작업이라 명시 옵션 필요."""
        with self._lock:
            chat_id = self._data.get("chat_id")
            # 새 Store 인스턴스 만들 때와 같은 초기 상태로 복귀
            self._data["chat_id"] = chat_id
            self._data["today_goals"] = []
            self._data["long_term_goal"] = None
            self._data["progress"] = None
            self._data["next_step"] = None
            self._data["profile"] = {}
            self._data["weak_spots"] = []
            self._data["alarms"] = []
            self._data["events"] = []
            self._data["habits"] = []
            self._data["history"] = []
            self._data["nag_policy"] = "balanced"
            self._data["nag_policy_asked"] = False
            self._data["nag_policy_temp"] = ""
            self._data["nag_policy_temp_until"] = 0.0
            self._data["nag_policy_recovery_pending"] = False
            self._data["daily_stats"] = {}
            self._data["last_weekly_review"] = None
            self._data["last_overload_checkin"] = None
            self._data["last_late_night_fired"] = None
            self._data["implementation_intentions"] = []
            self._data["weak_spot_candidates"] = {}
            self._data["pending_messages"] = []
            self._data["pending_chat_reply"] = None
            self._persist()
