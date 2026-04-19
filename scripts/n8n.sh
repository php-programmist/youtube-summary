#!/usr/bin/env bash
# n8n Workflow-as-Code sync wrapper.
#
# Uses the built-in n8n CLI inside the running container via `docker compose exec`.
# The `./workflows` directory is bind-mounted to `/workflows` inside the container,
# so no `docker cp` is needed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WF_HOST="$ROOT/workflows"
WF_CTR="/workflows"
EXPORT="$WF_CTR/.export"
EXEC=(docker compose exec -T n8n)

usage() {
    cat <<'EOF'
n8n workflow sync (CLI-based, works with local Docker n8n).

Usage:
  scripts/n8n.sh pull                  Export all workflows, normalize into workflows/
  scripts/n8n.sh push <slug>           Import workflows/<slug>.json
  scripts/n8n.sh push-all              Import every *.json from workflows/
  scripts/n8n.sh list                  List workflows currently in n8n
  scripts/n8n.sh activate <slug>       Activate workflow (uses id from JSON)
  scripts/n8n.sh activate-all          Activate every workflow in n8n
EOF
}

cmd_pull() {
    "${EXEC[@]}" sh -c "rm -rf $EXPORT && mkdir -p $EXPORT"
    "${EXEC[@]}" n8n export:workflow --backup --output="$EXPORT/"
    python3 "$ROOT/scripts/n8n_sync.py" normalize-dir "$WF_HOST/.export" "$WF_HOST"
    "${EXEC[@]}" rm -rf "$EXPORT"
}

apply_active_from_file() {
    # Reads id and active from a workflow JSON and syncs n8n state to match,
    # since `import:workflow` always deactivates on import.
    local file="$1"
    local id active
    id="$(python3 -c "import json; print(json.load(open('$file'))['id'])")"
    active="$(python3 -c "import json; print('true' if json.load(open('$file')).get('active') else 'false')")"
    "${EXEC[@]}" n8n update:workflow --id="$id" --active="$active" >/dev/null
    echo "  id=$id active=$active"
}

cmd_push() {
    local slug="${1:-}"
    [[ -n "$slug" ]] || { echo "Usage: scripts/n8n.sh push <slug>" >&2; exit 1; }
    local file="$WF_HOST/$slug.json"
    [[ -f "$file" ]] || { echo "No such file: $file" >&2; exit 1; }
    "${EXEC[@]}" n8n import:workflow --input="$WF_CTR/$slug.json"
    apply_active_from_file "$file"
}

cmd_push_all() {
    "${EXEC[@]}" n8n import:workflow --separate --input="$WF_CTR/"
    for f in "$WF_HOST"/*.json; do
        [[ -f "$f" ]] || continue
        apply_active_from_file "$f"
    done
}

cmd_list() {
    "${EXEC[@]}" n8n list:workflow
}

cmd_activate() {
    local slug="${1:-}"
    [[ -n "$slug" ]] || { echo "Usage: scripts/n8n.sh activate <slug>" >&2; exit 1; }
    local file="$WF_HOST/$slug.json"
    [[ -f "$file" ]] || { echo "No such file: $file" >&2; exit 1; }
    local id
    id="$(python3 -c "import json,sys; print(json.load(open('$file'))['id'])")"
    "${EXEC[@]}" n8n update:workflow --id="$id" --active=true
}

cmd_activate_all() {
    cmd_activate yt-summary-on-error
    cmd_activate yt-summary-per-user
    cmd_activate yt-summary-hourly
}

main() {
    local cmd="${1:-help}"
    shift || true
    case "$cmd" in
        pull)          cmd_pull "$@" ;;
        push)          cmd_push "$@" ;;
        push-all)      cmd_push_all "$@" ;;
        list)          cmd_list "$@" ;;
        activate)      cmd_activate "$@" ;;
        activate-all)  cmd_activate_all "$@" ;;
        help|-h|--help) usage ;;
        *) echo "unknown command: $cmd" >&2; usage >&2; exit 2 ;;
    esac
}

main "$@"
