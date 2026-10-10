"""Invariants of the watch, checked over many random feeds.

This file is the specification. Do not weaken or delete an assertion to make it pass;
change service.py instead. It is written by the auditor's side (Claude), not by the builder.

World: a feed is a list of rows, newest first. Each round puts new rows on top:
the account's own posts, other people's posts it reposted, and its reposts of its own
earlier posts. The fake upstream serves that list in pages of 10 with a cursor, the way
getAuthorFeed does. After each round the cron watch runs once.

What must always hold:
  I1  An own post that is within the newest 30 rows (the 3 pages one run may read) on the
      first run after it appeared is announced in that run.
  I2  A warning never replaces an announcement: I1 holds on runs that also warn, and on
      runs where the warning is thinned.
  I3  Nothing is announced twice, and nothing that was already there at the baseline is
      announced.
  I4  A run with no new rows announces nothing and does not warn about a possible miss.
  I5  No own post is stored as seen without having been announced. A stored post that was
      never announced is a post the owner will never hear about.
  I6  When more than 30 new rows arrived since the last run, the run warns about a possible
      miss, unless it already warned in the last 24 hours.
"""
from __future__ import annotations

import json
import random

import pytest

import service
from test_plugin import DID, OTHER, box, post, save  # noqa: F401  (box is a fixture)

PAGE = 10
WINDOW = 30
MISS = "取りこぼし"


def _iso(n: int) -> str:
    return f"2026-01-{1 + (n // 86400) % 28:02d}T{(n // 3600) % 24:02d}:{(n // 60) % 60:02d}:{n % 60:02d}.000Z"


class World:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.rows: list[dict] = []          # newest first
        self.own: list[str] = []            # every own post uri ever created
        self.n = 0

    def _own(self) -> dict:
        self.n += 1
        uri = f"at://own/{self.n}"
        self.own.append(uri)
        return post(uri, text=f"own {self.n}", created=_iso(self.n))

    def _other(self) -> dict:
        self.n += 1
        return post(
            f"at://other/{self.n}", did=OTHER, handle="other.example", text="theirs",
            reason={"$type": "app.bsky.feed.defs#reasonRepost", "by": {"did": DID}, "indexedAt": _iso(self.n)},
        )

    def _self_repost(self) -> dict:
        self.n += 1
        return post(
            self.rng.choice(self.own), text="mine again",
            reason={"$type": "app.bsky.feed.defs#reasonRepost", "by": {"did": DID}, "indexedAt": _iso(self.n)},
        )

    def add(self, count: int) -> list[dict]:
        """Put `count` new rows on top. Returns them newest first."""
        made: list[dict] = []
        for _ in range(count):
            roll = self.rng.random()
            if roll < 0.40:
                row = self._own()
            elif roll < 0.85 or not self.own:
                row = self._other()
            else:
                row = self._self_repost()
            made.insert(0, row)
            self.rows.insert(0, row)
        return made

    def transport(self, urls: list[str]):
        def call(url: str):
            urls.append(url)
            start = 0
            if "cursor=" in url:
                start = int(url.split("cursor=", 1)[1].split("&", 1)[0])
            page = self.rows[start:start + PAGE]
            body: dict = {"feed": page}
            if start + PAGE < len(self.rows):
                body["cursor"] = str(start + PAGE)
            return 200, json.dumps(body).encode()
        return call


def _is_own_post(row: dict) -> bool:
    return "reason" not in row and row["post"]["author"]["did"] == DID


@pytest.mark.parametrize("seed", range(150))
def test_watch_invariants_over_random_feeds(box, seed):
    rng = random.Random(seed)
    world = World(rng)
    world.add(rng.choice([0, 3, 12, 25, 40]))
    baseline_own = set(world.own)

    clock = {"t": 1_700_000_000.0}
    box.deps.now = lambda: clock["t"]
    box.deps.transport = world.transport(box.transport.urls)
    save(box, seen=[], baseline_done=False, job_id="job1")
    box.prove(True)

    first = service.author_watch(box.deps, {})
    assert first["new"] == [], f"seed {seed}: the baseline run announced {first['new']}"

    announced: list[str] = []
    last_warn_at: float | None = None

    for rnd in range(10):
        clock["t"] += rng.choice([3600.0, 3600.0, 90_000.0])
        added = world.add(rng.choice([0, 0, 1, 2, 5, 9, 10, 11, 20, 29, 30, 31, 45]))
        where = f"seed {seed} round {rnd} (+{len(added)} rows)"

        got = service.author_watch(box.deps, {})
        new = list(got.get("new") or [])
        message = str(got.get("message") or "")
        warned = MISS in message
        if warned:
            assert got["notify"] is True, f"{where}: a possible-miss text with notify false"

        # I3
        assert len(new) == len(set(new)), f"{where}: announced twice in one run: {new}"
        for uri in new:
            assert uri not in announced, f"{where}: {uri} was announced before"
            assert uri in world.own and uri not in baseline_own, f"{where}: {uri} is not a new own post"

        # I1 and I2
        newest = world.rows[:WINDOW]
        fresh_own = {row["post"]["uri"] for row in added if _is_own_post(row)}
        for row in newest:
            uri = row["post"]["uri"]
            if _is_own_post(row) and uri in fresh_own:
                assert uri in new, (
                    f"{where}: own post {uri} was in the newest {WINDOW} rows and was not announced "
                    f"(warned={warned}, message={message[:120]!r})"
                )
                assert got["notify"] is True, f"{where}: a new own post with notify false"

        # I4
        if not added:
            assert new == [], f"{where}: nothing was added but {new} was announced"
            assert not warned, f"{where}: nothing was added but the run warned: {message[:120]!r}"

        # I6
        if len(added) > WINDOW:
            recently = last_warn_at is not None and clock["t"] - last_warn_at < 86_400
            assert warned or recently, f"{where}: more than {WINDOW} new rows and no possible-miss warning"
        if warned:
            last_warn_at = clock["t"]

        announced.extend(new)

        # I5
        state = json.loads(box.path.read_text(encoding="utf-8"))
        for uri in state.get("seen") or []:
            if uri in world.own and uri not in baseline_own:
                assert uri in announced, f"{where}: {uri} is stored as seen but was never announced"
