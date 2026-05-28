"""
tracker.py — 백그라운드 활동 감시 + 트리거 감지

1초 주기로 active_window / idle_time / switch_count 를 수집하고,
4가지 트리거 중 하나라도 발생하면 Warning 상태로 전환하면서
on_trigger 콜백을 호출한다.

상태 머신:
    NORMAL  →  (트리거)        →  WARNING
    WARNING →  resume_normal() →  NORMAL
    *       →  sleep()         →  SLEEP   (모든 평가 정지)
"""

from __future__ import annotations

import datetime
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Deque, Dict, Optional, Tuple

# PC 활동 감시용 OS 의존 패키지 — Linux 헤드리스 컨테이너(Railway 백엔드) 등에는
# 설치되어 있지 않다. import 자체는 가드로 살리되, 실제 OS 호출은 Tracker
# 인스턴스화 시점에 자연스럽게 실패한다 (헤드리스에선 어차피 인스턴스 안 만듦).
# 이렇게 두면 ENUM·dataclass·유틸 함수만 import 하는 경로는 깨지지 않는다.
try:
    import pygetwindow as gw   # type: ignore[import-not-found]
    from pynput import keyboard, mouse   # type: ignore[import-not-found]
except ImportError:
    gw = None                  # type: ignore[assignment]
    keyboard = None            # type: ignore[assignment]
    mouse = None               # type: ignore[assignment]


class State(Enum):
    NORMAL = auto()
    WARNING = auto()
    SLEEP = auto()


class TriggerType(Enum):
    DOPAMINE_ZOMBIE = "도파민 좀비"
    DISTRACTED_SWITCHING = "산만함/널뛰기"
    FAKE_WORKING = "가짜 일하기"
    OVER_IMMERSION = "과몰입 딴짓"
    ACTIVE_SCROLL = "능동적 도파민 스크롤"
    LATE_NIGHT = "늦은 밤"
    OVERWORK = "휴식 없는 과로"
    PERSONAL_WEAKNESS = "개인 약점 앱"
    POMODORO_BREAK = "Pomodoro 휴식"


ENTERTAINMENT_KEYWORDS = (
    "youtube", "netflix", "twitch", "disney", "tiktok",
    "instagram", "facebook", "tving", "wavve", "afreecatv", "chzzk",
)
WORK_KEYWORDS = (
    "word", "excel", "powerpoint", "code", "notion",
    "visual studio", "intellij", "pycharm", "hwp", "한글", "google docs",
)
# 생산성 앱 — 활성 창이 여기 해당하면 약점·산만 외 트리거는 *억제*. 사용자가
# 일하고 있는 상황에서 잔소리가 끼어드는 걸 막는다. WORK_KEYWORDS 보다 넓고
# 도메인 별 도구를 포함.
PRODUCTIVITY_KEYWORDS = (
    # IDE·개발 도구
    "visual studio", "vscode", "intellij", "pycharm", "webstorm", "rider",
    "android studio", "eclipse", "xcode", "sublime text", "neovim",
    "terminal", "powershell", "cmd.exe", "wsl",
    # 문서·오피스
    "word", "excel", "powerpoint", "hwp", "한글", "notion", "obsidian",
    "google docs", "google sheets", "google slides", "onenote", "evernote",
    "confluence", "logseq",
    # 디자인·미디어 제작
    "figma", "sketch", "adobe", "photoshop", "illustrator", "premiere",
    "after effects", "blender", "davinci",
    # 학습·업무 협업
    "zoom", "teams", "slack", "asana", "jira", "trello", "linear",
    "github", "gitlab", "stack overflow", "arxiv", "wikipedia",
)
ADDICTIVE_KEYWORDS = (
    "league of legends", "kakaotalk", "카카오톡",
    "discord", "steam", "battle.net", "valorant", "overwatch",
)


# 약점 키워드의 한·영 변형 매핑. 사용자가 "유튜브" 한 단어로 등록해도 크롬
# 창 제목 "...YouTube..." 와 매칭되도록. 각 키는 lowercase, 값은 함께 매칭할
# 변형 라벨들 (lowercase 비교). 새 변형은 자유롭게 추가.
_WEAK_KEYWORD_EXPANSIONS = {
    # 영상·SNS
    "유튜브": ["youtube"],
    "유투브": ["youtube"],
    "youtube": ["유튜브", "유투브"],
    "인스타": ["instagram", "인스타그램"],
    "인스타그램": ["instagram", "인스타"],
    "instagram": ["인스타", "인스타그램"],
    "틱톡": ["tiktok"],
    "tiktok": ["틱톡"],
    "페북": ["facebook", "페이스북"],
    "페이스북": ["facebook", "페북"],
    "facebook": ["페북", "페이스북"],
    "트위터": ["twitter", "x.com", "엑스"],
    "엑스": ["twitter", "x.com", "트위터"],
    "twitter": ["트위터", "x.com"],
    "x.com": ["트위터", "twitter"],
    "넷플릭스": ["netflix"],
    "netflix": ["넷플릭스"],
    "트위치": ["twitch"],
    "twitch": ["트위치"],
    "치지직": ["chzzk"],
    "chzzk": ["치지직"],
    "레딧": ["reddit"],
    "reddit": ["레딧"],
    "스냅챗": ["snapchat"],
    "snapchat": ["스냅챗"],
    # 메신저
    "카톡": ["kakaotalk", "카카오톡"],
    "카카오톡": ["kakaotalk", "카톡"],
    "kakaotalk": ["카톡", "카카오톡"],
    "디스코드": ["discord"],
    "discord": ["디스코드"],
    "라인": ["line"],
    "line": ["라인"],
    "밴드": ["band"],
    "band": ["밴드"],
    # 한국 커뮤니티·쇼핑
    "디시": ["dcinside", "디씨", "디시인사이드"],
    "디씨": ["dcinside", "디시", "디시인사이드"],
    "디시인사이드": ["dcinside", "디시", "디씨"],
    "에펨코리아": ["fmkorea"],
    "쿠팡": ["coupang"],
    "당근": ["daangn"],
    "웹툰": ["webtoon", "comic"],
}


def matches_weak_keyword(keyword: str, title_lower: str) -> bool:
    """약점 키워드가 active window title 에 매칭되는지 — 한·영 변형 모두 시도.
    title_lower 는 *이미 lowercase* 된 문자열을 받는다 (호출자가 처리)."""
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    if kw in title_lower:
        return True
    for variant in _WEAK_KEYWORD_EXPANSIONS.get(kw, ()):
        if variant.lower() in title_lower:
            return True
    return False


# =================================================================
# Sanitization — LLM 판독용 라벨링.
# 창 제목에서 식별 정보(파일명·검색어·대화방·페이지 제목)는 모두 제거하고
# "어떤 종류의 화면이었는지"만 남긴다. 클라우드(Gemini)로 보낼 때 사용.
# =================================================================
_BROWSER_KEYWORDS = (
    "chrome", "edge", "firefox", "whale", "safari", "brave", "opera",
)

# 키워드 포함 여부 → 사이트 도메인. 모르는 사이트는 "web:unknown".
_SITE_LABELS: Dict[str, str] = {
    "youtube": "youtube.com",
    "netflix": "netflix.com",
    "twitch": "twitch.tv",
    "tiktok": "tiktok.com",
    "instagram": "instagram.com",
    "facebook": "facebook.com",
    "reddit": "reddit.com",
    "x.com": "x.com",
    "twitter": "twitter.com",
    "github": "github.com",
    "stack overflow": "stackoverflow.com",
    "stackoverflow": "stackoverflow.com",
    "google docs": "docs.google.com",
    "notion.so": "notion.so",
    "wikipedia": "wikipedia.org",
    "arxiv": "arxiv.org",
    "naver": "naver.com",
    "tistory": "tistory.com",
    "tving": "tving.com",
    "wavve": "wavve.com",
    "afreecatv": "afreecatv.com",
    "chzzk": "chzzk.naver.com",
    "disney": "disneyplus.com",
    "chatgpt": "chat.openai.com",
    "claude.ai": "claude.ai",
    "gemini": "gemini.google.com",
    "linear": "linear.app",
    "jira": "jira",
    "confluence": "confluence",
}

# 메신저: 대화방 이름은 무조건 버리고 앱 이름만 남긴다.
_MESSENGER_LABELS: Dict[str, str] = {
    "kakaotalk": "kakaotalk",
    "카카오톡": "kakaotalk",
    "slack": "slack",
    "discord": "discord",
    "telegram": "telegram",
    "teams": "teams",
}

# 에디터/IDE/오피스: 파일명 본체는 버리고 (앱 이름 + 확장자)만.
_EDITOR_LABELS: Dict[str, str] = {
    "visual studio code": "vscode",
    "visual studio": "visual-studio",
    "intellij": "intellij",
    "pycharm": "pycharm",
    "webstorm": "webstorm",
    "powerpoint": "powerpoint",
    "excel": "excel",
    "word": "word",
    "hwp": "hwp",
    "한글": "hwp",
    "notepad": "notepad",
    "obsidian": "obsidian",
    "sublime": "sublime-text",
}

# 파일 확장자 추출용 (첫 매칭만 사용 — 보통 제목 앞쪽에 파일명이 옴).
_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,6})\b")


def sanitize_window_title(title: str) -> str:
    """창 제목을 식별 정보 없는 카테고리 라벨로 변환.

    티어:
        브라우저 → web:<사이트도메인 or unknown>
        메신저   → <앱이름>
        에디터   → <앱이름>:<.확장자>  (확장자 없으면 앱이름만)
        그 외    → other
    """
    if not title:
        return "other"
    low = title.lower()

    if any(b in low for b in _BROWSER_KEYWORDS):
        for kw, site in _SITE_LABELS.items():
            if kw in low:
                return f"web:{site}"
        return "web:unknown"

    for kw, name in _MESSENGER_LABELS.items():
        if kw in low:
            return name

    for kw, name in _EDITOR_LABELS.items():
        if kw in low:
            m = _EXT_RE.search(title)
            return f"{name}:{m.group(0).lower()}" if m else name

    return "other"


@dataclass
class Snapshot:
    active_window: str
    idle_time: float
    switch_count: int
    state: State


@dataclass
class _InputState:
    last_event_ts: float = field(default_factory=time.time)
    recent_events: Deque[float] = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Tracker:
    POLL_INTERVAL = 1.0

    SWITCH_WINDOW_SEC = 300.0          # 5분 슬라이딩 윈도우
    SWITCH_THRESHOLD = 12              # > 12회 → Trigger 2
    MIN_DWELL_SEC = 3.0                # 새 창에 3초 미만 머문 전환은 노이즈로 무시

    DOPAMINE_IDLE_SEC = 600.0          # 엔터테인먼트 + 10분 무입력 → Trigger 1
    FAKE_WORK_IDLE_SEC = 300.0         # 업무앱 + 5분 무입력 → Trigger 3

    OVER_IMMERSION_DURATION = 1800.0   # 30분 지속 → Trigger 4
    OVER_IMMERSION_INPUT_WINDOW = 60.0 # 최근 60초 입력 카운트
    OVER_IMMERSION_INPUT_THRESHOLD = 60  # 분당 60회 이상 = "고빈도 입력"

    POST_RESUME_COOLDOWN_SEC = 60.0    # 잔소리 직후 같은 트리거 재발동 방지

    ACTIVE_SCROLL_DURATION = 900.0     # 엔터테인먼트 능동 사용 15분 → 능동 스크롤
    ACTIVE_SCROLL_MAX_IDLE = 120.0     # 유휴 2분 미만이어야 '능동'으로 침
    LATE_NIGHT_START_HOUR = 1          # 새벽 1시 ~ 5시 사이를 '늦은 밤'으로
    LATE_NIGHT_END_HOUR = 5
    OVERWORK_DURATION = 7200.0         # 휴식 없이 2시간 연속 → 과로
    POMODORO_DURATION = 3000.0         # 50분 연속 작업 → 짧은 휴식 권유 (Pomodoro)
    BREAK_IDLE_SEC = 300.0             # 5분 이상 유휴 = '쉬는 중'으로 인정
    WEAKNESS_DWELL_SEC = 180.0         # 개인 약점 앱에 3분 → 빠른 경고

    def __init__(
        self,
        on_trigger: Callable[[TriggerType, Snapshot], None],
        get_weak_spots: Optional[Callable[[], list]] = None,
    ) -> None:
        self._on_trigger = on_trigger
        self._get_weak_spots = get_weak_spots or (lambda: [])

        self._state = State.NORMAL
        self._state_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._input = _InputState()
        self._switch_history: Deque[float] = deque()
        # LLM 판독용: (확정된 전환 시각, sanitized 라벨) 튜플 — 같은 SWITCH_WINDOW_SEC.
        self._recent_sanitized: Deque[Tuple[float, str]] = deque()
        self._last_window: str = ""
        self._pending_window: str = ""        # dwell 필터용 후보 창
        self._pending_since: float = 0.0      # 후보 창이 처음 보인 시각
        self._immersion_started: Optional[float] = None
        self._scroll_started: Optional[float] = None
        self._continuous_since: Optional[float] = None
        self._weakness_started: Optional[float] = None
        # Pomodoro 휴식 — 50분 연속 작업 시 한 번 발사 (한 세션에 1회).
        # idle 5분 이상 가지면 카운터 리셋 + _pomodoro_fired 해제.
        self._pomodoro_started: Optional[float] = None
        self._pomodoro_fired: bool = False
        self._late_night_fired_on: Optional[datetime.date] = None
        self._cooldown_until: float = 0.0

        self._kb_listener: Optional[keyboard.Listener] = None
        self._mouse_listener: Optional[mouse.Listener] = None

    # =================================================== public API
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._start_input_listeners()
        self._thread = threading.Thread(
            target=self._loop, name="TrackerLoop", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """완전 종료 (앱 종료 시)."""
        self._stop_event.set()
        self._stop_input_listeners()

    def sleep(self) -> None:
        """Sleep State 진입 — 메인 목표 달성 시 호출."""
        with self._state_lock:
            self._state = State.SLEEP

    def resume_normal(self) -> None:
        """Warning → Normal 복귀 — 스몰 스텝 완료 시 호출."""
        with self._state_lock:
            if self._state == State.SLEEP:
                return
            self._state = State.NORMAL
            self._cooldown_until = time.time() + self.POST_RESUME_COOLDOWN_SEC
            self._immersion_started = None
            self._scroll_started = None
            self._continuous_since = None
            self._weakness_started = None
            self._pomodoro_started = None
            self._pomodoro_fired = False

    def wake(self) -> None:
        """Sleep → Normal 복귀 — 목표 완료 후 새 목표를 잡았을 때 호출.
        SLEEP이 아니면 아무 일도 하지 않는다."""
        with self._state_lock:
            if self._state != State.SLEEP:
                return
            self._state = State.NORMAL
            self._cooldown_until = time.time() + self.POST_RESUME_COOLDOWN_SEC
            self._immersion_started = None
            self._scroll_started = None
            self._continuous_since = None
            self._weakness_started = None
            self._pomodoro_started = None
            self._pomodoro_fired = False

    @property
    def state(self) -> State:
        return self._state

    def get_recent_window_freq(self) -> Dict[str, int]:
        """현재 슬라이딩 윈도우 안에 있는 sanitized 라벨의 빈도 dict.
        LLM '딴짓 판독' 호출 직전에 App 이 가져다 쓴다."""
        freq: Dict[str, int] = {}
        for _ts, label in self._recent_sanitized:
            freq[label] = freq.get(label, 0) + 1
        return freq

    def get_recent_label_sequence(self, max_items: int = 8) -> List[str]:
        """가장 최근 sanitized 라벨을 시간순(오래된 → 최근)으로 반환. 같은 라벨이
        연속되면 *압축*해서 한 번만 — 도파민 trail (직전 행동 시퀀스) 학습용."""
        out: List[str] = []
        for _ts, label in self._recent_sanitized:
            if not label:
                continue
            if out and out[-1] == label:
                continue
            out.append(label)
        return out[-max_items:]

    # =============================================== input listeners
    def _start_input_listeners(self) -> None:
        self._kb_listener = keyboard.Listener(on_press=self._on_input_event)
        self._mouse_listener = mouse.Listener(
            on_move=self._on_input_event,
            on_click=self._on_input_event,
            on_scroll=self._on_input_event,
        )
        self._kb_listener.start()
        self._mouse_listener.start()

    def _stop_input_listeners(self) -> None:
        if self._kb_listener:
            self._kb_listener.stop()
            self._kb_listener = None
        if self._mouse_listener:
            self._mouse_listener.stop()
            self._mouse_listener = None

    def _on_input_event(self, *_args, **_kwargs) -> None:
        now = time.time()
        with self._input.lock:
            self._input.last_event_ts = now
            self._input.recent_events.append(now)
            cutoff = now - self.OVER_IMMERSION_INPUT_WINDOW
            while self._input.recent_events and self._input.recent_events[0] < cutoff:
                self._input.recent_events.popleft()

    # ===================================================== main loop
    def _loop(self) -> None:
        while not self._stop_event.wait(self.POLL_INTERVAL):
            try:
                snap = self._collect()
            except Exception as exc:
                print(f"[Tracker] collect error: {exc}")
                continue

            with self._state_lock:
                state = self._state
                in_cooldown = time.time() < self._cooldown_until
            if state in (State.SLEEP, State.WARNING) or in_cooldown:
                continue

            trigger = self._evaluate(snap)
            if trigger is None:
                continue

            with self._state_lock:
                if self._state != State.NORMAL:
                    continue
                self._state = State.WARNING

            try:
                self._on_trigger(trigger, snap)
            except Exception as exc:
                print(f"[Tracker] callback error: {exc}")
                with self._state_lock:
                    self._state = State.NORMAL

    # ====================================================== sampling
    def _collect(self) -> Snapshot:
        title = self._get_active_window_title()
        now = time.time()

        # 창 전환은 "새 창에 MIN_DWELL_SEC 이상 머물렀을 때"만 카운트한다.
        # 알림 슥 보고 돌아오는 짧은 alt+tab 노이즈를 거른다.
        if title:
            if title == self._last_window:
                self._pending_window = ""
            elif self._pending_window != title:
                self._pending_window = title
                self._pending_since = now
            elif now - self._pending_since >= self.MIN_DWELL_SEC:
                self._switch_history.append(now)
                # 식별정보 제거된 라벨만 별도 보관 (LLM 판독용)
                self._recent_sanitized.append((now, sanitize_window_title(title)))
                self._last_window = title
                self._pending_window = ""

        cutoff = now - self.SWITCH_WINDOW_SEC
        while self._switch_history and self._switch_history[0] < cutoff:
            self._switch_history.popleft()
        while self._recent_sanitized and self._recent_sanitized[0][0] < cutoff:
            self._recent_sanitized.popleft()

        with self._input.lock:
            idle = now - self._input.last_event_ts

        return Snapshot(
            active_window=title,
            idle_time=idle,
            switch_count=len(self._switch_history),
            state=self._state,
        )

    @staticmethod
    def _get_active_window_title() -> str:
        try:
            win = gw.getActiveWindow()
            return win.title if win and win.title else ""
        except Exception:
            return ""

    # ====================================================== triggers
    @staticmethod
    def _is_productive_context(title_lower: str) -> bool:
        """현재 활성 창이 생산성 앱(IDE·문서·디자인 툴 등)이면 True.
        룰: 약점 앱·산만함·늦은 밤 외 트리거는 이 상태에서 발사 억제."""
        return any(k in title_lower for k in PRODUCTIVITY_KEYWORDS)

    def _evaluate(self, snap: Snapshot) -> Optional[TriggerType]:
        title = snap.active_window.lower()
        productive_context = self._is_productive_context(title)

        # Trigger 5: 늦은 밤 — 새벽 시간대 사용 (하룻밤에 한 번만)
        # 생산성 가드 *제외* — 야간 작업도 건강 이슈로 잔소리.
        now_dt = datetime.datetime.now()
        if self.LATE_NIGHT_START_HOUR <= now_dt.hour < self.LATE_NIGHT_END_HOUR:
            if self._late_night_fired_on != now_dt.date():
                self._late_night_fired_on = now_dt.date()
                return TriggerType.LATE_NIGHT

        # 이하 도파민·과몰입·과로 트리거는 생산성 컨텍스트에서 발사 억제.
        # 사용자가 *진짜 일하는 중*일 때 잔소리가 끼어드는 걸 막는다.
        # (산만함·약점 앱은 아래 별도 분기에서 따로 처리)

        # Trigger 1: 도파민 좀비 — 엔터테인먼트 + 10분 무입력
        if productive_context:
            pass  # 생산성 앱이면 도파민 좀비 트리거 스킵
        elif (
            any(k in title for k in ENTERTAINMENT_KEYWORDS)
            and snap.idle_time > self.DOPAMINE_IDLE_SEC
        ):
            return TriggerType.DOPAMINE_ZOMBIE

        # Trigger 2: 산만함/널뛰기 — 5분 내 창 전환 12회 초과
        # (각 전환은 새 창에 3초 이상 머무른 경우만 카운트됨 — _collect 참조)
        if snap.switch_count > self.SWITCH_THRESHOLD:
            return TriggerType.DISTRACTED_SWITCHING

        # Trigger 3: 가짜 일하기 — 업무앱 + 5분 무입력
        if (
            any(k in title for k in WORK_KEYWORDS)
            and snap.idle_time > self.FAKE_WORK_IDLE_SEC
        ):
            return TriggerType.FAKE_WORKING

        # Trigger 4: 과몰입 딴짓 — 중독성 앱 + 고빈도 입력 30분 지속
        # 생산성 컨텍스트면 스킵 — 사용자가 일하는 중에는 발사하지 않는다.
        if productive_context:
            self._immersion_started = None
        elif any(k in title for k in ADDICTIVE_KEYWORDS):
            with self._input.lock:
                events_per_min = len(self._input.recent_events)
            if events_per_min >= self.OVER_IMMERSION_INPUT_THRESHOLD:
                if self._immersion_started is None:
                    self._immersion_started = time.time()
                elif (time.time() - self._immersion_started) >= self.OVER_IMMERSION_DURATION:
                    self._immersion_started = None
                    return TriggerType.OVER_IMMERSION
            else:
                self._immersion_started = None
        else:
            self._immersion_started = None

        # Trigger 6: 능동적 도파민 스크롤 — 엔터테인먼트 + 활발한 사용 15분
        # 생산성 컨텍스트면 스킵.
        if productive_context:
            self._scroll_started = None
        elif (
            any(k in title for k in ENTERTAINMENT_KEYWORDS)
            and snap.idle_time < self.ACTIVE_SCROLL_MAX_IDLE
        ):
            if self._scroll_started is None:
                self._scroll_started = time.time()
            elif (time.time() - self._scroll_started) >= self.ACTIVE_SCROLL_DURATION:
                self._scroll_started = None
                return TriggerType.ACTIVE_SCROLL
        else:
            self._scroll_started = None

        # Trigger 7: 개인 약점 앱 — 프로필에서 학습한 약점 앱에 3분 머무름.
        # 한·영 변형 매핑으로 "유튜브" 등록해도 크롬 "YouTube" 창에 매칭.
        weak = [w for w in self._get_weak_spots() if w]
        if weak and any(matches_weak_keyword(w, title) for w in weak):
            if self._weakness_started is None:
                self._weakness_started = time.time()
            elif (time.time() - self._weakness_started) >= self.WEAKNESS_DWELL_SEC:
                self._weakness_started = None
                return TriggerType.PERSONAL_WEAKNESS
        else:
            self._weakness_started = None

        # Trigger 9: Pomodoro 휴식 — 50분 연속 작업 시 1회 권유 (한 세션 1회).
        # idle 5분 이상 시 카운터 리셋 + _pomodoro_fired 해제 → 다음 세션에 또.
        # 과로(120분) 와 별개 — 짧은 사이클의 가벼운 휴식 권유.
        if snap.idle_time >= self.BREAK_IDLE_SEC:
            self._pomodoro_started = None
            self._pomodoro_fired = False
        else:
            if self._pomodoro_started is None:
                self._pomodoro_started = time.time()
            elif (
                not self._pomodoro_fired
                and (time.time() - self._pomodoro_started) >= self.POMODORO_DURATION
            ):
                self._pomodoro_fired = True   # 한 세션에 한 번만
                return TriggerType.POMODORO_BREAK

        # Trigger 8: 휴식 없는 과로 — 5분 이상 쉬는 틈 없이 2시간 연속
        if snap.idle_time >= self.BREAK_IDLE_SEC:
            self._continuous_since = None
        else:
            if self._continuous_since is None:
                self._continuous_since = time.time()
            elif (time.time() - self._continuous_since) >= self.OVERWORK_DURATION:
                self._continuous_since = None
                return TriggerType.OVERWORK

        return None


# ============================================================ demo
def _demo() -> None:
    """단독 실행 시: 트리거가 발생할 때마다 콘솔에 출력하고 즉시 NORMAL로 복귀."""

    def on_trigger(trigger: TriggerType, snap: Snapshot) -> None:
        print(
            f"\n[TRIGGER] {trigger.value}\n"
            f"  active_window = {snap.active_window!r}\n"
            f"  idle_time     = {snap.idle_time:.1f}s\n"
            f"  switch_count  = {snap.switch_count}\n"
        )
        # demo: 즉시 NORMAL 복귀 (실제 앱에선 사용자가 [스몰 스텝 완료] 누를 때까지 대기)
        tracker.resume_normal()

    tracker = Tracker(on_trigger=on_trigger)
    tracker.start()
    print("[Tracker] 데모 시작. Ctrl+C 로 종료.")
    print("        활성 창/유휴 시간/창 전환을 1초마다 출력한다.\n")

    try:
        while True:
            time.sleep(5)
            snap = tracker._collect()  # 디버그용
            print(
                f"[snap] state={tracker.state.name:7s}  "
                f"idle={snap.idle_time:5.1f}s  "
                f"switches={snap.switch_count:2d}  "
                f"window={snap.active_window[:60]!r}"
            )
    except KeyboardInterrupt:
        print("\n[Tracker] 종료 중…")
    finally:
        tracker.stop()


if __name__ == "__main__":
    _demo()
