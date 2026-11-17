"""Isolated Antidetect proxy contract; no launcher, operator state or network."""
import copy
import importlib.util
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import pytest
import requests


_source = Path(__file__).parent / "integration" / "antidetect" / "web" / "commenter.py"
_spec = importlib.util.spec_from_file_location("isolated_commenter_workspace", _source)
workspace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(workspace)
PAGE = "61570000000001"
SESSION = {"X-Test-Session": "synthetic-session"}
INJECTED_PRIVATE_VALUE = "synthetic-private-value-never-forward"


class Templates:
    def TemplateResponse(self, *, request, name, context, status_code=200):
        return HTMLResponse(name, status_code=status_code)


class MemoryResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        self.text = body.decode("utf-8")

    def json(self):
        return json.loads(self.text)


class MemoryRequest:
    def __init__(self, method, headers, body):
        self.method = method
        self.headers = headers
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _run_ready(coroutine):
    """Drive only completed awaitables; the tests must never create a socket/loop."""
    try:
        yielded = coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError(f"Unexpected asynchronous wait in isolated route test: {yielded!r}")


@pytest.fixture
def proxy():
    calls = []
    responses = {
        "/api/metrics": (200, {
            "metrics": {"paused_all": True, "sheet_sync": {
                "sheet_url": "https://docs.google.com/spreadsheets/d/synthetic/edit",
                "last_error": None, "private": INJECTED_PRIVATE_VALUE,
            }, "private": INJECTED_PRIVATE_VALUE},
            "task_counts": {"PENDING": 1, "NEED_CHECK": 1, "private": INJECTED_PRIVATE_VALUE},
            "total_pages": 1, "active_pages": 0, "private": INJECTED_PRIVATE_VALUE,
        }),
        "/api/pages": (200, {"pages": [{
            "page_id": PAGE, "page_name": "Browser fixture", "send_mode": "BROWSER",
            "has_page_token": False, "send_ready": True, "browser_ready": True,
            "profile_operations_today": 5, "profile_operations_limit": 30,
            "page_access_token_enc": INJECTED_PRIVATE_VALUE,
        }]}),
        "/api/tasks?limit=200": (200, {"tasks": [{
            "id": 17, "page_id": PAGE, "status": "NEED_CHECK",
            "comment_content": "Synthetic comment", "private": INJECTED_PRIVATE_VALUE,
        }]}),
    }

    def local_request(method, path, body=None):
        calls.append((method, path, copy.deepcopy(body)))
        result = responses.get(path, (200, {"success": True, "message": "Done"}))
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    application = FastAPI()
    workspace.register_commenter_routes(
        application, lambda request: request.headers.get("X-Test-Session") == SESSION["X-Test-Session"],
        Templates(), local_request,
    )

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def inline_gather(*awaitables):
        values = []
        for awaitable in awaitables:
            values.append(await awaitable)
        return values

    def request(method, path, *, auth=True, **kwargs):
        address = urlsplit(path)
        headers = dict(SESSION if auth else {})
        headers.update(kwargs.get("headers") or {})
        if "json" in kwargs:
            body = kwargs["json"]
        elif "content" in kwargs:
            try:
                body = json.loads(kwargs["content"])
            except ValueError as exc:
                body = exc
        else:
            body = None
        route_request = MemoryRequest(method, headers, body)
        route = next((item for item in application.routes
                      if getattr(item, "path", None) == address.path
                      or ("{" in getattr(item, "path", "") and re.fullmatch(
                          re.sub(r"\{([^}]+)\}", r"(?P<\1>[^/]+)", item.path), address.path))), None)
        if route is None:
            return MemoryResponse(404, b'{"detail":"Not Found"}')
        path_values = {}
        if "{" in route.path:
            match = re.fullmatch(re.sub(r"\{([^}]+)\}", r"(?P<\1>[^/]+)", route.path), address.path)
            path_values = match.groupdict()
        all_params = {key: values[-1] for key, values in parse_qs(address.query, keep_blank_values=True).items()}
        params = {}
        if route.path == "/api/commenter/tasks":
            params = {key: all_params[key] for key in ("status", "page_id") if key in all_params}
        original_to_thread, original_gather = workspace.asyncio.to_thread, workspace.asyncio.gather
        workspace.asyncio.to_thread, workspace.asyncio.gather = inline_to_thread, inline_gather
        try:
            result = _run_ready(route.endpoint(route_request, **path_values, **params))
        except HTTPException as exc:
            result = MemoryResponse(exc.status_code, json.dumps({"detail": exc.detail}).encode("utf-8"))
        finally:
            workspace.asyncio.to_thread, workspace.asyncio.gather = original_to_thread, original_gather
        if hasattr(result, "body") and isinstance(result.body, bytes):
            return MemoryResponse(result.status_code, result.body)
        return MemoryResponse(200, json.dumps(result).encode("utf-8"))

    return request, calls, responses


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/commenter/state", None),
    ("GET", "/api/commenter/tasks?status=NEED_CHECK&page_id=" + PAGE, None),
    ("GET", f"/api/commenter/pages/{PAGE}/preview", None),
    ("POST", f"/api/commenter/pages/{PAGE}/check", None),
    ("POST", f"/api/commenter/pages/{PAGE}/reactivate", None),
    ("POST", f"/api/commenter/pages/{PAGE}/resume_tasks", None),
    ("POST", f"/api/commenter/pages/{PAGE}/automation", {"enabled": True}),
    ("POST", f"/api/commenter/pages/{PAGE}/send_mode", {"send_mode": "BROWSER"}),
    ("POST", "/api/commenter/system/pause", {"paused": True}),
    ("POST", "/api/commenter/sheets/sync", {"sheet_url": "https://docs.google.com/spreadsheets/d/test"}),
    ("POST", "/api/commenter/tasks/17/cancel", None),
    ("POST", "/api/commenter/tasks/17/retry", {"confirmed_not_posted": True}),
    ("POST", "/api/commenter/tasks/17/resolve", {}),
])
def test_unauthorized_workspace_never_reaches_commenter(proxy, method, path, body):
    request, calls, _ = proxy
    response = request(method, path, auth=False, json=body)
    assert response.status_code == 401
    assert calls == []


def test_loading_workspace_does_not_fetch_or_change_runtime(proxy):
    request, calls, _ = proxy
    assert request("GET", "/commenter/").text == "commenter.html"
    response = request("GET", "/commenter/", auth=False)
    assert response.status_code == 401 and response.text == "unlock.html"
    assert calls == []


def test_refresh_reads_only_fixed_endpoints_and_public_projection(proxy):
    request, calls, _ = proxy
    response = request("GET", "/api/commenter/state?path=/api/sync")
    assert response.status_code == 200
    assert sorted(calls) == sorted([
        ("GET", "/api/metrics", None), ("GET", "/api/pages", None),
        ("GET", "/api/tasks?limit=200", None),
    ])
    assert INJECTED_PRIVATE_VALUE not in response.text
    data = response.json()
    assert data["overview"]["metrics"]["paused_all"] is True
    assert data["pages"][0]["send_ready"] is True
    assert data["pages"][0]["has_page_token"] is False
    assert data["pages"][0]["profile_operations_today"] == 5
    assert data["tasks"][0]["status"] == "NEED_CHECK"


@pytest.mark.parametrize("pause", [None, 0, 1, "false"])
def test_refresh_refuses_unknown_pause_instead_of_assuming_running(proxy, pause):
    request, _, responses = proxy
    responses["/api/metrics"][1]["metrics"]["paused_all"] = pause
    response = request("GET", "/api/commenter/state")
    assert response.status_code == 502
    assert INJECTED_PRIVATE_VALUE not in response.text


def test_filtered_task_query_uses_validated_status_and_page_id(proxy):
    request, calls, responses = proxy
    path = f"/api/tasks?limit=200&status=NEED_CHECK&page_id={PAGE}"
    responses[path] = (200, {"tasks": [{"id": 22, "page_id": PAGE, "status": "NEED_CHECK"}]})
    response = request("GET", f"/api/commenter/tasks?status=NEED_CHECK&page_id={PAGE}")
    assert response.status_code == 200
    assert response.json()["tasks"][0]["status"] == "NEED_CHECK"
    assert calls == [("GET", path, None)]


@pytest.mark.parametrize("query", [
    "status=NOT_A_STATUS", "status=NEED_CHECK%26status%3DPENDING",
    "page_id=abc12345", "page_id=12345%26limit%3D999999", "page_id=" + "1" * 31,
])
def test_task_query_rejects_invalid_filters_without_forwarding(proxy, query):
    request, calls, _ = proxy
    response = request("GET", "/api/commenter/tasks?" + query)
    assert response.status_code == 400
    assert calls == []


def test_partial_refresh_failure_is_not_reported_as_healthy(proxy):
    request, _, responses = proxy
    responses["/api/tasks?limit=200"] = (503, {"detail": INJECTED_PRIVATE_VALUE})
    response = request("GET", "/api/commenter/state")
    assert response.status_code == 502
    assert INJECTED_PRIVATE_VALUE not in response.text


@pytest.mark.parametrize("method,suffix", [
    ("GET", "check"), ("GET", "automation"), ("GET", "send_mode"),
    ("POST", "preview"), ("POST", "recent_posts"), ("POST", "rule"),
    ("POST", "delete"), ("POST", "sync"),
])
def test_page_proxy_rejects_unlisted_or_wrong_method_actions(proxy, method, suffix):
    request, calls, _ = proxy
    assert request(method, f"/api/commenter/pages/{PAGE}/{suffix}").status_code == 404
    assert calls == []


@pytest.mark.parametrize("page", ["1", "abc12345", "-12345", "1234%3Ffoo", "1" * 31])
def test_page_id_cannot_inject_upstream_path(proxy, page):
    request, calls, _ = proxy
    assert request("POST", f"/api/commenter/pages/{page}/check").status_code == 404
    assert calls == []


@pytest.mark.parametrize("action,body", [
    ("automation", {"enabled": "true"}), ("automation", {"enabled": 1}),
    ("automation", {"enabled": None}), ("automation", {}),
    ("automation", {"enabled": True, "path": "/api/sync"}),
    ("send_mode", {"send_mode": "AUTO"}), ("send_mode", {"send_mode": ["BROWSER"]}),
    ("send_mode", {"send_mode": "BROWSER", "enabled": True}),
])
def test_page_configuration_rejects_coercion_and_unexpected_fields(proxy, action, body):
    request, calls, _ = proxy
    assert request("POST", f"/api/commenter/pages/{PAGE}/{action}", json=body).status_code == 400
    assert calls == []


def test_malformed_json_is_rejected_before_dispatch(proxy):
    request, calls, _ = proxy
    response = request("POST", "/api/commenter/system/pause", content="{broken", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize("action,body", [
    ("check", None), ("reactivate", None), ("resume_tasks", None),
    ("automation", {"enabled": False}), ("automation", {"enabled": True}),
    ("send_mode", {"send_mode": "BROWSER"}), ("send_mode", {"send_mode": "GRAPH_API"}),
])
def test_explicit_page_action_forwards_only_exact_operation(proxy, action, body):
    request, calls, responses = proxy
    path = f"/api/pages/{PAGE}/{action}"
    responses[path] = (200, {"success": True, "private": INJECTED_PRIVATE_VALUE})
    response = request("POST", f"/api/commenter/pages/{PAGE}/{action}", json=body)
    assert response.status_code == 200
    assert calls == [("POST", path, body)]
    assert INJECTED_PRIVATE_VALUE not in response.text


def test_preview_returns_sequence_without_unrelated_data(proxy):
    request, calls, responses = proxy
    sequence = [{"template": "Synthetic step", "delay_minutes": 10}]
    responses[f"/api/pages/{PAGE}/preview"] = (200, {
        "templates": [], "comment_sequence": sequence, "delay_minutes": 10,
        "source_error": None, "private": INJECTED_PRIVATE_VALUE,
    })
    response = request("GET", f"/api/commenter/pages/{PAGE}/preview")
    assert response.status_code == 200
    assert response.json()["comment_sequence"] == sequence
    assert calls == [("GET", f"/api/pages/{PAGE}/preview", None)]
    assert INJECTED_PRIVATE_VALUE not in response.text


@pytest.mark.parametrize("requested,actual", [(True, True), (False, False), (False, True)])
def test_pause_returns_actual_backend_state_including_concurrent_stop(proxy, requested, actual):
    request, calls, responses = proxy
    responses["/api/system/pause"] = (200, {"success": True, "paused": actual})
    response = request("POST", "/api/commenter/system/pause", json={"paused": requested})
    assert response.status_code == 200
    assert response.json()["paused"] is actual
    assert calls == [("POST", "/api/system/pause", {"paused": requested})]


@pytest.mark.parametrize("value", [0, "false", None, [], {}])
def test_pause_requires_strict_boolean(proxy, value):
    request, calls, _ = proxy
    assert request("POST", "/api/commenter/system/pause", json={"paused": value}).status_code == 400
    assert calls == []


@pytest.mark.parametrize("url", [
    "http://docs.google.com/spreadsheets/d/synthetic", "https://example.com/spreadsheets/d/synthetic",
    "https://docs.google.com.evil.test/spreadsheets/d/synthetic", "https://docs.google.com@evil.test/spreadsheets/d/synthetic",
    "https://docs.google.com:443/spreadsheets/d/synthetic", "https://docs.google.com/spreadsheets/d/",
    "https://[malformed/spreadsheets/d/synthetic",
    "file:///spreadsheets/d/synthetic", "https://docs.google.com/not-a-sheet", None,
])
def test_sheet_proxy_does_not_accept_arbitrary_urls(proxy, url):
    request, calls, _ = proxy
    assert request("POST", "/api/commenter/sheets/sync", json={"sheet_url": url}).status_code == 400
    assert calls == []


def test_sheet_sync_is_explicit_and_trims_url(proxy):
    request, calls, _ = proxy
    url = "https://docs.google.com/spreadsheets/d/synthetic/edit?gid=123#gid=123"
    response = request("POST", "/api/commenter/sheets/sync", json={"sheet_url": "  " + url + "  "})
    assert response.status_code == 200
    assert calls == [("POST", "/api/sheets/sync", {"sheet_url": url})]


@pytest.mark.parametrize("task,action", [("0", "cancel"), ("-1", "cancel"), ("x17", "cancel"),
    ("1" * 20, "cancel"), ("17", "delete"), ("17", "send")])
def test_task_proxy_accepts_only_specific_task_actions(proxy, task, action):
    request, calls, _ = proxy
    assert request("POST", f"/api/commenter/tasks/{task}/{action}").status_code == 404
    assert calls == []


@pytest.mark.parametrize("body", [{}, {"confirmed_not_posted": "true"},
    {"confirmed_not_posted": 1}, {"confirmed_not_posted": True, "status": "PENDING"}])
def test_retry_requires_explicit_boolean_confirmation(proxy, body):
    request, calls, _ = proxy
    assert request("POST", "/api/commenter/tasks/17/retry", json=body).status_code == 400
    assert calls == []


@pytest.mark.parametrize("confirmed", [True, False])
def test_retry_preserves_operator_confirmation(proxy, confirmed):
    request, calls, _ = proxy
    response = request("POST", "/api/commenter/tasks/17/retry", json={"confirmed_not_posted": confirmed})
    assert response.status_code == 200
    assert calls == [("POST", "/api/tasks/17/retry", {"confirmed_not_posted": confirmed})]


@pytest.mark.parametrize("comment_id", [None, "123456789", "123456789_987654321"])
def test_resolve_forwards_only_optional_comment_identifier(proxy, comment_id):
    request, calls, _ = proxy
    body = {"comment_id": comment_id}
    response = request("POST", "/api/commenter/tasks/17/resolve", json=body)
    assert response.status_code == 200
    assert calls == [("POST", "/api/tasks/17/resolve", body)]


@pytest.mark.parametrize("body", [{"comment_id": "https://example.test"}, {"comment_id": 123},
    {"comment_id": "123\n456"}, {"comment_id": "1" * 41}, {"status": "SUCCESS"}])
def test_resolve_rejects_urls_status_override_and_invalid_identifier(proxy, body):
    request, calls, _ = proxy
    assert request("POST", "/api/commenter/tasks/17/resolve", json=body).status_code == 400
    assert calls == []


@pytest.mark.parametrize("error", [OSError(INJECTED_PRIVATE_VALUE), ValueError(INJECTED_PRIVATE_VALUE),
    requests.RequestException(INJECTED_PRIVATE_VALUE)])
def test_backend_transport_errors_do_not_expose_exception_details(proxy, error):
    request, _, responses = proxy
    responses[f"/api/pages/{PAGE}/check"] = error
    response = request("POST", f"/api/commenter/pages/{PAGE}/check")
    assert response.status_code == 502
    assert INJECTED_PRIVATE_VALUE not in response.text


def test_backend_validation_error_keeps_status_and_public_message_only(proxy):
    request, _, responses = proxy
    responses["/api/tasks/17/retry"] = (409, {
        "detail": "Task needs review", "private": INJECTED_PRIVATE_VALUE,
    })
    response = request("POST", "/api/commenter/tasks/17/retry", json={"confirmed_not_posted": False})
    assert response.status_code == 409
    assert response.json() == {"detail": "Task needs review"}
