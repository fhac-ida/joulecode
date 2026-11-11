#!/usr/bin/env bash
# Recipe 07 – let an LLM propose faster code: `joulecode optimize`.
#
#   TOUR_ENERGY=1   measure the candidates' energy with the full workload (measuring host)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
tour_init
need_docker

title 07 "Optimise with an LLM" "5 min, +20 min with energy"
say "Now everything comes together. 'optimize' finds the hotspots (recipe 2), asks a local language model for faster versions of exactly these functions, keeps only candidates that pass the tests, has a second LLM role review their maintainability – and then measures every survivor against the original (recipe 3)."
echo
say "The LLM only makes suggestions; the measurement decides. Nothing is applied to your code."
pause

section "Is the language model ready?"
host="${JOULECODE_OLLAMA_HOST:-http://localhost:11434}"
model="${JOULECODE_OLLAMA_MODEL:-qwen2.5-coder:14b}"
tags="$(curl -sf --max-time 3 "$host/api/tags" || true)"
if [[ -z "$tags" ]]; then
    warn "Ollama is not reachable at $host."
    say "Start it with 'docker compose up -d' in the repository (NVIDIA GPU) – without a GPU see 'Let the LLM optimise' in the README. Then run this recipe again."
    exit 0
fi
if ! grep -q "\"name\":\"$model\"" <<<"$tags"; then
    warn "Ollama is running, but the model $model is missing."
    say "Fetch it with: docker exec joulecode-ollama ollama pull $model"
    exit 0
fi
ok "Ollama at $host with $model"
say "Which model fits your GPU: docs/installation.md (7B is enough for Python, PHP, JavaScript and Java need 14B)."
pause

server_busy && fail "The joule:code server is measuring right now (only one measurement may run on a machine). Wait until its queue is empty, then run this recipe again."

be="$(want_energy "about 20 minutes")"
out="$(run_dir optimize)"
section "Run the pipeline on wordstats"
if [[ -n "$be" ]]; then
    run joulecode optimize examples/python-wordstats --backend "$be" --out "$out"
else
    say "Quick variant: 2 hotspots × 2 candidates, CPU time, small workload."
    run joulecode optimize examples/python-wordstats --hotspots 2 --candidates 2 \
        --backend cgroup --env BENCH_ROUNDS=8 --repetitions 5 --cooldown 1 --out "$out"
fi

section "The candidates"
python3 - "$out/result.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
if r.get("status") not in ("ok", None) and not r.get("candidates"):
    print(f"  status: {r.get('status')} – {r.get('error')}"); sys.exit(1)
ranked = {x["id"]: x for x in r.get("ranking") or []}
pct = lambda x: f"{x*100:+.0f} %"
print(f"  {'id':<9} {'hotspot':<16} {'status':<14} {'change':>7}  {'95 % CI':<17} {'maint.':<6}  idea")
for c in (r.get("candidates") or []) + ([r["combination"]] if r.get("combination") else []):
    m = ranked.get(c["id"], {})
    ci = f"{pct(m['ci_low'])} … {pct(m['ci_high'])}" if m.get("ci_low") is not None else ""
    star = "★" if m.get("pareto") else " "
    maint = f"{m['maintainability']:.0f}/5" if m.get("maintainability") is not None else ""
    change = pct(m["rel_change"]) if m.get("rel_change") is not None else "–"
    print(f"{star} {c['id']:<9} {c['hotspot'][:16]:<16} {c.get('status') or 'measured':<14} {change:>7}  {ci:<17} {maint:<6}  {c['title'][:44]}")
PY
echo
say "★ = Pareto-optimal: no other candidate for the same hotspot is both more efficient and easier to maintain. 'combined' merges the best candidates of all hotspots and is measured once more. Candidates that broke the tests never get measured. The LLM is not deterministic – your list will look a bit different every time."
pause

section "A patch, ready to review"
best="$(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); p=[x for x in r.get("ranking") or [] if x.get("pareto") and x.get("verdict")=="better"]; p.sort(key=lambda x: x["rel_change"]); print(p[0]["id"] if p else "")' "$out/result.json")"
if [[ -n "$best" && -f "$out/candidates/$best.patch" ]]; then
    sed 's/^/    /' "$out/candidates/$best.patch" | head -40
    echo
    say "Every candidate also has a Markdown report with rationale and reviewer comments: $out/candidates/$best.md"
    say "To apply it to your working copy (nothing has been changed so far):"
    echo "    git apply ${out#"$TOUR_REPO"/}/candidates/$best.patch"
else
    say "No candidate was significantly better this time – that happens; the LLM varies from run to run."
fi
look "the full report with all candidates, the Pareto chart and the measurements:"
echo "    $out/report.html"

section "The same through the server"
say "On a measuring host, CI or a developer can start the same run on the server and follow it in the web UI (it takes long, so do not wait for it):"
echo "    joulecode submit --mode optimize --no-wait"

learned "optimize = hotspots → LLM candidates → tests → maintainability review → measurement → combination." \
        "Only tested and measured candidates are offered; ★ marks the best trade-offs between energy and maintainability." \
        "Nothing is applied automatically – you review the patch and decide."
next "scripts/tour.sh 8 – prepare your own repository."
