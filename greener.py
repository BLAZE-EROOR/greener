#!/usr/bin/env python3
"""
greener - scheduled commit automation for a GitHub activity repo.

Runs on a schedule (hourly via launchd). Each run decides whether to make a
commit toward a randomized daily target, so commits land at natural times
across the day rather than in a single burst.

Timestamps are real by default. Backfill mode exists but is off; see README.
"""

import json
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / ".state.json"
LOG_PATH = HERE / "greener.log"

DEFAULTS = {
    "repo_path": str(Path.home() / "priv"),
    "filename": "activity.log",
    "branch": "main",
    "commits_per_day_min": 4,
    "commits_per_day_max": 5,
    "active_hours": [9, 23],      # only commit between these hours, local time
    "skip_weekend_chance": 0.6,   # probability of skipping a given Sat/Sun
    "push_every_run": True,
    "backfill_enabled": False,    # see README before enabling
}


def log(msg):
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            log(f"WARN  bad config.json ({e}); using defaults")
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def git(args, repo, env=None, check=True):
    """Run a git command in repo. Returns (rc, stdout)."""
    p = subprocess.run(
        ["git"] + args, cwd=repo, env=env,
        capture_output=True, text=True,
    )
    if check and p.returncode != 0:
        log(f"ERROR git {' '.join(args)} -> rc={p.returncode}: {p.stderr.strip()}")
    return p.returncode, p.stdout.strip()


def preflight(repo, branch):
    """Verify the repo is usable before touching anything."""
    if not Path(repo, ".git").is_dir():
        log(f"ERROR {repo} is not a git repository")
        return False

    rc, _ = git(["fsck", "--no-progress", "--connectivity-only"], repo, check=False)
    if rc != 0:
        log("ERROR repo failed integrity check - refusing to commit into a broken repo")
        return False

    rc, email = git(["config", "user.email"], repo, check=False)
    if rc != 0 or not email:
        log("ERROR no user.email configured; commits would not be attributed")
        return False

    if email.endswith(".local") or "@" not in email:
        log(f"ERROR user.email '{email}' is not a real address; GitHub will ignore these commits")
        return False

    return True


def todays_plan(cfg, state):
    """Pick (and remember) how many commits today gets."""
    today = date.today().isoformat()
    if state.get("day") != today:
        target = random.randint(cfg["commits_per_day_min"], cfg["commits_per_day_max"])

        # Real activity isn't uniform across the week.
        if date.today().weekday() >= 5 and random.random() < cfg["skip_weekend_chance"]:
            target = 0

        state = {"day": today, "target": target, "done": 0}
        log(f"new day {today}: target={target} commits")
    return state


def make_commit(cfg, repo, when=None):
    """Create one commit. `when` backdates it; None uses the real clock."""
    path = Path(repo) / cfg["filename"]
    stamp = (when or datetime.now()).isoformat(timespec="seconds")

    with open(path, "a") as f:
        f.write(f"{stamp}\n")

    rc, _ = git(["add", cfg["filename"]], repo)
    if rc != 0:
        return False

    env = os.environ.copy()
    if when is not None:
        ts = when.strftime("%Y-%m-%dT%H:%M:%S")
        env["GIT_AUTHOR_DATE"] = ts
        env["GIT_COMMITTER_DATE"] = ts

    rc, _ = git(["commit", "-m", f"chore: activity log {stamp}"], repo, env=env)
    return rc == 0


def push(cfg, repo):
    """Push and verify it actually landed. The original script never checked."""
    before_rc, before = git(["rev-parse", f"origin/{cfg['branch']}"], repo, check=False)
    rc, _ = git(["push", "origin", cfg["branch"]], repo, check=False)
    if rc != 0:
        log("ERROR push failed")
        return False

    git(["fetch", "origin", cfg["branch"]], repo, check=False)
    _, local = git(["rev-parse", "HEAD"], repo, check=False)
    _, remote = git(["rev-parse", f"origin/{cfg['branch']}"], repo, check=False)

    if local != remote:
        log(f"ERROR push reported success but remote != local ({remote[:8]} vs {local[:8]})")
        return False

    log(f"push OK -> {remote[:8]}")
    return True


def run_scheduled(cfg):
    repo = cfg["repo_path"]
    if not preflight(repo, cfg["branch"]):
        return 1

    state = todays_plan(cfg, load_state())

    if state["done"] >= state["target"]:
        log(f"quota met for today ({state['done']}/{state['target']}) - nothing to do")
        save_state(state)
        return 0

    hour = datetime.now().hour
    lo, hi = cfg["active_hours"]
    if not (lo <= hour <= hi):
        log(f"outside active hours {lo}-{hi} (now {hour}) - skipping")
        save_state(state)
        return 0

    # Spread the remaining quota probabilistically over the remaining hours,
    # so commits don't clump at the start of the window.
    remaining_hours = max(1, hi - hour + 1)
    remaining_commits = state["target"] - state["done"]
    if random.random() > (remaining_commits / remaining_hours):
        log(f"holding this hour ({state['done']}/{state['target']} done)")
        save_state(state)
        return 0

    if make_commit(cfg, repo):
        state["done"] += 1
        log(f"committed ({state['done']}/{state['target']})")
        if cfg["push_every_run"]:
            push(cfg, repo)
    save_state(state)
    return 0


def run_backfill(cfg, days):
    """Backdate commits over the last N days. Off by default - read the README."""
    if not cfg.get("backfill_enabled"):
        log("backfill is disabled in config.json (backfill_enabled=false). Refusing.")
        return 1

    repo = cfg["repo_path"]
    if not preflight(repo, cfg["branch"]):
        return 1

    log(f"BACKFILL {days} days - these commits carry forged timestamps")

    # Chronological order matters: committing out of order produces commits
    # dated earlier than their own parent, which is the single most obvious
    # signature of a fabricated history.
    plan = []
    for d in range(days, 0, -1):
        day = datetime.now() - timedelta(days=d)
        if day.weekday() >= 5 and random.random() < cfg["skip_weekend_chance"]:
            continue
        n = random.randint(cfg["commits_per_day_min"], cfg["commits_per_day_max"])
        for _ in range(n):
            lo, hi = cfg["active_hours"]
            plan.append(day.replace(
                hour=random.randint(lo, hi),
                minute=random.randint(0, 59),
                second=random.randint(0, 59),
                microsecond=0,
            ))
    plan.sort()

    for i, when in enumerate(plan, 1):
        if make_commit(cfg, repo, when=when):
            log(f"[{i}/{len(plan)}] {when.isoformat(timespec='seconds')}")

    push(cfg, repo)
    return 0


def main():
    cfg = load_config()

    if len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        return run_backfill(cfg, days)

    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        st = load_state()
        print(json.dumps({"config": cfg, "state": st}, indent=2))
        return 0

    return run_scheduled(cfg)


if __name__ == "__main__":
    sys.exit(main())
