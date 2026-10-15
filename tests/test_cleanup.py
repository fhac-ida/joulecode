from __future__ import annotations

import os
import time

import pytest

from joulecode import cleanup as cl


class FakeContainer:
    def __init__(self, cid, status):
        self.short_id, self.status, self.removed = cid, status, False

    def remove(self, force=False):
        self.removed = True


class FakeImage:
    def __init__(self, iid, tags, size):
        self.id = self.short_id = iid
        self.tags, self.attrs = tags, {"Size": size}


class FakeClient:
    def __init__(self):
        self.box = [FakeContainer("abc", "running"), FakeContainer("def", "exited")]
        self.imgs = [FakeImage("i1", ["joulecode/baseline:1"], 50e6)]
        self.removed_images, self.filters, self.pruned = [], [], False

        client = self

        class Containers:
            def list(self, all=False, filters=None):
                client.filters.append(filters)
                return client.box

        class Images:
            def list(self, filters=None):
                client.filters.append(filters)
                return client.imgs

            def remove(self, iid, force=False):
                client.removed_images.append(iid)

            def prune(self, filters=None):
                client.pruned = True

        self.containers, self.images = Containers(), Images()


def test_cleanup_removes_only_joulecode_things(tmp_path):
    temp = tmp_path / "tmp"
    (temp / "joulecode-abc").mkdir(parents=True)
    (temp / "joulecode-abc" / "f").write_bytes(b"x" * 1000)
    (temp / "other").mkdir()
    runs = tmp_path / "runs"
    old, new = runs / "old", runs / "new"
    old.mkdir(parents=True)
    new.mkdir()
    os.utime(old, (time.time() - 10 * 86400,) * 2)

    client = FakeClient()
    report = cl.cleanup(
        client=client,
        images=True,
        runs_older_than_days=7,
        runs_dir=runs,
        temp_root=temp,
        stop_metrion=lambda: True,
    )
    assert all(c.removed for c in client.box)
    assert all(f == {"label": "joulecode=1"} for f in client.filters)  # labelled objects only
    assert client.removed_images == ["i1"] and client.pruned
    assert not (temp / "joulecode-abc").exists() and (temp / "other").exists()
    assert not old.exists() and new.exists()
    assert report.metrion_stopped
    assert "Measurement containers removed: 2" in cl.render(report, dry_run=False)


def test_dry_run_changes_nothing(tmp_path):
    (tmp_path / "joulecode-x").mkdir()
    client = FakeClient()
    stopped = []
    report = cl.cleanup(
        client=client, temp_root=tmp_path, stop_metrion=lambda: stopped.append(1), dry_run=True
    )
    assert not any(c.removed for c in client.box) and not client.pruned
    assert (tmp_path / "joulecode-x").exists() and stopped == []
    assert "to remove" in cl.render(report, dry_run=True)


def test_lock_blocks_parallel_runs_and_cleanup(tmp_path, monkeypatch):
    from joulecode.cli import main

    monkeypatch.setattr(cl, "LOCK", tmp_path / "lock")
    assert cl.active_run() is None
    cl.acquire_lock("optimize")
    assert "optimize" in cl.active_run()
    assert main(["cleanup", "--dry-run"]) == 2  # run active → no cleaning up
    assert main(["measure", "whatever"]) == 2  # no second run
    cl.release_lock()
    assert cl.active_run() is None


def test_stale_lock_of_dead_process_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "LOCK", tmp_path / "lock")
    (tmp_path / "lock").write_text("999999999 optimize")
    assert cl.active_run() is None


@pytest.mark.parametrize("content", ["", "broken"])
def test_broken_lock_is_ignored(tmp_path, monkeypatch, content):
    monkeypatch.setattr(cl, "LOCK", tmp_path / "lock")
    (tmp_path / "lock").write_text(content)
    assert cl.active_run() is None
