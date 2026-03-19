#!/usr/bin/env python3
"""
bgmus - Background music scheduler daemon.
Plays music according to a time-based schedule, with fade between slots.

Usage:
    bgmus <config>                  Run the scheduler daemon
    bgmus --check <config>          Parse and print the schedule, then exit
    bgmus --play <config>           Play the current active slot immediately
    bgmus --play FILE <config>      Play a specific file immediately

Config format:
    9-10 file.wav
    10-11 random:file2.wav,file3.wav
    11-13 playlist.txt          # work through sequentially
    13-14 random:playlist.txt
    Mon 18:30-18:40 monday.wav
    *:10-*:15 file.wav          # 10-15 minutes past every hour
"""
import argparse
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import inotify_simple
import pygame.mixer as mixer

FADE_STEPS    = 20    # number of volume steps when fading out
FADE_DURATION = 3.0   # seconds to fade out over
TICK          = 5     # seconds between track-end checks while playing a playlist

CONFIG_FORMAT = """
config format:
  9-10 file.wav                    # play daily 9–10am
  10-11 random:a.wav,b.wav         # pick one at random
  11-13 playlist.txt               # work through sequentially
  13-14 random:playlist.txt        # random from playlist
  Mon 18:30-18:40 monday.wav       # specific day + HH:MM times
  *:10-*:15 file.wav               # 10–15 minutes past every hour
  time ranges: H-H or HH:MM-HH:MM or *:MM-*:MM
  day prefix:  Mon Tue Wed Thu Fri Sat Sun
  random:      comma-separated files or a .txt playlist
  playlist:    one file per line, # comments ignored
"""

# ── Audio init ────────────────────────────────────────────────────────────────
def init_audio():
    mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=2048)
    mixer.init()
    print(f"[bgmus] Audio: {mixer.get_init()}")

# ── Formatting ────────────────────────────────────────────────────────────────
def fmt_duration(secs):
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"

# ── Config parsing ────────────────────────────────────────────────────────────
class ScheduleEntry:
    def __init__(self, days, start, end, source):
        self.days   = days    # None = every day, or set of 0..6 (Mon=0)
        self.start  = start   # (hour_or_None, minute)
        self.end    = end     # (hour_or_None, minute)
        self.source = source
    def __repr__(self):
        days = f"day={self.days}" if self.days else "daily"
        sh = "*" if self.start[0] is None else f"{self.start[0]:02d}"
        eh = "*" if self.end[0]   is None else f"{self.end[0]:02d}"
        return (f"<{days} {sh}:{self.start[1]:02d}-{eh}:{self.end[1]:02d} {self.source}>")

DAY_MAP = {"mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6}

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
    if parts[0].lower() in DAY_MAP:
        days = {DAY_MAP[parts[0].lower()]}
        parts = parts[1:]
    elif re.match(r'^[A-Za-z]{3}$', parts[0]):
        raise ValueError(f"Line {lineno}: unknown day {parts[0]!r}")
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

# ── Source resolution ─────────────────────────────────────────────────────────
def load_playlist(path):
    files = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                files.append(line)
    return files

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

def resolve_source(source, config_dir, playlists):
    if source.startswith("random:"):
        spec = source[len("random:"):]
        if spec.endswith(".txt"):
            files = load_playlist(config_dir / spec)
        else:
            files = [f.strip() for f in spec.split(",")]
        return lambda: (str(config_dir / random.choice(files)), False) if files else (None, False)
    elif source.endswith(".txt"):
        key = str(config_dir / source)
        if key not in playlists:
            playlists[key] = SequentialPlaylist(load_playlist(config_dir / source))
        return playlists[key].next
    else:
        path = str(config_dir / source)
        SUPPORTED = {".mp3", ".wav", ".ogg", ".flac"}
        ext = Path(path).suffix.lower()
        if ext not in SUPPORTED:
            print(f"[bgmus] Warning: unsupported format {ext!r}, skipping {path}", file=sys.stderr)
            return lambda: (None, False)
        return lambda: (path, True)

# ── Player ────────────────────────────────────────────────────────────────────
class Player:
    def __init__(self):
        self._channel = None
        self._sound   = None
        self._vol     = 1.0
        self.looping  = False
    def play(self, path, loop=False):
        self.stop()
        try:
            self._sound   = mixer.Sound(path)
            self._vol     = 1.0
            self.looping  = loop
            self._channel = self._sound.play(loops=-1 if loop else 0)
            print(f"[bgmus] Playing: {path}")
        except Exception as e:
            print(f"[bgmus] Error playing {path}: {e}", file=sys.stderr)
    def set_volume(self, vol):
        self._vol = max(0.0, min(1.0, vol))
        if self._channel:
            self._channel.set_volume(self._vol)
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
        if self._channel:
            self._channel.stop()
            self._channel = None
        self._sound  = None
        self.looping = False
    def is_playing(self):
        return self._channel is not None and self._channel.get_busy()

# ── Scheduler ─────────────────────────────────────────────────────────────────
def in_range(start, end, hour, minute):
    if start[0] is None:
        return start[1] <= minute < end[1]
    return start <= (hour, minute) < end

def next_event_time(entries, now):
    """Return the datetime of the next schedule boundary."""
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

def config_changed(inotify, config_path):
    events = inotify.read(timeout=0)
    name   = Path(config_path).name
    for e in events:
        print(f"[bgmus] inotify event: {e.name!r} flags={inotify_simple.flags.from_mask(e.mask)}")
    return any(e.name == name for e in events)

def run(config_path):
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    config_dir    = Path(config_path).parent.resolve()
    entries       = load_config(config_path)
    playlists     = {}
    player        = Player()
    current_entry = None
    current_next  = None
    inotify       = make_watcher(config_path)
    init_audio()
    print(f"[bgmus] Loaded {len(entries)} schedule entries from {config_path}")
    try:
        while True:
            if config_changed(inotify, config_path):
                print("[bgmus] Config changed, reloading...")
                try:
                    entries       = load_config(config_path)
                    playlists     = {}
                    current_entry = None
                    current_next  = None
                    print(f"[bgmus] Reloaded {len(entries)} schedule entries")
                except SystemExit:
                    print("[bgmus] Config reload failed, keeping old schedule", file=sys.stderr)
            now   = datetime.now()
            entry = active_entry(entries, now)
            if entry != current_entry:
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
                        print(f"[bgmus] Warning: no files for {entry.source}")
                else:
                    print("[bgmus] No active slot — silence.")
            elif entry and not player.is_playing():
                if current_next:
                    nxt, loop = current_next()
                    if nxt:
                        player.play(nxt, loop=loop)
            next_wake = next_event_time(entries, datetime.now())
            if next_wake:
                sleep_secs = (next_wake - datetime.now()).total_seconds()
                if player.is_playing() and not player.looping:
                    sleep_secs = min(sleep_secs, TICK)
                print(f"[bgmus] Sleeping until {next_wake.strftime('%H:%M:%S')} ({fmt_duration(sleep_secs)})")
                time.sleep(max(0, sleep_secs))
            else:
                time.sleep(60)
    except KeyboardInterrupt:
        print("\n[bgmus] Stopping...")
        player.fade_out()
        mixer.quit()
        sys.exit(0)

# ── Play mode ─────────────────────────────────────────────────────────────────
def run_play(config_path, file_path=None):
    """Play a specific file or the current active slot, then exit."""
    init_audio()
    config_dir = Path(config_path).parent.resolve()

    if file_path:
        # Play a specific file directly
        path = file_path
        loop = False
        print(f"[bgmus] --play: forcing playback of {path}")
    else:
        # Play whatever the current schedule says
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

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Background music scheduler",
        epilog=CONFIG_FORMAT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("config", help="Path to schedule config file")
    parser.add_argument("--check", action="store_true",
                        help="Parse config and print schedule, then exit")
    parser.add_argument("--play", metavar="FILE", nargs="?", const=True,
                        help="Play a file immediately and exit (omit FILE to play current active slot)")
    args = parser.parse_args()

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
        return  # run_play calls sys.exit, but just in case

    run(args.config)

if __name__ == "__main__":
    main()