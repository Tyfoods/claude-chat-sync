# Installs the scheduled task that runs sync_claude_chats.py every 60 seconds. Safe to run again; keeps the state.
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$pyw = (Get-Command pythonw).Source
New-Item -ItemType Directory -Force (Join-Path $env:USERPROFILE '.claude-chat-index') | Out-Null
$action = New-ScheduledTaskAction -Execute $pyw -Argument "-I -B `"$here\sync_claude_chats.py`" --apply" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName claude-chat-sync -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
"Installed. Check: Get-ScheduledTaskInfo claude-chat-sync"
