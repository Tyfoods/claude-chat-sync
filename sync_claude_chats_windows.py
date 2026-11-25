#!/usr/bin/env python3
"""Share this Windows user's Claude Desktop Code chat indexes across signed-in accounts.

Windows port of sync_claude_chats.py. Dry run unless --apply is given.

Rules:
- The newest copy of each chat wins, in the folders of accounts that are not open.
- The open account's folders are only read. Chats reach that account once it is no longer open.
- A chat removed from an account is not added back to it.
- If the open account cannot be established, nothing is written.
- Claude is never closed, reopened or signed in. Transcripts are never touched.
"""
import argparse
import ctypes
import json
import msvcrt
import os
import re
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

LOCAL = Path(os.environ["LOCALAPPDATA"])
ROAMING = Path(os.environ["APPDATA"])
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
SIGNED_IN = re.compile(
    r"\[LocalSessionManager\] Initialization succeeded[^\r\n]*?accountId=(" + UUID + r"), orgId=(" + UUID + r")")
# Claude Code's command line is also claude.exe, so Desktop is recognised by where it is installed.
DESKTOP_EXE = re.compile(
    r"\\WindowsApps\\Claude_[^\\]+\\app\\claude\.exe$|\\AnthropicClaude\\(?:app-[^\\]+\\)?claude\.exe$", re.I)
HISTORY = """sessionId cliSessionId cwd originCwd model effort effortInherited createdAt lastActivityAt
lastTurnEdgeAt latestUserFrameAt title titleSource titleTurn previousTitles completedTurns lastAssistantUuid
postTurnSummary postTurnSummaryFor rewindEdges priorCliSessionIds transcriptModelStates sideSessionNotes
forkedFromSessionId spawnedFrom peerReceipts peerMessaged writtenBranches scheduledTaskId""".split()


class Unsure(Exception):
    """Something could not be established, so this pass writes nothing."""


def find_root():
    """Claude's data folder. The Store package's own path also works outside the package, so it is preferred."""
    packaged = [p for p in LOCAL.glob("Packages/Claude_*/LocalCache/Roaming/Claude")
                if (p / "claude-code-sessions").is_dir()]
    found = packaged or [p for p in [ROAMING / "Claude"] if (p / "claude-code-sessions").is_dir()]
    if len(found) != 1:
        raise Unsure(f"expected one Claude data folder, found {len(found)}")
    return found[0]


def find_logs():
    folders = [*LOCAL.glob("Packages/Claude_*/LocalCache/Local/Claude/logs"),
               LOCAL / "Claude/logs", ROAMING / "Claude/logs"]
    for folder in folders:
        if (folder / "main.log").is_file():
            return folder
    raise Unsure("Claude's main.log was not found")


def sign_ins(logs):
    """Every (account, organization) Claude opened, oldest first."""
    found = []
    for path in sorted(logs.glob("main*.log"), key=lambda p: p.stat().st_mtime_ns):
        found += SIGNED_IN.findall(path.read_bytes().decode("utf-8", "replace"))
    return found


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def desktop_running():
    """Whether Claude Desktop is running. A claude.exe whose path cannot be read counts as running."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = k.CreateToolhelp32Snapshot(0x2, 0)
    if snapshot in (None, wintypes.HANDLE(-1).value):
        raise Unsure("could not list processes")
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        if not more:
            raise Unsure("could not list processes")
        while more:
            if entry.szExeFile.lower() == "claude.exe":
                handle = k.OpenProcess(0x1000, False, entry.th32ProcessID)
                if not handle:
                    if ctypes.get_last_error() != 87:  # 87: the process has already exited.
                        return True
                else:
                    try:
                        size = wintypes.DWORD(32768)
                        name = ctypes.create_unicode_buffer(size.value)
                        if not k.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                            return True
                        if DESKTOP_EXE.search(name.value):
                            return True
                    finally:
                        k.CloseHandle(handle)
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        k.CloseHandle(snapshot)


def open_account(root, pairs):
    """The account Claude has open, or None when Claude is not running."""
    if not desktop_running():
        return None
    account = read(root / "config.json").get("lastKnownAccountUuid")
    if not account or not pairs or pairs[-1][0] != account:
        raise Unsure("Claude is running, but its config and log do not agree on the open account")
    return account


def read(path):
    return json.loads(path.read_bytes().decode("utf-8"))


def write(path, record, *, new):
    """Publish atomically. With new=True, an entry that has appeared meanwhile is left alone."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".chat-index-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(json.dumps(record).encode("ascii"))
            out.flush()
            os.fsync(out.fileno())
        if new:
            os.rename(temp, path)  # On Windows this fails if the target exists.
        else:
            os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def revision(record):
    return tuple(record.get(k) or 0 for k in ("lastActivityAt", "lastTurnEdgeAt", "latestUserFrameAt"))


def stamp(path):
    s = path.stat()
    return [path.name, s.st_mtime_ns, s.st_size]


def plan(root, state, active):
    """Work out the writes. Returns (actions, seen, stamps); actions is None when nothing changed since last time."""
    sessions = root / "claude-code-sessions"
    folders = {}
    for account, org in state["logins"]:
        folder = sessions / account / org
        if folder.is_dir() and folder.resolve().is_relative_to(sessions.resolve()):
            folders[f"{account}/{org}"] = (folder, account == active)
    stamps = {"active": active, "folders": {k: sorted(stamp(p) for p in f.glob("local_*.json"))
                                            for k, (f, _) in folders.items()}}
    if stamps == state.get("stamps"):
        return None, state["seen"], stamps

    records, marks, newest = {}, {}, {}
    for key, (folder, _) in folders.items():
        rows = records[key] = {}
        for path in folder.glob("local_*.json"):
            try:
                marks[path] = stamp(path)
                row = read(path)
            except FileNotFoundError:
                continue  # Claude removed this chat between the listing and the read.
            if row.get("sessionId", "") + ".json" != path.name:
                raise Unsure("a chat file's name does not match its session id")
            rows[path.name] = row
            if path.name not in newest or revision(row) > revision(newest[path.name]):
                newest[path.name] = row

    actions, seen = [], dict(state["seen"])
    for key, (folder, is_active) in folders.items():
        rows, known = records[key], set(state["seen"].get(key, []))
        if is_active:
            seen[key] = sorted(known | set(rows))  # Only what it has held; the rest is still to come.
            continue
        for name, source in newest.items():
            old = rows.get(name)
            if old is None and name in known:
                continue  # Removed from this account by the user.
            if old is not None and revision(old) >= revision(source):
                continue
            row = {**(old or {}), **{k: source[k] for k in HISTORY if k in source}}
            if old is None:
                row["isArchived"] = source.get("isArchived", False)
                row["isStarred"] = source.get("isStarred", False)
            path = folder / name
            actions.append((path, row, old is None, marks.get(path)))
        seen[key] = sorted(known | set(rows) | set(newest))
    return actions, seen, stamps


def run(root, logs, state_dir, apply):
    state_file = state_dir / "state.json"
    state = read(state_file) if state_file.exists() else {"logins": [], "seen": {}}
    pairs = sign_ins(logs)
    for pair in pairs:
        if list(pair) not in state["logins"]:
            state["logins"].append(list(pair))
    active = open_account(root, pairs)
    actions, seen, stamps = plan(root, state, active)
    if actions is None:
        return "no change"
    result = {"folders": len(stamps["folders"]), "claude_open": active is not None,
              "add": sum(a[2] for a in actions), "refresh": sum(not a[2] for a in actions),
              "skipped": 0}
    if not apply:
        return f"DRY RUN {result}"
    # Claude may have opened or switched accounts during the scan.
    if actions and open_account(root, sign_ins(logs)) != active:
        raise Unsure("the open account changed during this pass")
    for path, row, new, mark in actions:
        try:
            if not new and stamp(path) != mark:
                result["skipped"] += 1  # It changed since it was read; the next pass looks again.
                continue
            write(path, row, new=new)
        except (FileExistsError, FileNotFoundError):
            result["skipped"] += 1
    state["seen"] = seen
    state["stamps"] = stamps
    with tempfile.NamedTemporaryFile("w", dir=state_dir, suffix=".tmp", delete=False, encoding="ascii") as out:
        json.dump(state, out)
    os.replace(out.name, state_file)
    return f"APPLIED {result}" if actions else "no change"


def log(state_dir, message):
    """One line per event; a repeated message is written once. Never contains ids or titles."""
    path, last = state_dir / "sync.log", state_dir / "last-message.txt"
    if last.exists() and last.read_text("utf-8") == message:
        return
    if path.exists() and path.stat().st_size > 1_000_000:
        path.unlink()
    with path.open("a", encoding="utf-8") as out:
        out.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    last.write_text(message, "utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--root", type=Path, help="Claude data folder to use, e.g. a copy")
    parser.add_argument("--logs", type=Path, help="folder holding main.log")
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    with (args.state_dir / "sync.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return 0  # Another pass is running.
        try:
            message, code = run(args.root or find_root(), args.logs or find_logs(), args.state_dir, args.apply), 0
        except Unsure as e:
            message, code = f"SKIPPED: {e}", 0
        except Exception as e:
            message, code = f"ERROR: {type(e).__name__} at line {e.__traceback__.tb_lineno}", 1
        if args.apply and message != "no change":
            log(args.state_dir, message)
        if sys.stdout:
            print(message)
        return code


if __name__ == "__main__":
    sys.exit(main())
