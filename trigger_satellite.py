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
WEAK_SPOTS_FETCH_INTERVAL = 300.0   # 5분마다 백엔드의 학습된 약점 키워드 동기화


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
        # 백엔드에서 fetch 한 약점 키워드 캐시 — 5분마다 갱신.
        self._weak_spots: list = []
        self._weak_spots_last_fetch: float = 0.0
        self._tracker = Tracker(
            on_trigger=self._on_trigger,
            # 백엔드의 학습된 약점 키워드를 주기적으로 fetch 해서 매칭에 사용.
            get_weak_spots=lambda: list(self._weak_spots),
        )

    def _fetch_weak_spots(self) -> None:
        """백엔드 /weak_spots 에서 사용자 약점 키워드를 받아 캐시에 저장.
        실패해도 기존 캐시 유지 — 일시 장애로 매칭이 깨지지 않도록."""
        try:
            resp = self._session.get(
                f"{self._url}/weak_spots",
                headers={"Authorization": f"Bearer {self._secret}"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                print(f"[Satellite] /weak_spots fetch 실패: HTTP {resp.status_code}")
                return
            data = resp.json()
            items = data.get("weak_spots") or []
            if items != self._weak_spots:
                self._weak_spots = list(items)
                print(f"[Satellite] 약점 키워드 갱신: {self._weak_spots}")
            self._weak_spots_last_fetch = time.time()
        except Exception as exc:
            print(f"[Satellite] /weak_spots fetch 오류: {exc}")

    def run(self) -> None:
        self._tracker.start()
        print(f"[Satellite] PC 감시 시작 — 트리거 발사 → {self._url}/trigger")
        # 시작 즉시 한 번 fetch — 첫 매칭부터 백엔드 학습이 반영되도록.
        self._fetch_weak_spots()
        try:
            while True:
                time.sleep(60.0)
                if time.time() - self._weak_spots_last_fetch >= WEAK_SPOTS_FETCH_INTERVAL:
                    self._fetch_weak_spots()
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
            "device": "pc",
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
        # 도파민 trail 학습용 — 트리거 직전 sanitized 라벨 시퀀스. 백엔드가 자주
        # 등장하는 prefix 를 누적해 사용자에게 if-then plan 으로 권유한다.
        try:
            seq = self._tracker.get_recent_label_sequence(max_items=8)
            if seq:
                body["trail"] = seq
        except Exception as exc:
            print(f"[Satellite] trail 추출 실패: {exc}")

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
