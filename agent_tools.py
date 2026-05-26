"""
agent_tools.py — 에이전트가 호출하는 '행동 도구' 레지스트리.

각 Tool 은 (1) 모델에게 보여줄 FunctionDeclaration 과 (2) 실제 실행 함수를
가진다. 새 능력을 추가하려면 여기에 Tool 하나만 더 만들면 된다 — 에이전트
루프(ai_engine.CoachAgent._agent_loop)가 언제 그 도구를 쓸지 알아서 판단한다.
기능을 코드에 하드코딩하지 않는 게 핵심.
"""

import datetime
from typing import Callable, Dict, Optional

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


def build_tools(
    calendar, store, on_goal_set: Optional[Callable[[str], None]] = None
) -> Dict[str, Tool]:
    """현재 사용 가능한 도구 목록을 만든다.
    calendar 가 None 이면 캘린더 도구는 빠진다 (그 능력만 비활성).
    on_goal_set 은 register_today_goal_with_steps 가 새 과제를 저장한 직후
    PC 감시 wake-up 등 후처리를 트리거하기 위한 콜백."""
    tools: Dict[str, Tool] = {}
    notify_goal_set = on_goal_set or (lambda _g: None)

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

    # ---- 단발 과제 분해: 큰 과제 → 작은 sub_step 트리로 등록 ----
    tools["register_today_goal_with_steps"] = Tool(
        types.FunctionDeclaration(
            name="register_today_goal_with_steps",
            description=(
                "오늘 할 단발 과제 하나를 '아주 작은 행동 3~5개' 로 미리 쪼개 "
                "등록한다. 사용자가 '코딩 1시간', '논문 쓰기', '방 정리' 같이 "
                "큰 과제를 던지면, 한 단계 더 캐물어 작업 내용을 알아낸 뒤 이 "
                "도구로 sub_steps 를 정해 저장해라. 각 step 은 5~15분이면 끝낼 "
                "만큼 작게. 사용자가 한 step 끝냈다고 하면 advance_today_goal_step "
                "으로 진척시킨다. (작은 단발 과제는 그냥 자연스러운 대화로 끝나니 "
                "이 도구 안 써도 된다.)"
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "name": types.Schema(
                        type="STRING",
                        description="과제 이름 (예: '로그인 버그 고치기')",
                    ),
                    "sub_steps": types.Schema(
                        type="ARRAY",
                        items=types.Schema(type="STRING"),
                        description=(
                            "작은 행동 3~5개. 첫 step 은 가장 작은 '시동 거는 "
                            "행동'. 예: ['에러 메시지 print 추가','실행해서 출력 "
                            "확인','원인 가설 메모','수정','테스트'] "
                        ),
                    ),
                },
                required=["name", "sub_steps"],
            ),
        ),
        run=lambda args: _register_today_goal_with_steps(
            store, args, notify_goal_set
        ),
    )
    tools["advance_today_goal_step"] = Tool(
        types.FunctionDeclaration(
            name="advance_today_goal_step",
            description=(
                "register_today_goal_with_steps 로 등록된 과제의 sub_step 하나가 "
                "끝났다고 표시한다. 사용자가 'X 했어' 처럼 한 단계만 끝났다고 "
                "보고할 때 호출. 마지막 step 이면 과제 자체가 자동 완료된다."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "name": types.Schema(
                        type="STRING",
                        description="진척시킬 과제 이름 (부분 일치 가능)",
                    ),
                },
                required=["name"],
            ),
        ),
        run=lambda args: _advance_today_goal_step(store, args),
    )

    # ---- WOOP / Implementation intention: 약점에 대한 if-then plan ----
    tools["register_implementation_intention"] = Tool(
        types.FunctionDeclaration(
            name="register_implementation_intention",
            description=(
                "WOOP/MCII 의 Plan 단계 — 사용자가 '만약 X 상황이 오면 그땐 Y "
                "한다' 식 if-then 문장으로 자기 약점·습관 대응을 정리했을 때 "
                "저장한다. Implementation intention 은 임상 연구에서 미루기·"
                "습관 형성 효과 검증된 기법 (메타분석 g=0.34, 24 trials). "
                "저장된 plan 들은 [상태 메모]에 노출되어 다음 대화·잔소리에서 "
                "코치가 참고한다. WOOP 4단계 모두 끌어낸 뒤 Plan 이 충분히 "
                "구체적일 때만 호출 — 모호한 다짐 X."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "situation": types.Schema(
                        type="STRING",
                        description=(
                            "'언제/어떤 상황이 오면' — 구체적인 cue 여야 한다. "
                            "예: '저녁 9시 이후 유튜브가 켜지면', '회의 끝나고 "
                            "카톡을 보면', '코드가 막혀서 짜증날 때'."
                        ),
                    ),
                    "response": types.Schema(
                        type="STRING",
                        description=(
                            "'그때 어떻게 한다' — 구체 행동. 예: '핸드폰을 책상 "
                            "서랍에 넣는다', '5분 산책 후 책상 앞에 다시 앉는다', "
                            "'문제를 종이에 다시 적어본다'."
                        ),
                    ),
                    "related_goal": types.Schema(
                        type="STRING",
                        description="(선택) 이 plan 이 어떤 목표·약점과 연결되는지",
                    ),
                },
                required=["situation", "response"],
            ),
        ),
        run=lambda args: _register_implementation_intention(store, args),
    )

    # ---- Focus session: Pomodoro 식 짧은 집중 타이머 (시작 장벽 ↓) ----
    tools["start_focus_session"] = Tool(
        types.FunctionDeclaration(
            name="start_focus_session",
            description=(
                "사용자가 작업 시작을 어려워하거나 미루고 있을 때 짧은 집중 "
                "타이머를 걸어준다 ('딱 N분만 해보자' 식). 끝나면 시스템이 "
                "자동으로 사용자한테 '어땠어?' 알람을 띄운다. Pomodoro 식 "
                "기법으로 시작 장벽을 낮춤. nag_policy 가 gentle 이면 10~15분, "
                "balanced 20~25분, strict 25분 권장. 사용자가 더 짧게 원하면 "
                "5분도 OK."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "minutes": types.Schema(
                        type="INTEGER",
                        description="타이머 분량 (5~50 범위, Pomodoro 식 권장 25)",
                    ),
                    "what": types.Schema(
                        type="STRING",
                        description="이 세션에서 할 작업 (예: '로그인 함수 print 추가')",
                    ),
                },
                required=["minutes", "what"],
            ),
        ),
        run=lambda args: _start_focus_session(store, args),
    )

    # ---- Mood log: 가벼운 활동-기분 연결 추적 (behavioral activation 핵심) ----
    tools["log_mood"] = Tool(
        types.FunctionDeclaration(
            name="log_mood",
            description=(
                "사용자가 자기 기분·컨디션을 말하면 (좋다·우울하다·지쳤다·신난다 "
                "등) 가벼운 mood log 로 저장한다. 사용자한테 명시적으로 '1~5점' "
                "을 묻지 말고, 발화에서 자연스럽게 짐작해서 rating 을 정해라 "
                "(1=아주 나쁨, 3=보통, 5=아주 좋음). note 에는 사용자가 그 "
                "기분과 함께 언급한 활동/상황 한 줄 (예: '논문 끝내고 산책함', "
                "'밤새 일하느라 지침'). 매 turn 호출하지 말고, 사용자가 직접 "
                "기분 얘기 꺼냈을 때만."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "rating": types.Schema(
                        type="INTEGER",
                        description="1(아주 나쁨)~5(아주 좋음) 추정값",
                    ),
                    "note": types.Schema(
                        type="STRING",
                        description="(선택) 기분 맥락 — 함께 언급된 활동/상황",
                    ),
                },
                required=["rating"],
            ),
        ),
        run=lambda args: _log_mood(store, args),
    )

    # ---- 주간 인사이트: 격려·칭찬에 구체적 근거 제공 ----
    tools["get_weekly_insight"] = Tool(
        types.FunctionDeclaration(
            name="get_weekly_insight",
            description=(
                "지난 7일 vs 그 전 7일 활동을 비교한 인사이트를 한 줄로 받아본다 "
                "(목표 완료·습관 수행·잔소리 트리거 발생 횟수). 사용자가 '요즘 "
                "어땠어?', '이번 주 잘하고 있어?' 처럼 자기 추세를 물을 때, "
                "또는 코치가 칭찬·격려를 막연한 말 대신 실제 숫자로 뒷받침하고 "
                "싶을 때 쓴다."
            ),
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        run=lambda args: _get_weekly_insight(store),
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


# ---------------------------------------------------------- 단발 과제 분해
def _register_today_goal_with_steps(
    store, args: dict, notify_goal_set: Callable[[str], None]
) -> str:
    name = str(args.get("name", "")).strip()
    steps = args.get("sub_steps")
    if not name:
        return "실패: name 이 필요하다."
    if not isinstance(steps, (list, tuple)) or not steps:
        return "실패: sub_steps (작은 행동 리스트) 가 필요하다."
    clean = [str(s).strip() for s in steps if str(s).strip()]
    if not clean:
        return "실패: sub_steps 가 비어 있다."
    if store.add_today_goal(name, sub_steps=clean):
        # PC 감시 wake-up 등 후처리 (CoachApp._on_goal_set).
        try:
            notify_goal_set(name)
        except Exception as exc:
            print(f"[agent_tools] on_goal_set 콜백 오류 (무시): {exc}")
        return (
            f"성공: '{name}' 을 {len(clean)}단계로 등록. "
            f"첫 행동: '{clean[0]}'."
        )
    return f"이미 등록된 오늘 목표다: '{name}'."


def _advance_today_goal_step(store, args: dict) -> str:
    name = str(args.get("name", "")).strip()
    if not name:
        return "실패: name 이 필요하다."
    result = store.advance_today_goal(name)
    if result is None:
        return f"실패: '{name}' 에 매칭되는 sub_step 등록 과제가 없다."
    if result["completed"]:
        return (
            f"성공: '{name}' 의 마지막 단계까지 끝났어 — 과제 자체가 완료됐다. "
            f"({result['current']}/{result['total']})"
        )
    return (
        f"성공: '{name}' 진척 {result['current']}/{result['total']}. "
        f"다음 행동: '{result['next_step']}'."
    )


# ---------------------------------------------------- Implementation intention
def _register_implementation_intention(store, args: dict) -> str:
    situation = str(args.get("situation", "")).strip()
    response = str(args.get("response", "")).strip()
    related = str(args.get("related_goal", "")).strip() or None
    if not situation:
        return "실패: situation 이 필요하다."
    if not response:
        return "실패: response 가 필요하다."
    if store.add_implementation_intention(situation, response, related):
        tail = f" (목표: {related})" if related else ""
        return (
            f"성공: if-then plan 저장 — '{situation}' → '{response}'{tail}. "
            f"다음에 그 상황 오면 같이 챙길게."
        )
    return f"비슷한 상황의 plan 이 이미 등록되어 있어 — '{situation}'."


# ---------------------------------------------------------- Focus session
FOCUS_END_MARKER = "[FOCUS_END]"  # alarm text 가 이걸로 시작하면 _fire_alarm 분기


def _start_focus_session(store, args: dict) -> str:
    try:
        minutes = int(args.get("minutes", 0))
    except Exception:
        return "실패: minutes 는 정수가 필요하다 (5~50 권장)."
    if not (1 <= minutes <= 120):
        return "실패: minutes 는 1~120 범위만 허용."
    what = str(args.get("what", "")).strip() or "집중 작업"
    next_ts = datetime.datetime.now().timestamp() + minutes * 60
    label = f"{minutes}분 집중 세션"
    # 알람 text 에 마커를 박아 _fire_alarm 이 specialized 메시지를 만든다.
    store.add_alarm({
        "text": f"{FOCUS_END_MARKER} {what}",
        "repeat": "once",
        "next_ts": next_ts,
        "label": label,
    })
    return (
        f"성공: '{what}' {minutes}분 타이머 시작 — 끝나면 시스템이 다시 "
        f"'어땠어?' 물어볼게."
    )


# ------------------------------------------------------------ Mood log
def _log_mood(store, args: dict) -> str:
    try:
        rating = int(args.get("rating", 0))
    except Exception:
        return "실패: rating 은 1~5 정수가 필요하다."
    if not (1 <= rating <= 5):
        return "실패: rating 은 1~5 범위만."
    note = str(args.get("note", "")).strip()
    store.add_mood_log(rating, note)
    return f"성공: mood {rating}/5 기록" + (f" (메모: {note[:60]})" if note else "") + "."


# ---------------------------------------------------------- 주간 인사이트
def _get_weekly_insight(store) -> str:
    """지난 7일 vs 그 전 7일 누적 비교를 한 줄 자연어로. 코치가 칭찬·격려에
    구체적 숫자를 녹이고, 새 과제 분량 결정에 캐파 가이드를 받기 위함."""
    s = store.weekly_summary(days=7)
    rec, prev = s["recent"], s["previous"]
    if (
        rec["goals_completed"] == 0
        and rec["habit_dones"] == 0
        and rec["trigger_total"] == 0
        and rec.get("goals_registered", 0) == 0
    ):
        return "최근 7일 활동 데이터가 아직 충분하지 않다."

    def _delta(now: int, before: int) -> str:
        d = now - before
        if d > 0:
            return f"+{d}"
        if d < 0:
            return str(d)
        return "±0"

    rate = rec.get("completion_rate")
    if rate is None:
        rate_line = "목표 완료율: 데이터 부족"
    else:
        rate_line = (
            f"목표 완료율: {int(rate * 100)}% "
            f"({rec['goals_completed']}/{rec['goals_registered']})"
            f"{ ' — 캐파에 비해 분량이 과한 신호, 다음 과제는 더 잘게 쪼개라' if rate < 0.4 else '' }"
        )

    mood_line = ""
    if rec.get("mood_count", 0) > 0:
        avg = rec["mood_avg"]
        prev_avg = prev.get("mood_avg")
        if prev_avg is not None:
            diff = avg - prev_avg
            sign = "+" if diff > 0 else ""
            mood_line = (
                f" Mood 평균 {avg:.1f}/5 (지난주 {prev_avg:.1f}, {sign}{diff:.1f}, "
                f"{rec['mood_count']}회 기록)."
            )
        else:
            mood_line = f" Mood 평균 {avg:.1f}/5 ({rec['mood_count']}회 기록)."
        notes = rec.get("mood_notes_recent") or []
        if notes:
            mood_line += " 최근 메모: " + "; ".join(f'"{n}"' for n in notes) + "."

    return (
        f"최근 7일 — {rate_line}; "
        f"목표 완료 {rec['goals_completed']}회 "
        f"({_delta(rec['goals_completed'], prev['goals_completed'])}), "
        f"습관 수행 {rec['habit_dones']}회 "
        f"({_delta(rec['habit_dones'], prev['habit_dones'])}), "
        f"잔소리 트리거 {rec['trigger_total']}회 "
        f"({_delta(rec['trigger_total'], prev['trigger_total'])}, 적을수록 좋음). "
        f"가장 자주 잡힌 패턴: '{rec['top_trigger'] or '-'}'."
        f"{mood_line}"
    )


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
