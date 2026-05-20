"""calendar_setup.py — Google 캘린더 최초 인증 + 연결 확인 (한 번만 실행).

실행하면 브라우저가 열린다. Google 계정으로 '허용'을 누르면 token.json 이
저장되고, 이후로는 봇이 알아서 캘린더에 접근한다. 끝으로 다가오는 일정을
출력해 연결이 됐는지 보여준다.
"""
import sys
import traceback

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from calendar_client import CalendarClient, CalendarError


def main() -> None:
    client = CalendarClient()
    if client.has_token():
        print("[i] 이미 token.json 이 있어 — 인증 건너뜀.")
    else:
        print("[i] 브라우저가 열리면 Google 계정으로 '허용'을 눌러줘...")

    try:
        client.authorize()
        events = client.list_upcoming(max_results=10)
    except CalendarError as exc:
        print(f"\n[실패] {exc}")
        return

    print("\n[성공] 캘린더 연결됨. 다가오는 일정:")
    if not events:
        print("  (다가오는 일정 없음)")
    for e in events:
        print(f"  - {e['start']}  {e['summary']}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
