"""Real hermes cron on v0.21.4 and main, against a local AppView.

The five gap cases. +24h is covered by tests/test_plugin.py, because a real
cron uses the wall clock. An unchanged second run is the same overlap.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HARNESS = Path.home() / "takeshitgal" / "ship" / "harness"
CACHE = Path.home() / "takeshitgal" / ".harness"
DID = "did:plc:z72i7hdynmk6r22z27h6tvur"
PROFILE = {
    "did": DID,
    "handle": "bsky.app",
    "displayName": "Bluesky",
    "followersCount": 1,
    "followsCount": 2,
    "postsCount": 3,
}


def repost(uri: str, indexed: str) -> dict:
    return {
        "post": {
            "uri": uri,
            "author": {"did": "did:aster", "handle": "aster.id"},
            "record": {"text": "r", "createdAt": indexed},
        },
        "reason": {
            "$type": "app.bsky.feed.defs#reasonRepost",
            "indexedAt": indexed,
            "by": {"did": DID},
        },
    }


def own(uri: str, text: str) -> dict:
    return {
        "post": {
            "uri": uri,
            "author": {"did": DID, "handle": "bsky.app"},
            "record": {"text": text, "createdAt": "2026-01-01T00:00:00.000Z"},
        }
    }


def pages_of(rows: list, full: int = 10) -> dict:
    out = {}
    cursor = ""
    start = 0
    number = 0
    while start < len(rows):
        chunk = rows[start:start + full]
        start += full
        body = {"feed": chunk}
        if start < len(rows):
            number += 1
            body["cursor"] = f"p{number}"
        out[cursor] = body
        cursor = body.get("cursor", "")
        if "cursor" not in body:
            break
    return out


def write_spec(path: Path, rows: list) -> None:
    path.write_text(json.dumps({"profile": PROFILE, "pages": pages_of(rows)}), encoding="utf-8")


def start_server(argv: list[str], log_path: Path) -> tuple[subprocess.Popen, int]:
    logf = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=logf, text=True)
    line = proc.stdout.readline() if proc.stdout else ""
    if not line.startswith("PORT "):
        raise RuntimeError(f"server did not start: {line!r}")
    return proc, int(line.split()[1])


def hermes(bin_path: Path, argv: list[str], env: dict, cwd: Path, timeout: int = 180) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [str(bin_path), *argv],
            env=env,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + f"\ntimeout after {timeout}s"


def jobs(home: Path) -> list:
    path = home / "cron" / "jobs.json"
    if not path.is_file():
        return []
    return (json.loads(path.read_text(encoding="utf-8")) or {}).get("jobs") or []


def state_file(home: Path) -> Path | None:
    found = list(home.rglob("plugin-data/author-feed/state.json"))
    return found[0] if found else None


def read_log(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def cron_once(bin_path: Path, env: dict, cwd: Path, home: Path, log_path: Path) -> list:
    job_ids = [job["id"] for job in jobs(home)]
    if len(job_ids) != 1:
        raise RuntimeError(f"expected one job, got {job_ids}")
    log_path.write_text("", encoding="utf-8")
    code, out = hermes(bin_path, ["cron", "run", job_ids[0]], env, cwd, timeout=120)
    if code != 0:
        raise RuntimeError(f"cron run exit {code}: {out[-800:]}")
    code, out = hermes(bin_path, ["cron", "tick"], env, cwd, timeout=180)
    if code != 0:
        raise RuntimeError(f"cron tick exit {code}: {out[-800:]}")
    return read_log(log_path)


def notified(rows: list, needle: str) -> bool:
    return any(row.get("notify") is True and needle in str(row.get("message") or "") for row in rows)


def quiet(rows: list) -> bool:
    if any(row.get("notify") is True and "取りこぼし" in str(row.get("message") or "") for row in rows):
        return False
    return any(row.get("notify") is False for row in rows) or not any(row.get("notify") is True for row in rows)


def load_state(home: Path) -> dict:
    path = state_file(home)
    if path is None:
        raise RuntimeError("state.json was not written")
    return json.loads(path.read_text(encoding="utf-8"))


def reset_account(bin_path: Path, env: dict, cwd: Path, home: Path) -> None:
    hermes(bin_path, ["author-feed", "unschedule"], env, cwd, timeout=60)
    path = state_file(home)
    if path is not None and path.is_file():
        path.unlink()
    for job in jobs(home):
        hermes(bin_path, ["cron", "remove", job["id"]], env, cwd, timeout=60)


def schedule(bin_path: Path, env: dict, cwd: Path) -> None:
    code, out = hermes(
        bin_path,
        ["author-feed", "schedule", "--actor", "bsky.app", "--deliver", "local"],
        env,
        cwd,
        timeout=120,
    )
    if code != 0:
        raise RuntimeError(f"schedule exit {code}: {out[-800:]}")


def run_version(version: str, work: Path, upstream: str, spec_path: Path, log_path: Path) -> None:
    bin_path = CACHE / "bin" / f"hermes-{version}"
    py = CACHE / "venv" / version / "bin" / "python"
    if not bin_path.is_file() or not py.is_file():
        raise RuntimeError(f"missing hermes for {version}")
    run_root = work / version
    home = run_root / "home"
    userhome = run_root / "userhome"
    home.mkdir(parents=True)
    userhome.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "OPENAI_", "ANTHROPIC_", "PYTHON", "VIRTUAL_ENV", "UV_"))}
    env.update({
        "HOME": str(userhome),
        "HERMES_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_DISABLE_UPDATE_CHECK": "1",
        "HARNESS_RUN_ROOT": str(run_root),
        "PYTHONPATH": str(HARNESS / "lib"),
        "HARNESS_SITEPATCH": str(HARNESS / "lib" / "harness_patch.py"),
        "HARNESS_PATCH": json.dumps([{"module": "client", "attr": "API_BASE", "value": upstream}]),
        "GAP_SPEC": str(spec_path),
        "NO_COLOR": "1",
        "TERM": "dumb",
    })
    code, out = hermes(bin_path, ["plugins", "install", f"file://{work / 'snapshot'}", "--enable"], env, run_root, timeout=300)
    if code != 0:
        raise RuntimeError(f"{version} install exit {code}: {out[-1200:]}")
    if "author-feed" not in out and not (home / "plugins" / "author-feed").exists():
        code, out = hermes(bin_path, ["plugins", "enable", "author-feed"], env, run_root, timeout=180)
        if code != 0:
            raise RuntimeError(f"{version} enable exit {code}: {out[-800:]}")
    env["model"] = ""  # placeholder so the dict stays obvious
    model = env.pop("model")
    del model
    # The caller fills the model URL into this env before scenarios.
    env["__MODEL__"] = ""
    return_env = env
    return_env["__BIN__"] = str(bin_path)
    (run_root / "env.json").write_text(json.dumps({"home": str(home)}), encoding="utf-8")
    # scenarios are run by the caller with this env; stash on the function via a file
    (work / f"{version}.ready").write_text(json.dumps({
        "bin": str(bin_path),
        "home": str(home),
        "cwd": str(run_root),
    }), encoding="utf-8")
    # config for the fake model is applied by the caller after it knows the port
    return_env  # noqa: used by side effect of the ready file


def apply_model(home: Path, model_url: str) -> None:
    path = home / "config.yaml"
    data = {}
    if path.is_file():
        try:
            import yaml
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    data = json.loads(json.dumps(data, default=str))
    data["model"] = {
        "provider": "custom",
        "base_url": model_url + "/v1",
        "default": "fake-model",
        "api_key": "harness-fake-model",
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def baseline_then(version: str, work: Path, env: dict, spec_path: Path, log_path: Path, baseline_rows: list, later_rows: list):
    info = json.loads((work / f"{version}.ready").read_text(encoding="utf-8"))
    bin_path = Path(info["bin"])
    home = Path(info["home"])
    cwd = Path(info["cwd"])
    reset_account(bin_path, env, cwd, home)
    write_spec(spec_path, baseline_rows)
    schedule(bin_path, env, cwd)
    cron_once(bin_path, env, cwd, home, log_path)
    stored = load_state(home)
    if not stored.get("baseline_done"):
        raise RuntimeError(f"{version} baseline did not store: {stored}")
    write_spec(spec_path, later_rows)
    return bin_path, home, cwd, stored


def make_snapshot(work: Path) -> None:
    snap = work / "snapshot"
    shutil.copytree(
        REPO,
        snap,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"),
    )
    subprocess.run(["git", "init"], cwd=snap, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=snap, check=True, capture_output=True)
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "TakeshiTGAL",
        "GIT_AUTHOR_EMAIL": "TakeshiTGAL@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "TakeshiTGAL",
        "GIT_COMMITTER_EMAIL": "TakeshiTGAL@users.noreply.github.com",
    })
    subprocess.run(["git", "commit", "-m", "snapshot"], cwd=snap, check=True, capture_output=True, env=env)


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="author-feed-gap-"))
    py = CACHE / "venv" / "v0214" / "bin" / "python"
    spec_path = work / "spec.json"
    spec_path.write_text("{}", encoding="utf-8")
    os.environ["GAP_SPEC"] = str(spec_path)
    log_path = work / "model.jsonl"
    log_path.write_text("", encoding="utf-8")
    make_snapshot(work)
    handler_spec = work / "upstream.json"
    handler_spec.write_text(json.dumps({"handler": str(REPO / "tests" / "gap_upstream.py")}), encoding="utf-8")
    up, up_port = start_server(
        [str(py), str(HARNESS / "lib" / "fake_upstream.py"), str(handler_spec)],
        work / "upstream.log",
    )
    mdl, m_port = start_server(
        [str(py), str(HARNESS / "lib" / "fake_model.py"), str(log_path), "author_watch"],
        work / "model.log",
    )
    upstream = f"http://127.0.0.1:{up_port}"
    model_url = f"http://127.0.0.1:{m_port}"
    try:
        failures = []
        for version in ("v0214", "main"):
            print(f"== install {version}", flush=True)
            run_version(version, work, upstream, spec_path, log_path)
            info = json.loads((work / f"{version}.ready").read_text(encoding="utf-8"))
            home = Path(info["home"])
            apply_model(home, model_url)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "OPENAI_", "ANTHROPIC_", "PYTHON", "VIRTUAL_ENV", "UV_"))}
            env.update({
                "HOME": str(work / version / "userhome"),
                "HERMES_HOME": str(home),
                "PYTHONDONTWRITEBYTECODE": "1",
                "HERMES_DISABLE_UPDATE_CHECK": "1",
                "HARNESS_RUN_ROOT": str(work / version),
                "PYTHONPATH": str(HARNESS / "lib"),
                "HARNESS_SITEPATCH": str(HARNESS / "lib" / "harness_patch.py"),
                "HARNESS_PATCH": json.dumps([{"module": "client", "attr": "API_BASE", "value": upstream}]),
                "GAP_SPEC": str(spec_path),
                "NO_COLOR": "1",
                "TERM": "dumb",
                "HTTP_PROXY": "http://127.0.0.1:9",
                "HTTPS_PROXY": "http://127.0.0.1:9",
                "NO_PROXY": "127.0.0.1,localhost,::1",
                "no_proxy": "127.0.0.1,localhost,::1",
            })
            bin_path = Path(info["bin"])
            cwd = Path(info["cwd"])
            try:
                check_version(version, work, env, spec_path, log_path, bin_path, home, cwd)
                print(f"PASS {version}", flush=True)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{version}: {exc}")
                print(f"FAIL {version}: {exc}", flush=True)
        if failures:
            raise SystemExit("\n".join(failures))
        print("PASS both versions", flush=True)
    finally:
        up.terminate()
        mdl.terminate()
        shutil.rmtree(work, ignore_errors=True)


def check_version(version, work, env, spec_path, log_path, bin_path, home, cwd) -> None:
    anchor = repost("at://re/anchor", "2020-01-01T00:00:00.000Z")
    # 1. X behind 31 reposts.
    bin_path, home, cwd, _stored = baseline_then(
        version, work, env, spec_path, log_path, [own("at://own/base", "base")],
        [repost(f"at://re/n{i}", f"2026-03-01T00:{i:02d}:00.000Z") for i in range(31)] + [own("at://own/x", "hidden")],
    )
    first = cron_once(bin_path, env, cwd, home, log_path)
    if not notified(first, "取りこぼしの可能性"):
        raise RuntimeError(f"case 1 first did not warn: {first[-2:]}")
    if "at://own/x" in load_state(home).get("seen", []):
        raise RuntimeError("case 1 stored the hidden post")
    second = cron_once(bin_path, env, cwd, home, log_path)
    if not quiet(second):
        raise RuntimeError(f"case 1 second was not quiet: {second[-2:]}")

    # 2. 31 reposts, no own post.
    baseline_then(
        version, work, env, spec_path, log_path, [own("at://own/base", "base")],
        [repost(f"at://re/m{i}", f"2026-03-02T00:{i:02d}:00.000Z") for i in range(31)],
    )
    first = cron_once(bin_path, env, cwd, home, log_path)
    if not notified(first, "取りこぼしの可能性"):
        raise RuntimeError(f"case 2 first did not warn: {first[-2:]}")
    second = cron_once(bin_path, env, cwd, home, log_path)
    if not quiet(second):
        raise RuntimeError(f"case 2 second was not quiet: {second[-2:]}")

    # 3. Five new reposts at a time, then an unchanged run.
    baseline_then(version, work, env, spec_path, log_path, [anchor], [anchor])
    grown = [anchor]
    for step in range(3):
        grown = [repost(f"at://re/g{step}-{i}", f"2026-04-0{step+1}T00:00:{i:02d}.000Z") for i in range(5)] + grown
        write_spec(spec_path, grown)
        rows = cron_once(bin_path, env, cwd, home, log_path)
        if notified(rows, "取りこぼし"):
            raise RuntimeError(f"case 3 step {step} warned: {rows[-2:]}")
    rows = cron_once(bin_path, env, cwd, home, log_path)
    if notified(rows, "取りこぼし"):
        raise RuntimeError(f"case 3 unchanged warned: {rows[-2:]}")

    # 4. Twelve new reposts, then X, then the stored anchor.
    head = [repost(f"at://re/h{i}", f"2026-05-01T00:00:{i:02d}.000Z") for i in range(12)]
    baseline_then(version, work, env, spec_path, log_path, [anchor], head + [own("at://own/x", "seen me"), anchor])
    rows = cron_once(bin_path, env, cwd, home, log_path)
    if not notified(rows, "at://own/x"):
        raise RuntimeError(f"case 4 did not announce X: {rows[-2:]}")
    if notified(rows, "取りこぼし"):
        raise RuntimeError(f"case 4 also warned: {rows[-2:]}")
    print(f"  {version} cases 1-4 ok", flush=True)


if __name__ == "__main__":
    main()
