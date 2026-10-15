"""Building the variants and running them in isolation in Docker containers.

Every test and every benchmark run gets a fresh container:
no network, CPU/RAM/PID limits, no capabilities. The container
starts with `sleep infinity`; the actual command runs inside it via `docker exec`.
That way the process lives in the container's cgroup (important for the
attribution by metrion) and the CPU time can be read before/after.

Dependencies are installed during the build (with network); at runtime
there is none.
"""

from __future__ import annotations

import contextlib
import hashlib
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from joulecode.config import LimitsConfig

LABEL = "joulecode"


class BuildFailed(RuntimeError):
    """The image could not be built – for compiled languages usually a compiler error.

    `log` contains the end of the build output; the pipeline passes it on to the repair attempt
    like a test output.
    """

    def __init__(self, message: str, log: str = "") -> None:
        super().__init__(message)
        self.log = log


@dataclass
class ExecResult:
    exit_code: int
    output: str
    wall_s: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def timed_out(self) -> bool:
        # `timeout -s KILL` exits with 137.
        return self.exit_code == 137


class Sandbox(Protocol):
    id: str

    def exec(self, command: str, timeout_s: int, env: dict[str, str] | None = None) -> ExecResult: ...

    def cpu_seconds(self) -> float | None: ...

    def remove(self) -> None: ...


class Runner(Protocol):
    def build(self, context: Path, dockerfile: str, tag: str) -> str: ...

    def start(self, image: str, limits: LimitsConfig) -> Sandbox: ...


def image_tag(variant_name: str, path: Path) -> str:
    digest = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(f.relative_to(path)).encode())
        digest.update(f.read_bytes())
    return f"joulecode/{variant_name}:{digest.hexdigest()[:12]}"


def _wrap(command: str, timeout_s: int) -> list[str]:
    inner = f"exec sh -c {shlex.quote(command)}"
    # `timeout` from coreutils is available in Debian/Alpine base images;
    # if not, the command runs without a hard limit.
    script = (
        f"if command -v timeout >/dev/null 2>&1; then "
        f"exec timeout -s KILL {int(timeout_s)} sh -c {shlex.quote(command)}; "
        f"else {inner}; fi"
    )
    return ["sh", "-c", script]


# Reads the cumulative CPU time of its own cgroup (v2 or v1) in seconds.
_CPU_SCRIPT = (
    "if [ -f /sys/fs/cgroup/cpu.stat ]; then "
    "awk '/^usage_usec/ {printf \"%.6f\", $2/1e6}' /sys/fs/cgroup/cpu.stat; "
    "elif [ -f /sys/fs/cgroup/cpuacct/cpuacct.usage ]; then "
    "awk '{printf \"%.6f\", $1/1e9}' /sys/fs/cgroup/cpuacct/cpuacct.usage; "
    "fi"
)


class DockerSandbox:
    def __init__(self, container) -> None:
        self._c = container
        self.id: str = container.id

    def exec(self, command: str, timeout_s: int, env: dict[str, str] | None = None) -> ExecResult:
        start = time.perf_counter()
        result = self._c.exec_run(_wrap(command, timeout_s), environment=env or {}, demux=False)
        wall = time.perf_counter() - start
        output = (result.output or b"").decode("utf-8", errors="replace")
        return ExecResult(result.exit_code, output, wall)

    def cpu_seconds(self) -> float | None:
        out = self._c.exec_run(["sh", "-c", _CPU_SCRIPT]).output.decode().strip()
        try:
            return float(out)
        except ValueError:
            return None

    def remove(self) -> None:
        # Cleaning up must not abort the run.
        with contextlib.suppress(Exception):
            self._c.remove(force=True)


class DockerRunner:
    def __init__(self, client=None) -> None:
        if client is None:
            import docker

            client = docker.from_env()
        self._client = client

    def build(self, context: Path, dockerfile: str, tag: str) -> str:
        # Always build: the tag only hashes the repository content, not the base image.
        # Docker caches unchanged layers anyway, so this costs hardly any time.
        import docker

        try:
            image, _ = self._client.images.build(
                path=str(context), dockerfile=dockerfile, tag=tag, rm=True, labels={LABEL: "1"}
            )
        except docker.errors.BuildError as exc:
            lines = []
            for chunk in exc.build_log or []:
                text = chunk.get("stream") or chunk.get("error") or ""
                lines.append(text)
            log = "".join(lines)
            raise BuildFailed(f"Image {tag} could not be built: {exc.msg}", log[-4000:]) from exc
        # The image ID (not the tag) starts the containers so that both
        # variants are guaranteed to run on the state just built.
        return image.id

    def tag(self, image: str, tag: str) -> str:
        """Give an image (by ID) a name, e.g. as the base for a derived image."""
        repo, _, name = tag.rpartition(":")
        self._client.images.get(image).tag(repo, name)
        return tag

    def start(self, image: str, limits: LimitsConfig) -> DockerSandbox:
        container = self._client.containers.run(
            image,
            # Overrides an ENTRYPOINT possibly set in the image.
            entrypoint=["sleep", "infinity"],
            detach=True,
            network_mode="none",
            nano_cpus=int(limits.cpus * 1e9),
            mem_limit=limits.memory,
            memswap_limit=limits.memory,
            cpuset_cpus=limits.cpuset,
            pids_limit=limits.pids,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            labels={LABEL: "1"},
        )
        return DockerSandbox(container)
