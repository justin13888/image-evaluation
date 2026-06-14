#!/usr/bin/env bash
#
# Deploy a benchmark report bundle to Cloudflare Pages via the wrangler CLI.
#
# Usage:
#   scripts/deploy_report.sh [REPORT_DIR]
#
# With no argument it deploys the newest bundle under results/. See the
# "Publishing the report" section of the README for the full one-time
# Cloudflare setup (account, Pages project, authentication).
#
# Configuration (env vars, all optional):
#   CF_PAGES_PROJECT   Cloudflare Pages project name   (default: image-evaluation)
#   CF_PAGES_BRANCH    Deploy branch (production)       (default: main)
#   CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID  for non-interactive auth
set -euo pipefail

PROJECT_NAME="${CF_PAGES_PROJECT:-image-evaluation}"
BRANCH="${CF_PAGES_BRANCH:-main}"
RESULTS_DIR="results"

err() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }

# --- 1. Select the report directory --------------------------------------
report_dir="${1:-}"
if [ -z "$report_dir" ]; then
    # Newest bundle that actually contains a report.html. `ls -dt` sorts by
    # mtime; scratch dirs (tmp_*) are skipped because they lack report.html.
    for d in $(ls -dt "$RESULTS_DIR"/*/ 2>/dev/null); do
        if [ -f "${d}report.html" ]; then
            report_dir="${d%/}"
            break
        fi
    done
fi

if [ -z "$report_dir" ]; then
    err "no report bundle found under '$RESULTS_DIR/'. Generate one first, e.g.:"
    err "  ./bench run --dataset kodak --sample 3 --quick"
    exit 1
fi

# --- 2. Pre-flight checks ------------------------------------------------
info "Selected report bundle: $report_dir"

if [ ! -d "$report_dir" ]; then
    err "report directory does not exist: $report_dir"
    exit 1
fi
if [ ! -f "$report_dir/report.html" ]; then
    err "no report.html in '$report_dir' — is this a report bundle?"
    exit 1
fi

if ! command -v wrangler >/dev/null 2>&1; then
    err "wrangler not found on PATH. Run 'mise install' to provision it."
    exit 1
fi

# Authentication: an API token (CI/headless) or an interactive `wrangler login`.
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] && ! wrangler whoami >/dev/null 2>&1; then
    err "not authenticated with Cloudflare. Either run:"
    err "  wrangler login"
    err "or set CLOUDFLARE_API_TOKEN (and CLOUDFLARE_ACCOUNT_ID) for non-interactive use."
    exit 1
fi

# Heads-up if the report was built from a dirty working tree.
manifest="$report_dir/manifest.json"
if [ -f "$manifest" ] && grep -q '"dirty"[[:space:]]*:[[:space:]]*true' "$manifest"; then
    warn "this report was built from a dirty git tree (manifest git.dirty = true)."
fi

# --- 3. Stage an index.html so the site root serves the report -----------
# Copy into a temp dir so the original bundle is never mutated.
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
cp -R "$report_dir"/. "$staging"/
cp "$staging/report.html" "$staging/index.html"

# --- 4. Deploy ----------------------------------------------------------
info "Deploying to Cloudflare Pages project '$PROJECT_NAME' (branch '$BRANCH')..."
wrangler pages deploy "$staging" \
    --project-name "$PROJECT_NAME" \
    --branch "$BRANCH" \
    --commit-message "report: $(basename "$report_dir")"

info "Done. The root URL serves report.html (staged as index.html)."
