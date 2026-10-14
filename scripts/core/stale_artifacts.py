#!/usr/bin/env python3
# =============================================================================
# Copyright (c) 2026 KritvaOS
# SPDX-License-Identifier: Apache-2.0
#
# File        : stale_artifacts.py
# Description : Generated-artifact inventory, stale-artifact gate and safe cleanup (KF-DQ-006)
#
# Component   : Kritva Forge
# Module      : core
# Layer       : Development Infrastructure
#
# Author      : Kritva Forge Team
# Created     : 02-10-2026
# =============================================================================
"""Stale generated artifacts (KF-DQ-006).

Every file under the managed data tree is classified into exactly one state:

==============  ===========================================================
``CURRENT``     part of the current output contract, derived from current
                canonical inputs, current schema, identities verified
``STALE``       no longer matches current inputs / schema / contract, or a
                superseded duplicate of a canonical artifact
``ORPHAN``      references a module, IP, record or identity that no longer
                exists in the canonical data
``HISTORICAL``  intentionally retained, excluded from active consumption
                (``*/legacy/``, ``datasets/source/``, KF-DQ-001 migration
                manifests)
``UNMANAGED``   not recognised by the output contract (including symlinks)
==============  ===========================================================

Freshness is decided from canonical identities only - never from mtime,
ctime, traversal order, absolute paths or object identity:

* the expected inventory is derived from ``raw/rtl/original``, canonical
  ``normalized/ir`` and the KF-DQ-005 provenance model (recomputed, not read
  from existing outputs);
* prompts must equal ``generate_module_prompt(<canonical IR>)`` byte for byte;
* dataset records must carry KF-DQ-005 provenance whose record_id,
  module_id, source sha256 and IR sha256 match the current inputs;
* split-manifest entries must reference current dataset records;
* manifests / reports must carry the current schema version and match a
  recomputation.

Outputs (``ARTIFACT_SCHEMA_VERSION = 1``):

* ``manifests/artifact_inventory.json``           expected + observed inventory
* ``analysis/reports/stale_artifact_report.json`` gate report + remediation plan

Remediation: ``REGENERATE`` (rerun the pipeline), ``REMOVE`` (orphans and
superseded duplicates in cleanable roots), ``QUARANTINE`` (unmanaged files,
moved to ``generated/legacy/quarantine/<path>`` and thereby HISTORICAL), or
``NONE``.  ``--check`` is read-only; ``--clean`` is a dry run unless
``--apply`` is given.  Cleanup never touches ``raw/``, ``normalized/`` or a
historical path, rejects symlinks, absolute paths and ``..``, and refuses
the whole plan if any entry is unsafe.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

ARTIFACT_SCHEMA_VERSION = 1
INVENTORY_PATH = "manifests/artifact_inventory.json"
REPORT_PATH = "analysis/reports/stale_artifact_report.json"
DATA_MANIFEST_PATH = "manifests/data_manifest.json"        # KF-DQ-007, validated by data_manifest.py
DATA_MANIFEST_REPORT_PATH = "analysis/reports/data_manifest_report.json"
SEMANTIC_REPORT_PATH = "analysis/reports/semantic_ir_report.json"   # KF-DQ-008 semantic gate
SEMANTIC_DIR = "normalized/semantic_ir/v2"                          # KF-DQ-008 canonical Semantic IR
BEHAVIOR_DIR = "normalized/behavior/v1"                             # KF-DQ-009 Behavioral Semantics
BEHAVIOR_REPORT_PATH = "analysis/reports/behavior_report.json"      # KF-DQ-009 behavior gate
STRUCTURAL_DIR = "normalized/structural/v1"                         # KF-DQ-010 Structural Analysis
STRUCTURAL_REPORT_PATH = "analysis/reports/structural_report.json"  # KF-DQ-010 structural gate
FSM_DIR = "normalized/fsm/v2"                                       # KF-DQ-011 / KF-DQ-011.1 FSM Analysis
FSM_OBSOLETE_DIRS = ("normalized/fsm/v1",)                          # superseded by v2 (KF-DQ-011.1)
FSM_REPORT_PATH = "analysis/reports/fsm_report.json"                # KF-DQ-011 FSM gate
PROMPT_V2_DIR = "generated/prompt/v2"                               # KF-DQ-012 Prompt v2
PROMPT_V2_REPORT_PATH = "analysis/reports/prompt_v2_report.json"    # KF-DQ-012 Prompt v2 gate
CLASSIFICATION_REPORT_PATH = "analysis/reports/prompt_leakage_classification.json"   # KF-DQ-012 rtl-sim-v1
COMPAT_REPORT_PATH = "analysis/reports/compatibility_report.json"    # KF-DQ-012 cross-repo compatibility
# Gate outputs: listed whether or not they exist yet (their hashes are not
# recorded), so the inventory never depends on itself; each is verified by
# its own validator.
SELF_OUTPUTS = {
    INVENTORY_PATH: "artifact_inventory",
    REPORT_PATH: "stale_artifact_report",
    DATA_MANIFEST_PATH: "data_manifest",
    DATA_MANIFEST_REPORT_PATH: "data_manifest_report",
    COMPAT_REPORT_PATH: "compatibility_report",       # KF-DQ-012: written after the manifest gate
}
QUARANTINE_ROOT = "generated/legacy/quarantine"
OVERRIDE_ENV = "KRITVA_FORGE_ALLOW_STALE"

STATES = ("CURRENT", "STALE", "ORPHAN", "HISTORICAL", "UNMANAGED")
FAILING_STATES = ("STALE", "ORPHAN", "UNMANAGED")

# Top-level layout of the data repository.
CANONICAL_INPUT_ROOTS = ("raw",)                    # never inspected for staleness
MANAGED_ROOTS = ("normalized", "generated", "analysis", "datasets", "splits", "manifests", "golden")
ROOT_FILES = ("README.md", ".gitignore", ".gitattributes")

HISTORICAL_PREFIXES = (
    "analysis/legacy/",
    "datasets/legacy/",
    "generated/legacy/",
    "normalized/legacy/",
    "datasets/source/",                             # external source datasets, not pipeline output
)
HISTORICAL_FILES = (
    "manifests/migration_manifest.json",            # KF-DQ-001 layout migration record
    "manifests/reorder_manifest.json",
)
# Roots whose files cleanup may remove or quarantine.
CLEANABLE_PREFIXES = ("generated/", "analysis/reports/", "datasets/pipeline/", "datasets/multitask/", "splits/",
                      "manifests/", "golden/")
PLACEHOLDER = ".gitkeep"

DATASET_SPLIT_FILES = ("train", "validation", "test")
DATASET_AUX_RECORD_FILES = ("dataset_new", "dataset_old", "dataset_mixed")
# Artifacts that the dataset stage of the pipeline (re)writes; the pre-dataset
# gate leaves them to the post-run check.
PIPELINE_OUTPUT_KINDS = frozenset({
    "dataset_split", "dataset_records", "dataset_manifest", "dataset_stats", "split_manifest",
    "provenance_manifest", "provenance_report", "split_leakage_report", "pipeline_stats",
    "artifact_inventory", "stale_artifact_report", "data_manifest", "data_manifest_report",
    "semantic_ir_report", "behavior_report", "structural_report", "fsm_report",
    "prompt_v2", "prompt_v2_sidecar", "prompt_v2_report", "prompt_leakage_classification",   # KF-DQ-012
    "multitask_split", "multitask_registry", "dataset_v2_report",                            # KF-DQ-013
})
_PROMPT_V2_RE = re.compile(r"^generated/prompt/v2/([^/]+)/([^/]+)\.([A-Za-z0-9_]+)\.(txt|json)$")
_PROMPT_RE = re.compile(r"^generated/prompts/([^/]+)/([^/]+)\.generate\.txt$")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _sha(path: Path) -> str:
    from scripts.core.provenance import sha256_file
    return sha256_file(path)


def is_historical(rel: str) -> bool:
    return rel.startswith(HISTORICAL_PREFIXES) or rel in HISTORICAL_FILES


def _walk(root: Path) -> list[tuple[str, bool]]:
    """``(relative path, is_symlink)`` for every file (and symlink) under the managed roots."""
    found = []
    for top in sorted(os.listdir(root)):
        if top == ".git":
            continue
        path = root / top
        if path.is_symlink() or path.is_file():
            found.append((top, path.is_symlink()))
            continue
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            base = Path(dirpath)
            for name in sorted(dirnames):
                if (base / name).is_symlink():
                    found.append(((base / name).relative_to(root).as_posix(), True))
            dirnames[:] = sorted(d for d in dirnames if not (base / d).is_symlink())
            for name in sorted(filenames):
                p = base / name
                found.append((p.relative_to(root).as_posix(), p.is_symlink()))
    return sorted(found)


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonl(path: Path) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    out.append(json.loads(line))
    except (OSError, ValueError):
        return None
    return out


# -----------------------------------------------------------------------------
# Canonical context (expected inventory)
# -----------------------------------------------------------------------------

class Context:
    """Current canonical state derived from raw RTL, canonical IR and KF-DQ-005 provenance."""

    def __init__(self, root: Path, use_recorded: bool = True):
        from scripts.core.paths import ForgeDataPaths, iter_module_yamls
        from scripts.core.provenance import build_manifest, load_yaml, module_id

        self.root = root
        self.data = ForgeDataPaths.from_root(root)
        self.provenance = build_manifest(root)                      # recomputed, never read back
        self.modules = {(m["ip"], m["module"]): m for m in self.provenance["modules"]}
        # What the canonical IR was generated from, as recorded by the last
        # pipeline run (KF-DQ-005).  Used to detect IR older than its source.
        stored = _load_json(self.data.manifests / "provenance_manifest.json") if use_recorded else None
        self.recorded = {
            (m.get("ip"), m.get("module")): m
            for m in (stored.get("modules", []) if isinstance(stored, dict) else [])
            if isinstance(m, dict)
        }
        self.module_ids = {module_id(ip, mod): (ip, mod) for ip, mod in self.modules}
        self.ips = sorted({ip for ip, _ in self.modules})
        self.raw_ips = sorted(
            p.name for p in (root / "raw/rtl/original").iterdir() if p.is_dir()
        ) if (root / "raw/rtl/original").is_dir() else []
        self.specs = {}
        for path in iter_module_yamls(self.data.normalized_ir):
            self.specs[(path.parent.parent.name, path.stem)] = load_yaml(path.read_text(encoding="utf-8")) or {}
        raw = {}
        for dirpath, _, files in os.walk(root / "raw/rtl/original"):
            for name in files:
                p = Path(dirpath) / name
                if not p.is_symlink():
                    raw.setdefault(_sha(p), p.relative_to(root).as_posix())
        self.raw_by_sha = raw
        self.canonical_ir_sha = {}
        for dirpath, _, files in os.walk(self.data.normalized_ir):
            for name in files:
                p = Path(dirpath) / name
                if not p.is_symlink():
                    self.canonical_ir_sha.setdefault(_sha(p), p.relative_to(root).as_posix())

    def module_ok(self, ip, module) -> bool:
        entry = self.modules.get((ip, module))
        return bool(entry) and entry["transformation"] == "normalized"

    def declared(self, ip, module) -> bool:
        """True if ``module`` is still declared in one of its contributing sources."""
        from scripts.dataset.leakage import module_body

        for src in self.modules[(ip, module)]["contributing_sources"]:
            text = (self.root / src["path"]).read_text(encoding="utf-8", errors="ignore")
            if module_body(text, module) is not None:
                return True
        return False

    def expected_paths(self) -> dict:
        """Expected output inventory: path -> (kind, ip, module), from canonical inputs only."""
        exp = {}
        for ip, mod in sorted(self.modules):
            exp[f"normalized/ir/{ip}/modules/{mod}.yaml"] = ("canonical_ir", ip, mod)
            exp[f"generated/prompts/{ip}/{mod}.generate.txt"] = ("prompt", ip, mod)
            exp[f"{SEMANTIC_DIR}/{ip}/{mod}.json"] = ("semantic_ir", ip, mod)          # KF-DQ-008
            exp[f"{BEHAVIOR_DIR}/{ip}/{mod}.json"] = ("behavior", ip, mod)             # KF-DQ-009
            exp[f"{STRUCTURAL_DIR}/{ip}/{mod}.json"] = ("structural", ip, mod)         # KF-DQ-010
            exp[f"{FSM_DIR}/{ip}/{mod}.json"] = ("fsm", ip, mod)                       # KF-DQ-011
            exp[f"{PROMPT_V2_DIR}/{ip}/{mod}.behavior_aware.txt"] = ("prompt_v2", ip, mod)            # KF-DQ-012
            exp[f"{PROMPT_V2_DIR}/{ip}/{mod}.behavior_aware.json"] = ("prompt_v2_sidecar", ip, mod)
        for ip in self.ips:
            exp[f"normalized/ir/{ip}/hierarchy.yaml"] = ("ip_metadata", ip, None)
            exp[f"normalized/ir/{ip}/summary.yaml"] = ("ip_metadata", ip, None)
        for m in self.provenance["modules"]:
            for art in m["artifacts"]:
                exp.setdefault(art["path"], ("rtl_copy", m["ip"], None))
        singles = {
            "analysis/reports/pipeline_stats.json": "pipeline_stats",
            "analysis/reports/split_leakage_report.json": "split_leakage_report",
            "analysis/reports/provenance_report.json": "provenance_report",
            SEMANTIC_REPORT_PATH: "semantic_ir_report",
            BEHAVIOR_REPORT_PATH: "behavior_report",
            STRUCTURAL_REPORT_PATH: "structural_report",
            FSM_REPORT_PATH: "fsm_report",
            PROMPT_V2_REPORT_PATH: "prompt_v2_report",                  # KF-DQ-012
            CLASSIFICATION_REPORT_PATH: "prompt_leakage_classification",
            REPORT_PATH: "stale_artifact_report",
            "splits/split_manifest.json": "split_manifest",
            "manifests/provenance_manifest.json": "provenance_manifest",
            INVENTORY_PATH: "artifact_inventory",
            DATA_MANIFEST_PATH: "data_manifest",
            DATA_MANIFEST_REPORT_PATH: "data_manifest_report",
            "datasets/pipeline/manifest.json": "dataset_manifest",
            "datasets/pipeline/dataset_stats.json": "dataset_stats",
        }
        for name in DATASET_SPLIT_FILES:
            singles[f"datasets/pipeline/{name}.jsonl"] = "dataset_split"
        for name in DATASET_AUX_RECORD_FILES:
            singles[f"datasets/pipeline/{name}.jsonl"] = "dataset_records"
        # KF-DQ-013 multi-task dataset
        from scripts.multitask import registry as MG
        for name in MG.SPLITS:
            singles[f"{MG.OUTPUT_DIR}/{name}.jsonl"] = "multitask_split"
        singles[MG.REGISTRY_PATH] = "multitask_registry"
        singles[MG.REPORT_PATH] = "dataset_v2_report"
        for path, kind in singles.items():
            exp[path] = (kind, None, None)
        return exp


# -----------------------------------------------------------------------------
# Record checks (datasets / splits)
# -----------------------------------------------------------------------------

def check_record(ctx: Context, record: dict, counters: Counter) -> tuple[str, str] | None:
    """Return (state, reason) for a non-current dataset record, else None."""
    from scripts.core.provenance import PROVENANCE_VERSION, module_id
    from scripts.dataset.leakage import record_id

    ip, mod = record.get("ip"), record.get("module")
    prov = record.get("provenance")
    if not isinstance(prov, dict):
        counters["invalid_provenance"] += 1
        return "STALE", f"{ip}/{mod}: record has no provenance"
    if prov.get("provenance_version") != PROVENANCE_VERSION:
        counters["obsolete_schema"] += 1
        return "STALE", f"{ip}/{mod}: provenance_version {prov.get('provenance_version')!r} is obsolete"
    if (ip, mod) not in ctx.modules:
        return "ORPHAN", f"{ip}/{mod}: record refers to a module that is not canonical"
    entry = ctx.modules[(ip, mod)]
    if not prov.get("module_id") or prov.get("module_id") != module_id(ip, mod):
        counters["invalid_module_identities"] += 1
        return "STALE", f"{ip}/{mod}: module identity {prov.get('module_id')!r} invalid"
    if prov.get("record_id") != record_id(record):
        counters["invalid_provenance"] += 1
        return "STALE", f"{ip}/{mod}: record_id mismatch"
    source = prov.get("source") or {}
    spath = str(source.get("path") or "")
    if not spath or os.path.isabs(spath) or not spath.startswith("raw/rtl/original/"):
        counters["invalid_provenance"] += 1
        return "STALE", f"{ip}/{mod}: non-portable or missing source path {spath!r}"
    if not source.get("sha256") or source.get("sha256") != entry["source"]["sha256"] \
            or spath != entry["source"]["path"]:
        counters["invalid_source_identities"] += 1
        return "STALE", f"{ip}/{mod}: source identity does not match current {entry['source']['path']}"
    ir = prov.get("normalized_ir") or {}
    if not ir.get("path") or not (ctx.root / str(ir["path"])).is_file() or os.path.isabs(str(ir["path"])):
        counters["missing_ir_references"] += 1
        return "STALE", f"{ip}/{mod}: normalized IR reference {ir.get('path')!r} missing"
    if ir.get("sha256") != entry["normalized_ir"]["sha256"]:
        counters["invalid_provenance"] += 1
        return "STALE", f"{ip}/{mod}: normalized IR sha256 is not current"
    return None


def _records_state(ctx, records, counters, seen=None):
    if records is None:
        return "STALE", "unreadable JSONL"
    worst, reasons = None, []
    ids = Counter()
    for record in records:
        counters["dataset_records_checked"] += 1
        res = check_record(ctx, record, counters)
        rid = (record.get("provenance") or {}).get("record_id")
        if rid:
            ids[rid] += 1
            if seen is not None:
                seen.append((rid, record.get("ip"), record.get("module")))
        if res:
            reasons.append(res[1])
            worst = "ORPHAN" if res[0] == "ORPHAN" or worst == "ORPHAN" else "STALE"
    dups = [r for r, n in ids.items() if n > 1]
    if dups:
        counters["duplicate_artifacts"] += len(dups)
        reasons.append(f"duplicate record_id(s) {sorted(dups)[:3]}")
        worst = worst or "STALE"
    if worst:
        return worst, "; ".join(reasons[:3]) + (f" (+{len(reasons) - 3} more)" if len(reasons) > 3 else "")
    return "CURRENT", None


# -----------------------------------------------------------------------------
# Classification
# -----------------------------------------------------------------------------

def classify(data_root, use_recorded: bool = True) -> dict:
    """Classify every file of the managed data tree. Read-only.

    ``use_recorded=False`` skips the comparison of canonical IR against the
    previously recorded provenance (used right after the pipeline rewrote IR).
    """
    from scripts.core.paths import find_absolute_paths
    from scripts.core.provenance import PROVENANCE_VERSION, dumps
    from scripts.dataset.leakage import LEAKAGE_SCHEMA_VERSION, SPLIT_SCHEMA_VERSION, SPLITS
    from scripts.dataset.yaml_generator import generate_module_prompt

    root = Path(os.path.abspath(data_root))
    ctx = Context(root, use_recorded=use_recorded)
    expected = ctx.expected_paths()
    counters = Counter()
    entries = []
    split_ids = {}

    # dataset records per split (needed by the split-manifest check)
    for name in DATASET_SPLIT_FILES:
        records = _jsonl(root / f"datasets/pipeline/{name}.jsonl")
        split_ids[name] = {(r.get("provenance") or {}).get("record_id") for r in (records or [])}

    prompts_per_module = Counter()
    v2_cache = {}

    def add(rel, kind, state, reason=None, ip=None, module=None, symlink=False):
        mod_entry = ctx.modules.get((ip, module)) if module else None
        path = root / rel
        entries.append({
            "path": rel,
            "kind": kind,
            "state": state,
            "reason": reason,
            "ip": ip,
            "module": module,
            "module_id": mod_entry["module_id"] if mod_entry else None,
            "source_sha256": mod_entry["source"]["sha256"] if mod_entry else None,
            "sha256": None if symlink or kind in SELF_OUTPUTS.values()
            else _sha(path),
            "symlink": symlink,
        })

    for rel, symlink in _walk(root):
        top = rel.split("/", 1)[0]
        if rel in SELF_OUTPUTS and not symlink:
            continue                    # KF-DQ-006 outputs: added below, presence-independent
        if top in CANONICAL_INPUT_ROOTS:
            continue
        if "/" not in rel and top in ROOT_FILES:
            continue
        exp = expected.get(rel)
        if symlink:
            counters["symlinks"] += 1
            add(rel, "symlink", "UNMANAGED", "symlink in managed data tree", symlink=True)
            continue
        if top not in MANAGED_ROOTS:
            add(rel, "unknown", "UNMANAGED", f"outside the managed roots {MANAGED_ROOTS}")
            continue
        if is_historical(rel):
            add(rel, "historical", "HISTORICAL", "retained historical artifact (policy)")
            continue
        if os.path.basename(rel) == PLACEHOLDER:
            add(rel, "placeholder", "CURRENT")
            continue

        kind, ip, mod = exp if exp else (None, None, None)
        path = root / rel

        if rel.startswith("normalized/"):
            if kind == "canonical_ir":
                recorded = ctx.recorded.get((ip, mod)) or {}
                current = ctx.modules[(ip, mod)]
                if ctx.module_ok(ip, mod) and not ctx.declared(ip, mod):
                    counters["invalid_source_identities"] += 1
                    add(rel, kind, "ORPHAN", f"module {mod} is no longer declared in "
                        f"{current['source']['path']}", ip, mod)
                elif ctx.module_ok(ip, mod) and recorded and \
                        (recorded.get("source") or {}).get("sha256") != current["source"]["sha256"]:
                    counters["invalid_source_identities"] += 1
                    add(rel, kind, "STALE", f"source {current['source']['path']} changed since this IR "
                        "was generated (KF-DQ-005 provenance)", ip, mod)
                elif ctx.module_ok(ip, mod):
                    add(rel, kind, "CURRENT", ip=ip, module=mod)
                else:
                    add(rel, kind, "ORPHAN", f"source {ctx.modules[(ip, mod)]['source']['path']} missing "
                        f"({ctx.modules[(ip, mod)]['transformation']})", ip, mod)
                    counters["invalid_source_identities"] += 1
            elif kind == "semantic_ir":
                state, reason = _semantic_state(ctx, path, ip, mod, counters)
                add(rel, kind, state, reason, ip, mod)
            elif kind == "behavior":
                state, reason = _behavior_state(ctx, root, path, ip, mod, counters)
                add(rel, kind, state, reason, ip, mod)
            elif kind == "structural":
                state, reason = _structural_state(ctx, root, path, ip, mod, counters)
                add(rel, kind, state, reason, ip, mod)
            elif kind == "fsm":
                state, reason = _fsm_state(ctx, root, path, ip, mod, counters)
                add(rel, kind, state, reason, ip, mod)
            elif kind == "ip_metadata":
                state = "CURRENT" if ip in ctx.raw_ips else "ORPHAN"
                add(rel, kind, state, None if state == "CURRENT" else f"IP {ip} has no raw RTL", ip)
            elif kind == "rtl_copy":
                ok = any(a["path"] == rel and a["matches_source"]
                         for m in ctx.provenance["modules"] for a in m["artifacts"])
                add(rel, kind, "CURRENT" if ok else "STALE",
                    None if ok else "RTL copy differs from its canonical source", ip)
            else:
                parts = rel.split("/")
                if len(parts) >= 2 and parts[1] == "fsm":
                    if any(rel.startswith(d + "/") for d in FSM_OBSOLETE_DIRS):
                        add(rel, "fsm", "STALE", "obsolete FSM Analysis v1 document (superseded by "
                            f"{FSM_DIR}, KF-DQ-011.1); remove it", parts[3] if len(parts) > 3 else None)
                        counters["obsolete_schema"] += 1
                    elif rel.startswith(FSM_DIR + "/") and len(parts) == 5 and parts[4].endswith(".json"):
                        add(rel, "fsm", "ORPHAN",
                            f"module {parts[3]}/{parts[4][:-5]} has no canonical IR", parts[3])
                    else:
                        add(rel, "fsm_other", "UNMANAGED",
                            f"not part of the FSM Analysis layout ({FSM_DIR}/<ip>/<module>.json)")
                elif len(parts) >= 2 and parts[1] == "structural":
                    if rel.startswith(STRUCTURAL_DIR + "/") and len(parts) == 5 and parts[4].endswith(".json"):
                        add(rel, "structural", "ORPHAN",
                            f"module {parts[3]}/{parts[4][:-5]} has no canonical IR", parts[3])
                    else:
                        add(rel, "structural_other", "UNMANAGED",
                            f"not part of the Structural Analysis layout ({STRUCTURAL_DIR}/<ip>/<module>.json)")
                elif len(parts) >= 2 and parts[1] == "behavior":
                    if rel.startswith(BEHAVIOR_DIR + "/") and len(parts) == 5 and parts[4].endswith(".json"):
                        add(rel, "behavior", "ORPHAN",
                            f"module {parts[3]}/{parts[4][:-5]} has no canonical IR", parts[3])
                    else:
                        add(rel, "behavior_other", "UNMANAGED",
                            f"not part of the Behavioral Semantics layout ({BEHAVIOR_DIR}/<ip>/<module>.json)")
                elif len(parts) >= 2 and parts[1] == "semantic_ir":
                    if rel.startswith(SEMANTIC_DIR + "/") and len(parts) == 5 and parts[4].endswith(".json"):
                        add(rel, "semantic_ir", "ORPHAN",
                            f"module {parts[3]}/{parts[4][:-5]} has no canonical IR", parts[3])
                    else:
                        add(rel, "semantic_other", "UNMANAGED",
                            f"not part of the Semantic IR layout ({SEMANTIC_DIR}/<ip>/<module>.json)")
                elif len(parts) >= 3 and parts[1] == "ir" and parts[2] not in ctx.ips:
                    add(rel, "ir_other", "ORPHAN", f"IP {parts[2]} has no canonical module IR", parts[2])
                elif len(parts) >= 4 and parts[1] == "ir" and parts[3] == "rtl":
                    add(rel, "rtl_copy", "STALE", "RTL copy not referenced by any canonical module", parts[2])
                else:
                    add(rel, "ir_other", "UNMANAGED", "not part of the canonical IR layout")
            continue

        if rel.startswith("generated/prompts/"):
            match = _PROMPT_RE.match(rel)
            if kind == "prompt":
                prompts_per_module[(ip, mod)] += 1
                want = generate_module_prompt(ctx.specs[(ip, mod)])
                have = path.read_text(encoding="utf-8", errors="replace")
                if have != want:
                    add(rel, kind, "STALE", "content differs from the prompt generated from current IR", ip, mod)
                else:
                    add(rel, kind, "CURRENT", ip=ip, module=mod)
            elif match:
                add(rel, "prompt", "ORPHAN", f"module {match.group(1)}/{match.group(2)} is not canonical",
                    match.group(1), None)
            else:
                stem = os.path.basename(rel).split(".")[0]
                ip_dir = rel.split("/")[2] if rel.count("/") >= 3 else None
                if ip_dir and (ip_dir, stem) in ctx.modules:
                    counters["duplicate_artifacts"] += 1
                    add(rel, "prompt", "UNMANAGED",
                        f"second prompt file for {ip_dir}/{stem} (would be consumed by dataset generation)",
                        ip_dir, stem)
                else:
                    add(rel, "prompt", "UNMANAGED", "not a <module>.generate.txt prompt")
            continue

        if rel.startswith("generated/prompt/"):                       # KF-DQ-012 Prompt v2
            match = _PROMPT_V2_RE.match(rel)
            if kind in ("prompt_v2", "prompt_v2_sidecar"):
                state, reason = _prompt_v2_state(ctx, root, rel, kind, ip, mod, counters, v2_cache)
                add(rel, kind, state, reason, ip, mod)
            elif match and match.group(3) != "behavior_aware":
                add(rel, "prompt_v2_other", "UNMANAGED", f"unsupported Prompt v2 variant {match.group(3)!r}")
            elif match:
                add(rel, "prompt_v2" if match.group(4) == "txt" else "prompt_v2_sidecar", "ORPHAN",
                    f"module {match.group(1)}/{match.group(2)} is not canonical", match.group(1))
            else:
                add(rel, "prompt_v2_other", "UNMANAGED",
                    f"not part of the Prompt v2 layout ({PROMPT_V2_DIR}/<ip>/<module>.<variant>.txt|json)")
            continue

        if rel.startswith("generated/rtl/"):
            sha = _sha(path)
            raw = ctx.raw_by_sha.get(sha)
            ip_dir = rel.split("/")[3] if rel.startswith("generated/rtl/pipeline/") and rel.count("/") >= 4 else None
            if raw and ip_dir and ip_dir not in ctx.ips:
                add(rel, "generated_rtl", "ORPHAN", f"copy of {raw}; IP {ip_dir} has no canonical IR", ip_dir)
            elif raw:
                counters["duplicate_artifacts"] += 1
                add(rel, "generated_rtl", "STALE", f"superseded duplicate of {raw} (previous pipeline layout)", ip_dir)
            else:
                add(rel, "generated_rtl", "UNMANAGED", "generated RTL not produced by the current pipeline", ip_dir)
            continue

        if kind is None:
            dup = ctx.canonical_ir_sha.get(_sha(path))
            if dup and rel.startswith(("analysis/", "generated/")):
                counters["duplicate_artifacts"] += 1
                add(rel, "superseded_copy", "STALE", f"superseded duplicate of canonical {dup} (previous layout)")
            else:
                add(rel, "unknown", "UNMANAGED", "not part of the current output contract")
            continue

        # ------------------------------------------------------------- singletons
        if kind == "dataset_split":
            records = _jsonl(path)
            state, reason = _records_state(ctx, records, counters)
            add(rel, kind, state, reason)
        elif kind == "dataset_records":
            state, reason = _records_state(ctx, _jsonl(path), counters)
            add(rel, kind, state, reason)
        elif kind == "dataset_manifest":
            data = _load_json(path)
            if not isinstance(data, dict):
                add(rel, kind, "STALE", "unreadable")
            else:
                gone = sorted(k for k in data if tuple(k.split("/")[:2]) not in ctx.modules)
                add(rel, kind, "ORPHAN" if gone else "CURRENT",
                    f"entries for non-canonical modules {gone[:3]}" if gone else None)
        elif kind == "dataset_stats":
            data = _load_json(path)
            total = sum(len(_jsonl(root / f"datasets/pipeline/{n}.jsonl") or []) for n in DATASET_SPLIT_FILES)
            ok = isinstance(data, dict) and data.get("total_examples") == total
            add(rel, kind, "CURRENT" if ok else "STALE", None if ok else "total_examples does not match datasets")
        elif kind == "split_manifest":
            data = _load_json(path)
            reason = None
            if not isinstance(data, dict):
                reason = "unreadable"
            elif (data.get("split_schema_version"), data.get("leakage_schema_version")) != \
                    (SPLIT_SCHEMA_VERSION, LEAKAGE_SCHEMA_VERSION):
                counters["obsolete_schema"] += 1
                reason = "obsolete split/leakage schema version"
            state = "STALE" if reason else "CURRENT"
            if not reason:
                bad = []
                for split in SPLITS:
                    for e in data.get(split, []):
                        counters["split_records_checked"] += 1
                        if (e.get("ip"), e.get("module")) not in ctx.modules:
                            bad.append(("ORPHAN", f"{split}: {e.get('ip')}/{e.get('module')} is not canonical"))
                        elif e.get("record_id") not in split_ids.get(split, set()):
                            bad.append(("STALE", f"{split}: record {e.get('record_id')} not in {split}.jsonl"))
                if bad:
                    state = "ORPHAN" if any(s == "ORPHAN" for s, _ in bad) else "STALE"
                    reason = "; ".join(r for _, r in bad[:3])
            add(rel, kind, state, reason)
        elif kind == "provenance_manifest":
            data = _load_json(path)
            if not isinstance(data, dict) or data.get("provenance_version") != PROVENANCE_VERSION:
                counters["obsolete_schema"] += 1
                add(rel, kind, "STALE", "missing or obsolete provenance_version")
            elif dumps(data) != dumps(ctx.provenance):
                add(rel, kind, "STALE", "differs from the provenance recomputed from current inputs")
            else:
                add(rel, kind, "CURRENT")
        elif kind in ("provenance_report", "split_leakage_report"):
            data = _load_json(path)
            version = PROVENANCE_VERSION if kind == "provenance_report" else LEAKAGE_SCHEMA_VERSION
            key = "provenance_version" if kind == "provenance_report" else "leakage_schema_version"
            ok = isinstance(data, dict) and data.get(key) == version and data.get("status") == "PASS"
            add(rel, kind, "CURRENT" if ok else "STALE", None if ok else "obsolete schema or non-PASS report")
        elif kind == "semantic_ir_report":
            state, reason = _semantic_report_state(root, path)
            add(rel, kind, state, reason)
        elif kind == "behavior_report":
            state, reason = _behavior_report_state(root, path)
            add(rel, kind, state, reason)
        elif kind == "structural_report":
            state, reason = _structural_report_state(root, path)
            add(rel, kind, state, reason)
        elif kind == "fsm_report":
            state, reason = _fsm_report_state(root, path)
            add(rel, kind, state, reason)
        elif kind == "prompt_v2_report":
            state, reason = _prompt_v2_report_state(root, path)
            add(rel, kind, state, reason)
        elif kind == "prompt_leakage_classification":
            state, reason = _classification_state(root, path)
            add(rel, kind, state, reason)
        elif kind in ("multitask_split", "multitask_registry", "dataset_v2_report"):
            state, reason = _multitask_state(ctx, root, rel, kind, counters)
            add(rel, kind, state, reason)
        elif kind == "pipeline_stats":
            data = _load_json(path)
            ok = isinstance(data, dict) and data.get("ips") == len(ctx.ips) and data.get("modules") == len(ctx.modules)
            add(rel, kind, "CURRENT" if ok else "STALE", None if ok else "IP/module counts are not current")
        else:
            add(rel, kind, "UNMANAGED", "no rule for this artifact kind")

    # KF-DQ-006 outputs are listed whether or not they exist yet, so the
    # inventory does not depend on itself; check() verifies their content.
    for rel, kind in sorted(SELF_OUTPUTS.items()):
        if not (root / rel).is_symlink():
            entries.append({"path": rel, "kind": kind, "state": "CURRENT", "reason": None, "ip": None,
                            "module": None, "module_id": None, "source_sha256": None, "sha256": None,
                            "symlink": False})

    present = {e["path"] for e in entries}
    missing = sorted(p for p, (kind, _, _) in expected.items()
                     if p not in present and kind not in SELF_OUTPUTS.values())

    # absolute / non-portable paths inside text artifacts of the active tree
    for e in entries:
        if e["state"] == "CURRENT" and not e["symlink"] and e["sha256"] is not None \
                and e["path"].endswith((".json", ".jsonl", ".yaml", ".txt")):
            text = (root / e["path"]).read_text(encoding="utf-8", errors="replace")
            if find_absolute_paths(text):
                counters["absolute_paths"] += 1
                e["state"], e["reason"] = "STALE", "contains a non-portable absolute path"

    for e in entries:
        e["remediation"] = remediation(e)
    entries.sort(key=lambda e: e["path"])
    return {"entries": entries, "missing": missing, "counters": counters, "expected": len(expected),
            "context": ctx}


def _multitask_state(ctx, root: Path, rel: str, kind: str, counters) -> tuple[str, str | None]:
    """KF-DQ-013: a multi-task split file is CURRENT when every record has the current dataset schema,
    a canonical module and current source hashes; the registry when it equals the forge registry; the
    report when it PASSed for exactly the current dataset files."""
    from scripts.multitask import registry as MG
    from scripts.multitask import validator as MV

    path = root / rel
    if kind == "multitask_split":
        sha_cache = {}
        recs = _jsonl(path)
        if recs is None:
            return "STALE", "unreadable multi-task dataset file"
        for rec in recs:
            counters["multitask_records_checked"] += 1
            if MV.record_type_problems(rec):  # KF-DQ-013.1 AC-017: typed shape before any dereference
                return "STALE", "malformed multi-task record"
            if rec["schema"] != MG.DATASET_SCHEMA:
                counters["obsolete_schema"] += 1
                return "STALE", "obsolete or unreadable dataset schema"
            m = rec["module"]
            if (m["ip"], m["name"]) not in ctx.modules:
                return "ORPHAN", f"record for non-canonical module {m['ip']}/{m['name']}"
            for src in rec["sources"]:
                sp = root / src["path"]
                if src["path"] not in sha_cache:
                    sha_cache[src["path"]] = _sha(sp) if sp.is_file() else None
                if sha_cache[src["path"]] != src["sha256"]:
                    return "STALE", f"derived from an older {src['layer']} revision ({src['path']})"
        return "CURRENT", None
    data = _load_json(path)
    if kind == "multitask_registry":
        if not isinstance(data, dict):
            return "STALE", "unreadable"
        strip = lambda d: {k: ([{x: y for x, y in t.items() if x != "records"} for t in v] if k == "tasks" else v)
                           for k, v in d.items()}
        if strip(data) != strip(MG.document()):
            counters["obsolete_schema"] += 1
            return "STALE", "task registry differs from the forge registry"
        return "CURRENT", None
    if not isinstance(data, dict) or data.get("status") != "PASS" or data.get("dataset_schema") != MG.DATASET_SCHEMA:
        return "STALE", "obsolete or non-PASS dataset report"
    lines = {}
    for name in MG.SPLITS:
        f = root / f"{MG.OUTPUT_DIR}/{name}.jsonl"
        if not f.is_file():
            return "STALE", "dataset files missing"
        try:
            lines[name] = f.read_bytes().decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return "STALE", "dataset files are not valid UTF-8"
    if data.get("dataset_identity") != MV.dataset_identity(lines):
        return "STALE", "report does not describe the current dataset"
    return "CURRENT", None


def _semantic_state(ctx, path: Path, ip, mod, counters) -> tuple[str, str | None]:
    """KF-DQ-008: a Semantic IR v2 document is CURRENT only if it carries the current
    schema version, module identity and source sha256 and passes the validator."""
    from scripts.semantic_ir import model as SM
    from scripts.semantic_ir.validator import validate_module

    current = ctx.modules[(ip, mod)]
    if not ctx.module_ok(ip, mod):
        counters["invalid_source_identities"] += 1
        return "ORPHAN", f"source {current['source']['path']} missing ({current['transformation']})"
    doc = _load_json(path)
    if not isinstance(doc, dict):
        return "STALE", "unreadable Semantic IR document"
    if doc.get("schema") != {"name": SM.SCHEMA_NAME, "version": SM.SCHEMA_VERSION}:
        counters["obsolete_schema"] += 1
        return "STALE", f"obsolete semantic schema {doc.get('schema')!r} (expected v{SM.SCHEMA_VERSION})"
    m = doc.get("module") or {}
    if m.get("module_id") != current["module_id"] or m.get("ip") != ip or m.get("name") != mod:
        counters["invalid_module_identities"] += 1
        return "STALE", "module identity does not match the canonical module"
    src = m.get("source") or {}
    if src.get("path") != current["source"]["path"] or src.get("sha256") != current["source"]["sha256"]:
        counters["invalid_source_identities"] += 1
        return "STALE", f"generated from an older {current['source']['path']} (source sha256 differs)"
    problems = validate_module(doc, set(ctx.module_ids))
    if problems:
        counters["invalid_semantic_ir"] += 1
        return "STALE", f"fails Semantic IR validation: [{problems[0][0]}] {problems[0][1]}"[:300]
    return "CURRENT", None


def _semantic_report_state(root: Path, path: Path) -> tuple[str, str | None]:
    from scripts.semantic_ir import model as SM
    from scripts.semantic_ir.validator import corpus_sha256

    rep = _load_json(path)
    if not isinstance(rep, dict) or rep.get("schema") != {"name": SM.SCHEMA_NAME, "version": SM.SCHEMA_VERSION}:
        return "STALE", "missing or obsolete semantic schema"
    if rep.get("status") != "PASS":
        return "STALE", "semantic gate did not pass"
    if rep.get("corpus_sha256") != corpus_sha256(root):
        return "STALE", "Semantic IR corpus changed since this report was written"
    return "CURRENT", None


def _behavior_state(ctx, root: Path, path: Path, ip, mod, counters) -> tuple[str, str | None]:
    """KF-DQ-009: a Behavioral Semantics v1 document is CURRENT only if it was derived from the
    current Semantic IR v2 document (sha256), re-analysis reproduces it byte for byte and it validates."""
    import hashlib

    from scripts.behavior import analyzer as BA
    from scripts.behavior import model as BM
    from scripts.behavior.validator import validate_module

    current = ctx.modules[(ip, mod)]
    if not ctx.module_ok(ip, mod):
        counters["invalid_source_identities"] += 1
        return "ORPHAN", f"source {current['source']['path']} missing ({current['transformation']})"
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        doc = json.loads(text)
    except ValueError:
        return "STALE", "unreadable Behavioral Semantics document"
    if not isinstance(doc, dict) or doc.get("schema") != {"name": BM.SCHEMA_NAME, "version": BM.SCHEMA_VERSION}:
        counters["obsolete_schema"] += 1
        return "STALE", f"obsolete behavioral schema {doc.get('schema') if isinstance(doc, dict) else None!r}"
    m = doc.get("module") or {}
    if m.get("module_id") != current["module_id"] or m.get("ip") != ip or m.get("name") != mod:
        counters["invalid_module_identities"] += 1
        return "STALE", "module identity does not match the canonical module"
    srel = BA.semantic_rel(ip, mod)
    spath = root / srel
    if not spath.is_file():
        counters["invalid_behavior"] += 1
        return "STALE", f"Semantic IR input {srel} missing"
    raw = spath.read_bytes()
    ssha = hashlib.sha256(raw).hexdigest()
    if (m.get("semantic_ir") or {}).get("sha256") != ssha or (m.get("source") or {}).get("sha256") != current["source"]["sha256"]:
        counters["invalid_behavior"] += 1
        return "STALE", "derived from an older Semantic IR / source revision"
    try:
        sem = json.loads(raw)
        fresh = BM.dumps(BA.analyze(sem, srel, ssha))
    except Exception as exc:                                  # noqa: BLE001 - reported as STALE
        counters["invalid_behavior"] += 1
        return "STALE", f"Semantic IR input not analysable: {exc}"[:300]
    if fresh != text:
        counters["invalid_behavior"] += 1
        return "STALE", "differs from a re-analysis of the current Semantic IR"
    problems = validate_module(doc, sem, ssha)
    if problems:
        counters["invalid_behavior"] += 1
        return "STALE", f"fails Behavioral Semantics validation: [{problems[0][0]}] {problems[0][1]}"[:300]
    return "CURRENT", None


def _behavior_report_state(root: Path, path: Path) -> tuple[str, str | None]:
    from scripts.behavior import model as BM
    from scripts.behavior.validator import corpus_sha256

    rep = _load_json(path)
    if not isinstance(rep, dict) or rep.get("schema") != {"name": BM.SCHEMA_NAME, "version": BM.SCHEMA_VERSION}:
        return "STALE", "missing or obsolete behavioral schema"
    if rep.get("status") != "PASS":
        return "STALE", "behavior gate did not pass"
    if rep.get("corpus_sha256") != corpus_sha256(root):
        return "STALE", "Behavioral Semantics corpus changed since this report was written"
    return "CURRENT", None


def _structural_state(ctx, root: Path, path: Path, ip, mod, counters) -> tuple[str, str | None]:
    """KF-DQ-010: a Structural Analysis v1 document is CURRENT only if it was derived from the current
    Semantic IR v2 and Behavioral Semantics v1 documents (sha256), with the current schema / analyzer,
    re-analysis reproduces it byte for byte and it validates."""
    from scripts.structural import analyzer as TA
    from scripts.structural import model as TM
    from scripts.structural.validator import validate_module

    current = ctx.modules[(ip, mod)]
    if not ctx.module_ok(ip, mod):
        counters["invalid_source_identities"] += 1
        return "ORPHAN", f"source {current['source']['path']} missing ({current['transformation']})"
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        doc = json.loads(text)
    except ValueError:
        return "STALE", "unreadable Structural Analysis document"
    if not isinstance(doc, dict) or doc.get("schema") != {"name": TM.SCHEMA_NAME, "version": TM.SCHEMA_VERSION} \
            or (doc.get("versions") or {}).get("analyzer") != TM.ANALYZER_VERSION:
        counters["obsolete_schema"] += 1
        return "STALE", f"obsolete structural schema / analyzer {doc.get('schema') if isinstance(doc, dict) else None!r}"
    m = doc.get("module") or {}
    if m.get("module_id") != current["module_id"] or m.get("ip") != ip or m.get("name") != mod:
        counters["invalid_module_identities"] += 1
        return "STALE", "module identity does not match the canonical module"
    try:
        sem, srel, ssha, beh, brel, bsha = TA.load_inputs(root, ip, mod)
    except TA.AnalysisError as exc:
        counters["invalid_structural"] += 1
        return "STALE", f"structural input unavailable: {exc}"[:300]
    if (m.get("semantic_ir") or {}).get("sha256") != ssha:
        counters["invalid_structural"] += 1
        return "STALE", "derived from an older Semantic IR revision"
    if (m.get("behavior") or {}).get("sha256") != bsha:
        counters["invalid_structural"] += 1
        return "STALE", "derived from an older Behavioral Semantics revision"
    inv = getattr(ctx, "_structural_inventory", None)
    if inv is None:
        inv = ctx._structural_inventory = TA.inventory(root)
    try:
        fresh = TM.dumps(TA.analyze(sem, srel, ssha, beh, brel, bsha, inv))
    except Exception as exc:                                  # noqa: BLE001 - reported as STALE
        counters["invalid_structural"] += 1
        return "STALE", f"structural inputs not analysable: {exc}"[:300]
    if fresh != text:
        counters["invalid_structural"] += 1
        return "STALE", "differs from a re-analysis of the current Semantic IR / Behavioral Semantics"
    problems = validate_module(doc, sem, beh, ssha, bsha)
    if problems:
        counters["invalid_structural"] += 1
        return "STALE", f"fails Structural Analysis validation: [{problems[0][0]}] {problems[0][1]}"[:300]
    return "CURRENT", None


def _structural_report_state(root: Path, path: Path) -> tuple[str, str | None]:
    import hashlib

    from scripts.structural import model as TM
    from scripts.structural.validator import SPLIT_MANIFEST, corpus_sha256

    rep = _load_json(path)
    if not isinstance(rep, dict) or rep.get("schema") != {"name": TM.SCHEMA_NAME, "version": TM.SCHEMA_VERSION}:
        return "STALE", "missing or obsolete structural schema"
    if rep.get("status") != "PASS":
        return "STALE", "structural gate did not pass"
    if rep.get("corpus_sha256") != corpus_sha256(root):
        return "STALE", "Structural Analysis corpus changed since this report was written"
    split = root / SPLIT_MANIFEST
    if split.is_file() and (rep.get("leakage") or {}).get("split_manifest_sha256") != hashlib.sha256(split.read_bytes()).hexdigest():
        return "STALE", "split manifest changed since the structural leakage check"
    return "CURRENT", None


def _fsm_state(ctx, root: Path, path: Path, ip, mod, counters) -> tuple[str, str | None]:
    """KF-DQ-011 / KF-DQ-011.1: an FSM Analysis v2 document is CURRENT only if it was derived from the current
    Semantic IR v2, Behavioral Semantics v1 and Structural Analysis v1 documents (sha256), with the
    current schema / analyzer, re-analysis reproduces it byte for byte and it validates."""
    from scripts.fsm import analyzer as FA
    from scripts.fsm import model as FM
    from scripts.fsm.validator import validate_module

    current = ctx.modules[(ip, mod)]
    if not ctx.module_ok(ip, mod):
        counters["invalid_source_identities"] += 1
        return "ORPHAN", f"source {current['source']['path']} missing ({current['transformation']})"
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        doc = json.loads(text)
    except ValueError:
        return "STALE", "unreadable FSM Analysis document"
    if not isinstance(doc, dict) or doc.get("schema") != {"name": FM.SCHEMA_NAME, "version": FM.SCHEMA_VERSION} \
            or (doc.get("versions") or {}).get("analyzer") != FM.ANALYZER_VERSION:
        counters["obsolete_schema"] += 1
        return "STALE", f"obsolete FSM schema / analyzer {doc.get('schema') if isinstance(doc, dict) else None!r}"
    m = doc.get("module") or {}
    if m.get("module_id") != current["module_id"] or m.get("ip") != ip or m.get("name") != mod:
        counters["invalid_module_identities"] += 1
        return "STALE", "module identity does not match the canonical module"
    try:
        inputs = FA.load_inputs(root, ip, mod)
    except FA.AnalysisError as exc:
        counters["invalid_fsm"] += 1
        return "STALE", f"FSM input unavailable: {exc}"[:300]
    for key, sha, what in (("semantic_ir", inputs[2], "Semantic IR"), ("behavior", inputs[5], "Behavioral Semantics"),
                           ("structural", inputs[8], "Structural Analysis")):
        if (m.get(key) or {}).get("sha256") != sha:
            counters["invalid_fsm"] += 1
            return "STALE", f"derived from an older {what} revision"
    try:
        fresh = FM.dumps(FA.analyze(*inputs))
    except Exception as exc:                                  # noqa: BLE001 - reported as STALE
        counters["invalid_fsm"] += 1
        return "STALE", f"FSM inputs not analysable: {exc}"[:300]
    if fresh != text:
        counters["invalid_fsm"] += 1
        return "STALE", "differs from a re-analysis of the current upstream evidence"
    problems = validate_module(doc, *inputs)
    if problems:
        counters["invalid_fsm"] += 1
        return "STALE", f"fails FSM Analysis validation: [{problems[0][0]}] {problems[0][1]}"[:300]
    return "CURRENT", None


def _prompt_v2_state(ctx, root: Path, rel, kind, ip, mod, counters, cache) -> tuple[str, str | None]:
    """KF-DQ-012 (AC-580 .. AC-615): a Prompt v2 text or sidecar is CURRENT only if its pair exists, it was
    derived from the current Semantic IR, Behavioral Semantics, Structural Analysis and FSM Analysis
    documents, regeneration is byte-identical and the pair validates."""
    from scripts.prompt_v2 import model as PM
    from scripts.prompt_v2 import render as PR
    from scripts.prompt_v2.validator import validate_module

    if not ctx.module_ok(ip, mod):
        counters["invalid_source_identities"] += 1
        return "ORPHAN", f"source of {ip}/{mod} missing"
    prel, srel = PM.prompt_rel(ip, mod), PM.sidecar_rel(ip, mod)
    if kind == "prompt_v2_sidecar" and not (root / prel).is_file():
        return "ORPHAN", f"sidecar without its prompt {prel}"
    if kind == "prompt_v2" and not (root / srel).is_file():
        return "STALE", f"prompt without its sidecar {srel}"
    if (ip, mod) not in cache:
        try:
            inputs = PR.load_inputs(root, ip, mod)
        except PR.PromptError as exc:
            counters["invalid_prompt_v2"] += 1
            cache[(ip, mod)] = ("STALE", f"Prompt v2 input unavailable: {exc}"[:300])
        else:
            problems = validate_module((root / prel).read_text(encoding="utf-8", errors="replace"),
                                       (root / srel).read_text(encoding="utf-8", errors="replace"), inputs)
            if problems:
                counters["invalid_prompt_v2"] += 1
                code, msg = problems[0]
                cache[(ip, mod)] = ("STALE", f"[{code}] {msg}"[:300])
            else:
                cache[(ip, mod)] = ("CURRENT", None)
    return cache[(ip, mod)]


def _prompt_v2_report_state(root: Path, path: Path) -> tuple[str, str | None]:
    from scripts.prompt_v2 import model as PM
    from scripts.prompt_v2.validator import corpus_sha256

    rep = _load_json(path)
    if not isinstance(rep, dict) or rep.get("schema") != {"name": PM.SCHEMA_NAME + "-report", "version": 1}:
        return "STALE", "missing or obsolete Prompt v2 report schema"
    if rep.get("status") != "PASS":
        return "STALE", "Prompt v2 gate did not pass"
    if rep.get("corpus_sha256") != corpus_sha256(root):
        return "STALE", "Prompt v2 corpus changed since this report was written"
    return "CURRENT", None


def _classification_state(root: Path, path: Path) -> tuple[str, str | None]:
    from scripts.prompt_v2 import classify as PC

    rep = _load_json(path)
    if not isinstance(rep, dict):
        return "STALE", "unreadable classification report"
    if PC.validate(rep):
        return "STALE", "obsolete or inconsistent classification report"
    try:
        fresh = PC.build(root)
    except PC.ClassificationError as exc:
        return "STALE", f"classification inputs unavailable: {exc}"[:300]
    if PC.dumps(fresh) != PC.dumps(rep):
        return "STALE", "split manifest or fingerprints changed since the classification was written"
    return "CURRENT", None


def _fsm_report_state(root: Path, path: Path) -> tuple[str, str | None]:
    import hashlib

    from scripts.fsm import model as FM
    from scripts.fsm.validator import SPLIT_MANIFEST, corpus_sha256

    rep = _load_json(path)
    if not isinstance(rep, dict) or rep.get("schema") != {"name": FM.SCHEMA_NAME, "version": FM.SCHEMA_VERSION}:
        return "STALE", "missing or obsolete FSM schema"
    if rep.get("status") != "PASS":
        return "STALE", "FSM gate did not pass"
    if rep.get("corpus_sha256") != corpus_sha256(root):
        return "STALE", "FSM Analysis corpus changed since this report was written"
    split = root / SPLIT_MANIFEST
    if split.is_file() and (rep.get("leakage") or {}).get("split_manifest_sha256") != hashlib.sha256(split.read_bytes()).hexdigest():
        return "STALE", "split manifest changed since the FSM leakage check"
    return "CURRENT", None


def remediation(entry: dict) -> str:
    state, rel = entry["state"], entry["path"]
    if state in ("CURRENT", "HISTORICAL"):
        return "NONE"
    if entry["symlink"]:
        return "MANUAL"                                   # never followed or deleted automatically
    if not rel.startswith(CLEANABLE_PREFIXES) or is_historical(rel):
        return "REGENERATE"                               # canonical IR area: rerun the pipeline
    if state == "UNMANAGED":
        return "QUARANTINE"
    if entry["kind"] in PIPELINE_OUTPUT_KINDS or (entry["kind"] == "prompt" and state == "STALE"):
        return "REGENERATE"
    return "REMOVE"


# -----------------------------------------------------------------------------
# Inventory, report, gate
# -----------------------------------------------------------------------------

def build_inventory(result: dict) -> dict:
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "policy": {
            "states": list(STATES),
            "historical_prefixes": list(HISTORICAL_PREFIXES),
            "historical_files": list(HISTORICAL_FILES),
            "cleanable_prefixes": list(CLEANABLE_PREFIXES),
            "quarantine_root": QUARANTINE_ROOT,
            "freshness": "canonical identities and recomputation; filesystem timestamps are not used",
        },
        "counts": dict(sorted(Counter(e["state"] for e in result["entries"]).items())),
        "artifacts": [
            {k: e[k] for k in ("path", "kind", "state", "ip", "module", "module_id", "source_sha256", "sha256")}
            for e in result["entries"]
        ],
    }


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def build_report(result: dict, inventory_status: str | None, override: bool = False) -> dict:
    states = Counter(e["state"] for e in result["entries"])
    c = result["counters"]
    plan = [
        {"path": e["path"], "state": e["state"], "action": e["remediation"], "reason": e["reason"]}
        for e in result["entries"] if e["remediation"] != "NONE"
    ] + [{"path": p, "state": "MISSING", "action": "REGENERATE", "reason": "expected artifact missing"}
         for p in result["missing"]]
    plan.sort(key=lambda p: p["path"])
    problems = [f"{p['state']}: {p['path']} — {p['reason']}" for p in plan]
    if inventory_status not in (None, "consistent"):
        problems.append(f"artifact inventory {inventory_status}")
    report = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_records_checked": len(result["entries"]),
        "expected_artifacts": result["expected"],
        **{s.lower(): states.get(s, 0) for s in STATES},
        "missing_expected": len(result["missing"]),
        "duplicate_artifacts": c["duplicate_artifacts"],
        "invalid_provenance": c["invalid_provenance"],
        "invalid_source_identities": c["invalid_source_identities"],
        "invalid_module_identities": c["invalid_module_identities"],
        "missing_ir_references": c["missing_ir_references"],
        "invalid_semantic_ir": c["invalid_semantic_ir"],
        "invalid_behavior": c["invalid_behavior"],
        "invalid_structural": c["invalid_structural"],
        "invalid_fsm": c["invalid_fsm"],
        "invalid_prompt_v2": c["invalid_prompt_v2"],
        "obsolete_schema": c["obsolete_schema"],
        "absolute_paths": c["absolute_paths"],
        "symlinks": c["symlinks"],
        "dataset_records_checked": c["dataset_records_checked"],
        "split_records_checked": c["split_records_checked"],
        "multitask_records_checked": c["multitask_records_checked"],
        "cleanup_candidates": sum(1 for p in plan if p["action"] in ("REMOVE", "QUARANTINE")),
        "remediation_counts": dict(sorted(Counter(p["action"] for p in plan).items())),
        "inventory_status": inventory_status or "not checked",
        "override": override,
        "remediation_plan": plan,
        "problems": problems[:200],
    }
    report["status"] = "FAIL" if problems else "PASS"
    return report


def check(data_root, scope: str = "all") -> dict:
    """Read-only gate. ``scope="inputs"`` ignores artifacts the dataset stage will rewrite."""
    root = Path(os.path.abspath(data_root))
    result = classify(root, use_recorded=(scope != "inputs"))
    if scope == "inputs":
        result["entries"] = [e for e in result["entries"] if e["kind"] not in PIPELINE_OUTPUT_KINDS]
        result["missing"] = [p for p in result["missing"]
                             if not p.startswith(("datasets/", "splits/", "manifests/", "analysis/",
                                                  PROMPT_V2_DIR + "/"))]
        return build_report(result, None)
    inventory = build_inventory(result)
    stored = root / INVENTORY_PATH
    if not stored.is_file():
        inventory_status = "absent"
    elif stored.read_text(encoding="utf-8") != dumps(inventory):
        inventory_status = "differs from recomputation"
    else:
        inventory_status = "consistent"
    report = build_report(result, inventory_status)
    rep = root / REPORT_PATH
    if inventory_status == "consistent" and rep.is_file() and rep.read_text(encoding="utf-8") != dumps(report):
        report["problems"].append("stale artifact report differs from recomputation")
        report["status"] = "FAIL"
    return report


def write(data_root, override: bool = False) -> dict:
    """Write inventory + report (deterministic), then return the verified report."""
    root = Path(os.path.abspath(data_root))
    result = classify(root)
    inventory = build_inventory(result)
    (root / "manifests").mkdir(parents=True, exist_ok=True)
    (root / INVENTORY_PATH).write_text(dumps(inventory), encoding="utf-8")
    report = build_report(result, "consistent", override=override)
    (root / "analysis/reports").mkdir(parents=True, exist_ok=True)
    (root / REPORT_PATH).write_text(dumps(report), encoding="utf-8")
    return check(root) if not override else report


def format_report(report: dict) -> str:
    rows = ["artifact_records_checked", "expected_artifacts", "current", "stale", "orphan", "historical",
            "unmanaged", "missing_expected", "duplicate_artifacts", "invalid_provenance",
            "invalid_source_identities", "invalid_module_identities", "missing_ir_references", "invalid_semantic_ir", "invalid_behavior",
            "invalid_structural", "invalid_fsm", "invalid_prompt_v2", "obsolete_schema", "absolute_paths", "symlinks", "dataset_records_checked",
            "split_records_checked", "multitask_records_checked", "cleanup_candidates", "remediation_counts",
            "inventory_status"]
    lines = [f"Stale artifact check: {report['status']}" + (" (override)" if report.get("override") else "")]
    lines += [f"  {k.replace('_', ' '):26s}: {report.get(k)}" for k in rows if k in report]
    lines += [f"  [FAIL] {p}" for p in report["problems"][:30]]
    if len(report["problems"]) > 30:
        lines.append(f"  ... {len(report['problems']) - 30} more (see remediation_plan)")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Safe cleanup
# -----------------------------------------------------------------------------

class UnsafePath(ValueError):
    pass


def validate_cleanup_path(root: Path, rel: str) -> Path:
    """Return the absolute path for ``rel`` or raise ``UnsafePath``."""
    pure = PurePosixPath(rel)
    if not rel or pure.is_absolute() or os.path.isabs(rel) or "\\" in rel:
        raise UnsafePath(f"{rel!r}: not a repository-relative path")
    if any(part in ("..", ".", "") for part in pure.parts):
        raise UnsafePath(f"{rel!r}: path traversal")
    if not rel.startswith(CLEANABLE_PREFIXES) or is_historical(rel):
        raise UnsafePath(f"{rel!r}: outside the cleanable generated roots")
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafePath(f"{rel!r}: symlink in path ({current.relative_to(root).as_posix()})")
    resolved = Path(os.path.realpath(current))
    if os.path.commonpath([str(resolved), str(Path(os.path.realpath(root)))]) != str(Path(os.path.realpath(root))):
        raise UnsafePath(f"{rel!r}: resolves outside the data root")
    if not current.is_file():
        raise UnsafePath(f"{rel!r}: not a regular file")
    return current


def cleanup(data_root, apply: bool = False, plan: list | None = None) -> list:
    """Execute (or, by default, only validate and list) REMOVE / QUARANTINE actions.

    Every action is validated before anything is changed; one unsafe entry
    aborts the whole cleanup.  Returns the deterministic action log.
    """
    root = Path(os.path.abspath(data_root))
    if plan is None:
        plan = check(root)["remediation_plan"]
    actions = sorted((p for p in plan if p["action"] in ("REMOVE", "QUARANTINE")), key=lambda p: p["path"])
    resolved = []
    for item in actions:
        src = validate_cleanup_path(root, item["path"])
        dest = None
        if item["action"] == "QUARANTINE":
            dest_rel = f"{QUARANTINE_ROOT}/{item['path']}"
            dest = root / dest_rel
            if dest.exists() or dest.is_symlink():
                raise UnsafePath(f"{item['path']!r}: quarantine target {dest_rel} already exists")
        resolved.append((item, src, dest))
    log = []
    for item, src, dest in resolved:
        entry = {"path": item["path"], "action": item["action"], "state": item["state"],
                 "reason": item["reason"], "applied": apply}
        if dest is not None:
            entry["to"] = dest.relative_to(root).as_posix()
        if apply:
            if dest is None:
                src.unlink()
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
            parent = src.parent
            while parent != root and parent.relative_to(root).as_posix().startswith(
                    tuple(p.rstrip("/") for p in CLEANABLE_PREFIXES)) and not any(parent.iterdir()) \
                    and parent.relative_to(root).as_posix() not in {p.rstrip("/") for p in CLEANABLE_PREFIXES}:
                parent.rmdir()
                parent = parent.parent
        log.append(entry)
    return log


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="KF-DQ-006 stale generated artifact gate")
    parser.add_argument("--data-root", default=os.environ.get("KRITVA_FORGE_DATA_ROOT"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="read-only gate (default)")
    mode.add_argument("--report", action="store_true", help="write inventory + report, then verify")
    mode.add_argument("--clean", action="store_true", help="list cleanup actions (dry run unless --apply)")
    parser.add_argument("--apply", action="store_true", help="with --clean: execute REMOVE / QUARANTINE")
    parser.add_argument("--json", help="write the report (or cleanup log) JSON here")
    args = parser.parse_args(argv)
    if args.apply and not args.clean:
        parser.error("--apply requires --clean")
    if not args.data_root:
        from scripts.core.paths import default_data_root
        args.data_root = str(default_data_root())

    if args.clean:
        try:
            log = cleanup(args.data_root, apply=args.apply)
        except UnsafePath as exc:
            print(f"[STOP] unsafe cleanup entry, nothing changed: {exc}")
            return 2
        verb = "applied" if args.apply else "dry run"
        print(f"Stale artifact cleanup ({verb}): {len(log)} action(s)")
        for item in log:
            print(f"  {item['action']:10s} {item['path']}" + (f" -> {item['to']}" if "to" in item else "")
                  + f"  [{item['state']}: {item['reason']}]")
        if args.json:
            Path(args.json).write_text(dumps(log), encoding="utf-8")
        return 0

    report = write(args.data_root) if args.report else check(args.data_root)
    print(format_report(report))
    if args.json:
        Path(args.json).write_text(dumps(report), encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    raise SystemExit(main())
