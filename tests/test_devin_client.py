from __future__ import annotations

import pytest

from harness.devin_client import DevinClient, DevinError


def _capture_requests(client: DevinClient, monkeypatch):
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"session_id": "devin-123"}

    monkeypatch.setattr(client, "_request", fake_request)
    return calls


def test_devin_mode_is_overridable_and_can_be_dropped(monkeypatch):
    client = DevinClient(api_key="cog_test", org_id="org-test")
    calls = _capture_requests(client, monkeypatch)

    monkeypatch.setenv("DEVIN_MODE", "lite")
    client.create_session("work")
    assert calls[-1][2]["json"]["devin_mode"] == "lite"

    client.create_session("work", devin_mode="ultra")
    assert calls[-1][2]["json"]["devin_mode"] == "ultra"

    monkeypatch.setenv("DEVIN_MODE", "")
    client.create_session("work")
    assert "devin_mode" not in calls[-1][2]["json"]


def test_v3_uses_org_scoped_paths_and_current_body(monkeypatch):
    client = DevinClient(api_key="cog_test", org_id="org-test")
    calls = _capture_requests(client, monkeypatch)

    client.create_session(
        "work",
        structured_output_schema={"type": "object"},
        max_acu_limit=5,
        platform="linux",
        repos=["https://github.com/example/autoimplants"],
    )
    client.list_sessions(limit=1)
    client.get_session("devin-123")
    client.send_message("devin-123", "continue")

    method, path, kwargs = calls[0]
    assert (method, path) == ("POST", "/organizations/org-test/sessions")
    assert kwargs["json"] == {
        "prompt": "work",
        "repos": ["https://github.com/example/autoimplants"],
        "structured_output_schema": {"type": "object"},
        "structured_output_required": True,
        "max_acu_limit": 5,
        "platform": "linux",
        "devin_mode": "fast",
    }
    assert calls[1] == (
        "GET",
        "/organizations/org-test/sessions",
        {"params": {"limit": 1}},
    )
    assert calls[2][:2] == ("GET", "/organizations/org-test/sessions/devin-123")
    assert calls[3][:2] == (
        "POST",
        "/organizations/org-test/sessions/devin-123/messages",
    )


def test_terminal_statuses_cover_v3():
    assert DevinClient.is_terminal({"status": "exit", "status_detail": "finished"})
    assert DevinClient.is_terminal({"status": "running", "status_detail": "waiting_for_user"})
    assert not DevinClient.is_terminal({"status": "running", "status_detail": "working"})


def test_legacy_api_base_is_rejected():
    with pytest.raises(DevinError, match="v3 API"):
        DevinClient(api_key="apk_test", base_url="https://api.devin.ai/v1", org_id="org-test")
