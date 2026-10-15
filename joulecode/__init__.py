"""joule:code – measure and improve the energy efficiency of software.

A containerised repository with a `joulecode.yml` is built in several variants (the
original and candidates), every variant must pass the same tests, and the benchmark is
measured repeatedly in interleaved blocks and compared statistically. On top of this
measurement chain sit profiling, static analysis, LLM agents that propose candidates,
and a small platform for CI.
"""

__version__ = "0.2.0.dev0"
