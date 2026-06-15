"""
ai_engine.py — Gemini 코치 에이전트 (google-genai SDK)

CoachAgent 는 텔레그램 사용자 메시지와 PC 추적기 이벤트를 하나의 대화 흐름에서
처리한다. 두 메커니즘이 분리돼 있다:

- 행동(agent loop): 멀티턴 Chat 세션 + 도구. 모델이 필요하다고 판단하면
  agent_tools.py 의 도구(캘린더 등)를 호출하고, 실행 결과를 되먹여 최종
  코치 답장을 만든다. 새 능력은 agent_tools 에 도구만 추가하면 된다.
- 지각(extraction): 대화 turn 끝마다 별도 focused 호출(_extract_state)로
  목표·진척·다음 행동·프로필을 뽑아 store 에 반영한다.

수다 모델에게 함수 호출까지 한 호흡에 시키면 답장이 깨지므로, 도구 실행
단계와 사용자에게 보여줄 최종 답장을 루프 안에서 분리해 다룬다.

환경변수:
    GEMINI_API_KEY   필수. https://aistudio.google.com/apikey 에서 발급.
    GEMINI_MODEL     선택. 기본값 gemini-3-flash-preview.
"""

import datetime
import json
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

from agent_tools import build_tools
from store import Store

load_dotenv()

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
# EXTRACT 는 구조화 JSON 추출만 — 창의성 X. 더 가벼운 모델로 분리 시 비용 절감.
# 미설정 시 메인 모델로 fall-back.
EXTRACT_MODEL = os.getenv("GEMINI_EXTRACT_MODEL", "") or DEFAULT_MODEL
API_KEY_ENV = "GEMINI_API_KEY"
RETRY_ATTEMPTS = 2
MAX_TOOL_ITERS = 5  # 에이전트 루프에서 도구 호출 연쇄 최대 횟수

# 모델이 시스템 입력 표시를 흉내 내 답장 맨 앞에 잘못 붙이는 대괄호 라벨.
_INTERNAL_TAG_RE = re.compile(r"^\s*\[[^\[\]\n]{1,16}\]\s*")

# 모델이 도구 실행 후 코치 답장 대신 내놓는 '상태 보고' 응답 (주로 영문).
_STATUS_ECHO_RE = re.compile(
    r"status:\s*ok"
    r"|successfully (updated|saved|set|completed|added|created)"
    r"|i('ve| have) (updated|saved|set|added|created)"
    r"|has been (saved|updated|set|added|created)",
    re.IGNORECASE,
)


class AIGenerationError(RuntimeError):
    """대화/판독 생성이 (재시도 포함) 모두 실패했을 때."""


# =================================================================
# 시스템 프롬프트
# =================================================================
COACH_SYSTEM_INSTRUCTION = """■ 정체
잔소리 코치 — 텔레그램으로 하루 종일 떠드는 챗봇. 포지션은 코치지만 본질은
오래 본 친구. 반말, 가볍게. 또박또박 설명조·"화이팅" 같은 영혼 없는 멘트 X.

■ 안전 가드 (자해·자살 신호) — 최우선
"죽고 싶어", "사라지고 싶어" 같은 자해·자살 의도를 직접 표현하면 잔소리·도구
호출 다 멈춰. 가볍게 받지 말고 잠깐 멈춰 들어줘 → "내가 AI 라 진짜 도움은
못 줘" 한 번 명시 → "1393(자살예방상담)이나 1577-0199(정신건강위기상담)에
전화해줘, 24시간 무료야" → 가까운 사람한테 연락 권유. 사용자한테 어떤
정신건강 진단명·상태 라벨링 금지, '치료'·'완치' 류 표현도 금지. 너는 그
영역 사람이 아니다.

■ 톤 정책 ([상태 메모]의 nag_policy)
- gentle: 압박 거의 빼라. 다그치지 말고 따뜻하게 옆에 있어주거나 작은 한 발만.
- balanced (기본): 농담조 친구. 적당히 쪼고 적당히 들어준다.
- strict: 좀 더 박력. 단 인격 모독·낙인은 절대 X, 행동에 대한 쪼임만.
nag_policy_asked == False 면 첫 잔소리 답장 끝에 한 줄: "(빡세거나 약하면
'살살 해줘' / '더 세게' 같이 말해줘)". 그 다음부턴 안 박아도 된다.

■ 평소 대화
- 먼저 '사람'이 돼라. 사용자 말에 진짜로 반응 — 어떤 말이든 목표로 잡아채려
  달려들지 마. "그거 됐어?" 매번 추궁 X — 친구다운 관심으로 가지 쳐 받아쳐.
- 잡담·하소연이면 코치 모드 끄고 들어주고 공감부터. 사용자 에너지·길이에 맞춰
  — 한 단어 인사에 문단으로 답하지 마.
- '오늘 뭐 할 거야' 는 자연스러운 타이밍에 슬쩍. 명령조 X.
- 두루뭉술한 목표("코딩", "공부") 면 "구체적으로 뭘?" 한 번 캐묻기. 취조 X.
- 진행은 가끔만 가볍게.

■ 사용자 알아가기 — **적극성 ↑**
- 잔소리 코치 = 친구. 잔소리만 잘하는 친구는 부담스럽다. 사용자가 *어떤
  사람*인지 꾸준히 궁금해해. [상태 메모] 의 '아직 모르는 부분' 항목을 봐:
  거기 떠 있는 영역 중 *대화 흐름에 자연스러운* 한 가지를 슬쩍 캐. 예:
  · 일·과제 얘기 흐름 → "전공이 뭐야?", "어떤 일 하는 거야?"
  · 휴식·딴짓 흐름 → "쉴 땐 보통 뭐 해?", "요즘 빠진 거 있어?"
  · 컨디션·잔소리 통하지 않을 때 → "보통 아침형이야 저녁형?"
- *한 turn 에 한 질문*만. 여러 개 들이밀면 취조다. 사용자가 답 안 하거나
  넘기면 *그 영역은 한동안 건드리지 마* — 다음 기회에 다른 영역.
- 알아낸 건 다음 대화에 자연스럽게 써먹어 — "너 저녁형이라며", "네 취미가
  ○○이라 했잖아".
- 평범한 대화에서도 *가지 쳐 받아쳐*. "오 그거 재밌어?", "오래 한 거야?"
  같은 follow-up 한 줄. 짧은 답으로 끝내는 친구는 매력 없음.
- 'core_values'(건강·성장·가족·창의성 같은 추상 가치)는 장기 목표·큰 과제를
  처음 꺼낼 때 "왜 의미 있어?" 한 번 캐묻기. 이후 작은 행동 권할 때 가끔
  가치와 연결 — "네 '성장' 가치랑 닿는 한 걸음이야". 매번 X.

■ 다음 행동 쪼개기
- 사용자가 시동 걸 때·막막해할 때 → 지금 당장 할 5~15분짜리 구체 행동 하나.
  일반론 X, 실제 작업에 밀착. 나쁜 예: "코드 한 줄 써". 좋은 예: "막혔다던
  로그인 함수, 에러 메시지부터 print 로 찍어봐".
- 저항 시 카드 돌려쓰기: 더 잘게 / 진짜 이유 캐묻기 / "딱 2분만" / 환경만 세팅.
- 큰 단발 과제("코딩 1시간", "방 정리") → register_today_goal_with_steps 로
  3~5개 작은 행동으로 미리 분해. sub_step 끝나면 advance_today_goal_step.
  분해 전 get_weekly_insight 한 번 확인 — 지난주 완료율 40% 미만이면 더 잘게
  (3개 이하, 각 2~5분). 캐파 안 맞는 분량 반복 = 좌절 학습 → 이탈.
- 시작 장벽 큰 작업이면 start_focus_session(분, 작업) 으로 짧은 타이머.
  gentle 10~15분 / balanced 20~25분 / strict 25분. 끝나면 시스템이 다시 묻는다.
- 한 걸음 내디딜 때마다(sub_step 진척·목표 완료·습관 수행) *짧게 축하*해 성취감을
  줘. advance_today_goal_step 결과에 진척바(▰▰▱)·누적 걸음·축하 지시가 오면 그
  진척바와 걸음 수를 답장에 그대로 보여주고 한 발 내디딘 걸 인정해. 누적 걸음은
  *절대 줄지 않는* 거라 가볍게 상기해도 좋아 (연속 streak 압박은 주지 마 — 끊겨도
  0 되는 거 없음). 과한 호들갑 X, 진심 어린 짧은 축하.

■ WOOP / if-then plan (반복 약점)
사용자가 같은 약점에 자꾸 무너지거나 큰 변화를 시도할 때 — 4단계로 끌어내
(한 번에 다 X, 대화 흐름에 끼워):
(1) Wish: "뭘 바꾸고 싶어?"
(2) Outcome: "그게 되면 어떤 느낌일 거 같아?"
(3) Obstacle: "근데 *너 안에서* 뭐가 막아?" (감정·습관·생각, 외부 환경 X)
(4) Plan: "'상황 X 오면, 그땐 Y 한다' 식으로 한 줄."
Plan 구체적이면 register_implementation_intention 저장. 의지력 대신 미리
정해둔 자동 반응으로 약점을 우회하는 방식.
저장된 plan 은 [상태 메모]에 떠. 매칭 상황 보이면 가볍게 상기 — "저번에 ○○
오면 △△한다 했잖아." 매번 X, 적절한 타이밍만.

■ 반복 실패 — 책망 금지
며칠 연속 못 끝내도 "왜 못 했지" 책망 절대 X. 자기비난이 쌓이면 더 회피하게
된다 — 자기연민에서 출발해야 풀린다. 방향 틀어 — 캐파/환경 점검 질문, 더
작은 한 걸음(2분짜리), 인정·풀어주는 톤 ("오늘 못 한 거 OK, 내일은 더 작게").
같은 크기로 또 잡지 마.

■ 습관
- 단발 과제와 다름: 매일 이어가며 조금씩 키움. register_habit 으로 3~4단계
  점진적 레벨 등록 (예: 독서 10분→20분→30분). 마지막이 상한, 무한정 X.
- set_alarm 으로 정기 알람도. 알람 문구는 일반적으로 ("독서할 시간!").
- 현재 레벨 = 단기 목표 ([상태 메모]의 레벨 기준). 며칠 이어가면 칭찬, 시스템
  레벨업 시 같이 신나게. 수행 기록·레벨업은 시스템이 자동.

■ mood 데이터 활용
- 하루 마무리 일지·주간 회고 메시지엔 자동으로 mood 1~5 inline 버튼이 붙는다.
  코치 답장 본문에 "기분 어땠어?" 같은 직접 질문 굳이 X — 버튼이 알아서 받음.
- [상태 메모] 의 'mood 추세' 항목이 보이면, 답장 흐름에 *짧게* 끌어와 사용자
  동기 부여 — "오늘 적은 mood 봤어, 어제보다 좋아졌네", "이번 주 평균 좀
  떨어진 것 같아 — 컨디션 어때?" 같은 식. 매 답장 X — 자연스러운 흐름일 때만.
- 자유 텍스트 답장에 기분이 묻어나면 log_mood 로 직접 기록 (rating 1~5).
  버튼·텍스트 둘 다 같은 통계에 들어간다.

■ 도구
- 활성 도구는 [상태 메모]의 '활성 도구' 항목에 떠. **그 목록에 없는 도구는
  호출·언급도 하지 마** — 예: 캘린더 도구가 없으면 "캘린더에 넣어줄까?" 같이
  없는 기능을 권하지 마. 사용자한테 거짓 약속하지 마.
- 캘린더·알람·일정: 약속·시각 잡을 때 진짜 호출. "내일"·"금요일" 은 [지금:] 으로
  절대 시각 환산. 셋은 다 다른 도구:
  · set_alarm — 정해진 시각에 한 번 메시지 보내달라는 단순 알람 ("1시간 뒤 물").
  · add_event — 봇 자체 일정 (Google 인증 X). 시각 N분 전 자동 미리 알림.
    "내일 3시 회의" 같은 *일정 자체* 를 저장해 다가오는 일정 목록에서 보여줌.
  · add_calendar_event — 활성 도구일 때만. 사용자 Google 계정 캘린더에 등록.
- get_weekly_insight: 추세 묻기·구체 격려·캐파 가이드 필요할 때. 매번 X.
- log_mood: 사용자가 기분·컨디션 직접 꺼낼 때만, 발화에서 1~5 짐작. 1~5점
  명시적으로 묻지 마.
- 평범한 수다엔 도구 X.
- 도구 결과는 코치답게 녹여 — "Status: OK" 절대 X. 그냥 사람처럼: "내일
  3시 회의 캘린더에 넣어놨어, 잊지 마!"

■ 완료 증거 사진
- "다 했어" 들어오면 곧장 축하 X — 사진 찍을 만한 일이면 "인증샷 ㄱㄱ" 요구.
  찍기 애매한 일(전화·명상)은 강요 X, 말로 받아줘.
- 사진 받으면 입력에 [완료 보고 사진] + AI 판독이 같이 옴. 판독 신뢰 —
  진짜면 신나게 인정, 가짜·흐릿하면 "이걸론 애매한데? 다시" 되돌려.
- 인증 안 보내면 그 목표는 미완료로 남음. 다음에 또 슬쩍 "그거 인증샷은?"

■ 답장 형식
- 첫 글자부터 사람처럼. "[자동 감지]"·"[지금:]" 같은 라벨 X (입력 표시지
  네 출력 X). 짧게 — 한두 문장, 길어도 세 문장. 이모지 가끔만.
- 한국어 반말. 영어·"Status: OK" 같은 시스템 문구 X.
- **도구·함수명·내부 키 절대 노출 금지** — [상태 메모]나 도구 description 에
  나오는 `start_focus_session`, `register_today_goal_with_steps`, `log_mood`,
  `add_event`, `nag_policy` 같은 영문 식별자는 사용자 답장에 *한 글자도* 박지
  마라. 사용자는 그 단어 보면 즉시 신뢰 잃는다.
- 자연어로 풀어서:
  · "start_focus_session 켤게" ✗ → "20분 타이머 켜둘게" ✓
  · "register_today_goal_with_steps 로 분해" ✗ → "네 단계로 쪼개 둘게" ✓
  · "log_mood 기록" ✗ → "기분 메모해뒀어" ✓
  · "add_event 등록" ✗ → "내일 3시 회의 일정으로 잡아둘게" ✓
  · "nag_policy gentle 로 변경" ✗ → "톤 좀 살살로 바꿔둘게" ✓"""


EXTRACT_SYSTEM_INSTRUCTION = """너는 코치 대화에서 '상태 변화'만 뽑아내는 추출기다.
[지금 시각], [현재 저장된 상태], 그리고 방금 오간 [방금 들어온 메시지]·[코치
답장]을 보고, 이번 turn에서 새로 생기거나 바뀐 것만 JSON 객체 하나로 출력한다.

키:
- "new_today_goals": 사용자가 '오늘 할 일'(단발성 과제)을 새로 말했으면 그
  목표들을 *단순 문자열 리스트* 로 (여러 개면 여러 개). 꾸준히 하는 습관은
  제외 — 그건 register_habit 도구가 따로 다룬다. 큰 과제의 sub_step 분해도
  여기에 넣지 마라 — 그건 register_today_goal_with_steps 도구가 따로 다룬다.
  [현재 저장된 상태]에 이미 있는 목표도 제외 (이름만 비교; 괄호 안 '(N/M, 다음
  단계: ...)' 같은 진척 표시는 무시). 없으면 [].
- "completed_today_goals": 오늘 목표 중 완료된 게 있으면 *단순 문자열 리스트*
  로. 단 '완료'는 코치 답장이 실제로 완료를 인정·축하한 경우만 — 코치가
  "인증샷 보내봐" 하고 증거를 요구한 상태면 아직 완료가 아니다. [현재 저장된
  상태]의 목표 이름에 맞춰서 *정확하게* (괄호 안 진척 표시는 무시). 단발 과제의
  sub_step 하나만 끝난 거면 여기 넣지 마라 — 그건 advance_today_goal_step 도구가
  처리한다. 없으면 [].
  · ⚠️ 두 목표에 모두 매칭될 짧은 키워드("발표"가 "발표 자료"·"발표 연습"
    둘 다일 때)는 *어느 쪽인지 명확할 때만* 넣어라. 모호하면 비운다 —
    스토어가 모호 매칭을 무시하므로 부정확한 표현은 그냥 누락된다.
- "long_term_goal": 장기적인 꿈·목표가 새로 드러났으면 한 문장, 아니면 "".
- "habit_done": 진행 중인 습관 중 오늘 수행이 코치 답장에서 인정된 게 있으면
  그 습관 이름 ([현재 저장된 상태]의 습관 목록 기준). 코치가 인증샷을 요구한
  상태면 아직 아니다. 아니면 "".
- "progress": 사용자가 진척("했어", "끝냈어", "잘되네" 등)을 알렸으면 그 내용을
  한 문장으로 요약, 아니면 "".
- "next_step": 코치 답장이 '지금 당장 할 구체적 행동 하나'를 제시했으면 그
  행동을 한 문장으로. 막연한 격려·질문은 제외하고 구체적 행동만. 없으면 "".
- "back_on_track": 딴짓하다가 다시 목표 작업으로 돌아온 게 확인되면 true,
  아니면 false.
- "profile_updates": 이번 대화에서 사용자가 *자기 입으로 직접 말한* 정보만
  {"항목": "내용"} 객체로. 나이·직업·취미·성격·사는 곳·생활 습관은 물론,
  "○○ 좋아함/싫어함" 같은 가벼운 취향·선호도 한 줄짜리도 적극 담아라.
  · 항목 이름을 정확하게 붙여라. 음식을 좋아하는 건 "취미"가 아니라
    "좋아하는 음식"이다. "취미"는 등산·게임·악기처럼 실제 활동에만.
  · **추측·간접 추정 금지** — 사용자가 *명시적으로* 말한 것만. 활동 어휘로
    직업·정체성 추측하면 안 된다:
    "발표자료 만들어야 돼" → 직업 '개발자'/'학생' 추정 ✗ (누구나 만듦)
    "코딩 좀 해야 돼" → 직업 '개발자' ✗ (취미·과제일 수도)
    "에러 메시지 print 추가" → 직업 추정 ✗
    "나 개발자야" / "회사에서 개발하고 있어" → 직업 '개발자' ✓
    "주말에 등산 가" → 취미 '등산' ✓
    "유튜브 자꾸 빠져" → 취미 '유튜브' ✗ (이건 약점 신호)
  · 애매하거나 추정이면 비운다 ({}).
  · "core_values": 사용자가 '왜 그 목표가 의미 있는지' 말하면서 드러난 핵심
    가치(예: "가족 챙기는 사람이고 싶어", "창의적으로 살고 싶어", "건강한 몸이
    1순위"). 가족·건강·창의성·자율·성장·관계 같은 추상 가치다. 한 사용자에
    1~3개. 이미 저장된 값과 중복 금지. 사용자가 직접 *그 가치를 입에 올린*
    경우만 — 활동에서 추정 X.
  · 예: {"직업": "개발자", "취미": "등산", "좋아하는 음료": "오레오치노",
        "core_values": "건강 / 성장"}.
  · 표준 항목 후보 (해당하는 게 있으면 *이런 키로*; 이 외에도 새 항목은 자유):
    "취미", "요즘 관심사", "직업", "전공", "강점", "스트레스원", "싫어하는 것",
    "생활 패턴 (아침형/저녁형)", "좋아하는 ○○", "core_values".
  단 '지금 잠깐의 기분·상태'(예: "지금 피곤함", "오늘 바쁨")나 그 순간의
  사실(날씨 등)은 프로필이 아니다 — 넣지 마라. [현재 저장된 상태]에 이미
  있는 정보도 넣지 마라. 없으면 {}.
- "weak_spots": 사용자가 "나 이거에 약해", "여기서 자꾸 무너져" 처럼 자기
  딴짓 약점을 털어놨으면 그 앱·사이트 키워드를 리스트로. **PC 브라우저 창
  제목과 매칭이 잘 되도록 한글·영문 둘 다** (사용자가 한쪽으로만 말했어도):
    "유튜브 자꾸 봐" → ["유튜브", "youtube"]
    "인스타 무너져" → ["인스타", "인스타그램", "instagram"]
    "카톡 봐버려" → ["카톡", "카카오톡", "kakaotalk"]
    "트위터 빠져" → ["트위터", "twitter", "x.com"]
    "디시 들어가" → ["디시", "디씨", "dcinside"]
  짧은 키워드만, [현재 저장된 상태] 에 이미 있는 건 제외. 없으면 [].
- "nag_policy_signal": 사용자가 *명시적으로* 톤 강도를 바꿔달라고 했을 때만
  "gentle"/"strict"/"balanced" 중 하나, 아니면 "". *영구* 적용 신호임.
  · "gentle": "살살 해줘", "쪼지 마", "그만 잔소리해", "오늘은 그냥 둬"
  · "strict": "더 세게", "빡세게", "더 쪼아줘"
  · "balanced": "그냥 보통으로", "원래대로 돌려" 처럼 명시적으로 중간 요청.
  ⚠️ "지쳤어/힘들어/우울해" 같은 *컨디션 토로* 는 여기 아님 — 아래 condition_signal 로.
  단순 잡담·짜증("아 짜증나")은 신호 아님. 명시 의향이 있을 때만 잡아라.
- "quiet_signal": 사용자가 *외출·취침*을 알려 자동 잔소리를 *모두 보류* 해야
  하면 "away" 또는 "sleep". 모드 *해제* 신호면 "exit". 신호 아니면 "".
  · "away" 진입: "나간다", "나갈게", "외출", "지하철이야", "이동 중",
    "약속 가", "잠깐 다녀올게", "출근", "퇴근" 등 — *지금 집 밖이거나 곧 그럴
    상황*.
  · "sleep" 진입: "잘게", "자러 간다", "이제 잘 거야", "취침", "굿나잇" 등.
  · "exit" 해제: "다녀왔어", "왔어", "들어왔어", "일어났어", "굿모닝",
    "이제 깨어났어" 같이 *돌아옴·깨어남* 명시. 모드가 활성 중이 아니면 LLM이
    굳이 "exit" 잡지 마라 — 빈 문자열.
  · 단순 잡담·예측("내일 외출할까")은 신호 아님. *지금* 들어가는 거여야.

- "condition_signal": 사용자가 컨디션 저하를 토로해 *일시적으로* 톤을 낮춰야
  할 신호. "down" 또는 "". *24시간 일시* 적용임 (base 톤은 안 건드림).
  · "down" 신호: "지쳤어", "힘들어", "우울해", "번아웃", "기력 없어",
    "뭐 하기 싫어", "오늘 망했어" 같은 상태 토로.
  · 사용자가 컨디션 회복을 말해도 ("이제 좀 괜찮아") 굳이 해제 신호로 잡지
    마라 — 시간 만료로 자연 복귀하는 게 기본 정책이다.
  · 명시적으로 톤 강도를 말한 거면 (위 nag_policy_signal) 거기로만 잡고
    여기는 "" 둔다 — 한 발화가 둘 다 발동시키지 않는다.

규칙: 이번 turn에서 실제로 일어난 것만 잡는다. 추측·과잉 추출 금지.
애매하면 비운다("" 또는 false). 설명 없이 JSON 객체 하나만 출력한다."""


JUDGE_SYSTEM_INSTRUCTION = """[시스템 역할]
너는 사용자의 화면 전환 패턴을 보고 '딴짓(도파민 서핑) 중'인지 '업무/리서치 중'인지 분류하는 분류기야.

[판단 기준]
- 메인 목표와 거쳐간 화면 카테고리/사이트의 부합도를 본다.
- 예: 목표 '논문 리서치' + arxiv.org, github.com, wikipedia 다수 → 업무 중.
- 예: 목표 '코드 작성' + youtube.com, reddit.com, instagram, 메신저 다수 → 딴짓 중.
- 메신저·일반 웹 1~2회 정도의 짧은 확인은 업무 중으로 봐줘. 비율로 판단해라.
- 'web:unknown' 이나 'other' 가 많아 단정이 어려우면 보수적으로 업무 중(FALSE)으로 판단해라.

[출력 규칙]
- 오직 'TRUE' 또는 'FALSE' 한 단어만 출력. 마침표·설명·이모지 금지.
- 'TRUE' = 딴짓 중. 'FALSE' = 업무/리서치 중.
- 애매하면 'FALSE'."""


class CoachAgent:
    """Gemini 대화 세션 + 도구 에이전트 루프 + 상태 추출을 감싼 코치 에이전트.

    _turn → _agent_loop 가 모델↔도구 루프를 돌려 답장을 만들고, 이어서
    _extract_state 가 목표·진척·프로필을 뽑아 store 에 반영한다.

    트래커·텔레그램 폴링·프로액티브·리마인더 스레드가 동시에 호출하므로 모든
    생성 호출은 _lock 으로 직렬화한다 (Chat 세션은 스레드 안전하지 않음)."""

    def __init__(
        self,
        store: Store,
        *,
        on_goal_set: Optional[Callable[[str], None]] = None,
        on_today_complete: Optional[Callable[[], None]] = None,
        on_back_on_track: Optional[Callable[[], None]] = None,
        on_quiet_exit: Optional[Callable[[dict], None]] = None,
        model_name: str = DEFAULT_MODEL,
        calendar=None,
    ) -> None:
        key = os.getenv(API_KEY_ENV)
        if not key:
            raise RuntimeError(
                f"환경변수 {API_KEY_ENV}가 비어있어. "
                ".env 파일에 Gemini API 키를 넣어줘."
            )
        self._client = genai.Client(api_key=key)

        self._store = store
        self._on_goal_set = on_goal_set or (lambda _g: None)
        self._on_today_complete = on_today_complete or (lambda: None)
        self._on_back_on_track = on_back_on_track or (lambda: None)
        # quiet_mode 해제 시 호출 — 보류 건수 한 줄 안내 발송 등에 활용.
        self._on_quiet_exit = on_quiet_exit

        self._lock = threading.Lock()
        self._model_name = model_name
        # EXTRACT 전용 모델 — 환경변수로 분리 시 cheap 모델 사용 가능. 미설정·
        # 미발견 시 메인 모델로 자동 fallback.
        self._extract_model_name = EXTRACT_MODEL
        if self._extract_model_name != self._model_name:
            print(f"[CoachAgent] EXTRACT 별도 모델: {self._extract_model_name}")
        self._calendar = calendar
        self._tools = build_tools(
            calendar, store,
            on_goal_set=self._on_goal_set,
            analyze_patterns=self._build_pattern_analysis,
        )  # {name: Tool}
        self._chat = self._new_chat()

    # 프로필 빈 영역 식별 — '사용자 알아가기' 가이드의 실시간 hint 원천.
    # key=(매칭 키워드 후보), value=사람이 읽을 한국어 라벨. 매칭은 substring·
    # 동의어 기반으로 가볍게. 사용자 발화는 다양해서 EXTRACT 가 임의의 키로
    # 저장할 수 있으니 *완벽* 보다는 *대략의 빈 영역* 식별이 목적.
    _PROFILE_TOPICS = [
        (("취미", "hobby"), "취미"),
        (("관심", "요즘 빠진", "recent_interest"), "요즘 관심사"),
        (("직업", "occupation", "전공", "공부", "학교"), "직업·전공"),
        (("강점", "잘하는", "자랑"), "강점·자랑할 만한 것"),
        (("싫어", "스트레스", "지친", "힘든"), "스트레스원·싫어하는 것"),
        (("아침형", "저녁형", "기상", "수면"), "생활 패턴 (아침형/저녁형)"),
        (("core_value", "가치"), "삶의 가치"),
    ]

    @classmethod
    def _missing_profile_topics(cls, profile: dict) -> List[str]:
        """표준 알아가기 항목 중 아직 채워지지 않은 것들의 한국어 라벨 목록.
        EXTRACT 가 어떤 키로 저장했든 *유사 매칭* 하므로 정확하진 않지만, 코치가
        '뭘 더 알아야 하지?' 단서로 활용하기엔 충분하다."""
        if not isinstance(profile, dict):
            profile = {}
        keys_lower = " ".join(str(k).lower() for k in profile.keys())
        vals_lower = " ".join(str(v).lower() for v in profile.values())
        haystack = keys_lower + " " + vals_lower
        missing: List[str] = []
        for candidates, label in cls._PROFILE_TOPICS:
            if not any(c.lower() in haystack for c in candidates):
                missing.append(label)
        return missing

    # ===================================================== 세션 관리
    def _state_summary(self) -> Optional[str]:
        """재시작 후에도 모델이 현재 상태를 알도록 대화 앞에 끼울 요약."""
        parts: List[str] = []
        # 활성 도구 목록 — 캘린더 미연결처럼 환경에 따라 빠지는 도구가 있어
        # 모델이 없는 기능을 권하거나 거짓 답변을 만들지 않도록 항상 노출.
        if self._tools:
            parts.append(
                "활성 도구 (목록에 있는 것만 호출·언급 가능): "
                + ", ".join(sorted(self._tools.keys()))
            )
        # quiet_mode 활성 — 사용자가 외출·취침을 알려 자동 발사가 보류된 상태.
        # 코치는 이 정보를 알아야 답장 톤이 어색하지 않다 ("잘 시간이지" 같이
        # 다그치는 어조 X). 또 사용자 발화에서 *해제* 신호가 보이면 EXTRACT 가
        # quiet_signal="exit" 로 잡아준다.
        qmode = self._store.quiet_mode
        if qmode:
            label = "외출 중" if qmode == "away" else "취침 중"
            parts.append(
                f"quiet_mode: {label} — 자동 잔소리 보류 상태. 사용자가 직접 "
                "메시지를 보냈으니 *친구처럼 자연스럽게* 받아줘. 다그치지 마."
            )
        # 활성 톤 (active) = temp 살아있으면 temp, 아니면 base. 잔소리 답변은
        # 이 값에 맞춰 작성. 명시 변경 시점에 base 도 같이 갱신됨.
        active_policy = self._store.active_nag_policy
        asked = self._store.nag_policy_asked
        # 항상 노출 — 모델이 잔소리 톤을 일관되게 맞추도록.
        parts.append(
            f"nag_policy: {active_policy}"
            + ("" if asked else " (첫 잔소리 직후 톤 조절 안내를 한 번 띄울 것)")
        )
        # 직전 turn 에서 텔레그램 전송이 실패한 답장이 있으면, 이번 답장에
        # 자연스럽게 합쳐서 응답하도록 가이드. 새 답장이 도달되면 자동 클리어.
        pending = self._store.get_pending_chat_reply()
        if pending and pending.get("text"):
            parts.append(
                "[직전 답장 전송 실패] 아래 답장이 사용자에게 도달하지 못했어. "
                "이번 답장에 *자연스럽게 합쳐서* 응답해 — '아까 못 보낸 말인데'처럼 "
                "어색하게 별도 언급하지 말고, 두 turn 의 맥락이 매끄럽게 이어지게:\n"
                f"  └ {pending['text']}"
            )
        goals_detailed = self._store.today_goals_detailed
        if goals_detailed:
            lines: List[str] = []
            for g in goals_detailed:
                sub = g.get("sub_steps") or []
                if sub:
                    cur = int(g.get("current", 0) or 0)
                    if cur < len(sub):
                        lines.append(
                            f"{g['name']} ({cur}/{len(sub)}, 다음 단계: '{sub[cur]}')"
                        )
                    else:
                        lines.append(f"{g['name']} ({cur}/{len(sub)})")
                else:
                    lines.append(g["name"])
            parts.append("오늘 목표: " + " / ".join(lines))
        if self._store.long_term_goal:
            parts.append(f"장기 목표: {self._store.long_term_goal}")
        if self._store.progress:
            parts.append(f"최근 진행: {self._store.progress}")
        if self._store.next_step:
            parts.append(f"지금 할 다음 행동: {self._store.next_step}")
        profile = self._store.profile
        if profile:
            facts = ", ".join(f"{k}={v}" for k, v in profile.items())
            parts.append(f"사용자에 대해 알아낸 것 — {facts}")
        # 빈 영역 hint — LLM 이 흐름에 맞게 한 가지씩 가볍게 물어 채우도록 단서 제공.
        # 같은 turn 에 여러 개 묻지 말고, 이미 알아낸 건 [사용자에 대해 알아낸 것] 에
        # 들어있으니 중복 X. 취조 톤 X — 친구가 슬쩍 묻는 분위기.
        missing = self._missing_profile_topics(profile)
        if missing:
            parts.append(
                "아직 모르는 부분: " + ", ".join(missing)
                + " — 이 중 *대화 흐름에 자연스러운* 한 가지만 가끔 슬쩍 물어봐 "
                "(취조 X, 한 turn 에 여러 개 X)."
            )
        # 폰 디바이스 컨텍스트 — 최근 1시간 안에 보고된 게 있으면 노출. 코치가
        # 답장에 '오늘 200걸음밖에 안 됐네' 같이 자연스럽게 끌어올 수 있게.
        try:
            ctx = self._store.phone_context
            ctx_age = time.time() - float(ctx.get("at") or 0.0)
            if ctx and ctx_age < 3600.0:
                bits = []
                place = ctx.get("place_category")
                if place and place != "other":
                    bits.append(f"지금 '{place}' 에 있음")
                if ctx.get("steps_today") is not None:
                    bits.append(f"오늘 걸음 {int(ctx['steps_today'])}보")
                if ctx.get("headphones_connected") is True:
                    bits.append("이어폰 연결 중")
                if ctx.get("dnd_active") is True:
                    bits.append("폰 방해 금지(DND) 상태")
                if ctx.get("charging") is True and ctx.get("screen_on") is False:
                    bits.append("폰 충전 중·화면 OFF")
                if bits:
                    parts.append("폰 상태: " + ", ".join(bits))
        except Exception:
            pass

        # mood 추세 — 데이터 충분히 쌓이면 코치가 답장에 *짧게* 끌어와 사용자
        # 동기 부여. 너무 자주 X — 자연스러운 흐름일 때만.
        try:
            weekly = self._store.weekly_summary(days=7)
            rec = weekly.get("recent") or {}
            prev = weekly.get("previous") or {}
            mood_count = int(rec.get("mood_count") or 0)
            mood_avg = rec.get("mood_avg")
            if mood_count >= 5 and mood_avg is not None:
                hint = f"mood 추세: 최근 7일 평균 {mood_avg:.1f}/5 ({mood_count}회 기록)"
                prev_avg = prev.get("mood_avg")
                if prev_avg is not None:
                    diff = mood_avg - prev_avg
                    sign = "+" if diff >= 0 else ""
                    hint += f", 직전 주 대비 {sign}{diff:.1f}"
                hint += " — 답장 자연스러운 흐름에 한 번씩 슬쩍 끌어와 (매번 X)."
                parts.append(hint)
        except Exception:
            pass
        events_line = self._upcoming_events_summary()
        if events_line:
            parts.append(events_line)
        intentions = self._store.implementation_intentions
        if intentions:
            # 컨텍스트 비대 방지 — 최근 10개만 노출. 사용자가 100개 쌓아도
            # 시스템 프롬프트가 폭주하지 않게.
            recent = intentions[-10:]
            ii_lines = [
                f"'{p['situation']}' → '{p['response']}'" for p in recent
            ]
            note = (
                f"미리 정해둔 if-then plan (최근 {len(recent)}/{len(intentions)})"
                if len(intentions) > 10
                else "미리 정해둔 if-then plan"
            )
            parts.append(f"{note} — " + "; ".join(ii_lines))
        habits = self._store.habits
        if habits:
            today = datetime.date.today().isoformat()
            hb = []
            for h in habits:
                done = "오늘 ✓" if h.get("last_done") == today else "오늘 아직"
                levels = h.get("levels") or []
                idx = h.get("level_idx", 0)
                cur = levels[idx] if 0 <= idx < len(levels) else h["name"]
                cap = " (정착 단계)" if levels and idx >= len(levels) - 1 else ""
                hb.append(
                    f"{h['name']} [현재 목표: {cur}{cap}, "
                    f"연속 {h.get('streak', 0)}일, {done}]"
                )
            parts.append("진행 중인 습관 — " + "; ".join(hb))
        if not parts:
            return None
        return "[상태 메모 — 이전 대화에서 파악된 내용] " + " / ".join(parts)

    def _upcoming_events_summary(self) -> Optional[str]:
        """다가오는 일정 한 줄 — Google 캘린더 + 봇 자체 일정 둘 다 합쳐서.
        둘 다 비어 있거나 둘 다 실패하면 None."""
        items: List[str] = []
        # Google 캘린더
        if self._calendar is not None:
            try:
                events = self._calendar.list_upcoming(max_results=5)
                for e in events:
                    items.append(f"{e['start']} {e['summary']}")
            except Exception as exc:
                print(f"[CoachAgent] Google 일정 조회 실패 (무시): {exc}")
        # 봇 자체 일정
        now_ts = time.time()
        own = sorted(
            [e for e in self._store.events if (e.get("start_ts") or 0) >= now_ts],
            key=lambda e: e.get("start_ts", 0),
        )[:5]
        for e in own:
            when = datetime.datetime.fromtimestamp(e["start_ts"]).strftime(
                "%Y-%m-%d %H:%M"
            )
            items.append(f"{when} {e.get('summary')}")
        if not items:
            return None
        return "다가오는 일정 — " + ", ".join(items)

    def _chat_config(self) -> types.GenerateContentConfig:
        config = types.GenerateContentConfig(
            system_instruction=COACH_SYSTEM_INSTRUCTION,
        )
        # 도구가 있으면 선언을 붙이고 자동 함수 호출(AFC)은 끈다 — 루프를
        # _agent_loop 가 직접 돌려야 도구 실행과 최종 답장을 분리할 수 있다.
        if self._tools:
            config.tools = [
                types.Tool(
                    function_declarations=[
                        t.declaration for t in self._tools.values()
                    ]
                )
            ]
            config.automatic_function_calling = (
                types.AutomaticFunctionCallingConfig(disable=True)
            )
        return config

    @staticmethod
    def _content(role: str, text: str) -> types.Content:
        return types.Content(role=role, parts=[types.Part(text=text)])

    def _new_chat(self):
        """store에 저장된 대화 기록 + 상태 요약으로 Chat 세션을 복원한다."""
        history: List[types.Content] = []
        summary = self._state_summary()
        if summary:
            history.append(self._content("user", summary))
            history.append(self._content("model", "응, 다 기억하고 있어."))
        for turn in self._store.history:
            role = turn.get("role")
            text = turn.get("text", "")
            if role in ("user", "model") and text:
                history.append(self._content(role, text))
        try:
            return self._client.chats.create(
                model=self._model_name,
                config=self._chat_config(),
                history=history,
            )
        except Exception as exc:
            print(f"[CoachAgent] 대화 기록 복원 실패 — 새 세션으로 시작: {exc}")
            return self._client.chats.create(
                model=self._model_name, config=self._chat_config()
            )

    def reset(self, hard: bool = False) -> None:
        """대화 기록·목표를 비우고 세션을 새로 시작한다 (/reset).
        hard=True 면 통계·설정·자기학습까지 전부 (/reset all)."""
        with self._lock:
            if hard:
                self._store.hard_reset()
            else:
                self._store.clear_conversation()
            self._chat = self._new_chat()

    # ===================================================== 대화 처리
    def chat(self, user_text: str, *, system_note: Optional[str] = None) -> str:
        """사용자가 텔레그램으로 보낸 메시지를 처리하고 코치 응답을 돌려준다.
        system_note 가 있으면 이번 turn 한정으로 코치에게 귀띔하는 시스템 참고를
        앞에 끼운다 (예: 밤잠 추론 '자다 깸?' 확인)."""
        with self._lock:
            if system_note:
                return self._turn(f"[시스템 참고: {system_note}]\n{user_text}")
            return self._turn(user_text)

    def handle_event(self, description: str) -> str:
        """트래커가 감지한 딴짓 상황을 대화 흐름에 끼워 잔소리를 생성한다."""
        with self._lock:
            return self._turn(f"[자동 감지] {description}")

    def proactive_checkin(self) -> str:
        """사용자가 한동안 조용할 때, 코치가 먼저 말을 거는 메시지를 생성한다.
        안 끝낸 오늘 목표가 있으면 그걸 콕 집어 챙기고, 다가오는 일정이 있으면
        자연스럽게 한 번 상기시킨다 (강요 X)."""
        with self._lock:
            # 야간(0~6시) proactive 는 '깨어있음이 명백할 때만'(폰 하느라 안 잠)
            # 발사된다 (app_loops 의 야간 가드). → 할 일 채근이 아니라 슬슬 자라고
            # 챙기는 쪽으로 전환.
            if datetime.datetime.now().hour < 7:
                return self._turn(
                    "[자동 트리거] 새벽인데 사용자가 안 자고 깨어 있어(기기 활동 감지). "
                    "다그치지 말고 '아직 안 자네?' 하고 슬슬 자라고 한마디 — 한두 문장, "
                    "따뜻하게. 할 일·목표 채근은 하지 마."
                )
            goals = self._store.today_goals
            if goals:
                goal_line = (
                    "아직 안 끝낸 오늘 목표가 있어: "
                    + ", ".join(f"'{g}'" for g in goals)
                    + ". 사용자가 이걸 하지도, 했다고 말하지도 않고 그냥 "
                    "미루는 중일 수 있어 — 그 목표를 콕 집어 '그거 아직이지? "
                    "슬슬 하자' 하고 챙겨."
                )
            else:
                goal_line = (
                    "안 끝낸 오늘 목표는 없어 — '뭐 하느라 조용해?' 하고 "
                    "안부나 가볍게 떠봐."
                )
            upcoming = self._upcoming_events_summary()
            upcoming_note = (
                f" 참고 — {upcoming}. 잊지 않게 자연스럽게 한 번 슬쩍 끼워 "
                "상기시켜도 좋아 (강요 X, 그날 목표 챙기는 본문에 가볍게 곁들이는 정도)."
                if upcoming
                else ""
            )
            prompt = (
                f"[자동 트리거] 사용자가 한참 답이 없어. {goal_line}{upcoming_note} "
                "다그치지 말고 친구처럼 가볍게, 한두 문장으로. 너무 늦은 "
                "시각이면 무리시키지 말고 쉬라고 해."
            )
            return self._turn(prompt)

    def event_reminder(self, event_summary: str, minutes_left: int) -> str:
        """캘린더 일정이 곧 시작할 때, 코치 톤의 리마인더 메시지를 생성한다."""
        with self._lock:
            prompt = (
                f"[자동 트리거] 곧 '{event_summary}' 일정이 {minutes_left}분 "
                f"뒤에 시작해. 사용자가 안 까먹게 코치로서 한마디 해 — "
                f"친구처럼 가볍게, 한두 문장으로."
            )
            return self._turn(prompt)

    def daily_journal(self) -> str:
        """매일 정해진 시간에 사용자에게 한 줄 회고를 유도. 부담 없이 한 문장,
        강요하지 않는 톤. 사용자가 답하면 EXTRACT 가 mood·note 로 자동 기록."""
        with self._lock:
            # 오늘 활동 컨텍스트를 짧게 — 답하기 좋은 단서.
            buckets = self._store.daily_stats
            today = datetime.date.today().isoformat()
            today_b = buckets.get(today) or {}
            gc = today_b.get("goals_completed", 0)
            hd = today_b.get("habit_dones", 0)
            ssa = today_b.get("sub_step_advances", 0)
            tt = sum((today_b.get("triggers") or {}).values())
            ctx_bits = []
            if gc:
                ctx_bits.append(f"목표 {gc}개 완료")
            if ssa:
                ctx_bits.append(f"한 발 {ssa}번")
            if hd:
                ctx_bits.append(f"습관 {hd}회")
            if tt:
                ctx_bits.append(f"잔소리 {tt}회")
            ctx = (", ".join(ctx_bits)) if ctx_bits else "기록 거의 없음"
            lifetime = self._store.lifetime_steps
            # 오늘 한 발이라도 내디뎠으면 '걸음 결산' 으로 먼저 짧게 축하.
            recap_note = (
                f" 오늘 앞으로 내디딘 걸음들({ctx}) 을 한 줄로 모아 *먼저 짧게 "
                f"축하*해줘 (누적 {lifetime}걸음 — 절대 안 줄어드는 거라 슬쩍 곁들여도 좋아). "
                "그다음 회고를 유도해."
                if (gc or ssa or hd)
                else ""
            )
            prompt = (
                "[자동 트리거] 하루 마무리 시간이야. 사용자에게 한 줄로 "
                "'오늘 어땠어?' 같은 가벼운 회고를 유도해 — 강요 X, 압박 X. "
                "한 문장이면 충분하다는 신호도 줘."
                f"{recap_note} "
                f"오늘 활동 요약 (참고용, 그대로 옮기지는 마): {ctx}. "
                "사용자가 답하면 mood·기분이 자동 기록되니, 추후 회고에 활용된다는 "
                "안내는 굳이 하지 마."
            )
            return self._turn(prompt)

    def risk_predict(self, target_hour: int, top_trigger_label: str, fire_count: int) -> str:
        """위험 예측 선제 알림. 과거 패턴상 target_hour 시간대에 자주 잡혔다는
        걸 사용자에게 *직전*에 살짝 짚어준다. 다그치지 않고 자기인식을 거든다.
        target_hour: 위험 시간대 (0~23). top_trigger_label: 그 시간대에 가장 잦은
        트리거 이름. fire_count: 최근 14일간 발사 누적 횟수."""
        with self._lock:
            prompt = (
                "[자동 트리거] 시간대 패턴 분석 결과, 사용자는 최근 2주 동안 "
                f"{target_hour}시 무렵에 '{top_trigger_label}' 트리거가 "
                f"{fire_count}번 잡혔어. 곧 그 시간대야. 다그치지 말고 자기인식을 "
                "돕는 톤으로, 한두 문장만. 예시: '곧 그 시간이네 — 어제 같은 시간에 "
                "유튜브 빠졌었어. 지금 뭘 하고 있어?' 같은 식. 패턴을 객관적으로 "
                "짚고, 사용자가 선택하게 둬. 라벨링 금지 ('습관이 약해' 같은 X)."
            )
            return self._turn(prompt)

    def overload_checkin(self) -> str:
        """잔소리가 누적해서 무시당하고 사용자가 한참 응답이 없을 때, 코치가
        '시스템 적합성' 점검을 자연스럽게 묻는 메시지를 생성한다. 자동으로 톤을
        바꾸지 않고 사용자에게 자기결정권을 돌려주는 게 핵심 — 시스템이 임의로
        분류·라벨링하지 않으면서도 자기인식 부재 케이스를 메꿈."""
        with self._lock:
            policy = self._store.nag_policy
            prompt = (
                "[자동 트리거] 사용자가 잔소리를 누적해서 무시하고 한참 답이 "
                "없어. 압박이 안 먹히고 있다는 신호일 수 있어 — 둘 중 하나일 "
                "가능성이 커: ① 지금 톤이 너무 빡세서 부담된다, ② 잡아둔 "
                f"목표가 사용자 캐파보다 크다. 현재 nag_policy={policy}. "
                "사용자한테 한 번 부드럽게 물어봐 — 다그치지 말고, '요즘 잔소리에 "
                "답이 잘 안 오네. 톤이 너무 빡센가? 아니면 목표가 좀 무겁나?' "
                "식으로. 한두 문장. 답이 'gentle 로 가자'면 그 신호를 받아 "
                "(다음 turn 에서 EXTRACT 가 잡아준다)."
            )
            return self._turn(prompt)

    def weekly_review(self) -> str:
        """매주 회고 ritual. 지난 7일 누적 데이터를 함께 끼워서 회고 대화를
        시작한다 — 잘된 것 하나·막힌 것 하나·다음 주 작은 한 가지를 친구처럼
        끌어내는 톤. 자주 잡혔지만 아직 weak_spots 에 없는 앱이 있으면
        '약점으로 등록할까?' 자연스럽게 한 번 확인."""
        with self._lock:
            summary = self._store.weekly_summary(days=7)
            rec = summary["recent"]
            prev = summary["previous"]
            top = rec["top_trigger"] or "-"
            week_steps = (
                rec["goals_completed"] + rec["habit_dones"]
                + rec.get("sub_step_advances", 0)
            )
            data_line = (
                f"이번 주 {week_steps}걸음 (목표·한 발·습관 합산, 누적 "
                f"{self._store.lifetime_steps}걸음 — 절대 안 줄어듦), "
                f"목표 완료 {rec['goals_completed']}회 (지난주 "
                f"{prev['goals_completed']}), 습관 수행 {rec['habit_dones']}회 "
                f"(지난주 {prev['habit_dones']}), 잔소리 트리거 "
                f"{rec['trigger_total']}회 (지난주 {prev['trigger_total']}), "
                f"가장 자주 잡힌 패턴 '{top}'"
            )
            # 자기학습 weak_spot 후보 — 자주 잡혔지만 아직 등록 안 된 앱들
            candidates = self._store.top_weak_spot_candidates(n=3)
            cand_line = ""
            if candidates:
                cand_str = ", ".join(
                    f"'{label}'(x{cnt})" for label, cnt in candidates
                )
                cand_line = (
                    f" 또 이번 주 자주 잡혔는데 약점 목록엔 아직 없는 앱: "
                    f"{cand_str}. 회고 끝물에 '이것도 약점으로 등록해둘까?' "
                    f"한 번 자연스럽게 물어봐 — 사용자가 '응' 하면 그게 EXTRACT 의 "
                    f"weak_spots 로 자동 잡힌다. 무리하게 들이밀진 마라."
                )
            prompt = (
                "[자동 트리거] 주간 회고 시각이야 (일요일 저녁). 지난 7일 "
                f"활동 데이터: {data_line}.{cand_line}\n"
                "사용자한테 회고 대화를 시작해 — 데이터를 인사처럼 가볍게 "
                "꺼내고 (수치는 자랑 말고 사실만), '잘됐던 거 하나, 막혔던 "
                "거 하나, 다음 주에 시도해볼 작은 거 하나' 식으로 한 단계씩 "
                "끌어내라. 첫 메시지는 두 문장 이내, 취조 말고 친구처럼. "
                "주말 저녁이니 너무 무겁지 않게."
            )
            return self._turn(prompt)

    def deliver_alarm(self, alarm_text: str) -> str:
        """예약된 알람 시각이 됐을 때, 코치 톤으로 그 알람을 전한다."""
        with self._lock:
            prompt = (
                f"[자동 트리거] 사용자가 미리 맞춰둔 알람 시각이야. 알람 내용: "
                f"'{alarm_text}'. 코치로서 이걸 자연스럽게 상기시키며 한마디 해 — "
                f"친구처럼 가볍게, 한두 문장으로."
            )
            return self._turn(prompt)

    def focus_session_end(self, what: str) -> str:
        """집중 세션 타이머가 끝났을 때 코치가 사용자한테 '어땠어?' 자연스럽게
        물음. 잘 됐다고 하면 칭찬, 더 가고 싶다고 하면 새 타이머 권유."""
        with self._lock:
            prompt = (
                f"[자동 트리거] 사용자가 시작한 '{what}' 집중 세션이 방금 끝났어. "
                f"짧게 어땠는지 물어봐 — '잘 됐어? 더 갈래, 좀 쉴래, 끝낼래?' "
                f"식으로. 한두 문장, 친구처럼 가볍게. 잘 됐다고 하면 작게 축하."
            )
            return self._turn(prompt)

    def verify_completion(
        self, image_bytes: bytes, caption: str = "",
        mime_type: str = "image/jpeg",
    ) -> str:
        """사용자가 보낸 '완료 증거' 사진을 분석하고 코치 톤으로 응답한다.
        사진 판독은 focused 비전 호출, 응답·완료 반영은 기존 대화 루프로."""
        with self._lock:
            observation = self._analyze_photo(image_bytes, mime_type, caption)
            note = f' (사진에 남긴 말: "{caption}")' if caption else ""
            send_text = (
                f"[완료 보고 사진] 사용자가 할 일을 끝냈다며 증거 사진을 "
                f"보냈어{note}. 사진 판독 결과: {observation}"
            )
            return self._turn(send_text)

    def _analyze_photo(
        self, image_bytes: bytes, mime_type: str, caption: str
    ) -> str:
        """완료 증거 사진을 판독해 '무엇을 했고 인정할 만한지'를 텍스트로 돌려준다."""
        goals = " / ".join(self._store.today_goals) or "(없음)"
        habits = ", ".join(h["name"] for h in self._store.habits) or "(없음)"
        next_step = self._store.next_step or "(없음)"
        cap_line = f"\n사용자가 사진과 함께 남긴 말: {caption}" if caption else ""
        prompt = (
            "사용자가 '할 일을 끝냈다'는 증거로 이 사진을 보냈다."
            f"{cap_line}\n"
            f"현재 오늘 목표: {goals}\n"
            f"현재 습관: {habits}\n"
            f"지금 할 다음 행동: {next_step}\n\n"
            "사진을 보고 판단해라: (1) 무엇을 한 것으로 보이는가, (2) 위 "
            "목표·습관·행동 중 어느 것의 완료 증거로 맞는가, (3) 정말 했다고 "
            "인정할 만한가 — 사진이 흐릿하거나 엉뚱하거나 증거가 약하면 솔직히 "
            "그렇다고 말해라. 2~3문장으로 간결하게."
        )
        config = types.GenerateContentConfig(
            system_instruction=(
                "너는 '할 일 완료 증거 사진'을 판독하는 분석기다. "
                "객관적으로, 간결하게, 한국어로 판단한다."
            ),
            temperature=0.2,
        )
        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            types.Part(text=prompt),
        ]
        try:
            response = self._call_genai(
                model=self._model_name,
                contents=contents,
                config=config,
                label="사진 판독",
            )
            text = self._safe_text(response)
        except Exception as exc:
            print(f"[CoachAgent] 사진 판독 실패: {exc}")
            return "사진을 제대로 분석하지 못했어 (판독 오류)."
        return text or "사진에서 뚜렷한 내용을 읽어내지 못했어."

    def _turn(self, send_text: str) -> str:
        """_lock을 잡은 상태에서 호출. 에이전트 루프로 한 번의 턴을 처리한다.
        빈 응답·상태 echo 같은 못 쓸 응답이면 세션을 store 기준으로 되돌려
        한 번 더 시도한다. 성공 시에만 store 에 기록하고 상태 추출을 돌린다."""
        last_err: Optional[str] = None
        for _ in range(2):
            try:
                raw = self._agent_loop(send_text)
            except Exception as exc:
                last_err = str(exc)
                self._chat = self._new_chat()
                continue
            reply = self._strip_internal_tags(raw)
            if reply and not self._looks_like_status_echo(reply):
                self._store.append_turn("user", send_text)
                self._store.append_turn("model", reply)
                self._extract_state(send_text, reply)
                return reply
            # 빈 응답 / 상태 echo — 세션 되돌리고 한 번 더.
            self._chat = self._new_chat()
        raise AIGenerationError(f"쓸 만한 응답을 받지 못함 ({last_err})")

    # ================================================= 에이전트 루프 (행동)
    def _agent_loop(self, send_text: str) -> str:
        """모델 ↔ 도구 루프. 모델이 도구를 호출하면 실행해 결과를 되먹이고,
        텍스트 응답이 나오면 그게 최종 코치 답장이다."""
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M (%a)")
        response = self._chat.send_message(f"[지금: {now}]\n{send_text}")
        for _ in range(MAX_TOOL_ITERS):
            calls = self._function_calls(response)
            if not calls:
                return self._safe_text(response)
            parts: List[types.Part] = []
            for call in calls:
                result = self._run_tool(call.name, dict(call.args or {}))
                parts.append(
                    types.Part.from_function_response(
                        name=call.name, response={"result": result}
                    )
                )
            response = self._chat.send_message(parts)
        return self._safe_text(response)

    def _run_tool(self, name: str, args: dict) -> str:
        """도구 하나를 실행하고 결과 요약 문자열을 돌려준다."""
        tool = self._tools.get(name)
        if tool is None:
            print(f"[CoachAgent] 알 수 없는 도구 호출: {name}")
            return f"실패: '{name}' 라는 도구는 없다."
        print(f"[CoachAgent] 도구 실행: {name}({args})")
        try:
            result = tool.run(args)
        except Exception as exc:
            result = f"실패: {exc}"
        print(f"[CoachAgent] 도구 결과: {result}")
        return result

    @staticmethod
    def _function_calls(response) -> list:
        """응답에서 function_call 파트들을 모은다."""
        calls = []
        try:
            for cand in response.candidates or []:
                content = getattr(cand, "content", None)
                for part in getattr(content, "parts", None) or []:
                    fc = getattr(part, "function_call", None)
                    if fc and getattr(fc, "name", None):
                        calls.append(fc)
        except Exception:
            pass
        return calls

    # ============================================= Gemini API 호출 wrapper
    # 일시적 503/504/timeout 에 짧은 backoff retry. 영구 오류(auth, quota)는
    # 즉시 raise — 시도해도 의미 없으므로 호출자가 빠르게 fail-fast.
    _GENAI_RETRY_BACKOFF = (1.0, 2.0)  # 시도 #2 전 1초, #3 전 2초

    def _call_genai(self, *, model: str, contents, config, label: str = "generate"):
        last_err: Optional[Exception] = None
        attempts = 1 + len(self._GENAI_RETRY_BACKOFF)
        for i in range(attempts):
            try:
                return self._client.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as exc:
                last_err = exc
                msg = str(exc).lower()
                # 영구 오류는 retry 무의미 — 즉시 raise
                if any(
                    k in msg
                    for k in ("api key", "permission", "unauthorized", "quota")
                ):
                    raise
                if i < attempts - 1:
                    delay = self._GENAI_RETRY_BACKOFF[i]
                    print(
                        f"[CoachAgent] {label} 일시 실패 ({exc}) — "
                        f"{delay:.0f}s 후 재시도 ({i+2}/{attempts})"
                    )
                    time.sleep(delay)
        raise last_err  # type: ignore[misc]

    # ================================================= 상태 추출 (지각)
    def _extract_state(self, last_input: str, coach_reply: str) -> None:
        """대화 turn 끝에 호출. 방금 오간 내용에서 목표·진척·프로필 변화를
        뽑아 store·콜백에 반영한다. 도구 없는 focused 호출이라 본 대화
        세션의 안정성을 해치지 않는다. 실패해도 조용히 넘어간다."""
        current = self._state_summary() or "(아직 저장된 목표·진척 없음)"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M (%a)")
        prompt = (
            f"[지금 시각]\n{now_str}\n\n"
            f"[현재 저장된 상태]\n{current}\n\n"
            f"[방금 들어온 메시지]\n{last_input}\n\n"
            f"[코치 답장]\n{coach_reply}"
        )
        config = types.GenerateContentConfig(
            system_instruction=EXTRACT_SYSTEM_INSTRUCTION,
            temperature=0.0,
            response_mime_type="application/json",
        )
        try:
            response = self._call_genai(
                model=self._extract_model_name,
                contents=prompt,
                config=config,
                label="상태 추출",
            )
            data = json.loads(self._safe_text(response) or "{}")
        except Exception as exc:
            print(f"[CoachAgent] 상태 추출 실패 (무시): {exc}")
            return
        if isinstance(data, dict):
            self._apply_extracted(data)

    def _apply_extracted(self, data: dict) -> None:
        """추출된 상태 변화를 store에 반영하고 필요한 콜백을 호출한다."""
        store = self._store

        def field(key: str) -> str:
            val = data.get(key)
            return val.strip() if isinstance(val, str) else ""

        new_goals = data.get("new_today_goals")
        if isinstance(new_goals, list):
            for g in new_goals:
                # 모델이 가끔 dict({"name": "..."}) 로 잘못 출력하는 케이스 방어.
                name = (
                    str(g.get("name", "")).strip()
                    if isinstance(g, dict)
                    else str(g).strip()
                )
                if name and store.add_today_goal(name):
                    print(f"[CoachAgent] 오늘 목표 추가: {name}")
                    self._on_goal_set(name)

        done_goals = data.get("completed_today_goals")
        if isinstance(done_goals, list):
            removed = False
            for g in done_goals:
                name = (
                    str(g.get("name", "")).strip()
                    if isinstance(g, dict)
                    else str(g).strip()
                )
                if name and store.complete_today_goal(name):
                    print(f"[CoachAgent] 오늘 목표 완료: {name}")
                    removed = True
            if removed and not store.today_goals:
                store.next_step = None
                print("[CoachAgent] 오늘 목표 전부 완료")
                self._on_today_complete()

        long_term = field("long_term_goal")
        if long_term and long_term != (store.long_term_goal or ""):
            store.long_term_goal = long_term
            print(f"[CoachAgent] 장기 목표 갱신: {long_term}")

        step = field("next_step")
        if step and step != (store.next_step or ""):
            store.next_step = step
            print(f"[CoachAgent] 다음 행동 갱신: {step}")

        progress = field("progress")
        if progress:
            store.progress = progress
            print(f"[CoachAgent] 진척 기록: {progress}")

        if data.get("back_on_track") is True:
            print("[CoachAgent] 목표 복귀 확인")
            self._on_back_on_track()

        updates = data.get("profile_updates")
        if isinstance(updates, dict):
            clean = {
                str(k).strip(): str(v).strip()
                for k, v in updates.items()
                if str(k).strip() and str(v).strip()
            }
            if clean:
                store.update_profile(clean)
                print(f"[CoachAgent] 프로필 갱신: {clean}")

        weak = data.get("weak_spots")
        if isinstance(weak, list):
            items = [str(w).strip() for w in weak if str(w).strip()]
            if items:
                store.add_weak_spots(items)
                print(f"[CoachAgent] 약점 앱 학습: {items}")

        policy_signal = field("nag_policy_signal")
        if policy_signal in ("gentle", "balanced", "strict"):
            if policy_signal != store.nag_policy:
                store.nag_policy = policy_signal
                print(f"[CoachAgent] 잔소리 강도 변경(base): {policy_signal}")

        condition_signal = field("condition_signal")
        if condition_signal == "down":
            # 컨디션 토로 — 24시간 동안만 gentle 로 일시 적용. base 톤은 보존.
            store.apply_temporary_policy("gentle", duration_sec=86400.0)
            print(f"[CoachAgent] 컨디션 신호 → 24h 일시 gentle 적용")

        # 사용자가 외출·취침을 알리거나 (away/sleep), 돌아옴·깨어남을 알릴 때
        # (exit) quiet_mode 진입·해제. 해제 시 보류 건수 안내는 app.py 의
        # _on_quiet_exit 콜백이 처리한다 (LLM 호출 X — 짧은 한 줄).
        quiet_signal = field("quiet_signal")
        if quiet_signal in ("away", "sleep"):
            if store.enter_quiet_mode(quiet_signal):
                print(f"[CoachAgent] quiet_mode 진입: {quiet_signal}")
        elif quiet_signal == "exit":
            info = store.exit_quiet_mode()
            if info is not None:
                print(f"[CoachAgent] quiet_mode 해제: {info}")
                if self._on_quiet_exit is not None:
                    try:
                        self._on_quiet_exit(info)
                    except Exception as exc:
                        print(f"[CoachAgent] quiet_exit 콜백 실패: {exc}")

        habit_done = field("habit_done")
        if habit_done:
            result = store.mark_habit_done(habit_done)
            if result:
                note = f"연속 {result['streak']}일"
                if result.get("leveled_up"):
                    note += f", 레벨업 → {result.get('level')}"
                print(f"[CoachAgent] 습관 수행: {habit_done} ({note})")

    # ============================================= 패턴 분석 (analyze_my_patterns)
    def _build_pattern_analysis(self, days: int) -> str:
        """최근 N일 daily_stats 를 자연어로 풀어 LLM 에 던지고, 의미 있는 패턴
        3~5개를 자연어로 받아온다. agent_tools 의 analyze_my_patterns 도구가
        이 메서드를 콜백으로 호출 — 그 호출 경로가 이미 _lock 잡힌 상태이므로
        여기선 lock 잡지 않는다 (재진입 deadlock 방지). store 메서드는 자체
        RLock 으로 thread-safe."""
        days = max(1, min(int(days or 30), 90))
        stats = self._store.daily_stats
        if not stats:
            return "최근 데이터가 아직 없다 — 며칠 더 써보고 분석해줘."

        # 최근 N일 raw 로그를 한 줄씩
        sorted_dates = sorted(stats.keys())[-days:]
        raw_lines: List[str] = []
        for d in sorted_dates:
            b = stats[d]
            triggers = b.get("triggers") or {}
            moods = b.get("mood_logs") or []
            if (
                not triggers
                and not moods
                and (b.get("goals_completed") or 0) == 0
                and (b.get("habit_dones") or 0) == 0
            ):
                continue
            parts = [d]
            if triggers:
                parts.append(
                    "트리거(" + ", ".join(f"{k}×{v}" for k, v in triggers.items()) + ")"
                )
            parts.append(
                f"목표 완료={b.get('goals_completed', 0)}/"
                f"{b.get('goals_registered', 0)}"
            )
            parts.append(f"습관={b.get('habit_dones', 0)}")
            if moods:
                ratings = [int(m.get("rating", 0) or 0) for m in moods]
                avg = sum(ratings) / max(1, len(ratings))
                parts.append(f"mood={avg:.1f}/5({len(moods)}회)")
                notes = [
                    str(m.get("note", "")).strip()
                    for m in moods
                    if str(m.get("note", "")).strip()
                ]
                if notes:
                    parts.append("메모: " + " / ".join(notes[:2]))
            raw_lines.append("  " + " | ".join(parts))

        if not raw_lines:
            return f"최근 {days}일 활동 데이터가 거의 없어 — 분석할 거리가 부족."

        weekly = self._store.weekly_summary(days=min(days, 14))
        rec = weekly["recent"]
        profile = self._store.profile
        weak = self._store.weak_spots

        ctx = (
            f"사용자 활동 로그 최근 {len(sorted_dates)}일:\n"
            + "\n".join(raw_lines)
            + f"\n\n누적 요약 (최근 {weekly['window_days']}일): "
            + f"목표 완료 {rec['goals_completed']}회 (등록 "
            + f"{rec.get('goals_registered', 0)}), "
            + f"습관 {rec['habit_dones']}회, "
            + f"잔소리 트리거 {rec['trigger_total']}회"
        )
        if rec.get("mood_avg") is not None:
            ctx += f", mood 평균 {rec['mood_avg']:.1f}/5 ({rec.get('mood_count', 0)}회)"
        if profile:
            ctx += f"\n프로필: {profile}"
        if weak:
            ctx += f"\n등록된 약점: {', '.join(weak)}"

        # 시간대별 매핑 — 골든타임 / 위험 시간대를 컨텍스트에 포함하면 LLM 이
        # 시간 관점 패턴을 짚을 수 있다. 데이터 적으면 자동 생략.
        hb = self._store.hourly_breakdown(days=min(days, 21))
        if hb.get("total_days_with_data", 0) >= 3:
            if hb["golden_hours"]:
                gh = ", ".join(f"{h}시(완료 {c}회)" for h, c in hb["golden_hours"])
                ctx += f"\n골든타임 (완료가 잦은 시간대): {gh}"
            if hb["risk_hours"]:
                rh = ", ".join(f"{h}시(트리거 {c}회)" for h, c in hb["risk_hours"])
                ctx += f"\n위험 시간대 (트리거가 잦은 시간대): {rh}"

        # 도파민 trail — 자주 등장하는 '딴짓 직전 행동 시퀀스'. if-then plan
        # 권유 후보로 LLM 이 활용. min_count=2 로 노이즈 차단.
        trails = self._store.top_dopamine_trails(n=3, min_count=2)
        if trails:
            ctx += "\n도파민 trail (딴짓 직전 자주 나오는 행동 흐름): " + " / ".join(
                f"[{' → '.join(seq)}] ×{c}" for seq, c in trails
            )

        prompt = (
            ctx + "\n\n"
            "위 데이터를 보고 의미 있는 패턴 3~5개를 뽑아라. 예시 방향:\n"
            "- 시간대·요일별 생산성/무너짐 패턴\n"
            "- 자주 잡히는 트리거 종류와 그 직전 활동\n"
            "- mood 가 ↑ / ↓ 되는 활동의 상관관계\n"
            "- 등록된 약점 외에 새로 의심되는 행동\n"
            "- 도파민 trail 이 있으면 그 흐름의 *첫 단계* 에서 끊는 if-then plan 제안\n"
            "각 패턴은 데이터로 뒷받침 가능해야 한다 — 추측 금지. "
            "친구한테 이야기 들려주듯 한국어 반말, 핵심만. "
            "맨 앞에 '최근 N일 패턴' 같은 라벨 X — 바로 본문 시작."
        )
        try:
            return self._generate_once(
                prompt,
                system_instruction=(
                    "너는 사용자 활동 로그에서 의미 있는 패턴을 추출하는 분석가. "
                    "데이터로 뒷받침되지 않는 추측·과장 금지. 한국어 반말, "
                    "친근하지만 사실 기반."
                ),
                temperature=0.3,
                max_output_tokens=600,
                label="pattern-analyze",
            )
        except AIGenerationError as exc:
            return f"패턴 분석 중 오류 — {exc}"

    # ================================================= 딴짓 판독 (무상태)
    def judge_distracted(
        self, main_goal: Optional[str], recent_windows: Dict[str, int]
    ) -> bool:
        """최근 화면 빈도를 보고 '딴짓 중'인지 LLM에게 묻는다.

        반환: True = 딴짓 중, False = 업무 중 / 애매. 실패 시 AIGenerationError.
        """
        items = sorted(recent_windows.items(), key=lambda x: -x[1])
        formatted = ", ".join(f"{label} (×{cnt})" for label, cnt in items)
        user_msg = (
            f"사용자의 메인 목표: {main_goal or '오늘의 목표(미설정)'}\n"
            f"최근 거쳐간 화면(빈도): {formatted or '(없음)'}\n\n"
            f"위 패턴이 딴짓인가?"
        )
        raw = self._generate_once(
            user_msg,
            system_instruction=JUDGE_SYSTEM_INSTRUCTION,
            temperature=0.0,
            max_output_tokens=10,
            label="judge",
        )
        cleaned = raw.strip().upper().lstrip("`*\"' ").rstrip("`*\"' .")
        return cleaned.startswith("TRUE")

    def _generate_once(
        self,
        user_msg: str,
        *,
        system_instruction: str,
        temperature: float,
        max_output_tokens: int,
        label: str,
    ) -> str:
        """focused 호출용 헬퍼 — 내부적으로 _call_genai 가 일시 오류 backoff retry.
        빈 응답은 1회 추가 시도 (안전 차단 등 케이스)."""
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        last_error: Optional[str] = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response = self._call_genai(
                    model=self._model_name,
                    contents=user_msg,
                    config=config,
                    label=label,
                )
                text = self._safe_text(response)
                if text:
                    return text
                last_error = "empty response"
            except Exception as exc:
                last_error = str(exc)
            print(
                f"[CoachAgent] {label} 빈 응답·실패 "
                f"({attempt}/{RETRY_ATTEMPTS}): {last_error}"
            )
        raise AIGenerationError(f"{label} 생성 실패: {last_error}")

    # --------------------------------------------------------------- helper
    @staticmethod
    def _strip_internal_tags(text: str) -> str:
        """모델이 답장 맨 앞에 잘못 붙인 대괄호 라벨을 제거한다."""
        cleaned = text
        while True:
            stripped = _INTERNAL_TAG_RE.sub("", cleaned, count=1)
            if stripped == cleaned:
                return cleaned.strip()
            cleaned = stripped

    @staticmethod
    def _looks_like_status_echo(text: str) -> bool:
        """코치 답장이 아니라 도구 실행 결과·JSON 을 echo 한 응답인지 판별."""
        if _STATUS_ECHO_RE.search(text):
            return True
        if re.search(r'\{\s*"', text):  # 답장에 JSON 객체 조각이 박힘
            return True
        return False

    @staticmethod
    def _safe_text(response) -> str:
        """response.text가 안전성 차단 등으로 비어있을 수 있어 candidates도 확인."""
        try:
            text = (response.text or "").strip()
        except Exception:
            text = ""
        if text:
            return text
        try:
            for cand in response.candidates or []:
                content = getattr(cand, "content", None)
                parts = getattr(content, "parts", None) or []
                joined = "".join(
                    getattr(p, "text", "") or "" for p in parts
                ).strip()
                if joined:
                    return joined
        except Exception:
            pass
        return ""
