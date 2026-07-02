#!/usr/bin/env bash
# Daily stale-work digest via gh CLI.
# Set GITHUB_USER to scan a specific account (defaults to authenticated gh user).
# Set GITHUB_ORG to limit results to one organization (e.g. kartenmacherei).
set -euo pipefail
exec python3 "$(dirname "$0")/github-stale-work.py" "$@"
