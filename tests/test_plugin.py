"""Offline checks for the frozen author-feed acceptance rows."""
from __future__ import annotations

import contextvars
import json
import socket
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import importlib

import client
import gates
import service
from service import Deps

_CANON = gates.canonical_deliver
_CRON = gates.cron_proven
_GUARD = gates.write_guard_error


DID = "did:plc:z72i7hdynmk6r22z27h6tvur"
OTHER = "did:plc:otherauthor000000000000"


def _network_blocked() -> bool:
    try:
        sock = socket.socket()
        try:
            sock.connect(("127.0.0.1", 1))
        finally:
            sock.close()
    except RuntimeError as exc:
        return "network blocked" in str(exc)
    except OSError:
        return False
    return False


class FakeCron:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.created: list[dict] = []
        self.fail_remove = False
        self.fail_create = False
        self.updates = 0
        self.fail_update_at: int | None = None

    def load_jobs(self):
        return list(self.jobs)

    def get_job(self, job_id):
        return next((job for job in self.jobs if job["id"] == job_id), None)

    def create_job(self, **kwargs):
        self.created.append(kwargs)
        if self.fail_create:
            raise RuntimeError("create failed")
        job = {"id": f"job{len(self.jobs) + 1}", **kwargs}
        self.jobs.append(job)
        return job

    def update_job(self, job_id, updates):
        self.updates += 1
        if self.fail_update_at is not None and self.updates == self.fail_update_at:
            return None
        job = self.get_job(job_id)
        if job is None:
            return None
        job.update(updates)
        return job

    def remove_job(self, job_id):
        if self.fail_remove:
            return False
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job["id"] != job_id]
        return len(self.jobs) != before


class Transport:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.urls: list[str] = []
        self.fail: Exception | None = None

    def __call__(self, url: str):
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        for key, value in self.routes.items():
            if key in url:
                status, body = value
                if isinstance(body, dict):
                    body = json.dumps(body).encode()
                return status, body
        return 404, b'{"error":"Missing","message":"no route"}'


def profile(did=DID, handle="bsky.app", posts=866, **extra):
    data = {
        "did": did,
        "handle": handle,
        "displayName": "Bluesky",
        "followersCount": 10,
        "followsCount": 20,
        "postsCount": posts,
        "avatar": "https://cdn.example/avatar.jpg",
        "banner": "https://cdn.example/banner.jpg",
        "description": "drop me",
        "pinnedPost": {"uri": "at://pinned"},
    }
    data.update(extra)
    return data


def post(uri, did=DID, handle="bsky.app", text="hello", reason=None, embed=None, labels=None, created="2020-01-01T00:00:00.000Z"):
    body = {
        "uri": uri,
        "author": {"did": did, "handle": handle, "avatar": "https://cdn.example/p.jpg"},
        "record": {"text": text, "createdAt": created},
    }
    if embed is not None:
        body["embed"] = embed
    if labels is not None:
        body["labels"] = labels
    item = {"post": body}
    if reason is not None:
        item["reason"] = reason
    return item


def feed_of(*items, cursor="cursor-1"):
    return {"feed": list(items), "cursor": cursor}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "write_guard_error", lambda path: None)
    monkeypatch.setattr(gates, "cron_proven", lambda: False)
    monkeypatch.setattr(gates, "canonical_deliver", lambda deliver, platforms=None: _deliver(deliver))
    cron = FakeCron()
    transport = Transport({})
    deps = Deps()
    deps.data_dir = tmp_path
    deps.cron = cron
    deps.transport = transport
    deps.now = lambda: 1_700_000_000.0

    def prove(value=True):
        monkeypatch.setattr(gates, "cron_proven", lambda: value)
        # A proven cron run means the saved job is still there. Tests that
        # want "gone" clear cron.jobs after this.
        state_path = tmp_path / "state.json"
        if value and state_path.is_file():
            try:
                stored = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored = None
            job_id = stored.get("job_id") if isinstance(stored, dict) else None
            if job_id and cron.get_job(str(job_id)) is None:
                cron.jobs.append({
                    "id": str(job_id),
                    "name": "author-feed",
                    "schedule": "every 60m",
                    "deliver": "local",
                })

    return types.SimpleNamespace(deps=deps, cron=cron, transport=transport, prove=prove, path=tmp_path / "state.json")


def _deliver(deliver, platforms=None):
    return _CANON(deliver, {"telegram", "discord"} if platforms is None else platforms)


def save(box, **kwargs):
    data = {
        "did": DID,
        "handle": "bsky.app",
        "deliver": "local",
        "interval_words": "every hour",
        "job_id": "job1",
        "baseline_done": True,
        "seen": [],
        "failure": None,
        "last_success_at": None,
    }
    data.update(kwargs)
    box.path.write_text(json.dumps(data), encoding="utf-8")
    return data


def test_profile_drops_avatar_and_feed_query(box):
    box.transport.routes = {
        "getProfile": (200, profile()),
        "getAuthorFeed": (200, feed_of(
            post("at://own/1", text="mine", created="2026-01-02T00:00:00.000Z"),
            post("at://re/2", did="did:aster", handle="aster.id", text="their words", reason={"$type": "app.bsky.feed.defs#reasonRepost", "by": {"handle": "bsky.app"}}),
            post("at://pin/3", text="old pin", reason={"$type": "app.bsky.feed.defs#reasonPin"}),
            post("at://other/4", did=OTHER, handle="someone.example", text="else"),
        )),
    }
    got = service.profile_get(box.deps, {"actor": "bsky.app"})
    assert got["ok"] is True
    assert set(got["profile"]) == set(service.PROFILE_KEYS)
    assert "cdn.example" not in json.dumps(got)
    assert got["profile"]["postsCount"] == 866
    fed = service.author_feed(box.deps, {"actor": "bsky.app"})
    assert [item["kind"] for item in fed["items"]] == ["post", "repost", "pin", "other"]
    assert fed["items"][1]["author_handle"] == "aster.id"
    assert fed["items"][1]["text"] == "their words"
    kinds = {item["uri"]: item["kind"] for item in fed["items"]}
    assert "their words" not in fed["message"]
    url = box.transport.urls[-1]
    assert "limit=10" in url and "filter=posts_no_replies" in url and "includePins" not in url
    assert "createdAt" not in url
    assert box.path.exists() is False
    assert kinds["at://pin/3"] == "pin"


def test_unknown_args_and_watch_actor_never_fetch(box):
    box.transport.routes = {"getProfile": (200, profile()), "getAuthorFeed": (200, feed_of())}
    assert service.profile_get(box.deps, {})["ok"] is False
    assert service.author_feed(box.deps, {"actor": "bsky.app", "filter": "nope"})["ok"] is False
    assert service.author_feed(box.deps, {"actor": "bsky.app", "limit": 50})["ok"] is False
    assert service.author_feed(box.deps, {"actor": "not a handle"})["ok"] is False
    watched = service.author_watch(box.deps, {"actor": "bsky.app"})
    assert watched["notify"] is True and watched["ok"] is False
    assert box.transport.urls == []


def test_failures_do_not_clear_seen(box):
    save(box, seen=["at://own/1"], baseline_done=True)
    before = box.path.read_bytes()
    cases = [
        (400, {"error": "InvalidRequest", "message": "Profile not found"}),
        (200, {"error": "InvalidRequest", "message": "Profile not found"}),
        (200, b""),
        (200, b"<html>nope</html>"),
        (200, {"feed": "nope"}),
        (200, {}),
        (500, {"error": "Upstream"}),
    ]
    for status, body in cases:
        box.transport.routes = {"getAuthorFeed": (status, body)}
        box.prove(True)
        got = service.author_watch(box.deps, {})
        assert got["ok"] is False, body
        assert "zero posts" not in got["message"]
        assert json.loads(box.path.read_text())["seen"] == ["at://own/1"]
    assert "届いて効いた" not in json.loads(box.path.read_text()).__repr__()
    box.transport.fail = TimeoutError("timed out")
    got = service.author_watch(box.deps, {})
    assert "既読は変わっていない。失敗は記録した。" in got["message"]
    assert json.loads(box.path.read_text())["failure"]["code"] == "timeout"
    assert "届いて効いた" not in got["message"]
    assert box.path.read_bytes() != before or json.loads(box.path.read_text())["seen"] == ["at://own/1"]
    calls = len(box.transport.urls)
    service.author_watch(box.deps, {})
    assert len(box.transport.urls) == calls + 1


def test_empty_feed_and_posts_count_are_not_a_cursor(box):
    save(box, seen=["at://own/1", "at://own/2"], baseline_done=True)
    box.prove(True)
    box.transport.routes = {"getAuthorFeed": (200, {"feed": [
        post("at://own/1"),
        post("at://own/2"),
    ], "postsCount": 866, "cursor": "next"})}
    got = service.author_watch(box.deps, {})
    assert got["notify"] is False
    assert got["new"] == []
    state = json.loads(box.path.read_text())
    assert state["seen"] == ["at://own/1", "at://own/2"]
    assert "cursor" not in state
    assert "postsCount" not in state
    assert "text" not in state
    box.transport.routes = {"getAuthorFeed": (200, {"feed": []})}
    got = service.author_watch(box.deps, {})
    assert got["ok"] is True
    assert json.loads(box.path.read_text())["seen"] == ["at://own/1", "at://own/2"]
    assert "消えた" not in got["message"]


def test_baseline_repost_pin_and_later_new_post(box):
    save(box, seen=[], baseline_done=False, job_id="job1")
    box.prove(True)
    repost = post("at://re/1", did="did:aster", handle="aster.id", text="not mine", reason={"$type": "app.bsky.feed.defs#reasonRepost"})
    pin = post("at://pin/1", text="pinned", reason={"$type": "app.bsky.feed.defs#reasonPin"})
    own = post("at://own/1", text="first")
    box.transport.routes = {"getAuthorFeed": (200, feed_of(repost, pin))}
    got = service.author_watch(box.deps, {})
    assert got["notify"] is True and got["new"] == []
    assert "no post by this account" in got["message"]
    state = json.loads(box.path.read_text())
    assert state["baseline_done"] is True and state["seen"] == []
    box.transport.routes = {"getAuthorFeed": (200, feed_of(repost, own, post("at://own/2", text="second")))}
    # reset baseline by rewriting file
    save(box, seen=[], baseline_done=False)
    got = service.author_watch(box.deps, {})
    assert got["new"] == []
    assert json.loads(box.path.read_text())["seen"] == ["at://own/1", "at://own/2"]
    assert "not mine" not in got["message"]
    third = post("at://own/3", text="third")
    box.transport.routes = {"getAuthorFeed": (200, feed_of(own, post("at://own/2", text="second"), third, repost))}
    got = service.author_watch(box.deps, {})
    assert got["notify"] is True
    assert got["new"] == ["at://own/3"]
    assert "at://re/1" not in json.loads(box.path.read_text())["seen"]
    assert "at://pin/1" not in json.loads(box.path.read_text())["seen"]


def test_full_page_does_not_claim_the_whole_history(box):
    save(box, seen=["at://old"], baseline_done=True)
    box.prove(True)
    items = [post(f"at://own/{i}", text=f"t{i}") for i in range(10)]
    box.transport.routes = {"getAuthorFeed": (200, {"feed": items})}
    got = service.author_watch(box.deps, {})
    assert len(got["new"]) == 10
    assert got["notify"] is True
    # The feed offered no cursor, so the whole feed was read and nothing slipped past.
    assert "取りこぼしの可能性" not in got["message"]
    assert "Older posts were not fetched" not in got["message"]
    assert "entire" not in got["message"].lower()
    assert "全部" not in got["message"]


def test_a_vanished_account_is_not_called_a_possible_miss(box):
    save(box, seen=["at://own/gone"], baseline_done=True)
    box.prove(True)
    box.transport.routes = {"getAuthorFeed": (200, {"feed": []})}
    for _ in range(3):
        got = service.author_watch(box.deps, {})
        assert got["ok"] is True
        assert got["notify"] is False
        assert got["message"] == "No new posts."
    assert json.loads(box.path.read_text())["seen"] == ["at://own/gone"]


def test_a_new_post_on_the_first_page_is_not_called_older(box):
    save(box, seen=[], baseline_done=True)
    box.prove(True)
    box.transport.routes = {"getAuthorFeed": (200, {"feed": [post("at://own/late", text="later")]})}
    got = service.author_watch(box.deps, {})
    assert got["new"] == ["at://own/late"]
    assert "may be older" not in got["message"]


def test_a_post_from_a_later_page_may_be_older(box):
    save(box, seen=["at://own/old"], baseline_done=True)
    box.prove(True)
    _paged(box, {
        "": {"feed": _reposts(1), "cursor": "p2"},
        "p2": {"feed": [post("at://own/late", text="later"), post("at://own/old", text="old")]},
    })
    got = service.author_watch(box.deps, {})
    assert got["new"] == ["at://own/late"]
    assert "may be older" in got["message"]


def test_manual_feed_does_not_advance_seen(box):
    save(box, seen=["at://own/1"], baseline_done=True)
    box.prove(True)
    box.transport.routes = {"getAuthorFeed": (200, feed_of(post("at://own/1"), post("at://own/2", text="new")))}
    service.author_watch(box.deps, {})
    assert json.loads(box.path.read_text())["seen"] == ["at://own/1", "at://own/2"]
    box.prove(False)
    before = box.path.read_bytes()
    fed = service.author_feed(box.deps, {"actor": "bsky.app"})
    assert any(item["uri"] == "at://own/2" for item in fed["items"])
    assert box.path.read_bytes() == before
    box.prove(True)
    box.transport.routes = {"getAuthorFeed": (200, feed_of(post("at://own/1"), post("at://own/2"), post("at://own/3", text="three")))}
    got = service.author_watch(box.deps, {})
    assert got["new"] == ["at://own/3"]


def test_env_cron_and_host_do_not_advance(box, monkeypatch):
    save(box, seen=["at://own/1"], baseline_done=True)
    box.cron.jobs.append({"id": "job1", "name": "author-feed", "schedule": "every 60m", "deliver": "local"})
    box.transport.routes = {"getAuthorFeed": (200, feed_of(post("at://own/1"), post("at://own/2")))}
    unset = object()
    var = contextvars.ContextVar("HERMES_CRON_SESSION", default=unset)
    mod = types.ModuleType("gateway.session_context")
    mod._UNSET = unset
    mod.HERMES_CRON_SESSION = var
    monkeypatch.setitem(__import__("sys").modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(__import__("sys").modules, "gateway.session_context", mod)
    monkeypatch.setattr(gates, "cron_proven", _CRON)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    got = service.author_watch(box.deps, {})
    assert got["notify"] is True
    assert "cannot monitor" in got["message"]
    assert "Nothing new" not in got["message"]
    assert box.transport.urls == []
    assert json.loads(box.path.read_text())["seen"] == ["at://own/1"]
    var.set("1")
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    again = service.author_watch(box.deps, {})
    assert box.transport.urls == []
    # Host cannot tell a manual call from the cron job, so every run says so.
    assert again["message"] == got["message"]
    assert again["notify"] is True
    assert json.loads(box.path.read_text())["seen"] == ["at://own/1"]
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "0")
    got = service.author_watch(box.deps, {})
    assert got["ok"] is True
    assert "at://own/2" in json.loads(box.path.read_text())["seen"]
    # handler accepts parent_agent and does not read it
    from service import author_watch
    assert author_watch(box.deps, {}, parent_agent=types.SimpleNamespace(platform="cron"))["ok"] in {True, False}


def test_private_cron_var_name_is_proven(monkeypatch):
    unset = object()
    var = contextvars.ContextVar("HERMES_CRON_SESSION", default=unset)
    mod = types.ModuleType("gateway.session_context")
    mod._UNSET = unset
    mod._CRON_SESSION = var
    monkeypatch.setitem(__import__("sys").modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(__import__("sys").modules, "gateway.session_context", mod)
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    assert gates.cron_proven() is False
    var.set("1")
    assert gates.cron_proven() is True
    var.set("")
    assert gates.cron_proven() is False


def test_missing_cron_name_fails_closed(monkeypatch):
    mod = types.ModuleType("gateway.session_context")
    monkeypatch.setitem(__import__("sys").modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(__import__("sys").modules, "gateway.session_context", mod)
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    assert gates.cron_proven() is False


def test_truncation_embed_and_labels(box):
    long_text = "x" * 301
    labels = [{"val": "v" * 120}] * 21
    box.transport.routes = {"getAuthorFeed": (200, feed_of(
        post("at://own/long", text=long_text, labels=labels),
        post("at://own/empty", text="", embed={"$type": "app.bsky.embed.record#view", "record": {"uri": "at://x"}}),
    ))}
    got = service.author_feed(box.deps, {"actor": "bsky.app"})
    assert got["items"][0]["text_truncated"] is True
    assert len(got["items"][0]["text"]) == 300
    assert len(got["items"][0]["labels"]) == 20
    assert len(got["items"][0]["labels"][0]) == 100
    assert got["items"][1]["text"] == ""
    assert got["items"][1]["embed_type"] == "app.bsky.embed.record#view"
    assert len(box.transport.urls) == 1
    assert "cdn.example" not in json.dumps(got)


def test_blocked_actor_is_not_an_empty_feed(box):
    box.transport.routes = {"getAuthorFeed": (400, {"error": "BlockedActor", "message": "blocked"})}
    got = service.author_feed(box.deps, {"actor": "bsky.app"})
    assert got["ok"] is False
    assert "BlockedActor" in got["message"]


def test_schedule_keeps_old_did_on_failure_and_resets_on_change(box):
    box.cron.jobs.append({"id": "job1", "name": "author-feed"})
    save(box, seen=["at://own/1"], handle="old.example", did=DID)
    box.transport.routes = {"getProfile": (400, {"error": "InvalidRequest", "message": "Profile not found"})}
    code, text = service.schedule(box.deps, "missing.example", "local", None)
    assert code == 1
    state = json.loads(box.path.read_text())
    assert state["did"] == DID and state["seen"] == ["at://own/1"]
    assert len(box.cron.jobs) == 1
    assert "Profile not found" in text
    box.transport.routes = {"getProfile": (200, profile(did=DID, handle="new.handle"))}
    code, text = service.schedule(box.deps, "new.handle", "local", "15 minutes")
    assert code == 0
    state = json.loads(box.path.read_text())
    assert state["seen"] == ["at://own/1"] and state["handle"] == "new.handle"
    assert state["interval_words"] == "every 15 minutes"
    assert len(box.cron.jobs) == 1
    assert "* * *" not in text and "*/" not in text
    for verb in ("create", "run", "list", "status", "remove"):
        assert f"hermes cron {verb}" in text
    assert "hermes -p" not in text
    box.transport.routes = {"getProfile": (200, profile(did=OTHER, handle="other.example"))}
    code, _text = service.schedule(box.deps, "other.example", "origin", "every 45 minutes")
    assert code == 0
    state = json.loads(box.path.read_text())
    assert state["did"] == OTHER and state["seen"] == [] and state["baseline_done"] is False
    assert len(box.cron.jobs) == 1
    assert box.cron.created == [] or all("interpreter" not in item and "no_agent" not in item for item in box.cron.created)


def test_interval_and_deliver_rejections_do_not_fetch(box, monkeypatch):
    box.cron.jobs.append({"id": "job1", "name": "author-feed", "deliver": "local"})
    save(box, seen=["at://keep"])
    for every in ("5 minutes", "every 7 minutes", "0 * * * *", "*/15 * * * *", "10"):
        code, text = service.schedule(box.deps, "bsky.app", "local", every)
        assert code == 1, every
        assert box.transport.urls == []
        assert "cron expression" in text or "rejected" in text or "not understood" in text
    code, text = service.schedule(box.deps, "bsky.app", "5m", None)
    assert code == 1 and "previous target remains local" in text
    code, text = service.schedule(box.deps, "bsky.app", "cli", None)
    assert code == 1 and box.transport.urls == []
    monkeypatch.setattr(gates, "canonical_deliver", lambda deliver, platforms=None: _CANON(deliver, None))
    code, text = service.schedule(box.deps, "bsky.app", "telegram", None)
    assert code == 1 and box.transport.urls == []
    box.transport.routes = {"getProfile": (200, profile())}
    code, text = service.schedule(box.deps, "bsky.app", "origin", "every hour")
    assert code == 0
    assert len(box.transport.urls) == 1
    code, text = service.schedule(box.deps, "bsky.app", "local", None)
    assert code == 0 and "Replaced deliver target origin with local" in text


def test_profile_prefix_and_no_profile_flag(monkeypatch):
    monkeypatch.setattr(gates, "active_profile_name", lambda: "harnessprof")
    lines = gates.hermes_cron_lines()
    assert lines == [f"hermes -p harnessprof cron {verb}" for verb in ("create", "list", "status")]
    assert gates.hermes_cron_lines("default")[0] == "hermes cron create"
    with_id = gates.hermes_cron_lines("harnessprof", "job1")
    assert with_id == [
        "hermes -p harnessprof cron create",
        "hermes -p harnessprof cron run job1",
        "hermes -p harnessprof cron list",
        "hermes -p harnessprof cron status",
        "hermes -p harnessprof cron remove job1",
    ]
    assert gates.hermes_cron_lines("default", "job1")[4] == "hermes cron remove job1"
    root = Path(__file__).resolve().parents[1]
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "add_argument" not in source or "--profile" not in source
    assert "-p" not in source.split("hermes -p")[0] or "add_argument('-p'" not in source
    assert "get_session_env" not in source
    assert "request_tool_approval" not in source
    assert "register_command" not in source
    assert "homeassistant" not in source
    assert "interpreter" not in source or "no_agent" in source


def test_named_profile_custom_without_identity(tmp_path):
    assert gates.named_profile_for_commands("custom", tmp_path, tmp_path.parent) == "default"
    home = tmp_path / "profiles" / "custom"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("x: 1", encoding="utf-8")
    assert gates.named_profile_for_commands("custom", home, tmp_path / "profiles") == "custom"


def test_unschedule_leaves_the_file(box):
    box.cron.jobs.append({"id": "job1", "name": "author-feed"})
    save(box, seen=["at://own/1"])
    code, text = service.unschedule(box.deps)
    assert code == 0
    assert box.cron.jobs == []
    state = json.loads(box.path.read_text())
    assert state["seen"] == ["at://own/1"] and state["job_id"] is None
    assert "plugins remove" in text
    code, text = service.status(box.deps)
    assert code == 0 and box.transport.urls == []
    assert "at://own/1" not in text


def test_second_job_is_not_created_and_failed_save_removes_new_id(box, monkeypatch):
    box.cron.jobs.append({"id": "already", "name": "author-feed"})
    box.transport.routes = {"getProfile": (200, profile())}
    code, _text = service.schedule(box.deps, "bsky.app", "local", None)
    assert code == 0
    assert [job["id"] for job in box.cron.jobs] == ["already"]
    box.path.unlink()
    box.cron.jobs.clear()
    monkeypatch.setattr(gates, "write_guard_error", lambda path: "blocked")
    code, text = service.schedule(box.deps, "bsky.app", "local", None)
    assert code == 1
    assert box.cron.jobs == []
    assert "removed" in text
    box.cron.fail_remove = True
    code, text = service.schedule(box.deps, "bsky.app", "local", None)
    assert code == 1 and "leftover job id" in text
    assert box.cron.created
    assert all("interpreter" not in item and "no_agent" not in item for item in box.cron.created)
    assert "author_watch" in box.cron.created[-1]["prompt"]


def test_failed_save_after_update_puts_the_job_back(box, monkeypatch):
    box.cron.jobs.append({
        "id": "job1",
        "name": "author-feed",
        "schedule": "every 60m",
        "deliver": "local",
    })
    save(box, seen=["at://own/1"], interval_words="every hour", deliver="local", job_id="job1")
    before = box.path.read_bytes()
    monkeypatch.setattr(gates, "write_guard_error", lambda path: "Write denied")
    box.transport.routes = {"getProfile": (200, profile())}
    code, text = service.schedule(box.deps, "bsky.app", "local", "every 15 minutes")
    assert code == 1
    assert "Write denied" in text
    assert "put back" in text
    assert box.cron.jobs[0]["schedule"] == "every 60m"
    assert box.cron.jobs[0]["deliver"] == "local"
    assert box.path.read_bytes() == before
    assert len(box.cron.jobs) == 1

    box.cron.jobs[0] = {"id": "job1", "name": "author-feed"}
    save(box, seen=["at://own/1"], interval_words="every 15 minutes", deliver="local", job_id="job1")
    before = box.path.read_bytes()
    box.cron.updates = 0
    code, text = service.schedule(box.deps, "bsky.app", "local", "every hour")
    assert code == 1 and "put back" in text
    assert box.cron.jobs[0]["schedule"] == "every 15m"
    assert box.cron.jobs[0]["deliver"] == "local"
    assert box.path.read_bytes() == before

    box.cron.updates = 0
    box.cron.fail_update_at = 2
    code, text = service.schedule(box.deps, "bsky.app", "origin", "every 30 minutes")
    assert code == 1
    assert "already on every 30m with deliver origin" in text
    assert "was not put back" in text
    assert box.cron.jobs[0]["schedule"] == "every 30m"
    assert box.cron.jobs[0]["deliver"] == "origin"
    assert box.path.read_bytes() == before


def test_corrupt_file_is_not_overwritten(box):
    box.path.write_bytes(b"{not json")
    before = box.path.read_bytes()
    box.prove(True)
    box.transport.routes = {"getAuthorFeed": (200, feed_of(post("at://own/1")))}
    got = service.author_watch(box.deps, {})
    assert got["notify"] is True and box.path.read_bytes() == before
    assert box.transport.urls == []
    code, _text = service.schedule(box.deps, "bsky.app", "local", None)
    assert code == 1 and box.path.read_bytes() == before


def test_failure_notify_gap_and_recovery(box):
    save(box, seen=["at://own/1"], baseline_done=True)
    box.prove(True)
    box.transport.fail = ConnectionError("refused")
    times = {"n": 1_700_000_000.0}
    box.deps.now = lambda: times["n"]
    first = service.author_watch(box.deps, {})
    assert first["notify"] is True
    second = service.author_watch(box.deps, {})
    assert second["notify"] is False
    box.transport.fail = None
    box.transport.routes = {"getAuthorFeed": (400, {"error": "InvalidRequest", "message": "Profile not found"})}
    changed = service.author_watch(box.deps, {})
    assert changed["notify"] is True
    assert json.loads(box.path.read_text())["failure"]["code"] == "http_400"
    box.transport.routes = {"getAuthorFeed": (503, {"error": "Upstream", "message": "down"})}
    busy = service.author_watch(box.deps, {})
    assert busy["notify"] is True
    assert json.loads(box.path.read_text())["failure"]["code"] == "http_503"
    box.transport.routes = {"getAuthorFeed": (400, {"error": "InvalidRequest", "message": "Profile not found"})}
    changed_status = service.author_watch(box.deps, {})
    assert changed_status["notify"] is True
    assert json.loads(box.path.read_text())["failure"]["code"] == "http_400"
    times["n"] += service.NOTIFY_GAP
    box.transport.routes = {"getAuthorFeed": (400, {"error": "InvalidRequest", "message": "Profile not found"})}
    later = service.author_watch(box.deps, {})
    assert later["notify"] is True
    box.transport.routes = {"getAuthorFeed": (200, feed_of(post("at://own/1")))}
    recovered = service.author_watch(box.deps, {})
    assert recovered["notify"] is True and "cleared" in recovered["message"]
    quiet = service.author_watch(box.deps, {})
    assert quiet["notify"] is False
    missing = service.author_watch(Deps(), {"actor": "nope"})
    assert missing["notify"] is True


def test_no_saved_account_is_not_a_failure_code(box):
    box.prove(True)
    got = service.author_watch(box.deps, {})
    assert got["notify"] is True and "No saved account" in got["message"]
    assert box.path.exists() is False
    assert box.transport.urls == []


def test_guard_exception_and_unreadable_dir(box, monkeypatch):
    save(box, seen=["at://own/1"])
    before = box.path.read_bytes()
    monkeypatch.setattr(gates, "write_guard_error", lambda path: "The write check raised, so the file was not saved.")
    box.prove(True)
    box.transport.routes = {"getAuthorFeed": (200, feed_of(post("at://own/9", text="x")))}
    got = service.author_watch(box.deps, {})
    assert got["ok"] is False
    assert box.path.read_bytes() == before

    def boom(_path, **_kwargs):
        raise RuntimeError("guard")

    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name == "agent.file_safety":
            mod = types.ModuleType(name)
            mod.get_write_denied_error = boom
            return mod
        return real_import(name, package)

    monkeypatch.setattr(gates.importlib, "import_module", fake_import)
    message = _GUARD(str(box.path))
    assert "raised" in message
    monkeypatch.setattr(gates.importlib, "import_module", lambda name, package=None: (_ for _ in ()).throw(RuntimeError("missing")))
    assert "could not be loaded" in _GUARD(str(box.path))


def _paged(box, pages: dict):
    def transport(url):
        box.transport.urls.append(url)
        cursor = ""
        if "cursor=" in url:
            cursor = url.split("cursor=", 1)[1].split("&", 1)[0]
        body = pages.get(cursor)
        if body is None:
            return 404, b'{"error":"Missing","message":"no page"}'
        return 200, json.dumps(body).encode()

    box.deps.transport = transport


def _reposts(count, start=0):
    return [
        post(
            f"at://re/{start + i}",
            did="did:aster",
            handle="aster.id",
            text="r",
            reason={"$type": "app.bsky.feed.defs#reasonRepost"},
        )
        for i in range(count)
    ]


def test_ten_reposts_do_not_hide_one_own_post(box):
    save(box, seen=["at://own/old"], baseline_done=True)
    box.prove(True)
    _paged(box, {
        "": {"feed": _reposts(10), "cursor": "p2"},
        "p2": {"feed": [post("at://own/new", text="hello"), post("at://own/old", text="old")]},
    })
    got = service.author_watch(box.deps, {})
    assert got["new"] == ["at://own/new"]
    assert got["notify"] is True
    assert "取りこぼし" not in got["message"]
    assert "Older posts were not fetched" not in got["message"]
    assert len(box.transport.urls) == 2


def test_eleven_new_posts_are_all_announced(box):
    save(box, seen=["at://own/old"], baseline_done=True)
    box.prove(True)
    page1 = [post(f"at://own/n{i}", text=f"n{i}") for i in range(10)]
    page2 = [post("at://own/n10", text="eleven"), post("at://own/old", text="old")]
    _paged(box, {"": {"feed": page1, "cursor": "p2"}, "p2": {"feed": page2}})
    got = service.author_watch(box.deps, {})
    assert got["new"] == [f"at://own/n{i}" for i in range(11)]
    assert "取りこぼし" not in got["message"]
    assert "Older posts were not fetched" not in got["message"]


def test_full_page_with_overlap_has_no_miss_note(box):
    seen = ["at://own/0"] + [f"at://own/old{i}" for i in range(8)]
    save(box, seen=seen, baseline_done=True)
    box.prove(True)
    items = [post("at://own/new", text="new"), post("at://own/0", text="t")] + [
        post(f"at://own/old{i}", text="t") for i in range(8)
    ]
    box.transport.routes = {"getAuthorFeed": (200, {"feed": items, "cursor": "more"})}
    got = service.author_watch(box.deps, {})
    assert got["new"] == ["at://own/new"]
    assert "Older posts were not fetched" not in got["message"]
    assert "取りこぼし" not in got["message"]
    assert len(box.transport.urls) == 1


def test_walk_stops_at_three_pages_and_reports_a_miss(box):
    save(box, seen=["at://own/old"], baseline_done=True)
    box.prove(True)
    pages = {}
    cursor = ""
    for number in range(3):
        pages[cursor] = {
            "feed": [post(f"at://own/p{number}-{i}", text="n") for i in range(10)],
            "cursor": f"p{number + 1}",
        }
        cursor = f"p{number + 1}"
    pages["p3"] = {"feed": [post("at://own/old", text="old")]}
    _paged(box, pages)
    got = service.author_watch(box.deps, {})
    expected = [f"at://own/p{number}-{i}" for number in range(3) for i in range(10)]
    assert len(box.transport.urls) == service.MAX_WATCH_PAGES
    assert got["notify"] is True
    assert got["new"] == expected
    assert "取りこぼしの可能性" in got["message"]
    assert "New post at://own/p0-0" in got["message"]
    stored = json.loads(box.path.read_text())["seen"]
    assert "at://own/old" in stored
    assert "at://own/p0-0" in stored


def _self_repost(uri, text="mine again"):
    return post(uri, text=text, reason={"$type": "app.bsky.feed.defs#reasonRepost", "by": {"did": DID}})


def test_self_repost_of_a_seen_post_does_not_stop_the_walk(box):
    save(box, seen=["at://own/old"], baseline_done=True)
    box.prove(True)
    page1 = [_self_repost("at://own/old")] + _reposts(9)
    page2 = [post("at://own/new", text="W"), post("at://own/old", text="old")]
    _paged(box, {"": {"feed": page1, "cursor": "p2"}, "p2": {"feed": page2}})
    got = service.author_watch(box.deps, {})
    assert got["new"] == ["at://own/new"]
    assert got["notify"] is True
    assert "No new posts" not in got["message"]
    assert "取りこぼし" not in got["message"]
    assert len(box.transport.urls) == 2
    assert json.loads(box.path.read_text())["seen"] == ["at://own/old", "at://own/new"]


def test_nine_items_with_a_cursor_are_not_the_last_page(box):
    save(box, seen=["at://own/old"], baseline_done=True)
    box.prove(True)
    page1 = _reposts(9)
    page2 = [post("at://own/new", text="W"), post("at://own/old", text="old")]
    _paged(box, {"": {"feed": page1, "cursor": "p2"}, "p2": {"feed": page2}})
    got = service.author_watch(box.deps, {})
    assert got["new"] == ["at://own/new"]
    assert "取りこぼし" not in got["message"]
    assert len(box.transport.urls) == 2


def test_miss_note_is_thinned_to_first_change_and_a_day(box):
    save(box, seen=["at://own/old"], baseline_done=True)
    box.prove(True)
    times = {"n": 1_700_000_000.0, "i": 0}
    box.deps.now = lambda: times["n"]

    def growing(_url):
        start = times["i"]
        times["i"] += 10
        items = [post(f"at://own/g{start + k}", text="n") for k in range(10)]
        return 200, json.dumps({"feed": items, "cursor": f"c{start}"}).encode()

    box.deps.transport = growing

    def batch(start: int) -> list[str]:
        return [f"at://own/g{start + k}" for k in range(30)]

    first = service.author_watch(box.deps, {})
    assert first["notify"] is True and "取りこぼしの可能性" in first["message"]
    assert first["new"] == batch(0)
    assert "New post at://own/g0" in first["message"]
    for start in (30, 60):
        again = service.author_watch(box.deps, {})
        assert again["notify"] is True
        assert again["new"] == batch(start)
        assert "取りこぼし" not in again["message"]
        assert "New post" in again["message"]
    times["n"] += service.NOTIFY_GAP
    later = service.author_watch(box.deps, {})
    assert later["notify"] is True and "取りこぼしの可能性" in later["message"]
    assert later["new"] == batch(90)
    box.deps.transport = box.transport
    box.transport.routes = {"getAuthorFeed": (200, {"feed": [post("at://own/old", text="old")]})}
    closed = service.author_watch(box.deps, {})
    assert closed["notify"] is False
    assert json.loads(box.path.read_text())["miss"] is None
    box.deps.transport = growing
    reopened = service.author_watch(box.deps, {})
    assert reopened["notify"] is True and "取りこぼしの可能性" in reopened["message"]


def test_recovery_does_not_repeat_the_miss_notice_inside_a_day(box):
    save(box, seen=["at://own/old"], baseline_done=True)
    box.prove(True)
    times = {"n": 1_700_000_000.0, "i": 0}
    box.deps.now = lambda: times["n"]

    def growing(_url):
        start = times["i"]
        times["i"] += 10
        items = [post(f"at://own/g{start + k}", text="n") for k in range(10)]
        return 200, json.dumps({"feed": items, "cursor": f"c{start}"}).encode()

    box.deps.transport = growing
    first = service.author_watch(box.deps, {})
    assert first["notify"] is True and "取りこぼしの可能性" in first["message"]
    assert first["new"] == [f"at://own/g{k}" for k in range(30)]
    box.deps.transport = box.transport
    box.transport.fail = ConnectionError("refused")
    failed = service.author_watch(box.deps, {})
    assert failed["ok"] is False and failed["notify"] is True
    box.transport.fail = None
    box.deps.transport = growing
    recovered = service.author_watch(box.deps, {})
    assert recovered["notify"] is True
    assert recovered["new"] == [f"at://own/g{30 + k}" for k in range(30)]
    assert "cleared" in recovered["message"]
    assert "New post at://own/g30" in recovered["message"]
    assert "取りこぼし" not in recovered["message"]


def _repost_row(uri, indexed):
    return post(
        uri,
        did="did:aster",
        handle="aster.id",
        text="r",
        reason={"$type": "app.bsky.feed.defs#reasonRepost", "indexedAt": indexed, "by": {"did": DID}},
    )


def test_thirty_one_reposts_warn_once_then_go_quiet(box):
    save(box, seen=[], baseline_done=False)
    box.prove(True)
    baseline = [_repost_row(f"at://re/old{i}", f"2020-01-01T00:00:{i:02d}.000Z") for i in range(2)]
    box.transport.routes = {"getAuthorFeed": (200, {"feed": baseline})}
    first = service.author_watch(box.deps, {})
    assert first["new"] == []
    state = json.loads(box.path.read_text())
    assert state["seen"] == [] and state["baseline_done"] is True
    assert state["marks"] == [service.repost_mark(item) for item in baseline]
    same = service.author_watch(box.deps, {})
    assert same["notify"] is False
    assert same["message"] == "No new posts."
    assert "取りこぼし" not in same["message"]
    added = [_repost_row(f"at://re/new{i}", f"2026-02-01T00:00:{i:02d}.000Z") for i in range(31)]
    pages = {}
    cursor = ""
    for number in range(3):
        pages[cursor] = {"feed": added[number * 10:(number + 1) * 10], "cursor": f"p{number + 1}"}
        cursor = f"p{number + 1}"
    pages["p3"] = {"feed": [added[30], baseline[0]]}
    _paged(box, pages)
    box.transport.urls.clear()
    got = service.author_watch(box.deps, {})
    assert len(box.transport.urls) == service.MAX_WATCH_PAGES
    assert got["new"] == []
    assert got["notify"] is True
    assert "取りこぼしの可能性" in got["message"]
    quiet = service.author_watch(box.deps, {})
    assert quiet["notify"] is False
    assert quiet["message"] == "No new posts."
    assert "取りこぼし" not in quiet["message"]


def test_own_post_behind_thirty_one_reposts_warns_once(box):
    save(box, seen=[], baseline_done=True, marks=[])
    box.prove(True)
    reposts = [_repost_row(f"at://re/n{i}", f"2026-03-01T00:00:{i:02d}.000Z") for i in range(31)]
    hidden = post("at://own/x", text="hidden")
    pages = {}
    cursor = ""
    rows = reposts + [hidden]
    for number in range(4):
        chunk = rows[number * 10:(number + 1) * 10]
        nxt = f"p{number + 1}" if number < 3 else ""
        body = {"feed": chunk}
        if nxt:
            body["cursor"] = nxt
        pages[cursor] = body
        cursor = nxt
    _paged(box, pages)
    box.transport.urls.clear()
    first = service.author_watch(box.deps, {})
    assert len(box.transport.urls) == service.MAX_WATCH_PAGES
    assert first["notify"] is True
    assert first["new"] == []
    assert "取りこぼしの可能性" in first["message"]
    assert "at://own/x" not in json.loads(box.path.read_text())["seen"]
    second = service.author_watch(box.deps, {})
    assert second["notify"] is False
    assert second["message"] == "No new posts."
    assert "取りこぼし" not in second["message"]
    assert "at://own/x" not in json.loads(box.path.read_text())["seen"]


def test_five_new_reposts_at_a_time_do_not_warn(box):
    anchor = _repost_row("at://re/anchor", "2020-01-01T00:00:00.000Z")
    save(box, seen=[], baseline_done=True, marks=[service.repost_mark(anchor)])
    box.prove(True)
    times = {"n": 1_700_000_000.0, "extra": 0}
    box.deps.now = lambda: times["n"]

    def feed():
        head = [_repost_row(f"at://re/g{i}", f"2026-04-01T00:00:{i:02d}.000Z") for i in range(times["extra"])]
        return {"feed": head + [anchor], "cursor": "more"}

    box.transport.routes = {"getAuthorFeed": (200, feed())}
    for _ in range(3):
        times["extra"] += 5
        box.transport.routes = {"getAuthorFeed": (200, feed())}
        got = service.author_watch(box.deps, {})
        assert got["notify"] is False
        assert got["message"] == "No new posts."
        assert "取りこぼし" not in got["message"]
    same = service.author_watch(box.deps, {})
    assert same["notify"] is False and "取りこぼし" not in same["message"]
    times["n"] += service.NOTIFY_GAP
    later = service.author_watch(box.deps, {})
    assert later["notify"] is False
    assert later["message"] == "No new posts."
    assert "取りこぼし" not in later["message"]


def test_a_post_above_a_stored_mark_is_announced(box):
    anchor = _repost_row("at://re/anchor", "2020-01-01T00:00:00.000Z")
    save(box, seen=["at://own/old"], baseline_done=True, marks=[service.repost_mark(anchor)])
    box.prove(True)
    head = [_repost_row(f"at://re/h{i}", f"2026-05-01T00:00:{i:02d}.000Z") for i in range(12)]
    page1 = head[:10]
    page2 = head[10:] + [post("at://own/x", text="seen me"), anchor]
    _paged(box, {"": {"feed": page1, "cursor": "p2"}, "p2": {"feed": page2}})
    got = service.author_watch(box.deps, {})
    assert got["new"] == ["at://own/x"]
    assert got["notify"] is True
    assert "取りこぼし" not in got["message"]
    assert "at://own/x" in got["message"]


def test_old_state_without_marks_still_runs(box):
    save(box, seen=["at://own/old"], baseline_done=True)
    raw = json.loads(box.path.read_text())
    assert "marks" not in raw
    box.prove(True)
    box.transport.routes = {"getAuthorFeed": (200, {"feed": [post("at://own/old"), post("at://own/new", text="n")]})}
    got = service.author_watch(box.deps, {})
    assert got["new"] == ["at://own/new"]
    assert "取りこぼし" not in got["message"]
    stored = json.loads(box.path.read_text())
    assert stored["seen"] == ["at://own/old", "at://own/new"]
    assert isinstance(stored.get("marks"), list)
    pages = {}
    cursor = ""
    for number in range(3):
        pages[cursor] = {"feed": _reposts(10, start=number * 10), "cursor": f"p{number + 1}"}
        cursor = f"p{number + 1}"
    _paged(box, pages)
    gap = service.author_watch(box.deps, {})
    assert gap["notify"] is True
    assert "取りこぼしの可能性" in gap["message"]
    assert "at://own/old" in json.loads(box.path.read_text())["seen"]
    again = service.author_watch(box.deps, {})
    assert again["notify"] is False
    assert "取りこぼし" not in again["message"]


def test_manual_then_host_cron_still_notifies(box, monkeypatch):
    save(box, seen=["at://own/1"], baseline_done=True, job_id="job1")
    box.cron.jobs.append({"id": "job1", "name": "author-feed"})
    box.transport.routes = {"getAuthorFeed": (200, feed_of(post("at://own/1"), post("at://own/2", text="new")))}
    unset = object()
    var = contextvars.ContextVar("HERMES_CRON_SESSION", default=unset)
    mod = types.ModuleType("gateway.session_context")
    mod._UNSET = unset
    mod.HERMES_CRON_SESSION = var
    monkeypatch.setitem(__import__("sys").modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(__import__("sys").modules, "gateway.session_context", mod)
    monkeypatch.setattr(gates, "cron_proven", _CRON)
    manual = service.author_watch(box.deps, {})
    assert manual["notify"] is True and "cannot monitor" in manual["message"]
    assert box.transport.urls == []
    var.set("1")
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    cron_run = service.author_watch(box.deps, {})
    assert cron_run["notify"] is True
    assert cron_run["message"] == manual["message"]
    assert box.transport.urls == []
    assert json.loads(box.path.read_text())["seen"] == ["at://own/1"]


def test_baseline_says_when_three_pages_were_not_the_end(box):
    save(box, seen=[], baseline_done=False)
    box.prove(True)
    pages = {}
    cursor = ""
    for number in range(4):
        pages[cursor] = {"feed": _reposts(10, start=number * 10), "cursor": f"p{number + 1}"}
        cursor = f"p{number + 1}"
    _paged(box, pages)
    got = service.author_watch(box.deps, {})
    assert len(box.transport.urls) == service.MAX_WATCH_PAGES
    assert got["notify"] is True and got["new"] == []
    assert "no post by this account" in got["message"]
    assert "3 pages were read and the feed offered more" in got["message"]
    state = json.loads(box.path.read_text())
    assert state["baseline_done"] is True and state["seen"] == []


def test_baseline_walks_past_a_page_of_reposts(box):
    save(box, seen=[], baseline_done=False)
    box.prove(True)
    _paged(box, {
        "": {"feed": _reposts(10), "cursor": "p2"},
        "p2": {"feed": [post("at://own/1", text="first")]},
    })
    got = service.author_watch(box.deps, {})
    assert got["new"] == []
    assert "not announced" in got["message"]
    assert json.loads(box.path.read_text())["seen"] == ["at://own/1"]
    assert json.loads(box.path.read_text())["baseline_done"] is True


def test_long_intervals_and_long_actors_are_rejected(box):
    box.cron.jobs.append({"id": "job1", "name": "author-feed", "schedule": "every 60m", "deliver": "local"})
    save(box, job_id="job1")
    for every in ("every 100000 hours", "every 10095 minutes", "every 8 days"):
        code, text = service.schedule(box.deps, "bsky.app", "local", every)
        assert code == 1, every
        assert box.transport.urls == []
        assert "rejected" in text or "not understood" in text
    box.transport.routes = {"getProfile": (200, profile())}
    code, text = service.schedule(box.deps, "bsky.app", "local", "every 7 days")
    assert code == 1 and "not understood" in text
    code, text = service.schedule(box.deps, "bsky.app", "local", "every 168 hours")
    assert code == 0 and "every 168 hours" in text
    assert json.loads(box.path.read_text())["interval_words"] == "every 168 hours"
    code, text = service.schedule(box.deps, "bsky.app", "local", None)
    assert code == 0 and "every 168 hours" in text
    code, text = service.schedule(box.deps, "bsky.app", "local", "every 120 minutes")
    assert code == 0 and "every 2 hours" in text
    long_actor = "a" * (service.ACTOR_CAP + 1)
    before = len(box.transport.urls)
    assert service.profile_get(box.deps, {"actor": long_actor})["ok"] is False
    assert service.author_feed(box.deps, {"actor": long_actor})["ok"] is False
    code, text = service.schedule(box.deps, long_actor, "local", None)
    assert code == 1 and "253 characters" in text
    assert len(box.transport.urls) == before


def test_a_target_outside_this_machine_gets_a_caution(box):
    box.transport.routes = {"getProfile": (200, profile())}
    code, text = service.schedule(box.deps, "bsky.app", "local", None)
    assert code == 0 and "is on the Hermes list" not in text
    code, text = service.schedule(box.deps, "bsky.app", "telegram", None)
    assert code == 0
    assert "The name telegram is on the Hermes list of delivery platforms" in text
    assert "the report is dropped and you never see it" in text


def test_changing_the_account_says_what_was_cleared(box):
    box.cron.jobs.append({"id": "job1", "name": "author-feed", "schedule": "every 60m", "deliver": "local"})
    save(box, job_id="job1", seen=["at://own/1", "at://own/2"], handle="bsky.app")
    box.transport.routes = {"getProfile": (200, profile(did=OTHER, handle="other.example"))}
    code, text = service.schedule(box.deps, "other.example", "local", None)
    assert code == 0
    assert f"Replaced watched account bsky.app ({DID}) with other.example ({OTHER})" in text
    assert "2 saved post URIs were cleared" in text
    assert "stores a new baseline without announcing it" in text
    assert json.loads(box.path.read_text())["seen"] == []


def test_printed_hermes_lines_carry_a_heading(box):
    box.cron.jobs.append({"id": "job1", "name": "author-feed", "schedule": "every 60m", "deliver": "local"})
    save(box, job_id="job1")
    box.transport.routes = {"getProfile": (200, profile())}
    code, text = service.schedule(box.deps, "bsky.app", "local", None)
    assert code == 0
    assert "Hermes commands for this job, for you to run:" in text
    assert not text.startswith("hermes cron")
    code, text = service.status(box.deps)
    assert "Hermes commands for this job, for you to run:" in text
    assert text.startswith("handle ")


def test_a_trickle_is_cut_off_by_the_total_deadline(monkeypatch):
    if _network_blocked():
        pytest.skip("socket connect is blocked")
    monkeypatch.setattr(client, "TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(client, "TOTAL_SECONDS", 1)

    class Drip(BaseHTTPRequestHandler):
        status = 200

        def do_GET(self):  # noqa: N802
            payload = b'{"did":"did:plc:drip","handle":"drip.example","error":"Slow"}' + b" " * 80
            self.send_response(self.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload[:4])
                self.wfile.flush()
            except OSError:
                return
            time.sleep(8)
            try:
                self.wfile.write(payload[4:])
                self.wfile.flush()
            except OSError:
                return

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(client, "API_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    started = time.perf_counter()
    try:
        with pytest.raises(client.ClientError) as caught:
            client.fetch(client.PROFILE_PATH, [("actor", "drip.example")])
    finally:
        server.shutdown()
    elapsed = time.perf_counter() - started
    assert caught.value.code == "timeout"
    assert elapsed < 4


def test_a_trickling_error_body_is_cut_off_by_the_total_deadline(monkeypatch):
    if _network_blocked():
        pytest.skip("socket connect is blocked")
    monkeypatch.setattr(client, "TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(client, "TOTAL_SECONDS", 1)

    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            payload = b'{"error":"Slow","message":"drip"}' + b" " * 80
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload[:4])
                self.wfile.flush()
            except OSError:
                return
            time.sleep(8)
            try:
                self.wfile.write(payload[4:])
                self.wfile.flush()
            except OSError:
                return

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(client, "API_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    started = time.perf_counter()
    try:
        with pytest.raises(client.ClientError) as caught:
            client.fetch(client.PROFILE_PATH, [("actor", "drip.example")])
    finally:
        server.shutdown()
    elapsed = time.perf_counter() - started
    assert caught.value.code == "timeout"
    assert elapsed < 4


def test_a_slow_error_body_closes_the_socket(monkeypatch):
    if _network_blocked():
        pytest.skip("socket connect is blocked")
    monkeypatch.setattr(client, "TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(client, "TOTAL_SECONDS", 1)
    closed = threading.Event()

    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            payload = b'{"error":"Upstream","message":"unavailable"}' + b" " * 400
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            for index in range(0, len(payload), 4):
                try:
                    self.wfile.write(payload[index:index + 4])
                    self.wfile.flush()
                except OSError:
                    closed.set()
                    return
                time.sleep(0.05)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(client, "API_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    try:
        with pytest.raises(client.ClientError) as caught:
            client.fetch(client.PROFILE_PATH, [("actor", "drip.example")])
        assert closed.wait(1.0)
    finally:
        server.shutdown()
    assert caught.value.code == "timeout"


def test_a_slow_header_is_inside_the_total_deadline(monkeypatch):
    if _network_blocked():
        pytest.skip("socket connect is blocked")
    monkeypatch.setattr(client, "TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(client, "TOTAL_SECONDS", 1)

    class SlowHeader(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            time.sleep(3)
            try:
                payload = b'{"did":"did:plc:slow","handle":"slow.example"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
            except OSError:
                return

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHeader)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(client, "API_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    started = time.perf_counter()
    try:
        with pytest.raises(client.ClientError) as caught:
            client.fetch(client.PROFILE_PATH, [("actor", "slow.example")])
    finally:
        server.shutdown()
    elapsed = time.perf_counter() - started
    assert caught.value.code == "timeout"
    assert elapsed < 2.5
    time.sleep(0.2)
    assert [item for item in threading.enumerate() if item.name == "author-feed-get" and item.is_alive()] == []


def test_omitted_every_keeps_the_saved_interval(box):
    box.cron.jobs.append({
        "id": "job1",
        "name": "author-feed",
        "schedule": "every 15m",
        "deliver": "local",
    })
    save(box, interval_words="every 15 minutes", deliver="local", job_id="job1")
    box.transport.routes = {"getProfile": (200, profile())}
    code, text = service.schedule(box.deps, "bsky.app", "local", None)
    assert code == 0
    assert "every 15 minutes" in text
    state = json.loads(box.path.read_text())
    assert state["interval_words"] == "every 15 minutes"
    assert box.cron.jobs[0]["schedule"] == "every 15m"
    assert "hermes cron run job1" in text
    assert "hermes cron remove job1" in text
    assert "cannot be stopped" not in service.CRON_PROMPT


def test_watch_says_the_job_is_gone_and_does_not_fetch(box):
    save(box, seen=["at://own/1"], baseline_done=True)
    box.prove(True)
    box.cron.jobs.clear()
    box.transport.routes = {"getAuthorFeed": (200, feed_of(post("at://own/2", text="new")))}
    got = service.author_watch(box.deps, {})
    assert got["ok"] is False
    assert "Job job1 is gone" in got["message"]
    assert "Seen URIs were left unchanged" in got["message"]
    assert box.transport.urls == []
    assert json.loads(box.path.read_text())["seen"] == ["at://own/1"]
    box.prove(False)
    again = service.author_watch(box.deps, {})
    assert "Job job1 is gone" in again["message"]
    assert box.transport.urls == []


def test_status_and_unschedule_when_the_job_was_removed(box):
    save(box, job_id="job1", seen=["at://own/1"])
    code, text = service.status(box.deps)
    assert code == 0 and "job job1 is gone" in text
    assert "cron run" not in text and "cron remove" not in text
    assert "hermes cron create" in text and "hermes cron list" in text and "hermes cron status" in text
    code, text = service.unschedule(box.deps)
    assert code == 0 and "already gone" in text
    state = json.loads(box.path.read_text())
    assert state["job_id"] is None and state["seen"] == ["at://own/1"]
    code, text = service.status(box.deps)
    assert code == 0 and "job none" in text and "is gone" not in text
    assert "cron run" not in text and "cron remove" not in text


def test_seen_cap_lookup_on_sixty_thousand_is_fast(box):
    uris = [f"at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.post/{i}" for i in range(60000)]
    save(box, seen=uris, baseline_done=True)
    box.prove(True)
    newest = uris[-1]
    box.transport.routes = {"getAuthorFeed": (200, feed_of(post(newest, text="n")))}
    started = time.perf_counter()
    got = service.author_watch(box.deps, {})
    elapsed = time.perf_counter() - started
    assert got["notify"] is False and got["new"] == []
    stored = json.loads(box.path.read_text())["seen"]
    assert len(stored) == service.SEEN_CAP
    assert stored[-1] == newest
    assert elapsed < 5
    print(f"SEEN_60K_SECONDS {elapsed:.3f}")


def test_drip_response_can_outlast_one_quiet_gap(monkeypatch):
    if _network_blocked():
        pytest.skip("socket connect is blocked")
    monkeypatch.setattr(client, "TIMEOUT_SECONDS", 1)

    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            payload = b'{"did":"did:plc:drip","handle":"drip.example"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            for index in range(0, len(payload), 8):
                self.wfile.write(payload[index:index + 8])
                self.wfile.flush()
                time.sleep(0.4)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setattr(client, "API_BASE", f"http://127.0.0.1:{port}")
    started = time.perf_counter()
    try:
        data = client.fetch(client.PROFILE_PATH, [("actor", "drip.example")])
    finally:
        server.shutdown()
    elapsed = time.perf_counter() - started
    assert data["did"] == "did:plc:drip"
    assert elapsed > 1


def test_redirect_http_server_is_not_followed(monkeypatch):
    if _network_blocked():
        pytest.skip("socket connect is blocked")
    landed: list[str] = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            landed.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"did":"nope"}')

        def log_message(self, *_args):
            return

    class Origin(BaseHTTPRequestHandler):
        auth = None

        def do_GET(self):  # noqa: N802
            Origin.auth = self.headers.get("Authorization")
            self.send_response(int(self.server.kind))
            self.send_header("Location", self.server.location)
            self.end_headers()

        def log_message(self, *_args):
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    origin_srv = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threads = [threading.Thread(target=target.serve_forever, daemon=True), threading.Thread(target=origin_srv.serve_forever, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        monkeypatch.setattr(client, "API_BASE", f"http://127.0.0.1:{origin_srv.server_address[1]}")
        for code in (301, 302, 303, 307, 308):
            origin_srv.kind = str(code)
            origin_srv.location = f"http://127.0.0.1:{target.server_address[1]}/landed"
            with pytest.raises(client.ClientError) as caught:
                client.fetch(client.PROFILE_PATH, [("actor", "bsky.app")])
            assert caught.value.code == "redirect"
            assert caught.value.status == code
        assert landed == []
        assert Origin.auth is None
    finally:
        origin_srv.shutdown()
        target.shutdown()


def test_registered_tool_results_are_json_strings(monkeypatch):
    monkeypatch.setattr(gates, "cron_proven", lambda: False)
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("author_feed_plugin_reg", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.__package__ = None
    captured = {}

    class Ctx:
        def register_tool(self, **kwargs):
            captured[kwargs["name"]] = kwargs["handler"]

        def register_cli_command(self, **kwargs):
            return None

    mod.register(Ctx())
    raw = captured["author_watch"]({})
    assert isinstance(raw, str)
    body = json.loads(raw)
    assert body["notify"] is True
    assert "cannot monitor" in body["message"]


def test_prompt_tools_and_readme_phrases():
    assert "author_watch" in service.CRON_PROMPT
    assert "[SILENT]" in service.CRON_PROMPT
    assert "interpreter" not in service.CRON_PROMPT
    assert "no_agent" not in service.CRON_PROMPT
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    plugin = (root / "plugin.yaml").read_text(encoding="utf-8")
    assert "built on the public AppView at public.api.bsky.app" in readme
    assert "hermes tools" in readme
    assert "slash" in readme.lower()
    assert "pip install" not in readme
    assert "requires_hermes: '>=0.21.4'" in plugin or 'requires_hermes: ">=0.21.4"' in plugin
    assert "manifest_version: 2" in plugin
    text = (root / "__init__.py").read_text(encoding="utf-8")
    assert "register_command" not in text
    assert "register_hook" not in text
    try:
        import yaml
    except Exception:
        return
    desc = yaml.safe_load(plugin)["description"]
    # Facts the catalog text must carry. The wording itself is free to change.
    assert len(desc) <= 1500
    assert desc.count("Disclosure —") == 1
    lead, disclosure = desc.split("Disclosure —", 1)
    for tool in ("profile_get", "author_feed", "author_watch"):
        assert tool in lead
    assert "public.api.bsky.app" in disclosure
    assert "hermes cron remove" in disclosure
    assert "no daily cap" in disclosure
    assert "plugins.isolation: host" in disclosure
    assert "hermes tools" in disclosure and "gateway" in disclosure
    assert "plugin-data/author-feed" in disclosure
    assert "on every run" in desc
    assert "when it recovers" not in desc
    assert "latest 3 pages" in desc
    assert "possible-miss" in desc
    assert "at most once a day" not in desc
    assert "2026-10-09" in readme
    assert "hermes cron remove <id>" in readme
    assert "HERMES_CRON_SESSION" not in desc
    assert "host process" not in desc
    assert "until you run unschedule" not in desc


def test_delivery_allowlist_without_hardcoded_platform():
    assert gates.canonical_deliver("origin", None) == "origin"
    assert gates.canonical_deliver("local,all", None) == "local,all"
    assert gates.canonical_deliver("telegram", None) is None
    assert gates.canonical_deliver("bot-chat", None) is None
    allowed = {"telegram"}
    assert gates.canonical_deliver("telegram", allowed) == "telegram"
    assert gates.canonical_deliver("bot-chat:room", allowed) == "bot-chat:room"
    assert gates.canonical_deliver("cli", allowed) is None
    assert gates.canonical_deliver("api_server", allowed) is None
    assert gates.canonical_deliver("every", allowed) is None
    assert gates.canonical_deliver("weekdays", allowed) is None
    assert "homeassistant" not in Path(gates.__file__).read_text(encoding="utf-8")
