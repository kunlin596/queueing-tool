#!/usr/bin/python3
"""qtop — interactive qstat TUI with per-job log streaming.

The LIST view is the qstat table, auto-refreshed. Navigate with arrows /
j / k, select with Enter (or mouse click) to open the job's log in an
external viewer chain:

* ``tspin`` (if installed) — zero-config log highlighting on top of less;
* ``less -R`` — ``+F`` follows a running job live (Ctrl+C pauses into
  scroll/search mode, ``F`` re-follows), ``+G`` opens finished logs at the
  end; ``q`` returns to the list;
* builtin tail view — only when neither tool is installed (arrows /
  PgUp / PgDn scroll, End / G re-follow, Esc / q back).

Log discovery: every job records its absolute log path in the persistent
registry ``~/.local/state/queueing-tool/job_logs/<id>`` at submit time (see
``queueing_tool.job.register_log_path``), so qtop resolves logs from
anywhere. For jobs submitted before the registry existed it falls back to
searching ``q.log`` in the cwd and its parents. Press ``f`` in the list to
also show finished jobs (registry + local ``q.log``, newest first,
status ``f``).
"""

import argparse
import curses
import glob
import os
import re
import shutil
import signal
import socket
import subprocess
from collections import deque

from queueing_tool import job as job_mod

REFRESH_S = 2.0  # job-list poll period
INPUT_TICK_MS = 200  # curses getch timeout (UI latency + log poll period)
SCROLLBACK = 5000  # lines kept per log
FINISHED_SHOWN = 30  # max finished logs listed with 'f'

# qstat quiet row, as produced by Server.Job.to_string:
# |0000034 v281_wm_prelim10  03-07-2026 15:20:08       r          kun  priority|
_ROW_RE = re.compile(
    r"^\|(?P<id>\d{7})\s+(?P<name>.+?)\s{2}(?P<time>\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2})"
    r"\s+(?P<status>[rwh])\s+(?P<user>\S+)\s+(?P<priority>\S+)\|$"
)


def fetch_jobs(server_address):
    """Query the server; return (rows, error). Each row is a dict."""
    rows = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(server_address)
        sock.sendall(b"qstat:quiet")
        reply = sock.recv(1024).decode()
        while reply != "":
            match = _ROW_RE.match(reply.strip())
            if match:
                rows.append(match.groupdict())
            sock.sendall(b"ack")
            reply = sock.recv(1024).decode()
        sock.close()
    except OSError:
        return rows, "no answer from server"
    return rows, None


def log_dirs():
    """Candidate q.log dirs: cwd upward to $HOME (or /), nearest first.

    Jobs write q.log relative to their submit directory; walking up lets
    qtop run from anywhere inside the project tree.
    """
    dirs = []
    cur = os.getcwd()
    stop = os.path.expanduser("~")
    while True:
        cand = os.path.join(cur, "q.log")
        if os.path.isdir(cand):
            dirs.append(cand)
        parent = os.path.dirname(cur)
        if cur in (stop, "/") or parent == cur:
            break
        cur = parent
    return dirs


def registry_entry(job_id):
    """The job's tracked log path as registered — file may not exist yet."""
    reg = os.path.join(job_mod.LOG_REGISTRY_DIR, f"{int(job_id):07d}")
    try:
        with open(reg) as f:
            return f.read().strip() or None
    except OSError:
        return None


def registry_log(job_id):
    """Look up the job's tracked log path; None if unregistered/missing."""
    path = registry_entry(job_id)
    return path if path is not None and os.path.isfile(path) else None


def registry_paths():
    """All tracked log paths that still exist on disk."""
    entries = glob.glob(os.path.join(job_mod.LOG_REGISTRY_DIR, "[0-9]*"))
    paths = []
    for entry in entries:
        try:
            with open(entry) as f:
                path = f.read().strip()
        except OSError:
            continue
        if os.path.isfile(path):
            paths.append(path)
    return paths


def finished_logs(limit=FINISHED_SHOWN):
    """Recent finished logs (registry + local q.log) as pseudo-rows."""
    paths = set(registry_paths())
    for d in log_dirs():
        paths.update(glob.glob(os.path.join(d, "*.[0-9]*")))
    paths = sorted(paths, key=lambda p: os.path.getmtime(p), reverse=True)
    rows = []
    for path in paths[:limit]:
        base = os.path.basename(path)
        name, _, job_id = base.rpartition(".")
        if not job_id.isdigit():
            continue
        rows.append(
            {
                "id": job_id,
                "name": name,
                "time": "",
                "status": "f",
                "user": "",
                "priority": "-",
                "log_path": path,
            }
        )
    return rows


def find_log(job_id):
    """Resolve a job's log: tracked registry first, cwd-walk fallback.

    The fallback (unique id suffix ``q.log/*.<id>`` in the cwd and its
    parents) only serves jobs submitted before the registry existed.
    """
    tracked = registry_log(job_id)
    if tracked is not None:
        return tracked
    for d in log_dirs():
        matches = glob.glob(os.path.join(d, f"*.{int(job_id):07d}"))
        if matches:
            return matches[0]
    return None


class LogTail:
    """Incremental reader keeping a scrollback of \\r-normalized lines."""

    def __init__(self, path):
        self.path = path
        self.lines = deque(maxlen=SCROLLBACK)
        self._fh = open(path, errors="replace")
        self._partial = ""

    def poll(self):
        chunk = self._fh.read()
        if not chunk:
            return
        text = self._partial + chunk
        parts = text.split("\n")
        self._partial = parts.pop()
        for line in parts:
            # tqdm-style progress: keep only the segment after the last \r.
            self.lines.append(line.rsplit("\r", 1)[-1])

    def visible_tail(self):
        # The unfinished line (e.g. a live progress bar) is shown too.
        tail = self._partial.rsplit("\r", 1)[-1]
        return list(self.lines) + ([tail] if tail else [])

    def close(self):
        self._fh.close()


def _addstr(scr, y, x, text, attr=0):
    """addstr that never throws on the bottom-right cell / small windows."""
    height, width = scr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    try:
        scr.addstr(y, x, text[: max(0, width - x - 1)], attr)
    except curses.error:
        pass


class QTop:
    LIST, LOG = 0, 1

    def __init__(self, scr, server_address):
        self.scr = scr
        self.server_address = server_address
        self.mode = self.LIST
        self.jobs = []
        self.error = None
        self.show_finished = False
        self.selected = 0
        self.last_fetch = 0.0
        self.tail = None
        self.tail_job = None
        self.scroll = None  # None = follow; int = offset from the end
        self._name_cache = {}  # job id -> full name (from the tracked log)

    def full_name(self, row):
        """The untruncated job name (the qstat table caps it at 16 chars).

        The tracked log path carries the full name — resolve once per id.
        """
        cached = self._name_cache.get(row["id"])
        if cached is not None:
            return cached
        # registry_entry (not registry_log): a waiting job's log does not
        # exist yet, but its registered path already carries the full name.
        path = row.get("log_path") or registry_entry(row["id"]) or find_log(row["id"])
        if path:
            name = os.path.basename(path).rpartition(".")[0]
            if name:
                self._name_cache[row["id"]] = name
                return name
        return row["name"]

    # ------------------------------ data ------------------------------ #
    def refresh_jobs(self, now):
        if now - self.last_fetch < REFRESH_S:
            return
        self.last_fetch = now
        rows, self.error = fetch_jobs(self.server_address)
        if self.show_finished:
            live_ids = {r["id"] for r in rows}
            rows += [r for r in finished_logs() if r["id"] not in live_ids]
        self.jobs = rows
        if self.jobs:
            self.selected = min(self.selected, len(self.jobs) - 1)

    def open_log(self, row):
        path = row.get("log_path") or find_log(row["id"])
        if self.tail is not None:
            self.tail.close()
            self.tail = None
        self.tail_job = row
        self.scroll = None
        if path is None or not os.path.isfile(path):
            self.error = (
                f"no tracked log for job {row['id']} and no q.log/*."
                f"{int(row['id']):07d} under {os.getcwd()} or its parents"
            )
            return
        # Chain into an external viewer (tspin -> less -> builtin): tspin
        # adds zero-config log highlighting on top of less; less -R keeps
        # ANSI colors, +F follows a running job live (Ctrl+C pauses into
        # scroll/search mode, F re-follows, q returns to the list), +G
        # opens a finished log at its end.
        if self._page_external(path, running=row["status"] == "r"):
            return
        # fallback viewer when neither tspin nor less is installed
        self.tail = LogTail(path)
        self.tail.poll()
        self.mode = self.LOG

    @staticmethod
    def _viewer_cmd(path, running):
        if shutil.which("tspin") is not None:
            if running:
                return ["tspin", "--follow", path]
            return ["tspin", "--pager", "less -R +G", path]
        if shutil.which("less") is not None:
            return ["less", "-R", "+F" if running else "+G", "--", path]
        return None

    def _page_external(self, path, running):
        cmd = self._viewer_cmd(path, running)
        if cmd is None:
            return False
        curses.def_prog_mode()
        curses.endwin()
        # Ctrl+C is a normal keystroke for less +F (pause following) but is
        # delivered to the whole foreground process group — ignore it in
        # qtop while the pager owns the terminal, or qtop dies with it.
        old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            subprocess.call(cmd)
        finally:
            signal.signal(signal.SIGINT, old_sigint)
            curses.reset_prog_mode()
            self._init_screen()
            self.scr.clear()
            self.scr.refresh()
        return True

    def _init_screen(self):
        curses.curs_set(0)
        curses.mousemask(
            curses.BUTTON1_CLICKED
            | curses.BUTTON1_DOUBLE_CLICKED
            | curses.BUTTON1_PRESSED
            | curses.BUTTON1_RELEASED
        )
        self.scr.timeout(INPUT_TICK_MS)

    def close_log(self):
        if self.tail is not None:
            self.tail.close()
        self.tail = None
        self.mode = self.LIST

    # ------------------------------ views ----------------------------- #
    def draw_list(self):
        scr = self.scr
        width = scr.getmaxyx()[1]
        scr.erase()
        _addstr(scr, 0, 0, "qtop — local queue", curses.A_BOLD)
        _addstr(
            scr,
            1,
            0,
            "[Enter/click] view log (tspin/less, q returns)   [f] finished logs "
            f"{'ON ' if self.show_finished else 'off'}   [q] quit",
            curses.A_DIM,
        )
        # Flexible layout: id/submitted/st/user are fixed-width, the name
        # column absorbs whatever the window has left (min 10 cols).
        user_w = 11
        name_w = max(10, width - (1 + 7 + 2 + 2 + 19 + 2 + 2 + 2 + user_w) - 1)
        header = (
            f" {'id':<7}  {'name':<{name_w}}  {'submitted':<19}  "
            f"{'st':<2}  {'user':<{user_w}}"
        )
        _addstr(scr, 3, 0, header.ljust(width - 1), curses.A_UNDERLINE)
        if not self.jobs:
            _addstr(scr, 5, 1, self.error or "queue is empty", curses.A_DIM)
        for i, row in enumerate(self.jobs):
            line = (
                f" {row['id']}  {self.full_name(row)[:name_w]:<{name_w}}  "
                f"{row['time']:<19}  {row['status']:<2}  {row['user'][:user_w]:<{user_w}}"
            )
            attr = curses.A_REVERSE if i == self.selected else 0
            if row["status"] == "r":
                attr |= curses.A_BOLD
            # pad to the full window width so the selection highlight spans
            # the entire row, not just the text
            _addstr(scr, 4 + i, 0, line.ljust(width - 1), attr)
        if self.error and self.jobs:
            _addstr(
                scr,
                self.scr.getmaxyx()[0] - 1,
                0,
                self.error,
                curses.A_BOLD | curses.A_REVERSE,
            )
        scr.refresh()

    def draw_log(self):
        assert self.tail is not None and self.tail_job is not None
        scr = self.scr
        height = scr.getmaxyx()[0]
        scr.erase()
        row = self.tail_job
        follow = "following" if self.scroll is None else f"scrollback {self.scroll}"
        _addstr(
            scr,
            0,
            0,
            f"{row['name']}.{row['id']}  [{row['status']}]  {self.tail.path}"
            f"  ({follow})",
            curses.A_BOLD,
        )
        _addstr(
            scr,
            1,
            0,
            "[Esc/q] back   [arrows/PgUp/PgDn] scroll   [End/G] follow",
            curses.A_DIM,
        )
        lines = self.tail.visible_tail()
        body = height - 3
        end = len(lines) if self.scroll is None else max(body, len(lines) - self.scroll)
        for i, line in enumerate(lines[max(0, end - body) : end]):
            _addstr(scr, 3 + i, 0, line)
        scr.refresh()

    # ------------------------------ input ----------------------------- #
    def handle_key_list(self, key):
        if key in (ord("q"), 27):
            return False
        if key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(self.selected + 1, max(0, len(self.jobs) - 1))
        elif key in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
        elif key == ord("f"):
            self.show_finished = not self.show_finished
            self.last_fetch = 0.0
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            if self.jobs:
                self.open_log(self.jobs[self.selected])
        elif key == curses.KEY_MOUSE:
            try:
                _, _, my, _, bstate = curses.getmouse()
            except curses.error:
                return True
            idx = my - 4
            if 0 <= idx < len(self.jobs):
                self.selected = idx
                # Terminals/tmux differ in what they deliver for a click:
                # some report CLICKED, others only PRESSED/RELEASED.
                click = (
                    curses.BUTTON1_CLICKED
                    | curses.BUTTON1_DOUBLE_CLICKED
                    | curses.BUTTON1_PRESSED
                    | curses.BUTTON1_RELEASED
                )
                if bstate & click:
                    self.open_log(self.jobs[idx])
        return True

    def handle_key_log(self, key):
        assert self.tail is not None
        body = self.scr.getmaxyx()[0] - 3
        n_lines = len(self.tail.visible_tail())
        max_scroll = max(0, n_lines - body)

        def scrolled(delta):
            base = 0 if self.scroll is None else self.scroll
            return min(max_scroll, max(0, base + delta)) or None

        if key in (ord("q"), 27):
            self.close_log()
        elif key in (curses.KEY_UP, ord("k")):
            self.scroll = scrolled(+1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.scroll = scrolled(-1)
        elif key == curses.KEY_PPAGE:
            self.scroll = scrolled(+body)
        elif key == curses.KEY_NPAGE:
            self.scroll = scrolled(-body)
        elif key in (curses.KEY_END, ord("G")):
            self.scroll = None
        return True

    # ------------------------------ loop ------------------------------ #
    def run(self):
        self._init_screen()
        import time

        while True:
            now = time.time()
            if self.mode == self.LIST:
                self.refresh_jobs(now)
                self.draw_list()
            else:
                assert self.tail is not None
                self.tail.poll()
                self.draw_log()
            key = self.scr.getch()
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                # terminal resized: refresh curses' notion of the size and
                # force a full repaint; draws re-read getmaxyx() every tick
                curses.update_lines_cols()
                self.scr.clear()
                continue
            alive = (
                self.handle_key_list(key)
                if self.mode == self.LIST
                else self.handle_key_log(key)
            )
            if not alive:
                break
        if self.tail is not None:
            self.tail.close()


def main():
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument(
        "--server_ip", type=str, default="localhost", help="ip address of the server"
    )
    arg_parser.add_argument(
        "--server_port", type=int, default=1234, help="port of the server"
    )
    arg_parser.add_argument(
        "--dump",
        action="store_true",
        help="print the parsed job rows and exit (no TUI; for scripting/tests)",
    )
    args = arg_parser.parse_args()
    server_address = (args.server_ip, args.server_port)

    if args.dump:
        rows, error = fetch_jobs(server_address)
        for row in rows:
            log = find_log(row["id"])
            print(f"{row['id']} {row['status']} {row['name']!r} log={log}")
        if error:
            print(f"error: {error}")
        return

    try:
        curses.wrapper(lambda scr: QTop(scr, server_address).run())
    except KeyboardInterrupt:
        pass  # Ctrl+C in the list view quits cleanly, same as q


if __name__ == "__main__":
    main()
