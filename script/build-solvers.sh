#!/usr/bin/env bash
shopt -s nullglob
set -e


ROOT=$(git rev-parse --show-toplevel 2>/dev/null || realpath "$(dirname "$(readlink -f "${0}")")/..")
SOLVERS_DEST="${ROOT}/src/streamlink/solvers"


# ----


log() {
    echo >&2 "build-solvers:" "${@}"
}

err() {
    log "ERROR:" "${@}"
    exit 1
}


# ----


check_deps() {
    local dep
    for dep in git python npm; do
        if ! command -v "${dep}" >/dev/null 2>&1; then
            err "Missing dependency: ${dep}"
        fi
    done
}

build_youtube() {
    local tmp
    # shellcheck disable=SC2064
    tmp=$(mktemp -d) && trap "rm -rf '${tmp}'" EXIT || exit 255

    log "Cloning yt-dlp/ejs"
    git clone --depth=1 https://github.com/yt-dlp/ejs.git "${tmp}"

    log "Building YouTube solver"
    cd "${tmp}"
    python hatch_build.py

    log "Copying YouTube solver files"
    cp dist/yt.solver.core.js     "${SOLVERS_DEST}/youtube/yt.solver.core.js"
    cp dist/yt.solver.lib.js      "${SOLVERS_DEST}/youtube/yt.solver.lib.js"
    cp dist/yt.solver.deno.lib.js "${SOLVERS_DEST}/youtube/yt.solver.deno.lib.js"
}


# ----


check_deps
build_youtube
