"""Browser contracts, entirely simulated: never launch a browser or send to Facebook."""
import copy
import json
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

import antidetect_client as browser


CANONICAL = "993921657144854"
ALIAS = "61575475678031"
POST = "100001234567890"
PROFILE = "profile-a"
COMMENT = "900001234567890"
MESSAGE = "Màu sắc đẹp quá!"
URL = f"https://www.facebook.com/{ALIAS}/posts/{POST}"


@pytest.fixture
def harness(monkeypatch):
    page = {"page_id": CANONICAL, "fb_profile_id": ALIAS, "page_name": "Page mẫu",
            "antidetect_profile_id": PROFILE, "page_url": f"https://www.facebook.com/{ALIAS}"}
    metadata = [{"id": PROFILE, "facebook_pages": [{"id": ALIAS, "name": "Page mẫu"}]}]
    state = {"url": URL, "accessState": "ok", "actorIds": [ALIAS], "actorOptions": [],
             "posts": [{"id": POST, "ownerIds": [ALIAS], "url": URL,
                        "created_time": "2026-11-15T08:00:00+00:00"}],
             "commentBoxRect": {"x": 100, "y": 200}, "commentIds": [], "comments": []}
    h = SimpleNamespace(page=page, metadata=metadata, state=state, calls=[], opened=[], closed=[], released=[],
                        draft="", focused=False, submitted=False, after_comments=None,
                        fail_method=None, fail_after_insert=None, before_eval=None, opened_by_us=False,
                        readiness=[], readiness_reads=0)

    class FakeConnection:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc):
            return False

    def acquire(profile):
        h.opened.append(profile)
        return 9222, h.opened_by_us, 8000 if h.opened_by_us else None

    async def call(ws, msg_id, method, params=None, timeout=10):
        h.calls.append((method, params))
        if method == h.fail_method:
            raise TimeoutError("simulated transport failure")
        if method == "Input.dispatchMouseEvent":
            h.focused = True
        if method == "Input.insertText":
            h.draft = params["text"]
            if h.fail_after_insert:
                h.fail_after_insert()
        if method == "Input.dispatchKeyEvent" and params["type"] == "keyDown":
            h.submitted = True
            h.draft = ""
        return {"id": msg_id, "result": {}}

    async def evaluate(ws, msg_id, expression, timeout=10):
        if expression == browser._BROWSER_READINESS_JS:
            h.readiness_reads += 1
            if h.readiness:
                result = h.readiness[0]
                if len(h.readiness) > 1:
                    h.readiness.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            return {"url": h.state["url"], "readyState": "complete", "mainPresent": True}
        if h.before_eval:
            h.before_eval()
        result = copy.deepcopy(h.state)
        result["commentBoxText"] = h.draft
        result["focused"] = h.focused
        if h.submitted:
            result["comments"] = h.after_comments if h.after_comments is not None else [
                {"id": COMMENT, "authorIds": [ALIAS], "text": MESSAGE}]
            result["commentIds"] = [c["id"] for c in result["comments"] if c.get("id")]
        return result

    async def no_sleep(*args, **kwargs):
        pass

    monkeypatch.setattr(browser, "_read_profiles_readonly", lambda: h.metadata)
    monkeypatch.setattr(browser, "_PROFILE_OPERATION_LOCKS", {})
    monkeypatch.setattr(browser, "_OWNED_PROFILE_LEASES", {})
    monkeypatch.setattr(browser, "acquire_profile_cdp_port", acquire)
    monkeypatch.setattr(browser, "_open_transient_tab", lambda port: ("owned-tab", "ws://mock"))
    monkeypatch.setattr(browser, "_close_transient_tab", lambda *args: h.closed.append(args))
    monkeypatch.setattr(browser, "release_profile_cdp_port", lambda *args: h.released.append(args))
    monkeypatch.setattr(browser, "_cdp_call", call)
    monkeypatch.setattr(browser, "_cdp_eval", evaluate)
    monkeypatch.setattr(browser.asyncio, "sleep", no_sleep)
    monkeypatch.setattr("websockets.connect", lambda *args, **kwargs: FakeConnection())
    return h


def send(h, **kwargs):
    return browser.send_page_comment_cdp(h.page, f"{CANONICAL}_{POST}", MESSAGE, post_url=URL, **kwargs)


def enters(h):
    return [p for method, p in h.calls if method == "Input.dispatchKeyEvent" and p["type"] == "keyDown"]


def test_success_requires_observed_real_comment_id_and_releases_only_owned_tab(harness):
    h = harness
    result = send(h)
    assert result["success"] is True
    assert result["comment_id"] == COMMENT
    assert result["category"] == "SUCCESS"
    assert len(enters(h)) == 1
    assert h.closed == [(9222, "owned-tab")]
    assert h.released == [(PROFILE, False, None)]


def test_loading_document_and_react_shell_wait_before_actor_or_input(harness):
    harness.readiness = [
        {"url": "about:blank", "readyState": "complete", "mainPresent": False},
        {"url": URL, "readyState": "loading", "mainPresent": False},
        {"url": URL, "readyState": "complete", "mainPresent": False},
        {"url": URL, "readyState": "complete", "mainPresent": True},
    ]
    assert send(harness)["success"]
    assert harness.readiness_reads == 4
    assert len(enters(harness)) == 1


def test_loading_timeout_never_types_and_cleans_up_owned_tab(harness):
    harness.readiness = [{"url": URL, "readyState": "complete", "mainPresent": False}]
    result = send(harness)
    assert result["category"] == "NETWORK_RETRYABLE"
    assert harness.readiness_reads == 20
    assert harness.draft == "" and enters(harness) == []
    assert harness.closed == [(9222, "owned-tab")]
    assert harness.released == [(PROFILE, False, None)]


@pytest.mark.parametrize("error", [
    "Execution context was destroyed.", "Cannot find context with specified id",
])
def test_navigation_context_replacement_is_retried_before_input(harness, error):
    harness.readiness = [RuntimeError("CDP Runtime.evaluate failed: " + error),
                         {"url": URL, "readyState": "complete", "mainPresent": True}]
    assert send(harness)["success"]
    assert harness.readiness_reads == 2
    assert len(enters(harness)) == 1


def test_persistent_navigation_context_errors_stop_with_cleanup(harness):
    harness.readiness = [RuntimeError("CDP Runtime.evaluate failed: Execution context was destroyed.")]
    assert send(harness)["category"] == "NETWORK_RETRYABLE"
    assert harness.readiness_reads == 20
    assert harness.draft == "" and enters(harness) == []
    assert harness.closed == [(9222, "owned-tab")]


def test_non_navigation_protocol_error_is_not_retried(harness):
    harness.readiness = [RuntimeError("CDP Runtime.evaluate failed: Unsupported operation")]
    assert not send(harness)["success"]
    assert harness.readiness_reads == 1
    assert harness.draft == "" and enters(harness) == []
    assert harness.closed == [(9222, "owned-tab")]


@pytest.mark.parametrize("field,status", [("loginPresent", "LOGIN_REQUIRED"), ("checkpointPresent", "CHECKPOINT")])
def test_auth_page_during_navigation_stops_without_waiting_or_input(harness, field, status):
    harness.readiness = [{"url": URL, "readyState": "loading", "mainPresent": False, field: True}]
    result = send(harness)
    assert result["browser_status"] == status
    assert harness.readiness_reads == 1
    assert enters(harness) == []


def test_navigation_to_non_facebook_domain_stops_before_input(harness):
    harness.readiness = [{"url": "https://outside.invalid/", "readyState": "complete", "mainPresent": True}]
    assert send(harness)["category"] == "INVALID_TARGET"
    assert harness.readiness_reads == 1
    assert enters(harness) == []


@pytest.mark.parametrize("identity", [ALIAS, CANONICAL])
def test_connection_accepts_verified_alias_and_returns_configured_identity(harness, identity):
    harness.state["actorIds"] = [identity]
    result = browser.verify_page_connection_cdp(harness.page)
    assert result["success"] is True
    assert result["browser_status"] == "READY"
    assert result["identity_id"] == ALIAS
    assert result["observed_identity_ids"] == [identity]


@pytest.mark.parametrize("actor_ids", [[], ["12345"], [ALIAS, "12345"]])
def test_name_only_or_wrong_numeric_actor_never_types(harness, actor_ids):
    harness.state["actorIds"] = actor_ids
    harness.state["actorNames"] = ["Page mẫu"]
    result = send(harness)
    assert result["category"] == "INVALID_TARGET"
    assert not harness.draft
    assert enters(harness) == []


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong_page"])
def test_mapping_must_be_unique_before_launch(harness, change):
    if change == "missing":
        harness.metadata.clear()
    elif change == "duplicate":
        harness.metadata.append(copy.deepcopy(harness.metadata[0]))
    else:
        harness.metadata[0]["facebook_pages"][0]["id"] = "12345"
    result = send(harness)
    assert result["category"] == "INVALID_TARGET"
    assert harness.opened == []


@pytest.mark.parametrize("url", [
    "https://evil.invalid/61575475678031/posts/100001234567890",
    "http://www.facebook.com/61575475678031/posts/100001234567890",
    "https://www.facebook.com.evil.invalid/61575475678031/posts/100001234567890",
    "https://person:pass@www.facebook.com/61575475678031/posts/100001234567890",
    "https://www.facebook.com:444/61575475678031/posts/100001234567890",
    "https://www.facebook.com/61575475678031/posts/99999",
    "https://www.facebook.com/77777/posts/100001234567890",
    "https://www.facebook.com/61575475678031/posts/100001234567890?comment_id=555",
])
def test_untrusted_or_mismatched_target_never_launches(harness, url):
    result = browser.send_page_comment_cdp(harness.page, POST, MESSAGE, post_url=url)
    assert result["category"] == "INVALID_TARGET"
    assert harness.opened == []


@pytest.mark.parametrize("field,value", [
    ("url", "https://www.facebook.com/61575475678031/posts/99999"),
    ("posts", []),
    ("posts", [{"id": POST, "ownerIds": ["77777"], "url": URL}]),
    ("posts", [{"id": POST, "ownerIds": [], "url": URL}]),
])
def test_wrong_rendered_post_or_owner_blocks_before_typing(harness, field, value):
    harness.state[field] = value
    result = send(harness)
    assert result["category"] == "INVALID_TARGET"
    assert not harness.draft
    assert enters(harness) == []


def test_before_submit_cancel_runs_after_typing_and_prevents_enter(harness):
    calls = []

    def cancel():
        calls.append(harness.draft)
        return False

    result = send(harness, before_submit=cancel)
    assert calls == [MESSAGE]
    assert result["category"] == "CANCELLED_BEFORE_SEND"
    assert enters(harness) == []


def test_mapping_change_after_typing_prevents_enter(harness):
    harness.fail_after_insert = lambda: harness.metadata.clear()
    result = send(harness)
    assert result["category"] == "INVALID_TARGET"
    assert enters(harness) == []


def test_mapping_change_inside_final_callback_prevents_enter(harness):
    def changed():
        harness.metadata.clear()
        return True
    result = send(harness, before_submit=changed)
    assert result["category"] == "INVALID_TARGET"
    assert enters(harness) == []


@pytest.mark.parametrize("change", ["actor", "post", "focus", "draft"])
def test_dom_change_during_slow_final_callback_prevents_enter(harness, change):
    def changed():
        if change == "actor":
            harness.state["actorIds"] = ["77777"]
        elif change == "post":
            harness.state["url"] = f"https://www.facebook.com/{ALIAS}/posts/99999"
        elif change == "focus":
            harness.focused = False
        else:
            harness.draft = "Different comment"
        return True
    result = send(harness, before_submit=changed)
    assert result["category"] in {"CANCELLED_BEFORE_SEND", "INVALID_TARGET"}
    assert enters(harness) == []


def test_matching_comment_loaded_during_callback_blocks_duplicate(harness):
    def loaded():
        harness.state["comments"] = [{"id": COMMENT, "authorIds": [ALIAS], "text": MESSAGE}]
        harness.state["commentIds"] = [COMMENT]
        return True
    assert send(harness, before_submit=loaded)["category"] == "NEED_CHECK"
    assert enters(harness) == []


def test_actor_change_after_typing_prevents_enter(harness):
    harness.fail_after_insert = lambda: harness.state.update(actorIds=["77777"])
    result = send(harness)
    assert result["category"] == "CANCELLED_BEFORE_SEND"
    assert enters(harness) == []


def test_busy_profile_cannot_open_second_tab(harness):
    lock = threading.Lock()
    lock.acquire()
    browser._PROFILE_OPERATION_LOCKS[PROFILE] = lock
    result = send(harness)
    assert result["category"] == "BROWSER_BUSY"
    assert harness.opened == []
    assert lock.locked()
    lock.release()


def test_operation_lock_released_after_exception(harness):
    harness.fail_method = "Page.navigate"
    assert send(harness)["category"] == "NETWORK_RETRYABLE"
    assert not browser._PROFILE_OPERATION_LOCKS[PROFILE].locked()
    harness.fail_method = None
    assert send(harness)["success"] is True


def test_transport_failure_at_enter_is_uncertain(harness):
    harness.fail_method = "Input.dispatchKeyEvent"
    result = send(harness)
    assert result["category"] == "NEED_CHECK"
    assert len(enters(harness)) == 1


def test_transport_failure_before_enter_is_retryable(harness):
    harness.fail_method = "Input.insertText"
    result = send(harness)
    assert result["category"] == "NETWORK_RETRYABLE"
    assert enters(harness) == []


@pytest.mark.parametrize("comments", [
    [],
    [{"id": COMMENT, "authorIds": ["77777"], "text": MESSAGE}],
    [{"id": COMMENT, "authorIds": [ALIAS], "text": MESSAGE[:5]}],
    [{"id": "cdp_fake", "authorIds": [ALIAS], "text": MESSAGE}],
    [{"id": COMMENT, "authorIds": [], "text": MESSAGE}],
    [{"id": COMMENT, "authorIds": [ALIAS], "text": MESSAGE},
     {"id": "900001234567891", "authorIds": [ALIAS], "text": MESSAGE}],
])
def test_incomplete_or_ambiguous_post_submit_evidence_needs_check(harness, comments):
    harness.after_comments = comments
    result = send(harness)
    assert result["category"] == "NEED_CHECK"
    assert len(enters(harness)) == 1


def test_preexisting_matching_comment_blocks_duplicate(harness):
    harness.state["comments"] = [{"id": COMMENT, "authorIds": [ALIAS], "text": MESSAGE}]
    harness.state["commentIds"] = [COMMENT]
    result = send(harness)
    assert result["category"] == "NEED_CHECK"
    assert enters(harness) == []


def test_preexisting_id_cannot_prove_new_submission(harness):
    harness.state["commentIds"] = [COMMENT]
    result = send(harness)
    assert result["category"] == "NEED_CHECK"
    assert len(enters(harness)) == 1


@pytest.mark.parametrize("access,status", [("login_required", "LOGIN_REQUIRED"), ("checkpoint", "CHECKPOINT")])
def test_connection_and_send_report_login_or_checkpoint(harness, access, status):
    harness.state["accessState"] = access
    result = browser.verify_page_connection_cdp(harness.page)
    assert result["browser_status"] == status
    assert result["success"] is False
    result = send(harness)
    assert result["browser_status"] == status
    assert enters(harness) == []


def test_verification_returns_canonical_id_and_requires_absolute_time(harness):
    result = browser.verify_post_for_page_cdp(harness.page, POST, URL)
    assert result["clean_post_id"] == f"{CANONICAL}_{POST}"
    assert result["created_time"] == "2026-11-15T08:00:00+00:00"
    harness.state["posts"][0]["created_time"] = "2 hours ago"
    with pytest.raises(browser.BrowserAutomationError, match="thời điểm"):
        browser.verify_post_for_page_cdp(harness.page, POST, URL)


def test_discovery_skips_unknown_or_relative_dates_and_respects_since(harness):
    template = harness.state["posts"][0]
    for post_id, timestamp in [("111111", "2 hours ago"), ("222222", None),
                               ("333333", "2026-11-14T08:00:00+00:00")]:
        harness.state["posts"].append({**template, "id": post_id,
            "url": f"https://www.facebook.com/{ALIAS}/posts/{post_id}", "created_time": timestamp})
    result = browser.get_recent_posts_for_page_cdp(harness.page, "2026-11-15T00:00:00+00:00")
    assert [item["id"] for item in result] == [f"{CANONICAL}_{POST}"]


def test_missing_timestamp_does_not_invent_now(harness):
    harness.state["posts"][0].update(created_time=None, ageVal=1, ageUnit="h")
    assert browser.get_recent_posts_for_page_cdp(harness.page) == []


def test_duplicate_permalink_preserves_later_timestamp_and_rejects_conflict(harness):
    record = harness.state["posts"][0]
    harness.state["posts"].insert(0, {**record, "created_time": None})
    assert browser.get_recent_posts_for_page_cdp(harness.page)[0]["created_time"] == record["created_time"]
    harness.state["posts"].append({**record, "created_time": "2026-11-14T08:00:00+00:00"})
    with pytest.raises(browser.BrowserAutomationError, match="không nhất quán"):
        browser.get_recent_posts_for_page_cdp(harness.page, "2026-11-15T00:00:00+00:00")


def test_read_error_exposes_scheduler_contract(harness):
    harness.state["accessState"] = "checkpoint"
    with pytest.raises(browser.BrowserAutomationError) as caught:
        browser.get_recent_posts_for_page_cdp(harness.page)
    exc = caught.value
    assert exc.category == "TOKEN_EXPIRED"
    assert exc.browser_status == exc.status == "CHECKPOINT"
    assert exc.message == str(exc)
    assert exc.as_result()["browser_status"] == "CHECKPOINT"


def test_owned_browser_flag_is_preserved(harness):
    harness.opened_by_us = True
    assert send(harness)["success"] is True
    assert harness.released == [(PROFILE, True, 8000)]


@pytest.mark.parametrize("mode", ["rejected", "unreachable_port"])
def test_existing_server_start_failure_never_falls_back_to_inproc(monkeypatch, mode):
    monkeypatch.setattr(browser, "_read_runtime_readonly", lambda: {})
    monkeypatch.setattr(browser, "_INPROC_LAUNCHER", None)
    monkeypatch.setattr(browser, "_probe_cdp_port", lambda port: False)
    monkeypatch.setattr(browser, "_get_running_antidetect_server", lambda: (8000, "mock-key"))

    def forbidden():
        raise AssertionError("An existing server owns profile lifecycle")

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({"port": 9222}).encode()

    def request(*args, **kwargs):
        if mode == "rejected":
            raise RuntimeError("simulated rejection")
        return Response()

    monkeypatch.setattr(browser, "_get_inproc_launcher", forbidden)
    monkeypatch.setattr(browser.urllib.request, "urlopen", request)
    with pytest.raises(browser.BrowserAutomationError):
        browser.acquire_profile_cdp_port(PROFILE)


@pytest.mark.parametrize("health_failure", ["timeout", "wrong_app", "invalid_json"])
def test_recorded_unhealthy_server_never_falls_back(monkeypatch, tmp_path, health_failure):
    data = tmp_path / "antidetect" / "data"
    data.mkdir(parents=True)
    (data / "server.pid").write_text('{"port":8000,"pid":123}', encoding="utf-8")
    monkeypatch.setattr(browser.config, "ANTIDETECT_DIR", str(data.parent))
    monkeypatch.setattr(browser, "_INPROC_LAUNCHER", None)
    monkeypatch.setattr(browser, "_probe_cdp_port", lambda port: False)

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b"bad json" if health_failure == "invalid_json" else b'{"application":"other-app"}'

    def health(*args, **kwargs):
        if health_failure == "timeout":
            raise TimeoutError("mock timeout")
        return Response()

    def forbidden():
        raise AssertionError("must not create competing launcher")

    monkeypatch.setattr(browser.urllib.request, "urlopen", health)
    monkeypatch.setattr(browser, "_get_inproc_launcher", forbidden)
    with pytest.raises(browser.BrowserAutomationError) as caught:
        browser.acquire_profile_cdp_port(PROFILE)
    assert caught.value.category == "NETWORK_RETRYABLE"


def test_server_existing_browser_is_not_owned_even_without_already_running_field(monkeypatch):
    monkeypatch.setattr(browser, "_read_runtime_readonly", lambda: {})
    monkeypatch.setattr(browser, "_INPROC_LAUNCHER", None)
    monkeypatch.setattr(browser, "_OWNED_PROFILE_LEASES", {})
    monkeypatch.setattr(browser, "_probe_cdp_port", lambda port: port == 9222)
    monkeypatch.setattr(browser, "_get_running_antidetect_server", lambda: (8000, "mock-key"))

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({"success": True, "port": 9222, "launched_now": False,
                               "cdp_health": {"launch_id": "user-launch"}}).encode()

    monkeypatch.setattr(browser.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert browser.acquire_profile_cdp_port(PROFILE) == (9222, False, 8000)
    assert browser._OWNED_PROFILE_LEASES == {}


@pytest.mark.parametrize("changed", ["launch_id", "port", "owner_instance", None])
def test_owned_cleanup_is_bound_to_original_launch(monkeypatch, changed):
    lease = {"port": 9222, "launch_id": "launch-a", "owner_instance": "owner-a"}
    current = dict(lease)
    if changed:
        current[changed] = 9333 if changed == "port" else "replacement"
    monkeypatch.setattr(browser.config, "ANTIDETECT_AUTO_CLOSE_BROWSER", True)
    monkeypatch.setattr(browser, "_OWNED_PROFILE_LEASES", {PROFILE: lease})
    monkeypatch.setattr(browser, "_read_runtime_readonly", lambda: {PROFILE: current})
    monkeypatch.setattr(browser, "_get_running_antidetect_server", lambda: (8000, "mock-key"))
    calls = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def request(req, **kwargs):
        calls.append(req.full_url)
        return Response()

    monkeypatch.setattr(browser.urllib.request, "urlopen", request)
    browser.release_profile_cdp_port(PROFILE, True, 8000)
    if changed:
        assert calls == []
    else:
        assert calls == [f"http://127.0.0.1:8000/api/profiles/{PROFILE}/stop/owned?expected_launch_id=launch-a"]
    assert browser._OWNED_PROFILE_LEASES == {}


def test_never_close_user_browser_or_foreign_launcher(monkeypatch):
    calls = []
    monkeypatch.setattr(browser.config, "ANTIDETECT_AUTO_CLOSE_BROWSER", True)
    monkeypatch.setattr(browser, "_get_running_antidetect_server", lambda: None)
    monkeypatch.setattr(browser, "_INPROC_LAUNCHER", SimpleNamespace(stop=lambda pid: calls.append(pid)))
    monkeypatch.setattr(browser, "_OWNED_PROFILE_LEASES", {})
    browser.release_profile_cdp_port(PROFILE, False, None)
    browser.release_profile_cdp_port(PROFILE, True, 8000)
    assert calls == []


def test_strict_url_builder_rejects_arbitrary_database_urls():
    with pytest.raises(browser.BrowserAutomationError):
        browser.build_facebook_post_url(CANONICAL, POST, post_url="https://evil.invalid")
    assert browser.build_facebook_post_url(CANONICAL, POST).endswith(f"story_fbid={POST}&id={CANONICAL}")


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed to execute the mock DOM fixture")
def test_readiness_dom_detects_login_routes_without_form_fields():
    fixture = r"""
const source = require('fs').readFileSync(0, 'utf8');
global.document = {readyState:'complete', querySelector:() => null};
const result = ['/login', '/login.php', '/login/device-based/', '/loginXphp', '/checkpoint/'].map(path => {
  global.location = {href:'https://www.facebook.com' + path, pathname:path};
  return eval(source);
});
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run([shutil.which("node"), "-e", fixture], input=browser._BROWSER_READINESS_JS,
                               capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert completed.returncode == 0, completed.stderr
    states = json.loads(completed.stdout)
    assert [state["loginPresent"] for state in states] == [True, True, True, False, False]
    assert states[-1]["checkpointPresent"]
    assert all(not state["mainPresent"] for state in states)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed to execute the mock DOM fixture")
def test_dom_evidence_preserves_timestamp_and_only_accepts_comments_on_target(harness):
    # This runs only against the constructed DOM below, with no browser/network.
    fixture = r"""
const config = JSON.parse(require('fs').readFileSync(0, 'utf8'));
global.location = {href:config.url, pathname:new URL(config.url).pathname};
global.getComputedStyle = () => ({visibility:'visible'});
global.Node = {DOCUMENT_POSITION_FOLLOWING:4};
const root = {closest:() => null};
const outer = {parentElement:root, getAttribute:() => null};
const makeLink = (href, article) => ({href, textContent:'',
  closest:selector => selector === '[role="article"]' ? article : null,
  matches:() => false, getAttribute:() => null, querySelector:() => null,
  compareDocumentPosition:() => 4, contains:() => false});
const first = makeLink(config.url, outer);
const timestamp = makeLink(config.url, outer);
timestamp.querySelector = () => ({getAttribute:key => key === 'data-utime' ? '1794729600' : null});
const allLinks = [first, timestamp];
const commentArticles = [];
for (const spec of [
  {id:'90001',post:config.post},
  {id:'90002',post:'99999'},
  {id:'90003',post:config.post,reply:true},
  {id:'90004',post:config.post,nested:true}
]) {
  const article = {parentElement:{closest:() => spec.nested ? {} : outer},
    getBoundingClientRect:() => ({width:100,height:50})};
  const body = {innerText:config.message, closest:selector => selector === '[role="article"]' ? article : null};
  const author = makeLink('https://www.facebook.com/' + config.actor, article);
  author.closest = selector => selector === '[role="article"]' ? article : (selector.startsWith('h3') ? {} : null);
  const permalink = makeLink('https://www.facebook.com/' + config.actor + '/posts/' + spec.post +
    '?comment_id=' + spec.id + (spec.reply ? '&reply_comment_id=55' : ''), article);
  article.querySelectorAll = selector => selector === 'a[href]' ? [author,permalink] :
    selector === '[dir="auto"]' ? [body] : [];
  allLinks.push(author,permalink); commentArticles.push(article);
}
outer.querySelectorAll = selector => selector === 'a[href]' ? allLinks : [];
global.document = {querySelector:() => null,
  querySelectorAll:selector => selector === '[role="article"]' ? [outer,...commentArticles] : []};
const inspect = eval('(' + config.source + ')');
process.stdout.write(JSON.stringify(inspect(config.post, config.message)));
"""
    payload = {"source": browser._BROWSER_EVIDENCE_JS, "url": URL, "post": POST,
               "message": MESSAGE, "actor": ALIAS}
    completed = subprocess.run([shutil.which("node"), "-e", fixture], input=json.dumps(payload),
                               capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert [item["created_time"] for item in evidence["posts"]] == [None, "1794729600"]
    assert [item["id"] for item in evidence["comments"]] == ["90001"]
    assert evidence["commentIds"] == ["90001"]
    harness.state["posts"] = evidence["posts"]
    posts = browser.get_recent_posts_for_page_cdp(harness.page)
    assert len(posts) == 1
    assert posts[0]["id"] == f"{CANONICAL}_{POST}"
