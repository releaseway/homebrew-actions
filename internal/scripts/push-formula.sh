#!/usr/bin/env bash
set -euo pipefail

branch="${TAP_BRANCH:-main}"
attempts="${PUSH_ATTEMPTS:-3}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

case "$attempts" in
  "" | *[!0-9]*)
    echo "PUSH_ATTEMPTS must be a positive integer" >&2
    exit 1
    ;;
esac

if [ "$attempts" -lt 1 ]; then
  echo "PUSH_ATTEMPTS must be a positive integer" >&2
  exit 1
fi

attempt=1
while [ "$attempt" -le "$attempts" ]; do
  git fetch origin "$branch:refs/remotes/origin/$branch"
  ruby "$script_dir/validate-formula-update.rb" "origin/${branch}"
  git rebase "origin/${branch}"
  if git push origin "HEAD:${branch}"; then
    exit 0
  fi

  if [ "$attempt" -eq "$attempts" ]; then
    echo "failed to push formula after ${attempts} attempts" >&2
    exit 1
  fi

  attempt=$((attempt + 1))
done
