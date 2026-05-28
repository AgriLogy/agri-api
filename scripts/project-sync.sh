#!/usr/bin/env bash
#
# project-sync.sh — sync GitHub issues/PRs to the AgriLogy project board.
#
# https://github.com/orgs/AgriLogy/projects/1/views/1
#
# Adds an issue or PR to the project board, sets its Estimate (story
# points using Fibonacci: 1, 2, 3, 5, 8, 13, 21) and its Status.
#
# Usage:
#   project-sync.sh add <url> --estimate <fib> [--status <name>]
#   project-sync.sh status <url> --status <name>
#   project-sync.sh close <url>                       # sets Status=Done
#
# Examples:
#   ./scripts/project-sync.sh add https://github.com/AgriLogy/agri-api/issues/49 --estimate 5
#   ./scripts/project-sync.sh add https://github.com/AgriLogy/agri-api/pull/50 --estimate 5 --status "In review"
#   ./scripts/project-sync.sh close https://github.com/AgriLogy/agri-api/issues/49
#
# Fibonacci sizing convention (story points):
#   1  — trivial (one-line, one-file change)
#   2  — small focused (docs, a couple lines)
#   3  — typical bug fix
#   5  — medium feature (per-repo Phase 0 rename, a sibling extraction)
#   8  — large feature (Phase 1 cleanup, Phase 5a/5b ingest apps)
#   13 — epic (Phase 2 settings split, Phase 4b ORM mirror, Phase 5x bridge)
#   21 — monumental (Phase 4c mirroring 47 tables; agri-core extraction)

set -euo pipefail

ORG="AgriLogy"
PROJECT_NUMBER=1
PROJECT_ID="PVT_kwDODmc8xc4BH-Fv"

ESTIMATE_FIELD_ID="PVTF_lADODmc8xc4BH-Fvzg4kZns"
STATUS_FIELD_ID="PVTSSF_lADODmc8xc4BH-Fvzg4kZV8"

declare -A STATUS_OPTIONS=(
    ["Backlog"]="f75ad846"
    ["Ready"]="61e4505c"
    ["In progress"]="47fc9ee4"
    ["In review"]="df73e18b"
    ["Done"]="98236657"
)

# ---------------------------------------------------------------------------
# Resolve a URL (issue or PR) to its global node id.
# ---------------------------------------------------------------------------
resolve_node_id() {
    local url="$1"
    # URLs: https://github.com/<owner>/<repo>/(issues|pull)/<n>
    local owner repo type number
    owner=$(echo "$url" | sed -nE 's|https://github.com/([^/]+)/([^/]+)/(issues|pull)/([0-9]+).*|\1|p')
    repo=$(echo "$url"  | sed -nE 's|https://github.com/([^/]+)/([^/]+)/(issues|pull)/([0-9]+).*|\2|p')
    type=$(echo "$url"  | sed -nE 's|https://github.com/([^/]+)/([^/]+)/(issues|pull)/([0-9]+).*|\3|p')
    number=$(echo "$url" | sed -nE 's|https://github.com/([^/]+)/([^/]+)/(issues|pull)/([0-9]+).*|\4|p')
    if [[ -z "$number" ]]; then
        echo "could not parse URL: $url" >&2
        return 1
    fi

    if [[ "$type" == "issues" ]]; then
        gh api graphql -f query='
          query($owner:String!, $repo:String!, $n:Int!) {
            repository(owner:$owner, name:$repo) { issue(number:$n) { id } }
          }' \
          -f owner="$owner" -f repo="$repo" -F n="$number" \
          --jq '.data.repository.issue.id'
    else
        gh api graphql -f query='
          query($owner:String!, $repo:String!, $n:Int!) {
            repository(owner:$owner, name:$repo) { pullRequest(number:$n) { id } }
          }' \
          -f owner="$owner" -f repo="$repo" -F n="$number" \
          --jq '.data.repository.pullRequest.id'
    fi
}

# ---------------------------------------------------------------------------
# Add a content node to the project; returns the project ITEM id.
# Idempotent — re-adding an existing item returns its existing id.
# ---------------------------------------------------------------------------
add_to_project() {
    local content_id="$1"
    gh api graphql -f query='
      mutation($project:ID!, $content:ID!) {
        addProjectV2ItemById(input:{projectId:$project, contentId:$content}) {
          item { id }
        }
      }' \
      -f project="$PROJECT_ID" -f content="$content_id" \
      --jq '.data.addProjectV2ItemById.item.id'
}

set_estimate() {
    local item_id="$1" value="$2"
    gh api graphql -f query='
      mutation($project:ID!, $item:ID!, $field:ID!, $value:Float!) {
        updateProjectV2ItemFieldValue(input:{
          projectId:$project, itemId:$item, fieldId:$field,
          value:{number:$value}
        }) { projectV2Item { id } }
      }' \
      -f project="$PROJECT_ID" -f item="$item_id" \
      -f field="$ESTIMATE_FIELD_ID" -F value="$value" \
      --jq '.data.updateProjectV2ItemFieldValue.projectV2Item.id' >/dev/null
}

set_status() {
    local item_id="$1" status_name="$2"
    local option_id="${STATUS_OPTIONS[$status_name]:-}"
    if [[ -z "$option_id" ]]; then
        echo "unknown status: $status_name (one of: ${!STATUS_OPTIONS[*]})" >&2
        return 1
    fi
    gh api graphql -f query='
      mutation($project:ID!, $item:ID!, $field:ID!, $option:String!) {
        updateProjectV2ItemFieldValue(input:{
          projectId:$project, itemId:$item, fieldId:$field,
          value:{singleSelectOptionId:$option}
        }) { projectV2Item { id } }
      }' \
      -f project="$PROJECT_ID" -f item="$item_id" \
      -f field="$STATUS_FIELD_ID" -f option="$option_id" \
      --jq '.data.updateProjectV2ItemFieldValue.projectV2Item.id' >/dev/null
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------
cmd_add() {
    local url="$1"
    shift
    local estimate="" status=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --estimate) estimate="$2"; shift 2 ;;
            --status)   status="$2"; shift 2 ;;
            *) echo "unknown arg: $1" >&2; return 2 ;;
        esac
    done

    local content_id item_id
    content_id=$(resolve_node_id "$url")
    item_id=$(add_to_project "$content_id")
    echo "✓ added: item_id=$item_id"

    if [[ -n "$estimate" ]]; then
        set_estimate "$item_id" "$estimate"
        echo "  estimate=$estimate"
    fi
    if [[ -n "$status" ]]; then
        set_status "$item_id" "$status"
        echo "  status=$status"
    fi
}

cmd_status() {
    local url="$1" status="$2"
    local content_id item_id
    content_id=$(resolve_node_id "$url")
    item_id=$(add_to_project "$content_id")
    set_status "$item_id" "$status"
    echo "✓ $url status=$status"
}

cmd_close() {
    local url="$1"
    cmd_status "$url" "Done"
}

main() {
    local sub="${1:-}"
    case "$sub" in
        add)    shift; cmd_add "$@" ;;
        status) shift; cmd_status "$@" ;;
        close)  shift; cmd_close "$@" ;;
        -h|--help|"")
            sed -nE '/^#/p' "$0" | sed -E 's|^#||;s|^ ||' | head -40
            ;;
        *) echo "unknown subcommand: $sub" >&2; exit 2 ;;
    esac
}

main "$@"
