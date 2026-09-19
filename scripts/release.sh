#!/bin/sh
# Cut a release: version bump, CHANGELOG heading, commit, tag. Optionally push.
#
# This script exists to settle, once, what a release *is*. The sequence had
# been improvised from memory five times in a row, and each time it grew a
# different set of checks around it — a full test suite here, a run of the
# published installer there, a poll of the release workflow the time after
# that. None of those were decided; they accreted. So the point of this file
# is as much the steps it leaves out as the ones it performs.
#
#   scripts/release.sh patch          # 0.15.1 -> 0.15.2
#   scripts/release.sh minor          # 0.15.1 -> 0.16.0
#   scripts/release.sh major          # 0.15.1 -> 1.0.0
#   scripts/release.sh 0.20.0         # or name the version outright
#   scripts/release.sh patch --push   # ...and push main and the tag
#
# What it deliberately does NOT do:
#
#   * Run the test suite. The code being released was tested when it was
#     written, and `tests.yml` runs on every push to main. All this adds on
#     top of that is a version string and a CHANGELOG heading, neither of
#     which any test can observe. Pass --test if you want it anyway.
#   * Push, unless told to. Committing and tagging locally is reversible;
#     pushing is not, so it stays a separate decision rather than a side
#     effect of running this.
#   * Watch the release workflow, install the published artifact, or check
#     anything after the push. If a release needs verifying, that is a
#     deliberate act with its own command, not a tail on this one.

set -eu

die() { printf 'release: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

usage() {
    cat <<'USAGE'
Cut a release: version bump, CHANGELOG heading, commit, tag.

  scripts/release.sh patch          0.15.1 -> 0.15.2
  scripts/release.sh minor          0.15.1 -> 0.16.0
  scripts/release.sh major          0.15.1 -> 1.0.0
  scripts/release.sh 0.20.0         or name the version outright

  --push    push main and the tag afterwards (otherwise nothing leaves your machine)
  --test    run the suite first (off by default — see the comment at the top of this file)

Write the notes under a '## [Unreleased]' heading in CHANGELOG.md before running this;
that heading is what becomes the new version's section.
USAGE
    exit 0
}

current_version() {
    sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' pyproject.toml | head -n 1
}

# "patch" and friends against the current version, or a literal X.Y.Z passed
# straight through. Anything else is a typo, and a typo that reached the tag
# would be a release nobody can ask for by name.
next_version() {
    _current="$1"
    _bump="$2"
    _major=$(printf '%s' "$_current" | cut -d. -f1)
    _minor=$(printf '%s' "$_current" | cut -d. -f2)
    _patch=$(printf '%s' "$_current" | cut -d. -f3)
    case "$_bump" in
        major) printf '%s.0.0' "$(( _major + 1 ))" ;;
        minor) printf '%s.%s.0' "$_major" "$(( _minor + 1 ))" ;;
        patch) printf '%s.%s.%s' "$_major" "$_minor" "$(( _patch + 1 ))" ;;
        [0-9]*.[0-9]*.[0-9]*)
            case "$_bump" in
                *[!0-9.]*) die "'$_bump' is not a version — expected e.g. 0.16.0." ;;
            esac
            printf '%s' "$_bump" ;;
        *) die "'$_bump' is not patch, minor, major, or an X.Y.Z version." ;;
    esac
}

main() {
    bump=""
    push=0
    run_tests=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --push) push=1; shift ;;
            --test) run_tests=1; shift ;;
            -h|--help) usage ;;
            -*) die "unknown option '$1' — see --help." ;;
            *) [ -z "$bump" ] || die "give one version or bump level, not two."
               bump="$1"; shift ;;
        esac
    done
    [ -n "$bump" ] || die "say what to bump: patch, minor, major, or an X.Y.Z version."

    [ -f pyproject.toml ] && [ -f CHANGELOG.md ] \
        || die "run this from the repository root."

    # The argument is checked before anything about the repository, because a
    # mistyped bump level is the likeliest mistake here and the cheapest to
    # detect. Checked later, `release.sh sideways` reports whatever repository
    # condition happens to be unmet first, which sends you looking at the
    # CHANGELOG over a typo in the command you just typed.
    current=$(current_version)
    [ -n "$current" ] || die "could not read the version out of pyproject.toml."
    version=$(next_version "$current" "$bump")
    tag="v$version"

    # A dirty tree means the release commit would carry work nobody reviewed as
    # part of it. Refused rather than stashed, for the reason `--upgrade`
    # refuses one: a stash the user did not ask for is a surprise they then
    # have to go and find.
    [ -z "$(git status --porcelain)" ] \
        || die "the working tree has uncommitted changes — commit or stash them first."

    branch=$(git symbolic-ref --quiet --short HEAD || echo "")
    [ "$branch" = "main" ] \
        || die "releases are cut from main; this is '${branch:-a detached HEAD}'."

    # The notes have to already exist. A release whose CHANGELOG section is
    # written at tagging time is one whose notes were written by whoever
    # happened to be tagging, from memory, after the fact.
    grep -q '^## \[Unreleased\]$' CHANGELOG.md \
        || die "CHANGELOG.md has no '## [Unreleased]' section — write the notes first."

    git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null \
        && die "$tag already exists."

    if [ "$run_tests" -eq 1 ]; then
        # $PYTHON for the usual case of a venv that is not the active shell's
        # `python3` — the suite needs the dev extras, so the interpreter that
        # has them is the one that must run it.
        say "Running the test suite…"
        "${PYTHON:-python3}" -m pytest -q || die "tests failed; nothing was changed."
    fi

    say "Releasing $current -> $version"

    # Both rewrites are anchored: the version line must be the one in
    # [project], and the heading must be the Unreleased one, or a second
    # match somewhere else in either file would be silently rewritten too.
    sed -i "0,/^version[[:space:]]*=[[:space:]]*\"$current\"/s//version = \"$version\"/" pyproject.toml
    sed -i "0,/^## \[Unreleased\]\$/s//## [$version]/" CHANGELOG.md

    [ "$(current_version)" = "$version" ] \
        || die "the version rewrite did not take; pyproject.toml is unchanged."

    git add pyproject.toml CHANGELOG.md
    git commit -q -m "$tag"
    git tag "$tag"
    say "Committed and tagged $tag."

    if [ "$push" -eq 1 ]; then
        git push origin "$branch"
        git push origin "$tag"
        say "Pushed. The release workflow builds from the tag."
    else
        say ""
        say "Nothing has been pushed. To ship it:"
        say "    git push origin $branch && git push origin $tag"
        say "To undo instead:"
        say "    git tag -d $tag && git reset --hard HEAD~1"
    fi
}

main "$@"
