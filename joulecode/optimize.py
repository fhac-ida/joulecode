"""Overall flow of `joulecode optimize`.

    build original → tests (mandatory) → static analysis + profiling → hotspots
    → per hotspot: the optimiser proposes candidates
        → apply, check syntax → tests (on failure: repair attempt)
        → ensure docstrings → maintainability reviewer (rules + LLM)
        → on veto: revision round with the reviewer's feedback
    → unload the LLM, wait for an idle GPU
    → measure all admitted candidates together with the original
    → Pareto selection energy vs. maintainability per hotspot
    → test and measure the combination of the best candidates of different hotspots
    → reports per candidate

LLM phases and measurement run strictly one after another.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from joulecode import prompts
from joulecode.agents import Candidate, Documenter, Optimizer, Reviewer, ReviewSettings, combine, materialize
from joulecode.analysis import StaticAnalysis
from joulecode.config import ProjectConfig
from joulecode.container import BuildFailed, Runner, image_tag
from joulecode.energy.base import EnergyBackend
from joulecode.lang import get_language
from joulecode.llm import LLM, LLMError, wait_for_idle_gpu
from joulecode.profiling import Hotspot, ProfileResult
from joulecode.protocol import MeasurementResult, measure, run_gate
from joulecode.variants import Variant, prepare_variants


@dataclass
class OptimizeResult:
    status: str  # ok | baseline_tests_failed | no_hotspots | no_candidates | measurement_failed
    started_at: str
    finished_at: str | None = None
    llm: str = ""
    language: str = "python"
    static: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    measurement: MeasurementResult | None = None
    ranking: list[dict] = field(default_factory=list)
    # combination of the best candidates of different hotspots (second measurement)
    combination: Candidate | None = None
    combination_measurement: MeasurementResult | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candidates"] = [c.to_dict() for c in self.candidates]
        d["combination"] = self.combination.to_dict() if self.combination else None
        return d


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _same_code(a: list[dict], b: list[dict], imports_a=(), imports_b=()) -> bool:
    """Same functions (name and code, whitespace ignored) and same imports?

    Imports count too: a repair that only adds the missing import (a typical compiler
    error in Java) is a real change.
    """

    def norm(fns: list[dict]) -> list[tuple[str, str]]:
        return sorted(
            (f["name"].replace("::", ".").rsplit(".", 1)[-1], " ".join(f["code"].split())) for f in fns
        )

    def imports(xs) -> set[str]:
        return {" ".join(str(x).split()).rstrip(";") for x in xs or ()}

    return norm(a) == norm(b) and imports(imports_a) == imports(imports_b)


def _tail(text: str, n: int = 60) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


class Pipeline:
    def __init__(
        self,
        cfg: ProjectConfig,
        runner: Runner,
        backend: EnergyBackend,
        llm: LLM,
        *,
        log=print,
        wait_gpu=wait_for_idle_gpu,
        on_checkpoint=None,
    ) -> None:
        self.cfg = cfg
        self.lang = get_language(cfg.language)
        # Called with the intermediate state as soon as all measurement runs are finished
        # (before the analysis) – so that nothing is lost if it aborts.
        self.on_checkpoint = on_checkpoint
        self.runner = runner
        self.backend = backend
        self.llm = llm
        self.log = log
        self.wait_gpu = wait_gpu
        self._counter = 0
        self._seen: dict[tuple, str] = {}  # comparison form → ID of the admitted candidate
        self._hotspots: list[Hotspot] = []

    # -- Develop a candidate -------------------------------------------------
    def _run_tests(self, cand: Candidate) -> bool:
        try:
            image = self.runner.build(
                cand.path / self.cfg.build.context, self.cfg.build.dockerfile, image_tag(cand.id, cand.path)
            )
        except BuildFailed as exc:
            # Compiled languages: a compiler error already shows up when building. It counts like
            # a red test, and the build output goes to the repair attempt.
            cand.tests_output = _tail(f"{exc}\n{exc.log}")
            return False
        box = self.runner.start(image, self.cfg.limits)
        try:
            res = box.exec(self.cfg.test.command, self.cfg.test.timeout_s, self.cfg.test.env)
        finally:
            box.remove()
        cand.tests_output = _tail(res.output)
        return res.ok

    def _develop(
        self,
        cand: Candidate,
        hotspot: Hotspot,
        baseline: Path,
        workdir: Path,
        static: StaticAnalysis,
        optimizer: Optimizer,
        reviewer: Reviewer,
        documenter: Documenter,
    ) -> Candidate:
        target = workdir / "candidates" / cand.id
        attempts = self.cfg.optimize.repair_attempts
        for attempt in range(attempts + 1):
            try:
                materialize(cand, baseline, target, self.lang)
            except (ValueError, KeyError, SyntaxError) as exc:
                cand.status = "invalid"
                cand.messages.append(f"Could not be applied: {exc}")
                self.log(f"{cand.id}: not applicable – {exc}")
                return cand
            self.log(f"{cand.id}: Tests …")
            if self._run_tests(cand):
                break
            cand.messages.append(f"Tests failed (attempt {attempt + 1}).")
            if attempt == attempts:
                cand.status = "tests_failed"
                self.log(f"{cand.id}: tests failed – discarded")
                return cand
            self.log(f"{cand.id}: tests failed – repair attempt")
            feedback = prompts.render(
                prompts.REPAIR_FEEDBACK,
                self.lang.prompts,
                title=cand.title,
                output=cand.tests_output[-3000:],
                code="\n\n".join(f["code"] for f in cand.functions),
            )
            repaired = optimizer.propose(hotspot, baseline, static, 1, feedback)
            if not repaired:
                cand.status = "tests_failed"
                self.log(f"{cand.id}: no repair received – discarded")
                return cand
            r = repaired[0]
            if _same_code(r["functions"], cand.functions, r.get("imports"), cand.imports):
                # Small models often repeat their mistake – another test run is pointless.
                cand.status = "tests_failed"
                cand.messages.append("Repair returned unchanged code – discarded.")
                self.log(f"{cand.id}: repair unchanged – discarded")
                return cand
            cand.functions, cand.imports, cand.origin = r["functions"], r.get("imports", []), "repair"
            cand.rationale = r.get("rationale", cand.rationale)
            cand.messages.append("Repaired by the optimiser.")

        cand.docs = documenter.ensure_docstrings(cand, baseline)
        cand.hotspot = self._hotspot_label(cand)
        # Same code as an already admitted candidate? Then do not review and measure it
        # twice (happens when the LLM rewrites the same functions in the same way for
        # another hotspot). The comparison ignores
        # formatting and comments – a different line break is not a different program.
        key = self._code_key(cand)
        same = self._seen.get(key)
        if same:
            cand.status = "duplicate"
            cand.messages.append(f"Identical to {same} – not measured again.")
            self.log(f"{cand.id}: identical to {same} – skipped")
            return cand
        cand.review = reviewer.review(cand, baseline)
        if cand.review["veto"]:
            cand.status = "vetoed"
            reasons = cand.review["rule_vetoes"] or (cand.review.get("llm") or {}).get("concerns", [])
            cand.messages.append("Reviewer veto: " + "; ".join(reasons))
        else:
            cand.status = "ready"
            self._seen[key] = cand.id
        self.log(f"{cand.id}: {cand.status}")
        return cand

    def _code_key(self, cand: Candidate) -> tuple:
        files = sorted({cand.file, *(f.get("file", cand.file) for f in cand.functions)})
        key = []
        for rel in files:
            source = (cand.path / rel).read_text(encoding="utf-8")
            try:
                key.append((rel, self.lang.canonical(source)))
            except (ValueError, SyntaxError):
                key.append((rel, source))
        return tuple(key)

    def _hotspot_label(self, cand: Candidate) -> str:
        """All hotspots the candidate changes – not only the one it was generated for.

        If the LLM also rewrites hotspot B for hotspot A, the candidate already is a
        combination: it belongs to the group “A + B” (its own Pareto front) and must not
        be combined with candidates for A or B.
        """
        changed = {(f.get("file", cand.file), f["name"]) for f in cand.functions}
        touched = [h.qualname for h in self._hotspots if (h.file, h.qualname) in changed]
        if cand.hotspot not in touched:
            touched.insert(0, cand.hotspot)
        order = {h.qualname: i for i, h in enumerate(self._hotspots)}
        return " + ".join(sorted(dict.fromkeys(touched), key=lambda q: order.get(q, -1)))

    def _new_candidate(self, proposal: dict, hotspot: Hotspot, round_: int) -> Candidate:
        self._counter += 1
        return Candidate(
            id=f"c{self._counter}",
            file=hotspot.file,
            hotspot=hotspot.qualname,
            title=proposal.get("title", "").strip() or f"Candidate {self._counter}",
            rationale=proposal.get("rationale", "").strip(),
            imports=proposal.get("imports", []),
            functions=proposal["functions"],
            round=round_,
            origin="initial" if round_ == 0 else "revision",
        )

    # -- Overall flow ---------------------------------------------------------
    def run(
        self,
        repo: Path,
        workdir: Path,
        *,
        baseline_ref: str | None = None,
        bench_env: dict[str, str] | None = None,
        measure_candidates: bool = True,
    ) -> OptimizeResult:
        cfg = self.cfg
        result = OptimizeResult(
            status="ok", started_at=_now(), llm=getattr(self.llm, "name", ""), language=self.lang.name
        )
        (baseline,) = prepare_variants(
            repo, workdir, protected_paths=cfg.protected_paths, baseline_ref=baseline_ref
        )

        self.log("Building original …")
        image = self.runner.build(
            baseline.path / cfg.build.context, cfg.build.dockerfile, image_tag("baseline", baseline.path)
        )
        gate = run_gate(self.runner, {"baseline": image}, cfg, self.log)[0]
        if not gate.ok:
            result.status, result.error = "baseline_tests_failed", gate.output_tail
            result.finished_at = _now()
            return result

        self.log("Static analysis …")
        static = self.lang.analyze_repo(baseline.path, cfg.protected_paths)
        result.static = static.to_dict()
        prof: ProfileResult = self.lang.profile(cfg, self.runner, image, baseline.path, log=self.log)
        result.profile = prof.to_dict()
        if not prof.ok or not prof.hotspots:
            result.status = "no_hotspots"
            result.error = prof.error or "No function above the threshold optimize.min_share."
            result.finished_at = _now()
            return result

        self._hotspots = list(prof.hotspots)
        internal = self.lang.internal_modules(static)
        optimizer = Optimizer(self.llm, self.log, self.lang)
        reviewer = Reviewer(
            self.llm, ReviewSettings(cfg.optimize.max_complexity_increase, internal), self.log, self.lang
        )
        documenter = Documenter(self.llm, self.log, self.lang)

        for hotspot in prof.hotspots:
            self.log(f"Hotspot {hotspot.qualname} ({hotspot.share:.0%} of self time)")
            feedback = ""
            for round_ in range(cfg.optimize.review_rounds + 1):
                proposals = optimizer.propose(
                    hotspot, baseline.path, static, cfg.optimize.candidates_per_hotspot, feedback
                )
                self.log(f"  Optimiser: {len(proposals)} candidate(s)")
                developed = [
                    self._develop(
                        self._new_candidate(p, hotspot, round_), hotspot, baseline.path, workdir,
                        static, optimizer, reviewer, documenter,
                    )
                    for p in proposals
                ]  # fmt: skip
                result.candidates.extend(developed)
                vetoed = [c for c in developed if c.status == "vetoed"]
                if any(c.status == "ready" for c in developed) or not vetoed:
                    break
                feedback = "".join(
                    prompts.render(
                        prompts.REVIEW_FEEDBACK,
                        self.lang.prompts,
                        title=c.title,
                        concerns="\n".join(
                            f"- {x}"
                            for x in c.review["rule_vetoes"]
                            or (c.review.get("llm") or {}).get("concerns", [])
                        ),
                        suggestion=(c.review.get("llm") or {}).get("suggestion", "–"),
                    )
                    for c in vetoed
                )

        ready = [c for c in result.candidates if c.status == "ready"]
        if not ready:
            result.status = "no_candidates"
            result.finished_at = _now()
            return result
        if not measure_candidates:
            result.finished_at = _now()
            return result

        # Never run the LLM and a measurement at the same time.
        try:
            self.llm.unload()
        except (LLMError, AttributeError) as exc:
            result.warnings.append(f"The LLM could not be unloaded: {exc}")
        if not self.wait_gpu():
            result.warnings.append("The GPU did not become idle before the measurement.")

        variants = [Variant("baseline", baseline.path)] + [Variant(c.id, c.path) for c in ready]

        def checkpoint(m: MeasurementResult) -> None:
            result.measurement = m
            if self.on_checkpoint:
                self.on_checkpoint(result)

        result.measurement = measure(
            cfg, variants, self.runner, self.backend, bench_env=bench_env, log=self.log, checkpoint=checkpoint
        )
        if result.measurement.status != "ok":
            result.status = "measurement_failed"
            result.error = result.measurement.error
        else:
            for c in ready:
                c.status = "measured"
            result.ranking = rank(ready, result.measurement)
            if cfg.optimize.combine:
                self._combine(result, baseline.path, workdir, bench_env)
        result.finished_at = _now()
        return result

    def _combine(self, result: OptimizeResult, baseline: Path, workdir: Path, bench_env) -> None:
        by_id = {c.id: c for c in result.candidates}
        parts, covered = select_combination(result.ranking, by_id)
        if len(parts) < 2:
            if covered:
                result.warnings.append(
                    f"No combination needed: {covered.id} already changes {covered.hotspot}."
                )
            return
        try:
            combo = combine(parts, baseline, workdir / "candidates" / "combined", lang=self.lang)
        except (ValueError, KeyError, SyntaxError) as exc:
            result.warnings.append(f"Combination not possible: {exc}")
            return
        result.combination = combo
        self.log("Combination: tests …")
        if not self._run_tests(combo):
            combo.status = "tests_failed"
            combo.messages.append("The combination fails the tests (interaction of the changes).")
            return
        combo.review = {
            "metrics": None,
            "rule_vetoes": [],
            "llm": None,
            "veto": False,
            "score": min(c.review.get("score", 3.0) for c in parts),
        }
        top = min(result.ranking, key=lambda r: r["rel_change"])
        variants = [
            Variant("baseline", baseline),
            Variant(top["id"], by_id[top["id"]].path),
            Variant(combo.id, combo.path),
        ]
        self.log(f"Combination: measuring against the original and the best single candidate ({top['id']}) …")

        def checkpoint(m: MeasurementResult) -> None:
            result.combination_measurement = m
            if self.on_checkpoint:
                self.on_checkpoint(result)

        m = measure(
            self.cfg,
            variants,
            self.runner,
            self.backend,
            bench_env=bench_env,
            log=self.log,
            checkpoint=checkpoint,
        )
        result.combination_measurement = m
        if m.status == "ok":
            combo.status = "measured"
            result.ranking += rank([combo], m)
            result.ranking.sort(key=lambda r: (r["hotspot"], not r["pareto"], r["rel_change"]))
        else:
            combo.status = "measurement_failed"
            combo.messages.append(m.error or "Measurement failed.")


def _changed(c: Candidate) -> set[tuple[str, str]]:
    return {(f.get("file", c.file), f["name"]) for f in c.functions}


def select_combination(
    ranking: list[dict], by_id: dict[str, Candidate]
) -> tuple[list[Candidate], Candidate | None]:
    """Candidates for the combination: the best per hotspot group, without shared functions.

    Greedy by savings: the most efficient Pareto candidate first, then every further one that changes
    no function already chosen. If a single candidate covers several hotspots (“A + B”),
    it displaces the single candidates for A and B. Second return value: such a candidate
    if it is the only one left (then no combination is needed).
    """
    best: dict[str, Candidate] = {}
    rows = sorted(
        (r for r in ranking if r["verdict"] == "better" and r["pareto"] and r["id"] in by_id),
        key=lambda r: r["rel_change"],
    )
    for row in rows:
        best.setdefault(by_id[row["id"]].hotspot, by_id[row["id"]])
    chosen: list[Candidate] = []
    taken: set[tuple[str, str]] = set()
    for row in rows:
        cand = by_id[row["id"]]
        if best.get(cand.hotspot) is cand and not (_changed(cand) & taken):
            chosen.append(cand)
            taken |= _changed(cand)
    covered = chosen[0] if len(chosen) == 1 and " + " in chosen[0].hotspot else None
    return chosen, covered


def rank(candidates: list[Candidate], measurement: MeasurementResult) -> list[dict]:
    """Energy change and maintainability score per candidate, Pareto front per hotspot among the significantly better ones."""
    rows = []
    for c in candidates:
        prim = measurement.comparison.get(c.id, {}).get("primary")
        if not prim:
            continue
        rows.append(
            {
                "id": c.id,
                "title": c.title,
                "rel_change": prim["rel_change"],
                "ci_low": prim["ci_low"],
                "ci_high": prim["ci_high"],
                "verdict": prim["verdict"],
                "maintainability": c.review.get("score", 3.0),
                "hotspot": c.hotspot,
                "pareto": False,
            }
        )
    # Candidates for different hotspots are not alternatives but complement
    # each other – the Pareto front is therefore determined per hotspot.
    for hotspot in {r["hotspot"] for r in rows}:
        better = [r for r in rows if r["verdict"] == "better" and r["hotspot"] == hotspot]
        for r in better:
            r["pareto"] = not any(_dominates(o, r) for o in better if o is not r)
    return sorted(rows, key=lambda r: (r["hotspot"], not r["pareto"], r["rel_change"]))


def _dominates(o: dict, r: dict) -> bool:
    """o dominates r: clearly more efficient (CIs do not overlap) with at least equal maintainability,
    or equally efficient (CIs overlap) with better maintainability. That way noise does not decide."""
    energy_better = o["ci_high"] < r["ci_low"]
    energy_tie = not energy_better and not r["ci_high"] < o["ci_low"]
    return (energy_better and o["maintainability"] >= r["maintainability"]) or (
        energy_tie and o["maintainability"] > r["maintainability"]
    )
