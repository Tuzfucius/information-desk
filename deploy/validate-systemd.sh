#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${INFO_DESK_BIN:-}" ]]; then
  info_desk_bin="$INFO_DESK_BIN"
else
  info_desk_bin="$(command -v info-desk || true)"
fi
if [[ -z "$info_desk_bin" || ! -x "$info_desk_bin" ]]; then
  echo "info-desk executable not found; set INFO_DESK_BIN to the installed entry point." >&2
  exit 2
fi

temporary_home="$(mktemp -d)"
temporary_units="$(mktemp -d)"
trap 'rm -rf "$temporary_home" "$temporary_units"' EXIT
chmod 700 "$temporary_home"
"$repo_root/deploy/render-service.sh" "$info_desk_bin" >"$temporary_units/information-desk.service"
cp "$repo_root/deploy/information-desk.timer" "$temporary_units/information-desk.timer"
systemd-analyze verify \
  "$temporary_units/information-desk.service" \
  "$temporary_units/information-desk.timer"

run_isolated() {
  if systemctl --user show-environment >/dev/null 2>&1 \
    && command -v systemd-run >/dev/null 2>&1; then
    systemd-run --user --pipe --wait --collect \
      --unit="information-desk-acceptance-$RANDOM" \
      --property="Environment=INFO_DESK_HOME=$temporary_home" \
      --property=UMask=0077 "$info_desk_bin" "$@"
  else
    INFO_DESK_HOME="$temporary_home" "$info_desk_bin" "$@"
  fi
}

run_isolated init
doctor_json="$(run_isolated doctor)"
python3 -c 'import json, sys; result=json.loads(sys.stdin.read()); assert result["ok"]; assert result["schema_version"] == 3' <<<"$doctor_json"
echo "systemd unit syntax and isolated init/doctor checks passed"
