#!/usr/bin/env bash
set -euo pipefail

: "${COMMIT:?COMMIT is required}"
: "${VERSION:?VERSION is required}"
: "${TAP_REPOSITORY:?TAP_REPOSITORY is required}"
: "${TAP_BRANCH:?TAP_BRANCH is required}"

case "${ALLOW_DOWNGRADE:-false}" in
  true|false) ;;
  *) echo "allow-downgrade must be true or false" >&2; exit 1 ;;
esac

reject_multiline() {
  case "$2" in
    *$'\n'*|*$'\r'*)
      echo "$1 must be a single-line value" >&2
      exit 1
      ;;
  esac
}

reject_multiline commit "$COMMIT"
reject_multiline version "$VERSION"
reject_multiline tap-repository "$TAP_REPOSITORY"
reject_multiline tap-branch "$TAP_BRANCH"

case "$COMMIT" in
  *[!0-9a-fA-F]*|"")
    echo "commit must be a full 40-character SHA" >&2
    exit 1
    ;;
esac
if [ "${#COMMIT}" -ne 40 ]; then
  echo "commit must be a full 40-character SHA" >&2
  exit 1
fi

case "$TAP_REPOSITORY" in
  */*/*|/*|*/|*".."*|*[!A-Za-z0-9._/-]*|"")
    echo "tap-repository must be owner/name" >&2
    exit 1
    ;;
  */*) ;;
  *)
    echo "tap-repository must be owner/name" >&2
    exit 1
    ;;
esac

if [ -z "$VERSION" ]; then
  echo "version must not be empty" >&2
  exit 1
fi
