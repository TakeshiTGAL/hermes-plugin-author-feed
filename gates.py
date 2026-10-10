"""Cron proof, delivery allowlist, profile prefix, and the write gate."""
from __future__ import annotations

import contextvars
import importlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

PLUGIN_NAME = "author-feed"
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
NEVER_DELIVER = frozenset({"cli", "cron", "api_server"})
DELIVER_EXACT = frozenset({"origin", "local", "all"})
SCHEDULE_WORDS = frozenset({
    "every", "in", "at", "on", "daily", "hourly", "weekly", "monthly", "yearly", "annually",
    "once", "now", "today", "tonight", "tomorrow", "midnight", "noon", "minute", "minutes",
    "hour", "hours", "day", "days", "week", "weeks", "weekdays", "weekends", "weekday", "weekend",
})
WEEKDAYS = frozenset({
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})
_DURATION = re.compile(
    r"(\d*)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\Z",
    re.IGNORECASE,
)
_PROFILE_IDENTITY_MARKERS = (
    "config.yaml",
    ".env",
    "SOUL.md",
    "profile.yaml",
    "auth.json",
    "state.db",
)
CANNOT_MONITOR = (
    "This run is not confirmed as the cron job, so this plugin cannot monitor. "
    "Saved post URIs were left unchanged. "
    "If the cron job itself reports this, the plugin is running outside the Hermes process: "
    "set plugins.isolation: in_process for this profile, then run hermes author-feed status."
)


def in_plugin_host_process() -> bool:
    return os.environ.get(HOST_PROCESS_ENV, "").strip() not in {"", "0"}


def _load(module: str, name: str) -> tuple[str, Any]:
    try:
        mod = importlib.import_module(module)
    except Exception:
        return "failed", None
    if not hasattr(mod, name):
        return "missing", None
    try:
        return "ok", getattr(mod, name)
    except Exception:
        return "failed", None


def _cron_session_var(module: Any) -> Any:
    """The ContextVar whose name is HERMES_CRON_SESSION.

    Hermes stores it as a private attribute. A missing name fails closed.
    """
    try:
        values = list(vars(module).values())
    except Exception:
        return None
    for value in values:
        if isinstance(value, contextvars.ContextVar) and value.name == "HERMES_CRON_SESSION":
            return value
    return None


def cron_proven() -> bool:
    """True only when the cron ContextVar is exactly \"1\" and this is not a plugin host.

    The environment variable of the same name is not read. A missing name, a failed
    read, or a host process are all not proven.
    """
    if in_plugin_host_process():
        return False
    try:
        module = importlib.import_module("gateway.session_context")
    except Exception:
        return False
    var = _cron_session_var(module)
    if var is None:
        return False
    unset = getattr(module, "_UNSET", None)
    if unset is None:
        return False
    try:
        value = var.get()
    except Exception:
        return False
    if value is unset:
        return False
    return value == "1"


def write_guard_error(path: str) -> str | None:
    status, fn = _load("agent.file_safety", "get_write_denied_error")
    if status != "ok":
        return "The write check could not be loaded, so the file was not saved."
    try:
        denied = fn(path)
    except Exception:
        return "The write check raised, so the file was not saved."
    if denied:
        return str(denied)
    return None


def plugin_data_dir() -> Path:
    status, fn = _load("plugins.plugin_storage", "plugin_data_dir")
    if status != "ok":
        raise RuntimeError("plugin_data_dir is not available")
    path = fn(PLUGIN_NAME)
    if not isinstance(path, Path):
        path = Path(path)
    return path


def _has_profile_identity(home: Path) -> bool:
    try:
        resolved = home.resolve()
    except OSError:
        return False
    for marker in _PROFILE_IDENTITY_MARKERS:
        path = resolved / marker
        try:
            if path.is_symlink() or path.is_file():
                return True
        except OSError:
            continue
    return False


def named_profile_for_commands(reported: str, home: Path, profiles_root: Path) -> str:
    if not isinstance(reported, str) or not PROFILE_NAME.fullmatch(reported.strip()):
        return "default"
    name = reported.strip()
    if name != "custom":
        return name
    try:
        parts = home.resolve().relative_to(profiles_root.resolve()).parts
    except Exception:
        return "default"
    if len(parts) == 1 and parts[0] == "custom" and _has_profile_identity(home):
        return "custom"
    return "default"


def active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        reported = get_active_profile_name()
    except Exception:
        return "default"
    if not isinstance(reported, str) or not PROFILE_NAME.fullmatch(reported.strip()):
        return "default"
    name = reported.strip()
    if name != "custom":
        return name
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.profiles import _get_profiles_root
        return named_profile_for_commands(name, get_hermes_home(), _get_profiles_root())
    except Exception:
        return "default"


def hermes_cron_lines(profile: str | None = None, job_id: str | None = None) -> list[str]:
    """Lines the user can paste. run and remove need an id, so they are left out without one."""
    name = active_profile_name() if profile is None else profile
    prefix = "hermes" if name == "default" else f"hermes -p {name}"
    lines = []
    ident = str(job_id).strip() if job_id else ""
    for verb in ("create", "run", "list", "status", "remove"):
        if verb in {"run", "remove"}:
            if ident:
                lines.append(f"{prefix} cron {verb} {ident}")
            continue
        lines.append(f"{prefix} cron {verb}")
    return lines


def load_platforms() -> set[str] | None:
    """None when the Hermes delivery list cannot be imported."""
    status, raw = _load("cron.scheduler_delivery", "_KNOWN_DELIVERY_PLATFORMS")
    if status != "ok":
        return None
    try:
        names = {str(item).strip().lower() for item in raw if str(item).strip()}
    except Exception:
        return None
    try:
        from gateway.platform_registry import platform_registry
        for entry_name in platform_registry.registered_names():
            text = str(entry_name).strip().lower()
            entry = platform_registry.get(text)
            if text and entry is not None and getattr(entry, "cron_deliver_env_var", None):
                names.add(text)
    except Exception:
        pass
    return names - NEVER_DELIVER


def _duration(text: str) -> bool:
    return _DURATION.fullmatch(text.strip()) is not None


def _looks_like_schedule(target: str) -> bool:
    word = target.strip().lower()
    if not word:
        return False
    if word in SCHEDULE_WORDS or word in WEEKDAYS or word.rstrip("s") in WEEKDAYS:
        return True
    if word[0].isdigit() or word[0] in "*@":
        return True
    if "/" in word or _duration(word):
        return True
    try:
        datetime.fromisoformat(word.replace("z", "+00:00"))
    except ValueError:
        return False
    return True


_MISSING = object()


def canonical_deliver(deliver: str, platforms: Any = _MISSING) -> str | None:
    if platforms is _MISSING:
        platforms = load_platforms()
    loaded = platforms is not None
    allowed = set(platforms or ())
    canon: list[str] = []
    for part in str(deliver).split(","):
        piece = part.strip()
        if not piece or any(ch.isspace() for ch in piece) or _looks_like_schedule(piece):
            return None
        low = piece.lower()
        if low in NEVER_DELIVER:
            return None
        if low in DELIVER_EXACT:
            canon.append(low)
            continue
        if not loaded:
            return None
        if low == "bot-chat":
            canon.append(low)
            continue
        if low.startswith("bot-chat:"):
            chat = piece.split(":", 1)[1].strip()
            if not chat or any(ch.isspace() for ch in chat):
                return None
            canon.append(f"bot-chat:{chat}")
            continue
        platform, sep, chat = piece.partition(":")
        key = platform.lower()
        if key not in allowed:
            return None
        if not sep:
            canon.append(key)
            continue
        if not chat or any(ch.isspace() for ch in chat):
            return None
        canon.append(f"{key}:{chat}")
    return ",".join(canon) if canon else None
