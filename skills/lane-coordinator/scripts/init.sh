#!/usr/bin/env bash
# Scaffold lane-coordinator state for a repo. Never overwrites existing files.
# Usage: bash init.sh [repo_path]
set -euo pipefail
SKILL="$(cd "$(dirname "$0")/.." && pwd)"
SD="$(python3 -I "$SKILL/scripts/state_dir.py" "${1:-$PWD}")"
SD="${SD#missing }"
PROJ="$(dirname "$SD")"
mkdir -p "$SD/sprints" "$PROJ/memory"

copy() { if [ -e "$2" ]; then echo "kept    $2"; else cp "$1" "$2"; echo "created $2"; fi; }
copy "$SKILL/templates/config.json"  "$SD/config.json"
copy "$SKILL/templates/routing.json" "$SD/routing.json"
copy "$SKILL/templates/review-patterns.md" "$PROJ/memory/review-patterns.md"
[ -e "$SD/ledger.jsonl" ] || { : > "$SD/ledger.jsonl"; echo "created $SD/ledger.jsonl"; }
python3 -I - "$SD/routing.json" <<'PY'
import datetime, json, sys
p = sys.argv[1]; d = json.load(open(p))
if d.get("updated") == "YYYY-MM-DD":
    d["updated"] = datetime.date.today().isoformat()
    open(p, "w").write(json.dumps(d, indent=2) + "\n")
PY

if ! grep -qs "review-patterns.md" "$PROJ/memory/MEMORY.md"; then
  echo "- [Review patterns](review-patterns.md) — recurring PR defects; link from every worker brief" >> "$PROJ/memory/MEMORY.md"
  echo "updated $PROJ/memory/MEMORY.md"
fi
echo "Next: edit $SD/config.json (integration_branch, queue_cmd, stacks)."
