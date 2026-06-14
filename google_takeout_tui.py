#!/usr/bin/env python3
"""
Google Takeout Bulk Downloader - TUI

The only interface. Captures come from the browser extension's
"Copy as JSON" button (paste the JSON below) or a pasted cURL command.

When the Google session cookie expires mid-download, the TUI rings the
terminal bell and flashes its title bar to tell you it needs a fresh
capture. Re-capture in the browser, click the extension's "Copy as JSON",
paste it in, and click Resume.

Usage:
    python google_takeout_tui.py
    # Or via the main script:
    python takeout.py
"""

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
import time
from typing import Optional, Dict
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Header, Footer, Static, Button, Input, Label,
    Log, DataTable, TextArea, ListView, ListItem, ProgressBar
)
from textual.binding import Binding
from textual import work

import requests

from takeout import (
    TakeoutDownloader, DownloadStats,
    validate_output_dir, compute_backoff, _retry_after_seconds,
    load_settings, save_settings,
    VERSION, CHUNK_SIZE, DEFAULT_FILE_COUNT, DEFAULT_OUTPUT_DIR, DEFAULT_PARALLEL,
    MAX_PARALLEL, MAX_FILE_COUNT, MAX_RETRIES
)
from takeout_payload import parse_payload, TakeoutPayload

try:
    from aria2c_integration import detect_aria2c
    ARIA2C_AVAILABLE = detect_aria2c()
except ImportError:
    ARIA2C_AVAILABLE = False


@dataclass
@dataclass
class ActiveDownload:
    """Track an active download."""
    filename: str
    downloaded: int = 0
    total: int = 0
    status: str = "Starting"
    # Speed/ETA tracking — updated every progress tick.
    last_speed_mbps: float = 0.0
    last_tick_bytes: int = 0
    last_tick_time: float = 0.0


class DirectoryPicker(ModalScreen):
    """Modal directory browser. Navigate the filesystem, or type/paste a path.

    Dismisses with the chosen absolute path (str) on "Use this folder", or
    None on Cancel. Symlinks are followed (resolve), so a path like
    ./downloads/opt -> /opt lands on the real target.

    Why not Textual's DirectoryTree? It stats every entry to render and
    recurses lazily on the UI thread; on a slow/large JuiceFS/encfs FUSE
    mount that locks the whole app (the "froze, had to docker kill" hang).
    This picker instead lists ONE level at a time with os.scandir, off the
    UI thread, with a bounded entry cap, so it can't freeze.
    """

    # Cap entries listed per directory — a 100k-entry dir would otherwise
    # take forever to render. We show the cap was hit in the header.
    MAX_ENTRIES = 1000

    CSS = """
    DirectoryPicker {
        align: center middle;
    }
    #picker-card {
        width: 90%;
        height: 90%;
        max-width: 120;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #picker-title { text-style: bold; color: $primary; height: 1; }
    #picker-cwd { color: $text-muted; height: 1; margin-bottom: 1; }
    #picker-path-input { margin-bottom: 1; }
    #picker-list { height: 1fr; border: round $secondary; }
    #picker-buttons { height: 3; align: center middle; margin-top: 1; }
    #picker-buttons Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("backspace", "go_up", "Up"),
    ]

    def __init__(self, start_dir: str = ".") -> None:
        super().__init__()
        try:
            p = Path(start_dir).expanduser().resolve()
            if not p.is_dir():
                p = p.parent if p.parent.is_dir() else Path.cwd()
        except (OSError, ValueError):
            p = Path.cwd()
        self._cwd = p
        # Maps a ListItem's index to the child Path it represents.
        self._entries: list[Path] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-card"):
            yield Label("Select output directory", id="picker-title")
            yield Label(str(self._cwd), id="picker-cwd")
            yield Input(value=str(self._cwd), placeholder="Type or paste a path, then Enter", id="picker-path-input")
            yield ListView(id="picker-list")
            with Horizontal(id="picker-buttons"):
                yield Button("\u2191 Up", id="picker-up", variant="default")
                yield Button("\u2705 Use this folder", id="picker-use", variant="success")
                yield Button("\u2716 Cancel", id="picker-cancel", variant="error")

    def on_mount(self) -> None:
        self._set_cwd(self._cwd)

    def _set_cwd(self, path: Path) -> None:
        """Navigate to a directory.

        Optimistic + threaded: the header/input update instantly, the list is
        greyed, and the slow filesystem work (resolve / is_dir / scandir) runs
        off the UI thread so the picker never freezes on JuiceFS / encfs /
        network mounts.
        """
        target = str(path)
        self.query_one("#picker-cwd", Label).update(f"\u23f3 {target}")
        self.query_one("#picker-path-input", Input).value = target
        self.query_one("#picker-list", ListView).loading = True
        self._scan_dir(path)

    @work(thread=True, exclusive=True, group="picker-load")
    def _scan_dir(self, path: Path) -> None:
        """Resolve + list a directory off the UI thread, then apply it.

        Only the immediate children are listed (one level), directories
        first, capped at MAX_ENTRIES. No recursion, no per-entry stat beyond
        the is_dir() that scandir already caches.
        """
        try:
            resolved = path.expanduser().resolve()
        except (OSError, ValueError):
            self.app.call_from_thread(self._apply_scan, None, [], False, False)
            return
        dirs: list[Path] = []
        truncated = False
        try:
            with os.scandir(resolved) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=True):
                            dirs.append(Path(entry.path))
                    except OSError:
                        continue  # unreadable entry — skip
                    if len(dirs) >= self.MAX_ENTRIES:
                        truncated = True
                        break
        except (OSError, ValueError):
            self.app.call_from_thread(self._apply_scan, resolved, [], False, False)
            return
        dirs.sort(key=lambda p: p.name.lower())
        self.app.call_from_thread(self._apply_scan, resolved, dirs, True, truncated)

    def _apply_scan(self, resolved: Optional[Path], dirs: list, ok: bool, truncated: bool) -> None:
        """Runs on the UI thread once _scan_dir has produced a listing."""
        lv = self.query_one("#picker-list", ListView)
        cwd_label = self.query_one("#picker-cwd", Label)
        if not ok or resolved is None:
            cwd_label.update(f"\u26a0 Can't open \u2014 staying in {self._cwd}")
            self.query_one("#picker-path-input", Input).value = str(self._cwd)
            lv.loading = False
            return
        self._cwd = resolved
        suffix = f"  ({len(dirs)} dirs{', capped' if truncated else ''})"
        cwd_label.update(str(resolved) + suffix)
        self.query_one("#picker-path-input", Input).value = str(resolved)
        self._entries = dirs
        lv.clear()
        for d in dirs:
            lv.append(ListItem(Label(f"\U0001F4C1 {d.name}")))
        lv.loading = False
        lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter a subdirectory when its row is activated."""
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._entries):
            self._set_cwd(self._entries[idx])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "picker-path-input":
            self._set_cwd(Path(event.value))

    def action_go_up(self) -> None:
        self._set_cwd(self._cwd.parent)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "picker-up":
            self._set_cwd(self._cwd.parent)
        elif event.button.id == "picker-use":
            # Honour a path typed in the box even if not yet navigated to.
            typed = self.query_one("#picker-path-input", Input).value.strip()
            chosen = typed or str(self._cwd)
            self.dismiss(chosen)
        elif event.button.id == "picker-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TakeoutTUI(App):
    """A Textual app for Google Takeout downloads with parallel support."""

    CSS = """
    Screen {
        background: $surface;
    }

    #main-container {
        height: 100%;
        padding: 1;
    }

    #input-section {
        height: auto;
        padding: 1;
        border: round $primary;
        margin-bottom: 1;
    }

    #input-section.needs-refresh {
        border: heavy $error;
    }

    #settings-labels {
        height: 1;
    }

    #outdir-row {
        height: 3;
        margin-bottom: 1;
    }

    #output-input {
        width: 1fr;
        margin-right: 1;
    }

    #browse-btn {
        width: auto;
    }

    .field-label {
        width: 1fr;
        margin-right: 1;
        color: $text-muted;
        text-style: bold;
    }

    #curl-input {
        height: 6;
        margin-bottom: 1;
    }

    #settings-row {
        height: 3;
        margin-bottom: 1;
    }

    #settings-row Input {
        width: 1fr;
        margin-right: 1;
    }

    #button-row {
        height: 3;
        align: center middle;
    }

    #button-row Button {
        margin: 0 1;
    }

    #alert-panel {
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
        color: $error;
        text-style: bold;
    }

    #stats-section {
        height: auto;
        border: round $success;
        margin-bottom: 1;
    }

    #stats-panel {
        height: 1;
        padding: 0 1;
    }

    #downloads-section {
        height: 12;
        border: round $secondary;
        margin-bottom: 1;
    }

    #downloads-table {
        height: 100%;
    }

    #log-section {
        height: 1fr;
        border: round $accent;
    }

    Log {
        height: 100%;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("p", "pause", "Pause"),
        Binding("c", "continue_dl", "Continue"),
        Binding("k", "clear_log", "Clear Log"),
        Binding("b", "browse", "Browse dir"),
    ]

    # How often to re-ring the bell / flash while waiting for a refresh.
    REFRESH_ALERT_INTERVAL = 5.0

    def __init__(self):
        super().__init__()
        self.downloader: Optional[TakeoutDownloader] = None
        self.is_downloading = False
        self.active_downloads: Dict[str, ActiveDownload] = {}
        self.stats = DownloadStats()
        self.bytes_at_last_update = 0
        self.last_update_time = datetime.now()
        self._lock = threading.Lock()

        # Refresh-alert state (cookie expired, waiting for a fresh paste)
        self.needs_refresh = False
        self._refresh_timer = None
        self._title_flash_on = False
        self._base_title = f"Google Takeout Downloader v{VERSION}"
        # Remember the last run parameters so Resume can pick up where it left off
        self._last_file_count = DEFAULT_FILE_COUNT
        self._last_parallel = DEFAULT_PARALLEL
        # Pre-download queue preview: list of (filename, status) where status
        # is "queued", "exists" (already on disk, will skip), or "resume"
        # (.downloading partial present). Populated after set_curl succeeds,
        # before any worker thread starts. The downloads table shows it so
        # the user sees what's coming.
        self.queue_preview: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header()

        with Container(id="main-container"):
            # Input section
            with Vertical(id="input-section") as input_section:
                input_section.border_title = "1 · Payload"
                yield Label(
                    "[bold]Paste payload — JSON from the extension's "
                    "\"Copy as JSON\", or a cURL command.[/]  "
                    "[dim]Paste broken over SSH/tmux? Write it to in.json and type just a dot (.)[/]"
                )
                yield TextArea(id="curl-input")

                # Output directory row: input + Browse button.
                yield Label("Output directory", classes="field-label")
                with Horizontal(id="outdir-row"):
                    yield Input(value=DEFAULT_OUTPUT_DIR, placeholder="Type/paste a path, or click Browse", id="output-input")
                    yield Button("\U0001F4C1 Browse", id="browse-btn", variant="primary")

                # Per-field labels so the meaning stays visible after the
                # placeholder text is replaced by a value.
                with Horizontal(id="settings-labels"):
                    yield Label("Max files (parts)", classes="field-label")
                    yield Label(f"Parallel (1-{MAX_PARALLEL})", classes="field-label")

                with Horizontal(id="settings-row"):
                    yield Input(value=str(DEFAULT_FILE_COUNT), placeholder="Max files", id="count-input")
                    yield Input(value=str(DEFAULT_PARALLEL), placeholder=f"Parallel 1-{MAX_PARALLEL}", id="parallel-input")

                with Horizontal(id="button-row"):
                    yield Button("▶ Start", id="start-btn", variant="success")
                    yield Button("⏸ Pause", id="pause-btn", variant="warning", disabled=True)
                    yield Button("▶ Resume", id="resume-btn", variant="warning", disabled=True)
                    yield Button("⏹ Stop", id="stop-btn", variant="error", disabled=True)
                    yield Button("🗑 Clear", id="clear-btn", variant="default")

            # Alert panel (hidden unless a refresh is needed)
            yield Static("", id="alert-panel")

            # Stats panel
            with Vertical(id="stats-section") as stats_section:
                stats_section.border_title = "2 · Totals"
                yield Static("", id="stats-panel")

            # Active downloads table
            with Vertical(id="downloads-section") as downloads_section:
                downloads_section.border_title = "3 · Active downloads"
                yield DataTable(id="downloads-table")

            # Per-file progress bars (one row per active download)
            with Vertical(id="progress-section") as progress_section:
                progress_section.border_title = "4 · Live progress bars"
                yield Static("(no active downloads)", id="progress-display")

            # Log section
            with Vertical(id="log-section") as log_section:
                log_section.border_title = "5 · Activity log"
                yield Log(highlight=True, max_lines=2000)

        yield Footer()

    def on_mount(self) -> None:
        """Called when app is mounted."""
        aria2c_status = " (aria2c: available)" if ARIA2C_AVAILABLE else " (aria2c: not found)"
        self.title = self._base_title
        self.sub_title = f"TUI Mode - Parallel Downloads{aria2c_status}"

        # Setup downloads table
        table = self.query_one("#downloads-table", DataTable)
        table.add_columns("File", "Progress", "Size", "Status")

        self.log_message(f"Google Takeout Downloader v{VERSION}")
        self.log_message("Paste a JSON payload (extension → Copy as JSON) or a cURL command, then Start")
        self.log_message("Keys: Q=quit  S=start  P=pause  C=continue  X=stop  K=clear log  B=browse dir")
        if ARIA2C_AVAILABLE:
            self.log_message("aria2c detected — available for high-speed downloads")
        else:
            self.log_message("Tip: Install aria2c for multi-connection downloads (apt install aria2)")
        self.log_message("Install the browser extension from helpers/ to capture payloads")
        self.log_message("Paste broken over SSH/tmux? Write JSON to in.json and type a single '.' then Start")

        # Startup diagnostics so the user can see exactly what's resolved and
        # where the TUI is looking for files. Visible in the on-screen log AND
        # mirrored to the log file (TAKEOUT_LOG_FILE, default ./takeout.log)
        # which can be `tail -f`'d from another SSH session.
        log_file = os.environ.get("TAKEOUT_LOG_FILE", "./takeout.log")
        self.log_message(f"Log file: {os.path.abspath(log_file)}")
        self.log_message(f"CWD: {os.getcwd()}")
        self.log_message(f"Default output dir: {DEFAULT_OUTPUT_DIR}")
        self.log_message(f"Payload search roots: output_dir, cwd, /downloads, /downloads/drop, /drop, /work, /work/drop, $HOME")
        self.log_message(f"Payload filenames: {', '.join(self.PAYLOAD_FILENAMES)}")

        # Restore last-used settings (output dir, file count, parallel).
        self._restore_settings()

        self.update_stats_display()

        # Focus the payload box so a right-click / Ctrl+Shift+V paste lands
        # there immediately instead of being swallowed by a focused Button.
        try:
            self.query_one("#curl-input", TextArea).focus()
        except Exception:
            pass

    def on_paste(self, event) -> None:
        """App-level paste router.

        Over SSH/tmux a right-click paste arrives as a bracketed-paste event,
        but Textual only delivers it to the focused widget. If focus is on a
        button or a settings Input, the payload would be lost (or dumped into
        the wrong field). We catch paste app-wide and, unless the user is
        clearly pasting into a small settings field, route it into the
        payload box and focus it.
        """
        text = getattr(event, "text", "") or ""
        if not text:
            return
        focused = self.focused
        focused_id = getattr(focused, "id", None)
        # Let pastes into the small settings fields behave normally.
        if focused_id in ("output-input", "count-input", "parallel-input", "picker-path-input"):
            return
        try:
            box = self.query_one("#curl-input", TextArea)
        except Exception:
            return
        # If the payload box is already focused, let it handle the paste itself.
        if focused is box:
            return
        # Otherwise capture it: replace box content and focus it.
        box.load_text(text)
        box.focus()
        self.log_message(f"Pasted {len(text)} chars into the payload box")
        event.stop()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Auto-expand "." / "@file" in the payload box on every keystroke.

        The user shouldn't have to hit Start just to load a payload file —
        typing the dot alone is enough to materialize the JSON into the box
        (and then they can still review/edit before Start). Anything beyond a
        bare "." or "@<path>" is left alone (mid-typing is preserved).
        """
        try:
            box = self.query_one("#curl-input", TextArea)
        except Exception:
            return
        # Only the payload box drives this; ignore changes from any other
        # TextArea (the picker has its own).
        if event.text_area is not box:
            return
        text = box.text.strip()
        if not text or text == "." or text.startswith("@"):
            output_dir = self.query_one("#output-input", Input).value.strip() or DEFAULT_OUTPUT_DIR
            loaded = self._read_payload_file(text, output_dir) if text else None
            if loaded:
                # Replace the dot/file-ref with the actual payload contents.
                # `load_text` resets the cursor to the top; that's fine — the
                # user usually wants to skim what was loaded.
                box.load_text(loaded)
                preview_chars = min(len(loaded), 80)
                self.log_message(
                    f"Auto-loaded {len(loaded)} chars from file into payload box "
                    f"(preview: {loaded[:preview_chars]!r}{'...' if len(loaded) > 80 else ''})"
                )

    def _restore_settings(self) -> None:
        """Pre-fill the input fields from the persisted settings file.

        The output dir is only restored if it still exists. A stale saved
        path (e.g. an old ./downloads from before a JuiceFS mount existed)
        must NOT shadow the smarter current default — otherwise the TUI keeps
        defaulting to ./downloads even though /srv/storage/... is now
        available.
        """
        s = load_settings()
        if not s:
            return
        out = s.get("output_dir")
        if isinstance(out, str) and out:
            try:
                exists = Path(out).expanduser().is_dir()
            except OSError:
                exists = False
            # A saved *generic* ./downloads must not shadow a smarter current
            # default (e.g. the JuiceFS path). Only let the saved value win if
            # it still exists AND it's a deliberate choice — i.e. not the bare
            # fallback when a better default is now available.
            generic = {"./downloads", "downloads", "/downloads"}
            is_stale_generic = (
                out.rstrip("/") in {g.rstrip("/") for g in generic}
                and DEFAULT_OUTPUT_DIR not in generic
            )
            if exists and not is_stale_generic:
                self.query_one("#output-input", Input).value = out
            elif is_stale_generic:
                self.log_message(
                    f"Ignoring stale saved dir '{out}' — using smarter "
                    f"default {DEFAULT_OUTPUT_DIR}", "info"
                )
            else:
                self.log_message(
                    f"Saved output dir '{out}' no longer exists — "
                    f"using default {DEFAULT_OUTPUT_DIR}", "warning"
                )
        fc = s.get("file_count")
        if isinstance(fc, int):
            self.query_one("#count-input", Input).value = str(fc)
        par = s.get("parallel")
        if isinstance(par, int):
            self.query_one("#parallel-input", Input).value = str(par)
        self.log_message("Restored last-used settings")

    def _persist_settings(self, output_dir: str, file_count: int, parallel: int) -> None:
        """Save the current settings so the next launch restores them."""
        save_settings({
            "output_dir": output_dir,
            "file_count": file_count,
            "parallel": parallel,
        })

    def log_message(self, message: str, level: str = "info"):
        """Add a message to the log widget AND mirror it to a file.

        ERROR-level messages also ring the bell and flash the title so they
        can't be missed if the Log widget is scrolled away or hidden.

        The on-screen Log widget can scroll past new lines or be hidden when
        the user resizes panes, so every message also goes to a plain-text
        file (default: ./takeout.log, override with TAKEOUT_LOG_FILE). The
        user can `tail -f takeout.log` from a second SSH session to watch
        progress even if the TUI itself is unfocused or stuck.
        """
        log = self.query_one(Log)
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"{timestamp} | {message}"
        log.write_line(line)
        # Pin to bottom so the newest message is always visible. Without this,
        # long logs scroll past new lines and the user thinks the TUI is silent.
        try:
            log.scroll_end(animate=False)
        except Exception:
            pass
        # Force a repaint. Textual batches widget updates; without an explicit
        # refresh, lines written from call_from_thread may sit in the buffer
        # without rendering until the next animation frame. This is what was
        # causing "log file shows changes but the TUI shows nothing".
        try:
            log.refresh()
        except Exception:
            pass
        # Errors must be unmissable: ring the bell, flash the title. This works
        # even when the user is looking at another pane in tmux (if `set -g
        # monitor-bell on` is set).
        if level == "error":
            try:
                self.bell()
            except Exception:
                pass
            try:
                self._fire_alert(f"⚠ {message.splitlines()[0][:60]}")
            except Exception:
                pass
        # Mirror to file (best-effort — never raise from a log call).
        try:
            log_path = os.environ.get("TAKEOUT_LOG_FILE", "./takeout.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _engine_log(self, message: str) -> None:
        """Logger injected into TakeoutDownloader. The engine calls this from
        worker threads, so marshal back onto the UI thread before touching the
        Log widget. Falls back silently if the app isn't running yet."""
        try:
            self.call_from_thread(self.log_message, message)
        except Exception:
            # Called from the UI thread itself (e.g. set_curl during start),
            # or the app is shutting down — write directly, best-effort.
            try:
                self.log_message(message)
            except Exception:
                pass

    # ------------------------------------------------------------------------
    # Refresh alert: bell + title flash while waiting for a fresh capture
    # ------------------------------------------------------------------------

    def start_refresh_alert(self, reason: str) -> None:
        """Enter "needs refresh" mode: ring the bell, flash the title, and
        prompt the user to paste a fresh capture."""
        self.needs_refresh = True

        # Visual: red border + alert panel
        try:
            self.query_one("#input-section").add_class("needs-refresh")
        except Exception:
            pass
        alert = self.query_one("#alert-panel", Static)
        alert.update(
            f"🔔 [bold red]COOKIE EXPIRED[/] — {reason}\n"
            "Re-capture in the browser → extension → Copy as JSON → paste above → click [bold]Resume[/]."
        )

        # Re-label Start as Resume
        start_btn = self.query_one("#start-btn", Button)
        start_btn.label = "▶ Resume"
        start_btn.disabled = False
        self.query_one("#stop-btn", Button).disabled = True

        self.log_message(f"🔔 AUTH EXPIRED — {reason} Paste a fresh capture and click Resume.")

        # Audible + visual alert loop
        self._fire_alert()  # immediate
        if self._refresh_timer is None:
            self._refresh_timer = self.set_interval(
                self.REFRESH_ALERT_INTERVAL, self._fire_alert
            )

    def _fire_alert(self) -> None:
        """Draw attention: terminal bell + title flash + tmux window alert."""
        if not self.needs_refresh:
            return
        # Audible: terminal bell. Often silent through tmux/SSH (tmux eats BEL
        # by default), but harmless — kept as a best-effort.
        self.bell()
        # Visual: flash the title bar.
        self._title_flash_on = not self._title_flash_on
        self.title = "🔔 NEEDS FRESH CAPTURE" if self._title_flash_on else self._base_title
        # tmux-native: rename the window + emit BEL through the raw PTY so
        # tmux's monitor-bell/activity lights up the window in the status bar
        # even when you're looking at a different window/pane. This is the
        # alert most likely to actually reach you over Docker→tmux→SSH.
        self._tmux_alert("🔔 TAKEOUT: needs cookie" if self._title_flash_on else "🔔 TAKEOUT")

    @staticmethod
    def _write_raw(seq: str) -> None:
        """Write an escape sequence straight to the controlling terminal,
        bypassing Textual's renderer. Best-effort; never raises."""
        try:
            with open("/dev/tty", "w") as tty:
                tty.write(seq)
                tty.flush()
        except OSError:
            try:
                sys.__stdout__.write(seq)
                sys.__stdout__.flush()
            except Exception:
                pass

    def _tmux_alert(self, title: str) -> None:
        """Rename the window (OSC 0) and ring BEL so tmux flags the window.

        tmux with `monitor-bell on` (or `monitor-activity on`) marks the
        window in the status line on BEL/activity — the practical way to get
        noticed from another window. The OSC title also renames the tmux
        window unless `allow-rename`/`automatic-rename` are off.
        """
        self._write_raw(f"\033]0;{title}\007")

    def _tmux_alert_clear(self) -> None:
        """Restore a calm window title once the alert is resolved."""
        self._write_raw("\033]0;Takeout Downloader\007")

    def stop_refresh_alert(self) -> None:
        """Leave "needs refresh" mode and restore normal UI."""
        self.needs_refresh = False
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None
        self.title = self._base_title
        self._tmux_alert_clear()
        try:
            self.query_one("#input-section").remove_class("needs-refresh")
        except Exception:
            pass
        self.query_one("#alert-panel", Static).update("")
        start_btn = self.query_one("#start-btn", Button)
        start_btn.label = "▶ Start"

    def update_stats_display(self):
        """Update the stats panel."""
        mb = self.stats.bytes_downloaded / (1024 * 1024)

        # Calculate speed
        now = datetime.now()
        elapsed = (now - self.last_update_time).total_seconds()
        if elapsed > 0:
            bytes_diff = self.stats.bytes_downloaded - self.bytes_at_last_update
            speed = (bytes_diff / elapsed) / (1024 * 1024)
        else:
            speed = 0

        panel = self.query_one("#stats-panel", Static)
        panel.update(
            f"[bold green]✓ Done:[/] {self.stats.completed_files}  "
            f"[bold red]✗ Failed:[/] {self.stats.failed_files}  "
            f"[bold yellow]⊘ Skip:[/] {self.stats.skipped_files}  "
            f"[bold cyan]↓[/] {mb:.1f} MB  "
            f"[bold magenta]⚡[/] {speed:.1f} MB/s  "
            f"[bold]Active:[/] {len(self.active_downloads)}"
        )

        self.bytes_at_last_update = self.stats.bytes_downloaded
        self.last_update_time = now

    def update_progress_display(self) -> None:
        """Render per-file progress bars with speed, ETA, bytes.

        Shows one line per active download with:
          filename  [████████░░░░░░] 45%  120/267MB  5.2MB/s  ETA 28s
        Plus a footer with overall totals. Falls back to "(idle)" when no
        active downloads.
        """
        panel = self.query_one("#progress-display", Static)
        with self._lock:
            active = dict(self.active_downloads)
        if not active:
            panel.update("[dim](no active downloads — paste a payload and press Start)[/]")
            return
        lines: list[str] = []
        # Overall speed from last update_stats_display cycle
        now = datetime.now()
        elapsed = (now - self.last_update_time).total_seconds() or 1
        bytes_diff = self.stats.bytes_downloaded - self.bytes_at_last_update
        overall_speed_mb = (bytes_diff / elapsed) / (1024 * 1024) if elapsed > 0 else 0
        for filename, dl in active.items():
            if dl.total > 0:
                pct = min(100, int((dl.downloaded / dl.total) * 100))
                # 20-char bar
                filled = int(pct / 5)
                bar = "█" * filled + "░" * (20 - filled)
                done_mb = dl.downloaded / (1024 * 1024)
                total_mb = dl.total / (1024 * 1024)
                remaining_mb = total_mb - done_mb
                # Per-file speed estimate from progress deltas (best-effort)
                if dl.last_speed_mbps > 0 and remaining_mb > 0:
                    eta_s = remaining_mb / dl.last_speed_mbps
                    eta_str = f"ETA {int(eta_s)}s"
                else:
                    eta_str = ""
                lines.append(
                    f"{filename[:30]:30s} [{bar}] {pct:3d}%  "
                    f"{done_mb:6.1f}/{total_mb:6.1f}MB  "
                    f"{dl.last_speed_mbps:5.1f}MB/s  {eta_str}"
                )
            else:
                lines.append(f"{filename[:30]:30s} [░░░░░░░░░░░░░░░░░░░░]  --   connecting...")
        # Footer: overall throughput + queue preview summary
        queued = sum(1 for _, s in self.queue_preview if s == "queued")
        resume = sum(1 for _, s in self.queue_preview if s == "resume")
        existing = sum(1 for _, s in self.queue_preview if s == "exists")
        lines.append(
            f"[dim]Overall: {overall_speed_mb:.1f} MB/s  |  "
            f"queue: {queued}  resume: {resume}  done: {existing}  |  "
            f"bytes: {self.stats.bytes_downloaded/(1024*1024):.1f}MB[/]"
        )
        panel.update("\n".join(lines))
        try:
            panel.refresh()
        except Exception:
            pass

    def update_downloads_table(self):
        """Update the active downloads table.

        The table has two layers:
          1. The queue preview (set by _build_queue_preview after payload
             parse) — shows every expected file with its disposition:
             • queued   — will be fetched
             ↻ resume  — has a .downloading partial
             ✓ exists  — already on disk, will be skipped
             ✗ missing — 0-byte final file present (will retry)
          2. Live progress overlay — when a file is actively downloading, its
             row is replaced with a live progress / size / status. When it
             finishes, the row stays visible (status -> "done" or "skipped").

        This way the user always sees the full file list (what's coming, what
        finished, what failed) instead of just the currently active workers.
        """
        table = self.query_one("#downloads-table", DataTable)
        table.clear()

        with self._lock:
            active = dict(self.active_downloads)

        # Start with the queue preview (or an empty list if not built yet).
        rows: list[tuple[str, str, str, str]] = []
        for filename, status in self.queue_preview:
            if status == "exists":
                rows.append((filename, "✓", "on disk", "skip"))
            elif status == "resume":
                rows.append((filename, "↻", "partial", "resume"))
            elif status == "missing":
                rows.append((filename, "✗", "0 B", "MISSING"))
            else:
                rows.append((filename, "•", "—", "queued"))

        # Overlay active downloads on top of the preview rows.
        for filename, dl in active.items():
            if dl.total > 0:
                percent = int((dl.downloaded / dl.total) * 100)
                progress = f"{percent}%"
                size_str = f"{dl.downloaded/(1024*1024):.1f}/{dl.total/(1024*1024):.1f} MB"
            else:
                progress = "..."
                size_str = f"{dl.downloaded/(1024*1024):.1f} MB"
            new_row = (filename, progress, size_str, dl.status)
            replaced = False
            for i, (fn, _, _, _) in enumerate(rows):
                if fn == filename:
                    rows[i] = new_row
                    replaced = True
                    break
            if not replaced:
                rows.append(new_row)

        # Render
        if not rows:
            table.add_row("(no payload loaded yet — paste JSON or type '.' to load)", "", "", "")
            return
        for row in rows:
            table.add_row(*row)
        # Summary footer
        if self.queue_preview:
            queued = sum(1 for _, s in self.queue_preview if s == "queued")
            resume = sum(1 for _, s in self.queue_preview if s == "resume")
            existing = sum(1 for _, s in self.queue_preview if s == "exists")
            table.add_row(
                f"[{len(self.queue_preview)} files]",
                "",
                f"q:{queued} r:{resume} done:{existing}",
                f"active:{len(active)}",
            )

    def _safe_stat(self, path: Path, timeout: float = 2.0) -> Optional[int]:
        """Stat a path with a timeout. Returns file size, or None on
        timeout / error / not-found.

        CRITICAL: this exists because JuiceFS / FUSE mounts can hang
        indefinitely on stat() if the underlying network is stuck. A naive
        filepath.exists() / filepath.stat() call on such a mount will freeze
        the UI thread forever, making the TUI unresponsive to Ctrl+C, 'q',
        or any input.
        """
        import threading
        result: dict = {"size": None, "err": None}
        def _do():
            try:
                st = path.stat()
                result["size"] = st.st_size
            except (FileNotFoundError, OSError) as e:
                result["err"] = e
        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            # Thread is still running — FUSE is hung. Give up on this file.
            return None
        if result["err"] is not None:
            return None
        return result["size"]

    def _build_queue_preview(self, file_count: int) -> None:
        """Populate self.queue_preview by inspecting the output directory.

        Runs INSIDE the worker thread (see run_download) so a hung FUSE mount
        can't freeze the UI thread. Each file is stat'd with a timeout; if
        the stat hangs, the file is listed as "unknown" rather than blocking.

        For each file N in 1..file_count, determine whether:
          - the final file is on disk and complete -> "exists" (skip)
          - a .downloading partial exists         -> "resume"
          - nothing on disk                        -> "queued"
          - stat hung or errored                   -> "unknown" (engine retries)
        This is purely a *preview* — the engine's cleanup_bad_files still
        re-validates before downloading.
        """
        if not self.downloader:
            return
        preview: list[tuple[str, str]] = []
        for num in range(1, file_count + 1):
            try:
                filename = self.downloader.get_filename(num)
                filepath = self.downloader.get_filepath(num)
                temp = filepath.with_suffix(".downloading")
                size = self._safe_stat(filepath)
                if size is not None and size > 0:
                    preview.append((filename, "exists"))
                    continue
                temp_size = self._safe_stat(temp)
                if temp_size is not None and temp_size > 0:
                    preview.append((filename, "resume"))
                    continue
                if size == 0:
                    # File exists but is empty — treat as missing
                    preview.append((filename, "missing"))
                else:
                    # size is None: either not found OR stat timed out.
                    # Default to "queued" so the engine attempts the download;
                    # if the file really doesn't exist, it'll 404 quickly.
                    preview.append((filename, "queued"))
            except Exception as e:  # invalid filename, etc. — don't crash preview
                preview.append((f"file #{num}", f"error: {e}"))
        self.queue_preview = preview

    # ------------------------------------------------------------------------
    # Actions / buttons
    # ------------------------------------------------------------------------

    def action_start(self) -> None:
        self.start_download()

    def action_stop(self) -> None:
        self.stop_download()

    def action_pause(self) -> None:
        if not self.is_downloading or not self.downloader:
            return
        if self.downloader.should_pause:
            # Already paused — treat 'p' as resume for ergonomics
            self.action_continue_dl()
            return
        self.downloader.should_pause = True
        self.log_message("⏸ Pause requested — current chunk will finish, then workers park", "warning")
        self.query_one("#pause-btn", Button).disabled = True
        self.query_one("#resume-btn", Button).disabled = False

    def action_continue_dl(self) -> None:
        if not self.is_downloading or not self.downloader:
            return
        if not self.downloader.should_pause:
            return
        self.downloader.should_pause = False
        self.log_message("▶ Resume requested — workers will pick up from where they parked", "info")
        self.query_one("#pause-btn", Button).disabled = False
        self.query_one("#resume-btn", Button).disabled = True

    def action_clear_log(self) -> None:
        self.query_one(Log).clear()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self.start_download()
        elif event.button.id == "stop-btn":
            self.stop_download()
        elif event.button.id == "pause-btn":
            self.action_pause()
        elif event.button.id == "resume-btn":
            self.action_continue_dl()
        elif event.button.id == "clear-btn":
            self.action_clear_log()
        elif event.button.id == "browse-btn":
            self.action_browse()

    def action_browse(self) -> None:
        """Open the directory picker, seeded with the current output dir."""
        start = self.query_one("#output-input", Input).value.strip() or DEFAULT_OUTPUT_DIR

        def _on_picked(chosen: Optional[str]) -> None:
            if chosen:
                self.query_one("#output-input", Input).value = chosen
                self.log_message(f"Output directory set to: {chosen}")

        self.push_screen(DirectoryPicker(start), _on_picked)

    # Where the file-based fallback looks for a payload, in order. The first
    # readable, non-empty file wins.
    PAYLOAD_FILENAMES = ("in.json", "payload.json", "curl.txt")

    def _read_payload_file(self, text: str, output_dir: str) -> Optional[str]:
        """Read a payload from a file (the no-paste fallback).

        Triggered when the payload box is just "." or "@<path>". Pasting
        through SSH -> tmux -> Docker frequently strips bracketed-paste
        markers so a normal paste never reaches the app; writing the JSON to
        a file and typing "." sidesteps the terminal entirely.

        "."        -> search output_dir then cwd for in.json / payload.json /
                      curl.txt and use the first one found.
        "@<path>"  -> read exactly that file (absolute, or relative to cwd /
                      output_dir).

        Returns the file contents, or None (after logging) if nothing usable
        was found.
        """
        # Search roots, in priority order. Includes mounted container paths
        # (/downloads, /opt drop dirs) so a file written on the host is found
        # regardless of the container's cwd (/app, which is NOT mounted).
        search_roots = [
            Path(output_dir),
            Path.cwd(),
            Path("/downloads"),
            Path("/downloads/drop"),
            Path("/drop"),
            # /work is the project root mounted by docker-compose (.:/work).
            # Also look in /work/drop as a convenience if the user put it in
            # a sibling folder.
            Path("/work"),
            Path("/work/drop"),
            Path.home(),
        ]
        # De-dupe while preserving order.
        seen = set()
        roots = []
        for r in search_roots:
            if r not in seen:
                seen.add(r)
                roots.append(r)

        candidates: list[Path] = []
        if text.startswith("@"):
            raw = text[1:].strip()
            if not raw:
                self.log_message("ERROR: '@' needs a filename, e.g. @in.json", "error")
                return None
            p = Path(raw).expanduser()
            if p.is_absolute():
                candidates.append(p)
            else:
                for root in roots:
                    candidates.append(root / p)
        else:  # "."
            for root in roots:
                for name in self.PAYLOAD_FILENAMES:
                    candidates.append(root / name)

        for cand in candidates:
            try:
                if cand.is_file():
                    content = cand.read_text(encoding="utf-8", errors="replace").strip()
                    if content:
                        self.log_message(f"Loaded payload from {cand}")
                        return content
            except OSError:
                continue

        searched = ", ".join(str(c) for c in candidates[:6])
        remaining = f" (+{len(candidates) - 6} more)" if len(candidates) > 6 else ""
        # Suggest the exact host-side command that would fix this in one step.
        host_hint = ""
        try:
            host_out = output_dir.replace("/downloads", "./downloads")
            host_hint = (
                f"\n  → Quick fix on the HOST: "
                f"`echo '{{\"schema\":1,...}}' > {host_out}/in.json` "
                f"then type '.' again."
            )
        except Exception:
            pass
        self.log_message(
            f"ERROR: No payload file found in any of "
            f"{len({Path(c).parent for c in candidates})} search roots.\n"
            f"  Filenames tried: {', '.join(self.PAYLOAD_FILENAMES)}\n"
            f"  Searched: {searched}{remaining}"
            f"{host_hint}",
            "error",
        )
        return None

    def start_download(self) -> None:
        """Parse the pasted payload and start (or resume) downloading.

        Wrapped in a try/except so ANY unexpected error (a hung FUSE mount
        raising EIO, a bug in payload parsing, etc.) is logged instead of
        silently killing the UI thread. This is what previously left the
        TUI frozen on the JuiceFS path.
        """
        try:
            self._start_download_impl()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.log_message("=" * 60, "error")
            self.log_message(f"START FAILED: {e}", "error")
            # Log the full traceback to the file (not the widget — too noisy)
            try:
                with open(os.environ.get("TAKEOUT_LOG_FILE", "./takeout.log"), "a", encoding="utf-8") as f:
                    f.write(tb + "\n")
            except Exception:
                pass
            # Make sure buttons aren't stuck
            try:
                self.query_one("#start-btn", Button).disabled = False
                self.query_one("#stop-btn", Button).disabled = True
                self.query_one("#pause-btn", Button).disabled = True
                self.query_one("#resume-btn", Button).disabled = True
                self.is_downloading = False
            except Exception:
                pass

    def _start_download_impl(self) -> None:
        """The actual start_download body. Split out so the outer wrapper
        can catch exceptions without swallowing the flow control."""
        # Loud diagnostic block so we can always tell from the log file what
        # state the TUI was in when Start was pressed. Without this, "nothing
        # happens" is impossible to debug.
        self.log_message("=" * 60)
        self.log_message("Start pressed…")
        self.log_message(f"  is_downloading = {self.is_downloading}")
        text = self.query_one("#curl-input", TextArea).text.strip()
        self.log_message(f"  payload box   = {len(text)} chars, first 80: {text[:80]!r}")
        output_dir = self.query_one("#output-input", Input).value.strip() or DEFAULT_OUTPUT_DIR
        self.log_message(f"  output_dir    = {output_dir}")
        try:
            file_count = int(self.query_one("#count-input", Input).value.strip() or DEFAULT_FILE_COUNT)
            file_count = min(max(1, file_count), MAX_FILE_COUNT)
        except ValueError:
            file_count = DEFAULT_FILE_COUNT
        self.log_message(f"  file_count    = {file_count}")
        try:
            parallel = min(max(1, int(self.query_one("#parallel-input", Input).value.strip() or DEFAULT_PARALLEL)), MAX_PARALLEL)
        except ValueError:
            parallel = DEFAULT_PARALLEL

        if not text:
            self.log_message("ERROR: Paste a JSON payload or cURL command first!", "error")
            return

        # File-based input fallback. Pasting through SSH -> tmux -> Docker
        # often loses the bracketed-paste markers, so a normal paste never
        # reaches the app. As a bulletproof workaround, a payload box that
        # contains just "." (or "@file") reads the payload from a file in the
        # output dir (or cwd). Write your JSON to in.json and type a dot.
        if text == "." or text.startswith("@"):
            loaded = self._read_payload_file(text, output_dir)
            if loaded is None:
                return  # error already logged
            text = loaded

        # Parse the payload (auto-detects JSON vs cURL)
        try:
            payload = parse_payload(text)
        except ValueError as e:
            self.log_message(f"ERROR: Could not parse payload: {e}", "error")
            return

        # Validate before committing
        ok, message = payload.validate()
        if not ok:
            self.log_message(f"ERROR: {message}", "error")
            return
        if message:
            # Non-fatal warning (e.g. cookie age) — log it but proceed
            self.log_message(f"⚠ {message}", "warning")

        # Validate output directory
        try:
            validated_dir = validate_output_dir(output_dir)
            output_dir = str(validated_dir)
        except ValueError as e:
            self.log_message(f"ERROR: Invalid output directory: {e}", "error")
            return

        was_refreshing = self.needs_refresh
        # Clear any active refresh alert now that we have a fresh capture
        if self.needs_refresh:
            self.stop_refresh_alert()

        # Create (or recreate) the downloader and feed it the payload via cURL
        # bridge. Inject a thread-safe logger so the engine's messages reach
        # the Log widget instead of print() (which Textual hijacks/crashes on).
        self.downloader = TakeoutDownloader(output_dir, parallel, logger=self._engine_log)
        if not self.downloader.set_curl(payload.to_curl()):
            self.log_message("ERROR: Failed to load payload into downloader!", "error")
            return

        cookie_chars = payload.cookie_chars()
        verb = "Resuming" if was_refreshing else "Starting"
        self.log_message(f"{verb}: {file_count} files, {parallel} parallel (cookie {cookie_chars} chars)")
        self.log_message(f"Output: {output_dir}")

        # NOTE: queue preview is built in run_download (worker thread) so a
        # hung FUSE mount on filepath.stat() can't freeze the UI thread. The
        # preview will appear in the table a beat after we hand off.

        # Reset run state. Preserve cumulative stats on resume so the user sees
        # total progress, but reset on a fresh start.
        self.is_downloading = True
        if not was_refreshing:
            self.stats = DownloadStats(start_time=datetime.now())
        self.active_downloads.clear()
        self._last_file_count = file_count
        self._last_parallel = parallel

        # Remember these for next launch (best-effort).
        self._persist_settings(output_dir, file_count, parallel)

        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#stop-btn", Button).disabled = False
        self.query_one("#pause-btn", Button).disabled = False
        self.query_one("#resume-btn", Button).disabled = True

        self.run_download(file_count, parallel)

    @work(thread=True)
    def run_download(self, file_count: int, parallel: int) -> None:
        """Run downloads in a background thread."""
        if not self.downloader:
            return

        self.downloader.file_count = file_count
        self.downloader.should_stop = False
        self.downloader.should_pause = False
        self.downloader.auth_failed = False

        # Build the queue preview HERE (worker thread, not UI thread) so a
        # hung FUSE/ JuiceFS mount on filepath.stat() can't freeze the UI.
        # Without this, the TUI deadlocked on the JuiceFS path the user has.
        try:
            self._build_queue_preview(file_count)
            queued = sum(1 for _, s in self.queue_preview if s == "queued")
            resume = sum(1 for _, s in self.queue_preview if s == "resume")
            existing = sum(1 for _, s in self.queue_preview if s == "exists")
            self.call_from_thread(self.log_message,
                f"Queue: {queued} to download, {resume} to resume, "
                f"{existing} already on disk (will skip)")
            self.call_from_thread(self.update_downloads_table)
        except Exception as e:
            self.call_from_thread(
                self.log_message,
                f"⚠ Could not build queue preview: {e}",
                "warning",
            )

        while not self.downloader.should_stop:
            # Clean up bad files
            self.call_from_thread(self.log_message, "Checking files...")
            first_needed = self.downloader.cleanup_bad_files()

            # Build download list
            to_download = []
            for num in range(first_needed, file_count + 1):
                filepath = self.downloader.get_filepath(num)
                if filepath.exists() and filepath.stat().st_size > 0:
                    expected = self.downloader.size_history.get_expected_size(filepath.name)
                    if not expected or filepath.stat().st_size >= expected:
                        self.stats.skipped_files += 1
                        continue
                to_download.append(num)

            self.call_from_thread(self.update_stats_display)

            if not to_download:
                self.call_from_thread(self.log_message, "All files downloaded!")
                break

            self.call_from_thread(self.log_message, f"Downloading {len(to_download)} files...")
            self.downloader.auth_failed = False

            # Parallel downloads
            with ThreadPoolExecutor(max_workers=parallel) as executor:
                futures = {executor.submit(self.download_file, num): num for num in to_download}

                for future in as_completed(futures):
                    if self.downloader.should_stop or self.downloader.auth_failed:
                        for f in futures:
                            f.cancel()
                        break

                    num = futures[future]
                    try:
                        success, error = future.result()
                        filename = self.downloader.get_filename(num)

                        # Remove from active
                        with self._lock:
                            self.active_downloads.pop(filename, None)

                        if success:
                            self.call_from_thread(self.log_message, f"✓ {filename}")
                            self.stats.completed_files += 1
                        elif error == "AUTH_FAILED":
                            self.call_from_thread(self.log_message, f"✗ {filename}: Auth failed!")
                            self.downloader.auth_failed = True
                        elif error == "NOT_FOUND":
                            self.call_from_thread(self.log_message, f"⊘ {filename}: Not found (past last file)")
                            self.stats.skipped_files += 1
                        elif error == "Stopped":
                            pass
                        else:
                            self.call_from_thread(self.log_message, f"✗ {filename}: {error}")
                            self.stats.failed_files += 1

                        self.call_from_thread(self.update_stats_display)
                        self.call_from_thread(self.update_downloads_table)

                    except Exception as e:
                        self.call_from_thread(self.log_message, f"✗ Error: {e}")
                        self.stats.failed_files += 1

            if self.downloader.auth_failed:
                self.call_from_thread(self.handle_auth_failure)
                return
            else:
                break

        self.call_from_thread(self.download_complete)

    def download_file(self, num: int) -> tuple:
        """Download a single file with progress updates, resume support, and retry loop."""
        if not self.downloader:
            return False, "No downloader"

        filepath = self.downloader.get_filepath(num)
        url = self.downloader.get_url(num)
        filename = filepath.name

        temp_path = filepath.with_suffix('.downloading')
        resume_from = 0

        # Check for existing partial download to resume
        if temp_path.exists():
            resume_from = temp_path.stat().st_size

        # Emit start-of-file log so the user sees activity immediately
        if resume_from > 0:
            self.call_from_thread(
                self.log_message,
                f"↻ Resuming {filename} from {resume_from / (1024 * 1024):.1f}MB",
            )
        else:
            self.call_from_thread(self.log_message, f"→ Starting {filename}")

        # Add to active downloads
        with self._lock:
            status = f"Resuming from {resume_from/(1024*1024):.1f}MB" if resume_from > 0 else "Connecting"
            self.active_downloads[filename] = ActiveDownload(filename=filename, status=status, downloaded=resume_from)
        self.call_from_thread(self.update_downloads_table)

        # Retry loop — replaces the old recursive call on 416
        for attempt in range(MAX_RETRIES):
            try:
                headers = dict(self.downloader_headers())

                # Add Range header for resume
                if resume_from > 0:
                    headers['Range'] = f'bytes={resume_from}-'

                response = requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(10, 300),
                )

                # Check for auth failure via status
                if response.status_code in (401, 403):
                    return False, "AUTH_FAILED"

                if response.status_code == 404:
                    return False, "NOT_FOUND"

                # requests follows redirects by default, so 302 won't be seen here.
                # Check if the final URL redirected to a login page.
                if 'accounts.google' in response.url:
                    return False, "AUTH_FAILED"

                # 429 / 503 = rate limited. Honour Retry-After, else jittered backoff.
                if response.status_code in (429, 503):
                    if attempt < MAX_RETRIES - 1:
                        import time
                        wait = _retry_after_seconds(response) or compute_backoff(attempt)
                        with self._lock:
                            if filename in self.active_downloads:
                                self.active_downloads[filename].status = f"Rate limited, waiting {wait:.0f}s"
                        self.call_from_thread(self.update_downloads_table)
                        self.call_from_thread(
                            self.log_message,
                            f"⏳ {filename} rate-limited ({response.status_code}), waiting {wait:.0f}s",
                            "warning",
                        )
                        time.sleep(wait)
                        continue
                    return False, "RATE_LIMITED"

                # 416 = Range Not Satisfiable (file might be complete or server doesn't support range)
                if response.status_code == 416:
                    if resume_from > 0:
                        # Verify with HEAD request
                        head_resp = requests.head(url, headers={'Cookie': self.downloader.cookie, 'User-Agent': headers.get('User-Agent', '')}, timeout=10)
                        if head_resp.status_code == 200:
                            expected_size = int(head_resp.headers.get('content-length', 0))
                            if expected_size > 0 and resume_from >= expected_size:
                                self.call_from_thread(
                                    self.log_message,
                                    f"✓ Resume offset >= total ({resume_from} >= {expected_size}) — already complete",
                                )
                                temp_path.rename(filepath)
                                self.downloader.size_history.record_size(filename, resume_from)
                                return True, "resumed-complete"
                    # File is not complete — restart from scratch
                    self.call_from_thread(
                        self.log_message,
                        f"⚠ {filename} resume invalid, restarting from 0",
                        "warning",
                    )
                    temp_path.unlink(missing_ok=True)
                    resume_from = 0
                    with self._lock:
                        self.active_downloads[filename] = ActiveDownload(filename=filename, status="Restarting")
                    self.call_from_thread(self.update_downloads_table)
                    continue  # Loop back with resume_from=0

                response.raise_for_status()

                content_type = response.headers.get('content-type', '')
                if 'text/html' in content_type:
                    return False, "AUTH_FAILED"

                # Get total size — for 206, content-length is remaining bytes
                content_length = int(response.headers.get('content-length', 0))

                if response.status_code == 206:
                    total_size = resume_from + content_length
                    self.call_from_thread(
                        self.log_message,
                        f"✓ Server accepted resume: remaining {content_length/(1024*1024):.1f}MB of {total_size/(1024*1024):.1f}MB total",
                    )
                else:
                    total_size = content_length
                    if resume_from > 0:
                        # Server doesn't support resume, start fresh
                        self.call_from_thread(
                            self.log_message,
                            f"⚠ Server doesn't support resume for {filename}, restarting",
                            "warning",
                        )
                        resume_from = 0

                if total_size < 1000 and resume_from == 0:
                    return False, "AUTH_FAILED"

                # Update active download info
                with self._lock:
                    if filename in self.active_downloads:
                        self.active_downloads[filename].total = total_size
                        self.active_downloads[filename].status = "Downloading"

                filepath.parent.mkdir(parents=True, exist_ok=True)

                # Open file in append mode for resume, write mode for fresh
                file_mode = 'ab' if resume_from > 0 and response.status_code == 206 else 'wb'
                downloaded = resume_from
                last_update = datetime.now()
                last_logged_pct = -10  # next milestone
                last_logged_mb = 0
                chunk_pause_check = 0

                with open(temp_path, file_mode) as f:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        # Honor stop and pause flags before doing any I/O.
                        if self.downloader.should_stop:
                            return False, "Stopped"
                        if self.downloader.should_pause:
                            self.call_from_thread(
                                self.log_message,
                                f"⏸ Paused at {downloaded/(1024*1024):.1f}MB ({filename})",
                            )
                            # Park the worker until pause is lifted or stop requested.
                            while self.downloader.should_pause and not self.downloader.should_stop:
                                time.sleep(0.5)
                            if self.downloader.should_stop:
                                return False, "Stopped"
                            self.call_from_thread(
                                self.log_message,
                                f"▶ Resumed {filename} from {downloaded/(1024*1024):.1f}MB",
                            )

                        if chunk:
                            # Check first chunk for ZIP magic (only on fresh downloads)
                            if downloaded == 0 and chunk[:2] != b'PK':
                                temp_path.unlink()
                                return False, "AUTH_FAILED"

                            f.write(chunk)
                            downloaded += len(chunk)
                            self.stats.bytes_downloaded += len(chunk)

                            # Throttled UI update every 300ms
                            now = datetime.now()
                            if (now - last_update).total_seconds() >= 0.3:
                                with self._lock:
                                    if filename in self.active_downloads:
                                        ad = self.active_downloads[filename]
                                        ad.downloaded = downloaded
                                        # Per-file speed: bytes since last tick / elapsed
                                        tick_elapsed = (now - datetime.fromtimestamp(ad.last_tick_time)).total_seconds() if ad.last_tick_time else 0.3
                                        if tick_elapsed > 0 and ad.last_tick_time:
                                            tick_bytes = downloaded - ad.last_tick_bytes
                                            ad.last_speed_mbps = (tick_bytes / tick_elapsed) / (1024 * 1024)
                                        ad.last_tick_bytes = downloaded
                                        ad.last_tick_time = now.timestamp()
                                self.call_from_thread(self.update_downloads_table)
                                self.call_from_thread(self.update_stats_display)
                                self.call_from_thread(self.update_progress_display)
                                last_update = now

                            # Verbose log milestones: every 10% or every ~25MB
                            if total_size > 0:
                                pct = int(downloaded * 100 / total_size)
                                if pct >= last_logged_pct + 10:
                                    self.call_from_thread(
                                        self.log_message,
                                        f"  {filename} {pct}% ({downloaded/(1024*1024):.0f}/{total_size/(1024*1024):.0f}MB)",
                                    )
                                    last_logged_pct = pct
                            else:
                                mb = downloaded // (25 * 1024 * 1024)
                                if mb > last_logged_mb:
                                    self.call_from_thread(
                                        self.log_message,
                                        f"  {filename} {downloaded/(1024*1024):.0f}MB",
                                    )
                                    last_logged_mb = mb

                # Verify ZIP integrity before finalizing
                with open(temp_path, 'rb') as f:
                    f.seek(max(0, downloaded - 1024))
                    tail = f.read()
                    if b'PK\x05\x06' not in tail:
                        temp_path.unlink()
                        return False, "INTEGRITY_FAILED"

                # Sanity check: written size matches Content-Length
                if total_size > 0 and downloaded != total_size:
                    self.call_from_thread(
                        self.log_message,
                        f"⚠ {filename} size mismatch: got {downloaded}, expected {total_size}",
                        "warning",
                    )

                temp_path.rename(filepath)
                self.downloader.size_history.record_size(filename, downloaded)
                self.call_from_thread(
                    self.log_message,
                    f"✓ {filename} done ({downloaded/(1024*1024):.1f}MB)",
                )
                return True, ""

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    return False, "NOT_FOUND"
                # Retry transient errors with jittered backoff
                if attempt < MAX_RETRIES - 1:
                    import time
                    wait = compute_backoff(attempt)
                    self.call_from_thread(
                        self.log_message,
                        f"⚠ {filename} HTTP error (attempt {attempt+1}/{MAX_RETRIES}), retrying in {wait:.1f}s",
                        "warning",
                    )
                    time.sleep(wait)
                    continue
                return False, str(e)
            except requests.exceptions.RequestException as e:
                # Retry transient network errors with jittered backoff
                if attempt < MAX_RETRIES - 1:
                    import time
                    wait = compute_backoff(attempt)
                    self.call_from_thread(
                        self.log_message,
                        f"⚠ {filename} network error (attempt {attempt+1}/{MAX_RETRIES}), retrying in {wait:.1f}s",
                        "warning",
                    )
                    time.sleep(wait)
                    continue
                return False, str(e)

        return False, "Max retries exceeded"

    def downloader_headers(self) -> dict:
        """Headers for a download request, including the captured cookie."""
        return {
            'Cookie': self.downloader.cookie,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        }

    def handle_auth_failure(self):
        """Handle authentication failure: stop downloads and alert the user."""
        self.is_downloading = False
        self.active_downloads.clear()
        self.update_downloads_table()
        # Beep + flash + prompt for a fresh capture
        self.start_refresh_alert("Google rejected the session (expired after a few files).")

    def download_complete(self):
        """Handle download completion."""
        self.is_downloading = False
        if self.needs_refresh:
            self.stop_refresh_alert()
        self.query_one("#start-btn", Button).disabled = False
        self.query_one("#stop-btn", Button).disabled = True
        self.query_one("#pause-btn", Button).disabled = True
        self.query_one("#resume-btn", Button).disabled = True
        self.active_downloads.clear()
        self.update_downloads_table()

        mb = self.stats.bytes_downloaded / (1024 * 1024)
        self.log_message(
            f"Done! ✓{self.stats.completed_files} ✗{self.stats.failed_files} "
            f"⊘{self.stats.skipped_files} | {mb:.1f} MB"
        )

    def stop_download(self) -> None:
        """Stop the download process."""
        if self.needs_refresh:
            # User chose to abandon the refresh wait
            self.stop_refresh_alert()
            self.log_message("Refresh cancelled.")
            self.query_one("#start-btn", Button).disabled = False
            self.query_one("#stop-btn", Button).disabled = True
            self.query_one("#pause-btn", Button).disabled = True
            self.query_one("#resume-btn", Button).disabled = True
            return
        if self.downloader:
            self.downloader.stop()
            self.log_message("Stopping...")
            # Stop also clears pause so any parked worker exits.
            self.query_one("#pause-btn", Button).disabled = True
            self.query_one("#resume-btn", Button).disabled = True


def main():
    import signal
    app = TakeoutTUI()
    # Ensure Ctrl+C in the terminal actually kills the process even if the
    # Textual event loop is wedged on a hung syscall (e.g. JuiceFS stat()
    # deadlock). Without this, a stuck UI thread ignores SIGINT entirely.
    def _kill(_signum, _frame):
        import os
        os._exit(130)  # 128 + SIGINT(2) = conventional Ctrl+C exit code
    try:
        signal.signal(signal.SIGINT, _kill)
    except (ValueError, OSError):
        pass  # not on main thread, or signal not available
    app.run()


if __name__ == "__main__":
    main()
