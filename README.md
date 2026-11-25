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
`sync_claude_chats_windows.py` is a separate, more cautious version. It needs Python 3 and Claude Desktop. Differences from the Mac script:
- It never writes to the open account's folders. Chats reach that account once it is no longer open (another account is open, or Claude has been fully quit for a minute).
- It never closes or reopens Claude.
- If it can't establish which account is open, it writes nothing and says so in `sync.log`.
- It is a dry run unless `--apply` is given. `--root` points it at a copy of Claude's data folder.

Back up `claude-code-sessions` first (Store install: `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude`). Put the script in a folder outside AppData, such as `%USERPROFILE%\claude-chat-sync`; it keeps `state.json` and `sync.log` beside itself. Do a dry run, then register it in PowerShell:

    $dir = "$env:USERPROFILE\claude-chat-sync"
    $pyw = (Get-Command pythonw).Source
    $action = New-ScheduledTaskAction -Execute $pyw -Argument "-I -B `"$dir\sync_claude_chats_windows.py`" --apply" -WorkingDirectory $dir
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName claude-chat-sync -Action $action -Trigger $trigger -Settings $settings

Remove it with `Unregister-ScheduledTask -TaskName claude-chat-sync`.

Tested on Windows 10 with the Store (MSIX) install and Python 3.11.

## License
MIT. See LICENSE.
