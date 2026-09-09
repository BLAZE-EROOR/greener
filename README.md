# greener

A scheduled commit bot for a personal activity repo — and a writeup of four
silent failure modes in the script it replaces.

If you've run a contribution-graph script and watched nothing appear on your
profile, the postmortem below probably explains why. All four failures are
silent: the script prints success and exits 0 while accomplishing nothing.

---

## Postmortem: why the original produced 568 commits and zero contributions

Rewritten from [Graph-Greener](https://github.com/sakshamrma/Graph-Greener).
A single run hit all four of these simultaneously.

### 1. Unverified author email — silently discards every commit

The script never sets `user.email`, so git falls back to a guess:

```
Aman Verma <aman@Amans-MacBook-Pro.local>
```

**GitHub only counts commits whose author email is verified on your account.**
A `.local` hostname address matches nothing, so every commit is attributed to
no one. There is no error, no warning, and the commits appear normally in the
repo — they just don't count. 567 of 568 commits were discarded this way.

Diagnose it:

```bash
git log --format='%ae' | sort | uniq -c
```

Anything other than your verified address or
`ID+USERNAME@users.noreply.github.com` is dead weight.

> **Fixed:** preflight refuses to run if `user.email` is unset, lacks an `@`, or
> ends in `.local`.

### 2. No push verification — "✅ All done!" after a failed push

The original ends with:

```python
subprocess.run(["git", "push"], cwd=repo_path)
print("✅ All done! Check your GitHub contribution graph in a few minutes.")
```

The return code is never checked. The success message prints unconditionally.
A run that pushed nothing is indistinguishable from one that worked, and you
find out days later when the graph is still empty.

Diagnose it:

```bash
git ls-remote origin refs/heads/main
```

Compare against `git rev-parse HEAD`. If they differ, nothing landed.

> **Fixed:** after pushing, the remote ref is re-fetched and compared to local
> HEAD. Mismatch is an error, not a checkmark.

### 3. Auto-gc corruption — hundreds of commits into an unpushable repo

Committing several hundred times in a tight loop crosses git's `gc.auto`
threshold. The repack runs mid-loop; if it's interrupted — closed terminal,
Ctrl-C, sleep — you get a half-written pack and missing objects:

```
missing commit 3d6dd6e010893322222486b52f7dbaccee10d245
missing blob   94de96f9698bb238ac433e9dbd57ea6133289b58
broken link from commit 49cc8f6 → 3d6dd6e
```

`git log` can't walk the history, so the push is refused, so nothing reaches
GitHub. The loop keeps committing regardless.

Diagnose it:

```bash
git fsck --no-progress
ls .git/objects/pack/tmp_pack_*   # leftover temp packs = interrupted repack
```

> **Fixed:** `gc.auto 0` documented as a setup step, plus `git fsck` in
> preflight so a damaged repo fails on the next tick instead of absorbing
> hundreds more commits.

### 4. Non-monotonic commit dates — the giveaway

Backdating with random dates in random order produces commits **dated earlier
than their own parent commit**. In a sample of 400 commits, 203 were backwards.

That state cannot occur in real development — you can't commit on a date before
the work you built on existed. It's the clearest possible signature of a
fabricated history, and one `git log` reveals it.

Diagnose it:

```bash
git rev-list HEAD | while read c; do
  p=$(git rev-parse -q --verify "$c^" 2>/dev/null) || continue
  [ "$(git log -1 --format=%at "$c")" -lt "$(git log -1 --format=%at "$p")" ] && echo "$c"
done | wc -l
```

> **Fixed:** backfill sorts its plan chronologically before committing.

---

## What this tool actually does

It appends a timestamp line to a file and commits it. That's the entire payload.
It's a scheduling and git-plumbing exercise, not a productivity tool — the
commits contain no work, and anyone reading the repo will see that.

Know what that means before pointing it at a repo tied to your identity:

- **Scheduled mode (default)** commits with real timestamps. Nothing is forged;
  the commits are low-value but honestly dated.
- **Backfill mode** forges timestamps. Fixing issue #4 removes the most obvious
  tell but not the others — see [Backfill](#backfill-mode).
- **GitHub keeps a server-side event log.** Commits pushed today produce a push
  event dated today, no matter what the commit timestamps claim. No client-side
  tool can touch that record. A year of history arriving in one push is visible
  to anyone who checks the events API.

## Install

Requires Python 3.7+, git, and a repo you can push to.

### 1. Configure

`config.json`:

```json
{
  "repo_path": "/Users/you/activity-repo",
  "filename": "activity.log",
  "branch": "main",
  "commits_per_day_min": 4,
  "commits_per_day_max": 5,
  "active_hours": [9, 23],
  "skip_weekend_chance": 0.6,
  "push_every_run": true,
  "backfill_enabled": false
}
```

| Key | Meaning |
|---|---|
| `repo_path` | Absolute path to the target git repo |
| `filename` | File the commits append to |
| `commits_per_day_min/max` | A random target is picked once per day |
| `active_hours` | Commits only happen inside this local-time window |
| `skip_weekend_chance` | Probability a given Sat/Sun is skipped entirely |
| `push_every_run` | Push after each commit, and verify it landed |
| `backfill_enabled` | Gate on `--backfill`; leave `false` unless you've read the warning |

### 2. Set the commit identity — this is issue #1

```bash
git -C ~/activity-repo config user.email "ID+USERNAME@users.noreply.github.com"
git -C ~/activity-repo config user.name  "USERNAME"
```

Exact address: <https://github.com/settings/emails>.

### 3. Disable auto-gc — this is issue #3

```bash
git -C ~/activity-repo config gc.auto 0
```

Run `git gc` manually when the bot isn't active.

### 4. Install the launchd job (macOS)

Create `~/Library/LaunchAgents/com.you.greener.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.you.greener</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/graph-greener-auto/greener.py</string>
  </array>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>RunAtLoad</key>
  <false/>
  <key>WorkingDirectory</key>
  <string>/Users/you/graph-greener-auto</string>
  <key>StandardOutPath</key>
  <string>/Users/you/graph-greener-auto/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/graph-greener-auto/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>/Users/you</string>
  </dict>
</dict>
</plist>
```

Replace every `/Users/you`, then:

```bash
launchctl load ~/Library/LaunchAgents/com.you.greener.plist
launchctl list | grep greener
```

Remove with `launchctl unload` and `rm`.

## Usage

```bash
python3 greener.py            # one scheduled tick
python3 greener.py --status   # config + today's progress, makes no commits
python3 greener.py --backfill 10
```

## How the scheduling works

`launchd` wakes the script hourly. Each tick:

1. **Preflight** — repo exists, passes `fsck`, has a usable commit email.
   Refuses to commit into a broken repo.
2. **Daily plan** — first tick of a new day picks a random 4–5 target and rolls
   for a weekend skip.
3. **Window check** — outside `active_hours`, does nothing.
4. **Probabilistic gate** — commits with probability
   `remaining_commits / remaining_hours`, so the quota spreads across the day
   instead of clumping at 9am.
5. **Commit and push** — then confirms the remote ref moved.

State lives in `.state.json`, reset daily.

## Backfill mode

`--backfill N` writes commits dated across the last N days via
`GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE`, gated behind
`"backfill_enabled": true`. It sorts chronologically, which fixes issue #4.

Remaining tells it does **not** fix:

- Every commit message follows one template.
- The file contains only timestamps — no diff a reader would mistake for work.
- Author and committer dates match exactly on every commit. Real histories
  diverge on every rebase, amend, and squash merge.
- The event log shows the whole range arriving in a single push.

Backfilled history doesn't survive someone opening the repo. Scheduled mode is
the default for that reason.

## Notes

- Private repos don't show on the contribution graph unless you enable
  **Private contributions** at <https://github.com/settings/profile>.
- `launchd` doesn't fire while the Mac is asleep; missed hours are skipped, not
  replayed.
- `.state.json` and `*.log` are gitignored runtime files.

## License

MIT
