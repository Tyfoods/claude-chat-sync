# claude-chat-sync

Keeps your Claude Desktop **Code** chats visible on every Claude account you sign in to on the same Mac. Not affiliated with or endorsed by Anthropic. It reads and writes Claude Desktop's own local chat-list files, which are undocumented, so an app update can break it. Back up `~/Library/Application Support/Claude*/claude-code-sessions` first, and follow the terms of your Claude plan.

It copies only the chat *index* files (titles, model, activity, where the chat lives), not transcripts. Claude Desktop itself keeps the transcripts, sign-ins, permissions and bridges.

## How it works
- Claude Desktop keeps one chat list per account and organization: `~/Library/Application Support/Claude*/claude-code-sessions/<account>/<org>/local_*.json`.
- The script runs every 60 seconds. For each chat it finds the newest copy across those folders and writes it into the others. For the account that's currently open, it never overwrites a chat entry already there, but it does add chats that are missing. It never brings back a chat you deleted from that account's list.
- It learns each new sign-in by reading `[LocalSessionManager] Initialization succeeded` lines in `~/Library/Logs/Claude/main.log`.
- Claude Desktop reads a sign-in's chat list once, when that sign-in opens. So the first time chats arrive for a sign-in that's already open, the script asks Claude to quit normally and reopens it once. It only does that when most of that list arrived in the same pass.
- State and errors: `~/Library/Application Support/Claude Chat Index/` (`state.json`, `sync-errors.log`).

## Install (macOS)
Needs Python 3 and Claude Desktop in /Applications. From this folder:

    ./install.sh

It's safe to run again: it updates the launch agent and keeps the existing state. Check it with `launchctl list | grep claude-chat` and `sync-errors.log`.

## Windows
The script is macOS-only. To port it, keep the same rules (newest copy wins, never touch the open account's folder, never resurrect deleted chats) and replace:
- `SUPPORT` with Claude's data folder (normally `%APPDATA%\Claude*`), and the log path with Claude's Windows log folder;
- `ps` / `osascript` / `open` with `tasklist`, a notification and a normal close/relaunch of Claude;
- `fcntl.flock` with `msvcrt.locking`, and launchd with a Task Scheduler task every minute.
