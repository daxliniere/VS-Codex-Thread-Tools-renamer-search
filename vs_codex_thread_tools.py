"""
VS-Codex Thread Tools

A small Windows/Tkinter GUI for renaming, searching, and reading Codex VS Code extension threads.

It can:
  1. Rename thread titles in .codex/session_index.jsonl.
  2. Rename matching thread_name values or matching title strings in .codex/sessions/**/*.jsonl.
  3. Search user/developer message content across session JSONL files.
  4. Read session JSONL files as a human-friendly chat transcript.

Safety features:
  - Warns on startup if VS Code appears to be running, with a Continue anyway option.
  - Blocks saving renamed threads while VS Code appears to be running.
  - Creates timestamped backups before writing any files.
  - Persists search and reader checkbox settings in settings.ini.
  - Writes a crash log if the windowed executable fails during startup.
"""

from __future__ import annotations

import configparser
import base64
import csv
import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "VS-Codex Thread Tools"
APP_VERSION = "v17"
APP_FOLDER_NAME = "VS-Codex Thread Tools"
COPYRIGHT_LINE = "Dax Liniere 2026."
DEFAULT_INDEX_RELATIVE = Path(".codex") / "session_index.jsonl"
DEFAULT_SESSIONS_RELATIVE = Path(".codex") / "sessions"
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# VS Code proper normally appears as Code.exe. The other names are included for
# common VS Code-like tools that may use or touch the same Codex files.
VSCODE_PROCESS_NAMES = (
    "Code.exe",
    "Code - Insiders.exe",
    "VSCodium.exe",
    "Cursor.exe",
    "Windsurf.exe",
)


@dataclass
class ThreadRecord:
    line_number: int
    thread_id: str
    old_name: str
    new_name: str
    updated_at: str = ""

    @property
    def changed(self) -> bool:
        return self.old_name != self.new_name


@dataclass
class SearchHit:
    role: str
    line_number: int
    count: int
    snippet: str


@dataclass
class SearchResult:
    thread_id: str
    title: str
    updated_at: str
    session_file: Path
    file_size: int = 0
    total_matches: int = 0
    roles: Counter = field(default_factory=Counter)
    hits: List[SearchHit] = field(default_factory=list)


@dataclass
class ChatThread:
    thread_id: str
    title: str
    updated_at: str
    session_file: Path
    file_size: int = 0


@dataclass
class ChatMessage:
    timestamp: str
    kind: str
    label: str
    text: str
    line_number: int
    phase: str = ""
    token_total: int = 0
    images: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Paths, settings, logging
# ---------------------------------------------------------------------------


def default_index_path() -> Path:
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        return Path(user_profile) / DEFAULT_INDEX_RELATIVE
    return Path.home() / DEFAULT_INDEX_RELATIVE


def default_sessions_path() -> Path:
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        return Path(user_profile) / DEFAULT_SESSIONS_RELATIVE
    return Path.home() / DEFAULT_SESSIONS_RELATIVE


def app_config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_FOLDER_NAME
    return Path.home() / f".{APP_FOLDER_NAME}"


def settings_path() -> Path:
    return app_config_dir() / "settings.ini"


def runtime_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_FOLDER_NAME / "runtime.log"
    return app_config_dir() / "runtime.log"


def log_runtime(message: str) -> None:
    try:
        path = runtime_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{_dt.datetime.now().isoformat()} {message}\n")
    except Exception:
        pass


def crash_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_FOLDER_NAME / "crash.log"
    return app_config_dir() / "crash.log"


def timestamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def parse_codex_datetime(value: str) -> Optional[_dt.datetime]:
    """Parse Codex ISO timestamps, including trailing Z, into aware datetimes."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        # Some Codex timestamps have seven fractional-second digits. Python accepts
        # six, so trim only the fractional part and try again.
        match = re.match(r"^(.*?\.\d{6})\d+([+-]\d\d:\d\d)?$", text)
        if match:
            try:
                return _dt.datetime.fromisoformat("".join(part for part in match.groups() if part))
            except ValueError:
                return None
        return None


def human_datetime(value: str) -> str:
    """Return a compact, local, human-readable timestamp with seconds, no ms."""
    parsed = parse_codex_datetime(value)
    if parsed is None:
        return value or ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def plural(count: int, singular: str, plural_word: Optional[str] = None) -> str:
    word = singular if count == 1 else (plural_word or singular + "s")
    return f"{count} {word}"


def file_size_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def human_filesize(num_bytes: int) -> str:
    try:
        value = float(num_bytes)
    except Exception:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    if idx == 0:
        return f"{int(value)} B"
    if value >= 100:
        return f"{value:.0f} {units[idx]}"
    if value >= 10:
        return f"{value:.1f} {units[idx]}"
    return f"{value:.2f} {units[idx]}"


def open_path_default(path: Path) -> None:
    if platform.system().lower() == "windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif platform.system().lower() == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_folder_default(path: Path) -> None:
    open_path_default(path)


def safe_temp_image_from_data_uri(data_uri: str) -> Optional[Path]:
    match = re.match(r"^data:image/(png|jpe?g);base64,(.+)$", data_uri, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    ext = ".jpg" if match.group(1).lower() in {"jpg", "jpeg"} else ".png"
    digest = hashlib.sha256(data_uri.encode("utf-8", errors="ignore")).hexdigest()[:24]
    folder = (os.environ.get("LOCALAPPDATA") and Path(os.environ["LOCALAPPDATA"]) / APP_FOLDER_NAME / "images") or app_config_dir() / "images"
    folder.mkdir(parents=True, exist_ok=True)
    image_path = folder / f"embedded-{digest}{ext}"
    if not image_path.exists():
        image_path.write_bytes(base64.b64decode(match.group(2)))
    return image_path


def normalize_image_ref(value: Any, session_file: Optional[Path] = None) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.lower().startswith("data:image/"):
        temp_path = safe_temp_image_from_data_uri(text)
        return str(temp_path) if temp_path else None
    if re.search(r"\.(?:png|jpe?g)(?:[?#].*)?$", text, flags=re.IGNORECASE):
        # Strip simple file:// wrapping and URL encoding used by some local links.
        cleaned = text
        if cleaned.lower().startswith("file:///"):
            cleaned = cleaned[8:]
        elif cleaned.lower().startswith("file://"):
            cleaned = cleaned[7:]
        cleaned = cleaned.replace("%20", " ")
        candidate = Path(cleaned)
        if not candidate.is_absolute() and session_file is not None:
            candidate = session_file.parent / candidate
        return str(candidate)
    return None


def extract_image_refs(value: Any, session_file: Optional[Path] = None) -> List[str]:
    refs: List[str] = []

    def add_ref(candidate: Any) -> None:
        normalized = normalize_image_ref(candidate, session_file)
        if normalized and normalized not in refs:
            refs.append(normalized)

    def walk(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            add_ref(item)
            return
        if isinstance(item, list):
            for child in item:
                walk(child)
            return
        if isinstance(item, dict):
            for key in ("path", "local_path", "file_path", "uri", "url", "image_url", "data", "src"):
                if key in item:
                    add_ref(item.get(key))
            for key in ("images", "local_images", "content"):
                if key in item:
                    walk(item.get(key))

    walk(value)
    return refs




@dataclass
class LocalFileLink:
    display: str
    path: Path
    line_number: Optional[int] = None


MD_LOCAL_LINK_RE = re.compile(r"\[([^\]\r\n]+)\]\((<[^>\r\n]+>|[^)\r\n]+)\)")


def normalize_local_file_target(raw_target: str) -> Optional[LocalFileLink]:
    """Return a local-file link for markdown targets such as </c:/folder/file.js:186>."""
    raw_target = raw_target.strip()
    # A broken Markdown link should never be allowed to eat the rest of the message.
    # Reject anything multiline or very long before normalising.
    if "\n" in raw_target or "\r" in raw_target or len(raw_target) > 600:
        return None
    target = raw_target.strip("<>").strip()
    if not target:
        return None
    target = urllib.parse.unquote(target)
    if "\n" in target or "\r" in target or "\\n" in target or "\\r" in target:
        return None
    target = target.replace("\\", "/")

    if target.lower().startswith("file:///"):
        target = target[8:]
    elif target.lower().startswith("file://"):
        target = target[7:]

    # Codex/VS Code sometimes emits /c:/folder/file.ext:123. Windows wants c:/folder/file.ext.
    if re.match(r"^/[A-Za-z]:/", target):
        target = target[1:]

    line_number: Optional[int] = None
    line_match = re.match(r"^(.*?):(\d+)(?::\d+)?$", target)
    if line_match and not re.fullmatch(r"[A-Za-z]", line_match.group(1)):
        target = line_match.group(1)
        try:
            line_number = int(line_match.group(2))
        except Exception:
            line_number = None

    if not re.match(r"^[A-Za-z]:/", target) and not target.startswith("/"):
        return None

    display_path = target
    if platform.system().lower() == "windows":
        display_path = display_path.replace("/", "\\")
    return LocalFileLink(display=display_path, path=Path(display_path), line_number=line_number)


def split_text_with_local_file_links(text: str) -> List[Tuple[str, Optional[LocalFileLink]]]:
    """Split reader text into plain and clickable local-file-link segments.

    Markdown links are made human-readable: [en.js](</c:/a/en.js:186>) becomes
    clickable text like en.js (line 186). Plain text around the link is preserved.
    """
    pieces: List[Tuple[str, Optional[LocalFileLink]]] = []
    pos = 0
    for match in MD_LOCAL_LINK_RE.finditer(text):
        raw_target = match.group(2)
        link = normalize_local_file_target(raw_target)
        if link is None:
            continue
        if match.start() > pos:
            pieces.append((text[pos:match.start()], None))
        label = match.group(1).strip() or link.path.name or link.display
        display = label
        if link.line_number is not None:
            display += f" (line {link.line_number})"
        pieces.append((display, link))
        pos = match.end()
    if pos < len(text):
        pieces.append((text[pos:], None))
    return pieces or [(text, None)]


def friendly_role(role: str) -> str:
    lookup = {"user": "User", "developer": "Developer"}
    return lookup.get(role.lower(), role[:1].upper() + role[1:])


def friendly_roles_text(roles: Counter) -> str:
    if not roles:
        return ""
    return ", ".join(f"{friendly_role(role)} {count}" for role, count in sorted(roles.items()))


def friendly_session_label(path: Path) -> str:
    """Return a shorter, scannable label for a session file."""
    parts = path.parts
    # Prefer the date folders when the path contains sessions/YYYY/MM/DD/file.jsonl.
    for idx, part in enumerate(parts):
        if part.lower() == "sessions" and idx + 4 < len(parts):
            return "/".join(parts[idx + 1 : idx + 4]) + " / " + path.name
    return path.name


def clean_chat_text(text: str) -> str:
    """Remove common Codex/VS Code wrapper text from previews."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return ""

    # Hide utility interruption records that are not real chat content.
    if cleaned.startswith("<turn_aborted>") and "</turn_aborted>" in cleaned:
        return ""

    marker = "## My request for Codex:"
    if marker in cleaned:
        cleaned = cleaned.split(marker, 1)[1].strip()

    # Remove leading IDE context if the explicit request marker was absent.
    cleaned = re.sub(
        r"^# Context from my IDE setup:\s*(?:## Open tabs:\s*(?:- .*?\n)+)?",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()

    # Drop a few high-noise XML-ish wrappers from previews.
    cleaned = re.sub(r"</?(?:environment_context|turn_aborted)>", "", cleaned).strip()

    return cleaned


def tighten_assistant_final_text(text: str) -> str:
    """Tighten final-answer text without flattening normal line breaks."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # Some Codex records arrive with paragraph breaks doubled after rendering. Collapse
    # runs of blank lines to a single blank line, and trim trailing spaces per line.
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    cleaned = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", cleaned)
    return cleaned


def display_snippet_text(text: str, needle: str) -> str:
    cleaned = clean_chat_text(text)
    if cleaned and needle.lower() in cleaned.lower():
        return cleaned
    return cleaned or text


def first_nonempty_line(text: str, max_chars: int = 140) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:max_chars] + ("..." if len(line) > max_chars else "")
    return ""


def format_hit_heading(hit: "SearchHit") -> str:
    return f"{friendly_role(hit.role)} message - source line {hit.line_number} - {plural(hit.count, 'match', 'matches')}"


def load_settings() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    path = settings_path()
    if path.exists():
        try:
            config.read(path, encoding="utf-8")
        except Exception:
            pass
    return config


def save_settings(config: configparser.ConfigParser) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        config.write(f)


def write_crash_log(exc: BaseException) -> Path:
    path = crash_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n" + "=" * 80 + "\n")
        f.write(f"{_dt.datetime.now().isoformat()}\n")
        f.write(f"Executable: {sys.executable}\n")
        f.write(f"Python: {sys.version}\n")
        f.write("\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    return path


# ---------------------------------------------------------------------------
# VS Code safety checks
# ---------------------------------------------------------------------------


def running_vscode_processes() -> List[str]:
    """Return names of running VS Code-like processes on Windows.

    Uses tasklist so the app has no dependency on psutil. On non-Windows
    platforms this returns an empty list because the target app is Windows.
    """
    if platform.system().lower() != "windows":
        return []

    running: List[str] = []
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    for process_name in VSCODE_PROCESS_NAMES:
        try:
            result = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    f"IMAGENAME eq {process_name}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=create_no_window,
                check=False,
            )
        except Exception:
            continue

        output = result.stdout.strip()
        if not output or output.upper().startswith("INFO:"):
            continue

        try:
            rows = list(csv.reader(output.splitlines()))
        except Exception:
            rows = []

        for row in rows:
            if not row:
                continue
            found_name = row[0].strip().strip('"')
            if found_name.lower() == process_name.lower():
                running.append(found_name)
                break

    return sorted(set(running), key=str.lower)


def ensure_vscode_is_closed_for_save(parent: tk.Tk) -> bool:
    """Show a blocking Retry/Cancel warning until VS Code is closed before saving."""
    while True:
        processes = running_vscode_processes()
        if not processes:
            return True

        process_list = ", ".join(processes)
        retry = messagebox.askretrycancel(
            APP_NAME,
            f"VS Code appears to be running ({process_list}).\n\n"
            "Close VS Code before saving changes. This prevents Codex files being "
            "edited while VS Code may also be reading or writing them.\n\n"
            "Saving cannot continue until VS Code is closed.",
            parent=parent,
        )
        if not retry:
            return False


def warn_vscode_running_on_startup(parent: tk.Tk) -> bool:
    """Warn on startup if VS Code is running. Return True to continue."""
    processes = running_vscode_processes()
    if not processes:
        return True

    process_list = ", ".join(processes)
    dialog = tk.Toplevel(parent)
    dialog.title(APP_NAME)
    dialog.transient(parent)
    dialog.resizable(False, False)
    dialog.attributes("-topmost", True)

    result = {"continue": False}

    frame = ttk.Frame(dialog, padding=16)
    frame.pack(fill=tk.BOTH, expand=True)

    heading = ttk.Label(
        frame,
        text="VS Code appears to be running",
        font=("Segoe UI", 11, "bold"),
    )
    heading.pack(anchor=tk.W, pady=(0, 8))

    message = (
        f"Detected process(es): {process_list}\n\n"
        "It is safest to close VS Code before using this tool because VS Code may "
        "also be reading or writing Codex files.\n\n"
        "You can continue anyway to view or prepare edits, but saving renamed "
        "threads will still be blocked until VS Code is closed."
    )
    ttk.Label(frame, text=message, wraplength=500, justify=tk.LEFT).pack(anchor=tk.W)

    buttons = ttk.Frame(frame)
    buttons.pack(fill=tk.X, pady=(16, 0))

    def exit_tool() -> None:
        result["continue"] = False
        dialog.destroy()

    def continue_anyway() -> None:
        result["continue"] = True
        dialog.destroy()

    ttk.Button(buttons, text="Close this tool", command=exit_tool).pack(side=tk.RIGHT)
    ttk.Button(buttons, text="Continue anyway", command=continue_anyway).pack(side=tk.RIGHT, padx=(0, 8))

    dialog.protocol("WM_DELETE_WINDOW", exit_tool)
    dialog.update_idletasks()
    width = dialog.winfo_reqwidth()
    height = dialog.winfo_reqheight()
    x = dialog.winfo_screenwidth() // 2 - width // 2
    y = dialog.winfo_screenheight() // 2 - height // 2
    dialog.geometry(f"{width}x{height}+{x}+{y}")
    dialog.lift()
    dialog.focus_force()
    dialog.after(250, lambda: dialog.attributes("-topmost", False))
    dialog.grab_set()
    parent.wait_window(dialog)
    return result["continue"]


# ---------------------------------------------------------------------------
# JSONL helpers and rename logic
# ---------------------------------------------------------------------------


def read_jsonl_lines(path: Path) -> Tuple[List[str], str]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        lines = f.readlines()

    crlf = sum(1 for line in lines if line.endswith("\r\n"))
    lf = sum(1 for line in lines if line.endswith("\n") and not line.endswith("\r\n"))
    newline = "\r\n" if crlf > lf else "\n"
    return lines, newline


def parse_index_file(index_path: Path) -> Tuple[List[ThreadRecord], List[str], str]:
    lines, newline = read_jsonl_lines(index_path)
    records: List[ThreadRecord] = []

    for line_no, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        if not isinstance(obj, dict):
            continue

        thread_id = obj.get("id")
        thread_name = obj.get("thread_name")
        if isinstance(thread_id, str) and isinstance(thread_name, str):
            updated_at = obj.get("updated_at", "")
            records.append(
                ThreadRecord(
                    line_number=line_no,
                    thread_id=thread_id,
                    old_name=thread_name,
                    new_name=thread_name,
                    updated_at=str(updated_at) if updated_at is not None else "",
                )
            )

    return records, lines, newline


def parse_index_map(index_path: Path) -> Dict[str, ThreadRecord]:
    records, _lines, _newline = parse_index_file(index_path)
    return {record.thread_id.lower(): record for record in records}


def json_dump_line(obj: Any, newline: str) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + newline


def contains_thread_name(obj: Any) -> bool:
    if isinstance(obj, dict):
        if "thread_name" in obj:
            return True
        return any(contains_thread_name(value) for value in obj.values())
    if isinstance(obj, list):
        return any(contains_thread_name(item) for item in obj)
    return False


def replace_thread_name_values(obj: Any, new_value: str) -> int:
    """Replace every key named thread_name inside obj. Return replacements."""
    replacements = 0
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if key == "thread_name":
                obj[key] = new_value
                replacements += 1
            else:
                replacements += replace_thread_name_values(obj[key], new_value)
    elif isinstance(obj, list):
        for item in obj:
            replacements += replace_thread_name_values(item, new_value)
    return replacements


def replace_string_occurrences(obj: Any, old_value: str, new_value: str) -> int:
    """Replace verbatim old title text inside JSON string values. Return occurrences changed."""
    if not old_value:
        return 0
    replacements = 0
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if isinstance(value, str):
                count = value.count(old_value)
                if count:
                    obj[key] = value.replace(old_value, new_value)
                    replacements += count
            else:
                replacements += replace_string_occurrences(value, old_value, new_value)
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            if isinstance(value, str):
                count = value.count(old_value)
                if count:
                    obj[idx] = value.replace(old_value, new_value)
                    replacements += count
            else:
                replacements += replace_string_occurrences(value, old_value, new_value)
    return replacements


def update_index_lines(index_path: Path, changes_by_id: Dict[str, str]) -> Tuple[List[str], int, str]:
    lines, newline = read_jsonl_lines(index_path)
    changed_lines = 0
    changes_by_id_lower = {k.lower(): v for k, v in changes_by_id.items()}

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        thread_id = obj.get("id")
        if isinstance(thread_id, str) and thread_id.lower() in changes_by_id_lower:
            new_name = changes_by_id_lower[thread_id.lower()]
            if obj.get("thread_name") != new_name:
                obj["thread_name"] = new_name
                lines[idx] = json_dump_line(obj, newline)
                changed_lines += 1

    return lines, changed_lines, newline


def find_session_files(sessions_root: Path, thread_id: str) -> List[Path]:
    if not sessions_root.exists():
        return []

    pattern = f"*{thread_id}*.jsonl"
    matches = [path for path in sessions_root.rglob(pattern) if path.is_file()]
    return sorted(matches, key=lambda p: str(p).lower())


def make_thread_name_updated_event(thread_id: str, new_name: str, newline: str) -> str:
    obj = {
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {
            "type": "thread_name_updated",
            "thread_id": thread_id,
            "thread_name": new_name,
        },
    }
    return json_dump_line(obj, newline)


def insert_thread_name_event_line(lines: List[str], newline: str, thread_id: str, new_name: str) -> None:
    """Insert a modern thread_name_updated event near the top of a rollout file.

    Codex rollout files that contain a thread title usually store it as an
    event_msg with payload.type=thread_name_updated. If the file does not yet
    contain any thread_name key, adding this event immediately after the leading
    session_meta line keeps the title close to the rest of the file metadata
    without disturbing the transcript itself.
    """
    event_line = make_thread_name_updated_event(thread_id, new_name, newline)
    insert_at = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "session_meta":
            insert_at = idx + 1
        break
    lines.insert(insert_at, event_line)


def update_session_lines(session_path: Path, thread_id: str, old_name: str, new_name: str) -> Tuple[List[str], int, int, int, str]:
    """Update a matching session file.

    Prefer structured thread_name keys when present. If a rollout file has no
    such key, replace the old title wherever it appears inside parsed JSON
    string values and also add a thread_name_updated metadata event. This keeps
    the file valid JSONL and avoids raw text replacement.
    """
    lines, newline = read_jsonl_lines(session_path)
    thread_name_replacements = 0
    fallback_title_replacements = 0
    inserted_thread_name_events = 0
    had_thread_name_key = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        line_thread_replacements = 0
        if contains_thread_name(obj):
            had_thread_name_key = True
            line_thread_replacements = replace_thread_name_values(obj, new_name)

        line_fallback_replacements = 0
        if old_name and old_name != new_name:
            # Even if a thread_name key exists, the old title may also be cached
            # elsewhere in the same rollout file. Replace both, but safely inside
            # JSON string values rather than with a blind raw-file replacement.
            line_fallback_replacements = replace_string_occurrences(obj, old_name, new_name)

        if line_thread_replacements or line_fallback_replacements:
            lines[idx] = json_dump_line(obj, newline)
            thread_name_replacements += line_thread_replacements
            fallback_title_replacements += line_fallback_replacements

    if not had_thread_name_key and thread_id and new_name:
        insert_thread_name_event_line(lines, newline, thread_id, new_name)
        inserted_thread_name_events = 1

    return lines, thread_name_replacements, fallback_title_replacements, inserted_thread_name_events, newline


def write_lines_atomic(path: Path, lines: Iterable[str]) -> None:
    path = path.resolve()
    temp_dir = path.parent
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(temp_dir), text=True)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        os.replace(str(temp_path), str(path))
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def backup_file(path: Path, backup_root: Path, codex_root: Path) -> Path:
    path = path.resolve()
    codex_root = codex_root.resolve()
    try:
        relative = path.relative_to(codex_root)
    except ValueError:
        relative = Path(path.name)
    destination = backup_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


# ---------------------------------------------------------------------------
# Search logic
# ---------------------------------------------------------------------------


def extract_thread_id_from_filename(path: Path) -> str:
    match = UUID_RE.search(path.name)
    return match.group(0).lower() if match else ""


def extract_text_from_content(value: Any) -> str:
    """Extract readable text from Codex message content structures."""
    parts: List[str] = []

    def walk(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            parts.append(item)
            return
        if isinstance(item, list):
            for child in item:
                walk(child)
            return
        if isinstance(item, dict):
            # Prefer explicit text fields. This catches Codex input_text blocks.
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif text is not None:
                walk(text)

            # Some messages use nested content arrays.
            nested_content = item.get("content")
            if nested_content is not None and nested_content is not value:
                walk(nested_content)

            # Fallback for event-like structures. This is intentionally narrow so
            # session metadata does not pollute chat search results.
            message = item.get("message")
            if isinstance(message, str):
                parts.append(message)
            elif isinstance(message, (list, dict)):
                walk(message)

    walk(value)
    return "\n".join(part for part in parts if part)


def extract_role_messages(obj: Any, selected_roles: Sequence[str]) -> List[Tuple[str, str]]:
    """Return (role, text) pairs for message records in one JSONL object."""
    if not isinstance(obj, dict):
        return []

    selected = {role.lower() for role in selected_roles}
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []

    # Codex session files store the important searchable messages as
    # {"type":"response_item","payload":{"type":"message","role":"user",...}}
    # or role="developer". Search this canonical structure first.
    role = payload.get("role")
    payload_type = payload.get("type")
    if isinstance(role, str) and role.lower() in selected and payload_type == "message":
        text = extract_text_from_content(payload.get("content"))
        return [(role.lower(), text)] if text else []

    return []


def count_case_insensitive(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    return haystack.lower().count(needle.lower())


def make_snippet(text: str, needle: str, radius: int = 120) -> str:
    preview_text = display_snippet_text(text, needle)
    collapsed = re.sub(r"\s+", " ", preview_text).strip()
    if not collapsed:
        return ""
    index = collapsed.lower().find(needle.lower())
    if index < 0:
        return collapsed[: radius * 2] + ("..." if len(collapsed) > radius * 2 else "")

    start = max(0, index - radius)
    end = min(len(collapsed), index + len(needle) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(collapsed) else ""
    return prefix + collapsed[start:end] + suffix


def search_session_file(
    session_file: Path,
    query: str,
    roles: Sequence[str],
    index_records: Dict[str, ThreadRecord],
) -> Optional[SearchResult]:
    thread_id = extract_thread_id_from_filename(session_file)
    record = index_records.get(thread_id.lower()) if thread_id else None
    title = record.old_name if record else ""
    updated_at = record.updated_at if record else ""

    result = SearchResult(
        thread_id=thread_id,
        title=title or session_file.stem,
        updated_at=updated_at,
        session_file=session_file,
        file_size=file_size_bytes(session_file),
    )

    try:
        with session_file.open("r", encoding="utf-8-sig") as f:
            for line_no, line in enumerate(f, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue

                if not result.thread_id:
                    possible_id = find_thread_id_in_object(obj)
                    if possible_id:
                        result.thread_id = possible_id.lower()
                        record = index_records.get(result.thread_id)
                        if record:
                            result.title = record.old_name
                            result.updated_at = record.updated_at

                for role, text in extract_role_messages(obj, roles):
                    match_count = count_case_insensitive(text, query)
                    if not match_count:
                        continue
                    result.total_matches += match_count
                    result.roles[role] += match_count
                    if len(result.hits) < 20:
                        result.hits.append(
                            SearchHit(
                                role=role,
                                line_number=line_no,
                                count=match_count,
                                snippet=make_snippet(text, query),
                            )
                        )
    except OSError:
        return None

    return result if result.total_matches else None


def find_thread_id_in_object(obj: Any) -> str:
    if isinstance(obj, dict):
        value = obj.get("id")
        if isinstance(value, str) and UUID_RE.fullmatch(value):
            return value
        for child in obj.values():
            found = find_thread_id_in_object(child)
            if found:
                return found
    elif isinstance(obj, list):
        for child in obj:
            found = find_thread_id_in_object(child)
            if found:
                return found
    return ""


# ---------------------------------------------------------------------------
# Chat reader logic
# ---------------------------------------------------------------------------


def format_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def token_usage_text(usage: Any) -> str:
    if not isinstance(usage, dict):
        return ""
    pieces: List[str] = []
    input_tokens = usage.get("input_tokens")
    cached_tokens = usage.get("cached_input_tokens")
    output_tokens = usage.get("output_tokens")
    reasoning_tokens = usage.get("reasoning_output_tokens")
    total_tokens = usage.get("total_tokens")
    if input_tokens is not None:
        text = f"input {format_number(input_tokens)}"
        if cached_tokens:
            text += f" ({format_number(cached_tokens)} cached)"
        pieces.append(text)
    if output_tokens is not None:
        pieces.append(f"output {format_number(output_tokens)}")
    if reasoning_tokens:
        pieces.append(f"reasoning {format_number(reasoning_tokens)}")
    if total_tokens is not None:
        pieces.append(f"total {format_number(total_tokens)}")
    return ", ".join(pieces)


def format_epoch_seconds(value: Any) -> str:
    try:
        dt = _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def format_used_percent(value: Any) -> str:
    try:
        number = float(value)
        # Codex logs have appeared with both 0..1 fractions and 0..100 percent values.
        percent = number * 100 if 0 <= number <= 1 else number
        return f"{percent:.1f}%"
    except Exception:
        return str(value)


def extract_credit_balance(payload: Dict[str, Any]) -> Optional[float]:
    rate_limits = payload.get("rate_limits")
    credits = payload.get("credits")
    if not isinstance(credits, dict) and isinstance(rate_limits, dict):
        credits = rate_limits.get("credits")
    if not isinstance(credits, dict):
        return None
    balance = credits.get("balance")
    if balance is None:
        return None
    try:
        return float(balance)
    except Exception:
        return None


def format_token_event(payload: Dict[str, Any], timestamp_value: str = "") -> Tuple[str, int, str, str, Optional[int], Optional[float]]:
    """Return token text, running token total, basic credits, verbose credits, rounded and raw credit balances."""
    info = payload.get("info")
    token_lines: List[str] = []
    credit_verbose_lines: List[str] = []
    if not isinstance(info, dict):
        info = {}

    last_usage = info.get("last_token_usage")
    total_usage = info.get("total_token_usage")
    last_text = token_usage_text(last_usage)
    total_text = token_usage_text(total_usage)
    if last_text:
        token_lines.append(f"Last usage: {last_text}")
    if total_text:
        token_lines.append(f"Running total: {total_text}")
    context_window = info.get("model_context_window")
    if context_window:
        token_lines.append(f"Model context window: {format_number(context_window)} tokens")

    rounded_balance: Optional[int] = None
    raw_credit_balance: Optional[float] = None
    credit_basic_text = ""
    rate_limits = payload.get("rate_limits")
    credits = payload.get("credits")
    if not isinstance(credits, dict) and isinstance(rate_limits, dict):
        credits = rate_limits.get("credits")
    if isinstance(credits, dict):
        balance = credits.get("balance")
        unlimited = credits.get("unlimited")
        has_credits = credits.get("has_credits")
        plan_type = credits.get("plan_type") or payload.get("plan_type")
        if not plan_type and isinstance(rate_limits, dict):
            plan_type = rate_limits.get("plan_type")
        credit_bits = []
        if balance is not None:
            credit_bits.append(f"balance {balance}")
            try:
                raw_credit_balance = float(balance)
                rounded_balance = int(round(raw_credit_balance))
            except Exception:
                raw_credit_balance = None
                rounded_balance = None
        if plan_type:
            credit_bits.append(f"plan {plan_type}")
        if unlimited is True:
            credit_bits.append("unlimited credits")
        elif unlimited is False and has_credits is not None:
            credit_bits.append("credits available" if has_credits else "no credits available")
        if credit_bits:
            credit_verbose_lines.append("Credit snapshot: " + ", ".join(credit_bits))

    if isinstance(rate_limits, dict):
        primary = rate_limits.get("primary")
        secondary = rate_limits.get("secondary")
        limit_lines = []
        for label, item in (("primary", primary), ("secondary", secondary)):
            if not isinstance(item, dict):
                continue
            bits = []
            used_percent = item.get("used_percent")
            if used_percent is not None:
                try:
                    bits.append(f"{format_used_percent(used_percent)} used")
                except Exception:
                    bits.append(f"{used_percent} used")
            window = item.get("window_minutes")
            if window is not None:
                bits.append(f"{window} minute window")
            resets_at = item.get("resets_at")
            if resets_at:
                bits.append(f"resets {format_epoch_seconds(resets_at)}")
            if bits:
                limit_lines.append(f"{label}: " + ", ".join(bits))
        if limit_lines:
            credit_verbose_lines.append("Rate limits: " + " | ".join(limit_lines))

    token_total = 0
    if isinstance(total_usage, dict):
        try:
            token_total = int(total_usage.get("total_tokens") or 0)
        except Exception:
            token_total = 0
    return "\n".join(token_lines), token_total, credit_basic_text, "\n".join(credit_verbose_lines), rounded_balance, raw_credit_balance

def trim_for_reader(text: str, limit: int = 12000) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[Trimmed in viewer: {format_number(len(text) - limit)} additional characters hidden.]"


def parse_json_arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def format_tool_call(payload: Dict[str, Any]) -> str:
    name = str(payload.get("name") or "tool")
    raw_args = parse_json_arguments(payload.get("arguments", payload.get("input", "")))

    if name == "shell_command" and isinstance(raw_args, dict):
        lines = ["Command"]
        command = raw_args.get("command")
        if command:
            lines.append(str(command))
        workdir = raw_args.get("workdir")
        if workdir:
            lines.append(f"\nWorking folder: {workdir}")
        timeout = raw_args.get("timeout_ms")
        if timeout:
            lines.append(f"Timeout: {timeout} ms")
        return trim_for_reader("\n".join(lines))

    if name == "apply_patch":
        patch_text = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False, indent=2)
        return trim_for_reader("Patch request\n" + str(patch_text))

    if isinstance(raw_args, (dict, list)):
        return trim_for_reader(f"{name}\n" + json.dumps(raw_args, ensure_ascii=False, indent=2))
    return trim_for_reader(f"{name}\n{raw_args}")


def format_tool_output(payload: Dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, str):
        parsed = parse_json_arguments(output)
        if isinstance(parsed, dict) and "output" in parsed:
            parts = []
            metadata = parsed.get("metadata")
            if isinstance(metadata, dict):
                exit_code = metadata.get("exit_code")
                duration = metadata.get("duration_seconds")
                meta_bits = []
                if exit_code is not None:
                    meta_bits.append(f"exit code {exit_code}")
                if duration is not None:
                    meta_bits.append(f"duration {duration}s")
                if meta_bits:
                    parts.append("Result: " + ", ".join(meta_bits))
            parts.append(str(parsed.get("output") or ""))
            return trim_for_reader("\n".join(part for part in parts if part))
        return trim_for_reader(output)
    if output is None:
        return "No readable output was recorded."
    return trim_for_reader(json.dumps(output, ensure_ascii=False, indent=2))



def is_instruction_context(text: str) -> bool:
    stripped = text.lstrip()
    return (
        stripped.startswith("# AGENTS.md instructions")
        or stripped.startswith("<permissions instructions>")
        or "<INSTRUCTIONS>" in stripped[:2000]
        or stripped.startswith("# System Instructions")
    )

def title_from_user_text(text: str) -> str:
    cleaned = clean_chat_text(text)
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^thread\s+title\s*:\s*(.+)$", stripped, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()[:120]
    return first_nonempty_line(cleaned, 120)


def summarize_session_file(session_file: Path, index_records: Dict[str, ThreadRecord]) -> ChatThread:
    thread_id = extract_thread_id_from_filename(session_file)
    record = index_records.get(thread_id.lower()) if thread_id else None
    title = record.old_name if record else ""
    updated_at = record.updated_at if record else ""
    first_user_title = ""
    meta_timestamp = ""

    try:
        with session_file.open("r", encoding="utf-8-sig") as f:
            for line_no, line in enumerate(f, 1):
                if line_no > 80 and title and updated_at:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not thread_id:
                    possible_id = find_thread_id_in_object(obj)
                    if possible_id:
                        thread_id = possible_id.lower()
                        record = index_records.get(thread_id)
                        if record:
                            title = record.old_name
                            updated_at = record.updated_at
                payload = obj.get("payload") if isinstance(obj, dict) else None
                if isinstance(payload, dict):
                    if not meta_timestamp and isinstance(payload.get("timestamp"), str):
                        meta_timestamp = payload.get("timestamp", "")
                    if obj.get("type") == "session_meta":
                        if not thread_id and isinstance(payload.get("id"), str):
                            thread_id = payload["id"].lower()
                        if not meta_timestamp and isinstance(payload.get("timestamp"), str):
                            meta_timestamp = payload["timestamp"]
                    if not first_user_title and payload.get("type") == "message" and payload.get("role") == "user":
                        first_user_title = title_from_user_text(extract_text_from_content(payload.get("content")))
                    if not first_user_title and payload.get("type") == "user_message":
                        first_user_title = title_from_user_text(str(payload.get("message") or ""))
    except OSError:
        pass

    return ChatThread(
        thread_id=thread_id,
        title=title or first_user_title or session_file.stem,
        updated_at=updated_at or meta_timestamp,
        session_file=session_file,
        file_size=file_size_bytes(session_file),
    )


def selected_reader_filter_names(app: "VSCodexThreadToolsApp") -> Dict[str, bool]:
    return {
        "user": app.read_user_var.get(),
        "assistant": app.read_assistant_var.get(),
        "assistant_final": app.read_assistant_final_var.get(),
        "developer": app.read_developer_var.get(),
        "tool_calls": app.read_tool_calls_var.get(),
        "tool_outputs": app.read_tool_outputs_var.get(),
        "reasoning": app.read_reasoning_var.get(),
        "events": app.read_events_var.get(),
        "tokens": app.read_tokens_var.get(),
        "credits": app.read_credits_var.get(),
        "credits_verbose": app.read_credits_verbose_var.get(),
    }


READER_FILTER_LABELS: List[Tuple[str, str]] = [
    ("user", "User"),
    ("assistant", "Assistant commentary"),
    ("assistant_final", "Assistant final answer"),
    ("developer", "Developer instructions"),
    ("tool_calls", "Tool calls"),
    ("tool_outputs", "Tool output"),
    ("reasoning", "Reasoning records"),
    ("events", "Status events"),
    ("tokens", "Token usage"),
    ("credits", "Credits"),
    ("credits_verbose", "Credits (verbose)"),
]


def all_reader_filters() -> Dict[str, bool]:
    return {key: True for key, _label in READER_FILTER_LABELS}


def detect_hidden_find_kinds(session_file: Path, query: str, visible_filters: Dict[str, bool]) -> set[str]:
    if not query:
        return set()
    query_lower = query.lower()
    hidden_kinds: set[str] = set()
    try:
        messages = parse_chat_messages(session_file, all_reader_filters())
    except Exception:
        return hidden_kinds
    for message in messages:
        if visible_filters.get(message.kind, False):
            continue
        haystacks = [message.text or ""] + [str(path) for path in message.images]
        if any(query_lower in haystack.lower() for haystack in haystacks):
            hidden_kinds.add(message.kind)
    return hidden_kinds


def parse_chat_messages(session_file: Path, filters: Dict[str, bool]) -> List[ChatMessage]:
    messages: List[ChatMessage] = []
    seen_user_or_agent: set[Tuple[str, str]] = set()
    last_credit_balance_raw: Optional[float] = None
    last_credit_balance_rounded: Optional[int] = None

    def add(timestamp_value: str, kind: str, label: str, text: str, line_no: int, phase: str = "", token_total: int = 0, images: Optional[List[str]] = None) -> None:
        text = trim_for_reader(text)
        if not text and images:
            text = "Image attachment"
        if not text:
            return
        messages.append(
            ChatMessage(
                timestamp=timestamp_value,
                kind=kind,
                label=label,
                text=text,
                line_number=line_no,
                phase=phase,
                token_total=token_total,
                images=images or [],
            )
        )

    try:
        with session_file.open("r", encoding="utf-8-sig") as f:
            for line_no, line in enumerate(f, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                timestamp_value = str(obj.get("timestamp") or "")
                top_type = obj.get("type")
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type")

                if top_type == "response_item" and payload_type == "message":
                    role = str(payload.get("role") or "").lower()
                    phase = str(payload.get("phase") or "")
                    assistant_is_final = role == "assistant" and phase in {"final", "final_answer"}
                    if role == "user" and not filters.get("user"):
                        continue
                    if role == "assistant" and assistant_is_final and not filters.get("assistant_final"):
                        continue
                    if role == "assistant" and not assistant_is_final and not filters.get("assistant"):
                        continue
                    if role == "developer" and not filters.get("developer"):
                        continue
                    if role not in {"user", "assistant", "developer"}:
                        continue
                    raw_text = extract_text_from_content(payload.get("content"))
                    images = extract_image_refs(payload, session_file)
                    instruction_context = is_instruction_context(raw_text)
                    if instruction_context:
                        if not filters.get("developer"):
                            continue
                        kind = "developer"
                        label = "Instruction context"
                        text = trim_for_reader(raw_text)
                    else:
                        if role == "assistant" and assistant_is_final:
                            kind = "assistant_final"
                            label = "Assistant final answer"
                        elif role == "assistant":
                            kind = "assistant"
                            label = "Assistant commentary"
                        else:
                            kind = role
                            label = friendly_role(role)
                        text = clean_chat_text(raw_text) if role in {"user", "assistant"} else trim_for_reader(raw_text)
                        if kind == "assistant_final":
                            text = tighten_assistant_final_text(text)

                    dedupe_key = (kind, re.sub(r"\s+", " ", text).strip())
                    if dedupe_key in seen_user_or_agent:
                        continue
                    seen_user_or_agent.add(dedupe_key)
                    add(timestamp_value, kind, label, text, line_no, phase=phase, images=images)
                    continue

                # Event user/agent messages are often duplicates of response_item messages.
                # They are useful as a fallback for older files, so include them only when not already seen.
                if top_type == "event_msg" and payload_type in {"user_message", "agent_message"}:
                    role = "user" if payload_type == "user_message" else "assistant"
                    phase = str(payload.get("phase") or "")
                    assistant_is_final = role == "assistant" and phase in {"final", "final_answer"}
                    if role == "user" and not filters.get("user"):
                        continue
                    if role == "assistant" and assistant_is_final and not filters.get("assistant_final"):
                        continue
                    if role == "assistant" and not assistant_is_final and not filters.get("assistant"):
                        continue
                    text = clean_chat_text(str(payload.get("message") or ""))
                    kind = "assistant_final" if assistant_is_final else role
                    if kind == "assistant_final":
                        text = tighten_assistant_final_text(text)
                    dedupe_key = (kind, re.sub(r"\s+", " ", text).strip())
                    if dedupe_key in seen_user_or_agent:
                        continue
                    seen_user_or_agent.add(dedupe_key)
                    label = "User" if role == "user" else ("Assistant final answer" if assistant_is_final else "Assistant commentary")
                    images = extract_image_refs(payload, session_file)
                    add(timestamp_value, kind, label, text, line_no, phase=phase, images=images)
                    continue

                if top_type == "response_item" and payload_type in {"function_call", "custom_tool_call"}:
                    if filters.get("tool_calls"):
                        add(timestamp_value, "tool_call", "Tool call", format_tool_call(payload), line_no)
                    continue

                if top_type == "response_item" and payload_type in {"function_call_output", "custom_tool_call_output"}:
                    if filters.get("tool_outputs"):
                        add(timestamp_value, "tool_output", "Tool output", format_tool_output(payload), line_no)
                    continue

                if top_type == "response_item" and payload_type == "reasoning":
                    if filters.get("reasoning"):
                        summary = payload.get("summary")
                        content = payload.get("content")
                        pieces: List[str] = []
                        if isinstance(summary, list):
                            pieces.extend(str(item) for item in summary if str(item).strip())
                        elif isinstance(summary, str) and summary.strip():
                            pieces.append(summary.strip())
                        if isinstance(content, str) and content.strip():
                            pieces.append(content.strip())
                        if not pieces and payload.get("encrypted_content"):
                            pieces.append("Reasoning was recorded in encrypted form, so no readable text is available.")
                        add(timestamp_value, "reasoning", "Reasoning", "\n".join(pieces), line_no)
                    continue

                if top_type == "event_msg" and payload_type == "token_count":
                    token_text, token_total, credit_basic_text, credit_verbose_text, rounded_balance, raw_balance = format_token_event(payload, timestamp_value)
                    if filters.get("tokens") and token_text:
                        add(timestamp_value, "tokens", "Token usage", token_text, line_no, token_total=token_total)
                    if filters.get("credits") and rounded_balance is not None and raw_balance is not None:
                        if last_credit_balance_rounded is None or rounded_balance != last_credit_balance_rounded:
                            previous = last_credit_balance_rounded if last_credit_balance_rounded is not None else rounded_balance
                            delta = rounded_balance - previous
                            delta_text = f"+{delta}" if delta > 0 else str(delta)
                            time_text = human_datetime(timestamp_value)
                            compact_credit_text = f"{time_text} Credits: {rounded_balance} Delta: {delta_text}" if time_text else f"Credits: {rounded_balance} Delta: {delta_text}"
                            add(timestamp_value, "credits", "Credits", compact_credit_text, line_no)
                            last_credit_balance_raw = raw_balance
                            last_credit_balance_rounded = rounded_balance
                    if filters.get("credits_verbose") and credit_verbose_text:
                        add(timestamp_value, "credits_verbose", "Credits (verbose)", credit_verbose_text, line_no)
                    continue

                if top_type == "event_msg" and filters.get("events"):
                    if payload_type == "task_started":
                        add(timestamp_value, "event", "Task started", "Codex started working on this turn.", line_no)
                    elif payload_type == "task_complete":
                        text = str(payload.get("last_agent_message") or "Codex completed this turn.")
                        add(timestamp_value, "event", "Task complete", clean_chat_text(text), line_no)
                    elif payload_type == "patch_apply_end":
                        success = payload.get("success")
                        stdout = str(payload.get("stdout") or "")
                        stderr = str(payload.get("stderr") or "")
                        text = ("Patch applied successfully." if success else "Patch application finished.")
                        if stdout.strip():
                            text += "\n" + stdout.strip()
                        if stderr.strip():
                            text += "\nErrors:\n" + stderr.strip()
                        add(timestamp_value, "event", "Patch result", text, line_no)
                    elif payload_type:
                        add(timestamp_value, "event", str(payload_type).replace("_", " ").title(), "Event recorded by Codex.", line_no)
    except OSError as exc:
        add("", "event", "Read error", f"Could not read session file:\n{exc}", 0)

    return messages


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class VSCodexThreadToolsApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1120x720")
        self.root.minsize(920, 560)

        self.config = load_settings()
        self.index_path_var = tk.StringVar(value=self._setting("paths", "index", str(default_index_path())))
        self.sessions_path_var = tk.StringVar(value=self._setting("paths", "sessions", str(default_sessions_path())))
        self.status_var = tk.StringVar(value="Choose a tool to begin.")

        self.search_query_var = tk.StringVar()
        self.search_user_var = tk.BooleanVar(value=self._setting_bool("search", "role_user", True))
        self.search_developer_var = tk.BooleanVar(value=self._setting_bool("search", "role_developer", True))

        self.apply_v11_reader_default_migration()
        self.read_user_var = tk.BooleanVar(value=self._setting_bool("reader", "show_user", True))
        self.read_assistant_var = tk.BooleanVar(value=self._setting_bool("reader", "show_assistant", False))
        self.read_assistant_final_var = tk.BooleanVar(value=self._setting_bool("reader", "show_assistant_final", True))
        self.read_developer_var = tk.BooleanVar(value=self._setting_bool("reader", "show_developer", False))
        self.read_tool_calls_var = tk.BooleanVar(value=self._setting_bool("reader", "show_tool_calls", False))
        self.read_tool_outputs_var = tk.BooleanVar(value=self._setting_bool("reader", "show_tool_outputs", False))
        self.read_reasoning_var = tk.BooleanVar(value=self._setting_bool("reader", "show_reasoning", False))
        self.read_events_var = tk.BooleanVar(value=self._setting_bool("reader", "show_events", False))
        self.read_tokens_var = tk.BooleanVar(value=self._setting_bool("reader", "show_tokens", False))
        self.read_credits_var = tk.BooleanVar(value=self._setting_bool("reader", "show_credits", True))
        self.read_credits_verbose_var = tk.BooleanVar(value=self._setting_bool("reader", "show_credits_verbose", False))

        self.records: List[ThreadRecord] = []
        self.rename_tree: Optional[ttk.Treeview] = None
        self.search_tree: Optional[ttk.Treeview] = None
        self.search_details: Optional[tk.Text] = None
        self.search_results: List[SearchResult] = []
        self.reader_tree: Optional[ttk.Treeview] = None
        self.reader_text: Optional[tk.Text] = None
        self.chat_threads: List[ChatThread] = []
        self.reader_initial_file: Optional[Path] = None
        self.reader_initial_line: Optional[int] = None
        self.search_sort_column = "updated"
        self.search_sort_reverse = True
        self.reader_sort_column = "updated"
        self.reader_sort_reverse = True
        self.search_reader_window: Optional["ChatReaderWindow"] = None
        self.edit_entry: Optional[tk.Entry] = None
        self.current_view = "menu"

        self.root.bind("<Control-s>", lambda event: self.save_changes() if self.current_view == "rename" else None)
        self.root.bind("<F5>", lambda event: self.load_index() if self.current_view == "rename" else None)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.show_menu()

    def apply_v11_reader_default_migration(self) -> None:
        """Apply the v11 reader default changes once for existing settings files."""
        try:
            if not self.config.has_section("reader"):
                return
            if self.config.get("reader", "defaults_migrated_v11", fallback="no") == "yes":
                return
            self.config.set("reader", "show_assistant", "no")
            self.config.set("reader", "show_tokens", "no")
            self.config.set("reader", "show_credits", "yes")
            self.config.set("reader", "show_credits_verbose", "no")
            self.config.set("reader", "defaults_migrated_v11", "yes")
            save_settings(self.config)
        except Exception:
            pass

    def _setting(self, section: str, option: str, fallback: str) -> str:
        try:
            return self.config.get(section, option, fallback=fallback)
        except Exception:
            return fallback

    def _setting_bool(self, section: str, option: str, fallback: bool) -> bool:
        try:
            return self.config.getboolean(section, option, fallback=fallback)
        except Exception:
            return fallback

    def persist_settings(self) -> None:
        if not self.config.has_section("paths"):
            self.config.add_section("paths")
        self.config.set("paths", "index", self.index_path_var.get())
        self.config.set("paths", "sessions", self.sessions_path_var.get())

        if not self.config.has_section("search"):
            self.config.add_section("search")
        self.config.set("search", "role_user", "yes" if self.search_user_var.get() else "no")
        self.config.set("search", "role_developer", "yes" if self.search_developer_var.get() else "no")

        if not self.config.has_section("reader"):
            self.config.add_section("reader")
        self.config.set("reader", "show_user", "yes" if self.read_user_var.get() else "no")
        self.config.set("reader", "show_assistant", "yes" if self.read_assistant_var.get() else "no")
        self.config.set("reader", "show_assistant_final", "yes" if self.read_assistant_final_var.get() else "no")
        self.config.set("reader", "show_developer", "yes" if self.read_developer_var.get() else "no")
        self.config.set("reader", "show_tool_calls", "yes" if self.read_tool_calls_var.get() else "no")
        self.config.set("reader", "show_tool_outputs", "yes" if self.read_tool_outputs_var.get() else "no")
        self.config.set("reader", "show_reasoning", "yes" if self.read_reasoning_var.get() else "no")
        self.config.set("reader", "show_events", "yes" if self.read_events_var.get() else "no")
        self.config.set("reader", "show_tokens", "yes" if self.read_tokens_var.get() else "no")
        self.config.set("reader", "show_credits", "yes" if self.read_credits_var.get() else "no")
        self.config.set("reader", "show_credits_verbose", "yes" if self.read_credits_verbose_var.get() else "no")
        self.config.set("reader", "defaults_migrated_v11", "yes")
        save_settings(self.config)

    def clear_root(self) -> None:
        self.cancel_edit()
        for child in self.root.winfo_children():
            # Keep independent Reader windows alive when switching the main window
            # between Menu/Rename/Search pages. Toplevels are still closed when the
            # root application closes.
            if isinstance(child, tk.Toplevel):
                continue
            child.destroy()
        self.rename_tree = None
        self.search_tree = None
        self.search_details = None
        self.reader_tree = None
        self.reader_text = None

    def show_menu(self) -> None:
        self.current_view = "menu"
        self.clear_root()
        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(outer, text=APP_NAME, font=("Segoe UI", 20, "bold")).pack(anchor=tk.W)
        ttk.Label(outer, text=COPYRIGHT_LINE).pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(outer, text="Rename Codex thread titles, search chat content, or read full chat transcripts.").pack(anchor=tk.W, pady=(8, 24))

        button_frame = ttk.Frame(outer)
        button_frame.pack(anchor=tk.W)
        ttk.Button(button_frame, text="Rename chat threads", command=self.show_rename).grid(row=0, column=0, sticky="ew", ipadx=24, ipady=12, pady=(0, 10))
        ttk.Button(button_frame, text="Search chat threads", command=self.show_search).grid(row=1, column=0, sticky="ew", ipadx=24, ipady=12, pady=(0, 10))
        ttk.Button(button_frame, text="Read chat threads", command=self.open_reader_window).grid(row=2, column=0, sticky="ew", ipadx=24, ipady=12)

        info = ttk.LabelFrame(outer, text="Current Codex paths", padding=10)
        info.pack(fill=tk.X, pady=(28, 0))
        info.columnconfigure(1, weight=1)
        ttk.Label(info, text="Index file").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(info, textvariable=self.index_path_var).grid(row=0, column=1, sticky=tk.EW, pady=3)
        ttk.Button(info, text="Browse...", command=self.browse_index).grid(row=0, column=2, padx=(8, 0), pady=3)
        ttk.Label(info, text="Sessions folder").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(info, textvariable=self.sessions_path_var).grid(row=1, column=1, sticky=tk.EW, pady=3)
        ttk.Button(info, text="Browse...", command=self.browse_sessions).grid(row=1, column=2, padx=(8, 0), pady=3)

        status = ttk.Label(outer, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status.pack(fill=tk.X, side=tk.BOTTOM)

    def build_paths_frame(self, parent: tk.Widget) -> ttk.LabelFrame:
        paths = ttk.LabelFrame(parent, text="Codex files", padding=10)
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="Index file").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(paths, textvariable=self.index_path_var).grid(row=0, column=1, sticky=tk.EW, pady=3)
        ttk.Button(paths, text="Browse...", command=self.browse_index).grid(row=0, column=2, padx=(8, 0), pady=3)

        ttk.Label(paths, text="Sessions folder").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(paths, textvariable=self.sessions_path_var).grid(row=1, column=1, sticky=tk.EW, pady=3)
        ttk.Button(paths, text="Browse...", command=self.browse_sessions).grid(row=1, column=2, padx=(8, 0), pady=3)
        return paths

    # ------------------------- rename page -------------------------

    def show_rename(self) -> None:
        self.current_view = "rename"
        self.clear_root()
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        ttk.Button(header, text="Back to menu", command=self.show_menu).pack(side=tk.LEFT)
        ttk.Label(header, text="Rename Codex chat threads", font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(outer, text=COPYRIGHT_LINE).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            outer,
            text="Startup warns if VS Code is open; saving renamed threads is blocked until VS Code is closed.",
        ).pack(anchor=tk.W, pady=(4, 12))

        self.build_paths_frame(outer).pack(fill=tk.X)

        controls = ttk.Frame(outer)
        controls.pack(fill=tk.X, pady=(10, 8))
        ttk.Button(controls, text="Load / Reload", command=self.load_index).pack(side=tk.LEFT)
        ttk.Button(controls, text="Save changes", command=self.save_changes).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="Reset unsaved edits", command=self.reset_unsaved).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(controls, text="Tip: double-click a cell in the New thread name column to edit it.").pack(side=tk.LEFT, padx=(16, 0))

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("old_name", "new_name")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        self.rename_tree = tree
        tree.heading("old_name", text="Current thread name")
        tree.heading("new_name", text="New thread name")
        tree.column("old_name", width=500, minwidth=220, stretch=True)
        tree.column("new_name", width=500, minwidth=220, stretch=True)
        tree.tag_configure("changed", background="#fff3bf")

        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        tree.bind("<Double-1>", self.begin_edit_cell)
        tree.bind("<F2>", self.begin_edit_selected)

        status = ttk.Label(outer, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status.pack(fill=tk.X, pady=(8, 0))

        if self.records:
            self.refresh_tree()
        else:
            try:
                if Path(self.index_path_var.get()).exists() and Path(self.sessions_path_var.get()).exists():
                    self.load_index(show_errors=False)
            except Exception:
                pass

    def browse_index(self) -> None:
        initial_path = Path(self.index_path_var.get() or str(default_index_path()))
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Select session_index.jsonl",
            filetypes=(("JSONL files", "*.jsonl"), ("All files", "*.*")),
            initialfile="session_index.jsonl",
            initialdir=str(initial_path.parent if initial_path.parent.exists() else Path.home()),
        )
        if selected:
            self.index_path_var.set(selected)
            guessed_sessions = Path(selected).parent / "sessions"
            if guessed_sessions.exists():
                self.sessions_path_var.set(str(guessed_sessions))
            self.persist_settings()

    def browse_sessions(self) -> None:
        initial_path = Path(self.sessions_path_var.get() or str(default_sessions_path()))
        selected = filedialog.askdirectory(
            parent=self.root,
            title="Select .codex sessions folder",
            initialdir=str(initial_path if initial_path.exists() else Path.home()),
        )
        if selected:
            self.sessions_path_var.set(selected)
            self.persist_settings()

    def load_index(self, show_errors: bool = True) -> None:
        index_path = Path(self.index_path_var.get()).expanduser()
        sessions_path = Path(self.sessions_path_var.get()).expanduser()

        if not index_path.exists():
            if show_errors:
                messagebox.showerror(APP_NAME, f"Index file not found:\n{index_path}", parent=self.root)
            return
        if not sessions_path.exists():
            if show_errors:
                messagebox.showerror(APP_NAME, f"Sessions folder not found:\n{sessions_path}", parent=self.root)
            return

        try:
            records, _lines, _newline = parse_index_file(index_path)
        except Exception as exc:
            if show_errors:
                messagebox.showerror(APP_NAME, f"Could not load index file:\n{exc}", parent=self.root)
            return

        self.records = records
        self.refresh_tree()
        self.status_var.set(f"Loaded {len(records)} thread records from {index_path}.")
        self.persist_settings()

    def refresh_tree(self) -> None:
        self.cancel_edit()
        tree = self.rename_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for idx, record in enumerate(self.records):
            tags = ("changed",) if record.changed else ()
            tree.insert("", tk.END, iid=str(idx), values=(record.old_name, record.new_name), tags=tags)

    def reset_unsaved(self) -> None:
        for record in self.records:
            record.new_name = record.old_name
        self.refresh_tree()
        self.status_var.set("Unsaved edits reset.")

    def begin_edit_selected(self, event: Optional[tk.Event] = None) -> None:
        tree = self.rename_tree
        if tree is None:
            return
        selection = tree.selection()
        if not selection:
            return
        self.begin_edit_item(selection[0])

    def begin_edit_cell(self, event: tk.Event) -> None:
        tree = self.rename_tree
        if tree is None:
            return
        region = tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        column = tree.identify_column(event.x)
        item = tree.identify_row(event.y)
        if column != "#2" or not item:
            return
        self.begin_edit_item(item)

    def begin_edit_item(self, item: str) -> None:
        tree = self.rename_tree
        if tree is None:
            return
        self.cancel_edit()
        bbox = tree.bbox(item, column="new_name")
        if not bbox:
            return

        x, y, width, height = bbox
        current_value = tree.set(item, "new_name")
        entry = tk.Entry(tree)
        entry.insert(0, current_value)
        entry.select_range(0, tk.END)
        entry.focus_set()
        entry.place(x=x, y=y, width=width, height=height)
        self.edit_entry = entry

        def commit(_event: Optional[tk.Event] = None) -> None:
            if self.edit_entry is None:
                return
            new_value = self.edit_entry.get()
            self.commit_edit(item, new_value)

        def cancel(_event: Optional[tk.Event] = None) -> None:
            self.cancel_edit()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", cancel)

    def commit_edit(self, item: str, new_value: str) -> None:
        tree = self.rename_tree
        if tree is None:
            return
        if self.edit_entry is not None:
            self.edit_entry.destroy()
            self.edit_entry = None

        try:
            idx = int(item)
        except ValueError:
            return
        if idx < 0 or idx >= len(self.records):
            return

        self.records[idx].new_name = new_value
        tree.set(item, "new_name", new_value)
        tree.item(item, tags=("changed",) if self.records[idx].changed else ())
        self.update_status_for_changes()

    def cancel_edit(self) -> None:
        if self.edit_entry is not None:
            self.edit_entry.destroy()
            self.edit_entry = None

    def update_status_for_changes(self) -> None:
        count = sum(1 for record in self.records if record.changed)
        self.status_var.set("1 unsaved change." if count == 1 else f"{count} unsaved changes.")

    def save_changes(self) -> None:
        self.cancel_edit()

        if not self.records:
            messagebox.showinfo(APP_NAME, "No records are loaded yet.", parent=self.root)
            return

        if not ensure_vscode_is_closed_for_save(self.root):
            self.status_var.set("Save cancelled because VS Code is still running.")
            return

        changed_records = [record for record in self.records if record.changed]
        if not changed_records:
            messagebox.showinfo(APP_NAME, "There are no changes to save.", parent=self.root)
            return

        if any(not record.new_name.strip() for record in changed_records):
            messagebox.showerror(
                APP_NAME,
                "Thread names cannot be blank. Fill in the New thread name cells before saving.",
                parent=self.root,
            )
            return

        index_path = Path(self.index_path_var.get()).expanduser()
        sessions_path = Path(self.sessions_path_var.get()).expanduser()
        codex_root = index_path.parent
        backup_root = codex_root / "backups" / APP_FOLDER_NAME / timestamp()

        try:
            save_result = self.prepare_and_write_changes(index_path, sessions_path, changed_records, backup_root, codex_root)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Save failed:\n{exc}", parent=self.root)
            self.status_var.set("Save failed. No further changes were attempted after the error.")
            return

        for record in changed_records:
            record.old_name = record.new_name
        self.refresh_tree()
        messagebox.showinfo(APP_NAME, save_result, parent=self.root)
        self.status_var.set("Save complete.")
        self.persist_settings()

    def prepare_and_write_changes(
        self,
        index_path: Path,
        sessions_path: Path,
        changed_records: List[ThreadRecord],
        backup_root: Path,
        codex_root: Path,
    ) -> str:
        changes_by_id: Dict[str, str] = {record.thread_id: record.new_name for record in changed_records}

        index_lines, index_changed_lines, _index_newline = update_index_lines(index_path, changes_by_id)
        if index_changed_lines == 0:
            raise RuntimeError(
                "The selected changes could not be matched back to session_index.jsonl. Reload the index file and try again."
            )

        session_write_plan: Dict[Path, Tuple[List[str], int, int, int]] = {}
        not_found: List[ThreadRecord] = []
        found_without_session_title_target: List[Path] = []
        matching_session_files = 0

        for record in changed_records:
            matches = find_session_files(sessions_path, record.thread_id)
            if not matches:
                not_found.append(record)
                continue

            matching_session_files += len(matches)
            for session_file in matches:
                new_lines, thread_name_replacements, fallback_replacements, inserted_events, _newline = update_session_lines(
                    session_file,
                    record.thread_id,
                    record.old_name,
                    record.new_name,
                )
                if thread_name_replacements == 0 and fallback_replacements == 0 and inserted_events == 0:
                    found_without_session_title_target.append(session_file)
                else:
                    session_write_plan[session_file] = (new_lines, thread_name_replacements, fallback_replacements, inserted_events)

        files_to_write = [index_path] + list(session_write_plan.keys())
        backup_root.mkdir(parents=True, exist_ok=True)
        for path in files_to_write:
            backup_file(path, backup_root, codex_root)

        for session_file, (new_lines, _thread_count, _fallback_count, _inserted_count) in session_write_plan.items():
            write_lines_atomic(session_file, new_lines)
        write_lines_atomic(index_path, index_lines)

        session_files_changed = len(session_write_plan)
        thread_name_replacements = sum(thread_count for _lines, thread_count, _fallback_count, _inserted_count in session_write_plan.values())
        fallback_replacements = sum(fallback_count for _lines, _thread_count, fallback_count, _inserted_count in session_write_plan.values())
        thread_name_files_changed = sum(1 for _lines, thread_count, _fallback_count, _inserted_count in session_write_plan.values() if thread_count)
        fallback_files_changed = sum(1 for _lines, _thread_count, fallback_count, _inserted_count in session_write_plan.values() if fallback_count)
        inserted_thread_name_events = sum(inserted_count for _lines, _thread_count, _fallback_count, inserted_count in session_write_plan.values())
        inserted_thread_name_files = sum(1 for _lines, _thread_count, _fallback_count, inserted_count in session_write_plan.values() if inserted_count)

        lines = [
            f"Saved {len(changed_records)} renamed thread(s).",
            f"Updated {index_changed_lines} line(s) in session_index.jsonl.",
            f"Found {matching_session_files} matching session file(s).",
            f"Updated {thread_name_replacements} existing thread_name value(s) across {thread_name_files_changed} session file(s).",
            f"Inserted {inserted_thread_name_events} thread_name_updated event(s) across {inserted_thread_name_files} session file(s).",
            f"Updated {fallback_replacements} old-title text occurrence(s) across {fallback_files_changed} session file(s).",
            f"Changed {session_files_changed} session file(s) in total.",
            "",
            f"Backups were saved here:\n{backup_root}",
        ]

        if not_found:
            lines.extend(["", "Session file not found for these ID(s), so only the index was updated for them:"])
            for record in not_found[:10]:
                lines.append(f"- {record.thread_id} ({record.old_name})")
            if len(not_found) > 10:
                lines.append(f"- ...and {len(not_found) - 10} more")

        if found_without_session_title_target:
            lines.extend(["", "These matching session file(s) could not be updated even after thread_name insertion and old-title fallback:"])
            for path in found_without_session_title_target[:10]:
                lines.append(f"- {path}")
            if len(found_without_session_title_target) > 10:
                lines.append(f"- ...and {len(found_without_session_title_target) - 10} more")

        return "\n".join(lines)

    # ------------------------- search page -------------------------

    def show_search(self) -> None:
        self.current_view = "search"
        self.clear_root()
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        ttk.Button(header, text="Back to menu", command=self.show_menu).pack(side=tk.LEFT)
        ttk.Label(header, text="Search Codex chat threads", font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(outer, text=COPYRIGHT_LINE).pack(anchor=tk.W, pady=(4, 0))
        self.build_paths_frame(outer).pack(fill=tk.X, pady=(10, 0))

        search_controls = ttk.Frame(outer)
        search_controls.pack(fill=tk.X, pady=(10, 8))
        ttk.Label(search_controls, text="Search text").pack(side=tk.LEFT, padx=(0, 8))
        query_entry = ttk.Entry(search_controls, textvariable=self.search_query_var)
        query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        query_entry.bind("<Return>", lambda event: self.run_search())
        ttk.Button(search_controls, text="Search", command=self.run_search).pack(side=tk.LEFT, padx=(8, 0))

        role_frame = ttk.Frame(outer)
        role_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(role_frame, text="Roles:").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(role_frame, text="user", variable=self.search_user_var, command=self.persist_settings).pack(side=tk.LEFT)
        ttk.Checkbutton(role_frame, text="developer", variable=self.search_developer_var, command=self.persist_settings).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(role_frame, text=f"Settings file: {settings_path()}").pack(side=tk.RIGHT)

        paned = ttk.Panedwindow(outer, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        results_frame = ttk.Frame(paned)
        details_frame = ttk.Frame(paned)
        paned.add(results_frame, weight=3)
        paned.add(details_frame, weight=2)

        columns = ("title", "matches", "roles", "updated", "filesize", "file")
        tree = ttk.Treeview(results_frame, columns=columns, show="headings", selectmode="browse")
        self.search_tree = tree
        tree.heading("title", text="Thread title", command=lambda: self.sort_search_results("title"))
        tree.heading("matches", text="Matches", command=lambda: self.sort_search_results("matches"))
        tree.heading("roles", text="Roles", command=lambda: self.sort_search_results("roles"))
        tree.heading("updated", text="Updated", command=lambda: self.sort_search_results("updated"))
        tree.heading("filesize", text="Filesize", command=lambda: self.sort_search_results("filesize"))
        tree.heading("file", text="Session file", command=lambda: self.sort_search_results("file"))
        tree.column("title", width=340, minwidth=180, stretch=True)
        tree.column("matches", width=80, minwidth=70, stretch=False, anchor=tk.E)
        tree.column("roles", width=140, minwidth=90, stretch=False)
        tree.column("updated", width=155, minwidth=120, stretch=False)
        tree.column("filesize", width=90, minwidth=80, stretch=False, anchor=tk.E)
        tree.column("file", width=420, minwidth=180, stretch=True)

        y_scroll = ttk.Scrollbar(results_frame, orient=tk.VERTICAL, command=tree.yview)
        x_scroll = ttk.Scrollbar(results_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)
        tree.bind("<<TreeviewSelect>>", self.show_selected_search_result)
        tree.bind("<Double-1>", lambda event: self.open_selected_search_in_reader())

        details_toolbar = ttk.Frame(details_frame)
        details_toolbar.pack(fill=tk.X)
        ttk.Label(details_toolbar, text="Match preview").pack(side=tk.LEFT)
        ttk.Button(details_toolbar, text="Open containing folder", command=self.open_selected_search_folder).pack(side=tk.RIGHT)
        ttk.Button(details_toolbar, text="Copy session path", command=self.copy_selected_search_path).pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(details_toolbar, text="Read selected chat", command=self.open_selected_search_in_reader).pack(side=tk.RIGHT, padx=(0, 8))

        text = tk.Text(details_frame, wrap=tk.WORD, height=8, selectbackground="#cde8ff", selectforeground="#000000")
        self.search_details = text
        details_scroll = ttk.Scrollbar(details_frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=details_scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(6, 0))
        details_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(6, 0))

        status = ttk.Label(outer, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status.pack(fill=tk.X, pady=(8, 0))
        query_entry.focus_set()

        self.populate_search_results()

    def sort_search_results(self, column: str, preserve_direction: bool = False) -> None:
        if not preserve_direction:
            if self.search_sort_column == column:
                self.search_sort_reverse = not self.search_sort_reverse
            else:
                self.search_sort_column = column
                self.search_sort_reverse = column in {"updated", "matches", "filesize"}

        def key(result: SearchResult) -> Any:
            if column == "title":
                return result.title.lower()
            if column == "matches":
                return result.total_matches
            if column == "roles":
                return friendly_roles_text(result.roles).lower()
            if column == "updated":
                return parse_codex_datetime(result.updated_at) or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
            if column == "filesize":
                return result.file_size
            if column == "file":
                return friendly_session_label(result.session_file).lower()
            return result.title.lower()

        self.search_results.sort(key=key, reverse=self.search_sort_reverse)
        self.populate_search_results()

    def selected_search_roles(self) -> List[str]:
        roles = []
        if self.search_user_var.get():
            roles.append("user")
        if self.search_developer_var.get():
            roles.append("developer")
        return roles

    def run_search(self) -> None:
        query = self.search_query_var.get().strip()
        roles = self.selected_search_roles()
        index_path = Path(self.index_path_var.get()).expanduser()
        sessions_path = Path(self.sessions_path_var.get()).expanduser()

        if not query:
            messagebox.showinfo(APP_NAME, "Enter text to search for.", parent=self.root)
            return
        if not roles:
            messagebox.showinfo(APP_NAME, "Select at least one role to search.", parent=self.root)
            return
        if not sessions_path.exists():
            messagebox.showerror(APP_NAME, f"Sessions folder not found:\n{sessions_path}", parent=self.root)
            return

        self.persist_settings()
        self.search_results = []
        self.populate_search_results()
        self.set_details_text("Searching...\n")
        self.root.configure(cursor="watch")
        self.root.update_idletasks()

        try:
            index_records = parse_index_map(index_path) if index_path.exists() else {}
            session_files = sorted(sessions_path.rglob("*.jsonl"), key=lambda p: str(p).lower())
            total_files = len(session_files)
            results: List[SearchResult] = []
            for idx, session_file in enumerate(session_files, 1):
                result = search_session_file(session_file, query, roles, index_records)
                if result is not None:
                    results.append(result)
                if idx == 1 or idx % 100 == 0 or idx == total_files:
                    self.status_var.set(f"Searched {idx} of {total_files} session file(s); found {len(results)} matching thread(s).")
                    self.root.update_idletasks()

            self.search_results = results
            self.sort_search_results(self.search_sort_column, preserve_direction=True)
            self.status_var.set(f"Search complete: {plural(len(results), 'matching thread')}, {plural(sum(r.total_matches for r in results), 'total match', 'total matches')}.")
            if not results:
                self.set_details_text("No matching chat threads found.\n")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Search failed:\n{exc}", parent=self.root)
            self.status_var.set("Search failed.")
        finally:
            self.root.configure(cursor="")

    def populate_search_results(self) -> None:
        tree = self.search_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for idx, result in enumerate(self.search_results):
            tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    result.title,
                    plural(result.total_matches, "match", "matches"),
                    friendly_roles_text(result.roles),
                    human_datetime(result.updated_at),
                    human_filesize(result.file_size),
                    friendly_session_label(result.session_file),
                ),
            )

    def selected_search_result(self) -> Optional[SearchResult]:
        tree = self.search_tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        try:
            idx = int(selection[0])
        except ValueError:
            return None
        if idx < 0 or idx >= len(self.search_results):
            return None
        return self.search_results[idx]

    def show_selected_search_result(self, event: Optional[tk.Event] = None) -> None:
        result = self.selected_search_result()
        if result is None:
            return
        lines = [
            result.title,
            f"Updated: {human_datetime(result.updated_at) or '(unknown)'}",
            f"Filesize: {human_filesize(result.file_size)}",
            f"Matches: {plural(result.total_matches, 'match', 'matches')}",
            f"Roles: {friendly_roles_text(result.roles) or '(none)'}",
            f"Session file: {friendly_session_label(result.session_file)}",
            f"Folder: {result.session_file.parent}",
            f"Thread ID: {result.thread_id or '(unknown)'}",
            "",
            "Match preview",
            "-------------",
        ]
        for hit in result.hits:
            if not hit.snippet:
                continue
            lines.append(format_hit_heading(hit))
            lines.append(hit.snippet)
            lines.append("")
        if len(result.hits) >= 20:
            lines.append("Only the first 20 matching message snippets are shown.")
        self.set_details_text("\n".join(lines))
        self.open_selected_search_in_reader(auto=True)

    def set_details_text(self, value: str) -> None:
        text = self.search_details
        if text is None:
            return
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert("1.0", value)
        text.configure(state=tk.NORMAL)

    def copy_selected_search_path(self) -> None:
        result = self.selected_search_result()
        if result is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(str(result.session_file))
        self.status_var.set("Session path copied to clipboard.")

    def open_selected_search_folder(self) -> None:
        result = self.selected_search_result()
        if result is None:
            return
        folder = result.session_file.parent
        try:
            open_folder_default(folder)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{exc}", parent=self.root)

    def open_selected_search_in_reader(self, auto: bool = False) -> None:
        result = self.selected_search_result()
        if result is None:
            return
        target_line = result.hits[0].line_number if result.hits else None
        self.open_reader_window(result.session_file, target_line, self.search_query_var.get().strip())

    def open_reader_window(self, session_file: Optional[Path] = None, target_line: Optional[int] = None, search_query: str = "") -> None:
        if self.search_reader_window is None or not self.search_reader_window.is_alive():
            self.search_reader_window = ChatReaderWindow(self, session_file=session_file, target_line=target_line, search_query=search_query)
        else:
            self.search_reader_window.focus(session_file=session_file, target_line=target_line, search_query=search_query)

    # ------------------------- chat reader page -------------------------

    def show_reader(self) -> None:
        self.current_view = "reader"
        self.clear_root()
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        ttk.Button(header, text="Back to menu", command=self.show_menu).pack(side=tk.LEFT)
        ttk.Label(header, text="Read Codex chat threads", font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(outer, text=COPYRIGHT_LINE).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            outer,
            text="Displays session JSONL files as a readable transcript. Use filters to hide tool chatter, developer instructions, and status events.",
        ).pack(anchor=tk.W, pady=(4, 10))

        self.build_paths_frame(outer).pack(fill=tk.X)

        controls = ttk.Frame(outer)
        controls.pack(fill=tk.X, pady=(10, 8))
        ttk.Button(controls, text="Load / Reload chats", command=self.load_chat_threads).pack(side=tk.LEFT)
        ttk.Button(controls, text="Open containing folder", command=self.open_selected_reader_folder).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="Copy session path", command=self.copy_selected_reader_path).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(controls, text="Tip: select a thread on the left to read it.").pack(side=tk.LEFT, padx=(16, 0))

        filter_frame = ttk.LabelFrame(outer, text="Show message types", padding=8)
        filter_frame.pack(fill=tk.X, pady=(0, 8))
        filter_vars = [
            ("User", self.read_user_var),
            ("Assistant commentary", self.read_assistant_var),
            ("Assistant final answer", self.read_assistant_final_var),
            ("Developer instructions", self.read_developer_var),
            ("Tool calls", self.read_tool_calls_var),
            ("Tool output", self.read_tool_outputs_var),
            ("Reasoning records", self.read_reasoning_var),
            ("Status events", self.read_events_var),
            ("Token usage", self.read_tokens_var),
            ("Credits", self.read_credits_var),
            ("Credits (verbose)", self.read_credits_verbose_var),
        ]
        for idx, (label, var) in enumerate(filter_vars):
            ttk.Checkbutton(filter_frame, text=label, variable=var, command=self.on_reader_filter_changed).grid(
                row=idx // 4,
                column=idx % 4,
                sticky=tk.W,
                padx=(0, 18),
                pady=2,
            )

        paned = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        list_frame = ttk.Frame(paned)
        transcript_frame = ttk.Frame(paned)
        paned.add(list_frame, weight=1)
        paned.add(transcript_frame, weight=3)

        columns = ("title", "updated", "filesize", "file")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self.reader_tree = tree
        tree.heading("title", text="Thread title", command=lambda: self.sort_reader_threads("title"))
        tree.heading("updated", text="Updated", command=lambda: self.sort_reader_threads("updated"))
        tree.heading("filesize", text="Filesize", command=lambda: self.sort_reader_threads("filesize"))
        tree.heading("file", text="Session file", command=lambda: self.sort_reader_threads("file"))
        tree.column("title", width=340, minwidth=140, stretch=False)
        tree.column("updated", width=145, minwidth=120, stretch=False)
        tree.column("filesize", width=85, minwidth=75, stretch=False, anchor=tk.E)
        tree.column("file", width=155, minwidth=80, stretch=False)
        y_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        x_scroll = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        tree.bind("<<TreeviewSelect>>", self.show_selected_chat_thread)
        tree.bind("<Double-1>", self.show_selected_chat_thread)

        toolbar = ttk.Frame(transcript_frame)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="Chat transcript").pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Refresh transcript", command=self.show_selected_chat_thread).pack(side=tk.RIGHT)

        text = tk.Text(transcript_frame, wrap=tk.WORD, padx=14, pady=12, undo=False)
        self.reader_text = text
        self.configure_reader_text_tags(text)
        text_scroll = ttk.Scrollbar(transcript_frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=text_scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(6, 0))
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(6, 0))

        status = ttk.Label(outer, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status.pack(fill=tk.X, pady=(8, 0))

        if self.chat_threads:
            self.populate_reader_threads()
        else:
            self.load_chat_threads(show_errors=False)

    def configure_reader_text_tags(self, text: tk.Text) -> None:
        base_font = ("Segoe UI", 10)
        header_font = ("Segoe UI", 10, "bold")
        meta_font = ("Segoe UI", 8)
        text.configure(font=base_font, selectbackground="#8cc8ff", selectforeground="#000000", exportselection=True)
        text.tag_configure("title", font=("Segoe UI", 13, "bold"), spacing3=8)
        text.tag_configure("meta", font=meta_font, foreground="#666666", spacing3=4)
        text.tag_configure("message_header", font=header_font, spacing1=8, spacing3=2)
        text.tag_configure("user", background="#eaf3ff", lmargin1=12, lmargin2=12, rmargin=90, spacing1=4, spacing3=8)
        text.tag_configure("assistant", background="#eff8ef", lmargin1=90, lmargin2=90, rmargin=12, spacing1=4, spacing3=8)
        text.tag_configure("assistant_final", background="#eff8ef", lmargin1=12, lmargin2=12, rmargin=12, spacing1=4, spacing3=8)
        text.tag_configure("developer", background="#fff4cc", lmargin1=12, lmargin2=12, rmargin=12, spacing1=4, spacing3=8)
        text.tag_configure("tool_call", background="#f2f2f2", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("tool_output", background="#f7f7f7", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("reasoning", background="#f6ecff", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("event", background="#f9f9f9", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("tokens", background="#eef1ff", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("credits", background="#fff0d6", lmargin1=30, lmargin2=30, rmargin=30, spacing1=2, spacing3=2)
        text.tag_configure("credits_verbose", background="#fff0d6", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        try:
            text.tag_raise("sel")
        except Exception:
            pass

    def load_chat_threads(self, show_errors: bool = True) -> None:
        sessions_path = Path(self.sessions_path_var.get()).expanduser()
        index_path = Path(self.index_path_var.get()).expanduser()
        if not sessions_path.exists():
            if show_errors:
                messagebox.showerror(APP_NAME, f"Sessions folder not found:\n{sessions_path}", parent=self.root)
            return

        self.persist_settings()
        self.chat_threads = []
        self.populate_reader_threads()
        self.set_reader_text("Loading chat threads...\n")
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            index_records = parse_index_map(index_path) if index_path.exists() else {}
            session_files = sorted(sessions_path.rglob("*.jsonl"), key=lambda p: str(p).lower())
            threads: List[ChatThread] = []
            total_files = len(session_files)
            for idx, session_file in enumerate(session_files, 1):
                threads.append(summarize_session_file(session_file, index_records))
                if idx == 1 or idx % 100 == 0 or idx == total_files:
                    self.status_var.set(f"Loaded {idx} of {total_files} chat file(s).")
                    self.root.update_idletasks()
            self.chat_threads = threads
            self.sort_reader_threads(self.reader_sort_column, preserve_direction=True)
            self.status_var.set(f"Loaded {plural(len(threads), 'chat thread')}.")
            self.set_reader_text("Select a chat thread to read it.\n")
            if self.reader_initial_file is not None:
                self.select_reader_file(self.reader_initial_file)
                self.reader_initial_file = None
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load chat threads:\n{exc}", parent=self.root)
            self.status_var.set("Loading chat threads failed.")
        finally:
            self.root.configure(cursor="")

    def populate_reader_threads(self) -> None:
        tree = self.reader_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for idx, thread in enumerate(self.chat_threads):
            tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    thread.title,
                    human_datetime(thread.updated_at),
                    human_filesize(thread.file_size),
                    friendly_session_label(thread.session_file),
                ),
            )

    def select_reader_file(self, session_file: Path) -> None:
        tree = self.reader_tree
        if tree is None:
            return
        target = str(session_file).lower()
        for idx, thread in enumerate(self.chat_threads):
            if str(thread.session_file).lower() == target:
                item = str(idx)
                tree.selection_set(item)
                tree.focus(item)
                tree.see(item)
                self.show_selected_chat_thread()
                return

    def sort_reader_threads(self, column: str, preserve_direction: bool = False) -> None:
        if not preserve_direction:
            if self.reader_sort_column == column:
                self.reader_sort_reverse = not self.reader_sort_reverse
            else:
                self.reader_sort_column = column
                self.reader_sort_reverse = column in {"updated", "filesize"}

        def key(thread: ChatThread) -> Any:
            if column == "title":
                return thread.title.lower()
            if column == "updated":
                return parse_codex_datetime(thread.updated_at) or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
            if column == "filesize":
                return thread.file_size
            if column == "file":
                return friendly_session_label(thread.session_file).lower()
            return thread.title.lower()

        self.chat_threads.sort(key=key, reverse=self.reader_sort_reverse)
        self.populate_reader_threads()

    def selected_chat_thread(self) -> Optional[ChatThread]:
        tree = self.reader_tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        try:
            idx = int(selection[0])
        except ValueError:
            return None
        if idx < 0 or idx >= len(self.chat_threads):
            return None
        return self.chat_threads[idx]

    def on_reader_filter_changed(self) -> None:
        self.persist_settings()
        if self.current_view == "reader":
            self.show_selected_chat_thread()

    def show_selected_chat_thread(self, event: Optional[tk.Event] = None) -> None:
        thread = self.selected_chat_thread()
        if thread is None:
            return
        filters = selected_reader_filter_names(self)
        self.persist_settings()
        messages = parse_chat_messages(thread.session_file, filters)
        self.render_chat_messages(thread, messages)
        self.status_var.set(f"Showing {plural(len(messages), 'visible item')} from {friendly_session_label(thread.session_file)}.")

    def render_chat_messages(self, thread: ChatThread, messages: List[ChatMessage]) -> None:
        text = self.reader_text
        if text is None:
            return
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert(tk.END, thread.title + "\n", ("title",))
        meta_parts = []
        if thread.updated_at:
            meta_parts.append(f"Updated: {human_datetime(thread.updated_at)}")
        if thread.thread_id:
            meta_parts.append(f"Thread ID: {thread.thread_id}")
        meta_parts.append(f"File: {thread.session_file}")
        text.insert(tk.END, "  |  ".join(meta_parts) + "\n\n", ("meta",))

        if not messages:
            text.insert(tk.END, "No readable messages match the current filters.\n")
            text.configure(state=tk.NORMAL)
            return

        for message in messages:
            if message.kind == "credits":
                text.insert(tk.END, message.text + "\n", ("credits",))
                continue
            time_text = human_datetime(message.timestamp)
            line_info = f"line {message.line_number}" if message.line_number else ""
            header_bits = [message.label]
            if time_text:
                header_bits.append(time_text)
            if line_info:
                header_bits.append(line_info)
            text.insert(tk.END, "  ".join(header_bits) + "\n", ("message_header", message.kind))
            body = message.text.strip()
            text.insert(tk.END, body + "\n\n", (message.kind,))
        try:
            text.tag_raise("sel")
        except Exception:
            pass
        text.configure(state=tk.DISABLED)

    def set_reader_text(self, value: str) -> None:
        text = self.reader_text
        if text is None:
            return
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert("1.0", value)
        text.configure(state=tk.NORMAL)

    def copy_selected_reader_path(self) -> None:
        thread = self.selected_chat_thread()
        if thread is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(str(thread.session_file))
        self.status_var.set("Session path copied to clipboard.")

    def open_selected_reader_folder(self) -> None:
        thread = self.selected_chat_thread()
        if thread is None:
            return
        folder = thread.session_file.parent
        try:
            open_folder_default(folder)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{exc}", parent=self.root)


    # ------------------------- close -------------------------

    def on_close(self) -> None:
        changed_count = sum(1 for record in self.records if record.changed)
        if changed_count:
            close = messagebox.askyesno(
                APP_NAME,
                f"You have {changed_count} unsaved rename change(s). Close without saving?",
                parent=self.root,
            )
            if not close:
                return
        try:
            self.persist_settings()
        except Exception:
            pass
        self.root.destroy()



class ChatReaderWindow:
    def __init__(self, app: VSCodexThreadToolsApp, session_file: Optional[Path] = None, target_line: Optional[int] = None, search_query: str = "") -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title(f"{APP_NAME} - Read chat threads")
        self.window.geometry("1180x760")
        self.window.minsize(940, 560)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.status_var = tk.StringVar(value="Loading chat threads...")
        self.find_var = tk.StringVar(value=search_query or "")
        self.find_status_var = tk.StringVar(value="")
        self.reader_tree: Optional[ttk.Treeview] = None
        self.reader_text: Optional[tk.Text] = None
        self.chat_threads: List[ChatThread] = []
        self.sort_column = "updated"
        self.sort_reverse = True
        self.target_file = session_file
        self.target_line = target_line
        self.line_index_map: Dict[int, str] = {}
        self.image_tag_counter = 0
        self.local_file_tag_counter = 0
        self.local_file_tags: Dict[str, Path] = {}
        self.find_matches: List[str] = []
        self.find_index = -1
        self.filter_checkbuttons: Dict[str, tk.Checkbutton] = {}
        self._building = False

        self.build_ui()
        self.load_chat_threads(show_errors=False)
        self.window.lift()
        self.window.focus_force()

    def is_alive(self) -> bool:
        try:
            return bool(self.window.winfo_exists())
        except Exception:
            return False

    def close(self) -> None:
        if self.app.search_reader_window is self:
            self.app.search_reader_window = None
        self.window.destroy()

    def focus(self, session_file: Optional[Path] = None, target_line: Optional[int] = None, search_query: str = "") -> None:
        if session_file is not None:
            self.target_file = session_file
        self.target_line = target_line
        if search_query:
            self.find_var.set(search_query)
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        if self.target_file is not None:
            self.select_reader_file(self.target_file)

    def build_ui(self) -> None:
        outer = ttk.Frame(self.window, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Read chat threads", font=("Segoe UI", 15, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text=COPYRIGHT_LINE).pack(side=tk.RIGHT)

        ttk.Label(
            outer,
            text="Displays session JSONL files as a readable transcript. Search-result selections open this window at the matching message.",
        ).pack(anchor=tk.W, pady=(4, 10))

        controls = ttk.Frame(outer)
        controls.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(controls, text="Load / Reload chats", command=self.load_chat_threads).pack(side=tk.LEFT)
        ttk.Button(controls, text="Open containing folder", command=self.open_selected_reader_folder).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="Open on disk", command=self.open_selected_reader_file).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="Copy session path", command=self.copy_selected_reader_path).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(controls, text="Right-click a thread for more actions.").pack(side=tk.LEFT, padx=(16, 0))

        filter_frame = ttk.LabelFrame(outer, text="Show message types", padding=8)
        filter_frame.pack(fill=tk.X, pady=(0, 8))
        filter_vars = {
            "user": self.app.read_user_var,
            "assistant": self.app.read_assistant_var,
            "assistant_final": self.app.read_assistant_final_var,
            "developer": self.app.read_developer_var,
            "tool_calls": self.app.read_tool_calls_var,
            "tool_outputs": self.app.read_tool_outputs_var,
            "reasoning": self.app.read_reasoning_var,
            "events": self.app.read_events_var,
            "tokens": self.app.read_tokens_var,
            "credits": self.app.read_credits_var,
            "credits_verbose": self.app.read_credits_verbose_var,
        }
        for idx, (kind, label) in enumerate(READER_FILTER_LABELS):
            check = tk.Checkbutton(
                filter_frame,
                text=label,
                variable=filter_vars[kind],
                command=self.on_filter_changed,
                anchor=tk.W,
                highlightthickness=0,
                background=filter_frame.winfo_toplevel().cget("background"),
                activebackground="#fff2a8",
            )
            check.grid(
                row=idx // 5,
                column=idx % 5,
                sticky=tk.W,
                padx=(0, 18),
                pady=2,
            )
            self.filter_checkbuttons[kind] = check

        paned = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        list_frame = ttk.Frame(paned)
        transcript_frame = ttk.Frame(paned)
        paned.add(list_frame, weight=1)
        paned.add(transcript_frame, weight=3)

        columns = ("title", "updated", "filesize", "file")
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self.reader_tree = tree
        tree.heading("title", text="Thread title", command=lambda: self.sort_threads("title"))
        tree.heading("updated", text="Updated", command=lambda: self.sort_threads("updated"))
        tree.heading("filesize", text="Filesize", command=lambda: self.sort_threads("filesize"))
        tree.heading("file", text="Session file", command=lambda: self.sort_threads("file"))
        tree.column("title", width=360, minwidth=150, stretch=False)
        tree.column("updated", width=150, minwidth=120, stretch=False)
        tree.column("filesize", width=85, minwidth=75, stretch=False, anchor=tk.E)
        tree.column("file", width=155, minwidth=80, stretch=False)
        y_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        x_scroll = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        tree.bind("<<TreeviewSelect>>", self.show_selected_chat_thread)
        tree.bind("<Double-1>", self.show_selected_chat_thread)
        tree.bind("<Button-3>", self.show_thread_context_menu)

        toolbar = ttk.Frame(transcript_frame)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="Chat transcript").pack(side=tk.LEFT)
        ttk.Label(toolbar, text="Find").pack(side=tk.LEFT, padx=(18, 4))
        find_entry = ttk.Entry(toolbar, textvariable=self.find_var, width=26)
        find_entry.pack(side=tk.LEFT)
        find_entry.bind("<Return>", lambda event: self.find_next())
        ttk.Button(toolbar, text="Prev", command=self.find_previous).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="Next", command=self.find_next).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(toolbar, textvariable=self.find_status_var).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="Refresh transcript", command=self.show_selected_chat_thread).pack(side=tk.RIGHT)

        text = tk.Text(
            transcript_frame,
            wrap=tk.WORD,
            padx=14,
            pady=12,
            undo=False,
            selectbackground="#8cc8ff",
            selectforeground="#000000",
            exportselection=True,
        )
        self.reader_text = text
        self.configure_text_tags(text)
        text.bind("<Key>", self.block_text_edit)
        text.bind("<Control-a>", self.select_all_transcript_text)
        text.bind("<<Paste>>", lambda event: "break")
        text.bind("<<Cut>>", lambda event: "break")
        text.bind("<<Clear>>", lambda event: "break")
        text_scroll = ttk.Scrollbar(transcript_frame, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=text_scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(6, 0))
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(6, 0))

        status = ttk.Label(outer, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status.pack(fill=tk.X, pady=(8, 0))

    def configure_text_tags(self, text: tk.Text) -> None:
        base_font = ("Segoe UI", 10)
        header_font = ("Segoe UI", 10, "bold")
        meta_font = ("Segoe UI", 8)
        text.configure(font=base_font, selectbackground="#8cc8ff", selectforeground="#000000", exportselection=True)
        text.tag_configure("title", font=("Segoe UI", 13, "bold"), spacing3=8)
        text.tag_configure("meta", font=meta_font, foreground="#666666", spacing3=4)
        text.tag_configure("message_header", font=header_font, spacing1=8, spacing3=2)
        text.tag_configure("user", background="#eaf3ff", lmargin1=12, lmargin2=12, rmargin=90, spacing1=4, spacing3=8)
        text.tag_configure("assistant", background="#eff8ef", lmargin1=90, lmargin2=90, rmargin=12, spacing1=4, spacing3=8)
        text.tag_configure("assistant_final", background="#eff8ef", lmargin1=12, lmargin2=12, rmargin=12, spacing1=4, spacing3=8)
        text.tag_configure("developer", background="#fff4cc", lmargin1=12, lmargin2=12, rmargin=12, spacing1=4, spacing3=8)
        text.tag_configure("tool_call", background="#f2f2f2", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("tool_output", background="#f7f7f7", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("reasoning", background="#f6ecff", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("event", background="#f9f9f9", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("tokens", background="#eef1ff", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("credits", background="#fff0d6", lmargin1=30, lmargin2=30, rmargin=30, spacing1=2, spacing3=2)
        text.tag_configure("credits_verbose", background="#fff0d6", lmargin1=30, lmargin2=30, rmargin=30, spacing1=4, spacing3=8)
        text.tag_configure("target_message", background="#ffe7a3")
        text.tag_configure("image_link", foreground="#003399", background="#fff2a8", underline=True, font=("Segoe UI", 10, "bold"), spacing1=3, spacing3=3)
        text.tag_configure("local_file_link", foreground="#003399", underline=True, font=("Segoe UI", 10, "bold"))
        text.tag_configure("find_match", background="#fff2a8")
        text.tag_configure("find_current", background="#ffb347")
        try:
            text.tag_raise("sel")
        except Exception:
            pass

    def load_chat_threads(self, show_errors: bool = True) -> None:
        sessions_path = Path(self.app.sessions_path_var.get()).expanduser()
        index_path = Path(self.app.index_path_var.get()).expanduser()
        if not sessions_path.exists():
            if show_errors:
                messagebox.showerror(APP_NAME, f"Sessions folder not found:\n{sessions_path}", parent=self.window)
            return

        self.app.persist_settings()
        self.chat_threads = []
        self.populate_threads()
        self.set_reader_text("Loading chat threads...\n")
        self.window.configure(cursor="watch")
        self.window.update_idletasks()
        try:
            index_records = parse_index_map(index_path) if index_path.exists() else {}
            session_files = sorted(sessions_path.rglob("*.jsonl"), key=lambda p: str(p).lower())
            threads: List[ChatThread] = []
            total_files = len(session_files)
            for idx, session_file in enumerate(session_files, 1):
                threads.append(summarize_session_file(session_file, index_records))
                if idx == 1 or idx % 100 == 0 or idx == total_files:
                    self.status_var.set(f"Loaded {idx} of {total_files} chat file(s).")
                    self.window.update_idletasks()
            self.chat_threads = threads
            self.sort_threads(self.sort_column, preserve_direction=True)
            self.status_var.set(f"Loaded {plural(len(threads), 'chat thread')}.")
            self.set_reader_text("Select a chat thread to read it.\n")
            if self.target_file is not None:
                self.select_reader_file(self.target_file)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load chat threads:\n{exc}", parent=self.window)
            self.status_var.set("Loading chat threads failed.")
        finally:
            self.window.configure(cursor="")

    def sort_threads(self, column: str, preserve_direction: bool = False) -> None:
        if not preserve_direction:
            if self.sort_column == column:
                self.sort_reverse = not self.sort_reverse
            else:
                self.sort_column = column
                self.sort_reverse = column in {"updated", "filesize"}

        selected_file = None
        current = self.selected_chat_thread()
        if current is not None:
            selected_file = current.session_file

        def key(thread: ChatThread) -> Any:
            if column == "title":
                return thread.title.lower()
            if column == "updated":
                return parse_codex_datetime(thread.updated_at) or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
            if column == "filesize":
                return thread.file_size
            if column == "file":
                return friendly_session_label(thread.session_file).lower()
            return thread.title.lower()

        self.chat_threads.sort(key=key, reverse=self.sort_reverse)
        self.populate_threads()
        if selected_file is not None:
            self.select_reader_file(selected_file)

    def populate_threads(self) -> None:
        tree = self.reader_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for idx, thread in enumerate(self.chat_threads):
            tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    thread.title,
                    human_datetime(thread.updated_at),
                    human_filesize(thread.file_size),
                    friendly_session_label(thread.session_file),
                ),
            )

    def selected_chat_thread(self) -> Optional[ChatThread]:
        tree = self.reader_tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        try:
            idx = int(selection[0])
        except ValueError:
            return None
        if idx < 0 or idx >= len(self.chat_threads):
            return None
        return self.chat_threads[idx]

    def select_reader_file(self, session_file: Path) -> None:
        tree = self.reader_tree
        if tree is None:
            return
        target = str(session_file).lower()
        for idx, thread in enumerate(self.chat_threads):
            if str(thread.session_file).lower() == target:
                item = str(idx)
                tree.selection_set(item)
                tree.focus(item)
                tree.see(item)
                self.show_selected_chat_thread()
                return

    def on_filter_changed(self) -> None:
        self.app.persist_settings()
        self.show_selected_chat_thread()
        self.update_hidden_find_filter_highlights()

    def show_selected_chat_thread(self, event: Optional[tk.Event] = None) -> None:
        thread = self.selected_chat_thread()
        if thread is None:
            return
        filters = selected_reader_filter_names(self.app)
        self.app.persist_settings()
        messages = parse_chat_messages(thread.session_file, filters)
        self.render_chat_messages(thread, messages)
        self.status_var.set(f"Showing {plural(len(messages), 'visible item')} from {friendly_session_label(thread.session_file)}.")

    def render_chat_messages(self, thread: ChatThread, messages: List[ChatMessage]) -> None:
        text = self.reader_text
        if text is None:
            return
        self.line_index_map = {}
        self.image_tag_counter = 0
        self.local_file_tag_counter = 0
        self.local_file_tags: Dict[str, Path] = {}
        self.find_matches = []
        self.find_index = -1
        self.find_status_var.set("")
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert(tk.END, thread.title + "\n", ("title",))
        meta_parts = []
        if thread.updated_at:
            meta_parts.append(f"Updated: {human_datetime(thread.updated_at)}")
        meta_parts.append(f"Filesize: {human_filesize(thread.file_size)}")
        if thread.thread_id:
            meta_parts.append(f"Thread ID: {thread.thread_id}")
        meta_parts.append(f"File: {thread.session_file}")
        text.insert(tk.END, "  |  ".join(meta_parts) + "\n\n", ("meta",))

        if not messages:
            text.insert(tk.END, "No readable messages match the current filters.\n")
            text.configure(state=tk.NORMAL)
            return

        target_index = None
        for message in messages:
            start_index = text.index(tk.END)
            if message.line_number:
                self.line_index_map[message.line_number] = start_index
            body_tags = (message.kind,)
            if self.target_line is not None and message.line_number == self.target_line:
                body_tags = body_tags + ("target_message",)
                target_index = start_index

            if message.kind == "credits":
                text.insert(tk.END, message.text.strip() + "\n", body_tags)
                continue

            time_text = human_datetime(message.timestamp)
            line_info = f"line {message.line_number}" if message.line_number else ""
            header_bits = [message.label]
            if time_text:
                header_bits.append(time_text)
            if line_info:
                header_bits.append(line_info)
            message_tags = ("message_header", message.kind)
            if self.target_line is not None and message.line_number == self.target_line:
                message_tags = message_tags + ("target_message",)
            text.insert(tk.END, "  ".join(header_bits) + "\n", message_tags)
            self.insert_text_with_local_links(text, message.text.strip(), body_tags)
            text.insert(tk.END, "\n", body_tags)
            if message.images:
                for image_no, image_path in enumerate(message.images, 1):
                    self.image_tag_counter += 1
                    tag_name = f"image_link_{self.image_tag_counter}"
                    label = f"[IMAGE] Open image {image_no}: {Path(image_path).name or image_path}"
                    text.insert(tk.END, label + "\n", ("image_link", tag_name))
                    text.tag_bind(tag_name, "<Button-1>", lambda event, p=image_path: self.open_image(p))
                    text.tag_bind(tag_name, "<Enter>", lambda event: text.configure(cursor="hand2"))
                    text.tag_bind(tag_name, "<Leave>", lambda event: text.configure(cursor=""))
            text.insert(tk.END, "\n")

        try:
            text.tag_raise("sel")
        except Exception:
            pass
        text.configure(state=tk.NORMAL)
        if target_index is not None:
            text.see(target_index)
        self.run_find(reset=True, quiet=True)
        self.update_hidden_find_filter_highlights()

    def set_reader_text(self, value: str) -> None:
        text = self.reader_text
        if text is None:
            return
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert("1.0", value)
        text.configure(state=tk.NORMAL)

    def show_thread_context_menu(self, event: tk.Event) -> None:
        tree = self.reader_tree
        if tree is None:
            return
        row = tree.identify_row(event.y)
        if row:
            tree.selection_set(row)
            tree.focus(row)
        menu = tk.Menu(self.window, tearoff=False)
        menu.add_command(label="Rename", command=self.rename_selected_thread)
        menu.add_separator()
        menu.add_command(label="Open on disk", command=self.open_selected_reader_file)
        menu.add_command(label="Open containing folder", command=self.open_selected_reader_folder)
        menu.add_command(label="Copy session path", command=self.copy_selected_reader_path)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def copy_selected_reader_path(self) -> None:
        thread = self.selected_chat_thread()
        if thread is None:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(str(thread.session_file))
        self.status_var.set("Session path copied to clipboard.")

    def open_selected_reader_folder(self) -> None:
        thread = self.selected_chat_thread()
        if thread is None:
            return
        try:
            open_folder_default(thread.session_file.parent)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{exc}", parent=self.window)

    def open_selected_reader_file(self) -> None:
        thread = self.selected_chat_thread()
        if thread is None:
            return
        try:
            open_path_default(thread.session_file)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open file:\n{exc}", parent=self.window)

    def open_image(self, image_path: str) -> None:
        try:
            open_path_default(Path(image_path))
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open image:\n{image_path}\n\n{exc}", parent=self.window)


    def insert_text_with_local_links(self, text: tk.Text, content: str, base_tags: Tuple[str, ...]) -> None:
        for piece, link in split_text_with_local_file_links(content):
            if link is None:
                text.insert(tk.END, piece, base_tags)
                continue
            self.local_file_tag_counter += 1
            tag_name = f"local_file_link_{self.local_file_tag_counter}"
            self.local_file_tags[tag_name] = link.path
            text.insert(tk.END, piece, base_tags + ("local_file_link", tag_name))
            text.tag_bind(tag_name, "<Button-1>", lambda event, p=link.path: self.open_local_file(p))
            text.tag_bind(tag_name, "<Button-3>", lambda event, p=link.path: self.show_local_file_context_menu(event, p))
            text.tag_bind(tag_name, "<Enter>", lambda event: text.configure(cursor="hand2"))
            text.tag_bind(tag_name, "<Leave>", lambda event: text.configure(cursor=""))

    def show_local_file_context_menu(self, event: tk.Event, path: Path) -> str:
        menu = tk.Menu(self.window, tearoff=False)
        menu.add_command(label="Open", command=lambda p=path: self.open_local_file(p))
        menu.add_command(label="Explore directory", command=lambda p=path: self.explore_local_file_directory(p))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def open_local_file(self, path: Path) -> None:
        try:
            open_path_default(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open local file:\n{path}\n\n{exc}", parent=self.window)

    def explore_local_file_directory(self, path: Path) -> None:
        try:
            open_folder_default(path.parent)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open folder:\n{path.parent}\n\n{exc}", parent=self.window)

    def block_text_edit(self, event: tk.Event) -> Optional[str]:
        # Keep the transcript copyable/selectable, but stop accidental edits.
        if event.state & 0x4 and str(event.keysym).lower() in {"c", "a"}:
            return None
        navigation_keys = {"Left", "Right", "Up", "Down", "Prior", "Next", "Home", "End"}
        if event.keysym in navigation_keys:
            return None
        return "break"

    def select_all_transcript_text(self, event: tk.Event) -> str:
        text = self.reader_text
        if text is not None:
            text.tag_add("sel", "1.0", tk.END)
            text.mark_set(tk.INSERT, "1.0")
            text.see("1.0")
        return "break"

    def rename_selected_thread(self) -> None:
        thread = self.selected_chat_thread()
        if thread is None:
            return
        if not thread.thread_id:
            messagebox.showerror(APP_NAME, "This session does not have a thread ID, so it cannot be renamed safely.", parent=self.window)
            return

        dialog = tk.Toplevel(self.window)
        dialog.title("Rename thread")
        dialog.transient(self.window)
        dialog.grab_set()
        dialog.resizable(False, False)
        ttk.Label(dialog, text="New thread title").grid(row=0, column=0, sticky=tk.W, padx=12, pady=(12, 4))
        value_var = tk.StringVar(value=thread.title)
        entry = ttk.Entry(dialog, textvariable=value_var, width=70)
        entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, columnspan=2, sticky=tk.E, padx=12, pady=(0, 12))

        def cancel() -> None:
            dialog.destroy()

        def ok() -> None:
            new_title = value_var.get().strip()
            if not new_title:
                messagebox.showerror(APP_NAME, "Thread title cannot be blank.", parent=dialog)
                return
            if new_title == thread.title:
                dialog.destroy()
                return
            if not ensure_vscode_is_closed_for_save(dialog):
                self.status_var.set("Rename cancelled because VS Code is still running.")
                return
            index_path = Path(self.app.index_path_var.get()).expanduser()
            sessions_path = Path(self.app.sessions_path_var.get()).expanduser()
            codex_root = index_path.parent
            backup_root = codex_root / "backups" / APP_FOLDER_NAME / timestamp()
            record = ThreadRecord(
                line_number=0,
                thread_id=thread.thread_id,
                old_name=thread.title,
                new_name=new_title,
                updated_at=thread.updated_at,
            )
            try:
                result = self.app.prepare_and_write_changes(index_path, sessions_path, [record], backup_root, codex_root)
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Rename failed:\n{exc}", parent=dialog)
                return
            thread.title = new_title
            self.populate_threads()
            self.select_reader_file(thread.session_file)
            dialog.destroy()
            messagebox.showinfo(APP_NAME, result, parent=self.window)
            self.status_var.set("Thread renamed.")

        ttk.Button(buttons, text="Cancel", command=cancel).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(buttons, text="OK", command=ok).pack(side=tk.RIGHT)
        entry.focus_set()
        entry.selection_range(0, tk.END)
        dialog.bind("<Return>", lambda event: ok())
        dialog.bind("<Escape>", lambda event: cancel())
        dialog.update_idletasks()
        x = self.window.winfo_rootx() + (self.window.winfo_width() - dialog.winfo_width()) // 2
        y = self.window.winfo_rooty() + (self.window.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def update_hidden_find_filter_highlights(self) -> None:
        normal_bg = self.window.cget("background")
        highlight_bg = "#fff2a8"
        for check in self.filter_checkbuttons.values():
            try:
                check.configure(background=normal_bg, activebackground=normal_bg)
            except Exception:
                pass
        query = self.find_var.get().strip()
        thread = self.selected_chat_thread()
        if not query or thread is None:
            return
        hidden_kinds = detect_hidden_find_kinds(thread.session_file, query, selected_reader_filter_names(self.app))
        for kind in hidden_kinds:
            check = self.filter_checkbuttons.get(kind)
            if check is not None:
                try:
                    check.configure(background=highlight_bg, activebackground=highlight_bg)
                except Exception:
                    pass


    def run_find(self, reset: bool = True, quiet: bool = False) -> None:
        text = self.reader_text
        if text is None:
            return
        query = self.find_var.get()
        text.tag_remove("find_match", "1.0", tk.END)
        text.tag_remove("find_current", "1.0", tk.END)
        self.find_matches = []
        self.find_index = -1
        if not query:
            self.find_status_var.set("")
            self.update_hidden_find_filter_highlights()
            return
        start = "1.0"
        count = tk.IntVar(value=0)
        while True:
            index = text.search(query, start, stopindex=tk.END, nocase=True, count=count)
            if not index:
                break
            length = count.get()
            if length <= 0:
                break
            end = f"{index}+{length}c"
            text.tag_add("find_match", index, end)
            self.find_matches.append(index)
            start = end
        if not self.find_matches:
            self.find_status_var.set("No visible matches")
            self.update_hidden_find_filter_highlights()
            return
        if reset:
            self.find_index = 0
        else:
            self.find_index = max(0, min(self.find_index, len(self.find_matches) - 1))
        self.apply_current_find_match()
        self.update_hidden_find_filter_highlights()

    def apply_current_find_match(self) -> None:
        text = self.reader_text
        if text is None or not self.find_matches:
            return
        text.tag_remove("find_current", "1.0", tk.END)
        index = self.find_matches[self.find_index]
        query = self.find_var.get()
        end = f"{index}+{len(query)}c"
        text.tag_add("find_current", index, end)
        text.see(index)
        self.find_status_var.set(f"{self.find_index + 1} of {len(self.find_matches)}")

    def find_next(self) -> None:
        query = self.find_var.get()
        if not query:
            self.find_status_var.set("")
            return
        if not self.find_matches:
            self.run_find(reset=True)
            return
        self.find_index = (self.find_index + 1) % len(self.find_matches)
        self.apply_current_find_match()

    def find_previous(self) -> None:
        query = self.find_var.get()
        if not query:
            self.find_status_var.set("")
            return
        if not self.find_matches:
            self.run_find(reset=True)
            return
        self.find_index = (self.find_index - 1) % len(self.find_matches)
        self.apply_current_find_match()


def main() -> int:
    log_runtime(f"Starting {APP_NAME} {APP_VERSION}; executable={sys.executable}; python={sys.version.split()[0]}")
    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    root.geometry("1120x720")
    root.minsize(920, 560)

    # Show a visible shell immediately. This avoids the confusing black-console-only
    # symptom if the app stalls before the main UI is created.
    startup_frame = ttk.Frame(root, padding=24)
    startup_frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(startup_frame, text=f"Starting {APP_NAME}...", font=("Segoe UI", 16, "bold")).pack(anchor=tk.W)
    ttk.Label(startup_frame, text=COPYRIGHT_LINE).pack(anchor=tk.W, pady=(2, 0))
    ttk.Label(startup_frame, text=f"Runtime log: {runtime_log_path()}").pack(anchor=tk.W, pady=(8, 0))
    root.update()

    def continue_startup() -> None:
        try:
            log_runtime("Checking for VS Code processes")
            if not warn_vscode_running_on_startup(root):
                log_runtime("User chose to close during startup VS Code warning")
                root.destroy()
                return
            for child in root.winfo_children():
                child.destroy()
            log_runtime("Creating main application UI")
            VSCodexThreadToolsApp(root)
            log_runtime("Main application UI created")
        except BaseException as exc:
            log_path = write_crash_log(exc)
            log_runtime(f"Startup failed; crash_log={log_path}")
            messagebox.showerror(
                APP_NAME,
                "The app failed during startup. A crash log was written here:\n\n"
                f"{log_path}\n\n"
                "The runtime log is here:\n\n"
                f"{runtime_log_path()}",
                parent=root,
            )
            root.destroy()

    root.after(100, continue_startup)
    root.mainloop()
    log_runtime("Application exited")
    return 0


def run_with_crash_dialog() -> int:
    try:
        log_runtime("run_with_crash_dialog entered")
        return main()
    except SystemExit:
        raise
    except BaseException as exc:
        log_path = write_crash_log(exc)
        try:
            temp_root = tk.Tk()
            temp_root.withdraw()
            messagebox.showerror(
                APP_NAME,
                "The app failed to start. A crash log was written here:\n\n"
                f"{log_path}\n\n"
                "Please send me the contents of that file if you want me to diagnose it.",
                parent=temp_root,
            )
            temp_root.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(run_with_crash_dialog())
