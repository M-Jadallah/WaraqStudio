#!/usr/bin/env bash
set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"
: "${GITHUB_REF:?GITHUB_REF is required}"
: "${TAG:?TAG is required}"

# Release jobs are serialized, but main can advance while an older build runs.
# GITHUB_TOKEN cannot create workflow-changing refs relative to newer main.
# Skip superseded main builds before creating their tag or release. Once the
# release exists, finish attaching assets even if main advances during upload.
skip_superseded_main() {
  if [[ "$GITHUB_REF" != refs/heads/main ]]; then
    return 0
  fi
  local current
  if ! current="$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq .object.sha)"; then
    echo "::error::Could not verify current main before publishing."
    exit 1
  fi
  if [[ ! "$current" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "::error::GitHub returned an invalid main commit; refusing to publish."
    exit 1
  fi
  if [[ "$current" != "$GITHUB_SHA" ]]; then
    echo "::notice::Skipping superseded main release $TAG: built $GITHUB_SHA, current main is $current."
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      printf '### Release skipped\n\nMain advanced from `%s` to `%s` while this build ran. The newer main run owns publication; no existing tag was changed.\n' \
        "$GITHUB_SHA" "$current" >> "$GITHUB_STEP_SUMMARY"
    fi
    exit 0
  fi
}

# Existing tags are immutable, including annotated tags (peeled SHA).
# A transport/auth failure must never be mistaken for a missing tag.
check_tag() {
  local refs status resolved
  if refs="$(git ls-remote --exit-code --tags origin "refs/tags/$TAG" "refs/tags/$TAG^{}")"; then
    resolved="$(printf '%s\n' "$refs" | awk '$2 ~ /\^\{\}$/ {print $1; exit}')"
    if [[ -z "$resolved" ]]; then
      resolved="$(printf '%s\n' "$refs" | awk 'NR == 1 {print $1}')"
    fi
    if [[ "$resolved" != "$GITHUB_SHA" ]]; then
      echo "::error::Refusing to publish: tag $TAG points to $resolved, expected $GITHUB_SHA. Existing tags will not be changed."
      exit 1
    fi
    return 0
  else
    status=$?
    if [[ "$status" != 2 ]]; then
      echo "::error::Could not verify remote tag $TAG (git exit $status)."
      exit "$status"
    fi
    return 2
  fi
}

skip_superseded_main
if check_tag; then
  echo "Verified immutable tag $TAG at $GITHUB_SHA."
else
  if gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs" \
    -f "ref=refs/tags/$TAG" -f "sha=$GITHUB_SHA" >/dev/null; then
    :
  else
    status=$?
    # Main may advance between the preflight and the ref request. Only that
    # verified race is a successful skip; genuine permission/API errors fail.
    skip_superseded_main
    echo "::error::Could not create release tag $TAG; check the GitHub token permissions."
    exit "$status"
  fi
fi
check_tag
skip_superseded_main
if gh release view "$TAG" --json id --jq .id >/dev/null 2>&1; then
  gh release edit "$TAG" --title "WaraqStudio $TAG" --notes-file RELEASE_NOTES.md
else
  gh release create "$TAG" --verify-tag --target "$GITHUB_SHA" \
    --title "WaraqStudio $TAG" --notes-file RELEASE_NOTES.md
fi
# Recheck immediately before attaching assets. Never move/delete tags.
check_tag
gh release upload "$TAG" dist/*.zip dist/*-setup.exe dist/*.sha256 \
  dist/SHA256SUMS "dist/WaraqStudio-$TAG-provenance.json" --clobber
