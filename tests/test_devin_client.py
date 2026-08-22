from __future__ import annotations

from harness.devin_client import DevinClient


def _capture_requests(client: DevinClient, monkeypatch):
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"session_id": "devin-123"}

    monkeypatch.setattr(client, "_request", fake_request)
    return calls


def test_v3_uses_org_scoped_paths_and_current_body(monkeypatch):
    client = DevinClient(api_key="cog_test", org_id="org-test")
    calls = _capture_requests(client, monkeypatch)

    client.create_session(
        "work",
        structured_output_schema={"type": "object"},
        max_acu_limit=5,
        platform="linux",
        snapshot_id="legacy-is-ignored",
    )
    client.get_session("devin-123")
    client.send_message("devin-123", "continue")

    method, path, kwargs = calls[0]
    assert (method, path) == ("POST", "/organizations/org-test/sessions")
    assert kwargs["json"] == {
        "prompt": "work",
        "structured_output_schema": {"type": "object"},
        "structured_output_required": True,
        "max_acu_limit": 5,
        "platform": "linux",
    }
    assert calls[1][:2] == ("GET", "/organizations/org-test/sessions/devin-123")
    assert calls[2][:2] == (
        "POST",
        "/organizations/org-test/sessions/devin-123/messages",
    )


def test_v1_keeps_legacy_snapshot_and_paths(monkeypatch):
    client = DevinClient(api_key="apk_test", base_url="https://api.devin.ai/v1")
    calls = _capture_requests(client, monkeypatch)

    client.create_session("work", snapshot_id="snap-1")
    client.send_message("session-1", "continue")

    assert calls[0][1] == "/sessions"
    assert calls[0][2]["json"] == {
        "prompt": "work",
        "idempotent": True,
        "snapshot_id": "snap-1",
    }
    assert calls[1][1] == "/sessions/session-1/message"


def test_terminal_statuses_cover_v1_and_v3():
    assert DevinClient.is_terminal({"status_enum": "finished"})
    assert DevinClient.is_terminal({"status": "exit", "status_detail": "finished"})
    assert DevinClient.is_terminal({"status": "running", "status_detail": "waiting_for_user"})
    assert not DevinClient.is_terminal({"status": "running", "status_detail": "working"})
