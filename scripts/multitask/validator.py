#!/usr/bin/env python3
# =============================================================================
# Copyright (c) 2026 KritvaOS
# SPDX-License-Identifier: Apache-2.0
#
# File        : validator.py
# Description : Multi-task dataset gate and dataset report (KF-DQ-013)
#
# Component   : Kritva Forge
# Module      : multitask
# Layer       : Dataset
#
# Author      : Kritva Forge Team
# Created     : 02-10-2026
# =============================================================================
"""Multi-task dataset gate (KF-DQ-013 AC-040 .. AC-045).

``check(data_root)`` fails closed on any of:

- schema / version / registry mismatch;
- a record that is not canonical JSON or has extra / missing (nested) fields;
- an unknown task, a declared-task record, or a populated task missing a module;
- a wrong or duplicate ``record_id``, or a wrong sort order;
- split inheritance violated (a module in two splits, or a split differing
  from the split schema v2 manifest);
- task provenance inconsistent (source sha256 / module / projection / versions);
- an id, location or absolute-path leak in the metadata or JSON targets;
- the entry authorization not holding;
- any byte difference from a re-derivation of the dataset from the persisted
  artifacts.

It returns the dataset report (``analysis/reports/dataset_v2_report.json``):
per-task and per-split counts, size statistics, FSM positives / negatives,
null counts, the retained soft findings and the dataset identity ``ds2:``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from scripts.multitask import build as B
from scripts.multitask import registry as G

_ID_LEAK = re.compile(r"\b(?:sem1|beh1|str1|fsm1|n1|grp1|pv2):[0-9a-f]{16}\b")
_EXPECTED_FILES = tuple(f"{G.OUTPUT_DIR}/{s}.jsonl" for s in G.SPLITS) + (G.REGISTRY_PATH,)


def _quantiles(values: list) -> dict:
    v = sorted(values)
    if not v:
        return {}
    return {f"p{q}": v[min(len(v) - 1, int(round(q / 100 * (len(v) - 1))))] for q in (50, 90, 99, 100)}


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _object(value, path: str, fields: dict, problems: list) -> bool:
    """``value`` must be an object with exactly ``fields`` (name -> predicate, description)."""
    if not isinstance(value, dict):
        problems.append(f"{path}: expected an object, got {type(value).__name__}")
        return False
    if sorted(value) != sorted(fields):
        problems.append(f"{path} fields {sorted(value)}, expected {sorted(fields)}")
        return False
    ok = True
    for name, (pred, what) in fields.items():
        if not pred(value[name]):
            problems.append(f"{path}.{name}: expected {what}, got {type(value[name]).__name__}")
            ok = False
    return ok


_STR = (lambda v: isinstance(v, str), "a string")
_INT = (_is_int, "an integer")


def record_type_problems(rec) -> list:
    """Typed shape of one multi-task record (KF-DQ-013.1 AC-010 / AC-011).

    Never raises for any JSON value; returns the list of problems, each naming the field path.
    """
    problems = []
    if not isinstance(rec, dict):
        return [f"record: expected an object, got {type(rec).__name__}"]
    if sorted(rec) != sorted(G.RECORD_KEYS):
        return [f"record fields {sorted(rec)}, expected {sorted(G.RECORD_KEYS)}"]
    for key in ("record_id", "split"):
        if not isinstance(rec[key], str):
            problems.append(f"record.{key}: expected a string, got {type(rec[key]).__name__}")
    _object(rec["schema"], "record.schema", {"name": _STR, "version": _INT}, problems)
    _object(rec["task"], "record.task", {"id": _STR, "version": _INT}, problems)
    _object(rec["module"], "record.module", {"ip": _STR, "module_id": _STR, "name": _STR}, problems)
    _object(rec["input"], "record.input",
            {"kind": (lambda v: isinstance(v, str) and v in G.INPUT_KINDS, f"one of {list(G.INPUT_KINDS)}"),
             "text": _STR}, problems)
    tgt = rec["target"]
    if not isinstance(tgt, dict):
        problems.append(f"record.target: expected an object, got {type(tgt).__name__}")
    elif tgt.get("kind") == "json":
        _object(tgt, "record.target", {"kind": _STR, "projection": _STR,
                                       "value": (lambda v: isinstance(v, dict), "an object")}, problems)
    elif isinstance(tgt.get("kind"), str) and tgt["kind"] in G.INPUT_KINDS:
        _object(tgt, "record.target", {"kind": _STR, "text": _STR}, problems)
    else:
        problems.append(f"record.target.kind: expected one of {['json', *G.INPUT_KINDS]}, got {tgt.get('kind')!r}")
    srcs = rec["sources"]
    if not isinstance(srcs, list):
        problems.append(f"record.sources: expected a list, got {type(srcs).__name__}")
    else:
        for i, s in enumerate(srcs):
            _object(s, f"record.sources[{i}]", {"layer": _STR, "path": _STR, "sha256": _STR}, problems)
    ver = rec["versions"]
    if not isinstance(ver, dict):
        problems.append(f"record.versions: expected an object, got {type(ver).__name__}")
    else:
        for k in sorted(ver):
            if not (isinstance(ver[k], str) or _is_int(ver[k])):
                problems.append(f"record.versions.{k}: expected a string or an integer, got {type(ver[k]).__name__}")
    return problems


def dataset_identity(lines_by_split: dict):
    """``ds2:`` identity; ``None`` unless every line is a well-typed record (AC-015)."""
    rows = []
    for split, lines in lines_by_split.items():
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                return None
            if record_type_problems(rec):
                return None
            rows.append(f"{rec['record_id']}\t{split}\t{hashlib.sha256(line.encode('utf-8')).hexdigest()}")
    return "ds2:" + hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()[:16]


def _record_problems(rec, split, problems):
    """Value checks of one well-typed record (AC-013, AC-014, AC-018); the typed shape is checked first."""
    tgt = rec["target"]
    if rec["schema"] != G.DATASET_SCHEMA:
        problems.append(f"{split}: record {rec['record_id']} schema {rec['schema']!r}")
    if rec["split"] != split:
        problems.append(f"{split}: record {rec['record_id']} declares split {rec['split']!r}")
    _leak_problems(rec, split, problems)


def _leak_problems(rec: dict, split, problems):
    """Text-only leak scan of the metadata and JSON target (AC-018); safe for any JSON object."""
    meta = {k: v for k, v in rec.items() if k not in ("input", "target")}
    tgt = rec.get("target")
    meta["target"] = {k: v for k, v in tgt.items() if k != "text"} if isinstance(tgt, dict) else tgt
    text = json.dumps(meta, sort_keys=True)
    rid = rec.get("record_id")
    if _ID_LEAK.search(text) or '"loc"' in text:
        problems.append(f"{split}: record {rid} leaks an internal id or location")
    from scripts.core.paths import find_absolute_paths
    if find_absolute_paths(text):
        problems.append(f"{split}: record {rid} contains an absolute path")


def check(data_root, authorize: bool = True) -> dict:
    """Never raises (KF-DQ-013.1 AC-016): every input yields a report; unexpected errors FAIL."""
    try:
        return _check(data_root, authorize)
    except Exception as exc:  # last-resort guard; malformed-input tests never rely on it
        print(f"[internal] multi-task validator: {exc!r}", file=sys.stderr)
        return _report([f"internal validation error: {type(exc).__name__}"], {}, {}, None, None, None)


def _check(data_root, authorize: bool = True) -> dict:
    from scripts.prompt_v2.render import canonical_modules

    root = Path(os.path.abspath(data_root))
    problems = []
    lines_by_split = {}
    for rel in _EXPECTED_FILES:
        if not (root / rel).is_file():
            problems.append(f"missing {rel}")
    base = root / G.OUTPUT_DIR
    extra = sorted(p.relative_to(root).as_posix() for p in (root / "datasets/multitask").rglob("*")
                   if p.is_file() and p.relative_to(root).as_posix() not in _EXPECTED_FILES) \
        if (root / "datasets/multitask").is_dir() else []
    if extra:
        problems.append(f"unmanaged files under datasets/multitask: {extra[:5]}")
    if problems:
        return _report(problems, {}, {}, None, None, None)

    records = {}
    ids = Counter()
    by_module = defaultdict(set)
    task_modules = defaultdict(set)
    for split in G.SPLITS:
        try:
            raw = (base / f"{split}.jsonl").read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"{split}.jsonl: not valid UTF-8 (byte {exc.start})")
            continue
        lines = raw.splitlines(keepends=True)
        lines_by_split[split] = lines
        recs = []
        for n, line in enumerate(lines, 1):
            try:
                rec = json.loads(line)
            except ValueError as exc:
                problems.append(f"{split}.jsonl:{n}: invalid JSON ({exc})")
                continue
            typed = record_type_problems(rec)
            if typed:
                problems.extend(f"{split}.jsonl:{n}: {p}" for p in typed)
                if isinstance(rec, dict):
                    _leak_problems(rec, split, problems)
                continue
            if G.dumps_record(rec) != line:
                problems.append(f"{split}.jsonl:{n}: not canonical JSON")
            _record_problems(rec, split, problems)
            task = G.BY_ID.get(rec["task"]["id"])
            if task is None:
                problems.append(f"{split}.jsonl:{n}: unknown task {rec.get('task')!r}")
                continue
            if task["status"] != G.POPULATED:
                problems.append(f"{split}.jsonl:{n}: record of declared task {task['id']}")
            if rec["task"]["version"] != task["version"]:
                problems.append(f"{split}.jsonl:{n}: task {task['id']} version {rec['task']['version']} "
                                f"(registry {task['version']})")
            m = rec["module"]
            if rec["record_id"] != G.record_id(task["id"], rec["task"]["version"], m.get("module_id")):
                problems.append(f"{split}.jsonl:{n}: record_id is not derived from task / module identity")
            ids[rec["record_id"]] += 1
            by_module[(m.get("ip"), m.get("name"))].add(split)
            task_modules[task["id"]].add((m.get("ip"), m.get("name")))
            tgt = rec["target"]
            if task["projection"] and (tgt["kind"] != "json" or tgt.get("projection") != task["projection"]):
                problems.append(f"{split}.jsonl:{n}: target projection {tgt.get('projection')!r} (registry {task['projection']})")
            for s in rec["sources"]:
                p = root / s["path"]
                if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != s["sha256"]:
                    problems.append(f"{split}.jsonl:{n}: stale or missing source {s['layer']} {s['path']}")
            recs.append(rec)
        keys = [(r["task"]["id"], r["module"]["ip"], r["module"]["name"]) for r in recs]
        if keys != sorted(keys):
            problems.append(f"{split}.jsonl: records not sorted by (task, ip, module)")
        records[split] = recs
    dup = sorted(r for r, c in ids.items() if c > 1)
    if dup:
        problems.append(f"duplicate record_id {dup[:3]}")

    # coverage (AC-009) and split inheritance (AC-035 / AC-036)
    mods = set(canonical_modules(root))
    for task in G.TASKS:
        have = task_modules.get(task["id"], set())
        if task["status"] == G.POPULATED and have != mods:
            problems.append(f"task {task['id']}: {len(mods - have)} canonical modules missing, "
                            f"{len(have - mods)} non-canonical")
    try:
        splits = B.module_splits(root)
    except B.BuildError as exc:
        problems.append(str(exc))
        splits = {}
    for key, ss in sorted(by_module.items()):
        if len(ss) > 1:
            problems.append(f"module {key[0]}/{key[1]} has records in splits {sorted(ss)}")
        elif splits and splits.get(key) not in ss:
            problems.append(f"module {key[0]}/{key[1]}: records in {sorted(ss)}, split manifest {splits.get(key)!r}")

    # registry (AC-011)
    counts = Counter(r["task"]["id"] for s in G.SPLITS for r in records.get(s, []))
    reg_bytes = (root / G.REGISTRY_PATH).read_bytes()
    if reg_bytes != G.dumps(G.document(dict(counts))).encode("utf-8"):
        problems.append("registry.json differs from the forge task registry (with record counts)")

    # entry authorization and re-derivation (AC-037, AC-042)
    auth, stats = None, {}
    try:
        expected = B.build(root, authorize=authorize)
        auth, stats = expected["authorization"], expected["stats"]
        files = B.serialize(expected)
        diff = sorted(rel for rel, text in files.items() if (root / rel).read_bytes() != text.encode("utf-8"))
        if diff:
            problems.append(f"dataset differs from a re-derivation from the persisted artifacts: {diff}")
    except B.BuildError as exc:
        problems.append(f"re-derivation refused: {exc}")
    return _report(problems, records, counts, lines_by_split, auth, stats)


def _report(problems, records, counts, lines_by_split, auth, stats) -> dict:
    per_split = {s: len(records.get(s, [])) for s in G.SPLITS}
    by_task_split = {t["id"]: {s: sum(1 for r in records.get(s, []) if r["task"]["id"] == t["id"]) for s in G.SPLITS}
                     for t in G.TASKS}
    sizes = {}
    for t in G.POPULATED_TASKS:
        recs = [r for s in G.SPLITS for r in records.get(s, []) if r["task"]["id"] == t["id"]]
        tgt = [len(r["target"]["text"]) if "text" in r["target"]
               else len(json.dumps(r["target"]["value"], sort_keys=True, separators=(",", ":"))) for r in recs]
        sizes[t["id"]] = {"input_chars": _quantiles([len(r["input"]["text"]) for r in recs]),
                          "target_chars": _quantiles(tgt)}
    fsm = [r["target"]["value"].get("fsms") for s in G.SPLITS for r in records.get(s, [])
           if r["task"]["id"] == "fsm_extraction" and r["target"]["kind"] == "json"]
    fsm = [f if isinstance(f, list) else [] for f in fsm]
    empty = Counter()
    for s in G.SPLITS:
        for r in records.get(s, []):
            v = r["target"].get("value")
            if isinstance(v, dict):
                for k, x in v.items():
                    if isinstance(x, list) and not x:
                        empty[f"{r['target']['projection']}:{k}"] += 1
    return {
        "schema": {"name": "kritva-forge-dataset-report", "version": 1},
        "dataset_schema": G.DATASET_SCHEMA,
        "task_registry": G.TASK_REGISTRY_VERSION,
        "rtl_slice": G.RTL_SLICE_VERSION,
        "tasks": {t["id"]: {"version": t["version"], "status": t["status"], "projection": t["projection"],
                            "records": counts.get(t["id"], 0)} for t in G.TASKS},
        "counts": {"records": sum(per_split.values()), "splits": per_split, "by_task": by_task_split},
        "dataset_identity": dataset_identity(lines_by_split)
        if lines_by_split and sorted(lines_by_split) == sorted(G.SPLITS) else None,
        "sizes": sizes,
        "fsm_extraction": {"positive": sum(1 for f in fsm if f), "negative": sum(1 for f in fsm if not f),
                           "fsms": sum(len(f) for f in fsm)},
        "empty_collections": dict(sorted(empty.items())),
        "nulls_and_unresolved": stats,
        "authorization": auth,
        "soft_findings": {
            "structural_similarity": (auth or {}).get("structural_similarity"),
            "ruling": "KF-DQ-013 AC-039: retained cross-split structural_similarity groups reviewed and kept soft",
        },
        "problems": problems,
        "status": "FAIL" if problems else "PASS",
    }


def write_report(data_root, report: dict) -> Path:
    path = Path(os.path.abspath(data_root)) / G.REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(G.dumps(report), encoding="utf-8")
    return path


def format_report(rep: dict) -> str:
    c = rep["counts"]
    lines = [f"Multi-task dataset check: {rep['status']} ({rep['dataset_schema']['name']} v"
             f"{rep['dataset_schema']['version']}, task registry v{rep['task_registry']})",
             f"  records             : {c['records']} (train / validation / test "
             f"{c['splits']['train']} / {c['splits']['validation']} / {c['splits']['test']})"]
    for tid, t in rep["tasks"].items():
        lines.append(f"  {tid:22s}: v{t['version']} {t['status']:9s} {t['records']} records")
    f = rep["fsm_extraction"]
    lines.append(f"  fsm_extraction      : {f['positive']} positive / {f['negative']} negative modules, {f['fsms']} FSMs")
    lines.append(f"  dataset identity    : {rep['dataset_identity']}")
    a = rep.get("authorization") or {}
    lines.append(f"  entry authorization : kf_dq_013_entry {a.get('kf_dq_013_entry')} (split gate {a.get('split_gate')})")
    for p in rep["problems"][:30]:
        lines.append(f"  [FAIL] {p}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="KF-DQ-013 multi-task dataset gate")
    parser.add_argument("--data-root", default=os.environ.get("KRITVA_FORGE_DATA_ROOT"))
    parser.add_argument("--check", action="store_true", help="validate (default)")
    parser.add_argument("--report", action="store_true", help=f"write {G.REPORT_PATH}")
    args = parser.parse_args(argv)
    if not args.data_root:
        from scripts.core.paths import default_data_root
        args.data_root = str(default_data_root())
    rep = check(args.data_root)
    if args.report:
        write_report(args.data_root, rep)
    print(format_report(rep))
    return 0 if rep["status"] == "PASS" else 1


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    sys.exit(main())
