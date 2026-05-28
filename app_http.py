"""app_http — 원격 트리거 HTTP 핸들러 분리.

app.py 가 1500줄+ god class 가 되어 책임별로 모듈을 떼어내는 과정에서
이 파일이 가장 명확히 독립된 첫 조각. CoachApp 자체와는 *팩토리 주입*
방식으로만 연결되어 양방향 import 없음.

엔드포인트:
  POST /trigger     — PC/폰 위성이 보낸 트리거 (Bearer 인증)
  GET  /health      — 외부 헬스체크 (인증 없음)
  GET  /weak_spots  — 위성이 학습된 약점 키워드를 받아가는 동기화 (Bearer 인증)
"""

from __future__ import annotations

import http.server
import json
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover — 타입 힌트 전용
    from app import CoachApp


# 폰 위성이 보내는 Android 패키지명을 사람이 읽는 라벨로. 매핑에 없으면 그대로.
# (PackageManager.getApplicationLabel 우선 — 매핑은 fallback 이지만 호환성 위해 유지.)
PHONE_APP_LABELS = {
    "com.instagram.android": "인스타그램",
    "com.google.android.youtube": "유튜브",
    "com.zhiliaoapp.musically": "틱톡",
    "com.ss.android.ugc.trill": "틱톡",
    "com.facebook.katana": "페이스북",
    "com.twitter.android": "X(트위터)",
    "com.reddit.frontpage": "레딧",
    "com.snapchat.android": "스냅챗",
    "com.kakao.talk": "카카오톡",
    "com.discord": "디스코드",
    "com.nhn.android.band": "밴드",
    "com.linecorp.linelite": "라인",
    "phone-screen": "휴대폰",
}


class TriggerHTTPHandler(http.server.BaseHTTPRequestHandler):
    """원격 위성에서 보내는 트리거를 받는 미니 HTTP 핸들러.
    Bearer 토큰으로 인증하고, 검증된 요청만 CoachApp.handle_remote_trigger 로 위임."""

    # 서브클래스 팩토리에서 주입된다 (_start_http_server).
    coach_app: Optional["CoachApp"] = None
    trigger_secret: str = ""

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 인터페이스
        if self.path != "/trigger":
            self._respond(404, {"ok": False, "error": "not found"})
            return
        if not self.trigger_secret:
            self._respond(503, {"ok": False, "error": "secret not configured"})
            return
        if self.headers.get("Authorization", "") != f"Bearer {self.trigger_secret}":
            self._respond(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            self._respond(400, {"ok": False, "error": f"bad json: {exc}"})
            return
        try:
            result = self.coach_app.handle_remote_trigger(body)
        except Exception as exc:
            print(f"[App] 원격 트리거 처리 오류: {exc}")
            self._respond(500, {"ok": False, "error": "internal"})
            return
        self._respond(200, result)

    def do_GET(self) -> None:  # noqa: N802
        # 외부에서 curl 로 살아있는지 찔러볼 수 있는 헬스체크용 엔드포인트.
        if self.path in ("/", "/health"):
            self._respond(200, {"ok": True, "service": "nagging_coach"})
            return
        # 위성이 백엔드의 학습된 약점 키워드를 주기적으로 fetch — Bearer 인증 필수.
        if self.path == "/weak_spots":
            if not self.trigger_secret:
                self._respond(503, {"ok": False, "error": "secret not configured"})
                return
            if self.headers.get("Authorization", "") != f"Bearer {self.trigger_secret}":
                self._respond(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                items = list(self.coach_app._store.weak_spots)
            except Exception as exc:
                print(f"[App] /weak_spots 조회 오류: {exc}")
                self._respond(500, {"ok": False, "error": "internal"})
                return
            self._respond(200, {"ok": True, "weak_spots": items})
            return
        # 폰 위성이 매칭에 사용할 좌표 목록 — Bearer 인증 필수.
        if self.path == "/places":
            if not self.trigger_secret:
                self._respond(503, {"ok": False, "error": "secret not configured"})
                return
            if self.headers.get("Authorization", "") != f"Bearer {self.trigger_secret}":
                self._respond(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                items = self.coach_app._store.places
            except Exception as exc:
                print(f"[App] /places 조회 오류: {exc}")
                self._respond(500, {"ok": False, "error": "internal"})
                return
            self._respond(200, {"ok": True, "places": items})
            return
        self._respond(404, {"ok": False, "error": "not found"})

    def log_message(self, format, *args) -> None:  # noqa: A002
        # 기본 access log 는 stderr 로 시끄러움 — 우리 print 만 남긴다.
        return

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HttpServerMixin:
    """CoachApp 에 mix-in 되어 HTTP 서버 띄우는 책임만 담는다.
    의존: self._http_port (또는 PORT env), self.handle_remote_trigger, self._store.
    """

    def _start_http_server(self) -> None:
        """원격 트리거 HTTP 서버를 데몬 스레드로 띄운다. TRIGGER_SECRET 이
        설정돼 있을 때만 활성 — 시크릿 없이 띄우면 누구나 트리거를 발사할 수
        있게 되므로 보안상 차단."""
        import os
        import threading

        secret = os.getenv("TRIGGER_SECRET", "")
        if not secret:
            print("[App] TRIGGER_SECRET 미설정 — 원격 트리거 HTTP 서버 비활성")
            return
        port = int(os.getenv("PORT", "8080"))

        # 핸들러 클래스에 인스턴스 참조·시크릿 주입 (서브클래스 팩토리)
        handler_cls = type(
            "BoundTriggerHTTPHandler",
            (TriggerHTTPHandler,),
            {"coach_app": self, "trigger_secret": secret},
        )
        try:
            server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
        except Exception as exc:
            print(f"[App] HTTP 서버 시작 실패 (포트 {port}): {exc}")
            return
        threading.Thread(
            target=server.serve_forever, name="TriggerHTTP", daemon=True
        ).start()
        print(f"[App] 원격 트리거 HTTP 서버 시작 — 포트 {port}, POST /trigger")
