#!/usr/bin/env bash
set -euo pipefail

: "${TAP_PATH:?}"
: "${SOURCE_PATH:?}"
: "${REPOSITORY:?}"
: "${COMMIT:?}"
: "${SPEC_PATH:?}"
: "${GITHUB_OUTPUT:?}"

VALIDATION_MODE="${VALIDATION_MODE:-release}"
case "$VALIDATION_MODE" in
  release | spec) ;;
  *)
    echo "validation-mode must be release or spec" >&2
    exit 1
    ;;
esac

reject_multiline() {
  case "$2" in
    *$'\n'* | *$'\r'*)
      echo "$1 must be a single-line value" >&2
      exit 1
      ;;
  esac
}

reject_multiline "repository" "$REPOSITORY"
reject_multiline "commit" "$COMMIT"
reject_multiline "version" "${VERSION:-}"
reject_multiline "spec-path" "$SPEC_PATH"

validate_repo() {
  case "$1" in
    */*/* | /* | */ | *".."* | *[!A-Za-z0-9._/-]* | "")
      echo "repository must be owner/name, got: $1" >&2
      exit 1
      ;;
    */*) ;;
    *)
      echo "repository must be owner/name, got: $1" >&2
      exit 1
      ;;
  esac
}

normalize_formula_version() {
  local value="$1"

  case "$value" in
    refs/tags/*)
      value="${value#refs/tags/}"
      ;;
    tags/*)
      value="${value#tags/}"
      ;;
  esac

  if [[ "$value" =~ ^[vV]([0-9].*)$ ]]; then
    value="${BASH_REMATCH[1]}"
  fi

  printf '%s\n' "$value"
}

validate_repo "$REPOSITORY"

if [[ ! "$COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "commit must be a full 40-character SHA" >&2
  exit 1
fi

case "$SPEC_PATH" in
  /* | *"/../"* | ../* | */.. | "..")
    echo "spec-path must stay inside the source repository: $SPEC_PATH" >&2
    exit 1
    ;;
esac

spec="${SOURCE_PATH%/}/${SPEC_PATH}"
if [ ! -f "$spec" ]; then
  echo "formula spec not found: $SPEC_PATH" >&2
  exit 1
fi

source_commit="$(git -C "$SOURCE_PATH" rev-parse HEAD)"
if [[ ! "$source_commit" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "could not resolve source checkout to a commit SHA: $source_commit" >&2
  exit 1
fi
normalized_commit="$(printf '%s' "$COMMIT" | tr '[:upper:]' '[:lower:]')"
if [ "$source_commit" != "$normalized_commit" ]; then
  echo "source checkout does not match commit: checkout=$source_commit commit=$normalized_commit" >&2
  exit 1
fi

version="${VERSION:-}"
if [ -z "$version" ]; then
  version="${normalized_commit:0:12}"
fi
version="$(normalize_formula_version "$version")"
if [ -z "$version" ]; then
  echo "version must not be empty" >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

TAP_PATH="$TAP_PATH" \
REPOSITORY="$REPOSITORY" \
VERSION="$version" \
SOURCE_COMMIT="$source_commit" \
SOURCE_PATH="$SOURCE_PATH" \
SPEC="$spec" \
VALIDATION_MODE="$VALIDATION_MODE" \
GITHUB_OUTPUT="$GITHUB_OUTPUT" \
ruby "$script_dir/render.rb"
