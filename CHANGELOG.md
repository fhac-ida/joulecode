# Changelog

## 0.2.0 (unreleased)

First public version.

- **English throughout:** CLI messages, reports, web UI, LLM prompts, code comments and examples.
  Result values changed accordingly: verdicts are `better` / `worse` / `no significant difference`,
  the combination candidate is called `combined` (reports in `measurement-combined/`).

- **Measurement chain:** variants, Docker sandbox, test gate, interleaved randomised blocks,
  bootstrap confidence intervals, Mann–Whitney U, adaptive stopping, plausibility checks and
  top-up measurements; back-ends `metrion`, `metrion-legacy`, `metrion-live`, `rapl`, `cgroup`.
- **Analysis:** static analysis with existing tools and hotspots by profiling.
- **Optimisation:** LLM agents (Ollama) for candidates, test repair, documentation and
  maintainability review; Pareto selection per hotspot and measured combinations.
- **Languages:** Python, PHP, JavaScript/TypeScript, Java.
- **Reports:** Markdown and a self-contained HTML report.
- **Platform:** `joulecode serve` / `joulecode submit` with the modes review, gate and optimize,
  web UI, SARIF output and CI templates.
- **Test suite:** twelve examples with expectations, holdout tests and reference patches.
- **Settings in `.joulecode.env`:** back-end (`JOULECODE_BACKEND`, new), model, METRION paths, server and
  client are configured once per machine; command-line options only override them.
  `scripts/install.sh` keeps your own entries when it is run again.
- **Default model `qwen2.5-coder:14b`** (was `7b`), because the 7B model rarely produces passing
  candidates for PHP, JavaScript and Java. Recommendations per GPU size in `docs/installation.md`.
- **Examples:** the Python examples are named `python-*` like those of the other languages.
