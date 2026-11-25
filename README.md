# claude-chat-sync

Keeps your Claude Desktop **Code** chats visible on every Claude account you sign in to on the same computer, on macOS and Windows. Not affiliated with or endorsed by Anthropic. It reads and writes Claude Desktop's own local chat-list files, which are undocumented, so an app update can break it. Back up `claude-code-sessions` first (see below for where it is), and follow the terms of your Claude plan.

It copies only the chat *index* files (titles, model, activity, where the chat lives), not transcripts. Claude Desktop itself keeps the transcripts, sign-ins, permissions and bridges.

## How it works
One script, `sync_claude_chats.py`, does the same thing on both systems. Only where Claude keeps its files and how Claude is quit and reopened differ.
- Claude Desktop keeps one chat list per account and organization: `claude-code-sessions/<account>/<org>/local_*.json` in its data folder.
- The script runs every 60 seconds. For each chat it finds the newest copy across those folders and writes it into the others. For the account that's currently open, it never overwrites a chat entry already there, but it does add chats that are missing. It never brings back a chat you deleted from that account's list.
- It learns each new sign-in by reading `[LocalSessionManager] Initialization succeeded` lines in Claude's `main.log`, and only for the account Claude has open. If Claude is running and its config and log disagree on which account is open (for example mid-switch), it writes nothing that pass.
- Claude Desktop reads a sign-in's chat list once, when that sign-in opens. So the first time most of an open sign-in's list arrives in one pass, the script asks Claude to quit the normal way and reopens it, once per sign-in. It waits while a Code turn is running (Claude logs that). If a turn keeps running for 30 minutes, it asks you to reopen Claude yourself instead. It never force-closes Claude.
  - macOS: a normal quit, as with Cmd-Q, so Claude still asks before stopping running work.
  - Windows: the request Windows sends when an installer needs an app closed. Claude answers it with its own orderly quit.
- It's a dry run unless `--apply` is given. `--root` points it at a copy of Claude's data folder, `--no-reopen` leaves a due reopen to a later pass.
- State and log (`state.json`, `sync.log`): `~/Library/Application Support/Claude Chat Index/` on macOS, `%LOCALAPPDATA%\Claude Chat Index\` on Windows.

## Install (macOS)
Needs Python 3 and Claude Desktop in /Applications. Back up `~/Library/Application Support/Claude*/claude-code-sessions`. From this folder:

    ./install.sh

It's safe to run again: it updates the launch agent and keeps the existing state. Check it with `launchctl list | grep claude-chat` and `sync.log`.

## Install (Windows)
Needs Python 3 and Claude Desktop. Back up `claude-code-sessions` (Store install: `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`). Keep this folder outside AppData, such as `%USERPROFILE%\claude-chat-sync`. Do a dry run, then install the scheduled task. In PowerShell, from this folder:

    python -I -B .\sync_claude_chats.py
    .\install.ps1

It's safe to run again: it updates the scheduled task and keeps the existing state. Check it with `Get-ScheduledTaskInfo claude-chat-sync` and `sync.log`. Remove it with `Unregister-ScheduledTask -TaskName claude-chat-sync`.

Tested on Windows 10 with the Store (MSIX) install and Python 3.11.

## License
MIT. See LICENSE.
