Here's what's in the current background-music / bgmus:

# Scheduling

Time-based schedule from a config file (9-10 file.mp3)
Day-of-week prefixes (Mon 18:30-18:40 file.mp3)
Wildcard minute ranges (*:10-*:15 file.mp3)
Source types

# Single file (loops)
random: — pick randomly from a list or playlist file
Sequential playlist (.txt file, works through in order)
continuing: — persistent position across sessions, resumes where it left off
continuing+random: — persistent sequential, then random fill when exhausted
Directory source — scans directory alphabetically instead of a playlist file

# Format handling

Whitelist of supported formats: mp3, wav, ogg, flac
Warns and skips unsupported formats (e.g. opus)
Runtime

# Fade in/out between slots
inotify config file watching — wakes up immediately on config change rather than waiting for next slot
Config hot-reload without restart
Persistent playlist state saved to .state file
CLI

bgmus config — run daemon
bgmus --check config — parse and print schedule, then exit
bgmus --play config — play current active slot immediately
bgmus --play FILE config — play a specific file immediately

# Logging

Slot changes with timestamp
What's playing
Next slot shown in sleep message
Warns when no files resolve
Logs config reads and reloads

