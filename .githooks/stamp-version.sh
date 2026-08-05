#!/bin/sh
# Write a version string into the <!--VERSION-->...<!--/VERSION--> markers in
# every page that shows a git-stamped version badge -- currently the landing
# page (www/index.html) and the WiFiMon header (wifi_utilization_monitor/templates/index.html)
# -- so the static files served from the Pi always report the deployed version.
# The Pi never runs git.
#
# Called by the pre-commit hook, but safe to run by hand too -- e.g. right after
# creating a release tag, to refresh the badges without waiting for a commit:
#     git tag v0.3
#     .githooks/stamp-version.sh && git commit -am "Bump version badges"
#
# With no argument it derives the version from git: latest tag, commits since,
# short hash, and a -dirty suffix if the working tree has uncommitted changes.
set -e

ROOT="$(git rev-parse --show-toplevel)"
PAGES="$ROOT/www/index.html $ROOT/wifi_utilization_monitor/templates/index.html"
VERSION="${1:-$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)}"

for PAGE in $PAGES; do
    # Portable in-place edit (macOS and Debian both ship perl): replace only the
    # text between the markers, leaving the surrounding HTML untouched.
    perl -0pi -e "s{(<!--VERSION-->).*?(<!--/VERSION-->)}{\${1}${VERSION}\${2}}s" "$PAGE"
    echo "Stamped version in ${PAGE#$ROOT/}: ${VERSION}"
done
