#!/usr/bin/env bash
# Recipe 06 – the energy gate: does a pull request make the program measurably worse?
#
#   TOUR_ENERGY=1   measure energy with the full workload without asking (measuring host)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
tour_init
need_docker

title 06 "Energy gate for a pull request" "4 min, +20 min with energy"
say "A review (recipe 05) only finds known patterns. Whether a change really costs more is only answered by measuring. 'submit --mode gate' sends both sides of a pull request to the server: the target branch and the PR. The server builds both, runs the tests of both and measures them against each other."
echo
say "The gate fails only if the PR is significantly worse AND the whole confidence interval lies above the threshold (--max-regression, default 5 %) – noise alone never makes CI red."
pause

server_busy && fail "The joule:code server is measuring right now (only one measurement may run on a machine). Wait until its queue is empty, then run this recipe again."

section "1. Server and project"
be="$(want_energy "about 10 minutes per gate run, two runs")"
if [[ -n "$be" ]]; then
    start_tour_server full --backend "$be"
else
    say "Quick variant: CPU time instead of energy, smaller workload."
    start_tour_server full --backend cgroup
fi
create_tour_project tour
ok "Project 'tour' created, JOULECODE_SERVER/_PROJECT/_TOKEN set (as in recipe 05)."

section "2. A pull request that looks harmless"
tour_repo "$TOUR_TMP/repo"
if [[ -z "$be" ]]; then
    # the gate takes benchmark and measurement settings from the target branch
    sed -i 's/BENCH_ROUNDS: "40"/BENCH_ROUNDS: "8"/; s/repetitions: 10/repetitions: 6/; s/cooldown_s: 5/cooldown_s: 1/' joulecode.yml
    commit_all "smaller workload for the tour"
    say "(For the quick variant the throw-away repository uses a smaller workload. The gate always takes benchmark and settings from the target branch – a PR cannot change what it is measured with.)"
fi
git switch -q -c feature
sed -i 's/^        if word not in seen:.*/        if seen.count(word) == 0:  # explicit count instead of "in"/' src/wordstats/core.py
commit_all "explicit count in unique_words"
run git --no-pager diff main
say "Same behaviour, the tests pass – a reviewer would probably approve it. But 'in' stops at the first match, 'count' always walks the whole list."
pause

section "3. Gate: PR against main"
set +e
run joulecode submit --mode gate --base main --max-regression 5 --output "$TOUR_TMP/gate1.json"
rc=$?
set -e
echo
case $rc in
    1) ok "Exit code 1 – the PR costs measurably more than 5 %, CI turns red." ;;
    0) warn "Exit code 0 – the regression stayed below the threshold on this machine this time." ;;
    *) warn "Exit code $rc – an error, see the output above." ;;
esac
pause

section "4. Undo the change and measure again"
git -c user.name=tour -c user.email=tour@localhost revert --no-edit HEAD >/dev/null
say "The PR now contains the original code again – there should be no difference."
set +e
run joulecode submit --mode gate --base main --max-regression 5 --output "$TOUR_TMP/gate2.json"
rc=$?
set -e
echo
[[ $rc -eq 0 ]] && ok "Exit code 0 – no measurable regression, the PR passes." || warn "Expected exit code 0, got $rc."
cd "$TOUR_REPO"

section "5. In the web UI"
say "Open $TOUR_URL, Access → admin token:"
echo "    $TOUR_ADMIN"
say "Projects → Tour shows both gate runs; the project page draws them as 'Energy over time' – the history your CI builds up over many pull requests."
if [[ "$TOUR_YES" != 1 ]]; then
    read -r -p "${D}  ⏎ when you are done – the temporary server is then stopped and deleted${N} " _ || true
fi

learned "submit --mode gate measures the PR against its target branch (merge base) on the server." \
        "Configuration and benchmark come from the target branch; both sides must pass their tests." \
        "CI turns red only for a significant regression whose whole confidence interval exceeds --max-regression." \
        "In CI: run the gate on demand (e.g. a PR label), it takes minutes – see docs/platform.md."
next "scripts/tour.sh 7 – let an LLM propose faster code, tested and measured."
