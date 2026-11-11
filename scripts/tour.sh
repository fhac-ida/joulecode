#!/usr/bin/env bash
# joule:code tour – learn the features step by step by running them.
#
#   scripts/tour.sh            menu
#   scripts/tour.sh 2          one recipe
#   scripts/tour.sh all        all available recipes one after another
#   scripts/tour.sh all --yes  without pauses and questions (e.g. in CI)
#   scripts/tour.sh 8 PATH     check your own repository
#
#   TOUR_ENERGY=1  also run the (slower) energy measurements on a measuring host without asking
#   TOUR_PORT=N    port of the temporary server in recipes 5 and 6 (default 8799)
#
# Every recipe works on the bundled examples or on temporary copies; your repositories, the
# examples and an existing `joulecode serve` stay untouched. Results go to joulecode-runs/tour/.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tour"
source "$DIR/lib.sh"

# number | file | title | duration | requirements
RECIPES=(
    "1|01-doctor.sh|Check this machine|1 min|Docker"
    "2|02-analyze.sh|Find hotspots|2 min|Docker"
    "3|03-measure.sh|Measure a patch|3 min (+10 with energy)|Docker"
    "4|04-noise.sh|Noise check of the machine (A/A)|2 min (+10 with energy)|Docker"
    "5|05-platform.sh|Platform: server, project, review|3 min|server extras"
    "6|06-gate.sh|Energy gate for a pull request|4 min (+20 with energy)|Docker, server extras"
    "7|07-optimize.sh|Optimise with an LLM|5 min (+20 with energy)|Docker, Ollama"
    "8|08-own-repo.sh|Prepare your own repository|5 min|–"
)

menu() {
    echo
    echo "${B}${C}joule:code tour${N}"
    say "Each recipe runs one feature on a bundled example and explains what you see. Start with 1 and work your way down – nothing is changed in your repositories. Energy measurements are optional; without them every recipe runs in a few minutes."
    echo
    local n f t d r
    for line in "${RECIPES[@]}"; do
        IFS='|' read -r n f t d r <<<"$line"
        if [[ -n "$f" ]]; then
            printf "  ${B}%s${N}  %-38s %-26s ${D}%s${N}\n" "$n" "$t" "$d" "$r"
        else
            printf "  ${D}%s  %-38s coming soon${N}\n" "$n" "$t"
        fi
    done
    echo
}

file_of() {
    local n f _
    for line in "${RECIPES[@]}"; do
        IFS='|' read -r n f _ <<<"$line"
        [[ "$n" == "$1" ]] && { echo "$f"; return; }
    done
}

run_recipe() {
    local f
    f="$(file_of "$1")"
    [[ -n "$f" ]] || fail "Recipe '$1' does not exist (yet). Available: $(available | tr '\n' ' ')"
    bash "$DIR/$f" "${EXTRA[@]}"
}

available() {
    local n f _
    for line in "${RECIPES[@]}"; do
        IFS='|' read -r n f _ <<<"$line"
        [[ -n "$f" ]] && echo "$n"
    done
}

choice=""
EXTRA=()
for a in "$@"; do
    case "$a" in
        -y|--yes) EXTRA+=(--yes) ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) if [[ -z "$choice" ]]; then choice="$a"; else EXTRA+=("$a"); fi ;;   # e.g. 8 PATH
    esac
done

if [[ -z "$choice" ]]; then
    menu
    [[ "$TOUR_YES" == 1 ]] && exit 0
    read -r -p "Which recipe? [1, … or 'all'] " choice || exit 0
    [[ -n "$choice" ]] || exit 0
fi

if [[ "$choice" == all ]]; then
    for n in $(available); do
        run_recipe "$n"
        [[ "$TOUR_YES" == 1 ]] || ask "Continue with the next recipe?" y || exit 0
    done
    echo
    ok "Tour finished. Results: joulecode-runs/tour/"
else
    run_recipe "$choice"
fi
