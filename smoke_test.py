"""smoke_test.py — google-genai 마이그레이션 라이브 검증 (임시 스크립트)."""
import os
import sys
import traceback

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from ai_engine import CoachAgent
from store import Store

TMP = "smoke_test_state.json"


def main() -> None:
    store = Store(TMP)
    agent = CoachAgent(store)  # genai.Client + Chat 세션

    print("=== [1] 일반 대화 + 프로필 추출 ===")
    r1 = agent.chat("안녕! 나 26살 개발자인데 요즘 일이 많아서 좀 피곤하네")
    print("코치:", r1)
    print("-> store.profile =", store.profile)

    print("\n=== [2] 단기 목표 추출 ===")
    r2 = agent.chat("오늘 할 일은 보고서 3페이지 초안 쓰는 거야.")
    print("코치:", r2)
    print("-> store.today_goals =", store.today_goals)

    print("\n=== [3] 다음 행동 쪼개기 (AFC: set_next_step) ===")
    r3 = agent.chat("좋아 시작하려는데 막막하네. 뭐부터 하면 돼?")
    print("코치:", r3)
    print("-> store.next_step =", store.next_step)

    print("\n=== [4] 자동 감지 이벤트 잔소리 (handle_event) ===")
    r4 = agent.handle_event(
        "사용자가 'YouTube' 화면을 12분째 입력도 없이 보고 있어 (도파민 좀비). "
        "참고로 오늘 목표는 '보고서 3페이지 초안 작성'."
    )
    print("코치:", r4)

    print("\n=== [5] 딴짓 판독 (models.generate_content) ===")
    verdict = agent.judge_distracted(
        "보고서 3페이지 초안 작성",
        {"web:youtube.com": 8, "web:instagram.com": 3, "web:reddit.com": 2},
    )
    print("-> judge_distracted =", verdict, "(True=딴짓이면 정상)")

    print("\n[OK] 라이브 스모크 테스트 전부 통과")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
    finally:
        for f in (TMP, f"{TMP}.tmp"):
            if os.path.exists(f):
                os.remove(f)
        print("[cleanup] 임시 상태 파일 삭제 완료")
