"""Prepare variants (original and candidate) as separate working copies."""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv", ".pytest_cache", "*.pyc", ".idea")


@dataclass
class Variant:
    name: str
    path: Path
    # notes, e.g. that protected paths were reset.
    notes: list[str] = field(default_factory=list)


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=IGNORE, symlinks=True)


def _export_git_ref(repo: Path, ref: str, dst: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", ref],
        check=True,
        capture_output=True,
    ).stdout
    dst.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dst, filter="data")


def _apply_patch(target: Path, patch: Path) -> None:
    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(patch.resolve())],
        cwd=target,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Patch {patch} cannot be applied:\n{result.stderr}")


def _restore_protected(baseline: Path, candidate: Variant, protected: list[str]) -> None:
    for rel in protected:
        src, dst = baseline / rel, candidate.path / rel
        if not src.exists() and not dst.exists():
            continue
        changed = _differs(src, dst)
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, ignore=IGNORE)
        elif src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        if changed:
            candidate.notes.append(f"Protected path '{rel}' was changed in the candidate and has been reset.")


def _differs(a: Path, b: Path) -> bool:
    if a.exists() != b.exists() or a.is_dir() != b.is_dir():
        return True
    if not a.exists():
        return False
    if a.is_file():
        return a.read_bytes() != b.read_bytes()
    files_a = {p.relative_to(a) for p in a.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    files_b = {p.relative_to(b) for p in b.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    if files_a != files_b:
        return True
    return any((a / f).read_bytes() != (b / f).read_bytes() for f in files_a)


def prepare_variants(
    repo: Path,
    workdir: Path,
    *,
    protected_paths: list[str],
    baseline_ref: str | None = None,
    candidate_dir: Path | None = None,
    candidate_patch: Path | None = None,
) -> list[Variant]:
    """Create `workdir/baseline` and (if specified) `workdir/candidate`.

    Without a candidate only the original is prepared (e.g. to check the measurement chain
    itself: A/A comparison).
    """
    if candidate_dir and candidate_patch:
        raise ValueError("Specify either --candidate-dir or --candidate-patch, not both.")

    workdir.mkdir(parents=True, exist_ok=True)
    baseline = Variant("baseline", workdir / "baseline")
    if baseline_ref:
        _export_git_ref(repo, baseline_ref, baseline.path)
    else:
        _copy_tree(repo, baseline.path)
    variants = [baseline]

    if candidate_dir or candidate_patch:
        candidate = Variant("candidate", workdir / "candidate")
        if candidate_dir:
            _copy_tree(candidate_dir, candidate.path)
        else:
            _copy_tree(baseline.path, candidate.path)
            _apply_patch(candidate.path, candidate_patch)
        _restore_protected(baseline.path, candidate, protected_paths)
        variants.append(candidate)

    return variants
