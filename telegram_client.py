"""
telegram_client.py — 텔레그램 Bot API 경량 클라이언트

requests 기반 long-polling. 외부 비동기 프레임워크 없이
앱의 스레드 모델(트래커 데몬 스레드 + 폴링 스레드)에 그대로 얹는다.

사용법:
    client = TelegramClient(token)
    client.get_me()                     # 연결 확인
    client.start_polling(on_update)     # 폴링 스레드 시작
    client.send_message(chat_id, "안녕")
    client.stop()
"""

from __future__ import annotations

import threading
from typing import Callable, List, Optional

import requests


class TelegramClient:
    API_TEMPLATE = "https://api.telegram.org/bot{token}/{method}"
    FILE_TEMPLATE = "https://api.telegram.org/file/bot{token}/{path}"
    POLL_TIMEOUT = 50          # getUpdates long-poll 대기 시간(초)
    RETRY_DELAY = 3.0          # 폴링 오류 후 재시도 전 대기

    def __init__(self, token: str) -> None:
        self._token = token
        self._session = requests.Session()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------- 저수준 호출
    def _call(
        self, method: str, params: Optional[dict] = None, timeout: float = 15.0
    ):
        url = self.API_TEMPLATE.format(token=self._token, method=method)
        resp = self._session.post(url, json=params or {}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API 오류: {data.get('description')}")
        return data["result"]

    # --------------------------------------------------------- 공개 API
    def get_me(self) -> dict:
        return self._call("getMe")

    def send_message(self, chat_id: int, text: str) -> bool:
        try:
            self._call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            return True
        except Exception as exc:
            print(f"[Telegram] 메시지 전송 실패: {exc}")
            return False

    def download_file(self, file_id: str) -> bytes:
        """file_id 로 텔레그램 서버에서 파일(사진 등) 원본 바이트를 받아온다."""
        info = self._call("getFile", {"file_id": file_id})
        path = info.get("file_path")
        if not path:
            raise RuntimeError("getFile 응답에 file_path 가 없음")
        url = self.FILE_TEMPLATE.format(token=self._token, path=path)
        resp = self._session.get(url, timeout=30.0)
        resp.raise_for_status()
        return resp.content

    def start_polling(self, on_update: Callable[[dict], None]) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll,
            args=(on_update,),
            name="TelegramPoll",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 내부
    def _drain_backlog(self) -> Optional[int]:
        """앱이 꺼져 있던 동안 쌓인 메시지는 건너뛴다.
        마지막 update_id 다음 offset을 돌려줘 오래된 메시지 재처리를 막는다."""
        try:
            updates: List[dict] = self._call(
                "getUpdates", {"timeout": 0, "offset": -1}, timeout=15.0
            )
        except Exception as exc:
            print(f"[Telegram] 백로그 정리 실패 (무시): {exc}")
            return None
        if updates:
            return updates[-1]["update_id"] + 1
        return None

    def _poll(self, on_update: Callable[[dict], None]) -> None:
        offset = self._drain_backlog()
        print("[Telegram] 폴링 시작")
        while not self._stop.is_set():
            try:
                params = {
                    "timeout": self.POLL_TIMEOUT,
                    "allowed_updates": ["message"],
                }
                if offset is not None:
                    params["offset"] = offset
                updates: List[dict] = self._call(
                    "getUpdates", params, timeout=self.POLL_TIMEOUT + 10
                )
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        on_update(update)
                    except Exception as exc:
                        print(f"[Telegram] 업데이트 처리 오류: {exc}")
            except requests.exceptions.Timeout:
                continue
            except Exception as exc:
                if self._stop.is_set():
                    break
                print(f"[Telegram] 폴링 오류: {exc}")
                self._stop.wait(self.RETRY_DELAY)
        print("[Telegram] 폴링 종료")
