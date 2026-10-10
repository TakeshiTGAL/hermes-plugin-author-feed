"""Profile read, feed read, and one saved-account watch."""
from __future__ import annotations

import inspect
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

if __package__:
    from . import client, gates
else:
    import client
    import gates

TOOLSET = "author_feed"
JOB_NAME = "author-feed"
STATE_NAME = "state.json"
NOTIFY_GAP = 24 * 60 * 60
TEXT_CAP = 300
LABEL_CAP = 20
LABEL_LEN = 100
PAGE_SIZE = 10
MAX_WATCH_PAGES = 3
SEEN_CAP = 2000
ACTOR_CAP = 253
MAX_INTERVAL_MINUTES = 7 * 24 * 60
SELF_TARGETS = frozenset({"origin", "local", "all"})
PROFILE_KEYS = ("did", "handle", "displayName", "followersCount", "followsCount", "postsCount")
REASON_REPOST = "app.bsky.feed.defs#reasonRepost"
REASON_PIN = "app.bsky.feed.defs#reasonPin"
CRON_PROMPT = (
    "Call author_watch with an empty argument object only. "
    "If notify is true, reply with the message field only. "
    "If notify is false, reply with [SILENT]. "
    "If the tool is missing, say the watch is not running. "
    "Do not search and do not complete post text."
)
MISS_MESSAGE = (
    "These pages were all new rows, and the feed still offered more, so a post may have been missed. "
    "取りこぼしの可能性."
)
_CRON_EXPR = re.compile(r"^[A-Za-z0-9*@,\-/]+$")
_EVERY = re.compile(
    r"^(?:every\s+)?(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?$",
    re.IGNORECASE,
)


class Deps:
    def __init__(self) -> None:
        self.transport: client.Transport | None = None
        self.now: Callable[[], float] | None = None
        self.cron: Any | None = None
        self.data_dir: Path | None = None


def _now(deps: Deps) -> float:
    if deps.now is not None:
        return float(deps.now())
    return time.time()


def _clock_dir(deps: Deps) -> Path:
    if deps.data_dir is not None:
        path = deps.data_dir
    else:
        path = gates.plugin_data_dir()
    if not isinstance(path, Path):
        path = Path(path)
    return path


def state_path(deps: Deps) -> Path:
    directory = _clock_dir(deps)
    # The file name is fixed. User text is never joined onto this path.
    return directory / STATE_NAME


def _result(ok: bool, message: str, *, notify: bool | None = None, next_step: str = "", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "message": message}
    if notify is not None:
        out["notify"] = notify
    if next_step:
        out["next_step"] = next_step
    out.update(extra)
    return out


def _fail(message: str, *, notify: bool | None = None, next_step: str = "") -> dict[str, Any]:
    return _result(False, message, notify=notify, next_step=next_step or "Nothing was saved.")


def load_state(deps: Deps) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    """Return (state, raw, error). error is 'missing', 'corrupt', 'unreadable', or None."""
    try:
        path = state_path(deps)
    except Exception:
        return None, None, "unreadable"
    try:
        if not path.is_file():
            return None, None, "missing"
        raw = path.read_bytes()
    except Exception:
        return None, None, "unreadable"
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, raw, "corrupt"
    if not isinstance(data, dict) or not isinstance(data.get("seen", []), list):
        return None, raw, "corrupt"
    if any(not isinstance(item, str) for item in data.get("seen", [])):
        return None, raw, "corrupt"
    return data, raw, None


def save_state(deps: Deps, data: dict[str, Any]) -> str | None:
    try:
        path = state_path(deps)
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return "The data folder could not be prepared, so the file was not saved."
    denied = gates.write_guard_error(str(path))
    if denied:
        return denied
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd = None
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
        os.write(fd, payload)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
        tmp = ""
    except Exception:
        return "The file could not be replaced, so the saved account was left as it was."
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return None


def _reason(item: dict[str, Any]) -> str | None:
    reason = item.get("reason")
    if reason is None:
        return None
    if not isinstance(reason, dict):
        return ""
    return str(reason.get("$type") or "")


def _labels(post: dict[str, Any]) -> list[str]:
    raw = post.get("labels")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for lab in raw[:LABEL_CAP]:
        if isinstance(lab, dict) and lab.get("val") is not None:
            out.append(str(lab["val"])[:LABEL_LEN])
        elif isinstance(lab, str):
            out.append(lab[:LABEL_LEN])
    return out


def classify_item(item: Any, actor: str) -> dict[str, Any]:
    post = item.get("post") if isinstance(item, dict) else None
    if not isinstance(post, dict):
        return {
            "kind": "other", "uri": "", "text": "", "author_handle": "", "author_did": "",
            "embed_type": "", "text_truncated": False, "labels": [],
        }
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    record = post.get("record") if isinstance(post.get("record"), dict) else {}
    text = record.get("text") if isinstance(record.get("text"), str) else ""
    truncated = len(text) > TEXT_CAP
    if truncated:
        text = text[:TEXT_CAP]
    embed = post.get("embed") if isinstance(post.get("embed"), dict) else {}
    embed_type = str(embed.get("$type") or "") if text == "" and embed.get("$type") else ""
    did = str(author.get("did") or "")
    handle = str(author.get("handle") or "")
    rtype = _reason(item) if isinstance(item, dict) else ""
    if rtype == REASON_REPOST:
        kind = "repost"
    elif rtype == REASON_PIN:
        kind = "pin"
    elif rtype is None and _same_actor(actor, did, handle):
        kind = "post"
    else:
        kind = "other"
    return {
        "kind": kind,
        "uri": str(post.get("uri") or ""),
        "text": text,
        "author_handle": handle,
        "author_did": did,
        "embed_type": embed_type,
        "text_truncated": truncated,
        "labels": _labels(post),
    }


def _same_actor(actor: str, did: str, handle: str) -> bool:
    if actor.startswith("did:"):
        return did == actor
    return did == actor or handle.lower() == actor.lower()


def own_post_uri(item: Any, saved_did: str) -> str:
    if not isinstance(item, dict) or _reason(item) is not None:
        return ""
    post = item.get("post") if isinstance(item.get("post"), dict) else {}
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    uri = str(post.get("uri") or "")
    if uri and str(author.get("did") or "") == saved_did:
        return uri
    return ""


def profile_view(data: dict[str, Any]) -> dict[str, Any]:
    return {key: data.get(key) for key in PROFILE_KEYS}


def _bad_actor(actor: Any) -> str | None:
    if not isinstance(actor, str) or not actor.strip():
        return "actor is required. Nothing was requested."
    if any(ch.isspace() for ch in actor):
        return "actor contains whitespace and was rejected before any request."
    if len(actor) > ACTOR_CAP:
        return (
            f"actor is longer than {ACTOR_CAP} characters, which no handle or DID is, "
            "and was rejected before any request."
        )
    return None


def _unknown(args: Any, allowed: set[str]) -> str | None:
    if not isinstance(args, dict):
        return "Arguments must be an object. Nothing was requested."
    extra = sorted(key for key in args if key not in allowed)
    if extra:
        return f"Unknown argument {extra[0]} was rejected before any request."
    return None


def _one(deps: Deps, path: str, pairs: list[tuple[str, str]]) -> dict[str, Any]:
    return client.fetch(path, pairs, deps.transport)


def profile_get(deps: Deps, args: dict[str, Any] | None, **_kwargs: Any) -> dict[str, Any]:
    problem = _unknown(args, {"actor"})
    if problem:
        return _fail(problem)
    actor = (args or {}).get("actor")
    problem = _bad_actor(actor)
    if problem:
        return _fail(problem)
    try:
        data = _one(deps, client.PROFILE_PATH, [("actor", actor)])
    except client.ClientError as exc:
        return _fail(exc.message, next_step="The saved account was not changed. This call is not sent again.")
    return _result(True, "Profile fetched.", profile=profile_view(data))


def author_feed(deps: Deps, args: dict[str, Any] | None, **_kwargs: Any) -> dict[str, Any]:
    problem = _unknown(args, {"actor"})
    if problem:
        return _fail(problem)
    actor = (args or {}).get("actor")
    problem = _bad_actor(actor)
    if problem:
        return _fail(problem)
    try:
        data = _one(
            deps,
            client.FEED_PATH,
            [("actor", actor), ("limit", str(PAGE_SIZE)), ("filter", "posts_no_replies")],
        )
    except client.ClientError as exc:
        return _fail(exc.message, next_step="Seen URIs were not changed. This call is not sent again.")
    feed = data["feed"]
    items = [classify_item(item, actor) for item in feed]
    message = (
        "Returned one page in API order. The request asks for 10 items. "
        "A page can be shorter than 10."
    )
    return _result(True, message, actor=actor, items=items)


def _apply_failure(state: dict[str, Any], code: str, now: float) -> bool:
    prev = state.get("failure") if isinstance(state.get("failure"), dict) else None
    notify = True
    if prev and prev.get("code") == code:
        notified_at = float(prev.get("notified_at") or 0)
        notify = (now - notified_at) >= NOTIFY_GAP
    state["failure"] = {
        "code": code,
        "at": now,
        "notified_at": now if notify else (prev or {}).get("notified_at"),
    }
    return notify


def _watch_failure(deps: Deps, state: dict[str, Any], exc: client.ClientError) -> dict[str, Any]:
    now = _now(deps)
    notify = _apply_failure(state, exc.code, now)
    _apply_notice(state, "blind", False, now)
    err = save_state(deps, state)
    if err:
        return _result(
            False,
            exc.message + " The failure note could not be saved. Seen URIs were left unchanged.",
            notify=True,
            next_step="This call is not sent again.",
        )
    prefix = "" if notify else "The same failure was already reported. "
    # The fetch itself wrote nothing. This watch then records the failure and leaves seen URIs.
    message = exc.message.replace(
        "Saved state is unchanged.",
        "Seen URIs were left unchanged. The failure was recorded.",
    ).replace("保存は変わっていない。", "既読は変わっていない。失敗は記録した。")
    return _result(False, prefix + message, notify=notify, next_step="This call is not sent again.")


def _apply_notice(state: dict[str, Any], key: str, active: bool, now: float) -> bool:
    """Notify on the first run, again when the condition returns, and once a day while it lasts.

    A standing condition would otherwise repeat the same line at every interval.
    """
    prev = state.get(key) if isinstance(state.get(key), dict) else None
    if not active:
        state[key] = None
        return False
    notify = True
    if prev:
        notified_at = float(prev.get("notified_at") or 0)
        notify = (now - notified_at) >= NOTIFY_GAP
    state[key] = {
        "at": now,
        "notified_at": now if notify else (prev or {}).get("notified_at"),
    }
    return notify


def _cannot_monitor(deps: Deps) -> dict[str, Any]:
    """The same wording on every unconfirmed run. Nothing is stored.

    Under plugins.isolation: host a manual call and the cron job are the same
    shape, so a quiet repeat would hide the owner's first notice for a day.
    """
    return _result(
        False,
        gates.CANNOT_MONITOR,
        notify=True,
        next_step="Leave the saved URIs unchanged.",
    )


def _baseline_message(own: list[str], reposts: int, more: bool) -> str:
    parts = ["Baseline stored. These posts are not announced as new."]
    if not own:
        parts.append("These pages had no post by this account.")
        if reposts:
            parts.append("Reposts were not stored as this account's posts.")
    if more:
        parts.append(f"{MAX_WATCH_PAGES} pages were read and the feed offered more, which were not fetched.")
    return " ".join(parts)


def _announce(feed: list[Any], uris: list[str], saved_did: str, *, later: set[str]) -> str:
    by_uri = {}
    for item in feed:
        uri = own_post_uri(item, saved_did)
        if uri:
            by_uri[uri] = classify_item(item, saved_did)
    parts = []
    for uri in uris:
        if uri in later:
            parts.append("This post may be older than the first stored page.")
        item = by_uri.get(uri) or {}
        text = item.get("text") or ""
        embed = item.get("embed_type") or ""
        if text:
            parts.append(f"New post {uri}: {text}")
        elif embed:
            parts.append(f"New post {uri}: embed_type {embed}")
        else:
            parts.append(f"New post {uri}")
    return " ".join(parts)


def _cap_seen(uris: list[str]) -> list[str]:
    """Keep the newest SEEN_CAP URIs. Membership tests use a set."""
    found: set[str] = set()
    ordered: list[str] = []
    for uri in uris:
        if uri in found:
            continue
        found.add(uri)
        ordered.append(uri)
    if len(ordered) > SEEN_CAP:
        return ordered[-SEEN_CAP:]
    return ordered


def repost_mark(item: Any) -> str:
    """A repost row, not the original post. indexedAt plus the original URI.

    A later repost of a post already in `seen` is a different row, so this mark
    does not match that saved URI.
    """
    if not isinstance(item, dict) or _reason(item) != REASON_REPOST:
        return ""
    reason = item.get("reason") if isinstance(item.get("reason"), dict) else {}
    post = item.get("post") if isinstance(item.get("post"), dict) else {}
    uri = str(post.get("uri") or "")
    if not uri:
        return ""
    indexed = str(reason.get("indexedAt") or "")
    return f"repost\t{indexed}\t{uri}"


def _watch_pages(
    deps: Deps, actor: str, seen_set: set[str], mark_set: set[str]
) -> tuple[list[Any], bool, int, bool, set[str], list[str]]:
    """Read up to MAX_WATCH_PAGES. Stop when a stored continuation mark is on the page.

    A saved post URI counts. So does a repost row stored earlier, keyed by the
    reason's indexedAt and the original post URI. A new repost of an already
    seen post is not that row, and does not stop the walk.
    page_miss is true when another cursor was still offered and no saved post and no
    stored continuation mark was found. That unread gap is a miss even when no post
    by the account was on these pages. A new post by the account on the pages read
    is still announced. Only that announced post is stored as seen. The repost rows
    read are stored, so the next run overlaps and does not repeat the notice when
    those rows are still on the pages it reads. A run that finds a stored mark does not
    miss, including when nothing changed and when new rows only pushed a mark deeper.
    more is true when the page budget ran out while another page was still offered.
    The cursor is not stored.
    """
    items: list[Any] = []
    marks: list[str] = []
    reposts = 0
    cursor = ""
    used: set[str] = set()
    overlapped = False
    more = False
    later: set[str] = set()
    page_no = 0
    for _ in range(MAX_WATCH_PAGES):
        page_no += 1
        pairs = [("actor", actor), ("limit", str(PAGE_SIZE)), ("filter", "posts_no_replies")]
        if cursor:
            pairs.append(("cursor", cursor))
        data = _one(deps, client.FEED_PATH, pairs)
        feed = data["feed"]
        if not isinstance(feed, list):
            feed = []
        items.extend(feed)
        for item in feed:
            mark = repost_mark(item)
            if mark:
                reposts += 1
                marks.append(mark)
                if mark in mark_set:
                    overlapped = True
            own = own_post_uri(item, actor)
            if own and page_no >= 2:
                later.add(own)
            if own and own in seen_set:
                overlapped = True
        if overlapped:
            more = False
            break
        # A short page is not the last page. getAuthorFeed drops filtered rows after the
        # limit is applied, so 9 items with a cursor is normal. Only a missing cursor ends it.
        nxt = data.get("cursor")
        if not isinstance(nxt, str) or not nxt.strip() or nxt in used:
            more = False
            break
        used.add(nxt)
        cursor = nxt
        more = True
    # A miss is only possible when pages were left unread. If the feed ran out of
    # cursors, nothing slipped past: those posts are gone from the feed.
    page_miss = not overlapped and more
    return items, page_miss, reposts, more, later, marks


def _saved_job_gone(deps: Deps, state: dict[str, Any] | None) -> str | None:
    """A saved id that cron no longer has. Checked before the cron-proof gate.

    A tool call after `hermes cron remove` is not a cron session, so the gone
    sentence has to come out before "cannot monitor".
    """
    if not isinstance(state, dict):
        return None
    job_id = str(state.get("job_id") or "").strip()
    if not job_id:
        return None
    try:
        mod = _cron_module(deps)
    except Exception:
        return None
    if _job_by_id(mod, job_id) is None:
        return (
            f"Job {job_id} is gone. Nothing was requested. "
            "Seen URIs were left unchanged."
        )
    return None


def author_watch(deps: Deps, args: dict[str, Any] | None, **_kwargs: Any) -> dict[str, Any]:
    problem = _unknown(args or {}, set())
    if problem:
        return _fail(problem, notify=True, next_step="Call author_watch with an empty argument object.")
    state, _raw, err = load_state(deps)
    # A tool call after `hermes cron remove` is not a cron session, so the gone
    # sentence has to come out before "cannot monitor".
    gone = _saved_job_gone(deps, state if isinstance(state, dict) else None)
    if gone:
        return _result(False, gone, notify=True, next_step="The saved file was left in place.")
    if not gates.cron_proven():
        return _cannot_monitor(deps)
    if err == "corrupt":
        return _result(
            False,
            "The saved file could not be read, so it was not replaced. Seen URIs were left unchanged.",
            notify=True,
            next_step="Do not overwrite the file.",
        )
    if err == "unreadable":
        return _result(
            False,
            "The data folder could not be read, so nothing was requested. Seen URIs were left unchanged.",
            notify=True,
        )
    if err == "missing" or not state or not state.get("did"):
        return _result(
            False,
            "No saved account. Run hermes author-feed schedule. Nothing was requested.",
            notify=True,
            next_step="Schedule from the local CLI.",
        )
    saved_did = str(state.get("did") or "")
    prev = [item for item in state.get("seen") or [] if isinstance(item, str)]
    seen_set = set(prev)
    raw_marks = state.get("marks")
    prev_marks = [item for item in raw_marks if isinstance(item, str)] if isinstance(raw_marks, list) else []
    try:
        feed, page_miss, reposts, more, later, marks = _watch_pages(
            deps,
            saved_did,
            seen_set if state.get("baseline_done") else set(),
            set(prev_marks) if state.get("baseline_done") else set(),
        )
    except client.ClientError as exc:
        return _watch_failure(deps, state, exc)
    own: list[str] = []
    for item in feed:
        uri = own_post_uri(item, saved_did)
        if uri and uri not in own:
            own.append(uri)
    now = _now(deps)
    recovery = bool(state.get("failure"))
    if not state.get("baseline_done"):
        state["seen"] = _cap_seen(own)
        state["marks"] = _cap_seen(marks)
        state["baseline_done"] = True
        state["failure"] = None
        state["miss"] = None
        state["blind"] = None
        state["last_success_at"] = now
        err = save_state(deps, state)
        if err:
            return _result(False, err + " Seen URIs were left unchanged.", notify=True)
        return _result(True, _baseline_message(own, reposts, more), notify=True, new=[])
    fresh = [uri for uri in own if uri not in seen_set]
    # An unread gap is a miss even when these pages held no post by the account.
    # The warning does not replace an announcement: a post on these pages is still announced.
    # Only an announced post is stored. A post past the pages read is not in fresh.
    missed = page_miss
    miss_notify = _apply_notice(state, "miss", missed, now)
    _apply_notice(state, "blind", False, now)
    state["seen"] = _cap_seen(prev + fresh)
    state["marks"] = _cap_seen(prev_marks + marks)
    state["failure"] = None
    state["last_success_at"] = now
    err = save_state(deps, state)
    if err:
        return _result(False, err + " Seen URIs were left unchanged.", notify=True)
    if missed and not miss_notify and not fresh and not recovery:
        return _result(True, "The same possible miss was already reported.", notify=False, new=[])
    if not fresh and not recovery and not missed:
        return _result(True, "No new posts.", notify=False, new=[])
    parts = []
    if recovery:
        parts.append("The earlier failure cleared on this check.")
    if fresh:
        parts.append(_announce(feed, fresh, saved_did, later=later))
    if missed and miss_notify:
        parts.append(MISS_MESSAGE)
    elif missed and not fresh:
        parts.append("The same possible miss was already reported.")
    notify = bool(fresh or recovery or (missed and miss_notify))
    return _result(True, " ".join(part for part in parts if part), notify=notify, new=fresh)


def _cron_help(job_id: str | None) -> str:
    lines = gates.hermes_cron_lines(job_id=job_id)
    return "Hermes commands for this job, for you to run:\n" + "\n".join(lines)


def _deliver_caution(canon: str) -> str:
    """Hermes checks the name against a fixed list, not against what this machine has set up."""
    outside = [part for part in canon.split(",") if part.split(":", 1)[0] not in SELF_TARGETS]
    if not outside:
        return ""
    return (
        f" The name {outside[0]} is on the Hermes list of delivery platforms, but nothing checks that "
        "you have set that platform up. If it is not set up, the report is dropped and you never see it."
    )


def _cron_module(deps: Deps) -> Any:
    if deps.cron is not None:
        return deps.cron
    import cron.jobs as jobs
    return jobs


def _hermes_schedule(minutes: int) -> str:
    return f"every {minutes}m"


def parse_interval(text: str | None) -> tuple[int, str] | str:
    if text is None or str(text).strip() == "":
        return 60, "every hour"
    raw = str(text).strip()
    if _is_cron_expr(raw):
        return "A cron expression is rejected. Use every hour or every 15 minutes. Nothing was requested."
    lowered = re.sub(r"^every\s+", "", raw.strip(), flags=re.IGNORECASE)
    if lowered.lower() in {"hour", "1 hour", "an hour"}:
        return 60, "every hour"
    match = _EVERY.fullmatch(lowered)
    if not match:
        return "The interval was not understood. Use every hour or every 15 minutes. Nothing was requested."
    count = int(match.group(1))
    unit = (match.group(2) or "m").lower()
    minutes = count * 60 if unit.startswith("h") else count
    if minutes < 15 or minutes % 15 != 0:
        return (
            "Intervals faster than every 15 minutes, or not a multiple of 15 minutes, are rejected. "
            "Nothing was requested."
        )
    if minutes > MAX_INTERVAL_MINUTES:
        return "Intervals longer than every 7 days are rejected. Nothing was requested."
    return minutes, _interval_words(minutes)


def _interval_words(minutes: int) -> str:
    """Words a person reads. A typo of 6000000m should not print as minutes."""
    if minutes == 60:
        return "every hour"
    if minutes % 60 == 0:
        return f"every {minutes // 60} hours"
    return f"every {minutes} minutes"


def _is_cron_expr(raw: str) -> bool:
    if "*" in raw or raw.startswith("@") or "/" in raw:
        return True
    parts = raw.split()
    if len(parts) >= 5 and all(_CRON_EXPR.fullmatch(part) for part in parts):
        return True
    return False


def _job_by_id(mod: Any, job_id: str) -> dict[str, Any] | None:
    getter = getattr(mod, "get_job", None)
    if not callable(getter):
        return None
    try:
        job = getter(job_id)
    except Exception:
        return None
    return job if isinstance(job, dict) else None


def _named_job_ids(mod: Any) -> list[str] | None:
    loader = getattr(mod, "load_jobs", None)
    if not callable(loader):
        return None
    try:
        jobs = loader()
    except Exception:
        return None
    if not isinstance(jobs, list):
        return None
    return [str(job.get("id")) for job in jobs if isinstance(job, dict) and job.get("name") == JOB_NAME and job.get("id")]


def _create_job(mod: Any, minutes: int, deliver: str) -> tuple[str | None, str | None]:
    create = getattr(mod, "create_job", None)
    if not callable(create):
        return None, "The cron job was not created because cron.jobs could not be loaded."
    kwargs = {
        "prompt": CRON_PROMPT,
        "schedule": _hermes_schedule(minutes),
        "name": JOB_NAME,
        "deliver": deliver,
        "enabled_toolsets": [TOOLSET],
    }
    try:
        params = inspect.signature(create).parameters
        has_var = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in params.values())
    except Exception:
        params = {}
        has_var = True
    if has_var:
        accepted = dict(kwargs)
    else:
        accepted = {key: value for key, value in kwargs.items() if key in params}
    if "interpreter" in accepted or "no_agent" in accepted:
        return None, "The cron job was not created because the call would have passed interpreter or no_agent."
    try:
        created = create(**accepted)
    except Exception as exc:
        return None, f"The cron job could not be created. {exc}"
    job_id = created.get("id") if isinstance(created, dict) else None
    if not job_id:
        return None, "The cron job did not return an id, so the account was not saved."
    return str(job_id), None


def _update_job(mod: Any, job_id: str, minutes: int, deliver: str) -> str | None:
    update = getattr(mod, "update_job", None)
    if not callable(update):
        return "The existing job could not be updated."
    try:
        result = update(job_id, {"schedule": _hermes_schedule(minutes), "deliver": deliver})
    except Exception as exc:
        return f"The existing job could not be updated. {exc}"
    if result is None:
        return "The existing job could not be updated."
    return None


def _saved_schedule(job: dict[str, Any] | None, old: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """The schedule and deliver to put back if the state file cannot be saved.

    The job's own fields win. The saved words are used only when the job has none.
    """
    schedule = None
    deliver = None
    if isinstance(job, dict):
        if isinstance(job.get("schedule"), str) and job.get("schedule").strip():
            schedule = job["schedule"]
        if isinstance(job.get("deliver"), str) and job.get("deliver").strip():
            deliver = job["deliver"]
    if schedule is None and isinstance(old, dict):
        words = old.get("interval_words")
        if isinstance(words, str) and words.strip():
            parsed = parse_interval(words)
            if isinstance(parsed, tuple):
                schedule = _hermes_schedule(parsed[0])
    if deliver is None and isinstance(old, dict):
        saved = old.get("deliver")
        if isinstance(saved, str) and saved.strip():
            deliver = saved
    return schedule, deliver


def _restore_job(mod: Any, job_id: str, schedule: str | None, deliver: str | None) -> bool:
    if not schedule or not deliver:
        return False
    update = getattr(mod, "update_job", None)
    if not callable(update):
        return False
    try:
        result = update(job_id, {"schedule": schedule, "deliver": deliver})
    except Exception:
        return False
    return result is not None


def _remove_job(mod: Any, job_id: str) -> bool:
    remove = getattr(mod, "remove_job", None)
    if not callable(remove):
        return False
    try:
        return bool(remove(job_id))
    except Exception:
        return False


def _blank_state(did: str, handle: str, deliver: str, words: str, job_id: str) -> dict[str, Any]:
    return {
        "did": did,
        "handle": handle,
        "deliver": deliver,
        "interval_words": words,
        "job_id": job_id,
        "baseline_done": False,
        "seen": [],
        "marks": [],
        "failure": None,
        "miss": None,
        "blind": None,
        "last_success_at": None,
    }


def _chosen_every(every: str | None, state: dict[str, Any] | None) -> str | None:
    """An omitted interval keeps the saved words. A new job defaults to every hour."""
    if every is not None and str(every).strip() != "":
        return every
    if isinstance(state, dict):
        saved = state.get("interval_words")
        if isinstance(saved, str) and saved.strip():
            return saved
    return None


def schedule(deps: Deps, actor: str, deliver: str, every: str | None) -> tuple[int, str]:
    state, _raw, err = load_state(deps)
    parsed = parse_interval(_chosen_every(every, state if err is None else None))
    if isinstance(parsed, str):
        return 1, parsed
    minutes, words = parsed
    canon = gates.canonical_deliver(deliver)
    previous = state.get("deliver") if isinstance(state, dict) else None
    if canon is None:
        if previous:
            return 1, f"Delivery target was rejected. The previous target remains {previous}. The job was not changed."
        return 1, "Delivery target was rejected. No job was created."
    if err == "corrupt":
        return 1, "The saved file could not be read, so it was not replaced. The job was not changed."
    if err == "unreadable":
        return 1, "The data folder could not be read, so nothing was requested."
    problem = _bad_actor(actor)
    if problem:
        return 1, problem
    try:
        data = _one(deps, client.PROFILE_PATH, [("actor", actor)])
    except client.ClientError as exc:
        kept = ""
        if isinstance(state, dict) and state.get("did"):
            kept = f" The saved DID {state.get('did')} was left in place."
        return 1, exc.message + kept + " The job was not changed."
    view = profile_view(data)
    did = str(view.get("did") or "")
    handle = str(view.get("handle") or actor)
    try:
        mod = _cron_module(deps)
    except Exception:
        return 1, "cron.jobs could not be loaded, so the account was not saved. The job was not changed."
    old = state if isinstance(state, dict) else None
    old_id = str(old.get("job_id") or "") if old else ""
    created_new = False
    job_id = old_id if old_id and _job_by_id(mod, old_id) else ""
    if not job_id:
        named = _named_job_ids(mod)
        if named is None:
            named = []
        if len(named) > 1:
            return 1, "More than one author-feed job already exists, so another was not created. Ids: " + ", ".join(named)
        if len(named) == 1:
            job_id = named[0]
        else:
            job_id, create_err = _create_job(mod, minutes, canon)
            if create_err or not job_id:
                return 1, create_err or "The cron job was not created."
            created_new = True
    previous_schedule = None
    previous_deliver = None
    if not created_new:
        previous_schedule, previous_deliver = _saved_schedule(_job_by_id(mod, job_id), old)
        update_err = _update_job(mod, job_id, minutes, canon)
        if update_err:
            return 1, update_err + " The saved file was not changed."
    swapped = ""
    if old and old.get("did") == did:
        new_state = dict(old)
        new_state["handle"] = handle
        new_state["deliver"] = canon
        new_state["interval_words"] = words
        new_state["job_id"] = job_id
    else:
        if old and old.get("did"):
            dropped = len([uri for uri in old.get("seen") or [] if isinstance(uri, str)])
            swapped = (
                f" Replaced watched account {old.get('handle')} ({old.get('did')}) with {handle} ({did}). "
                f"{dropped} saved post URIs were cleared, and the next run stores a new baseline "
                "without announcing it."
            )
        new_state = _blank_state(did, handle, canon, words, job_id)
    save_err = save_state(deps, new_state)
    if save_err:
        if created_new:
            removed = _remove_job(mod, job_id)
            if not removed:
                return 1, save_err + f" The leftover job id is {job_id}."
            return 1, save_err + " The new job was removed."
        moved = f"{_hermes_schedule(minutes)} with deliver {canon}"
        unchanged = previous_schedule == _hermes_schedule(minutes) and previous_deliver == canon
        if unchanged:
            return 1, save_err + " The saved file was not changed."
        if _restore_job(mod, job_id, previous_schedule, previous_deliver):
            return 1, save_err + " The job was put back on its previous schedule and deliver. The saved file was not changed."
        return 1, save_err + f" The job {job_id} is already on {moved} and was not put back."
    replaced = ""
    if previous and previous != canon:
        replaced = f" Replaced deliver target {previous} with {canon}."
    text = (
        f"Scheduled {handle} ({did}), {words}, deliver {canon}, job {job_id}."
        f"{replaced}{swapped}{_deliver_caution(canon)}\n"
        f"{_cron_help(job_id)}"
    )
    return 0, text


def unschedule(deps: Deps) -> tuple[int, str]:
    state, _raw, err = load_state(deps)
    if err == "corrupt":
        return 1, "The saved file could not be read, so it was not replaced. The job was not changed."
    if err == "unreadable":
        return 1, "The data folder could not be read."
    if not isinstance(state, dict) or not state.get("job_id"):
        return 0, "No cron job is stored. The saved file was left in place."
    try:
        mod = _cron_module(deps)
    except Exception:
        return 1, "cron.jobs could not be loaded, so the job was not removed. The saved file was left in place."
    job_id = str(state["job_id"])
    if _job_by_id(mod, job_id) is None:
        state["job_id"] = None
        save_err = save_state(deps, state)
        if save_err:
            return 1, save_err + f" Job {job_id} is already gone. The saved id was left in place."
        return 0, f"Job {job_id} is already gone. The saved id was cleared. Seen URIs were left in place."
    removed = _remove_job(mod, job_id)
    if not removed:
        if _job_by_id(mod, job_id) is None:
            state["job_id"] = None
            save_err = save_state(deps, state)
            if save_err:
                return 1, save_err + f" Job {job_id} is already gone. The saved id was left in place."
            return 0, f"Job {job_id} is already gone. The saved id was cleared. Seen URIs were left in place."
        return 1, f"The job {job_id} was not removed. The saved file was left in place."
    state["job_id"] = None
    save_err = save_state(deps, state)
    if save_err:
        return 1, save_err + f" The job {job_id} was removed. Seen URIs were left in place if the file still has them."
    return 0, (
        f"Removed job {job_id}. The saved file remains, including seen URIs. "
        "Run unschedule before hermes plugins remove. "
        "If the plugin is already gone and the job is still listed, "
        "remove that leftover job with hermes cron remove and its id."
    )


def status(deps: Deps) -> tuple[int, str]:
    state, _raw, err = load_state(deps)
    if err == "corrupt":
        return 1, "The saved file could not be read, so it was not replaced."
    if err == "unreadable":
        return 1, "The data folder could not be read."
    if not isinstance(state, dict) or not state.get("did"):
        return 0, "No saved account."
    seen = state.get("seen") if isinstance(state.get("seen"), list) else []
    job_id = str(state.get("job_id") or "")
    gone = False
    if job_id:
        try:
            mod = _cron_module(deps)
        except Exception:
            return 1, "cron.jobs could not be loaded, so the job was not checked."
        if _job_by_id(mod, job_id) is None:
            gone = True
    job_bit = f"job {job_id} is gone" if gone else f"job {job_id or 'none'}"
    text = (
        f"handle {state.get('handle')} did {state.get('did')} "
        f"deliver {state.get('deliver')} interval {state.get('interval_words')} "
        f"{job_bit} baseline_done {bool(state.get('baseline_done'))} "
        f"seen {len(seen)}\n"
        f"{_cron_help(None if gone else job_id or None)}"
    )
    return 0, text
