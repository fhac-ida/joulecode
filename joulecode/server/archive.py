"""Pack a repository (client) and unpack it safely (server).

Packing: in a Git repository, the files Git knows or would know count
(`git ls-files -co --exclude-standard`) – i.e. the working tree including
uncommitted changes, but without files matched by `.gitignore`. Locally that is what the
developer sees; in CI it is exactly the checked-out commit. The state of the
target branch (for `gate`) comes via `git archive <commit>`.
Without Git, the usual directories (.git, node_modules, .venv …) are left out.

Unpacking: the archive comes over the network and is not trustworthy. Only
regular files and directories inside the target directory are created –
no absolute paths, no `..`, no links, no devices – and number and
total size are limited (archive bombs).
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".idea", ".vscode", "joulecode-runs", "suite-runs", ".gradle",
    "target", "build", "dist",
}  # fmt: skip

MAX_FILES = 50_000
MAX_EXTRACTED_BYTES = 1024**3  # 1 GiB


class ArchiveError(ValueError):
    pass


# -- Packing (client) -----------------------------------------------------------------
def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120, check=False
    )
    if check and r.returncode != 0:
        raise ArchiveError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def is_git_repo(repo: Path) -> bool:
    try:
        return git(repo, "rev-parse", "--is-inside-work-tree", check=False) == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def list_files(repo: Path) -> list[str]:
    """Relative POSIX paths of the files that belong to the repository."""
    repo = repo.resolve()
    if is_git_repo(repo):
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=120,
            check=True,
        ).stdout.decode("utf-8", "surrogateescape")
        files = [f for f in out.split("\0") if f and (repo / f).is_file()]
        return sorted(set(files))
    files = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel_root = Path(root).relative_to(repo)
        files.extend((rel_root / n).as_posix() for n in sorted(names) if (Path(root) / n).is_file())
    return files


def pack_worktree(repo: Path) -> bytes:
    """Working tree as tar.gz (in memory)."""
    repo = repo.resolve()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in list_files(repo):
            path = repo / rel
            info = tar.gettarinfo(str(path), arcname=rel)
            if not info.isfile():  # the server does not allow symlinks and the like anyway
                continue
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as f:
                tar.addfile(info, f)
    return buf.getvalue()


def pack_commit(repo: Path, commit: str) -> bytes:
    """State of a commit as tar.gz (`git archive`)."""
    r = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar.gz", commit],
        capture_output=True,
        timeout=300,
        check=False,
    )
    if r.returncode != 0:
        raise ArchiveError(f"git archive {commit}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


# -- Unpacking (server) -----------------------------------------------------------------
def safe_extract(archive: Path, dest: Path) -> int:
    """Unpack `archive` (tar, tar.gz) to `dest`; return the number of files."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    total = 0
    try:
        with tarfile.open(archive, mode="r:*") as tar:
            for member in tar:
                name = _safe_name(member.name)
                if name is None:
                    continue  # "." or empty name
                if member.isdir():
                    (dest / name).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ArchiveError(f"entry not allowed (link/device): {member.name}")
                count += 1
                total += member.size
                if count > MAX_FILES:
                    raise ArchiveError(f"more than {MAX_FILES} files")
                if total > MAX_EXTRACTED_BYTES:
                    raise ArchiveError(f"larger than {MAX_EXTRACTED_BYTES // 1024**2} MB when unpacked")
                target = (dest / name).resolve()
                if not target.is_relative_to(dest):
                    raise ArchiveError(f"path outside the target directory: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    continue
                with src, target.open("wb") as out:
                    while chunk := src.read(1024 * 1024):
                        out.write(chunk)
                if member.mode & 0o111:
                    target.chmod(0o755)
    except tarfile.TarError as e:
        raise ArchiveError(f"not a valid tar archive: {e}") from e
    return count


def _safe_name(name: str) -> str | None:
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        raise ArchiveError(f"absolute path in the archive: {name}")
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ArchiveError(f"'..' in the archive path: {name}")
    return "/".join(parts) or None
