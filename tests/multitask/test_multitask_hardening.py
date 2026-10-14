# =============================================================================
# Copyright (c) 2026 KritvaOS
# SPDX-License-Identifier: Apache-2.0
#
# File        : test_multitask_hardening.py
# Description : Multi-task dataset hardening tests (KF-DQ-013.1)
#
# Component   : Kritva Forge
# Module      : tests/multitask
# Layer       : Test
#
# Author      : Kritva Forge Team
# Created     : 02-10-2026
# =============================================================================
"""KF-DQ-013.1: fail-closed record validation in every consumer (AC-010 .. AC-020)
and recoverable directory-level publication (AC-030 .. AC-036)."""

import copy
import json
import os
import shutil
from pathlib import Path

import pytest

from scripts.core import data_manifest as DM
from scripts.core import stale_artifacts as S
from scripts.multitask import build as B
from scripts.multitask import registry as G
from scripts.multitask import validator as V
from tests.multitask.test_multitask import _make_data_root, _run  # shared fixture corpus


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return _run(_make_data_root(tmp_path_factory.mktemp("multitask_hardening")))


@pytest.fixture
def scratch(corpus, tmp_path):
    dst = tmp_path / "copy" / "kritva-forge-data"
    shutil.copytree(corpus.root, dst)
    return dst


def _split_path(root, split="train"):
    return Path(root) / G.OUTPUT_DIR / f"{split}.jsonl"


def _valid_record(root):
    return json.loads(_split_path(root).read_text().splitlines()[0])


def _with_line(root, line: str, after_valid: bool = True):
    """Replace the train split with [one valid record +] the given line."""
    path = _split_path(root)
    first = path.read_text().splitlines(keepends=True)[0]
    path.write_text((first if after_valid else "") + line + "\n")


def _tree_bytes(path: Path) -> dict:
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def _hidden(root):
    return sorted(p.name for p in (Path(root) / G.OUTPUT_DIR).parent.iterdir() if p.name.startswith("."))


# -----------------------------------------------------------------------------
# Typed shape matrix (AC-010, AC-011, AC-019) against record_type_problems and check()
# -----------------------------------------------------------------------------

def _mutations():
    def setk(path, value):
        def f(r):
            node = r
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = value
            return r
        return f

    def drop(key):
        def f(r):
            del r[key]
            return r
        return f

    def extra(r):
        r["extra"] = 1
        return r

    def first_source(value):
        def f(r):
            r["sources"][0] = value
            return r
        return f

    def source_sha(r):
        r["sources"][0]["sha256"] = 5
        return r

    return [
        ("missing key", drop("versions"), "record fields"),
        ("extra key", extra, "record fields"),
        ("schema not object", setk(["schema"], "kritva"), "record.schema: expected an object"),
        ("task not object", setk(["task"], "interface_extraction"), "record.task: expected an object"),
        ("module not object", setk(["module"], ["ipa"]), "record.module: expected an object"),
        ("input not object", setk(["input"], 3), "record.input: expected an object"),
        ("task.id integer", setk(["task", "id"], 7), "record.task.id: expected a string"),
        ("task.version string", setk(["task", "version"], "1"), "record.task.version: expected an integer"),
        ("task.version boolean", setk(["task", "version"], True), "record.task.version: expected an integer"),
        ("module.name null", setk(["module", "name"], None), "record.module.name: expected a string"),
        ("input.text list", setk(["input", "text"], ["x"]), "record.input.text: expected a string"),
        ("input.kind unknown", setk(["input", "kind"], "verilog"), "record.input.kind: expected one of"),
        ("target not object", setk(["target"], "json"), "record.target: expected an object"),
        ("target.kind unknown", setk(["target", "kind"], "yaml"), "record.target.kind: expected one of"),
        ("json value not object", setk(["target"], {"kind": "json", "projection": "p", "value": [1]}),
         "record.target.value: expected an object"),
        ("text target text not string", setk(["target"], {"kind": "rtl", "text": 1}), "record.target.text: expected a string"),
        ("sources not list", setk(["sources"], {"a": 1}), "record.sources: expected a list"),
        ("source not object", first_source("raw/rtl/x.sv"), "record.sources[0]: expected an object"),
        ("source sha non-string", source_sha, "record.sources[0].sha256: expected a string"),
        ("versions not object", setk(["versions"], [1]), "record.versions: expected an object"),
        ("versions value list", setk(["versions", "dataset_schema"], [2]), "record.versions.dataset_schema: expected a string or an integer"),
        ("versions value object", setk(["versions", "dataset_schema"], {"v": 2}), "record.versions.dataset_schema: expected a string or an integer"),
        ("versions value boolean", setk(["versions", "dataset_schema"], True), "record.versions.dataset_schema: expected a string or an integer"),
        ("record_id integer", setk(["record_id"], 12), "record.record_id: expected a string"),
        ("split null", setk(["split"], None), "record.split: expected a string"),
    ]


MUTATIONS = _mutations()


def test_valid_records_have_no_type_problems(corpus):
    for s in G.SPLITS:
        for line in _split_path(corpus.root, s).read_text().splitlines():
            assert V.record_type_problems(json.loads(line)) == []


@pytest.mark.parametrize("value", [[1, "a"], "text", 3, 2.5, True, None], ids=["array", "string", "int", "float", "bool", "null"])
def test_non_object_record(scratch, value):
    assert V.record_type_problems(value) == [f"record: expected an object, got {type(value).__name__}"]
    _with_line(scratch, json.dumps(value))
    rep = V.check(scratch, authorize=False)
    assert rep["status"] == "FAIL"
    assert any(p.startswith("train.jsonl:2: record: expected an object") for p in rep["problems"]), rep["problems"][:5]
    assert rep["dataset_identity"] is None
    assert not any(p.startswith("internal validation error") for p in rep["problems"])


@pytest.mark.parametrize("name,mutate,needle", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_type_matrix(scratch, name, mutate, needle):
    rec = mutate(copy.deepcopy(_valid_record(scratch)))
    problems = V.record_type_problems(rec)
    assert any(needle in p for p in problems), (name, problems)
    _with_line(scratch, json.dumps(rec, sort_keys=True))
    rep = V.check(scratch, authorize=False)
    assert rep["status"] == "FAIL"
    assert any(p.startswith("train.jsonl:2: ") and needle in p for p in rep["problems"]), (name, rep["problems"][:5])
    assert not any(p.startswith("internal validation error") for p in rep["problems"])
    assert rep["dataset_identity"] is None


def test_invalid_json_line(scratch):
    _with_line(scratch, '{"record_id": ')
    rep = V.check(scratch, authorize=False)
    assert rep["status"] == "FAIL" and any(p.startswith("train.jsonl:2: invalid JSON") for p in rep["problems"])
    assert rep["dataset_identity"] is None


def test_non_utf8_file(scratch):
    _split_path(scratch).write_bytes(b'{"a": "\xff\xfe"}\n')
    rep = V.check(scratch, authorize=False)
    assert rep["status"] == "FAIL" and any(p.startswith("train.jsonl: not valid UTF-8") for p in rep["problems"])
    assert rep["dataset_identity"] is None


def test_internal_error_guard_is_a_distinct_class(scratch, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("/secret/local/path")
    monkeypatch.setattr(V, "_check", boom)
    rep = V.check(scratch, authorize=False)
    assert rep["status"] == "FAIL" and rep["problems"] == ["internal validation error: RuntimeError"]
    assert "/secret/local/path" not in json.dumps(rep)
    assert "/secret/local/path" in capsys.readouterr().err


def test_dataset_identity_never_raises():
    assert V.dataset_identity({"train": ["[1]\n"]}) is None
    assert V.dataset_identity({"train": ["{bad\n"]}) is None
    assert V.dataset_identity({"train": ['{"task": "x"}\n']}) is None


# -----------------------------------------------------------------------------
# Consumer boundaries (AC-017, AC-018, AC-019a)
# -----------------------------------------------------------------------------

def _regressions(root):
    rec = _valid_record(root)
    task_str = dict(rec, task="interface_extraction")
    module_list = dict(rec, module=["ipa", "ctrl"])
    target_str = dict(rec, target="json")
    return {"array": "[1, 2]", "task string": json.dumps(task_str, sort_keys=True),
            "module not object": json.dumps(module_list, sort_keys=True),
            "target not object": json.dumps(target_str, sort_keys=True)}


@pytest.mark.parametrize("case", ["array", "task string", "module not object", "target not object", "non-utf8"])
def test_consumers_fail_safely(scratch, case):
    if case == "non-utf8":
        _split_path(scratch).write_bytes(_split_path(scratch).read_bytes() + b"\xff\n")
    else:
        _with_line(scratch, _regressions(scratch)[case], after_valid=True)  # malformed line after a valid record
    rel = f"{G.OUTPUT_DIR}/train.jsonl"
    # validator
    rep = V.check(scratch, authorize=False)
    assert rep["status"] == "FAIL" and rep["dataset_identity"] is None
    assert not any(p.startswith("internal validation error") for p in rep["problems"])
    # stale gate
    entry = next(e for e in S.classify(scratch)["entries"] if e["path"] == rel)
    assert entry["state"] == "STALE"
    assert entry["reason"] in ("malformed multi-task record", "unreadable multi-task dataset file")
    # data manifest: the summary never raises and its identity is null; a manifest built from it fails
    # the check; the manifest build itself refuses (ManifestBlocked) naming the malformed file; the
    # stored manifest no longer matches and FAILs
    summary = DM._multitask_summary(Path(scratch))
    assert summary["dataset_identity"] is None
    with pytest.raises(DM.ManifestBlocked, match=r"datasets/multitask/v2/train\.jsonl"):
        DM.build(scratch)
    stored = json.loads((Path(scratch) / "manifests/data_manifest.json").read_text())
    forged = copy.deepcopy(stored)
    forged["multitask"] = summary
    drep = DM.check(scratch, manifest=forged)
    assert drep["status"] == "FAIL" and any("multitask: invalid dataset identity" in p for p in drep["problems"])
    assert DM.check(scratch)["status"] == "FAIL"


def test_valid_data_unchanged_by_consumers(corpus):
    rep = V.check(corpus.root, authorize=False)
    assert rep["status"] == "PASS" and rep["dataset_identity"].startswith("ds2:")
    assert DM._multitask_summary(Path(corpus.root))["dataset_identity"] == rep["dataset_identity"]


# -----------------------------------------------------------------------------
# Recoverable directory-level publication (AC-030 .. AC-036)
# -----------------------------------------------------------------------------

def _published(root):
    return _tree_bytes(Path(root) / G.OUTPUT_DIR)


def test_f_normal_rebuild_identical_no_hidden(scratch):
    before = _published(scratch)
    B.write(scratch)
    assert _published(scratch) == before and _hidden(scratch) == []


def test_a_failure_while_staging(scratch, monkeypatch):
    before = _published(scratch)
    real = Path.write_bytes

    def fail(self, data):
        if ".v2.tmp-" in str(self) and self.name == "test.jsonl":
            raise OSError("disk full")
        return real(self, data)
    monkeypatch.setattr(Path, "write_bytes", fail)
    with pytest.raises(OSError):
        B.write(scratch)
    assert _published(scratch) == before and _hidden(scratch) == []


def test_i_invalid_staged_generation(scratch, monkeypatch):
    before = _published(scratch)
    real = B._verify_staged

    def corrupt(stage, root, files, counts):
        (stage / "train.jsonl").write_bytes(b"[1]\n")
        return real(stage, root, files, counts)
    monkeypatch.setattr(B, "_verify_staged", corrupt)
    with pytest.raises(B.BuildError, match="differs from the serialized dataset"):
        B.write(scratch)
    assert _published(scratch) == before and _hidden(scratch) == []


def test_b_failure_between_renames_then_recovery(scratch, monkeypatch):
    before = _published(scratch)
    real = os.rename

    def fail(src, dst):
        if ".v2.tmp-" in str(src):
            raise OSError("interrupted")
        return real(src, dst)
    monkeypatch.setattr(os, "rename", fail)
    with pytest.raises(B.BuildError, match="publication interrupted"):
        B.write(scratch)
    assert not (Path(scratch) / G.OUTPUT_DIR).exists()
    old = [h for h in _hidden(scratch) if h.startswith(".v2.old-")]
    assert len(old) == 1
    assert _tree_bytes((Path(scratch) / G.OUTPUT_DIR).parent / old[0]) == before
    monkeypatch.setattr(os, "rename", real)
    B.write(scratch)
    assert _published(scratch) == before and _hidden(scratch) == []
    assert V.check(scratch, authorize=False)["status"] == "PASS"


def test_h_old_generation_cleanup_failure(scratch, monkeypatch, capsys):
    before = _published(scratch)
    real = shutil.rmtree

    def fail(path, *a, **k):
        if ".v2.old-" in str(path):
            raise OSError("busy")
        return real(path, *a, **k)
    monkeypatch.setattr(shutil, "rmtree", fail)
    B.write(scratch)
    assert "[WARN]" in capsys.readouterr().out
    assert _published(scratch) == before
    assert any(h.startswith(".v2.old-") for h in _hidden(scratch))
    rep = V.check(scratch, authorize=False)
    assert rep["status"] == "FAIL" and any("unmanaged" in p for p in rep["problems"])
    monkeypatch.setattr(shutil, "rmtree", real)
    B.write(scratch)
    assert _published(scratch) == before and _hidden(scratch) == []


def test_c_failure_after_publication_step(scratch, monkeypatch):
    """Crash after step 2: the new v2 is complete; the next write removes the old directory."""
    before = _published(scratch)
    parent = (Path(scratch) / G.OUTPUT_DIR).parent
    shutil.copytree(parent / "v2", parent / ".v2.old-999")
    B.write(scratch)
    assert _published(scratch) == before and _hidden(scratch) == []


def test_d_leftover_staging_removed(scratch):
    before = _published(scratch)
    parent = (Path(scratch) / G.OUTPUT_DIR).parent
    (parent / ".v2.tmp-123").mkdir()
    (parent / ".v2.tmp-123" / "train.jsonl").write_text("partial")
    rep = V.check(scratch, authorize=False)
    assert rep["status"] == "FAIL" and any("unmanaged" in p for p in rep["problems"])
    B.write(scratch)
    assert _published(scratch) == before and _hidden(scratch) == []


@pytest.mark.parametrize("case", ["e", "j"])
def test_e_j_ambiguous_recovery_keeps_candidates(scratch, case):
    parent = (Path(scratch) / G.OUTPUT_DIR).parent
    os.rename(parent / "v2", parent / ".v2.old-1")
    shutil.copytree(parent / ".v2.old-1", parent / ".v2.old-2")
    with pytest.raises(B.BuildError, match="several previous generations"):
        B.write(scratch)
    assert _hidden(scratch) == [".v2.old-1", ".v2.old-2"]
    assert not (parent / "v2").exists()


def test_g_restore_failure_keeps_old(scratch, monkeypatch):
    parent = (Path(scratch) / G.OUTPUT_DIR).parent
    before = _published(scratch)
    os.rename(parent / "v2", parent / ".v2.old-7")
    real = os.rename

    def fail(src, dst):
        if ".v2.old-" in str(src):
            raise OSError("read-only")
        return real(src, dst)
    monkeypatch.setattr(os, "rename", fail)
    with pytest.raises(B.BuildError, match="cannot restore"):
        B.write(scratch)
    assert _hidden(scratch) == [".v2.old-7"] and not (parent / "v2").exists()
    assert _tree_bytes(parent / ".v2.old-7") == before


def test_stale_gate_reports_leftover_directories(scratch):
    parent = (Path(scratch) / G.OUTPUT_DIR).parent
    shutil.copytree(parent / "v2", parent / ".v2.old-5")
    states = {e["path"]: e["state"] for e in S.classify(scratch)["entries"]}
    left = [p for p in states if "/.v2.old-5/" in p]
    assert left and all(states[p] == "UNMANAGED" for p in left)
