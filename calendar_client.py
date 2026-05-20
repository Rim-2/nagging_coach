"""
calendar_client.py — Google 캘린더 연동 (OAuth 데스크톱 흐름)

봇이 사용자의 기본 캘린더('primary')를 읽고 일정을 추가한다.

인증 파일:
    credentials.json  OAuth 데스크톱 클라이언트 (Google Cloud Console 발급).
    token.json        첫 인증 후 자동 생성·갱신되는 토큰 캐시. (비밀)

첫 인증은 calendar_setup.py 로 한 번만 (브라우저 동의). 이후로는 token.json
을 자동으로 갱신하므로 봇 실행 중에 다시 물어보지 않는다.
"""

import datetime
import os
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# events 읽기/쓰기에 필요한 최소 범위.
_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

_BASE = os.path.dirname(os.path.abspath(__file__))
_CREDENTIALS_PATH = os.path.join(_BASE, "credentials.json")
_TOKEN_PATH = os.path.join(_BASE, "token.json")
_CALENDAR_ID = "primary"  # OAuth 는 본인 계정으로 접근 → 기본 캘린더


class CalendarError(RuntimeError):
    """캘린더 인증·조회·쓰기 실패."""


class CalendarClient:
    """Google 캘린더 경량 클라이언트. 서비스는 처음 쓸 때 지연 생성한다."""

    def __init__(self) -> None:
        self._service = None

    # ----------------------------------------------------------- 인증
    def _ensure_service(self):
        if self._service is None:
            creds = self._load_credentials()
            try:
                self._service = build(
                    "calendar", "v3", credentials=creds,
                    cache_discovery=False,
                )
            except Exception as exc:
                raise CalendarError(f"캘린더 서비스 생성 실패: {exc}") from exc
        return self._service

    def _load_credentials(self) -> Credentials:
        creds: Optional[Credentials] = None
        if os.path.exists(_TOKEN_PATH):
            try:
                creds = Credentials.from_authorized_user_file(
                    _TOKEN_PATH, _SCOPES
                )
            except Exception:
                creds = None

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_token(creds)
                return creds
            except Exception as exc:
                print(f"[Calendar] 토큰 갱신 실패 — 재인증 필요: {exc}")
                creds = None

        # token.json 이 없거나 못 살릴 때만 브라우저 동의 흐름.
        if not os.path.exists(_CREDENTIALS_PATH):
            raise CalendarError(
                f"{_CREDENTIALS_PATH} 가 없어. Google Cloud 에서 받은 "
                "OAuth 데스크톱 클라이언트 JSON 을 그 경로에 둬줘."
            )
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                _CREDENTIALS_PATH, _SCOPES
            )
            creds = flow.run_local_server(port=0)
        except Exception as exc:
            raise CalendarError(f"OAuth 인증 실패: {exc}") from exc
        self._save_token(creds)
        return creds

    @staticmethod
    def _save_token(creds: Credentials) -> None:
        try:
            with open(_TOKEN_PATH, "w", encoding="utf-8") as fp:
                fp.write(creds.to_json())
        except Exception as exc:
            print(f"[Calendar] 토큰 저장 실패: {exc}")

    def has_token(self) -> bool:
        """이미 인증된 token.json 이 있는지 (브라우저 흐름 없이 쓸 수 있는지)."""
        return os.path.exists(_TOKEN_PATH)

    def authorize(self) -> None:
        """필요하면 인증 흐름을 돌려 token.json 을 만들어 둔다 (최초 1회)."""
        self._ensure_service()

    # -------------------------------------------------------- 공개 API
    def list_upcoming(self, max_results: int = 10) -> List[dict]:
        """지금 이후 다가오는 일정을 시간순으로 돌려준다.
        각 항목: {id, summary, start} (start 는 ISO 문자열)."""
        service = self._ensure_service()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            result = (
                service.events()
                .list(
                    calendarId=_CALENDAR_ID,
                    timeMin=now,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except Exception as exc:
            raise CalendarError(f"일정 조회 실패: {exc}") from exc

        events: List[dict] = []
        for item in result.get("items", []):
            start = item.get("start", {})
            events.append(
                {
                    "id": item.get("id"),
                    "summary": item.get("summary", "(제목 없음)"),
                    "start": start.get("dateTime") or start.get("date"),
                }
            )
        return events

    def add_event(
        self,
        summary: str,
        start: datetime.datetime,
        end: Optional[datetime.datetime] = None,
    ) -> dict:
        """일정을 추가한다. end 가 없으면 1시간짜리로 만든다.
        naive datetime 이면 PC 로컬 시간대로 간주한다."""
        service = self._ensure_service()
        if start.tzinfo is None:
            start = start.astimezone()
        if end is None:
            end = start + datetime.timedelta(hours=1)
        elif end.tzinfo is None:
            end = end.astimezone()

        body = {
            "summary": summary,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        try:
            created = (
                service.events()
                .insert(calendarId=_CALENDAR_ID, body=body)
                .execute()
            )
        except Exception as exc:
            raise CalendarError(f"일정 추가 실패: {exc}") from exc
        return {
            "id": created.get("id"),
            "summary": summary,
            "start": start.isoformat(),
            "link": created.get("htmlLink"),
        }
