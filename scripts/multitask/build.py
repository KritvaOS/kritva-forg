#!/usr/bin/env python3
# =============================================================================
# Copyright (c) 2026 KritvaOS
# SPDX-License-Identifier: Apache-2.0
#
# File        : build.py
# Description : Build the multi-task dataset datasets/multitask/v2 (KF-DQ-013)
#
# Component   : Kritva Forge
# Module      : multitask
# Layer       : Dataset
#
# Author      : Kritva Forge Team
# Created     : 02-10-2026
# =============================================================================
"""Multi-task dataset builder (KF-DQ-013).

For every canonical module (sorted) and every ``populated`` task of the
registry, one ``kritva-forge-dataset`` v2 record is derived from persisted
artifacts:

- ``rtl_generation`` v2: Prompt v2 text -> ``rtl-slice-v1``;
- ``rtl_understanding`` v1: ``rtl-slice-v1`` -> Prompt v2 text;
- ``interface_extraction`` / ``structural_extraction`` / ``dependency_analysis``
  / ``fsm_extraction`` v1: ``rtl-slice-v1`` -> frozen JSON projection.

Every record inherits its module's split from the split schema v2 manifest
(the split algorithm is never re-run on task records).  The build refuses
unless the split schema is 2, the split leakage gate passes and the entry
authorization ``kf_dq_013_entry`` derived from the current classification is
``open``.  Inputs are validated fail-closed; files are written to temporary
paths and replaced only when the whole dataset was built.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

from scripts.multitask import projections as PJ
from scripts.multitask import registry as G
from scripts.multitask import rtl_slice as RS


class BuildError(RuntimeError):
    """The multi-task dataset cannot be built (fail closed)."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# -----------------------------------------------------------------------------
# preconditions (AC-035 .. AC-037)
# -----------------------------------------------------------------------------

def module_splits(root: Path) -> dict:
    """(ip, module) -> split of the module's record in the split schema v2 manifest."""
    from scripts.dataset import leakage as L

    path = root / "splits/split_manifest.json"
    if not path.is_file():
        raise BuildError("splits/split_manifest.json missing")
    sm = json.loads(path.read_text(encoding="utf-8"))
    if sm.get("split_schema_version") != L.SPLIT_SCHEMA_VERSION or L.SPLIT_SCHEMA_VERSION != 2:
        raise BuildError(f"split manifest schema {sm.get('split_schema_version')!r} (split schema 2 required)")
    out = {}
    for split in G.SPLITS:
        for e in sm.get(split, []):
            key = (e.get("ip"), e.get("module"))
            if out.get(key, split) != split:
                raise BuildError(f"module {key[0]}/{key[1]} has records in more than one split")
            out[key] = split
    return out


def authorization(root: Path) -> dict:
    """Entry authorization recomputed from the current data: split gate PASS and no near-duplicate group."""
    from scripts.dataset import leakage as L
    from scripts.dataset import near_duplicate as ND
    from scripts.prompt_v2 import classify as C

    manifest = json.loads((root / "splits/split_manifest.json").read_text(encoding="utf-8"))
    try:
        gate = L.check_leakage(L.load_split_identities(str(root)), manifest, ND.compute(root))
        cls = C.build(root)
    except (ND.NearDuplicateError, C.ClassificationError) as exc:
        raise BuildError(f"entry authorization cannot be evaluated: {exc}") from exc
    return {"split_gate": gate["status"], "split_problems": gate.get("problems", [])[:5],
            "near_duplicate": cls["counts"]["near_duplicate"], "kf_dq_013_entry": cls["kf_dq_013_entry"],
            "structural_similarity": [sorted(f"{m['ip']}/{m['module']}" for m in g["members"])
                                      for g in cls["groups"] if g["classification"] == "structural_similarity"]}


def require_authorization(auth: dict):
    if auth["split_gate"] != "PASS":
        raise BuildError(f"split leakage gate {auth['split_gate']}: {auth['split_problems']}")
    if auth["kf_dq_013_entry"] != "open" or auth["near_duplicate"]:
        raise BuildError(f"KF-DQ-013 entry authorization not granted (kf_dq_013_entry "
                         f"{auth['kf_dq_013_entry']!r}, near_duplicate {auth['near_duplicate']})")


# -----------------------------------------------------------------------------
# per module
# -----------------------------------------------------------------------------

def _prompt_v2(root: Path, inputs: dict) -> tuple:
    """Prompt v2 text + sidecar of a module, validated against the current inputs (AC-033)."""
    from scripts.prompt_v2 import model as P
    from scripts.prompt_v2 import render as R

    ip, mod = inputs["ip"], inputs["module"]
    prel, srel = P.prompt_rel(ip, mod), P.sidecar_rel(ip, mod)
    if not (root / prel).is_file() or not (root / srel).is_file():
        raise BuildError(f"{ip}/{mod}: Prompt v2 text or sidecar missing")
    raw, sraw = (root / prel).read_bytes(), (root / srel).read_bytes()
    text = raw.decode("utf-8")
    try:
        sc = json.loads(sraw)
    except ValueError as exc:
        raise BuildError(f"{srel}: invalid JSON ({exc})") from exc
    problems = []
    if sc.get("schema") != {"name": P.SCHEMA_NAME, "version": P.SCHEMA_VERSION} or sc.get("versions") != R.versions():
        problems.append("unsupported Prompt v2 schema / versions")
    if sc.get("identity") != P.identity(sc):
        problems.append("Prompt v2 identity not derived from the sidecar")
    if (sc.get("prompt") or {}).get("sha256") != _sha(raw) or (sc.get("prompt") or {}).get("path") != prel:
        problems.append("Prompt v2 text disagrees with its sidecar")
    if (sc.get("module") or {}).get("module_id") != inputs["docs"]["semantic_ir"]["module"]["module_id"]:
        problems.append("Prompt v2 module identity disagrees")
    for layer in P.UPSTREAM:
        ref = (sc.get("inputs") or {}).get(layer) or {}
        if ref.get("path") != inputs["rels"][layer] or ref.get("sha256") != inputs["shas"][layer]:
            problems.append(f"Prompt v2 derived from a stale {layer} revision")
    if sc.get("source") != inputs["source"]:
        problems.append("Prompt v2 source disagrees with Semantic IR")
    if problems:
        raise BuildError(f"{ip}/{mod}: " + "; ".join(problems))
    return text, [{"layer": "prompt_v2", "path": prel, "sha256": _sha(raw)},
                  {"layer": "prompt_v2_sidecar", "path": srel, "sha256": _sha(sraw)}], sc["identity"]


def _versions(task: dict) -> dict:
    from scripts.core import compat as C

    v = {"dataset_schema": G.DATASET_SCHEMA["version"], "task_registry": G.TASK_REGISTRY_VERSION,
         "task_version": task["version"], "rtl_slice": G.RTL_SLICE_VERSION}
    keys = {"semantic_ir": ("semantic_ir", "semantic_identity"),
            "structural": ("structural", "structural_identity", "structural_analyzer"),
            "fsm": ("fsm", "fsm_identity", "fsm_analyzer"),
            "prompt_v2": ("prompt", "prompt_generator", "prompt_identity")}
    for layer in task["source_layers"]:
        for k in keys.get(layer, ()):
            v[k] = C.REQUIRED[k]
    return v


def module_records(root: Path, ip: str, module: str, split: str, stats: Counter | None = None) -> list:
    from scripts.prompt_v2 import render as R

    try:
        inputs = R.load_inputs(root, ip, module)
    except R.PromptError as exc:
        raise BuildError(str(exc)) from exc
    module_id = inputs["docs"]["semantic_ir"]["module"]["module_id"]
    try:
        rtl = RS.module_slice(inputs["source_text"], module)
    except RS.SliceError as exc:
        raise BuildError(f"{ip}/{module}: {exc}") from exc
    src = {"layer": "source", "path": inputs["source"]["path"], "sha256": inputs["source"]["sha256"]}
    layer_src = {layer: {"layer": layer, "path": inputs["rels"][layer], "sha256": inputs["shas"][layer]}
                 for layer in inputs["rels"]}
    pv2 = None
    out = []
    for task in G.POPULATED_TASKS:
        sources = [src]
        if task["input_kind"] == "prompt_v2" or task["target_kind"] == "prompt_v2":
            pv2 = pv2 or _prompt_v2(root, inputs)
            text, pv2_sources, _ = pv2
            sources = sources + pv2_sources
            if task["input_kind"] == "prompt_v2":
                inp, tgt = {"kind": "prompt_v2", "text": text}, {"kind": "rtl", "text": rtl}
            else:
                inp, tgt = {"kind": "rtl", "text": rtl}, {"kind": "prompt_v2", "text": text}
        else:
            proj = task["projection"]
            try:
                value, pstats = PJ.PROJECTORS[proj](inputs)
            except PJ.ProjectionError as exc:
                raise BuildError(str(exc)) from exc
            if stats is not None:
                for k, n in pstats.items():
                    stats[f"{proj}:{k}"] += n
            sources = sources + [layer_src[x] for x in task["source_layers"] if x != "source"]
            inp, tgt = {"kind": "rtl", "text": rtl}, {"kind": "json", "projection": proj, "value": value}
        out.append({
            "schema": dict(G.DATASET_SCHEMA),
            "record_id": G.record_id(task["id"], task["version"], module_id),
            "task": {"id": task["id"], "version": task["version"]},
            "module": {"ip": ip, "name": module, "module_id": module_id},
            "split": split,
            "input": inp,
            "target": tgt,
            "sources": sorted(sources, key=lambda s: (s["layer"], s["path"])),
            "versions": _versions(task),
        })
    return out


# -----------------------------------------------------------------------------
# corpus
# -----------------------------------------------------------------------------

def build(data_root, authorize: bool = True) -> dict:
    """All records by split, the registry document and statistics (nothing written)."""
    from scripts.prompt_v2.render import canonical_modules

    root = Path(os.path.abspath(data_root))
    splits = module_splits(root)
    auth = authorization(root) if authorize else None
    if auth is not None:
        require_authorization(auth)
    mods = canonical_modules(root)
    missing = sorted(f"{ip}/{m}" for ip, m in mods if (ip, m) not in splits)
    if missing:
        raise BuildError(f"modules without a split: {missing[:5]}")
    stats = Counter()
    records = {s: [] for s in G.SPLITS}
    for ip, module in mods:
        for rec in module_records(root, ip, module, splits[(ip, module)], stats):
            records[rec["split"]].append(rec)
    for s in G.SPLITS:
        records[s].sort(key=lambda r: (r["task"]["id"], r["module"]["ip"], r["module"]["name"]))
    counts = Counter(r["task"]["id"] for s in G.SPLITS for r in records[s])
    return {"records": records, "registry": G.document(dict(counts)), "stats": dict(sorted(stats.items())),
            "authorization": auth}


def serialize(result: dict) -> dict:
    """Relative path -> exact file text."""
    files = {f"{G.OUTPUT_DIR}/{s}.jsonl": "".join(G.dumps_record(r) for r in result["records"][s]) for s in G.SPLITS}
    files[G.REGISTRY_PATH] = G.dumps(result["registry"])
    return files


def _recover(parent: Path, final: Path) -> list:
    """Deterministic recovery of an interrupted publication (KF-DQ-013.1 AC-033); returns log lines."""
    import shutil

    log = []
    if not parent.is_dir():
        return log
    try:
        for d in sorted(parent.glob(f".{final.name}.tmp-*")):
            shutil.rmtree(d)
            log.append(f"removed unpublished staging directory {d.name}")
    except OSError as exc:
        raise BuildError(f"cannot remove an unpublished staging directory: {type(exc).__name__}") from exc
    old = sorted(parent.glob(f".{final.name}.old-*"))
    if not final.exists():
        if len(old) > 1:
            raise BuildError(f"{final.name} is missing and several previous generations exist: "
                             f"{[d.name for d in old]}; restore one by hand")
        if old:
            try:
                os.rename(old[0], final)
            except OSError as exc:
                raise BuildError(f"cannot restore {final.name} from {old[0].name} "
                                 f"({type(exc).__name__}); it is kept unchanged") from exc
            log.append(f"restored {final.name} from {old[0].name}")
            old = []
    # an existing final directory only ever comes from renaming a verified staging directory,
    # so it is complete and the previous generations are no longer needed
    for d in old:
        shutil.rmtree(d)
        log.append(f"removed previous generation {d.name}")
    return log


def _verify_staged(stage: Path, root: Path, files: dict, counts: dict) -> None:
    """Re-read the staged generation before publishing (AC-031); fail closed."""
    from scripts.multitask import validator as V

    for rel, text in files.items():
        staged = stage / Path(rel).relative_to(G.OUTPUT_DIR)
        if staged.read_bytes() != text.encode("utf-8"):
            raise BuildError(f"staged {rel} differs from the serialized dataset")
        if rel.endswith(".jsonl"):
            for n, line in enumerate(text.splitlines(), 1):
                problems = V.record_type_problems(json.loads(line))
                if problems:
                    raise BuildError(f"staged {rel}:{n}: {problems[0]}")
    if files[G.REGISTRY_PATH] != G.dumps(G.document(counts)):
        raise BuildError("staged registry differs from the forge task registry")


def write(data_root, result: dict | None = None) -> dict:
    """Build, stage the complete generation, verify it, then publish it with one directory switch.

    KF-DQ-013.1 AC-030 .. AC-033: files are never replaced one by one. The only window in which the
    published directory is absent is between the two renames; the previous generation is then kept
    under ``.v2.old-<pid>`` and the next ``write()`` restores it (``_recover``).
    """
    import shutil

    root = Path(os.path.abspath(data_root))
    final = root / G.OUTPUT_DIR
    parent = final.parent
    for line in _recover(parent, final):
        print(f"[INFO] multi-task publication recovery: {line}")
    result = result if result is not None else build(root)
    files = serialize(result)
    counts = Counter(r["task"]["id"] for s in G.SPLITS for r in result["records"][s])
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / f".{final.name}.tmp-{os.getpid()}"
    old = parent / f".{final.name}.old-{os.getpid()}"
    try:
        stage.mkdir()
        for rel, text in files.items():
            (stage / Path(rel).relative_to(G.OUTPUT_DIR)).write_bytes(text.encode("utf-8"))
        _verify_staged(stage, root, files, dict(counts))
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    if final.exists():
        os.rename(final, old)          # step 1
    try:
        os.rename(stage, final)        # step 2
    except OSError as exc:
        raise BuildError(f"publication interrupted ({type(exc).__name__}); the previous generation is kept "
                         f"in {old.name} and the next build restores it") from exc
    if old.exists():
        try:
            shutil.rmtree(old)         # step 3
        except OSError as exc:
            print(f"[WARN] published {G.OUTPUT_DIR}; could not remove the previous generation {old.name} "
                  f"({type(exc).__name__}); the next build removes it")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="KF-DQ-013 multi-task dataset builder")
    parser.add_argument("--data-root", default=os.environ.get("KRITVA_FORGE_DATA_ROOT"))
    args = parser.parse_args(argv)
    if not args.data_root:
        from scripts.core.paths import default_data_root
        args.data_root = str(default_data_root())
    try:
        result = write(args.data_root)
    except BuildError as exc:
        print(f"[STOP] multi-task dataset refused: {exc}")
        return 1
    counts = {s: len(v) for s, v in result["records"].items()}
    print(f"Multi-task dataset ({G.DATASET_SCHEMA['name']} v{G.DATASET_SCHEMA['version']}, task registry "
          f"v{G.TASK_REGISTRY_VERSION}): {sum(counts.values())} records · train / validation / test "
          f"{counts['train']} / {counts['validation']} / {counts['test']}")
    return 0


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    sys.exit(main())
