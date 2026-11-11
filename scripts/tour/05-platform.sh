#!/usr/bin/env bash
# Recipe 05 – the platform: start a server, create a project, submit reviews.
#
# Uses its own temporary server (port $TOUR_PORT, default 8799) with an empty data directory, so an
# existing `joulecode serve` and its projects are not touched. Everything is removed at the end.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
tour_init

title 05 "Platform: server, project, review" "3 min"
say "In a team you do not run joule:code by hand. 'joulecode serve' runs on the measuring host – a bit like a local SonarQube for energy. CI jobs send their code there with 'joulecode submit' and get the result back."
echo
echo "  ${B}review${N}    seconds   analysis findings for the changed functions"
echo "  ${B}gate${N}      minutes   energy of the pull request vs. the target branch (recipe 06)"
echo "  ${B}optimize${N}  hours     LLM candidates, tested and measured (recipe 07)"
pause

section "1. Start a server"
say "A temporary server just for the tour: own port, empty database, fixed admin token. '--processor review' makes reviews real and simulates gate/optimize – enough for this recipe."
start_tour_server review
say "On a real installation 'serve' prints the admin token on its first start and keeps it in ~/.joulecode/server/admin-token."

section "2. Create a project"
say "In the web UI: Access → paste the admin token → New project. Here the same via the REST API:"
echo "${D}\$${N} ${B}curl -X POST $TOUR_URL/api/projects -d '{\"key\": \"tour\"}'${N}"
create_tour_project tour
ok "Project 'tour' created. It gets its own token (gcp_…), shown only once – in CI it becomes the secret JOULECODE_TOKEN."
echo
say "submit reads three variables:"
echo "  export JOULECODE_SERVER=$JOULECODE_SERVER"
echo "  export JOULECODE_PROJECT=$JOULECODE_PROJECT"
echo "  export JOULECODE_TOKEN=${JOULECODE_TOKEN:0:8}…"
pause

section "3. A small Git repository with a pull request"
say "We copy wordstats into a throw-away Git repository: branch 'main' is the original, branch 'feature' adds one new function – written with an inefficient loop."
tour_repo "$TOUR_TMP/repo"
git switch -q -c feature
add_word_lengths() {
    git show main:src/wordstats/core.py >src/wordstats/core.py
    printf '\n\ndef word_lengths(text: str) -> list[int]:\n    """Length of every word."""\n%s\n' "$1" >>src/wordstats/core.py
}
add_word_lengths '    lengths = []
    for word in text.split():
        lengths.append(len(word))
    return lengths'
commit_all "add word_lengths"
run git --no-pager diff main --stat
pause

section "4. Review of the pull request"
say "--base main: only functions changed relative to main count. --fail-on major: the job fails if such code contains a 'major' finding (a known efficiency anti-pattern)."
set +e
run joulecode submit --base main --fail-on major --sarif "$TOUR_TMP/review.sarif"
rc=$?
set -e
echo
if [[ $rc -eq 1 ]]; then
    ok "Exit code 1 – exactly what makes a CI job red. Exit codes: 0 passed, 1 gate failed, 2 error."
else
    warn "Expected exit code 1, got $rc."
fi
say "The --sarif file can be uploaded to GitHub code scanning; the finding then appears directly in the pull request."
pause

section "5. Fix it and submit again"
add_word_lengths '    return [len(word) for word in text.split()]'
commit_all "list comprehension"
sed -n '/^def word_lengths/,$p' src/wordstats/core.py | sed 's/^/    /'
set +e
run joulecode submit --base main --fail-on major
rc=$?
set -e
[[ $rc -eq 0 ]] && ok "Exit code 0 – the pull request passes." || warn "Expected exit code 0, got $rc."
cd "$TOUR_REPO"

section "6. Look around in the web UI"
say "Open $TOUR_URL in your browser, choose Access and paste the admin token:"
echo "    $TOUR_ADMIN"
say "Under Projects → Tour you find both reviews; the project page also has ready-to-copy GitHub Actions and GitLab CI templates (Setup & token)."
if [[ "$TOUR_YES" != 1 ]]; then
    read -r -p "${D}  ⏎ when you are done – the temporary server is then stopped and deleted${N} " _ || true
fi

learned "joulecode serve = web UI + REST API + a queue that measures one job at a time." \
        "Every project has its own token; CI only needs JOULECODE_SERVER, _PROJECT and _TOKEN." \
        "review with --base checks only the changed functions; --fail-on turns findings into a failing exit code." \
        "Real setup and CI templates: docs/platform.md"
next "scripts/tour.sh 6 – does a pull request cost more energy? The energy gate."
