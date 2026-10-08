#!/usr/bin/env bash
set -euo pipefail

: "${TAP_PATH:?}"
: "${FORMULA:?}"

VALIDATION_MODE="${VALIDATION_MODE:-release}"
case "$VALIDATION_MODE" in
  release | spec) ;;
  *)
    echo "validation-mode must be release or spec" >&2
    exit 1
    ;;
esac

validation_root="$(mktemp -d)"
validation_path="${validation_root}/homebrew-validation"
validation_id="${GITHUB_RUN_ID:-$$}-${GITHUB_RUN_ATTEMPT:-1}"
validation_tap="releaseway/validation-${validation_id}"
isolated_tap_paths=()

restore_taps() {
  local index original backup failed=0
  for ((index=0; index<${#isolated_tap_paths[@]}; index++)); do
    original="${isolated_tap_paths[$index]}"
    backup="${validation_root}/isolated-taps/${index}"
    [ -e "$backup" ] || [ -L "$backup" ] || continue
    if [ -e "$original" ] || [ -L "$original" ] || ! mv "$backup" "$original"; then
      echo "Failed to restore tap to $original; backup retained at $backup" >&2
      failed=1
    fi
  done
  return "$failed"
}

cleanup() {
  status="$?"
  set +e
  if ! restore_taps; then
    echo "Validation files retained at $validation_root for recovery" >&2
    exit 1
  fi
  brew untap --force "$validation_tap" >/dev/null 2>&1
  brew untrust --tap "$validation_path" >/dev/null 2>&1
  rm -rf "$validation_root"
  exit "$status"
}
trap cleanup EXIT

cp -R "$TAP_PATH" "$validation_path"
rm -rf "$validation_path/.git"
git init --initial-branch=main "$validation_path"
git -C "$validation_path" config user.name "github-actions[bot]"
git -C "$validation_path" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git -C "$validation_path" add -A
git -C "$validation_path" commit -m "test: validate $FORMULA" >/dev/null

hosted_runner=false
if [ "${GITHUB_ACTIONS:-}" = "true" ] && [ "${RUNNER_ENVIRONMENT:-}" = "github-hosted" ]; then
  hosted_runner=true
  export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1
fi

brew trust --tap "$validation_path"
if [ "$hosted_runner" = "true" ]; then
  # `brew tap` also refreshes image-wide metadata. A validation-only checkout
  # needs no completion links or description-cache updates.
  validation_checkout="$(brew --repository)/Library/Taps/releaseway/homebrew-validation-${validation_id}"
  mkdir -p "$(dirname "$validation_checkout")"
  git clone --quiet --template= "$validation_path" "$validation_checkout"
else
  brew tap "$validation_tap" "$validation_path"
fi
qualified_formula="${validation_tap}/${FORMULA}"

# Hosted images contain unrelated third-party taps. Keep the target and its
# dependency taps visible, and restore the image's other checkouts on exit.
if [ "$hosted_runner" = "true" ]; then
  # Query the named Formula directly: `brew deps` can evaluate unrelated taps
  # while resolving installed packages on preconfigured runner images.
  dependency_taps="$(brew ruby -rformulary -e '
    formula = Formulary.factory(ARGV.fetch(0))
    puts formula.recursive_dependencies { |_dependent, _dependency| nil }
                .filter_map { |dependency| dependency.to_formula.tap&.name }.uniq
  ' -- "$qualified_formula")"
  required_taps=" homebrew/core homebrew/cask ${validation_tap} "
  while IFS= read -r dependency_tap; do
    required_taps+="${dependency_tap} "
  done <<< "$dependency_taps"
  installed_taps="$(brew tap)"
  mkdir -p "${validation_root}/isolated-taps"
  while IFS= read -r tap; do
    [ -n "$tap" ] || continue
    case "$tap" in homebrew/*) continue ;; esac
    [[ "$required_taps" == *" $tap "* ]] && continue
    tap_path="$(brew --repository "$tap")"
    index="${#isolated_tap_paths[@]}"
    isolated_tap_paths+=("$tap_path")
    mv "$tap_path" "${validation_root}/isolated-taps/${index}"
  done <<< "$installed_taps"
fi

brew audit --strict --formula "$qualified_formula"
if [ "$VALIDATION_MODE" = "spec" ]; then
  exit 0
fi
brew install --build-from-source "$qualified_formula"
brew test "$qualified_formula"
