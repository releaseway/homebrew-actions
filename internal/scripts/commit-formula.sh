#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_OUTPUT:?}"
: "${FORMULA_PATH:?}"
: "${FORMULA:?}"
: "${VERSION:?}"

if [ -z "$(git status --porcelain -- "$FORMULA_PATH")" ]; then
  echo "changed=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ruby "$script_dir/validate-formula-update.rb" HEAD

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add "$FORMULA_PATH"
git commit -m "chore: update ${FORMULA} to ${VERSION}"
echo "changed=true" >> "$GITHUB_OUTPUT"
