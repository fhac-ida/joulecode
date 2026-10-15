"""Prompts and JSON schemas of the agents.

The texts are templates: language-specific parts (language, code fence, examples,
name of the linter) come from the `LanguagePrompts` of the respective back-end,
see `render`.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from joulecode.lang.base import LanguagePrompts


def render(template: str, lang: LanguagePrompts, **values) -> str:
    """Fill a template with the building blocks of the language and further values."""
    return template.format(**asdict(lang), **values)


# -- Optimiser ------------------------------------------------------------------
OPTIMIZER_SYSTEM = """You are an experienced {language} performance engineer. The goal is lower energy consumption \
(less CPU work, fewer memory allocations) with UNCHANGED observable behaviour and \
GOOD maintainability. Rules:
- Signature (name, parameters, defaults) and return type stay the same.
- The return value stays EXACTLY the same: same structure, same element types, same order.
- No new dependencies outside {stdlib}.
{idioms}
- Prefer algorithmic improvements (complexity class, data structures) over micro-optimisations.
- {function_hint}
Answer exclusively with JSON according to the schema."""

OPTIMIZER_PROMPT = """Optimise the hotspot `{qualname}` in `{file}`.

Profiling of the benchmark: {share:.0%} of the total self time, {calls}, \
self time {tottime:.3f} s, including sub-calls {cumtime:.3f} s.
Cyclomatic complexity: {complexity}.
Findings of static tools ({lint_tool}) in this function:
{hints}

Hotspot:
```{fence}
{code}
```

Context – the whole file `{file}` (you may also change other functions of THIS file \
if that is necessary for the optimisation):
```{fence}
{context}
```
{feedback}
Generate {n} DIFFERENT candidates (different approaches, not variants of the same one).
For each candidate: a short title, a rationale why it needs less energy, \
required additional imports (complete statements such as {import_example}, \
not just the module name; an empty list if none are needed) and the changed functions \
(qualified name as in the original, e.g. {qualname_example}, and complete code)."""

OPTIMIZER_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "imports": {"type": "array", "items": {"type": "string"}},
                    "functions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "code": {"type": "string"}},
                            "required": ["name", "code"],
                        },
                    },
                },
                "required": ["title", "rationale", "imports", "functions"],
            },
        }
    },
    "required": ["candidates"],
}

REPAIR_FEEDBACK = """
ATTENTION – an earlier candidate ("{title}") did NOT pass the tests. Test output:
```
{output}
```
Your code at the time:
```{fence}
{code}
```
Compare expected (-) and actual (+) in the test output and fix exactly \
this deviation – including structure and type of the return value. Do NOT return the same code again. \
The behaviour must match the original exactly.
"""

REVIEW_FEEDBACK = """
The maintainability reviewer rejected an earlier candidate ("{title}"):
{concerns}
Reviewer's suggestion: {suggestion}
Take this into account in the new candidates.
"""

# -- Maintainability reviewer ---------------------------------------------------
REVIEWER_SYSTEM = """You are a strict code reviewer focused on maintainability: readability, \
comprehensibility, testability, idiomatic {language}, appropriate documentation. Energy efficiency is \
NOT your task – another process measures it. Assess whether the change makes maintainability \
worse compared with the original. Veto only if it gets clearly worse \
(e.g. hard-to-understand trick code, lost documentation, hidden side effects). \
Answer exclusively with JSON according to the schema."""

REVIEWER_PROMPT = """File `{file}`. Candidate "{title}". The optimiser's rationale: {rationale}

Original:
```{fence}
{before}
```

Candidate:
```{fence}
{after}
```

Measured metrics: {metrics}

Rate the maintainability of the candidate relative to the original on a scale of 1–5 \
(1 = clearly worse, 3 = equal, 5 = clearly better), name concrete concerns \
and one suggestion for improvement. veto = true only if it gets clearly worse.
Answer BRIEFLY: at most 3 concerns of one short sentence each, the suggestion in one sentence."""

REVIEWER_RETRY = """

Your previous answer was too long and was cut off. Now answer extremely briefly
(concerns as keywords, suggestion at most 15 words)."""

REVIEWER_SCHEMA = {
    "type": "object",
    "properties": {
        "maintainability": {"type": "integer", "minimum": 1, "maximum": 5},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "suggestion": {"type": "string"},
        "veto": {"type": "boolean"},
    },
    "required": ["maintainability", "concerns", "suggestion", "veto"],
}

# -- Documentation --------------------------------------------------------------
DOC_SYSTEM = """You write concise, precise {language} documentation ({doc_term}, same natural language as the rest \
of the code). Describe WHAT the function does, its parameters and return value – not the implementation in detail. \
Answer exclusively with JSON according to the schema."""

DOC_PROMPT = """Write a {doc_term} for this function from `{file}` ({doc_format}):
```{fence}
{code}
```
Example of the style of other {doc_term}s in the file: {style}"""

DOC_SCHEMA = {
    "type": "object",
    "properties": {"docstring": {"type": "string"}},
    "required": ["docstring"],
}
