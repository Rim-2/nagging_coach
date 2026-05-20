"""
agent_tools.py — 에이전트가 호출하는 '행동 도구' 레지스트리.

각 Tool 은 (1) 모델에게 보여줄 FunctionDeclaration 과 (2) 실제 실행 함수를
가진다. 새 능력을 추가하려면 여기에 Tool 하나만 더 만들면 된다 — 에이전트
루프(ai_engine.CoachAgent._agent_loop)가 언제 그 도구를 쓸지 알아서 판단한다.
기능을 코드에 하드코딩하지 않는 게 핵심.
"""

import datetime
from typing import Callable, Dict

from google.genai import types


class Tool:
    """도구 하나 — 모델용 선언 + 실제 실행 함수."""

    def __init__(
        self,
        declaration: types.FunctionDeclaration,
        run: Callable[[dict], str],
    ) -> None:
        self.declaration = declaration
        self.name = declaration.name
        self.run = run  # (args dict) -> 결과를 요약한 문자열


def build_tools(calendar, store) -> Dict[str, Tool]:
    """현재 사용 가능한 도구 목록을 만든다.
    calendar 가 None 이면 캘린더 도구는 빠진다 (그 능력만 비활성)."""
    tools: Dict[str, Tool] = {}

    if calendar is not None:
        tools["add_calendar_event"] = Tool(
            types.FunctionDeclaration(
                name="add_calendar_event",
                description=(
                    "Google 캘린더에 '일정·약속·회의'를 추가한다. 사용자가 "
                    "특정 시각에 무슨 일정이 있다고 할 때 쓴다 (그 시각에 알림을 "
                    "보내달라는 게 아니라, 일정 자체를 캘린더에 남기는 것)."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "summary": types.Schema(
                            type="STRING", description="일정 제목"
                        ),
                        "datetime": types.Schema(
                            type="STRING",
                            description=(
                                "시작 시각 'YYYY-MM-DDTHH:MM'. 메시지 앞의 "
                                "[지금:] 표시로 상대 표현을 환산해 넣어라."
                            ),
                        ),
                        "end_datetime": types.Schema(
                            type="STRING",
                            description="(선택) 종료 시각 'YYYY-MM-DDTHH:MM'.",
                        ),
                    },
                    required=["summary", "datetime"],
                ),
            ),
            run=lambda args: _add_calendar_event(calendar, args),
        )
        tools["list_calendar_events"] = Tool(
            types.FunctionDeclaration(
                name="list_calendar_events",
                description="사용자의 다가오는 캘린더 일정 목록을 조회한다.",
                parameters=types.Schema(type="OBJECT", properties={}),
            ),
            run=lambda args: _list_calendar_events(calendar),
        )

    # ---- 알람: 특정 시각/매일 사용자에게 잔소리를 보내달라는 예약 ----
    tools["set_alarm"] = Tool(
        types.FunctionDeclaration(
            name="set_alarm",
            description=(
                "특정 시각이나 매일 정해진 시각에 사용자에게 '알림·잔소리를 "
                "보내달라'는 요청을 예약한다. 예: '1시간 뒤 물 마시라고 해줘', "
                "'매일 아침 9시에 오늘 계획 물어봐줘'. 캘린더 일정과 다르다 — "
                "이건 그 시각에 코치가 먼저 말 거는 알람이다."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "message": types.Schema(
                        type="STRING",
                        description="그 시각에 사용자에게 상기시킬 내용",
                    ),
                    "when": types.Schema(
                        type="STRING",
                        description=(
                            "repeat=once 면 'YYYY-MM-DDTHH:MM' 절대 시각 "
                            "(메시지 앞 [지금:] 으로 환산). repeat=daily 면 "
                            "'HH:MM' 24시간 형식."
                        ),
                    ),
                    "repeat": types.Schema(
                        type="STRING",
                        description="'once'(한 번) 또는 'daily'(매일 반복)",
                    ),
                },
                required=["message", "when", "repeat"],
            ),
        ),
        run=lambda args: _set_alarm(store, args),
    )
    tools["list_alarms"] = Tool(
        types.FunctionDeclaration(
            name="list_alarms",
            description="현재 예약된 알람 목록을 조회한다.",
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        run=lambda args: _list_alarms(store),
    )
    tools["cancel_alarm"] = Tool(
        types.FunctionDeclaration(
            name="cancel_alarm",
            description="예약된 알람을 취소한다. 알람 내용 일부로 지목한다.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "description": types.Schema(
                        type="STRING",
                        description="취소할 알람을 알아볼 내용 키워드",
                    ),
                },
                required=["description"],
            ),
        ),
        run=lambda args: _cancel_alarm(store, args),
    )

    # ---- 습관 등록: 난이도 레벨 단계로 쪼개서 ----
    tools["register_habit"] = Tool(
        types.FunctionDeclaration(
            name="register_habit",
            description=(
                "사용자가 꾸준히 들이려는 '습관'을 등록한다. 습관을 '아주 작은 "
                "시작'부터 '이만하면 습관 됐다' 싶은 정착 수준까지 3~4단계 "
                "레벨로 쪼개서 등록한다. 등록 후 set_alarm 으로 정기 알람도 "
                "따로 깔아주면 좋다."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "name": types.Schema(
                        type="STRING",
                        description="습관 이름 (예: 독서, 아침 운동)",
                    ),
                    "levels": types.Schema(
                        type="ARRAY",
                        items=types.Schema(type="STRING"),
                        description=(
                            "작은 시작 → 정착 수준까지 점진적 목표 3~4개. 예: "
                            "['하루 10분 독서','하루 20분 독서','하루 30분 독서']. "
                            "마지막이 상식적인 상한선 (무한정 키우지 말 것)."
                        ),
                    ),
                },
                required=["name", "levels"],
            ),
        ),
        run=lambda args: _register_habit(store, args),
    )

    return tools


# ---------------------------------------------------------- 캘린더 실행
def _add_calendar_event(calendar, args: dict) -> str:
    summary = str(args.get("summary", "")).strip()
    dt_str = str(args.get("datetime", "")).strip()
    if not summary or not dt_str:
        return "실패: summary 와 datetime 이 모두 필요하다."
    try:
        start = datetime.datetime.fromisoformat(dt_str)
    except ValueError:
        return f"실패: datetime 형식이 잘못됨 ({dt_str!r})."
    end = None
    end_str = str(args.get("end_datetime", "")).strip()
    if end_str:
        try:
            end = datetime.datetime.fromisoformat(end_str)
        except ValueError:
            end = None
    try:
        calendar.add_event(summary, start, end)
    except Exception as exc:
        return f"실패: 캘린더 추가 중 오류 — {exc}"
    return f"성공: '{summary}' 일정을 {dt_str} 에 추가했다."


def _list_calendar_events(calendar) -> str:
    try:
        events = calendar.list_upcoming(max_results=10)
    except Exception as exc:
        return f"실패: 캘린더 조회 중 오류 — {exc}"
    if not events:
        return "다가오는 일정이 없다."
    return "다가오는 일정 — " + "; ".join(
        f"{e['start']} {e['summary']}" for e in events
    )


# ------------------------------------------------------------ 알람 실행
def _next_daily_ts(hour: int, minute: int) -> float:
    """오늘/내일 중 가장 가까운 HH:MM 의 epoch 시각."""
    now = datetime.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return target.timestamp()


def _set_alarm(store, args: dict) -> str:
    message = str(args.get("message", "")).strip()
    when = str(args.get("when", "")).strip()
    repeat = str(args.get("repeat", "once")).strip().lower()
    if not message or not when:
        return "실패: message 와 when 이 모두 필요하다."

    if repeat == "daily":
        time_part = when.split("T")[-1] if "T" in when else when
        try:
            hh, mm = (int(x) for x in time_part.split(":")[:2])
            assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:
            return f"실패: 매일 알람 시각 형식 오류 ({when!r}), 'HH:MM' 필요."
        next_ts = _next_daily_ts(hh, mm)
        label = f"매일 {hh:02d}:{mm:02d}"
    else:
        repeat = "once"
        try:
            dt = datetime.datetime.fromisoformat(when)
        except ValueError:
            return f"실패: 일회성 알람 시각 형식 오류 ({when!r})."
        next_ts = dt.timestamp()
        label = dt.strftime("%Y-%m-%d %H:%M")

    store.add_alarm(
        {"text": message, "repeat": repeat, "next_ts": next_ts, "label": label}
    )
    return f"성공: '{message}' 알람을 [{label}] 로 예약했다."


def _list_alarms(store) -> str:
    alarms = store.alarms
    if not alarms:
        return "예약된 알람이 없다."
    parts = []
    for a in alarms:
        kind = "매일" if a.get("repeat") == "daily" else "한 번"
        parts.append(f"({kind}) {a.get('label')} — {a.get('text')}")
    return "예약된 알람: " + "; ".join(parts)


def _cancel_alarm(store, args: dict) -> str:
    desc = str(args.get("description", "")).strip().lower()
    if not desc:
        return "실패: 취소할 알람을 알려줄 description 이 필요하다."
    alarms = store.alarms
    kept = [a for a in alarms if desc not in str(a.get("text", "")).lower()]
    removed = len(alarms) - len(kept)
    if removed == 0:
        return f"실패: '{desc}' 에 맞는 알람을 못 찾았다."
    store.replace_alarms(kept)
    return f"성공: 알람 {removed}개 취소했다."


# ------------------------------------------------------------ 습관 실행
def _register_habit(store, args: dict) -> str:
    name = str(args.get("name", "")).strip()
    levels = args.get("levels")
    if not name:
        return "실패: 습관 name 이 필요하다."
    if not isinstance(levels, (list, tuple)) or not levels:
        return "실패: levels (난이도 단계 리스트) 가 필요하다."
    clean = [str(lv).strip() for lv in levels if str(lv).strip()]
    if not clean:
        return "실패: levels 가 비어 있다."
    if store.add_habit(name, clean):
        return f"성공: 습관 '{name}' 등록 ({len(clean)}단계, 시작 목표: {clean[0]})."
    return f"이미 등록된 습관이다: '{name}'."
