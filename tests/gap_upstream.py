"""Local AppView for the real-cron gap checks. Pages come from GAP_SPEC."""
import json
import os
from urllib.parse import parse_qs


def handle(method, path, query, body, state):  # noqa: ARG001
    spec = json.loads(open(os.environ["GAP_SPEC"], encoding="utf-8").read())
    if path.endswith("getProfile"):
        return {"json": spec["profile"]}
    if path.endswith("getAuthorFeed"):
        cursor = (parse_qs(query or "").get("cursor") or [""])[0]
        page = (spec.get("pages") or {}).get(cursor)
        if page is None:
            return {"status": 404, "json": {"error": "Missing", "message": "no page"}}
        return {"json": page}
    return None
