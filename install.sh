#!/bin/zsh
# Installs the launch agent that runs sync_claude_chats.py every 60 seconds. Safe to run again.
set -e
here=${0:A:h}
python=$(command -v python3)
state="$HOME/Library/Application Support/Claude Chat Index"
plist="$HOME/Library/LaunchAgents/local.claude-chat-sync.plist"
mkdir -p "$state" "$HOME/Library/LaunchAgents"
[ -f "$state/state.json" ] || print '{"logins": [], "seen": {}}' > "$state/state.json"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.claude-chat-sync</string>
  <key>ProgramArguments</key><array><string>$python</string><string>-I</string><string>$here/sync_claude_chats.py</string></array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardErrorPath</key><string>$state/sync-errors.log</string>
</dict></plist>
PLIST
launchctl bootout "gui/$UID/local.claude-chat-sync" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$plist"
print "Installed. Check: launchctl list | grep claude-chat-sync"
