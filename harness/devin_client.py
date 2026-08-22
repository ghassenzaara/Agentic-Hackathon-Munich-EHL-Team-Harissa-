"""Small Devin API client used by the smoke test and design loop.

The default is Devin's current organization-scoped v3 API. Legacy v1 remains
supported when ``DEVIN_API_BASE`` ends in ``/v1`` so an existing event key can
still be used while it is being migrated.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://api.devin.ai/v3"

_V1_TERMINAL = {"blocked", "expired", "finished"}
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
        self.is_v3 = self.base_url.endswith("/v3")
        self.org_id = org_id or os.environ.get("DEVIN_ORG_ID", "")
        if self.is_v3 and not self.org_id:
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
        if self.is_v3:
            return f"/organizations/{self.org_id}/sessions"
        return "/sessions"

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
        snapshot_id: str | None = None,
        idempotent: bool = True,
    ) -> dict:
        """Start a session using the fields supported by the selected API version."""
        platform = platform or os.environ.get("DEVIN_PLATFORM") or None
        snapshot_id = snapshot_id or os.environ.get("DEVIN_SNAPSHOT_ID") or None
        body: dict[str, Any] = {"prompt": prompt}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
            if self.is_v3:
                body["structured_output_required"] = True
        if max_acu_limit is not None:
            body["max_acu_limit"] = max_acu_limit

        if self.is_v3:
            if platform:
                body["platform"] = platform
        else:
            body["idempotent"] = idempotent
            if snapshot_id:
                body["snapshot_id"] = snapshot_id

        return self._request("POST", self.sessions_path, json=body)

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"{self.sessions_path}/{session_id}")

    def send_message(self, session_id: str, message: str) -> dict:
        suffix = "messages" if self.is_v3 else "message"
        return self._request(
            "POST", f"{self.sessions_path}/{session_id}/{suffix}", json={"message": message}
        )

    @staticmethod
    def is_terminal(payload: dict) -> bool:
        legacy = payload.get("status_enum")
        if legacy:
            return legacy in _V1_TERMINAL
        return (
            payload.get("status") in _V3_TERMINAL_STATUS
            or payload.get("status_detail") in _V3_TERMINAL_DETAIL
        )

    @staticmethod
    def status_label(payload: dict) -> str:
        status = payload.get("status_enum") or payload.get("status") or "unknown"
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
