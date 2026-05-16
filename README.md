# VS-Codex Thread Tools v17

Dax Liniere 2026.

VS-Codex Thread Tools is a small Windows/Tkinter utility for working with local Codex VS Code extension thread files.

It can:

1. Rename Codex / VS Code thread titles by updating:
   - `%USERPROFILE%\.codex\session_index.jsonl`
   - matching files under `%USERPROFILE%\.codex\sessions\**\*.jsonl`
2. Search Codex chat thread contents by scanning session JSONL files.
3. Read Codex chat threads as a human-friendly transcript instead of raw JSONL.

SQLite support is intentionally not included yet. Current releases only edit `session_index.jsonl` and matching session JSONL files.

## License

MIT License. See `LICENSE`.

## Privacy warning

Your `.codex` directory can contain private chat transcripts, file paths, project names, prompts, command output, token records, and other local development details. Do not upload your real `.codex` files, screenshots, or generated logs to a public repository unless you have reviewed them carefully.

## Default Codex paths

The app uses the current user's home/profile directory by default. It does **not** hard-code `C:\Users\dax`.

Typical Windows paths:

```text
%USERPROFILE%\.codex\session_index.jsonl
%USERPROFILE%\.codex\sessions
```

The paths are editable in the app and can be selected using Browse buttons.

## Safety behavior

- On startup, the app checks whether VS Code-like processes are running.
- If VS Code is running, it warns you and offers Continue anyway.
- On Save, it checks again and blocks saving until VS Code is closed.
- Before writing files, it creates timestamped backups under:

```text
%USERPROFILE%\.codex\backups\VS-Codex Thread Tools\...
```

## Features

### Rename chat threads

- Loads `session_index.jsonl`.
- Shows the current title and editable new title.
- Updates changed titles in `session_index.jsonl`.
- Finds the matching rollout/session JSONL file by thread ID.
- Updates existing `thread_name` values when present.
- If no `thread_name` key is present, inserts a `thread_name_updated` metadata event after `session_meta`.
- Also performs a safe parsed-JSON fallback replacement for the old title string inside JSON string values.

### Search chat threads

- Searches readable message content across session JSONL files.
- Search results show thread title, match counts, role counts, updated time, filesize, and session file.
- Sortable column headers toggle ascending/descending order.
- Clicking a search result opens the matching chat in a separate reader window at the first matching message.
- If a match is hidden by reader filters, the relevant message-type checkbox label is highlighted.

### Read chat threads

- Opens session JSONL files as a chat-style transcript.
- Runs in a separate window so search results can remain visible.
- Shows user messages, assistant final answers, and compact credit-balance lines by default.
- Hides assistant commentary, developer/repo instructions, tool calls, tool output, reasoning records, status events, token usage, and verbose credits by default.
- Includes a basic Find box for the transcript pane.
- Right-click a thread for Rename, Open on disk, Open containing folder, and Copy session path.
- PNG/JPG image references are shown as prominent clickable `[IMAGE] Open image` links when present.
- Local file links in transcript text can be clicked; right-click them for Open or Explore directory.

## Settings and logs

Settings are remembered in:

```text
%APPDATA%\VS-Codex Thread Tools\settings.ini
```

Runtime log:

```text
%LOCALAPPDATA%\VS-Codex Thread Tools\runtime.log
```

Crash log:

```text
%LOCALAPPDATA%\VS-Codex Thread Tools\crash.log
```

## Running from source

Install standard Python for Windows from python.org. Tkinter is included with the normal Windows installer.

Then double-click:

```text
run_from_source.bat
```

The terminal prints Python and Tkinter checks before launching the GUI. It stays open after the app closes so errors are not lost.

## Building the EXE

Double-click:

```text
build_exe.bat
```

The executable will be created at:

```text
dist\VS-Codex Thread Tools.exe
```

For a console/debug build, run:

```text
build_debug_exe.bat
```

or:

```text
build_exe.bat debug
```

## Publishing to GitHub

Commit the source files, not your build output or private Codex data.

Recommended repository contents:

```text
vs_codex_thread_tools.py
README.md
LICENSE
.gitignore
requirements-build.txt
build_exe.bat
build_debug_exe.bat
run_from_source.bat
```

Do not commit:

```text
dist/
build/
.venv_build/
__pycache__/
*.pyc
*.spec
build_output.log
*.jsonl
*.sqlite
*.db
*.log
```

If you want to distribute the compiled EXE, attach it to a GitHub Release rather than committing it directly to the repository.

For GitHub's web upload flow, extract this zip first, then drag the extracted files into the new repository. GitHub will store the zip itself as a single file if you upload it directly.

## Files in this package

- `vs_codex_thread_tools.py`
- `run_from_source.bat`
- `build_exe.bat`
- `build_debug_exe.bat`
- `requirements-build.txt`
- `README.md`
- `LICENSE`
- `.gitignore`

## Changelog

### v17

- Switched the project license to MIT for open-source distribution.
- Changed the visible footer line to "Dax Liniere 2026."
- Prepared this zip layout for GitHub web upload after extraction.

### v16

- Prepared the project for public distribution.
- Removed the hard-coded `C:\Users\dax` fallback and now uses `Path.home()` / `%USERPROFILE%` defaults.
- Converted `README.txt` to GitHub-friendly `README.md`.
- Added `.gitignore` for build output, caches, logs, private Codex JSONL files, and SQLite databases.
- Added initial public-distribution project metadata.
- Corrected the visible footer line for public packaging.
- Left SQLite support out for a later release.

### v15

- If a matching rollout/session file has no existing `thread_name` key, renaming inserts a `thread_name_updated` metadata event after `session_meta`.
- The save summary reports inserted `thread_name_updated` events separately from existing `thread_name` replacements and old-title fallback replacements.

### v14

- Reader Find marks hidden matching message types by highlighting the checkbox label when the search text exists only in filtered-out content.
- Search-result clicks pass the searched text into the Reader Find box.
- Session rename falls back to replacing the old title string inside parsed JSON string values when a matching rollout/session file has no `thread_name` key.

### v13

- Compact Credits entries render as one line only: timestamp, rounded balance, and delta from the previous displayed rounded balance.
- Assistant final-answer text has tighter line-break handling.
- Switching the main window between Rename/Search/Menu no longer destroys separate Reader windows.
- Local Markdown file-link parsing is stricter so broken links cannot swallow the rest of a message into the path.

### v12

- Added Credits and Credits (verbose) as separate reader checkboxes.
- Added clickable local file links and local file-link context menus.
- Added Rename to the reader thread context menu.
- Added horizontal scrolling for reader thread lists.

### v11

- Separated Token usage and Credits visibility.
- Changed defaults: Assistant commentary off, Token usage off, Credits on.
- Made image links more prominent.

### v10

- Renamed the project to VS-Codex Thread Tools.
- Built executable name became `VS-Codex Thread Tools.exe`.
- Added Filesize columns, sortable column headers, separate chat reader windows, transcript Find, image links, and separate Assistant commentary/final-answer controls.

## Token and credit note

Codex session files may contain `token_count` events. These usually include rolling totals and a `last_token_usage` field. The file does not reliably name the exact operation that caused each token event, so VS-Codex Thread Tools displays token records as timestamped snapshots rather than claiming exact per-command attribution.

Some `token_count` events also include credit balance snapshots. Those snapshots can be useful, but the log does not reliably say "this operation used X credits". The compact Credits view rounds those balances to whole credits, shows a delta from the previous displayed rounded balance, and suppresses repeated unchanged displayed balances. Credits (verbose) keeps the fuller snapshot/rate-limit details. The tool does not pretend credit snapshots are exact per-operation costs.
