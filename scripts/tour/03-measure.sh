#!/usr/bin/env bash
# Recipe 03 – measure a hand-made patch against the original with `joulecode measure`.
#
#   TOUR_ENERGY=1   also run the slower energy measurement without asking (measuring host)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
tour_init
need_docker

title 03 "Measure a patch" "3 min, +5–10 min with energy"
say "Someone claims a change makes wordstats faster. joule:code does not trust claims – it measures. 'measure' builds the original and the changed variant, runs the tests in both (both must pass) and then runs the benchmark alternately, in random order, several times."
pause

section "The change we are going to measure"
say "examples/python-wordstats-optimized.patch replaces lists with sets and counters (excerpt, - removed, + added):"
grep -E '^[+-][^+-]' examples/python-wordstats-optimized.patch | head -14 | sed 's/^/    /'
echo "    …"
pause

server_busy && fail "The joule:code server is measuring right now (only one measurement may run on a machine). Wait until its queue is empty, then run this recipe again."

out="$(run_dir measure-cpu)"
section "Quick measurement: CPU time, small workload"
say "For the tour we shrink the workload (BENCH_ROUNDS=8 instead of 40) and the pauses. You will see a warning that the runs are short – expected here, real measurements use runs of 20 s or more."
run joulecode measure examples/python-wordstats --candidate-patch examples/python-wordstats-optimized.patch \
    --backend cgroup --env BENCH_ROUNDS=8 --repetitions 5 --cooldown 1 --out "$out"

section "Result"
measure_summary "$out/result.json"
echo
say "How to read it: 'better' requires two things – the difference is statistically significant (p below α), and the whole 95 % confidence interval lies below zero. A large saving with an interval crossing zero would be 'no significant difference'."
look "the report in your browser:"
echo "    $out/report.html"

section "Optional: the same with real energy"
be="$(want_energy "about 5–10 minutes")"
if [[ -n "$be" ]]; then
    out_e="$(run_dir measure-energy)"
    run joulecode measure examples/python-wordstats --candidate-patch examples/python-wordstats-optimized.patch \
        --backend "$be" --out "$out_e"
    section "Result (energy)"
    measure_summary "$out_e/result.json"
    look "$out_e/report.html – 'dynamic energy' is the energy above idle that the container's processes caused."
else
    say "Skipped. Any time later: joulecode measure examples/python-wordstats --candidate-patch examples/python-wordstats-optimized.patch"
fi

learned "joulecode measure compares two variants: tests first, then interleaved, repeated runs." \
        "A change only counts as 'better' with a significant result whose whole confidence interval is below zero." \
        "With --backend cgroup it is CPU time; on a measuring host 'auto' measures energy."
next "scripts/tour.sh 4 – how precise is this machine? Measure the original against itself."
