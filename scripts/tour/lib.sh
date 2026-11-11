# Shared helpers for the joule:code tour. Sourced by scripts/tour.sh and every recipe.
#
# Every recipe can also be run on its own:  scripts/tour/02-analyze.sh
# Non-interactive (CI, no pauses, defaults for every question):  TOUR_YES=1 or --yes

set -euo pipefail

TOUR_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOUR_YES="${TOUR_YES:-0}"
for _arg in "$@"; do
    case "$_arg" in -y|--yes) TOUR_YES=1 ;; esac
done
[[ -t 0 ]] || TOUR_YES=1   # no keyboard → never wait for one
export TOUR_YES

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    B=$'\e[1m'; D=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else
    B=""; D=""; G=""; Y=""; R=""; C=""; N=""
fi
_width() { local w; w=$(tput cols 2>/dev/null || echo 80); (( w > 90 )) && w=90; echo "$w"; }

# -- Output ---------------------------------------------------------------------------
title() {   # title "02" "Find hotspots" "2 min"
    echo
    echo "${B}${C}━━━ Recipe $1 · $2 ${D}(about $3)${N}"
}
say()     { echo "$*" | fold -s -w "$(_width)"; }
section() { echo; echo "${B}▸ $*${N}"; }
ok()      { echo "${G}✔${N} $*"; }
warn()    { echo "${Y}!${N} $*" | fold -s -w "$(_width)"; }
fail()    { echo "${R}✘ $*${N}" | fold -s -w "$(_width)" >&2; exit 1; }
look()    { echo "${D}  Look for:${N} $*" | fold -s -w "$(_width)"; }
learned() { echo; echo "${B}What you learned${N}"; for l in "$@"; do echo "  • $l"; done; }
next()    { echo; echo "${B}Next:${N} $*"; }

# run CMD …  – print the command as you would type it, then run it
run() {
    echo "${D}\$${N} ${B}$*${N}"
    "$@"
}

pause() {
    [[ "$TOUR_YES" == 1 ]] && return 0
    read -r -p "${D}  ⏎ continue · Ctrl+C quit${N} " _ || true
}

# ask "Question" default(y|n) → exit status 0 for yes
ask() {
    local def="${2:-n}" ans
    if [[ "$TOUR_YES" == 1 ]]; then [[ "$def" == y ]]; return; fi
    read -r -p "  $1 [$([[ $def == y ]] && echo Y/n || echo y/N)] " ans || true
    ans="${ans:-$def}"
    [[ "$ans" =~ ^[yYjJ] ]]
}

# -- Environment ----------------------------------------------------------------------
tour_init() {
    cd "$TOUR_REPO"
    if [[ -z "${VIRTUAL_ENV:-}" && -f .venv/bin/activate ]]; then
        # shellcheck disable=SC1091
        source .venv/bin/activate      # also loads .joulecode.env written by install.sh
    fi
    command -v joulecode >/dev/null || fail "joulecode is not installed. In the repository: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
    TOUR_RUNS="$TOUR_REPO/joulecode-runs/tour"
    mkdir -p "$TOUR_RUNS"
    TOUR_TMP="$(mktemp -d "${TMPDIR:-/tmp}/joulecode-tour.XXXXXX")"
    trap _tour_cleanup EXIT
}

_TOUR_CLEANUP=()
on_exit() { _TOUR_CLEANUP+=("$*"); }   # register a cleanup command; they run last-in, first-out
_tour_cleanup() {
    local i
    for ((i = ${#_TOUR_CLEANUP[@]} - 1; i >= 0; i--)); do eval "${_TOUR_CLEANUP[i]}" >/dev/null 2>&1 || true; done
    [[ -n "${TOUR_TMP:-}" ]] && rm -rf "$TOUR_TMP"
}

have_docker() { command -v docker >/dev/null && docker info >/dev/null 2>&1; }

need_docker() {
    command -v docker >/dev/null || fail "Docker is missing. Install it first (docs/installation.md)."
    docker info >/dev/null 2>&1 || fail "The Docker daemon is not reachable. Is it running, and is your user in the 'docker' group?"
}

# A server-side measurement holds the measurement lock; a manual measure would abort.
server_busy() {
    local url="${JOULECODE_SERVER:-http://127.0.0.1:8765}"
    curl -sf --max-time 2 "$url/api/health" 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("current_job") else 1)' 2>/dev/null
}

# Output folder for a recipe: joulecode-runs/tour/<name>-<timestamp>
run_dir() { echo "$TOUR_RUNS/$1-$(date +%Y%m%d-%H%M%S)"; }

# `joulecode doctor` output, cached per recipe
_DOCTOR=""
doctor_out() { [[ -n "$_DOCTOR" ]] || _DOCTOR="$(joulecode doctor 2>&1 || true)"; echo "$_DOCTOR"; }

# Most accurate back-end according to `joulecode doctor` (empty if none)
best_backend() { doctor_out | sed -n 's/^Most accurate available back-end: //p' | tail -1; }

# Back-end for an on-demand energy measurement: metrion, metrion-legacy or rapl (empty if none).
# metrion-live is left out on purpose: it is only "ready" here when a METRION service is
# still running from an earlier (possibly aborted) run.
energy_backend() {
    local b
    for b in metrion metrion-legacy rapl; do
        doctor_out | grep -Eq "✔ Backend ${b} +ready" && { echo "$b"; return; }
    done
    return 0
}

# true if a METRION service left over from an aborted run blocks the metrion back-end
metrion_leftover() { doctor_out | grep -q "already running"; }

# Ask whether to also measure energy (TOUR_ENERGY=1: yes without asking). Prints the back-end
# to use, or nothing. Explains why if energy is not available right now.
want_energy() {   # want_energy "about 10 minutes"
    doctor_out >/dev/null
    local b
    b="$(energy_backend)"
    if [[ -z "$b" ]]; then
        metrion_leftover && warn "Energy measurement not available right now: a METRION service from an interrupted measurement is still running. Run 'joulecode cleanup', then this recipe again." >&2
        return 0
    fi
    say "This machine can measure energy (back-end '$b'). With the full workload this takes $1." >&2
    if [[ "${TOUR_ENERGY:-0}" == 1 ]] || ask "Measure energy now?" n; then echo "$b"; fi
}

# -- Temporary server for the platform recipes --------------------------------------------
# start_tour_server PROCESSOR [serve options…]
# Own server on $TOUR_PORT (default 8799) with an empty data directory, so an existing
# `joulecode serve` is not touched. Sets TOUR_URL and TOUR_ADMIN; stopped when the recipe ends.
start_tour_server() {
    local processor="$1" pid
    shift
    TOUR_PORT="${TOUR_PORT:-8799}"
    TOUR_URL="http://127.0.0.1:$TOUR_PORT"
    python3 -c "import fastapi, uvicorn, multipart" 2>/dev/null \
        || fail "The server extras are missing: pip install -e '.[server]'"
    curl -sf --max-time 1 "$TOUR_URL/api/health" >/dev/null 2>&1 \
        && fail "Port $TOUR_PORT is already in use. Choose another one: TOUR_PORT=8800 $0"
    TOUR_ADMIN="gca_tour_$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "${D}\$${N} ${B}joulecode serve --port $TOUR_PORT --data-dir … --processor $processor $*${N}"
    JOULECODE_ADMIN_TOKEN="$TOUR_ADMIN" joulecode serve --port "$TOUR_PORT" --data-dir "$TOUR_TMP/server" \
        --processor "$processor" "$@" --log-level warning >"$TOUR_TMP/serve.log" 2>&1 &
    pid=$!
    on_exit "kill $pid; wait $pid"
    on_exit "cancel_tour_jobs"   # runs first: a job cancelled properly removes its containers
    for _ in $(seq 60); do curl -sf "$TOUR_URL/api/health" >/dev/null 2>&1 && break; sleep 0.25; done
    curl -sf "$TOUR_URL/api/health" >/dev/null || { cat "$TOUR_TMP/serve.log" >&2; fail "The server did not start."; }
    ok "Server running: $TOUR_URL"
}

# Cancel jobs still running on the tour server (Ctrl+C during a measurement) and wait for them
cancel_tour_jobs() {
    local ids id
    ids="$(curl -sf "$TOUR_URL/api/queue" | python3 -c 'import json,sys; q=json.load(sys.stdin); print(" ".join(j["id"] for j in q["running"] + q["queued"]))')" || return 0
    [[ -n "$ids" ]] || return 0
    echo "Cancelling the running job and removing its containers …" >/dev/tty 2>/dev/null || true
    for id in $ids; do
        curl -sf -X POST -H "Authorization: Bearer $TOUR_ADMIN" "$TOUR_URL/api/jobs/$id/cancel" >/dev/null || true
    done
    for _ in $(seq 120); do
        curl -sf "$TOUR_URL/api/queue" | python3 -c 'import json,sys; sys.exit(1 if json.load(sys.stdin)["running"] else 0)' && return 0
        sleep 0.5
    done
}

# create_tour_project KEY – creates the project and exports JOULECODE_SERVER/_PROJECT/_TOKEN
create_tour_project() {
    local token
    token="$(curl -sf -X POST -H "Authorization: Bearer $TOUR_ADMIN" -H "Content-Type: application/json" \
        -d "{\"key\":\"$1\",\"name\":\"Tour\"}" "$TOUR_URL/api/projects" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
    export JOULECODE_SERVER="$TOUR_URL" JOULECODE_PROJECT="$1" JOULECODE_TOKEN="$token"
}

# tour_repo DIR – throw-away Git repository with examples/python-wordstats on branch main; cd's into it
tour_repo() {
    cp -r "$TOUR_REPO/examples/python-wordstats" "$1"
    cd "$1"
    git init -q -b main
    commit_all "wordstats"
}
commit_all() { git add -A && git -c user.name=tour -c user.email=tour@localhost commit -qm "$1"; }

# Key figures of a measurement result (result.json of measure, or a measurement of optimize)
measure_summary() {   # measure_summary RESULT_JSON [VARIANT]
    python3 - "$1" "${2:-}" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
if r.get("status") != "ok":
    print(f"  status: {r.get('status')} – {r.get('error')}"); sys.exit(1)
b, q = r["backend"], r["summary"]
variant = sys.argv[2] or next(v for v in r["comparison"])
unit = "J" if b.get("is_energy") else "CPU-s"
c = r["comparison"][variant]["primary"]
base, cand = q["baseline"]["primary"], q[variant]["primary"]
pct = lambda x: f"{x*100:+.1f} %"
print(f"  back-end      {b['name']} – {b['quantity']}")
print(f"  baseline      {base['median']:.3g} {unit}   median of {base['n']} runs, spread (CV) {base['cv']*100:.1f} %")
print(f"  {variant:<13} {cand['median']:.3g} {unit}   median of {cand['n']} runs, spread (CV) {cand['cv']*100:.1f} %")
print(f"  change        {pct(c['rel_change'])}   95 % CI {pct(c['ci_low'])} … {pct(c['ci_high'])}   p = {c['p_value']:.3g}")
print(f"  verdict       {c['verdict']}")
PY
}
