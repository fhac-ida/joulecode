#!/usr/bin/env bash
# Recipe 08 – prepare your own repository.
#
#   scripts/tour.sh 8              demo on a copy of an example without joule:code files
#   scripts/tour.sh 8 PATH         check your repository and suggest the missing files
#
# Your repository is only read. Files are written only if you agree, and never overwritten.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
target=""
for a in "$@"; do [[ "$a" == -* ]] || target="$a"; done
if [[ -n "$target" ]]; then
    [[ -d "$target" ]] || fail "Not a directory: $target"
    target="$(cd "$target" && pwd)"
fi
tour_init

title 08 "Prepare your own repository" "5–30 min, mostly writing a benchmark"
demo=0
if [[ -z "$target" ]]; then
    demo=1
    target="$TOUR_TMP/my-project"
    cp -r examples/python-wordstats "$target"
    rm "$target/Dockerfile" "$target/joulecode.yml"
    say "Demo mode: we prepare a copy of wordstats from which the joule:code files were removed – a project that has never seen joule:code. For your own repository: scripts/tour.sh 8 PATH"
else
    say "Checking $target – nothing is changed without asking."
fi
pause

section "1. What is in the repository?"
cd "$target"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    files="$(git ls-files -co --exclude-standard)"
else
    files="$(find . -type f -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/vendor/*' -not -path '*/target/*' -not -path '*/.venv/*' | sed 's|^\./||')"
fi
lang="$(python3 -c '
import os, sys
from collections import Counter
ext = {".py": "python", ".php": "php", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript", ".java": "java"}
c = Counter(ext[e] for e in (os.path.splitext(f)[1] for f in sys.stdin.read().splitlines()) if e in ext)
print(c.most_common(1)[0][0] if c else "")
' <<<"$files")"
[[ -n "$lang" ]] || fail "No Python, PHP, JavaScript/TypeScript or Java files found."
ok "Language: $lang"
has() { grep -qE "$1" <<<"$files"; }
check() { if has "$2"; then ok "$1"; else warn "$1 – missing"; fi; }
check "Dockerfile" '^Dockerfile$'
check "joulecode.yml" '^(joulecode|greencode)\.yml$'
check "Tests" '(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*\.py$|\.test\.[jt]s$|Test\.java$'
check "Benchmark (a folder bench/ or benchmark/)" '^(bench|benchmark)/'

case "$lang" in
    python)     example=python-wordstats; test_cmd="pytest -q -p no:cacheprovider tests"; bench_cmd="python bench/run.py";       protected="[tests, bench, joulecode.yml]" ;;
    php)        example=php-wordstats;  test_cmd="vendor/bin/phpunit --do-not-cache-result tests"; bench_cmd="php bench/run.php"; protected="[tests, bench, joulecode.yml, composer.json, composer.lock]" ;;
    javascript|typescript) example=js-wordstats; test_cmd="node --test tests/*.test.js"; bench_cmd="node bench/run.js"; protected="[tests, bench, joulecode.yml, package.json, package-lock.json]" ;;
    java)       example=java-wordstats; test_cmd="(see examples/java-wordstats/joulecode.yml)"; bench_cmd="java -cp out/main:out/bench Run"; protected="[src/test, bench, joulecode.yml, Dockerfile]" ;;
esac
pause

section "2. Dockerfile – everything the program needs, installed at build time"
say "Tests and benchmark run in containers WITHOUT network, so all dependencies must be installed during 'docker build'. A starting point from examples/$example:"
sed 's/^/    /' "$TOUR_REPO/examples/$example/Dockerfile"

section "3. joulecode.yml – how to test and how to load the program"
yml="$TOUR_TMP/joulecode.yml"
cat >"$yml" <<EOF
version: 1
language: $lang
test:
  command: $test_cmd
benchmark:
  command: $bench_cmd   # TODO: your own fixed, repeatable workload
  min_duration_s: 20                  # shorter runs are too noisy for energy measurement
  env:
    BENCH_ROUNDS: "40"                # scale the workload with a variable like this
protected_paths: $protected   # candidates cannot change these
EOF
sed 's/^/    /' "$yml"
say "Every other key has a sensible default – all of them: docs/configuration.md. Language specifics (Composer, npm, Maven/Gradle): docs/languages.md."

section "4. The benchmark – the part only you can write"
say "joule:code optimises towards the benchmark, so it decides what 'faster' means. A good benchmark:"
echo "  • calls the code the way it is really used (not just one helper function)"
echo "  • is deterministic: fixed input data, fixed seed, no network, no clock"
echo "  • runs at least ~20 s on the measuring host – scale it with a variable such as BENCH_ROUNDS"
echo "  • lives in a protected folder (bench/), so candidates cannot game it"
say "Example: examples/$example/bench/"
pause

section "5. Write the files?"
write_file() {   # write_file NAME SOURCE
    if [[ -e "$target/$1" ]]; then
        say "$1 exists – left unchanged."
    elif [[ $demo == 1 ]] || ask "Create $1 in $target?" n; then
        cp "$2" "$target/$1"
        ok "$1 created – adapt it (TODOs)."
    fi
}
write_file Dockerfile "$TOUR_REPO/examples/$example/Dockerfile"
write_file joulecode.yml "$yml"
[[ $demo == 1 ]] && sed -i 's|   # TODO: your own fixed, repeatable workload||' "$target/joulecode.yml"

if [[ -f "$target/joulecode.yml" && -f "$target/Dockerfile" ]]; then
    section "6. Check the setup"
    say "Step 1 – static analysis only, without Docker (seconds):"
    cd "$TOUR_REPO"
    set +e
    run joulecode analyze "$target" --no-profile --out "$TOUR_TMP/analysis"
    rc=$?
    set -e
    [[ $rc -eq 0 ]] && ok "The configuration is readable." || warn "analyze failed (exit $rc) – check joulecode.yml."
    if [[ $demo == 1 ]] && have_docker; then
        say "Step 2 – build the image, run the benchmark with the profiler:"
        run joulecode analyze "$target" --out "$TOUR_TMP/analysis2"
    fi
fi

section "Your checklist"
echo "  1. Dockerfile + joulecode.yml in the repository root, benchmark in bench/"
echo "  2. ${B}joulecode analyze PATH${N}          builds the image, profiles the benchmark → hotspots"
echo "  3. ${B}joulecode measure PATH --aa${N}     original against itself: must show no difference (recipe 4)"
echo "  4. ${B}joulecode optimize PATH${N}         candidates, tested and measured (recipe 7)"
echo "  5. ${B}joulecode submit${N} from CI          review on every PR, gate on demand (recipes 5 and 6)"

learned "joule:code needs a Dockerfile (dependencies at build time, no network at run time) and a joulecode.yml." \
        "The benchmark defines what gets optimised – deterministic, representative, ≥ 20 s on the measuring host." \
        "Check the setup step by step: analyze → measure --aa → optimize."
next "That was the tour. Reference: README.md and docs/."
