#!/usr/bin/env bash
set -euo pipefail

# Render a user unit for the explicitly selected installed executable.
if [[ $# != 1 || "$1" != /* || ! -x "$1" || -d "$1" ]]; then
  echo "Usage: $0 /absolute/path/to/installed/info-desk" >&2
  exit 2
fi
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 - "$1" "$repo_root/deploy/information-desk.service" <<'PY'
import pathlib
import sys

path = sys.argv[1]
if any(c in path for c in "\n\r\0"):
    raise SystemExit("Executable path contains a prohibited control character")
# systemd parses quoted words itself and expands % specifiers even inside quotes.
escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
template = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
print(template.replace("@INFO_DESK_BIN@", f'"{escaped}"'), end="")
PY
