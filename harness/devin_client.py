"""Small client for Devin's organization-scoped v3 API."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://api.devin.ai/v3"
DEFAULT_ACU_LIMIT = 5

_V3_TERMINAL_STATUS = {"error", "exit", "suspended"}
_V3_TERMINAL_DETAIL = {
    "error",
    "finished",
    "inactivity",
    "no_quota_allocation",
    "org_usage_limit_exceeded",
    "out_of_credits",
    "out_of_quota",
    "payment_declined",
    "total_session_limit_exceeded",
    "usage_limit_exceeded",
    "user_request",
    "waiting_for_approval",
    "waiting_for_user",
}


def load_env(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env without overwriting the real environment."""
    env_path = path or (REPO_ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class DevinError(RuntimeError):
    pass


class DevinClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        org_id: str | None = None,
    ):
        load_env()
        self.api_key = api_key or os.environ.get("DEVIN_API_KEY", "")
        if not self.api_key:
            raise DevinError(
                "DEVIN_API_KEY is not set. Copy .env.example to .env and add a "
                "Devin service-user key (cog_...)."
            )

        self.base_url = (
            base_url or os.environ.get("DEVIN_API_BASE") or DEFAULT_BASE_URL
        ).rstrip("/")
        if not self.base_url.endswith("/v3"):
            raise DevinError(
                "DEVIN_API_BASE must point to the v3 API (for example "
                "https://api.devin.ai/v3)."
            )
        self.org_id = org_id or os.environ.get("DEVIN_ORG_ID", "")
        if not self.org_id:
            raise DevinError(
                "DEVIN_ORG_ID is required by the v3 API. Find it under Devin "
                "Settings > Service users and add it to .env."
            )

        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        )

    @property
    def sessions_path(self) -> str:
        return f"/organizations/{self.org_id}/sessions"

    def _request(self, method: str, path: str, **kw: Any) -> dict:
        try:
            resp = self.session.request(method, f"{self.base_url}{path}", timeout=60, **kw)
            resp.raise_for_status()
        except requests.RequestException as exc:
            detail = ""
            if exc.response is not None:
                detail = f": {exc.response.text[:800]}"
            raise DevinError(f"{method} {path} failed{detail}") from exc
        return resp.json() if resp.content else {}

    def create_session(
        self,
        prompt: str,
        title: str | None = None,
        tags: list[str] | None = None,
        structured_output_schema: dict | None = None,
        max_acu_limit: int | None = None,
        platform: str | None = None,
    ) -> dict:
        """Start a session, requiring validated structured output when a schema is given."""
        platform = platform or os.environ.get("DEVIN_PLATFORM") or None
        body: dict[str, Any] = {"prompt": prompt}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
            body["structured_output_required"] = True
        if max_acu_limit is not None:
            body["max_acu_limit"] = max_acu_limit

        if platform:
            body["platform"] = platform

        return self._request("POST", self.sessions_path, json=body)

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"{self.sessions_path}/{session_id}")

    def send_message(self, session_id: str, message: str) -> dict:
        return self._request(
            "POST", f"{self.sessions_path}/{session_id}/messages", json={"message": message}
        )

    @staticmethod
    def is_terminal(payload: dict) -> bool:
        return (
            payload.get("status") in _V3_TERMINAL_STATUS
            or payload.get("status_detail") in _V3_TERMINAL_DETAIL
        )

    @staticmethod
    def status_label(payload: dict) -> str:
        status = payload.get("status") or "unknown"
        detail = payload.get("status_detail")
        return f"{status}/{detail}" if detail else str(status)

    def wait(
        self,
        session_id: str,
        timeout_s: int = 3600,
        poll_s: int = 15,
        on_poll: Callable[[dict], None] | None = None,
    ) -> dict:
        """Poll until the session finishes, pauses for input, or reaches its cap."""
        deadline = time.monotonic() + timeout_s
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.get_session(session_id)
            if on_poll:
                on_poll(last)
            if self.is_terminal(last):
                return last
            time.sleep(poll_s)
        last["_timed_out"] = True
        return last
