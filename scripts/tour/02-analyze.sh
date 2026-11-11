#!/usr/bin/env bash
# Recipe 02 – find hotspots with `joulecode analyze`.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
tour_init
need_docker

title 02 "Find hotspots" "2 min"
say "Before optimising anything, joule:code finds out where a program actually spends its time. We use the example 'examples/python-wordstats': a small text-statistics library with deliberate inefficiencies."
echo
say "Every repository joule:code works on needs two files:"
echo "  • ${B}Dockerfile${N}      – installs dependencies; programs run in containers without network"
echo "  • ${B}joulecode.yml${N}   – how to run the tests and the benchmark (a fixed, repeatable workload)"
pause

section "examples/python-wordstats/joulecode.yml (excerpt)"
sed -n '/^test:/,/^limits:/p' examples/python-wordstats/joulecode.yml | sed '$d' | sed 's/^/    /'
say "The benchmark is the workload joule:code optimises for. BENCH_ROUNDS scales it."
pause

out="$(run_dir analyze)"
section "Analyse: static checks + profiling run in the container"
run joulecode analyze examples/python-wordstats --out "$out"
look "the 'Hotspots' list: share of the benchmark's own run time per function."

section "The source of the biggest hotspot"
python3 - "$out/analysis.json" <<'PY'
import json, sys
hs = json.load(open(sys.argv[1]))["profile"]["hotspots"]
if not hs:
    print("  (no hotspots found)")
    sys.exit()
h = hs[0]
print(f"  {h['qualname']} – {h['share']*100:.0f} % of the run time, {h['file']}:{h['line']}\n")
for line in h["code"].rstrip().splitlines():
    print("    " + line)
PY
say "Two nested loops over all words – the cost grows with the square of the text length. The comment in the code gives it away; in real code the profile is how you find it."

learned "joulecode analyze profiles the benchmark and ranks functions by their share of run time." \
        "Only these hotspots are later handed to the LLM in 'optimize' – the rest of the code stays untouched." \
        "Full report: ${out#"$TOUR_REPO"/}/analysis.md"
next "scripts/tour.sh 3 – measure whether a faster version really saves something."
