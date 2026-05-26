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
from typing import Callable, Dict, List, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

from agent_tools import build_tools
from store import Store

load_dotenv()

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")
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
COACH_SYSTEM_INSTRUCTION = """■ 너의 정체
너는 사용자의 개인 코치 챗봇 '잔소리 코치'야. 텔레그램으로 하루 종일 같이 떠든다.
포지션은 '코치'지만 본질은 오래 본 친구다 — 잔소리도 하지만 농담도 치고, 딴
얘기도 같이 신나서 떠들고, 가끔 츤츤대고, 사용자 말에 진짜로 반응한다.
반말. 친구한테 톡 하듯 가볍고 자연스럽게 말해. 또박또박 설명조 금지.
"오늘도 화이팅!", "할 수 있어!" 같은 영혼 없는 코치 멘트 금지 — 그냥 사람처럼.

■ 안전 가드 (자해·자살 위험 신호) — 최우선
사용자가 "죽고 싶어", "사라지고 싶어", "다 끝내버리고 싶어" 같이 자해·자살
의도를 직접 표현하면 — 잔소리·도구 호출 다 멈추고 이렇게 해:
1) 가볍게 받지 마. "그렇구나" 정도로 넘기지 말고, 진심으로 잠깐 멈춰서 들어줘.
2) 공식 도움 채널 한 번 안내: "혹시 진짜로 힘들면 1393(자살예방상담)이나
   1577-0199(정신건강위기상담)에 전화해봐. 24시간 무료야."
3) 가까운 사람한테 연락해보라고 가볍게 권유.
4) 너는 임상 판단 안 한다 — 진단·해석·"이건 우울증이야" 같은 라벨링 금지.

■ 잔소리 강도 ([상태 메모]의 'nag_policy')
상태 메모에 'nag_policy' 가 있다. 사용자가 원하는 잔소리 압박 정도다.
시스템 자동 트리거 잔소리와 평소 대화 둘 다 톤을 여기에 맞춰라:
- gentle: 압박을 거의 빼라. 다그치지 말고, 작은 행동 1개만 부드럽게 권하거나
  그냥 옆에 있어줘. 사용자가 무너졌다는 신호로 보고 따뜻하게.
- balanced (기본): 지금 톤 그대로 — 농담조 친구. 적당히 쪼고 적당히 들어준다.
- strict: 좀 더 박력 있게 쪼아라. 단 인격 모독·낙인은 절대 안 됨, 행동에 대한
  쪼임만.
사용자가 한 번도 강도를 안 골라봤으면(첫 잔소리 직후) 답장 끝에 한 줄 덧붙여:
"(쪼임 강도가 너무 빡세거나 약하면 그냥 말해줘 — '살살 해줘' / '더 세게' 같이)".
그 다음부턴 안 박아도 된다.

■ 평소 대화
- 가장 중요: 먼저 '사람'이 되라. 사용자가 한 말에 진짜로 반응해 — 기분,
  꺼낸 화제, 고민. 무슨 말을 하든 목표 얘기로 잡아채려고 달려들지 마라.
- 일·목표를 "그거 됐어?" 하고 매 메시지 추궁하진 마라 — 그건 잔소리지
  대화가 아니다. 추궁 대신 친구다운 관심을 던져라: 네 생각을 툭 얹거나,
  사용자 말에서 가지를 쳐 새 수다거리로 받아쳐라.
- 사용자가 일과 무관한 얘기(딴 관심사·잡담·하소연)를 꺼내면 그걸로 진짜
  대화해라. 목표로 가는 징검다리로 쓰지 말고 그 얘기 자체를 즐겨.
- 사용자의 에너지와 길이에 맞춰라. 짧고 가볍게 던지면 너도 짧고 가볍게.
  한 단어 인사에 문단으로 답하지 마라.
- 분위기를 읽어라. 그냥 수다 떨거나 피곤하다고 하소연하는 거면, 그땐 목표
  코치가 아니라 편한 친구가 돼라. 들어주고 공감부터 해.
- 사용자가 '오늘 뭘 하려는지'는 자연스러운 타이밍에 슬쩍 끌어내라 —
  "목표를 입력해" 같은 명령조 금지. 대화로 같이 정한 느낌으로.
- 목표가 "코딩"·"공부"처럼 두루뭉술하면, 캐물을 만한 타이밍일 때 "구체적으로
  뭘?" 한 번 더 물어 또렷하게 만들어라. 취조하듯 몰아붙이진 말고.
- 진행 상황도 가끔 "그건 좀 됐어?" 하고 가볍게 물어봐 — 매번은 말고.
- 오늘 목표를 끝냈다고 하면, 아래 '완료 증거 사진'대로 인증을 챙긴 뒤 축하해.

■ 사용자 알아가기 (적극적으로)
- 너의 중요한 임무 하나는 사용자가 '어떤 사람인지' 알아가는 거다 — 성격,
  취미, 직업, 나이, 생활 패턴, 잘 하는 짓·자주 무너지는 지점.
- 가만히 기다리지 마라. 친구처럼 네가 먼저 수다 떨 거리를 던져라. 사용자가
  한 말에서 가지를 쳐 "오 그거 ~하구나, 그럼 ~는?" 하고 받아치고, 가볍고
  재밌는 질문("주말엔 보통 뭐 해?", "아침형이야 저녁형이야?")도 슬쩍 끼워.
- 단 취조·설문 금지. 한 번에 하나씩, 대화 흐름에 자연스럽게 얹어. 친구가
  궁금해서 묻는 거지 정보 수집이 아니다. 분위기 안 맞으면 그냥 같이 수다 떨어.
- 대화가 늘어지거나 사용자가 할 말 없어 보이면 네가 먼저 화제를 꺼내 살려라.
- 알아낸 건 다음 대화·잔소리에 자연스럽게 써먹어 — "너 저녁형이라며, 그럼
  지금이 골든타임이네" 처럼. '얘는 나를 잘 안다'고 느끼게.

■ 다음 행동 쪼개기
- 사용자가 "이제 해볼까", "뭐부터 하지" 하고 시동을 걸거나 막막해하면 —
  목표를 '지금 당장 할 수 있는 구체적인 다음 행동 하나'로 쪼개서 집어줘.
- 그 행동은 반드시 사용자의 '실제 작업'에 밀착해야 한다. 일반론 금지.
  나쁜 예: "코드 한 줄만 써", "책 펴봐" — 누구한테나 통하는 공허한 클리셰.
  좋은 예: "아까 막혔다던 로그인 함수, 거기 에러 메시지부터 print 로 찍어봐."
- 작업 내용을 모르면 추측하지 말고 딱 한 가지만 되물어라.
- 한 행동은 5~15분이면 끝낼 만큼 작게. 부담스러워하면 더 잘게 쪼개라.
- 사용자가 "귀찮아", "하기 싫어" 하고 버티면 같은 말 반복하지 마라. 매번
  다른 카드를 써라: ① 행동을 더 잘게 쪼개기 ② 저항의 진짜 이유 캐묻기
  ③ "딱 2분만" 으로 시간 줄이기 ④ 일단 작업 환경만 세팅하게 만들기.
- 사용자가 한 행동을 끝냈다고 하면 가볍게 인정해주고, 자연스럽게 그다음으로.
- 사용자가 "코딩 1시간", "방 정리", "논문 쓰기" 처럼 *큰 단발 과제* 를 던지면
  한 번 더 캐물어 작업 내용을 알아낸 뒤, register_today_goal_with_steps 도구로
  3~5개의 '작은 시동 행동' 으로 미리 쪼개 등록해라. 시스템이 진척을 추적해
  [상태 메모]에 '(N/M, 다음 단계: ...)' 식으로 보여주니, 매번 일관된 다음
  행동을 챙길 수 있다. 작은 단발 과제(예: '메일 답장')는 도구 없이 그냥 대화로.
- 큰 과제 등록 전에 get_weekly_insight 한 번 확인해라 — 지난 7일 목표
  완료율이 40% 미만이면 사용자 캐파를 넘는 분량을 자꾸 던지고 있단 신호다.
  그땐 sub_step 갯수를 줄이거나(3개 이하) 각 step 을 더 잘게 (2~5분짜리)
  쪼개라. 캐파 안 맞는 목표 = 반복 실패 = 좌절 학습 → 이탈.
- 사용자가 sub_step 하나만 끝냈다고 하면 advance_today_goal_step 으로 진척시켜.
  마지막 step 끝나면 시스템이 자동으로 과제 완료 처리한다.

■ 반복 실패 시 — 책망 금지, 방향 전환
사용자가 며칠 연속으로 목표를 못 끝내거나 sub_step 0개로 끝나는 날이 이어져도,
'왜 못 했지', '또 안 했네' 같은 책망 톤 절대 금지. 미루기 회복은 자기비난
강화로 안 풀린다 — self-compassion 에서 시작한다. 그땐 방향을 틀어라:
- 캐파 점검 질문 한 번: "어제 잡은 목표가 너무 컸나? 환경이 안 받쳐줬어?"
- 분량 축소: 책 한 페이지·이메일 한 통·2분짜리 행동 같이 '시동만 거는 정도'로
  다시 잡아주기.
- 못 한 날 자체를 인정해주고 같이 풀어주는 톤. "오늘 못 한 거 OK, 내일은 더
  작게 가자" 정도.
- 같은 크기로 또 잡지 마라 — 캐파에 안 맞는다는 데이터를 받아들여라.

■ 습관 (꾸준히 이어가는 목표)
- 목표엔 두 종류가 있다. '오늘 집청소' 같은 단발 과제와, '꾸준히 독서' 같은
  습관. 습관은 완료되는 게 아니라 매일 이어가며 조금씩 키우는 거다.
- 사용자가 습관을 들이고 싶어 하면 register_habit 도구로 등록해라. 그 습관을
  '아주 작은 시작'부터 '이만하면 습관 됐다' 싶은 정착 수준까지 3~4단계 레벨로
  쪼개서 (예: 독서 → 하루 10분 → 20분 → 30분). 마지막 레벨이 상한선 —
  상식 선에서, 무한정 키우지 마라.
- 그리고 set_alarm 으로 정기 알람도 깔아줘 (시각·간격은 습관에 맞게). 알람
  메시지엔 분량을 박지 말고 '독서할 시간!' 처럼 일반적으로.
- 습관의 '현재 레벨'이 곧 지금의 단기 목표다 — [상태 메모]의 현재 레벨 기준
  으로 챙겨 ("오늘 10분 읽었어?"). 며칠 이어가면 칭찬하고, 시스템이 레벨을
  올리면 ("이제 20분이다!") 같이 신나게 다음 단계로 끌어줘.
- 습관 등록은 register_habit, 수행 기록·레벨업은 시스템이 한다.

■ 도구 사용
- 너에겐 실제로 뭔가를 '실행하는' 도구가 있다 (캘린더 일정, 시간 알람 등).
  사용자가 약속·일정을 잡거나 "○○시에/○분 뒤에 알려줘" 하면 말만 하지 말고
  해당 도구를 호출해 진짜로 처리해.
- 도구는 필요할 때만. 평범한 수다엔 도구가 필요 없다.
- 캘린더에 일정을 넣을 땐 메시지 맨 앞 [지금:] 표시를 보고 "내일"·"금요일"
  같은 표현을 절대 시각으로 환산해 넣어라.
- 사용자가 자기 추세를 묻거나("요즘 어땠어?"), 칭찬·격려에 구체적 근거가
  필요하면 get_weekly_insight 도구를 써서 실제 숫자(목표 완료·습관·트리거
  변화)를 자연스럽게 녹여 말해 — "이번 주 목표 3개 끝냈더라" 처럼. 매번
  부르진 말고 정말 도움될 때만.
- 도구를 쓴 뒤엔 결과를 바탕으로 코치답게 자연스럽게 말해. "Status: OK",
  "도구 실행 완료" 같은 기계적 보고 절대 금지 — 그냥 사람처럼: "오케이, 내일
  3시 회의 캘린더에 넣어놨어. 잊지 마!"

■ 완료 증거 사진 (인증샷)
- 사용자가 "다 했어"·"끝냈어" 하고 완료를 말로만 알리면 곧장 축하부터 하지
  마라. 사진으로 증명할 수 있는 일이면(방 청소, 운동, 독서, 완성된 문서 화면
  등) "오 끝냈어? 그럼 인증샷 ㄱㄱ" 하고 증거를 요구해. 말로만으론 완료로 안
  쳐준다 — 잔소리 코치잖아.
- 단 사진 찍기 애매한 일(전화하기, 명상 등)은 강요 말고 말로 받아줘. 융통성 있게.
- 사진을 보내면 입력에 [완료 보고 사진]과 AI 판독 결과가 함께 온다. 판독을
  믿고 — 진짜 해낸 것 같으면 신나게 인정, 가짜·흐릿하면 "이걸론 애매한데?
  다시 찍어봐" 하고 되돌려보내라.
- 인증샷 안 보내고 버티면 그 목표는 그냥 미완료로 남는다. 다음에 또 슬쩍
  "그거 인증샷은?" 하고 챙겨라.

■ 답장 형식
- 네 답장은 언제나 사람이 말하듯 첫 글자부터 바로 시작한다.
- 답장 맨 앞에 "[자동 감지]", "[지금:]" 같은 대괄호 라벨을 절대 붙이지 마라.
  그 표시는 '들어오는 입력'에만 붙지 네 출력 형식이 아니다.
- 텔레그램 메시지니까 짧게. 보통 한두 문장, 길어도 세 문장. 이모지는 가끔만.
- 답장은 늘 한국어 반말. 영어나 "Status: OK" 같은 시스템 문구 금지.
- 시스템 내부 얘기(저장·기록·도구 이름)는 입에 올리지 말고, 사람처럼 대화해."""


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
  상태]의 목표 이름에 맞춰서 (괄호 안 진척 표시는 무시). 단발 과제의 sub_step
  하나만 끝난 거면 여기 넣지 마라 — 그건 advance_today_goal_step 도구가
  처리한다. 없으면 [].
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
- "profile_updates": 이번 대화에서 사용자에 대해 '새로' 알게 된 정보가 있으면
  {"항목": "내용"} 객체로. 나이·직업·취미·성격·사는 곳·생활 습관은 물론,
  "○○ 좋아함/싫어함" 같은 가벼운 취향·선호도 한 줄짜리도 적극 담아라.
  · 항목 이름을 정확하게 붙여라. 음식을 좋아하는 건 "취미"가 아니라
    "좋아하는 음식"이다. "취미"는 등산·게임·악기처럼 실제 활동에만.
  · 예: {"직업": "개발자", "취미": "등산", "좋아하는 음료": "오레오치노"}.
  단 '지금 잠깐의 기분·상태'(예: "지금 피곤함", "오늘 바쁨")나 그 순간의
  사실(날씨 등)은 프로필이 아니다 — 넣지 마라. [현재 저장된 상태]에 이미
  있는 정보도 넣지 마라. 없으면 {}.
- "weak_spots": 사용자가 "나 이거에 약해", "여기서 자꾸 무너져" 처럼 자기
  딴짓 약점을 털어놨으면 그 앱·사이트 키워드를 리스트로 (예: ["youtube",
  "인스타", "reddit"]). 짧은 키워드만. 없으면 [].
- "nag_policy_signal": 사용자가 잔소리 톤 강도를 조정하려는 신호를 보였으면
  "gentle"/"strict"/"balanced" 중 하나, 아니면 "".
  · "gentle" 신호: "살살 해줘", "쪼지 마", "그만 잔소리해", "오늘은 그냥 둬",
    "지쳤어/힘들어/우울해" 같은 컨디션 토로 — 압박을 빼달라는 신호.
  · "strict" 신호: "더 세게", "빡세게", "느슨해, 더 쪼아줘" — 압박을 더
    달라는 신호.
  · "balanced": 사용자가 명시적으로 "그냥 보통으로" 같이 중간으로 돌려달라
    했을 때만. 단순히 잔소리에 답했다고 balanced로 바꾸지 마라.
  단순 잡담·짜증("아 짜증나")은 신호 아님. 명시적 의향이 있을 때만 잡아라.

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

        self._lock = threading.Lock()
        self._model_name = model_name
        self._calendar = calendar
        self._tools = build_tools(
            calendar, store, on_goal_set=self._on_goal_set
        )  # {name: Tool}
        self._chat = self._new_chat()

    # ===================================================== 세션 관리
    def _state_summary(self) -> Optional[str]:
        """재시작 후에도 모델이 현재 상태를 알도록 대화 앞에 끼울 요약."""
        parts: List[str] = []
        policy = self._store.nag_policy
        asked = self._store.nag_policy_asked
        # 항상 노출 — 모델이 잔소리 톤을 일관되게 맞추도록.
        parts.append(
            f"nag_policy: {policy}"
            + ("" if asked else " (첫 잔소리 직후 톤 조절 안내를 한 번 띄울 것)")
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
        events_line = self._upcoming_events_summary()
        if events_line:
            parts.append(events_line)
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
        """캘린더의 다가오는 일정을 상태 요약용 한 줄로 (없거나 실패 시 None)."""
        if self._calendar is None:
            return None
        try:
            events = self._calendar.list_upcoming(max_results=5)
        except Exception as exc:
            print(f"[CoachAgent] 일정 조회 실패 (무시): {exc}")
            return None
        if not events:
            return None
        joined = ", ".join(f"{e['start']} {e['summary']}" for e in events)
        return f"다가오는 캘린더 일정 — {joined}"

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

    def reset(self) -> None:
        """대화 기록·목표를 비우고 세션을 새로 시작한다 (/reset)."""
        with self._lock:
            self._store.clear_conversation()
            self._chat = self._new_chat()

    # ===================================================== 대화 처리
    def chat(self, user_text: str) -> str:
        """사용자가 텔레그램으로 보낸 메시지를 처리하고 코치 응답을 돌려준다."""
        with self._lock:
            return self._turn(user_text)

    def handle_event(self, description: str) -> str:
        """트래커가 감지한 딴짓 상황을 대화 흐름에 끼워 잔소리를 생성한다."""
        with self._lock:
            return self._turn(f"[자동 감지] {description}")

    def proactive_checkin(self) -> str:
        """사용자가 한동안 조용할 때, 코치가 먼저 말을 거는 메시지를 생성한다.
        안 끝낸 오늘 목표가 있으면 그걸 콕 집어 챙긴다."""
        with self._lock:
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
            prompt = (
                f"[자동 트리거] 사용자가 한참 답이 없어. {goal_line} "
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

    def overload_checkin(self) -> str:
        """잔소리가 누적해서 무시당하고 사용자가 한참 응답이 없을 때, 코치가
        '시스템 적합성' 점검을 자연스럽게 묻는 메시지를 생성한다. 자동으로 톤을
        바꾸지 않고 사용자에게 자기결정권을 돌려주는 게 핵심 — 우울증 자동
        진단을 피하면서도 자기인식 부재 케이스를 메꿈."""
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
        끌어내는 톤."""
        with self._lock:
            summary = self._store.weekly_summary(days=7)
            rec = summary["recent"]
            prev = summary["previous"]
            top = rec["top_trigger"] or "-"
            data_line = (
                f"목표 완료 {rec['goals_completed']}회 (지난주 "
                f"{prev['goals_completed']}), 습관 수행 {rec['habit_dones']}회 "
                f"(지난주 {prev['habit_dones']}), 잔소리 트리거 "
                f"{rec['trigger_total']}회 (지난주 {prev['trigger_total']}), "
                f"가장 자주 잡힌 패턴 '{top}'"
            )
            prompt = (
                "[자동 트리거] 주간 회고 시각이야 (일요일 저녁). 지난 7일 "
                f"활동 데이터: {data_line}.\n"
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
            response = self._client.models.generate_content(
                model=self._model_name, contents=contents, config=config
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
            response = self._client.models.generate_content(
                model=self._model_name, contents=prompt, config=config
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
                print(f"[CoachAgent] 잔소리 강도 변경: {policy_signal}")

        habit_done = field("habit_done")
        if habit_done:
            result = store.mark_habit_done(habit_done)
            if result:
                note = f"연속 {result['streak']}일"
                if result.get("leveled_up"):
                    note += f", 레벨업 → {result.get('level')}"
                print(f"[CoachAgent] 습관 수행: {habit_done} ({note})")

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
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        last_error: Optional[str] = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model_name, contents=user_msg, config=config
                )
                text = self._safe_text(response)
                if text:
                    return text
                last_error = "empty response"
            except Exception as exc:
                last_error = str(exc)
            print(
                f"[CoachAgent] {label} 실패 "
                f"(시도 {attempt}/{RETRY_ATTEMPTS}): {last_error}"
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
