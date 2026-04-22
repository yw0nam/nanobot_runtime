#!/usr/bin/env bash
# Sync nanobot_runtime source from this parent repo into a yuri-style
# workspace clone. Two modes:
#
#   (default) — require parent HEAD to be clean + pushed, then fast-forward
#               the workspace clone via `git pull`. This is the *normal* path.
#   --quick   — rsync src/ + tests/ into the clone without going through git.
#               For urgent smoke tests only. You MUST commit + push afterwards,
#               otherwise the next `git pull` inside the clone will conflict
#               with the files rsync put there.
#
# Set WORKSPACE_CLONE to point at a different clone; defaults to
# ../yuri/nanobot_runtime relative to this script.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
parent_dir="$(cd "$script_dir/.." && pwd)"
clone_dir="${WORKSPACE_CLONE:-$parent_dir/../yuri/nanobot_runtime}"

die() { echo "❌ $*" >&2; exit 1; }

[[ -d "$clone_dir" ]] || die "workspace clone not found at $clone_dir (override with WORKSPACE_CLONE)"

mode="${1:-normal}"

if [[ "$mode" == "--quick" ]]; then
    echo "⚠️  quick sync (rsync) — remember to commit+push parent afterwards"
    rsync -a --delete \
        --exclude '__pycache__' --exclude '.pytest_cache' \
        "$parent_dir/src/" "$clone_dir/src/"
    rsync -a --delete \
        --exclude '__pycache__' --exclude '.pytest_cache' \
        "$parent_dir/tests/" "$clone_dir/tests/"
    echo
    echo "✅ rsynced src/ and tests/ to $clone_dir"
    echo "→ parent status (commit + push these!):"
    git -C "$parent_dir" status --short
    exit 0
fi

if [[ "$mode" != "normal" && "$mode" != "" ]]; then
    die "unknown mode '$mode' (use --quick or omit)"
fi

# Normal path: parent must be clean and pushed, clone fast-forwards.
if ! git -C "$parent_dir" diff-index --quiet HEAD --; then
    echo "❌ parent has uncommitted changes — commit first, then re-run:"
    git -C "$parent_dir" status --short
    exit 1
fi

local_head="$(git -C "$parent_dir" rev-parse HEAD)"
upstream="$(git -C "$parent_dir" rev-parse --abbrev-ref '@{u}' 2>/dev/null || true)"
[[ -n "$upstream" ]] || die "parent has no upstream branch — set one with 'git push -u origin <branch>'"
remote_head="$(git -C "$parent_dir" rev-parse "$upstream")"
if [[ "$local_head" != "$remote_head" ]]; then
    die "parent HEAD not pushed (local=$local_head upstream=$remote_head) — push first"
fi

echo "→ fetching + fast-forwarding $clone_dir"
git -C "$clone_dir" fetch --quiet
git -C "$clone_dir" merge --ff-only
echo "✅ clone at $(git -C "$clone_dir" rev-parse HEAD)"
