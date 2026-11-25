#!/usr/bin/env python3
"""Share this Mac user's Code chat indexes. Claude owns the shared transcripts and each login's permissions and bridges."""
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

APP = "/Applications/Claude.app"
SUPPORT = Path.home() / "Library/Application Support"
STATE = SUPPORT / "Claude Chat Index" / "state.json"
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
SIGNED_IN = re.compile(r"\[LocalSessionManager\] Initialization succeeded.*?accountId=(" + UUID + r"), orgId=(" + UUID + r")")
HISTORY = """sessionId cliSessionId cwd originCwd model effort effortInherited createdAt lastActivityAt
lastTurnEdgeAt latestUserFrameAt title titleSource titleTurn previousTitles completedTurns lastAssistantUuid
postTurnSummary postTurnSummaryFor rewindEdges priorCliSessionIds transcriptModelStates sideSessionNotes
forkedFromSessionId spawnedFrom peerReceipts peerMessaged writtenBranches scheduledTaskId""".split()


def read(path):
    return json.loads(path.read_bytes())


def save(path, record, *, new=False):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".chat-index-", delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(json.dumps(record, ensure_ascii=False).encode())
            out.flush()
            if new:
                os.link(temp, path)
            else:
                os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def profiles():
    result = {}
    for root in sorted(SUPPORT.glob("Claude*")):
        if not (root / "claude-code-sessions").is_dir():
            continue
        config = read(root / "config.json")
        account = config.get("lastKnownAccountUuid")
        pid = config.get("updaterRunningInstance", {}).get("pid")
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout.strip() if pid else ""
        active = cmd.startswith(APP + "/Contents/MacOS/Claude") and (
            f"--user-data-dir={root}" in cmd or root.name == "Claude" and "--user-data-dir" not in cmd)
        result[root] = (account, pid if active else None)
    return result


def learn_logins(logins, current):
    """Record every account and organization a running login opened; return the one Desktop opened last."""
    log = Path.home() / "Library/Logs/Claude/main.log"
    if not log.exists():
        return None
    with log.open("rb") as f:
        f.seek(max(0, f.seek(0, os.SEEK_END) - 524288))
        matches = SIGNED_IN.findall(f.read().decode(errors="replace"))
    for account, org in matches:
        # Desktop keeps one chat list per account and organization; every one a login opens shares the chats.
        if account in current and [account, org] not in logins:
            logins.append([account, org])
    return matches[-1] if matches else None


def say(message):
    subprocess.run(["osascript", "-e", f'display notification "{message}" with title "Claude chats"'], check=True)


def reopen(profile, pid):
    """Desktop reads a login's chat list only when the login opens, so chats copied in after that need a relaunch."""
    say("Copied your chats into this sign-in. Reopening Claude to show them.")
    # A normal quit, as with Cmd-Q: Claude still asks before stopping running work.
    subprocess.run(["osascript", "-l", "JavaScript", "-e", 'ObjC.import("AppKit");'
                    f"$.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}).terminate"], check=True)
    deadline = time.monotonic() + 120
    while subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0:
        if time.monotonic() > deadline:
            say("Quit and reopen Claude to see your chats on this sign-in.")
            return
        time.sleep(1)
    extra = [] if profile.name == "Claude" else ["-n", "--args", f"--user-data-dir={profile}"]
    subprocess.run(["open", "-a", APP, *extra], check=True)


def revision(record):
    return tuple(record.get(k) or 0 for k in ("lastActivityAt", "lastTurnEdgeAt", "latestUserFrameAt"))


def sync(state):
    apps = profiles()
    logins = state["logins"]
    latest = learn_logins(logins, {a for a, pid in apps.values() if pid})
    directories = {}
    shown = {}  # The chat folder each running Desktop is displaying, and that Desktop's process.
    for profile, (current, pid) in apps.items():
        for account, org in logins:
            folder = profile / "claude-code-sessions" / account / org
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            directories[folder] = pid is not None and account == current
            if directories[folder] and (account, org) == latest:
                shown[folder] = (profile, pid)

    # Desktop publishes its indexes by atomic replacement. Unchanged directory stamps need no chat reads.
    stamps = {str(p): p.stat().st_mtime_ns for p in directories}
    if stamps == state.get("stamps"):
        return 0, 0, []
    records = {}
    newest = {}
    for folder in directories:
        rows = records[folder] = {}
        for path in folder.glob("local_*.json"):
            try:
                row = read(path)
            except FileNotFoundError:
                continue  # Desktop deleted this chat between the listing and the read.
            if row["sessionId"] + ".json" != path.name:
                raise ValueError(f"Session ID mismatch in {path}")
            rows[path.name] = row
            if path.name not in newest or revision(row) > revision(newest[path.name]):
                newest[path.name] = row

    added = updated = 0
    stale = []
    for folder, active in directories.items():
        rows = records[folder]
        known = set(state["seen"].get(str(folder), []))
        before = added
        for name, source in newest.items():
            old = rows.get(name)
            if old is None and name in known:
                continue  # The user removed this entry from this login.
            if old is not None and (active or revision(old) >= revision(source)):
                continue
            row = {**(old or {}), **{k: source[k] for k in HISTORY if k in source}}
            if old is None:
                row["isArchived"] = source.get("isArchived", False)
                row["isStarred"] = source.get("isStarred", False)
            save(folder / name, row, new=old is None)
            added += old is None
            updated += old is not None
        state["seen"][str(folder)] = sorted(known | set(rows) | set(newest))
        # A first sign-in shows a near-empty list; most of its chats arriving now means Desktop is showing a stale list.
        if folder in shown and added - before > len(rows):
            stale.append(shown[folder])
    # Taking the stamps from before the scan also catches a concurrent Desktop save on the next pass.
    state["stamps"] = stamps
    return added, updated, stale


def main():
    with (STATE.parent / "sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read(STATE)
        before = json.dumps(state, sort_keys=True)
        added, updated, stale = sync(state)
        if json.dumps(state, sort_keys=True) != before:
            save(STATE, state)
        if added or updated:
            print(f"Added {added} and refreshed {updated} local Code chat entries.")
    for profile, pid in stale:
        reopen(profile, pid)


if __name__ == "__main__":
    main()
