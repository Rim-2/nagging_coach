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

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """텔레그램 메시지 발송. reply_markup 으로 inline_keyboard 부착 가능.
        예: reply_markup={"inline_keyboard": [[{"text": "😊", "callback_data": "mood:4"}, ...]]}

        반환값 의미 (중복 발송 방지가 핵심):
          True  = 보냈음 *또는 보냈을 수도 있음* → 재시도하지 마라.
          False = *확실히 미도달* → 재시도 안전.

        텔레그램 sendMessage 는 멱등키가 없어서, 도달했는데 응답만 못 받은 걸
        '실패'로 보고 재시도하면 같은 메시지가 두 번 간다. 그래서 read timeout
        (요청은 보냈는데 응답 지연)처럼 *모호한* 경우는 True 로 간주해 재시도를
        막는다 (드물게 메시지 1건 유실 < 잦은 중복). 연결 자체 실패처럼 *확실히
        미도달* 인 경우만 False 로 재시도를 허용한다."""
        try:
            params = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                params["reply_markup"] = reply_markup
            self._call("sendMessage", params)
            return True
        except requests.exceptions.ConnectTimeout:
            # 연결 단계 타임아웃 = 요청이 안 나갔다 → 미도달 → 재시도 안전.
            print("[Telegram] 연결 타임아웃 (미도달) — 재시도 가능")
            return False
        except requests.exceptions.Timeout:
            # read timeout 등 = 요청은 나갔는데 응답을 못 받음 → 도달했을 수 있음.
            # 재시도하면 중복 위험 → '보낸 것'으로 간주.
            print("[Telegram] 응답 타임아웃 — 도달했을 수 있어 재시도 안 함 (중복 방지)")
            return True
        except Exception as exc:
            print(f"[Telegram] 메시지 전송 실패: {exc}")
            return False

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
    ) -> None:
        """사용자가 inline keyboard 버튼을 탭하면 텔레그램이 로딩 표시를 띄운다.
        이 호출이 그 로딩을 멈춤 + (옵션) toast 메시지로 짧은 확인 표시.
        실패해도 사용자 데이터엔 영향 없으므로 silent."""
        try:
            params = {"callback_query_id": callback_query_id}
            if text:
                params["text"] = text
            self._call("answerCallbackQuery", params, timeout=10.0)
        except Exception as exc:
            print(f"[Telegram] answerCallbackQuery 실패 (무시): {exc}")

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
                    "allowed_updates": ["message", "callback_query"],
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
