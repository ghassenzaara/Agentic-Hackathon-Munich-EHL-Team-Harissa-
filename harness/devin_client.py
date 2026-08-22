"""Thin wrapper over the Devin v1 API.

Endpoints (from docs.devin.ai, not guessed):
    POST /v1/sessions                       -> {session_id, url, is_new_session}
    GET  /v1/sessions/{session_id}          -> {status_enum, structured_output, pull_request, ...}
    POST /v1/sessions/{session_id}/message  -> {"message": "..."}

status_enum values we act on: working | blocked | finished

Deliberately dependency-light: requests plus a hand-rolled .env reader, so the
harness has no install step of its own.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://api.devin.ai/v1"

TERMINAL_STATUSES = ("blocked", "finished", "expired")


def load_env(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env into os.environ without clobbering real env vars."""
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
    def __init__(self, api_key: str | None = None, base_url: str = BASE_URL):
        load_env()
        self.api_key = api_key or os.environ.get("DEVIN_API_KEY", "")
        if not self.api_key:
            raise DevinError(
                "DEVIN_API_KEY is not set. Copy .env.example to .env and paste the key "
                "(apk_...). .env is gitignored."
            )
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, **kw: Any) -> dict:
        resp = self.session.request(method, f"{self.base_url}{path}", timeout=60, **kw)
        if resp.status_code >= 400:
            raise DevinError(f"{method} {path} -> {resp.status_code}: {resp.text[:800]}")
        return resp.json() if resp.content else {}

    # -- sessions --------------------------------------------------------------

    def create_session(
        self,
        prompt: str,
        title: str | None = None,
        tags: list[str] | None = None,
        structured_output_schema: dict | None = None,
        max_acu_limit: int | None = None,
        snapshot_id: str | None = None,
        idempotent: bool = True,
    ) -> dict:
        """Start a session. Returns {session_id, url, is_new_session}.

        ``max_acu_limit`` is a real cost guardrail -- set it before any unattended
        run. ``snapshot_id`` lets every session start from a machine that already
        has the CadQuery toolchain installed, which is the single biggest source of
        wasted minutes otherwise.
        """
        body: dict[str, Any] = {"prompt": prompt, "idempotent": idempotent}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
        if max_acu_limit is not None:
            body["max_acu_limit"] = max_acu_limit
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        return self._request("POST", "/sessions", json=body)

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{session_id}")

    def send_message(self, session_id: str, message: str) -> dict:
        return self._request("POST", f"/sessions/{session_id}/message", json={"message": message})

    def wait(
        self,
        session_id: str,
        timeout_s: int = 3600,
        poll_s: int = 15,
        on_poll: Callable[[dict], None] | None = None,
    ) -> dict:
        """Poll until the session reaches a terminal status or the timeout expires.

        Returns the last session payload either way -- a timeout is a fact for the
        caller to handle, not an exception, because a slow Devin run is normal and
        the loop may still want the partial result.
        """
        deadline = time.monotonic() + timeout_s
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.get_session(session_id)
            if on_poll:
                on_poll(last)
            if last.get("status_enum") in TERMINAL_STATUSES:
                return last
            time.sleep(poll_s)
        last["_timed_out"] = True
        return last
