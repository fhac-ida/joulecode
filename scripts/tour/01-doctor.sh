#!/usr/bin/env bash
# Recipe 01 – check this machine with `joulecode doctor`.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
tour_init

title 01 "Check this machine" "1 min"
say "joule:code measures how much energy a program needs. How exactly it can measure depends on the machine: on a dedicated Linux measuring host it reads real energy counters, anywhere else it falls back to CPU time. 'doctor' tells you which case you are in."
pause

section "Run the check"
need_docker
echo "${D}\$${N} ${B}joulecode doctor${N}"
doctor_out
doctor_out | grep -q "✘" && warn "doctor reports a failed required check – fix the lines marked ✘ first."

if metrion_leftover; then
    section "A leftover from an aborted measurement"
    warn "A METRION service is still running although nothing is being measured – usually after a measurement was interrupted (Ctrl+C, server restart). It blocks the 'metrion' back-end, so 'auto' falls back to a less suitable one."
    say "Fix: ${B}joulecode cleanup --dry-run${N} shows what is left over, ${B}joulecode cleanup${N} removes it (only things joule:code created). Then run this recipe again."
fi

best="$(energy_backend)"
if [[ -z "$best" ]] && metrion_leftover; then
    best=blocked
elif [[ -z "$best" ]]; then
    best="$(best_backend)"
fi
section "What the result means for you"
case "$best" in
    blocked)
        ok "This is a measuring host with METRION – only blocked right now by the leftover service (see above)."
        say "Until 'joulecode cleanup' has run, measurements here would not use the right back-end." ;;
    metrion|metrion-legacy)
        ok "Back-end '$best': real CPU + DRAM energy, attributed to the measured container. This is a measuring host."
        say "Lines marked '!' for other back-ends (metrion-live, rapl …) are fine – they are alternatives you do not need." ;;
    rapl)
        ok "Back-end 'rapl': energy of the whole CPU socket minus idle. Real energy, but other processes on the machine add noise." ;;
    cgroup)
        warn "Back-end 'cgroup': this machine can only measure CPU time, not energy."
        say "That is enough to learn joule:code and to compare variants – every report will say 'CPU time (proxy)'. For real energy numbers see docs/installation.md (measuring host)." ;;
    *)
        warn "No back-end is ready – most likely Docker or cgroup v2 is missing (see the ✘ lines above)." ;;
esac
look "'CPU governor performance' and 'Turbo Boost off' – both reduce noise. Recommended on a measuring host, not required for the tour."

learned "joulecode doctor shows which measurement back-ends this machine supports." \
        "'--backend auto' (the default everywhere) picks the most accurate one that is ready." \
        "Without energy counters everything still works, with CPU time instead of energy."
next "scripts/tour.sh 2 – find the functions where a program spends its time."
