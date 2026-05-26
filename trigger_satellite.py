"""trigger_satellite.py — 로컬 PC 트래커 위성.

같은 봇 토큰으로 두 인스턴스가 텔레그램 폴링을 동시에 하면 409 충돌이
나기 때문에, '클라우드 24/7 봇 + 로컬 PC 감시' 를 같이 운영하려면 로컬은
텔레그램을 전혀 건드리지 않고 트리거만 Railway 로 쏴야 한다.

이 진입점은 PC 감시(Tracker) 만 띄우고, 트리거가 잡히면 Railway 의
POST /trigger 엔드포인트로 HTTP 통보한다. Railway 봇이 자기 AI 로
잔소리를 만들어 자기 텔레그램으로 보낸다. 모든 상태(state.json) 는
Railway 한 곳에서 일관되게 관리.

실행 전 .env 또는 환경변수:
    NAGGING_COACH_URL   Railway 공개 도메인 (예: https://xxx.up.railway.app)
    TRIGGER_SECRET      Railway 와 공유하는 인증 토큰 (Bearer)

사용법:
    python trigger_satellite.py
"""

from __future__ import annotations

import os
import sys
import time

import requests
from dotenv import load_dotenv

from tracker import Snapshot, Tracker, TriggerType

load_dotenv()

# Windows 콘솔(cp949) 에서 한글이 깨지지 않도록 UTF-8 로 전환.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

URL = os.getenv("NAGGING_COACH_URL", "").rstrip("/")
SECRET = os.getenv("TRIGGER_SECRET", "")
POST_TIMEOUT = 20.0


class Satellite:
    """PC 트래커 위성. Tracker 가 트리거를 보내면 Railway 로 HTTP POST."""

    def __init__(self, url: str, secret: str) -> None:
        if not url or not secret:
            raise RuntimeError(
                "NAGGING_COACH_URL / TRIGGER_SECRET 가 둘 다 필요해. "
                "Railway 의 public 도메인과 같은 시크릿을 .env 에 넣어줘."
            )
        self._url = url
        self._secret = secret
        self._session = requests.Session()
        self._tracker = Tracker(
            on_trigger=self._on_trigger,
            # 약점 키워드 동기화는 추후 GET 엔드포인트로 — 일단 비움.
            get_weak_spots=lambda: [],
        )

    def run(self) -> None:
        self._tracker.start()
        print(f"[Satellite] PC 감시 시작 — 트리거 발사 → {self._url}/trigger")
        try:
            while True:
                time.sleep(60.0)
        except KeyboardInterrupt:
            print("\n[Satellite] 종료 중…")
        finally:
            self._tracker.stop()

    def _on_trigger(self, trigger: TriggerType, snap: Snapshot) -> None:
        """Tracker 콜백. POST 발사 후 어떤 결과든 즉시 resume_normal 로 다음
        감시 사이클 진입 — Tracker.resume_normal 안에 60초 쿨다운이 있어서
        잔소리가 연발되는 건 자동으로 막힌다."""
        body = {
            "trigger": trigger.value,
            "snapshot": {
                "active_window": snap.active_window,
                "idle_time": snap.idle_time,
                "switch_count": snap.switch_count,
            },
        }
        # 산만함 트리거는 Railway 가 LLM 으로 한 번 더 판독 — freq 동봉.
        if trigger == TriggerType.DISTRACTED_SWITCHING:
            try:
                body["window_freq"] = self._tracker.get_recent_window_freq()
            except Exception as exc:
                print(f"[Satellite] window_freq 추출 실패: {exc}")

        try:
            resp = self._session.post(
                f"{self._url}/trigger",
                json=body,
                headers={"Authorization": f"Bearer {self._secret}"},
                timeout=POST_TIMEOUT,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text[:200]}
                print(f"[Satellite] 트리거 [{trigger.value}] → {data}")
            else:
                print(
                    f"[Satellite] 트리거 발송 실패: HTTP {resp.status_code} — "
                    f"{resp.text[:200]}"
                )
        except Exception as exc:
            print(f"[Satellite] 트리거 발송 실패: {exc}")
        finally:
            self._tracker.resume_normal()


def main() -> None:
    try:
        Satellite(URL, SECRET).run()
    except Exception as exc:
        print(f"[Satellite] 시작 실패: {exc}")


if __name__ == "__main__":
    main()
