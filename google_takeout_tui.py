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
from typing import Optional, Dict
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Header, Footer, Static, Button, Input, Label,
    Log, DataTable, TextArea, DirectoryTree
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
class ActiveDownload:
    """Track an active download."""
    filename: str
    downloaded: int = 0
    total: int = 0
    status: str = "Starting"


class DirectoryPicker(ModalScreen):
    """Modal directory browser. Navigate the filesystem, or type/paste a path.

    Dismisses with the chosen absolute path (str) on "Use this folder", or
    None on Cancel. Symlinks are followed (resolve), so a path like
    ./downloads/opt -> /opt lands on the real target.
    """

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
    #picker-tree { height: 1fr; border: round $secondary; }
    #picker-buttons { height: 3; align: center middle; margin-top: 1; }
    #picker-buttons Button { margin: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
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

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-card"):
            yield Label("Select output directory", id="picker-title")
            yield Label(str(self._cwd), id="picker-cwd")
            yield Input(value=str(self._cwd), placeholder="Type or paste a path, then Enter", id="picker-path-input")
            tree = DirectoryTree(str(self._cwd), id="picker-tree")
            yield tree
            with Horizontal(id="picker-buttons"):
                yield Button("\u2191 Up", id="picker-up", variant="default")
                yield Button("\u2705 Use this folder", id="picker-use", variant="success")
                yield Button("\u2716 Cancel", id="picker-cancel", variant="error")

    def _set_cwd(self, path: Path) -> None:
        """Navigate to a directory.

        Optimistic + threaded: the header/input update to the requested path
        instantly, the tree is greyed with a loading overlay, and the slow
        filesystem work (resolve / is_dir / listing) runs off the UI thread so
        the picker never freezes on JuiceFS / encfs / network mounts.
        """
        target = str(path)
        # Instant feedback — no stale header while the FS is being stat'd.
        self.query_one("#picker-cwd", Label).update(f"\u23f3 {target}")
        self.query_one("#picker-path-input", Input).value = target
        tree = self.query_one("#picker-tree", DirectoryTree)
        tree.loading = True
        self._load_dir(path)

    @work(thread=True, exclusive=True, group="picker-load")
    def _load_dir(self, path: Path) -> None:
        """Resolve + validate a directory off the UI thread, then apply it."""
        try:
            resolved = path.expanduser().resolve()
            ok = resolved.is_dir()
        except (OSError, ValueError):
            resolved, ok = None, False
        self.app.call_from_thread(self._apply_dir, resolved, ok)

    def _apply_dir(self, resolved: Optional[Path], ok: bool) -> None:
        """Runs on the UI thread once _load_dir has validated the path."""
        tree = self.query_one("#picker-tree", DirectoryTree)
        cwd_label = self.query_one("#picker-cwd", Label)
        if not ok or resolved is None:
            # Revert the optimistic header to the last good directory.
            cwd_label.update(f"\u26a0 Not a directory \u2014 staying in {self._cwd}")
            self.query_one("#picker-path-input", Input).value = str(self._cwd)
            tree.loading = False
            self.app.bell()
            return
        self._cwd = resolved
        cwd_label.update(str(resolved))
        self.query_one("#picker-path-input", Input).value = str(resolved)
        tree.path = str(resolved)
        tree.reload()
        # The tree lists its own children in a background worker from here;
        # drop the overlay so the user sees rows populate as they arrive.
        tree.loading = False

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        # Single click highlights; selecting a directory navigates into it.
        self._set_cwd(Path(event.path))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "picker-path-input":
            self._set_cwd(Path(event.value))

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
        Binding("c", "clear_log", "Clear Log"),
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

    def compose(self) -> ComposeResult:
        yield Header()

        with Container(id="main-container"):
            # Input section
            with Vertical(id="input-section") as input_section:
                input_section.border_title = "1 · Payload"
                yield Label(
                    "[bold]Paste payload — JSON from the extension's "
                    "\"Copy as JSON\", or a cURL command:[/]"
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

            # Log section
            with Vertical(id="log-section") as log_section:
                log_section.border_title = "4 · Activity log"
                yield Log(highlight=True)

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
        self.log_message("Keys: Q=quit, S=start, X=stop, C=clear, B=browse output dir")
        if ARIA2C_AVAILABLE:
            self.log_message("aria2c detected — available for high-speed downloads")
        else:
            self.log_message("Tip: Install aria2c for multi-connection downloads (apt install aria2)")
        self.log_message("Install the browser extension from helpers/ to capture payloads")

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

    def _restore_settings(self) -> None:
        """Pre-fill the input fields from the persisted settings file."""
        s = load_settings()
        if not s:
            return
        out = s.get("output_dir")
        if isinstance(out, str) and out:
            self.query_one("#output-input", Input).value = out
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
        """Add a message to the log."""
        log = self.query_one(Log)
        timestamp = datetime.now().strftime("%H:%M:%S")
        log.write_line(f"{timestamp} | {message}")

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

    def update_downloads_table(self):
        """Update the active downloads table."""
        table = self.query_one("#downloads-table", DataTable)
        table.clear()

        with self._lock:
            for filename, dl in self.active_downloads.items():
                if dl.total > 0:
                    percent = int((dl.downloaded / dl.total) * 100)
                    progress = f"{percent}%"
                    size_str = f"{dl.downloaded/(1024*1024):.1f}/{dl.total/(1024*1024):.1f} MB"
                else:
                    progress = "..."
                    size_str = f"{dl.downloaded/(1024*1024):.1f} MB"
                table.add_row(filename, progress, size_str, dl.status)

    # ------------------------------------------------------------------------
    # Actions / buttons
    # ------------------------------------------------------------------------

    def action_start(self) -> None:
        self.start_download()

    def action_stop(self) -> None:
        self.stop_download()

    def action_clear_log(self) -> None:
        self.query_one(Log).clear()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self.start_download()
        elif event.button.id == "stop-btn":
            self.stop_download()
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

    def start_download(self) -> None:
        """Parse the pasted payload and start (or resume) downloading."""
        if self.is_downloading:
            return

        text = self.query_one("#curl-input", TextArea).text.strip()
        output_dir = self.query_one("#output-input", Input).value.strip() or DEFAULT_OUTPUT_DIR

        try:
            file_count = int(self.query_one("#count-input", Input).value.strip() or DEFAULT_FILE_COUNT)
            file_count = min(max(1, file_count), MAX_FILE_COUNT)
        except ValueError:
            file_count = DEFAULT_FILE_COUNT

        try:
            parallel = min(max(1, int(self.query_one("#parallel-input", Input).value.strip() or DEFAULT_PARALLEL)), MAX_PARALLEL)
        except ValueError:
            parallel = DEFAULT_PARALLEL

        if not text:
            self.log_message("ERROR: Paste a JSON payload or cURL command first!", "error")
            return

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

        # Create (or recreate) the downloader and feed it the payload via cURL bridge
        self.downloader = TakeoutDownloader(output_dir, parallel)
        if not self.downloader.set_curl(payload.to_curl()):
            self.log_message("ERROR: Failed to load payload into downloader!", "error")
            return

        cookie_chars = payload.cookie_chars()
        verb = "Resuming" if was_refreshing else "Starting"
        self.log_message(f"{verb}: {file_count} files, {parallel} parallel (cookie {cookie_chars} chars)")
        self.log_message(f"Output: {output_dir}")

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

        self.run_download(file_count, parallel)

    @work(thread=True)
    def run_download(self, file_count: int, parallel: int) -> None:
        """Run downloads in a background thread."""
        if not self.downloader:
            return

        self.downloader.file_count = file_count
        self.downloader.should_stop = False
        self.downloader.auth_failed = False

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
                                temp_path.rename(filepath)
                                self.downloader.size_history.record_size(filename, resume_from)
                                return True, "resumed-complete"
                    # File is not complete — restart from scratch
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
                else:
                    total_size = content_length
                    if resume_from > 0:
                        # Server doesn't support resume, start fresh
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

                with open(temp_path, file_mode) as f:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if self.downloader.should_stop:
                            return False, "Stopped"

                        if chunk:
                            # Check first chunk for ZIP magic (only on fresh downloads)
                            if downloaded == 0 and chunk[:2] != b'PK':
                                temp_path.unlink()
                                return False, "AUTH_FAILED"

                            f.write(chunk)
                            downloaded += len(chunk)
                            self.stats.bytes_downloaded += len(chunk)

                            # Update progress every 300ms
                            now = datetime.now()
                            if (now - last_update).total_seconds() >= 0.3:
                                with self._lock:
                                    if filename in self.active_downloads:
                                        self.active_downloads[filename].downloaded = downloaded
                                self.call_from_thread(self.update_downloads_table)
                                self.call_from_thread(self.update_stats_display)
                                last_update = now

                # Verify ZIP integrity before finalizing
                with open(temp_path, 'rb') as f:
                    f.seek(max(0, downloaded - 1024))
                    tail = f.read()
                    if b'PK\x05\x06' not in tail:
                        temp_path.unlink()
                        return False, "INTEGRITY_FAILED"

                temp_path.rename(filepath)
                self.downloader.size_history.record_size(filename, downloaded)

                return True, ""

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    return False, "NOT_FOUND"
                # Retry transient errors with jittered backoff
                if attempt < MAX_RETRIES - 1:
                    import time
                    time.sleep(compute_backoff(attempt))
                    continue
                return False, str(e)
            except requests.exceptions.RequestException as e:
                # Retry transient network errors with jittered backoff
                if attempt < MAX_RETRIES - 1:
                    import time
                    time.sleep(compute_backoff(attempt))
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
            return
        if self.downloader:
            self.downloader.stop()
            self.log_message("Stopping...")


def main():
    app = TakeoutTUI()
    app.run()


if __name__ == "__main__":
    main()
