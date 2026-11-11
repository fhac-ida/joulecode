#!/usr/bin/env bash
# Recipe 04 – how noisy is this machine? An A/A measurement with `joulecode measure --aa`.
#
#   TOUR_ENERGY=1   also run the energy A/A measurement without asking (measuring host)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
tour_init
need_docker

title 04 "Noise check of the machine (A/A)" "2 min, +10 min with energy"
say "Every measurement varies a little: other processes, temperature, caches. Before you trust a '−3 %' you want to know how much this machine varies when NOTHING changes. That is an A/A test: the original is measured against an identical copy of itself."
echo
say "A good A/A result:"
echo "  • verdict 'no significant difference'"
echo "  • a narrow confidence interval around 0 – its width is roughly the smallest effect this machine can detect"
echo "  • a small spread (CV) of the single runs – about 1 % or less on a tuned measuring host"
pause

server_busy && fail "The joule:code server is measuring right now (only one measurement may run on a machine). Wait until its queue is empty, then run this recipe again."

out="$(run_dir aa-cpu)"
section "A/A with CPU time, small workload"
run joulecode measure examples/python-wordstats --aa \
    --backend cgroup --env BENCH_ROUNDS=8 --repetitions 6 --fixed --cooldown 1 --out "$out"

section "Result"
measure_summary "$out/result.json" baseline-b
python3 - "$out/result.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))["comparison"]["baseline-b"]["primary"]
width = (c["ci_high"] - c["ci_low"]) * 100
print()
if c["verdict"] == "no significant difference":
    print(f"  ✔ As expected: no difference. The interval is {width:.1f} percentage points wide –")
    print(f"    effects much smaller than about {width/2:.1f} % cannot be told apart from noise here.")
else:
    print(f"  ! The copy came out '{c['verdict']}' – identical code should not. Either chance (it happens")
    print("    in about 1 of 20 A/A runs at α = 5 %) or a noisy machine: repeat, and see docs/installation.md")
    print("    (noise reduction) if it happens again.")
PY
say "--fixed measures all blocks; normally joule:code stops early once the verdict is clear."

section "Optional: A/A with real energy"
be="$(want_energy "about 10 minutes")"
if [[ -n "$be" ]]; then
    out_e="$(run_dir aa-energy)"
    run joulecode measure examples/python-wordstats --aa --backend "$be" --out "$out_e"
    section "Result (energy)"
    measure_summary "$out_e/result.json" baseline-b
    say "Energy usually varies more than CPU time. If the CV is above a few percent: turn off Turbo Boost, set the governor to 'performance', stop background jobs (docs/installation.md)."
else
    say "Skipped. On a measuring host, run it once after the installation and after every larger change: joulecode measure examples/python-wordstats --aa"
fi

learned "An A/A test measures the noise of the machine: identical variants, no difference expected." \
        "The width of its confidence interval tells you which effects this machine can resolve at all." \
        "A significant A/A result is a warning sign – repeat it, and reduce noise if it persists."
next "scripts/tour.sh 5 – the same building blocks as a server for CI."
