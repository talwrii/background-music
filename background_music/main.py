#!/usr/bin/env python3
"""
bgmus - Background music scheduler daemon.
Plays music according to a time-based schedule, with fade between slots.

Usage:
    bgmus <config>                  Run the scheduler daemon
    bgmus --check <config>          Parse and print the schedule, then exit
    bgmus --play <config>           Play the current active slot immediately
    bgmus --play FILE <config>      Play a specific file immediately
    bgmus --cmd "pause 1h"          Send a command to a running daemon
    bgmus --cmd "play FILE"         Tell running daemon to play FILE, then resume
    bgmus --cmd status              Ask running daemon what it's doing
    bgmus --socket PATH ...         Override default socket location

Config format:
    9-10 file.wav
    10-11 random:file2.wav,file3.wav
    11-13 playlist.txt          # work through sequentially
    13-14 random:playlist.txt
    13-14 continuing:playlist.txt       # persistent position across sessions
    13-14 continuing+random:playlist.txt # persistent, then random fill
    Mon 18:30-18:40 monday.wav
    *:10-*:15 file.wav          # 10-15 minutes past every hour

Remote control commands (via --cmd):
    pause DURATION   Pause for e.g. 30s, 15m, 1h, 90m, 2h30m
    resume           Cancel an active pause (no-op if not paused)
    play FILE        Interrupt current playback, play FILE, then resume schedule
    status           Print current state and active slot
"""
import argparse
import json
import os
import random
import re
import select
import signal
import socket
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import inotify_simple
import pygame.mixer as mixer

FADE_STEPS    = 20    # number of volume steps when fading out
FADE_DURATION = 3.0   # seconds to fade out over
TICK          = 5     # seconds between track-end checks while playing a playlist
SUPPORTED     = {".mp3", ".wav", ".ogg", ".flac"}

CONFIG_FORMAT = """
config format:
  9-10 file.wav                    # play daily 9-10am
  10-11 random:a.wav,b.wav         # pick one at random
  11-13 playlist.txt               # work through sequentially
  13-14 random:playlist.txt        # random from playlist
  13-14 continuing:playlist.txt    # persistent position across sessions
  13-14 continuing+random:playlist.txt  # persistent, then random fill
  Mon 18:30-18:40 monday.wav       # specific day + HH:MM times
  *:10-*:15 file.wav               # 10-15 minutes past every hour

  time ranges: H-H or HH:MM-HH:MM or *:MM-*:MM
  day prefix:  Mon Tue Wed Thu Fri Sat Sun
  random:      comma-separated files or a .txt playlist or directory
  playlist:    one file per line, # comments ignored
  continuing:  playlist with persistent position between sessions
  continuing+random: persistent position, then random fill when exhausted

remote control:
  bgmus --cmd "pause 1h"           # pause daemon for 1 hour
  bgmus --cmd resume               # cancel an active pause
  bgmus --cmd "play /path/song.wav"  # interrupt, play once, resume schedule
  bgmus --cmd status               # print daemon status
"""

# Socket location
def default_socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "bgmus.sock")
    return f"/tmp/bgmus-{os.getuid()}.sock"

# Duration parsing
DURATION_RE = re.compile(r"(\d+)\s*([hms])", re.IGNORECASE)

def parse_duration(s):
    """Parse '1h', '30m', '45s', '1h30m', '90m' into seconds."""
    s = s.strip().lower()
    if not s:
        raise ValueError("empty duration")
    total = 0
    matched = 0
    for m in DURATION_RE.finditer(s):
        n, unit = int(m.group(1)), m.group(2)
        if unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
        elif unit == "s":
            total += n
        matched += len(m.group(0))
    # reject garbage: require whole string be consumed (allowing whitespace)
    stripped = re.sub(r"\s+", "", s)
    consumed = re.sub(r"\s+", "", "".join(m.group(0) for m in DURATION_RE.finditer(s)))
    if consumed != stripped or total == 0:
        raise ValueError(f"invalid duration {s!r} — use e.g. 30s, 15m, 1h, 1h30m")
    return total

# Audio init
def init_audio():
    mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=2048)
    mixer.init()
    print(f"[bgmus] Audio: {mixer.get_init()}")

# Formatting
def fmt_duration(secs):
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"

# --- Config parsing (unchanged) ---
class ScheduleEntry:
    def __init__(self, days, start, end, source):
        self.days   = days
        self.start  = start
        self.end    = end
        self.source = source

    def __repr__(self):
        days = f"day={self.days}" if self.days else "daily"
        sh = "*" if self.start[0] is None else f"{self.start[0]:02d}"
        eh = "*" if self.end[0]   is None else f"{self.end[0]:02d}"
        return f"<{days} {sh}:{self.start[1]:02d}-{eh}:{self.end[1]:02d} {self.source}>"

DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

def parse_time(s):
    if s.startswith("*:"):
        return (None, int(s[2:]))
    if ":" in s:
        h, m = s.split(":")
        return int(h), int(m)
    return int(s), 0

def parse_line(line, lineno):
    line = line.split("#")[0].strip()
    if not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        raise ValueError(f"Line {lineno}: too few fields: {line!r}")
    days = None
    if re.match(r'^[A-Za-z]{3}(,[A-Za-z]{3})*$', parts[0]):
        day_strs = parts[0].split(",")
        days = set()
        for ds in day_strs:
            if ds.lower() not in DAY_MAP:
                raise ValueError(f"Line {lineno}: unknown day {ds!r}")
            days.add(DAY_MAP[ds.lower()])
        parts = parts[1:]
    time_part = parts[0]
    if "-" not in time_part:
        raise ValueError(f"Line {lineno}: expected time range like 9-10 or 09:00-10:00 or *:MM-*:MM")
    start_s, end_s = time_part.split("-", 1)
    start = parse_time(start_s)
    end   = parse_time(end_s)
    if (start[0] is None) != (end[0] is None):
        raise ValueError(f"Line {lineno}: wildcard must appear in both start and end (e.g. *:10-*:15)")
    if start[0] is None and end[1] <= start[1]:
        raise ValueError(f"Line {lineno}: wildcard range end minute must be greater than start minute")
    source = " ".join(parts[1:]).split("#")[0].strip()
    if not source:
        raise ValueError(f"Line {lineno}: missing source")
    return ScheduleEntry(days, start, end, source)

def load_config(path):
    print(f"[bgmus] Reading config from {path}")
    entries = []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            try:
                entry = parse_line(line, i)
                if entry:
                    entries.append(entry)
            except ValueError as e:
                print(f"[bgmus] Config error: {e}", file=sys.stderr)
                sys.exit(1)
    return entries

# --- Source resolution (unchanged) ---
def load_playlist(path):
    path = Path(path)
    if path.is_dir():
        files = sorted(
            str(f) for f in path.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED
        )
        return files
    files = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                files.append(line)
    return files

def is_playlist_or_dir(path):
    p = Path(path)
    if p.is_dir():
        return True
    return p.suffix.lower() not in SUPPORTED

class SequentialPlaylist:
    def __init__(self, files):
        self.files = files
        self.index = 0

    def next(self):
        if not self.files:
            return None, False
        f = self.files[self.index % len(self.files)]
        self.index += 1
        return f, False

class ContinuingPlaylist:
    def __init__(self, playlist_path, random_fill=False):
        self.playlist_path = Path(playlist_path)
        if self.playlist_path.is_dir():
            self.state_path = self.playlist_path.with_name(self.playlist_path.name + ".state")
        else:
            self.state_path = self.playlist_path.with_suffix(self.playlist_path.suffix + ".state")
        self.random_fill = random_fill
        self.files       = load_playlist(playlist_path)
        self.index       = self._load_state()
        self.exhausted   = self.index >= len(self.files)

    def _load_state(self):
        try:
            data = json.loads(self.state_path.read_text())
            idx = int(data.get("index", 0))
            print(f"[bgmus] continuing: resuming at index {idx} of {len(self.files)}")
            return idx
        except Exception:
            return 0

    def _save_state(self):
        try:
            self.state_path.write_text(json.dumps({"index": self.index}))
        except Exception as e:
            print(f"[bgmus] Warning: could not save continuing state: {e}", file=sys.stderr)

    def next(self):
        if not self.files:
            return None, False
        if self.index < len(self.files):
            f = self.files[self.index]
            self.index += 1
            self._save_state()
            if self.index >= len(self.files):
                self.exhausted = True
                self.index = 0
                self._save_state()
                print(f"[bgmus] continuing: playlist exhausted, will restart from beginning next session")
            return f, False
        else:
            if self.random_fill:
                f = random.choice(self.files)
                return f, False
            return None, False

def resolve_source(source, config_dir, playlists):
    if source.startswith("continuing+random:"):
        spec = source[len("continuing+random:"):]
        path = str(config_dir / spec)
        key = f"continuing+random:{path}"
        if key not in playlists:
            playlists[key] = ContinuingPlaylist(path, random_fill=True)
        return playlists[key].next
    elif source.startswith("continuing:"):
        spec = source[len("continuing:"):]
        path = str(config_dir / spec)
        key = f"continuing:{path}"
        if key not in playlists:
            playlists[key] = ContinuingPlaylist(path, random_fill=False)
        return playlists[key].next
    elif source.startswith("random:"):
        spec = source[len("random:"):]
        full = config_dir / spec
        if is_playlist_or_dir(full):
            files = load_playlist(full)
        else:
            files = [str(config_dir / f.strip()) for f in spec.split(",")]
        return (lambda files: lambda: (random.choice(files), False) if files else (None, False))(files)
    elif is_playlist_or_dir(config_dir / source):
        key = str(config_dir / source)
        if key not in playlists:
            playlists[key] = SequentialPlaylist(load_playlist(config_dir / source))
        return playlists[key].next
    else:
        path = str(config_dir / source)
        ext = Path(path).suffix.lower()
        if ext not in SUPPORTED:
            print(f"[bgmus] Warning: unsupported format {ext!r}, skipping {path}", file=sys.stderr)
            return lambda: (None, False)
        return lambda: (path, True)

# --- Player (unchanged) ---
class Player:
    def __init__(self):
        self._vol    = 1.0
        self.looping = False
        self._playing = False

    def play(self, path, loop=False):
        self.stop()
        try:
            self.looping  = loop
            self._vol     = 1.0
            mixer.music.load(path)
            mixer.music.set_volume(self._vol)
            mixer.music.play(loops=-1 if loop else 0)
            self._playing = True
            print(f"[bgmus] Playing: {path}")
        except Exception as e:
            print(f"[bgmus] Error playing {path}: {e}", file=sys.stderr)
            self._playing = False

    def set_volume(self, vol):
        self._vol = max(0.0, min(1.0, vol))
        mixer.music.set_volume(self._vol)

    def fade_out(self):
        if not self.is_playing():
            return
        step     = self._vol / FADE_STEPS
        interval = FADE_DURATION / FADE_STEPS
        for _ in range(FADE_STEPS):
            self.set_volume(self._vol - step)
            time.sleep(interval)
        self.stop()

    def stop(self):
        mixer.music.stop()
        self._playing = False
        self.looping  = False

    def is_playing(self):
        return mixer.music.get_busy()

# --- Scheduler ---
def in_range(start, end, hour, minute):
    if start[0] is None:
        return start[1] <= minute < end[1]
    return start <= (hour, minute) < end

def next_event_time(entries, now):
    candidates = []
    for entry in entries:
        if entry.start[0] is None:
            for minute in [entry.start[1], entry.end[1]]:
                t = now.replace(minute=minute, second=0, microsecond=0)
                if t <= now:
                    t += timedelta(hours=1)
                candidates.append(t)
        else:
            for boundary_hm in [entry.start, entry.end]:
                for day_offset in range(8):
                    d = now + timedelta(days=day_offset)
                    if entry.days is not None and d.weekday() not in entry.days:
                        continue
                    t = d.replace(hour=boundary_hm[0], minute=boundary_hm[1],
                                  second=0, microsecond=0)
                    if t > now:
                        candidates.append(t)
                        break
    return min(candidates) if candidates else None

def active_entry(entries, now):
    dow = now.weekday()
    for entry in entries:
        if entry.days is not None and dow not in entry.days:
            continue
        if in_range(entry.start, entry.end, now.hour, now.minute):
            return entry
    return None

def make_watcher(config_path):
    inotify = inotify_simple.INotify()
    flags   = inotify_simple.flags.CLOSE_WRITE | inotify_simple.flags.MOVED_TO
    inotify.add_watch(str(Path(config_path).parent), flags)
    return inotify

def drain_inotify(inotify, config_path):
    """Read all pending inotify events, return True if config changed."""
    events = inotify.read(timeout=0)
    name   = Path(config_path).name
    matched = [e for e in events if e.name == name]
    for e in matched:
        print(f"[bgmus] inotify event: {e.name!r} flags={inotify_simple.flags.from_mask(e.mask)}")
    return bool(matched)

# --- Control state ---
class Controller:
    """Holds daemon's runtime state: pause timer and one-shot play request."""
    def __init__(self):
        self.pause_until = None   # datetime or None
        self.oneshot    = None    # path string or None (file to play once, then resume)
        self.oneshot_playing = False  # True while oneshot is active

    def is_paused(self, now=None):
        if self.pause_until is None:
            return False
        now = now or datetime.now()
        if now >= self.pause_until:
            print(f"[bgmus] Pause expired at {now.strftime('%H:%M:%S')}")
            self.pause_until = None
            return False
        return True

    def pause_remaining(self, now=None):
        if self.pause_until is None:
            return 0
        now = now or datetime.now()
        return max(0, (self.pause_until - now).total_seconds())

    def set_pause(self, duration_secs):
        self.pause_until = datetime.now() + timedelta(seconds=duration_secs)
        print(f"[bgmus] Pausing for {fmt_duration(duration_secs)}, until {self.pause_until.strftime('%H:%M:%S')}")

    def request_oneshot(self, path):
        self.oneshot = path
        print(f"[bgmus] One-shot queued: {path}")

    def next_wake_override(self):
        """If paused, we want to wake when pause expires."""
        return self.pause_until

# --- Socket server ---
def make_socket_server(socket_path):
    """Create a Unix domain socket listening for commands."""
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[bgmus] Warning: could not remove stale socket {socket_path}: {e}", file=sys.stderr)
    parent = os.path.dirname(socket_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(socket_path)
    os.chmod(socket_path, 0o600)
    s.listen(4)
    s.setblocking(False)
    print(f"[bgmus] Listening on {socket_path}")
    return s

def handle_command(line, controller, entries):
    """Parse and apply a command line. Return a reply string."""
    line = line.strip()
    if not line:
        return "error: empty command\n"
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "pause":
        if not arg:
            return "error: pause requires a duration (e.g. 'pause 1h')\n"
        try:
            secs = parse_duration(arg)
        except ValueError as e:
            return f"error: {e}\n"
        controller.set_pause(secs)
        return f"ok: paused for {fmt_duration(secs)}\n"

    elif cmd == "play":
        if not arg:
            return "error: play requires a file path\n"
        path = arg
        if not os.path.isfile(path):
            return f"error: file not found: {path}\n"
        ext = Path(path).suffix.lower()
        if ext not in SUPPORTED:
            return f"error: unsupported format {ext}\n"
        controller.request_oneshot(path)
        return f"ok: queued one-shot {path}\n"

    elif cmd == "resume":
        if controller.pause_until is None:
            return "ok: not paused\n"
        controller.pause_until = None
        return "ok: resumed\n"

    elif cmd == "status":
        now = datetime.now()
        lines = [f"time: {now.strftime('%Y-%m-%d %H:%M:%S')}"]
        if controller.is_paused(now):
            remaining = controller.pause_remaining(now)
            lines.append(f"state: paused (resumes in {fmt_duration(remaining)}, at {controller.pause_until.strftime('%H:%M:%S')})")
        elif controller.oneshot_playing:
            lines.append(f"state: one-shot playing")
        else:
            lines.append("state: normal")
        entry = active_entry(entries, now)
        if entry:
            lines.append(f"active_slot: {entry}")
        else:
            lines.append("active_slot: none")
        if controller.oneshot:
            lines.append(f"oneshot_queued: {controller.oneshot}")
        return "\n".join(lines) + "\n"

    else:
        return f"error: unknown command {cmd!r} (pause|resume|play|status)\n"

def accept_and_handle(server, controller, entries):
    """Accept any pending connections and process one command per connection."""
    while True:
        try:
            conn, _ = server.accept()
        except BlockingIOError:
            return
        try:
            conn.settimeout(2.0)
            data = b""
            while b"\n" not in data and len(data) < 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                data += chunk
            line = data.decode("utf-8", errors="replace").split("\n", 1)[0]
            print(f"[bgmus] cmd: {line!r}")
            reply = handle_command(line, controller, entries)
            conn.sendall(reply.encode("utf-8"))
        except Exception as e:
            print(f"[bgmus] socket error: {e}", file=sys.stderr)
        finally:
            try:
                conn.close()
            except Exception:
                pass

# --- Event wait (select on inotify fd + socket fd) ---
def wait_for_event(inotify, server, timeout_secs):
    """Sleep up to timeout_secs, waking early on inotify or socket activity.
    Returns (config_changed, command_available)."""
    if timeout_secs <= 0:
        return False, False
    fds = [inotify.fd, server.fileno()]
    try:
        ready, _, _ = select.select(fds, [], [], timeout_secs)
    except InterruptedError:
        return False, False
    config_changed = inotify.fd in ready
    command_available = server.fileno() in ready
    return config_changed, command_available

def reload_config(config_path, entries, playlists, current_entry, current_next):
    try:
        entries       = load_config(config_path)
        playlists     = {}
        current_entry = None
        current_next  = None
        print(f"[bgmus] Reloaded {len(entries)} schedule entries")
    except SystemExit:
        print("[bgmus] Config reload failed, keeping old schedule", file=sys.stderr)
    return entries, playlists, current_entry, current_next

def run(config_path, socket_path):
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

    config_dir    = Path(config_path).parent.resolve()
    entries       = load_config(config_path)
    playlists     = {}
    player        = Player()
    current_entry = None
    current_next  = None
    controller    = Controller()
    inotify       = make_watcher(config_path)
    server        = make_socket_server(socket_path)

    init_audio()
    print(f"[bgmus] Loaded {len(entries)} schedule entries from {config_path}")

    try:
        while True:
            now = datetime.now()

            # Drain any pending inotify events (from previous iteration's select)
            if drain_inotify(inotify, config_path):
                print("[bgmus] Config changed, reloading...")
                entries, playlists, current_entry, current_next = reload_config(
                    config_path, entries, playlists, current_entry, current_next)

            # --- Oneshot handling ---
            if controller.oneshot and not controller.oneshot_playing:
                path = controller.oneshot
                controller.oneshot = None
                if player.is_playing():
                    print("[bgmus] Fading out for one-shot...")
                    player.fade_out()
                player.play(path, loop=False)
                controller.oneshot_playing = True
                current_entry = None  # force re-evaluation when oneshot finishes
                current_next  = None

            if controller.oneshot_playing and not player.is_playing():
                print("[bgmus] One-shot finished, returning to schedule")
                controller.oneshot_playing = False
                # fall through to schedule logic

            # --- Pause handling ---
            if controller.is_paused(now) and not controller.oneshot_playing:
                if player.is_playing():
                    print("[bgmus] Pausing — fading out...")
                    player.fade_out()
                    current_entry = None
                    current_next  = None
                # sleep until pause ends or a command arrives
                remaining = controller.pause_remaining(now)
                cfg_changed, cmd_ready = wait_for_event(inotify, server, remaining)
                if cmd_ready:
                    accept_and_handle(server, controller, entries)
                continue

            # --- Normal schedule logic (skip if oneshot is playing) ---
            if not controller.oneshot_playing:
                entry = active_entry(entries, now)
                if entry != current_entry:
                    print(f"[bgmus] Slot change at {now.strftime('%H:%M:%S')}: {current_entry} -> {entry}")
                    if player.is_playing():
                        print("[bgmus] Fading out...")
                        player.fade_out()
                    current_entry = entry
                    current_next  = None
                    if entry:
                        current_next = resolve_source(entry.source, config_dir, playlists)
                        nxt, loop = current_next()
                        if nxt:
                            player.play(nxt, loop=loop)
                        else:
                            print(f"[bgmus] Warning: no files resolved for {entry.source} — not playing")
                    else:
                        print("[bgmus] No active slot — silence.")
                elif entry and not player.is_playing():
                    if current_next:
                        nxt, loop = current_next()
                        if nxt:
                            player.play(nxt, loop=loop)
                        else:
                            print(f"[bgmus] No next track resolved for {entry.source} — staying silent")
                    else:
                        print(f"[bgmus] No resolver for {entry.source} — not playing")

            # --- Decide how long to sleep ---
            next_wake = next_event_time(entries, datetime.now())
            if controller.pause_until and (not next_wake or controller.pause_until < next_wake):
                next_wake = controller.pause_until

            if next_wake:
                sleep_secs = (next_wake - datetime.now()).total_seconds()
                if (player.is_playing() and not player.looping) or controller.oneshot_playing:
                    sleep_secs = min(sleep_secs, TICK)
                next_entry = active_entry(entries, next_wake + timedelta(seconds=1))
                next_str = f" (next: {next_entry.source})" if next_entry else " (next: silence)"
                print(f"[bgmus] Sleeping until {next_wake.strftime('%H:%M:%S')} ({fmt_duration(sleep_secs)}){next_str}")
                cfg_changed, cmd_ready = wait_for_event(inotify, server, max(0, sleep_secs))
                if cmd_ready:
                    accept_and_handle(server, controller, entries)
                if cfg_changed:
                    print("[bgmus] Config changed during sleep, reloading...")
                    entries, playlists, current_entry, current_next = reload_config(
                        config_path, entries, playlists, current_entry, current_next)
            else:
                cfg_changed, cmd_ready = wait_for_event(inotify, server, 60)
                if cmd_ready:
                    accept_and_handle(server, controller, entries)

    except KeyboardInterrupt:
        print("\n[bgmus] Stopping...")
        player.fade_out()
        mixer.quit()
        try:
            server.close()
            os.unlink(socket_path)
        except Exception:
            pass
        sys.exit(0)

# --- Play mode (unchanged behaviour) ---
def run_play(config_path, file_path=None):
    init_audio()
    config_dir = Path(config_path).parent.resolve()
    if file_path:
        path = file_path
        loop = False
        print(f"[bgmus] --play: forcing playback of {path}")
    else:
        entries = load_config(config_path)
        entry = active_entry(entries, datetime.now())
        if not entry:
            print("[bgmus] --play: no active slot right now. Check your schedule with --check.", file=sys.stderr)
            mixer.quit()
            sys.exit(1)
        playlists = {}
        nxt, loop = resolve_source(entry.source, config_dir, playlists)()
        if not nxt:
            print(f"[bgmus] --play: could not resolve a file for slot {entry}", file=sys.stderr)
            mixer.quit()
            sys.exit(1)
        path = nxt
        print(f"[bgmus] --play: active slot is {entry}")
    player = Player()
    player.play(path, loop=loop)
    if not player.is_playing():
        print("[bgmus] --play: playback failed to start (see error above).", file=sys.stderr)
        mixer.quit()
        sys.exit(1)
    print("[bgmus] Press Ctrl+C to stop.")
    try:
        while player.is_playing():
            time.sleep(1)
        print("[bgmus] Playback finished.")
    except KeyboardInterrupt:
        print("\n[bgmus] Stopping...")
        player.fade_out()
    mixer.quit()
    sys.exit(0)

# --- Command client (--cmd) ---
def send_command(socket_path, cmd):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(socket_path)
        s.sendall((cmd + "\n").encode("utf-8"))
        reply = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            reply += chunk
        sys.stdout.write(reply.decode("utf-8", errors="replace"))
        s.close()
        # exit non-zero if the daemon replied with "error:"
        if reply.startswith(b"error"):
            sys.exit(1)
        sys.exit(0)
    except FileNotFoundError:
        print(f"[bgmus] No daemon socket at {socket_path} — is bgmus running?", file=sys.stderr)
        sys.exit(2)
    except ConnectionRefusedError:
        print(f"[bgmus] Connection refused on {socket_path} — stale socket?", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"[bgmus] Failed to send command: {e}", file=sys.stderr)
        sys.exit(2)

# --- CLI ---
def main():
    parser = argparse.ArgumentParser(
        description="Background music scheduler",
        epilog=CONFIG_FORMAT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", nargs="?", help="Path to schedule config file (not needed for --cmd)")
    parser.add_argument("--check", action="store_true",
                        help="Parse config and print schedule, then exit")
    parser.add_argument("--play", metavar="FILE", nargs="?", const=True,
                        help="Play a file immediately and exit (omit FILE to play current active slot)")
    parser.add_argument("--cmd", metavar="COMMAND",
                        help="Send a command to a running daemon, e.g. 'pause 1h', 'play /path.wav', 'status'")
    parser.add_argument("--socket", metavar="PATH", default=None,
                        help=f"Override socket path (default: {default_socket_path()})")
    args = parser.parse_args()

    socket_path = args.socket or default_socket_path()

    # --cmd: send-and-exit, doesn't need a config
    if args.cmd is not None:
        send_command(socket_path, args.cmd)
        return

    # All other modes need a config
    if not args.config:
        parser.error("config is required (unless using --cmd)")
    if not os.path.exists(args.config):
        print(f"[bgmus] Config not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    entries = load_config(args.config)

    if args.check:
        print(f"Loaded {len(entries)} entries:")
        for e in entries:
            print(f"  {e}")
        sys.exit(0)

    if args.play is not None:
        file_path = None if args.play is True else args.play
        run_play(args.config, file_path)
        return

    run(args.config, socket_path)

if __name__ == "__main__":
    main()