#!/bin/sh
# CoBirb's installer — and its upgrader, and its downgrader.
#
# There is deliberately only one of these. `cobirb --upgrade` on a managed
# install does not reimplement any of the work below; it runs this same script
# with a different --version, so "install v0.14.0" and "go back to v0.0.3" are
# the same code path and can never drift apart.
#
# What it does: finds a Python, builds a venv at ~/.local/share/cobirb/venv,
# installs a checksum-verified wheel from the GitHub release into it, and puts
# a `cobirb` symlink in ~/.local/bin. No sudo, no pipx, nothing outside your
# home directory.
#
# It never touches ~/.cobirb/ — that is your config, sessions and memory, and
# it outlives any install. --uninstall leaves it alone too.
#
# Everything is inside main() with the call on the very last line: a script
# read over a pipe is executed as it arrives, so a connection that dies
# halfway through a plain top-to-bottom script runs the first half of it. This
# way a truncated download defines some functions and does nothing at all.

set -eu

REPO="HeckerBirb/CoBirb"
INSTALL_DIR="${COBIRB_INSTALL_DIR:-$HOME/.local/share/cobirb}"
BIN_DIR="${COBIRB_BIN_DIR:-$HOME/.local/bin}"
VENV="$INSTALL_DIR/venv"
SHIM="$BIN_DIR/cobirb"
MARKER="$INSTALL_DIR/install.json"

# Where release assets are fetched from. Overridable for the same reason
# `upgrade._find_repo_root` takes a `start`: the download half of this script
# is otherwise only exercisable against a real published release, which is a
# poor thing to discover a bug in. Point it at a directory of assets
# (file://...) to run the whole path offline.
RELEASE_BASE="${COBIRB_RELEASE_BASE:-https://github.com/$REPO/releases/download}"

say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
CoBirb installer.

  install.sh                    install, or move to, the latest release
  install.sh --version v0.0.3   install exactly that release
  install.sh --force            permit moving to an older version
  install.sh --uninstall        remove CoBirb, keep ~/.cobirb/
  install.sh --help             this

Over a pipe, pass arguments after `-s --`:

  curl -fsSL .../install.sh | bash -s -- --version v0.0.3 --force

Environment:
  COBIRB_INSTALL_DIR   where the venv lives (default ~/.local/share/cobirb)
  COBIRB_BIN_DIR       where the `cobirb` symlink goes (default ~/.local/bin)
USAGE
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is not on PATH, and this needs it."
}

# 1.2.3 -> 1002003, so two versions compare with a plain integer test. Minor
# and patch are given three digits each, which is more room than a project
# using semver tags will ever need.
version_key() {
    _major=$(printf '%s' "$1" | cut -d. -f1)
    _minor=$(printf '%s' "$1" | cut -d. -f2)
    _patch=$(printf '%s' "$1" | cut -d. -f3)
    printf '%s' "$(( _major * 1000000 + _minor * 1000 + _patch ))"
}

# A release tag with or without its "v", as a bare X.Y.Z. Rejects anything
# else rather than letting a typo become a 404 three steps later.
normalise_version() {
    _v="${1#v}"
    case "$_v" in
        *[!0-9.]* | "") die "'$1' is not a release version — expected e.g. v0.14.0." ;;
    esac
    [ "$(printf '%s' "$_v" | tr -cd . | wc -c)" -eq 2 ] \
        || die "'$1' is not a release version — expected e.g. v0.14.0."
    printf '%s' "$_v"
}

# The newest release, without calling the API: /releases/latest redirects to
# /releases/tag/vX.Y.Z, so the resolved URL carries the answer. The API would
# do as well until it rate-limits an unauthenticated caller at 60/hour, which
# is a confusing way for an installer to fail.
resolve_latest() {
    _url=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
        "https://github.com/$REPO/releases/latest") \
        || die "could not reach GitHub to find the latest release."
    _tag="${_url##*/}"
    [ -n "$_tag" ] && [ "$_tag" != "latest" ] \
        || die "GitHub did not name a latest release — pass --version explicitly."
    normalise_version "$_tag"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        die "no sha256sum or shasum on PATH — refusing to install an unverified download."
    fi
}

verify_sha256() {
    _file="$1"
    _sums="$2"
    _name=$(basename "$_file")
    _want=$(awk -v n="$_name" '{ sub(/^\*/, "", $2); if ($2 == n) print $1 }' "$_sums")
    [ -n "$_want" ] || die "$_name is not listed in SHA256SUMS — refusing to install it."
    _got=$(sha256_of "$_file")
    [ "$_want" = "$_got" ] || die "checksum mismatch for $_name.
  expected $_want
  got      $_got
Refusing to install. This is worth looking into rather than retrying."
}

# The first interpreter that is actually new enough. `python3` is tried before
# the versioned names so the system default wins where it qualifies; the rest
# are the fallback for a distro whose `python3` is older than CoBirb needs.
find_python() {
    for _candidate in python3 python3.14 python3.13 python3.12 python3.11; do
        command -v "$_candidate" >/dev/null 2>&1 || continue
        if "$_candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
            >/dev/null 2>&1; then
            printf '%s' "$_candidate"
            return 0
        fi
    done
    return 1
}

installed_version() {
    [ -f "$MARKER" ] || return 0
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$MARKER" | head -n 1
}

do_uninstall() {
    _removed=0
    # Only a shim that points into the venv this script manages. A `cobirb` on
    # PATH from pipx or a venv of your own is not ours to delete.
    if [ -L "$SHIM" ]; then
        case "$(readlink "$SHIM")" in
            "$VENV"/*) rm -f "$SHIM"; say "removed $SHIM"; _removed=1 ;;
            *) warn "left $SHIM alone — it does not point into $VENV" ;;
        esac
    fi
    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
        say "removed $INSTALL_DIR"
        _removed=1
    fi
    [ "$_removed" -eq 1 ] || say "nothing to uninstall — no managed install found."
    say "Your ~/.cobirb/ (config, sessions, memory) was not touched."
}

main() {
    version_arg=""
    force=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --version) [ $# -ge 2 ] || die "--version needs a release, e.g. --version v0.14.0."
                       version_arg="$2"; shift 2 ;;
            --version=*) version_arg="${1#--version=}"; shift ;;
            --force) force=1; shift ;;
            --uninstall) do_uninstall; return 0 ;;
            -h|--help) usage; return 0 ;;
            *) die "unknown option '$1' — see --help." ;;
        esac
    done

    need_cmd curl
    need_cmd awk
    need_cmd sed

    python=$(find_python) || die "no Python 3.11 or newer on PATH. Install one, then run this again."

    if [ -n "$version_arg" ]; then
        target=$(normalise_version "$version_arg")
    else
        say "Finding the latest release..."
        target=$(resolve_latest)
    fi
    tag="v$target"

    current=$(installed_version)
    if [ -n "$current" ]; then
        if [ "$(version_key "$current")" -eq "$(version_key "$target")" ]; then
            say "Already on v$current — nothing to do."
            return 0
        fi
        if [ "$(version_key "$target")" -lt "$(version_key "$current")" ] && [ "$force" -eq 0 ]; then
            die "v$target is older than the installed v$current — that's a downgrade.
Pass --force if that's actually what you want."
        fi
    fi

    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT INT TERM

    wheel="cobirb-$target-py3-none-any.whl"
    base="$RELEASE_BASE/$tag"

    say "Downloading $wheel..."
    curl -fsSL -o "$tmp/$wheel" "$base/$wheel" \
        || die "could not download $wheel from the $tag release."
    curl -fsSL -o "$tmp/SHA256SUMS" "$base/SHA256SUMS" \
        || die "could not download SHA256SUMS from the $tag release — refusing to install unverified."
    verify_sha256 "$tmp/$wheel" "$tmp/SHA256SUMS"
    say "Checksum verified."

    if [ ! -d "$VENV" ]; then
        mkdir -p "$INSTALL_DIR"
        "$python" -m venv "$VENV" 2>/dev/null || die "could not create a virtualenv at $VENV.
On Debian/Ubuntu the venv module ships separately: 'sudo apt install python3-venv'."
    fi

    say "Installing into $VENV..."
    "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check "$tmp/$wheel" \
        || die "pip could not install $wheel into $VENV."

    mkdir -p "$BIN_DIR"
    ln -sf "$VENV/bin/cobirb" "$SHIM"

    cat > "$MARKER" <<EOF
{
  "kind": "managed",
  "venv": "$VENV",
  "shim": "$SHIM",
  "version": "$target",
  "tag": "$tag",
  "source": "https://github.com/$REPO/releases/tag/$tag",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

    if [ -n "$current" ]; then
        say "CoBirb v$current -> v$target."
    else
        say "CoBirb v$target installed."
    fi
    say "  binary: $SHIM"

    case ":$PATH:" in
        *":$BIN_DIR:"*) say "Run 'cobirb' to start." ;;
        *) say ""
           warn "$BIN_DIR is not on your PATH. Add this to your shell's rc file:"
           warn ""
           warn "    export PATH=\"\$PATH:$BIN_DIR\""
           warn ""
           warn "Until then, run it as $SHIM." ;;
    esac
}

main "$@"
