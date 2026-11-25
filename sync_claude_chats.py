#!/usr/bin/env python3
"""Keep Claude Desktop's Code chat list the same on every account signed in on this computer (macOS and Windows).

Dry run unless --apply is given. One sync core for both systems; only the Mac and Windows classes at the end differ:
where Claude keeps its data and log, which account is open, and how Claude is quit and reopened.

Rules:
- Every account and organization Claude has opened gets the newest copy of every chat.
- In the open account's list, chats are only added, never overwritten.
- A chat removed from an account's list is not added back to it.
- If the open account cannot be established, nothing is written.
- Transcripts, sign-ins and settings are never touched.
- Claude reads a list when that sign-in opens. The first time most of the open list arrives in one pass, Claude is
  asked to quit the normal way and reopened, once. That waits while a Code turn is running, and never force-closes.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path

UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
SIGNED_IN = re.compile(
    r"\[LocalSessionManager\] Initialization succeeded[^\r\n]*?accountId=(" + UUID + r"), orgId=(" + UUID + r")")
CODE_TURN = re.compile(r"\[keep-awake\] code session hold (taken|released)")
HISTORY = """sessionId cliSessionId cwd originCwd model effort effortInherited createdAt lastActivityAt
lastTurnEdgeAt latestUserFrameAt title titleSource titleTurn previousTitles completedTurns lastAssistantUuid
postTurnSummary postTurnSummaryFor rewindEdges priorCliSessionIds transcriptModelStates sideSessionNotes
forkedFromSessionId spawnedFrom peerReceipts peerMessaged writtenBranches scheduledTaskId""".split()
REOPEN_WAIT = 30 * 60  # How long a reopen waits for running Code turns before asking the user to reopen instead.
REOPEN_NOTICE = "Your chats were copied into this sign-in. Quit and reopen Claude to see them."

# One Claude data folder: the account its config says is open, and its running main process, if any.
Profile = namedtuple("Profile", "root account running pid exe")


class Unsure(Exception):
    """Something could not be established, so this pass writes nothing."""


def read(path):
    return json.loads(path.read_bytes().decode("utf-8"))


def save(path, record, *, new=False):
    """Publish atomically. With new=True, an entry that has appeared meanwhile is left alone (FileExistsError)."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".chat-index-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(json.dumps(record).encode("ascii"))
            out.flush()
            os.fsync(out.fileno())
        if not new:
            os.replace(temp, path)
        elif os.name == "nt":
            os.rename(temp, path)  # Fails if the target exists.
        else:
            os.link(temp, path)  # Fails if the target exists.
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


def read_log(folder):
    """Every (account, organization) Claude opened, oldest first, and whether a Code turn is running now."""
    pairs, turn = [], None
    for path in sorted(folder.glob("main*.log"), key=lambda p: p.stat().st_mtime_ns):
        text = path.read_bytes().decode("utf-8", "replace")
        pairs += SIGNED_IN.findall(text)
        turn = ([turn] + CODE_TURN.findall(text))[-1]
    return pairs, turn == "taken"


def upgrade(state, profiles):
    """The first Windows script keyed folders as account/org under its one data folder; keys are now full paths."""
    if len(profiles) == 1:
        base = profiles[0].root / "claude-code-sessions"
        state["seen"] = {k if os.path.isabs(k) else str(base / k): v for k, v in state["seen"].items()}


def chat_folders(profiles, logins, latest):
    """Each chat folder by key: (folder, profile, whether it is open, whether it is the list on screen)."""
    folders = {}
    for profile in profiles:
        for account, org in logins:
            if not (re.fullmatch(UUID, account) and re.fullmatch(UUID, org)):
                raise Unsure("the state file holds a sign-in that is not an id")
            folder = profile.root / "claude-code-sessions" / account / org
            is_open = profile.running and account == profile.account
            folders[str(folder)] = (folder, profile, is_open, is_open and (account, org) == latest)
    return folders


def plan(folders, state):
    """Work out the writes: (actions, seen, stamps, filled). actions is None when nothing changed since last time."""
    stamps = {"open": sorted(k for k, f in folders.items() if f[2]),
              "files": {k: sorted(stamp(p) for p in f[0].glob("local_*.json")) for k, f in folders.items()}}
    if stamps == state.get("stamps"):
        return None, state["seen"], stamps, []

    records, marks, newest = {}, {}, {}
    for key, (folder, *_) in folders.items():
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

    actions, seen, filled = [], dict(state["seen"]), []
    for key, (folder, _, is_open, shown) in folders.items():
        rows, known = records[key], set(state["seen"].get(key, []))
        added = 0
        for name, source in newest.items():
            old = rows.get(name)
            if old is None and name in known:
                continue  # Removed from this account by the user.
            if old is not None and (is_open or revision(old) >= revision(source)):
                continue  # The open account's own entries are left to Claude.
            row = {**(old or {}), **{k: source[k] for k in HISTORY if k in source}}
            if old is None:
                row["isArchived"] = source.get("isArchived", False)
                row["isStarred"] = source.get("isStarred", False)
            path = folder / name
            actions.append((path, row, old is None, marks.get(path)))
            added += old is None
        seen[key] = sorted(known | set(rows) | set(newest))
        # A first sign-in shows a near-empty list; most of it arriving now means the list on screen is stale.
        if shown and added > len(rows) and key not in state["reopened"]:
            filled.append(key)
    return actions, seen, stamps, filled


def run(system, state_dir, apply, may_reopen):
    state_file = state_dir / "state.json"
    state = read(state_file) if state_file.exists() else {"logins": [], "seen": {}}
    state.setdefault("reopened", [])
    before = json.dumps(state, sort_keys=True)
    profiles = system.profiles()
    upgrade(state, profiles)
    pairs, busy = read_log(system.logs())
    current = {p.account for p in profiles if p.running}
    if None in current:
        raise Unsure("Claude is running, but its config does not name the open account")
    for account, org in pairs:
        # Only sign-ins of an open account count: mid-switch the log briefly pairs the old account with the new org.
        if account in current and [account, org] not in state["logins"]:
            state["logins"].append([account, org])
    latest = pairs[-1] if pairs else None
    if latest and current and latest[0] not in current:
        raise Unsure("Claude is running, but its config and log do not agree on the open account")

    folders = chat_folders(profiles, state["logins"], latest)
    actions, seen, stamps, filled = plan(folders, state)
    message = "no change"
    if actions is not None:
        result = {"folders": len(folders), "claude_open": bool(current), "add": sum(a[2] for a in actions),
                  "refresh": sum(not a[2] for a in actions), "skipped": 0, "reopen": bool(filled)}
        if not apply:
            return f"DRY RUN {result}"
        if actions and system.profiles() != profiles:
            raise Unsure("Claude opened, quit or switched accounts during this pass")
        for path, row, new, mark in actions:
            try:
                if not new and stamp(path) != mark:
                    result["skipped"] += 1  # It changed since it was read; the next pass looks again.
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                save(path, row, new=new)
            except (FileExistsError, FileNotFoundError):
                result["skipped"] += 1
        state["seen"], state["stamps"] = seen, stamps
        for key in filled:
            profile = folders[key][1]
            state["reopened"].append(key)
            state["reopen"] = {"root": str(profile.root), "pid": profile.pid, "since": time.time()}
        if actions:
            message = f"APPLIED {result}"
    elif not apply:
        return "DRY RUN no change"

    reopen = None
    pending = state.get("reopen")
    if pending:
        profile = next((p for p in profiles if str(p.root) == pending["root"]), None)
        if profile is None or not profile.running or profile.pid != pending["pid"]:
            del state["reopen"]  # Claude was reopened since, so it read the list then.
        elif may_reopen and not busy:
            del state["reopen"]
            reopen = profile
        elif time.time() - pending["since"] > REOPEN_WAIT:
            del state["reopen"]
            system.notify(REOPEN_NOTICE)
            message = "REOPEN GAVE UP: a Code turn kept running; asked the user to reopen Claude"
    if json.dumps(state, sort_keys=True) != before:
        save(state_file, state)
    if reopen:
        system.reopen(reopen)
        message = "REOPENED Claude" if message == "no change" else message + "; REOPENED Claude"
    return message


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
    system = Windows() if os.name == "nt" else Mac() if sys.platform == "darwin" else None
    if system is None:
        sys.exit("Only macOS and Windows are supported.")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--no-reopen", action="store_true", help="leave a due reopen of Claude to a later pass")
    parser.add_argument("--root", type=Path, help="Claude data folder to use, e.g. a copy")
    parser.add_argument("--logs", type=Path, help="folder holding Claude's main.log")
    parser.add_argument("--state-dir", type=Path, default=system.state_dir)
    args = parser.parse_args()
    system.root_override, system.logs_override = args.root, args.logs
    args.state_dir.mkdir(parents=True, exist_ok=True)
    with (args.state_dir / "sync.lock").open("a+b") as lock:
        if not system.lock(lock):
            return 0  # Another pass is running.
        try:
            message, code = run(system, args.state_dir, args.apply, not args.no_reopen), 0
        except Unsure as e:
            message, code = f"SKIPPED: {e}", 0
        except Exception as e:
            message, code = f"ERROR: {type(e).__name__} at line {e.__traceback__.tb_lineno}", 1
        if args.apply and message != "no change":
            log(args.state_dir, message)
        if sys.stdout:
            print(message)
        return code


class Mac:
    APP = "/Applications/Claude.app"
    SUPPORT = Path.home() / "Library/Application Support"
    state_dir = SUPPORT / "Claude Chat Index"
    root_override = logs_override = None

    def profiles(self):
        roots = [self.root_override] if self.root_override else sorted(self.SUPPORT.glob("Claude*"))
        result = []
        for root in roots:
            if not (root / "claude-code-sessions").is_dir():
                continue
            config = read(root / "config.json")
            pid = config.get("updaterRunningInstance", {}).get("pid")
            cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True).stdout.strip() if pid else ""
            running = cmd.startswith(self.APP + "/Contents/MacOS/Claude") and (
                f"--user-data-dir={root}" in cmd or root.name == "Claude" and "--user-data-dir" not in cmd)
            result.append(Profile(root, config.get("lastKnownAccountUuid"), running, pid if running else None, None))
        return result

    def logs(self):
        return self.logs_override or Path.home() / "Library/Logs/Claude"

    def lock(self, file):
        import fcntl
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def notify(self, message):
        subprocess.run(["osascript", "-e", f'display notification "{message}" with title "Claude chats"'])

    def reopen(self, profile):
        self.notify("Copied your chats into this sign-in. Reopening Claude to show them.")
        # A normal quit, as with Cmd-Q: Claude still asks before stopping running work.
        subprocess.run(["osascript", "-l", "JavaScript", "-e", 'ObjC.import("AppKit");'
                        f"$.NSRunningApplication.runningApplicationWithProcessIdentifier({profile.pid}).terminate"],
                       check=True)
        deadline = time.monotonic() + 120
        while subprocess.run(["ps", "-p", str(profile.pid)], capture_output=True).returncode == 0:
            if time.monotonic() > deadline:
                return self.notify(REOPEN_NOTICE)
            time.sleep(1)
        extra = [] if profile.root.name == "Claude" else ["-n", "--args", f"--user-data-dir={profile.root}"]
        subprocess.run(["open", "-a", self.APP, *extra], check=True)


class Windows:
    # Claude Code's command line is also claude.exe, so Desktop is recognised by where it is installed.
    DESKTOP_EXE = re.compile(
        r"\\WindowsApps\\Claude_[^\\]+\\app\\claude\.exe$|\\AnthropicClaude\\(?:app-[^\\]+\\)?claude\.exe$", re.I)
    root_override = logs_override = None

    def __init__(self):
        self.local, self.roaming = Path(os.environ["LOCALAPPDATA"]), Path(os.environ["APPDATA"])
        self.state_dir = self.local / "Claude Chat Index"

    def profiles(self):
        root = self.root_override or self.find_root()
        running, pid, exe = self.desktop()
        account = read(root / "config.json").get("lastKnownAccountUuid") if running else None
        return [Profile(root, account, running, pid, exe)]

    def find_root(self):
        """Claude's data folder. The Store package's own path also works outside the package, so it is preferred."""
        packaged = [p for p in self.local.glob("Packages/Claude_*/LocalCache/Roaming/Claude")
                    if (p / "claude-code-sessions").is_dir()]
        found = packaged or [p for p in [self.roaming / "Claude"] if (p / "claude-code-sessions").is_dir()]
        if len(found) != 1:
            raise Unsure(f"expected one Claude data folder, found {len(found)}")
        return found[0]

    def logs(self):
        if self.logs_override:
            return self.logs_override
        for folder in [*self.local.glob("Packages/Claude_*/LocalCache/Local/Claude/logs"),
                       self.local / "Claude/logs", self.roaming / "Claude/logs"]:
            if (folder / "main.log").is_file():
                return folder
        raise Unsure("Claude's main.log was not found")

    def lock(self, file):
        import msvcrt
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    @staticmethod
    def win32():
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        u = ctypes.WinDLL("user32", use_last_error=True)
        k.CreateToolhelp32Snapshot.restype = k.OpenProcess.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                          wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        return ctypes, wintypes, k, u

    def desktop(self):
        """(running, main process id, its path). A claude.exe whose path cannot be read counts as running."""
        ctypes, wintypes, k, _ = self.win32()

        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("pid", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("ppid", wintypes.DWORD),
                        ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        snapshot = k.CreateToolhelp32Snapshot(0x2, 0)
        if snapshot in (None, wintypes.HANDLE(-1).value):
            raise Unsure("could not list processes")
        found, unreadable = {}, False
        try:
            entry = Entry(dwSize=ctypes.sizeof(Entry))
            more = k.Process32FirstW(snapshot, ctypes.byref(entry))
            if not more:
                raise Unsure("could not list processes")
            while more:
                if entry.szExeFile.lower() == "claude.exe":
                    handle = k.OpenProcess(0x1000, False, entry.pid)
                    if not handle:
                        unreadable |= ctypes.get_last_error() != 87  # 87: the process has already exited.
                    else:
                        try:
                            size = wintypes.DWORD(32768)
                            name = ctypes.create_unicode_buffer(size.value)
                            if not k.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                                unreadable = True
                            elif self.DESKTOP_EXE.search(name.value):
                                found[entry.pid] = (entry.ppid, name.value)
                        finally:
                            k.CloseHandle(handle)
                more = k.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            k.CloseHandle(snapshot)
        mains = [(pid, path) for pid, (ppid, path) in found.items() if ppid not in found]
        if len(mains) == 1:
            return (True, *mains[0])
        return bool(found) or unreadable, None, None

    def notify(self, message):
        """A message box from a separate process, so this pass does not wait for it."""
        code = f"import ctypes; ctypes.windll.user32.MessageBoxW(0, {message!r}, 'Claude chats', 0x40040)"
        subprocess.Popen([sys.executable, "-I", "-c", code], creationflags=0x8)  # DETACHED_PROCESS

    def reopen(self, profile):
        ctypes, wintypes, k, u = self.win32()
        if profile.pid is None:
            return self.notify(REOPEN_NOTICE)
        windows = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(hwnd, _):
            owner = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == profile.pid:
                windows.append(hwnd)
            return True

        u.EnumWindows(collect, 0)
        process = k.OpenProcess(0x00100000, False, profile.pid)  # SYNCHRONIZE
        if not process:
            return self.launch(profile.exe)
        try:
            # What Windows sends when an installer needs an app closed (ENDSESSION_CLOSEAPP). Claude answers it with
            # its own orderly quit; nothing is killed. Sent with a timeout, so a busy window cannot stall this pass.
            done = ctypes.c_size_t()
            for message, wparam in ((0x11, 0), (0x16, 1)):  # WM_QUERYENDSESSION, then WM_ENDSESSION
                for hwnd in windows:
                    u.SendMessageTimeoutW(hwnd, message, wparam, 0x1, 0x2, 5000, ctypes.byref(done))
            if k.WaitForSingleObject(process, 120_000) != 0:
                return self.notify(REOPEN_NOTICE)
        finally:
            k.CloseHandle(process)
        self.launch(profile.exe)

    def launch(self, exe):
        package = re.search(r"\\WindowsApps\\([^\\_]+)_[^\\]*__([^\\]+)\\", exe, re.I)
        if package:
            manifest = (Path(exe[:package.end()]) / "AppxManifest.xml").read_text("utf-8")
            app = re.search(r'<Application Id="([^"]+)" Executable="app\\Claude\.exe"', manifest, re.I)
            target = f"shell:AppsFolder\\{package[1]}_{package[2]}!{app[1] if app else 'Claude'}"
            subprocess.Popen(["explorer.exe", target])
        else:
            stub = Path(exe).parent.parent / "claude.exe"  # The launcher above app-<version>
            use = stub if Path(exe).parent.name.startswith("app-") and stub.is_file() else Path(exe)
            subprocess.Popen([str(use)], creationflags=0x8)


if __name__ == "__main__":
    sys.exit(main())
