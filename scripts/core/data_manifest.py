#!/usr/bin/env python3
# =============================================================================
# Copyright (c) 2026 KritvaOS
# SPDX-License-Identifier: Apache-2.0
#
# File        : data_manifest.py
# Description : Canonical data manifest generation, validation and publication gate (KF-DQ-007)
#
# Component   : Kritva Forge
# Module      : core
# Layer       : Development Infrastructure
#
# Author      : Kritva Forge Team
# Created     : 02-10-2026
# =============================================================================
"""Canonical data manifest (KF-DQ-007).

``manifests/data_manifest.json`` is the single authoritative inventory of the
Kritva Forge data repository (``schema: kritva-forge-data-manifest``,
``version: MANIFEST_VERSION``).  JSON is the canonical representation: every
other machine manifest of the repository is JSON, the standard library
serialises it byte-for-byte deterministically (``sort_keys``), and it parses
an order of magnitude faster than YAML for a ~1 MB inventory.

Sections (all lists sorted; keys sorted):

``sources``    every file under ``raw/`` - path, sha256, size, status, role,
               the module identities it feeds (``raw/rtl/original`` RTL and
               file lists used by canonical modules are ``canonical``; other
               raw files are ``excluded``: unreferenced RTL, documentation,
               ``raw/rtl/curated``)
``modules``    one record per canonical module - ``module_id`` (``mod1:``,
               KF-DQ-005), primary source and contributing sources, IR path /
               sha256 / ``ir_content`` (``n1:``) / ``module_body`` (``m1:``,
               KF-DQ-004), Semantic IR v2 document (path / sha256 /
               schema_version, KF-DQ-008), Behavioral Semantics v1 document
               (path / sha256 / versions / source Semantic IR, KF-DQ-009),
               Structural Analysis v1 document (path / sha256 / versions /
               ``str1:`` structural identity / source Semantic IR and
               Behavioral Semantics, KF-DQ-010), FSM Analysis v2 document
               (path / sha256 / versions / ``fsm1:`` document identity /
               source Semantic IR, Behavioral Semantics and Structural
               Analysis, KF-DQ-011), Prompt v2 text + sidecar (paths /
               sha256 / identity / versions / variant / the four documents it
               was derived from, KF-DQ-012), prompt, RTL copy, dataset
               records and split
``artifacts``  every file of the managed tree (``normalized``, ``generated``,
               ``analysis``, ``datasets``, ``splits``, ``manifests``,
               ``golden``) with kind, status and sha256
``records``    every dataset record - ``record_id`` (``r1:``), split, ip,
               module, ``module_id``, task, prompt variant; a record whose
               provenance declares a structural dependency also carries
               ``structural`` {path, sha256}, which must be the module's
               current structural artifact (KF-DQ-010 traceability); likewise
               ``fsm`` {path, sha256} for an FSM dependency (KF-DQ-011)
``splits``     split manifest reference, schema versions, counts and a split
               identity ``sp1:`` over the sorted (record_id, split) pairs
``manifests``  role of every manifest file (exactly one is ``authoritative``)

Status vocabulary: ``canonical`` (inputs and canonical IR), ``generated``
(current pipeline outputs), ``historical`` (KF-DQ-006 historical paths),
``deprecated`` (reserved; none at version 6), ``excluded`` (present, not a
dataset input: unreferenced raw files, curated RTL, ``.gitkeep``).

The manifest is derived from canonical inputs and the KF-DQ-006
classification, never from an existing manifest.  Generation is refused
while any artifact is STALE, ORPHAN or UNMANAGED, so stale data can never be
listed as current.  Identities reuse KF-DQ-003/004/005 definitions; nothing
in the manifest depends on time, host, user, PID, absolute paths, random
values or traversal order.  The manifest excludes its own hash.

``--check`` validates the stored manifest (schema, paths, identities,
hashes, references, classification, completeness, reproducibility) and is
the dataset publication gate.

Version history: 1 (KF-DQ-007); 2 (KF-DQ-008) adds ``modules[].semantic_ir``,
``versions.semantic_ir`` / ``semantic_identity`` / ``semantic_parser_version`` and the
``semantic_ir`` / ``semantic_ir_report`` artifact kinds; 3 (KF-DQ-009) adds
``modules[].behavior`` (path, sha256, schema / identity version, source
Semantic IR identity, module identity, status), ``versions.behavior`` /
``behavior_identity`` and the ``behavior`` / ``behavior_report`` artifact kinds;
4 (KF-DQ-010) adds ``modules[].structural`` (path, sha256, schema / identity /
analyzer / provenance version, ``structural_id``, module identity, status,
source Semantic IR and Behavioral Semantics), ``records[].structural`` (only
for records with a structural dependency), ``versions.structural`` /
``structural_identity`` / ``structural_analyzer`` and the ``structural`` /
``structural_report`` artifact kinds; 5 (KF-DQ-011) adds ``modules[].fsm``
(path, sha256, schema / identity / analyzer / provenance version, ``fsm_id``,
module identity, status, source Semantic IR, Behavioral Semantics and
Structural Analysis), ``records[].fsm`` (only for records with an FSM
dependency), ``versions.fsm`` / ``fsm_identity`` / ``fsm_analyzer`` and the
``fsm`` / ``fsm_report`` artifact kinds; 6 (KF-DQ-012) adds ``modules[].prompt_v2`` (path, sha256,
sidecar path / sha256, identity, schema / generator version, variant, ``derived_from`` Semantic IR,
Behavioral Semantics, Structural Analysis and FSM Analysis with sha256), the Prompt v2 / leakage /
classifier versions, ``compatibility`` (the forge version contract the data was produced against),
``leakage_classification`` (cross-split group counts and unresolved near-duplicates), Prompt v2 counts
and the ``prompt_v2`` / ``prompt_v2_sidecar`` / ``prompt_v2_report`` / ``prompt_leakage_classification``
artifact kinds; 7 (KF-DQ-013) adds ``multitask`` (multi-task dataset ``datasets/multitask/v2``: dataset
schema, task registry version, ``rtl-slice`` version, per-task version / status / record counts, split
counts, dataset identity ``ds2:`` over the sorted (record_id, split, line sha256), report path) and the
``multitask_split`` / ``multitask_registry`` / ``dataset_v2_report`` artifact kinds; the compatibility
contract requires ``dataset_schema`` / ``task_registry``.  An older manifest is rejected by ``--check`` and is regenerated by ``--write``
(the manifest is always derived, never migrated).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

MANIFEST_VERSION = 7
MANIFEST_NAME = "kritva-forge-data-manifest"
GENERATOR = "kritva-forge/scripts/core/data_manifest.py v7"
MANIFEST_PATH = "manifests/data_manifest.json"
REPORT_PATH = "analysis/reports/data_manifest_report.json"
STATUSES = ("canonical", "generated", "historical", "deprecated", "excluded")
RTL_SUFFIXES = (".v", ".sv", ".vh", ".svh")

_SHA = re.compile(r"[0-9a-f]{64}")
_ID = {
    "module_id": re.compile(r"mod1:[0-9a-f]{16}"),
    "record_id": re.compile(r"r1:[0-9a-f]{16}"),
    "module_body": re.compile(r"m1:[0-9a-f]{16}"),
    "ir_content": re.compile(r"n1:[0-9a-f]{16}"),
    "split_identity": re.compile(r"sp1:[0-9a-f]{16}"),
    "dataset_identity": re.compile(r"ds2:[0-9a-f]{16}"),
}

# Manifest files and their role in the canonical model.
MANIFEST_ROLES = {
    MANIFEST_PATH: ("authoritative", "canonical data manifest (this file)"),
    "manifests/provenance_manifest.json": ("component", "KF-DQ-005 provenance, incorporated by reference"),
    "manifests/artifact_inventory.json": ("component", "KF-DQ-006 artifact classification, incorporated by reference"),
    "splits/split_manifest.json": ("component", "KF-DQ-004 split assignment, incorporated by reference"),
    "datasets/pipeline/manifest.json": ("non-canonical", "incremental change tracking of the dataset generator"),
    "manifests/migration_manifest.json": ("historical", "KF-DQ-001 layout migration record"),
    "manifests/reorder_manifest.json": ("historical", "KF-DQ-001 layout migration record"),
    REPORT_PATH: ("report", "KF-DQ-007 manifest validation report"),
}

_CANONICAL_KINDS = {"canonical_ir", "semantic_ir", "behavior", "structural", "fsm", "ip_metadata", "data_manifest"}


class ManifestBlocked(RuntimeError):
    """Manifest generation refused: the data tree is not in a canonical state."""


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _digest(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts)
    return f"{prefix}:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _artifact_status(entry: dict) -> str:
    if entry["state"] == "HISTORICAL":
        return "historical"
    if entry["kind"] == "placeholder":
        return "excluded"
    if entry["kind"] in _CANONICAL_KINDS:
        return "canonical"
    return "generated"


def _raw_role(rel: str, referenced: dict) -> tuple[str, str]:
    if rel in referenced:
        return "canonical", ("filelist" if rel.endswith(".f") else "rtl")
    if rel.startswith("raw/rtl/curated/"):
        return "excluded", "curated_rtl"
    if rel.startswith("raw/rtl/original/") and rel.endswith(RTL_SUFFIXES):
        return "excluded", "unreferenced_rtl"
    return "excluded", "documentation_or_other"


# -----------------------------------------------------------------------------
# Generation
# -----------------------------------------------------------------------------

def build(data_root) -> dict:
    """Derive the canonical manifest from canonical inputs + KF-DQ-006 classification."""
    from scripts.core import stale_artifacts as S
    from scripts.core.provenance import PROVENANCE_VERSION, iter_dataset_records
    from scripts.dataset.leakage import LEAKAGE_SCHEMA_VERSION, SPLIT_SCHEMA_VERSION, SPLITS
    from scripts.core.identity import IDENTITY_VERSION
    from scripts.semantic_ir import model as SM
    from scripts.behavior import model as BM
    from scripts.structural import model as TM
    from scripts.fsm import model as FM
    from scripts.core import compat as C
    from scripts.prompt_v2 import classify as PC
    from scripts.prompt_v2 import leakage as PL
    from scripts.prompt_v2 import model as PM
    from scripts.multitask import registry as MG

    root = Path(os.path.abspath(data_root))
    result = S.classify(root)
    ctx = result.pop("context")
    blocking = [e for e in result["entries"]
                if e["state"] in S.FAILING_STATES and e["path"] not in S.SELF_OUTPUTS]
    if blocking or result["missing"]:
        problems = [f"{e['state']}: {e['path']} — {e['reason']}" for e in blocking]
        problems += [f"MISSING: {p}" for p in result["missing"]]
        raise ManifestBlocked("; ".join(problems[:10]) + (f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""))

    prov = ctx.provenance
    sem_sha = {e["path"]: e["sha256"] for e in result["entries"] if e["kind"] == "semantic_ir"}
    beh_sha = {e["path"]: e["sha256"] for e in result["entries"] if e["kind"] == "behavior"}
    str_sha = {e["path"]: e["sha256"] for e in result["entries"] if e["kind"] == "structural"}
    str_id = {p: json.loads((root / p).read_text(encoding="utf-8"))["id"] for p in sorted(str_sha)}
    fsm_sha = {e["path"]: e["sha256"] for e in result["entries"] if e["kind"] == "fsm"}
    fsm_doc_id = {p: json.loads((root / p).read_text(encoding="utf-8"))["id"] for p in sorted(fsm_sha)}
    pv2_sha = {e["path"]: e["sha256"] for e in result["entries"] if e["kind"] in ("prompt_v2", "prompt_v2_sidecar")}
    pv2_side = {p: json.loads((root / p).read_text(encoding="utf-8")) for p in sorted(pv2_sha) if p.endswith(".json")}
    sem_parsers = sorted({
        ((json.loads((root / p).read_text(encoding="utf-8")).get("generator") or {}).get("parser_version") or "unknown")
        for p in sorted(sem_sha)
    })

    # --- dataset records (the three split files) ---------------------------------
    records, by_module = [], defaultdict(list)
    for split, _, rec in iter_dataset_records(root):
        p = rec["provenance"]
        item = {
            "record_id": p["record_id"], "split": split, "ip": rec["ip"], "module": rec["module"],
            "module_id": p["module_id"], "task": rec.get("task"), "prompt_variant": rec.get("prompt_variant"),
        }
        if p.get("structural") is not None:                 # KF-DQ-010: records that consume structure
            item["structural"] = {"path": p["structural"].get("path"), "sha256": p["structural"].get("sha256")}
        if p.get("fsm") is not None:                        # KF-DQ-011: records that consume FSM analysis
            item["fsm"] = {"path": p["fsm"].get("path"), "sha256": p["fsm"].get("sha256")}
        records.append(item)
        by_module[(rec["ip"], rec["module"])].append((p["record_id"], split))
    records.sort(key=lambda r: r["record_id"])

    # --- modules -----------------------------------------------------------------
    referenced = defaultdict(set)
    modules = []
    for m in prov["modules"]:
        key = (m["ip"], m["module"])
        for src in m["contributing_sources"]:
            referenced[src["path"]].add(m["module_id"])
        recs = sorted(by_module.get(key, []))
        modules.append({
            "ip": m["ip"],
            "module": m["module"],
            "module_id": m["module_id"],
            "status": "canonical",
            "source": {"path": m["source"]["path"], "sha256": m["source"]["sha256"]},
            "contributing_sources": [s["path"] for s in m["contributing_sources"]],
            "external_includes": m["external_includes"],
            "ir": {
                "path": m["normalized_ir"]["path"],
                "sha256": m["normalized_ir"]["sha256"],
                "ir_content": m["normalized_ir"]["ir_content"],
                "module_body": m["module_body"],
            },
            "semantic_ir": {
                "path": f"{SM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                "sha256": sem_sha.get(f"{SM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                "schema_version": SM.SCHEMA_VERSION,
                "identity_version": SM.IDENTITY_VERSION,
                "status": "canonical",
                # provenance relationship: generated from this source revision,
                # coexisting with (not replacing) the v1 IR above
                "derived_from": {"source": m["source"]["path"], "source_sha256": m["source"]["sha256"]},
            },
            "behavior": {
                "path": f"{BM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                "sha256": beh_sha.get(f"{BM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                "schema_version": BM.SCHEMA_VERSION,
                "identity_version": BM.IDENTITY_VERSION,
                "module_id": m["module_id"],
                "status": "canonical",
                # provenance relationship: derived from exactly this Semantic IR v2 document
                "derived_from": {"semantic_ir": f"{SM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                                 "semantic_ir_sha256": sem_sha.get(f"{SM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json")},
            },
            "structural": {
                "path": f"{TM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                "sha256": str_sha.get(f"{TM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                "structural_id": str_id.get(f"{TM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                "schema_version": TM.SCHEMA_VERSION,
                "identity_version": TM.IDENTITY_VERSION,
                "analyzer_version": TM.ANALYZER_VERSION,
                "provenance_version": TM.PROVENANCE_VERSION,
                "module_id": m["module_id"],
                "status": "canonical",
                # provenance relationship: derived from exactly these two documents
                "derived_from": {
                    "semantic_ir": f"{SM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                    "semantic_ir_sha256": sem_sha.get(f"{SM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                    "behavior": f"{BM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                    "behavior_sha256": beh_sha.get(f"{BM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                },
            },
            "fsm": {
                "path": f"{FM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                "sha256": fsm_sha.get(f"{FM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                "fsm_id": fsm_doc_id.get(f"{FM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                "schema_version": FM.SCHEMA_VERSION,
                "identity_version": FM.IDENTITY_VERSION,
                "analyzer_version": FM.ANALYZER_VERSION,
                "provenance_version": FM.PROVENANCE_VERSION,
                "module_id": m["module_id"],
                "status": "canonical",
                # provenance relationship: derived from exactly these three documents
                "derived_from": {
                    "semantic_ir": f"{SM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                    "semantic_ir_sha256": sem_sha.get(f"{SM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                    "behavior": f"{BM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                    "behavior_sha256": beh_sha.get(f"{BM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                    "structural": f"{TM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json",
                    "structural_sha256": str_sha.get(f"{TM.OUTPUT_DIR}/{m['ip']}/{m['module']}.json"),
                },
            },
            "prompt_v2": _prompt_v2_entry(m, pv2_sha, pv2_side, sem_sha, beh_sha, str_sha, fsm_sha),
            "prompt": f"generated/prompts/{m['ip']}/{m['module']}.generate.txt",
            "rtl_copy": m["artifacts"][0]["path"] if m["artifacts"] else None,
            "records": [r for r, _ in recs],
            "split": sorted({s for _, s in recs})[0] if len({s for _, s in recs}) == 1 else sorted({s for _, s in recs}),
        })
    modules.sort(key=lambda m: (m["ip"], m["module"]))
    # files.f of each IP with canonical modules is a parser input
    for ip in ctx.ips:
        flist = f"raw/rtl/original/{ip}/files.f"
        if (root / flist).is_file():
            referenced[flist] |= {m["module_id"] for m in modules if m["ip"] == ip}

    # --- sources (all of raw/) ---------------------------------------------------
    sources = []
    for dirpath, dirnames, files in os.walk(root / "raw"):
        dirnames.sort()
        for name in sorted(files):
            path = Path(dirpath) / name
            if path.is_symlink():
                raise ManifestBlocked(f"symlink in raw/: {path.relative_to(root).as_posix()}")
            rel = path.relative_to(root).as_posix()
            status, role = _raw_role(rel, referenced)
            content = path.read_bytes()
            sources.append({
                "path": rel, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                "status": status, "role": role, "modules": sorted(referenced.get(rel, ())),
            })
    sources.sort(key=lambda s: s["path"])

    # --- artifacts (managed tree, from the KF-DQ-006 classification) -----------
    artifacts = []
    for e in result["entries"]:
        item = {"path": e["path"], "kind": e["kind"], "status": _artifact_status(e), "sha256": e["sha256"]}
        if e.get("module_id"):
            item["module_id"] = e["module_id"]
        if e["path"] in MANIFEST_ROLES:
            item["role"] = MANIFEST_ROLES[e["path"]][0]
        artifacts.append(item)
    artifacts.sort(key=lambda a: a["path"])

    # --- splits ----------------------------------------------------------------
    counts = Counter(r["split"] for r in records)
    split_pairs = "\n".join(f"{r['record_id']}\t{r['split']}" for r in records)
    splits = {
        "split_manifest": "splits/split_manifest.json",
        "split_schema_version": SPLIT_SCHEMA_VERSION,
        "leakage_schema_version": LEAKAGE_SCHEMA_VERSION,
        "counts": {s: counts.get(s, 0) for s in SPLITS},
        "split_identity": _digest("sp1", "kf-split", "v1", split_pairs),
    }

    # role table (presence is recorded by the artifact inventory, not here, so
    # the manifest never depends on the order in which gate outputs are written)
    manifests = [
        {"path": path, "role": role, "description": desc}
        for path, (role, desc) in sorted(MANIFEST_ROLES.items())
    ]

    status_counts = Counter(a["status"] for a in artifacts) + Counter(s["status"] for s in sources)
    return {
        "schema": {"name": MANIFEST_NAME, "version": MANIFEST_VERSION},
        "repository": {"name": "kritva-forge-data"},
        "generator": GENERATOR,
        "versions": {
            "manifest": MANIFEST_VERSION,
            "identity": IDENTITY_VERSION,
            "provenance": PROVENANCE_VERSION,
            "leakage_schema": LEAKAGE_SCHEMA_VERSION,
            "split_schema": SPLIT_SCHEMA_VERSION,
            "artifact_schema": S.ARTIFACT_SCHEMA_VERSION,
            "semantic_ir": SM.SCHEMA_VERSION,
            "semantic_identity": SM.IDENTITY_VERSION,
            "behavior": BM.SCHEMA_VERSION,
            "behavior_identity": BM.IDENTITY_VERSION,
            "structural": TM.SCHEMA_VERSION,
            "structural_identity": TM.IDENTITY_VERSION,
            "structural_analyzer": TM.ANALYZER_VERSION,
            "fsm": FM.SCHEMA_VERSION,
            "fsm_identity": FM.IDENTITY_VERSION,
            "fsm_analyzer": FM.ANALYZER_VERSION,
            "prompt": PM.SCHEMA_VERSION,
            "prompt_generator": PM.GENERATOR_VERSION,
            "prompt_identity": PM.IDENTITY_VERSION,
            "tokenizer": PL.TOKENIZER_VERSION,
            "leakage_metric": PL.METRIC_VERSION,
            "leakage_thresholds": PL.THRESHOLD_VERSION,
            "leakage_classifier": PC.CLASSIFIER_VERSION,
            "dataset_schema": MG.DATASET_SCHEMA["version"],                 # KF-DQ-013
            "task_registry": MG.TASK_REGISTRY_VERSION,
            "semantic_parser_version": ", ".join(sem_parsers) or None,
            "parser": prov["pipeline"].get("parser"),
            "parser_version": prov["pipeline"].get("parser_version"),
        },
        "ordering": "sources/artifacts by path; modules by (ip, module); records by record_id; keys sorted",
        "status_vocabulary": list(STATUSES),
        "counts": {
            "ips": len(ctx.ips),
            "modules": len(modules),
            "sources": len(sources),
            "canonical_sources": sum(1 for s in sources if s["status"] == "canonical"),
            "artifacts": len(artifacts),
            "dataset_records": len(records),
            "prompt_v2": sum(1 for p in pv2_sha if p.endswith(".txt")),
            "prompt_v2_sidecars": sum(1 for p in pv2_sha if p.endswith(".json")),
            "by_status": dict(sorted(status_counts.items())),
        },
        "compatibility": {"contract_version": C.CONTRACT_VERSION, "required": dict(C.REQUIRED),
                          "basis": "schema / identity / analyzer versions, not commit hashes"},
        "leakage_classification": _classification_summary(root),
        "multitask": _multitask_summary(root),
        "manifests": manifests,
        "sources": sources,
        "modules": modules,
        "artifacts": artifacts,
        "records": records,
        "splits": splits,
    }


def _prompt_v2_entry(m, pv2_sha, pv2_side, sem_sha, beh_sha, str_sha, fsm_sha) -> dict:
    from scripts.prompt_v2 import model as PM
    from scripts.prompt_v2.render import upstream_rel

    ip, mod = m["ip"], m["module"]
    prel, srel = PM.prompt_rel(ip, mod), PM.sidecar_rel(ip, mod)
    side = pv2_side.get(srel) or {}
    ups = {layer: upstream_rel(layer, ip, mod) for layer in PM.UPSTREAM}
    shas = {"semantic_ir": sem_sha, "behavior": beh_sha, "structural": str_sha, "fsm": fsm_sha}
    return {
        "path": prel,
        "sha256": pv2_sha.get(prel),
        "sidecar_path": srel,
        "sidecar_sha256": pv2_sha.get(srel),
        "identity": side.get("identity"),
        "schema_version": PM.SCHEMA_VERSION,
        "generator_version": PM.GENERATOR_VERSION,
        "variant": PM.DEFAULT_VARIANT,
        "module_id": m["module_id"],
        "status": "generated",
        "derived_from": {
            {"semantic_ir": "semantic", "behavior": "behavior", "structural": "structural", "fsm": "fsm"}[layer]:
                {"path": ups[layer], "sha256": shas[layer].get(ups[layer])}
            for layer in PM.UPSTREAM
        },
    }


def _multitask_summary(root: Path) -> dict:
    """KF-DQ-013 multi-task dataset summary (counts and identity derived from the dataset files)."""
    from scripts.multitask import registry as MG
    from scripts.multitask import validator as MV

    lines, counts, splits = {}, Counter(), {}
    readable = True
    for name in MG.SPLITS:
        f = root / f"{MG.OUTPUT_DIR}/{name}.jsonl"
        try:
            lines[name] = f.read_bytes().decode("utf-8").splitlines(keepends=True) if f.is_file() else []
        except UnicodeDecodeError:  # KF-DQ-013.1 AC-018: never raise; the identity becomes null
            lines[name], readable = [], False
        splits[name] = len(lines[name])
        for line in lines[name]:
            try:
                rec = json.loads(line)
            except ValueError:
                counts[None] += 1
                continue
            counts[None if MV.record_type_problems(rec) else rec["task"]["id"]] += 1
    present = readable and all((root / f"{MG.OUTPUT_DIR}/{n}.jsonl").is_file() for n in MG.SPLITS)
    return {
        "path": MG.OUTPUT_DIR,
        "registry": MG.REGISTRY_PATH,
        "report": MG.REPORT_PATH,
        "dataset_schema": MG.DATASET_SCHEMA,
        "task_registry": MG.TASK_REGISTRY_VERSION,
        "rtl_slice": MG.RTL_SLICE_VERSION,
        "tasks": {t["id"]: {"version": t["version"], "status": t["status"], "projection": t["projection"],
                            "records": counts.get(t["id"], 0)} for t in MG.TASKS},
        "splits": splits,
        "records": sum(splits.values()),
        "dataset_identity": MV.dataset_identity(lines) if present else None,
        "split_source": "splits/split_manifest.json (module split inherited, split schema 2)",
    }


def _classification_summary(root: Path) -> dict:
    from scripts.prompt_v2 import model as PM

    path = root / PM.CLASSIFICATION_REPORT_PATH
    rep = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {
        "report": PM.CLASSIFICATION_REPORT_PATH,
        "classifier_version": rep.get("classifier_version"),
        "counts": rep.get("counts"),
        "unresolved_near_duplicates": rep.get("unresolved_near_duplicates"),
        "kf_dq_013_entry": rep.get("kf_dq_013_entry"),
        "splits_changed": rep.get("splits_changed"),
    }


def write(data_root) -> Path:
    root = Path(os.path.abspath(data_root))
    manifest = build(root)
    path = root / MANIFEST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(manifest), encoding="utf-8")
    return path


# -----------------------------------------------------------------------------
# Validation / publication gate
# -----------------------------------------------------------------------------

def _path_problem(rel) -> str | None:
    if not isinstance(rel, str) or not rel:
        return "empty path"
    if "\\" in rel:
        return "non-POSIX separator"
    if rel.startswith("/") or os.path.isabs(rel) or re.match(r"^[A-Za-z]:", rel):
        return "absolute path"
    if any(part in ("..", ".") for part in PurePosixPath(rel).parts) or "//" in rel:
        return "path traversal"
    return None


def check(data_root, manifest: dict | None = None) -> dict:
    """Validate the stored (or given) manifest against the data tree."""
    from scripts.core import stale_artifacts as S
    from scripts.core.paths import find_absolute_paths
    from scripts.core.provenance import PROVENANCE_VERSION, module_id, sha256_file
    from scripts.dataset.leakage import LEAKAGE_SCHEMA_VERSION, SPLIT_SCHEMA_VERSION
    from scripts.semantic_ir import model as SM
    from scripts.behavior import model as BM
    from scripts.structural import model as TM
    from scripts.fsm import model as FM
    from scripts.core import compat as C
    from scripts.prompt_v2 import model as PM
    from scripts.prompt_v2.render import upstream_rel

    root = Path(os.path.abspath(data_root))
    counters = Counter()
    problems = []

    def bad(kind, msg):
        counters[kind] += 1
        problems.append(msg)

    stored_path = root / MANIFEST_PATH
    if manifest is None:
        if not stored_path.is_file():
            bad("missing_manifest", f"canonical manifest missing: {MANIFEST_PATH}")
            manifest = {}
        else:
            try:
                manifest = json.loads(stored_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                bad("schema", f"manifest is not valid JSON: {exc}")
                manifest = {}

    # -- schema ----------------------------------------------------------------
    if manifest:
        if manifest.get("schema") != {"name": MANIFEST_NAME, "version": MANIFEST_VERSION}:
            bad("schema", f"unsupported manifest schema {manifest.get('schema')!r}")
        v = manifest.get("versions") or {}
        supported = {"provenance": PROVENANCE_VERSION, "leakage_schema": LEAKAGE_SCHEMA_VERSION,
                     "split_schema": SPLIT_SCHEMA_VERSION, "artifact_schema": S.ARTIFACT_SCHEMA_VERSION,
                     "semantic_ir": SM.SCHEMA_VERSION, "semantic_identity": SM.IDENTITY_VERSION,
                     "behavior": BM.SCHEMA_VERSION, "behavior_identity": BM.IDENTITY_VERSION,
                     "structural": TM.SCHEMA_VERSION, "structural_identity": TM.IDENTITY_VERSION,
                     "structural_analyzer": TM.ANALYZER_VERSION,
                     "fsm": FM.SCHEMA_VERSION, "fsm_identity": FM.IDENTITY_VERSION,
                     "fsm_analyzer": FM.ANALYZER_VERSION}
        supported.update({k: v for k, v in C.REQUIRED.items() if k != "manifest"})       # KF-DQ-012
        for key, want in supported.items():
            if v.get(key) != want:
                bad("schema", f"unsupported {key} version {v.get(key)!r} (expected {want})")
        comp = manifest.get("compatibility") or {}
        if comp.get("required") != C.REQUIRED or comp.get("contract_version") != C.CONTRACT_VERSION:
            bad("schema", "compatibility contract recorded in the manifest differs from the forge contract (KF-DQ-012)")
        lc = manifest.get("leakage_classification") or {}
        if lc.get("classifier_version") != C.REQUIRED["leakage_classifier"]:
            bad("schema", f"unsupported leakage classifier {lc.get('classifier_version')!r}")
        for e in manifest.get("artifacts", []) + manifest.get("sources", []):
            if e.get("status") not in STATUSES:
                bad("classification", f"{e.get('path')}: invalid status {e.get('status')!r}")

    text = dumps(manifest)
    for line_no, line in find_absolute_paths(text):
        bad("absolute_paths", f"absolute path in manifest line {line_no}: {line.strip()[:100]}")
    if str(root) in text:
        bad("absolute_paths", "manifest contains the data-root location")

    sources = manifest.get("sources", [])
    artifacts = manifest.get("artifacts", [])
    modules = manifest.get("modules", [])
    records = manifest.get("records", [])

    # -- paths -----------------------------------------------------------------
    seen_paths = Counter(e.get("path") for e in sources + artifacts)
    for path, n in sorted(seen_paths.items(), key=lambda kv: str(kv[0])):
        if n > 1:
            bad("duplicate_paths", f"duplicate canonical path {path}")
    for e in sources + artifacts:
        problem = _path_problem(e.get("path"))
        if problem:
            bad("absolute_paths" if problem == "absolute path" else "path_traversal" if problem == "path traversal"
                else "invalid_paths", f"{e.get('path')!r}: {problem}")

    # -- identities --------------------------------------------------------------
    for field, entries in (("module_id", modules), ("record_id", records)):
        ids = Counter(e.get(field) for e in entries)
        for value, n in ids.items():
            if n > 1:
                bad("duplicate_identities", f"duplicate {field} {value}")
    for m in modules:
        key = f"{m.get('ip')}/{m.get('module')}"
        mid = m.get("module_id")
        if not isinstance(mid, str) or not _ID["module_id"].fullmatch(mid) or mid != module_id(m.get("ip"), m.get("module")):
            bad("invalid_identities", f"{key}: invalid module identity {mid!r}")
        ir = m.get("ir") or {}
        for field in ("ir_content", "module_body"):
            if not isinstance(ir.get(field), str) or not _ID[field].fullmatch(ir[field]):
                bad("invalid_identities", f"{key}: invalid {field} {ir.get(field)!r}")
    for r in records:
        if not isinstance(r.get("record_id"), str) or not _ID["record_id"].fullmatch(r["record_id"]):
            bad("invalid_identities", f"invalid record_id {r.get('record_id')!r}")
    sid = (manifest.get("splits") or {}).get("split_identity")
    if manifest and (not isinstance(sid, str) or not _ID["split_identity"].fullmatch(sid)):
        bad("invalid_identities", f"invalid split_identity {sid!r}")

    # -- content -----------------------------------------------------------------
    for e in sources + artifacts:
        path, sha = e.get("path"), e.get("sha256")
        if _path_problem(path):
            continue
        if sha is None and path in S.SELF_OUTPUTS:
            continue                     # gate outputs (incl. this manifest) carry no hash
        target = root / path
        if not target.is_file() or target.is_symlink():
            bad("missing_artifacts", f"listed artifact missing: {path}")
        elif not isinstance(sha, str) or not _SHA.fullmatch(sha):
            bad("invalid_hashes", f"{path}: invalid sha256 {sha!r}")
        elif sha256_file(target) != sha:
            bad("invalid_hashes", f"{path}: sha256 mismatch")

    # -- references ----------------------------------------------------------------
    src_by_path = {s.get("path"): s for s in sources}
    art_by_path = {a.get("path"): a for a in artifacts}
    rec_by_id = {r.get("record_id"): r for r in records}
    mod_by_id = {m.get("module_id"): m for m in modules}
    for m in modules:
        key = f"{m.get('ip')}/{m.get('module')}"
        src = (m.get("source") or {}).get("path")
        if src not in src_by_path or src_by_path[src].get("status") != "canonical":
            bad("broken_references", f"{key}: source {src!r} is not a canonical source")
        elif (m.get("source") or {}).get("sha256") != src_by_path[src].get("sha256"):
            bad("broken_references", f"{key}: source sha256 disagrees with the source inventory")
        for c in m.get("contributing_sources", []):
            if c not in src_by_path:
                bad("broken_references", f"{key}: contributing source {c!r} not in the source inventory")
        for ref in ((m.get("ir") or {}).get("path"), m.get("prompt"), m.get("rtl_copy")):
            if ref is not None and ref not in art_by_path:
                bad("broken_references", f"{key}: artifact {ref!r} not in the artifact inventory")
        sem = m.get("semantic_ir") or {}
        sem_art = art_by_path.get(sem.get("path"))
        if sem_art is None or sem_art.get("kind") != "semantic_ir":
            bad("broken_references", f"{key}: Semantic IR {sem.get('path')!r} not in the artifact inventory")
        elif sem_art.get("sha256") != sem.get("sha256"):
            bad("broken_references", f"{key}: Semantic IR sha256 disagrees with the artifact inventory")
        if (sem.get("derived_from") or {}).get("source_sha256") != (m.get("source") or {}).get("sha256") \
                or (sem.get("derived_from") or {}).get("source") != (m.get("source") or {}).get("path"):
            bad("broken_references", f"{key}: Semantic IR provenance disagrees with the module source")
        if sem.get("identity_version") != SM.IDENTITY_VERSION:
            bad("schema", f"{key}: unsupported Semantic IR identity version {sem.get('identity_version')!r}")
        if sem.get("schema_version") != SM.SCHEMA_VERSION:
            bad("schema", f"{key}: unsupported Semantic IR schema version {sem.get('schema_version')!r}")
        beh = m.get("behavior") or {}
        beh_art = art_by_path.get(beh.get("path"))
        if beh_art is None or beh_art.get("kind") != "behavior":
            bad("broken_references", f"{key}: Behavioral Semantics {beh.get('path')!r} not in the artifact inventory")
        elif beh_art.get("sha256") != beh.get("sha256"):
            bad("broken_references", f"{key}: Behavioral Semantics sha256 disagrees with the artifact inventory")
        if beh.get("module_id") != m.get("module_id"):
            bad("broken_references", f"{key}: Behavioral Semantics module identity disagrees with the module")
        bsrc = beh.get("derived_from") or {}
        if bsrc.get("semantic_ir") != sem.get("path") or bsrc.get("semantic_ir_sha256") != sem.get("sha256"):
            bad("broken_references", f"{key}: Behavioral Semantics is not derived from the listed Semantic IR")
        if beh.get("schema_version") != BM.SCHEMA_VERSION or beh.get("identity_version") != BM.IDENTITY_VERSION:
            bad("schema", f"{key}: unsupported Behavioral Semantics schema/identity version")
        st = m.get("structural") or {}
        st_art = art_by_path.get(st.get("path"))
        if st_art is None or st_art.get("kind") != "structural":
            bad("broken_references", f"{key}: Structural Analysis {st.get('path')!r} not in the artifact inventory")
        elif st_art.get("sha256") != st.get("sha256"):
            bad("broken_references", f"{key}: Structural Analysis sha256 disagrees with the artifact inventory")
        else:
            try:
                sdoc = json.loads((root / st["path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                sdoc = {}
            if sdoc.get("id") != st.get("structural_id"):
                bad("broken_references", f"{key}: structural identity disagrees with the structural document")
        if st.get("module_id") != m.get("module_id"):
            bad("broken_references", f"{key}: Structural Analysis module identity disagrees with the module")
        tsrc = st.get("derived_from") or {}
        if tsrc.get("semantic_ir") != sem.get("path") or tsrc.get("semantic_ir_sha256") != sem.get("sha256") \
                or tsrc.get("behavior") != beh.get("path") or tsrc.get("behavior_sha256") != beh.get("sha256"):
            bad("broken_references", f"{key}: Structural Analysis is not derived from the listed Semantic IR / Behavioral Semantics")
        if (st.get("schema_version"), st.get("identity_version"), st.get("analyzer_version"), st.get("provenance_version")) \
                != (TM.SCHEMA_VERSION, TM.IDENTITY_VERSION, TM.ANALYZER_VERSION, TM.PROVENANCE_VERSION):
            bad("schema", f"{key}: unsupported Structural Analysis schema/identity/analyzer/provenance version")
        if not isinstance(st.get("structural_id"), str) or not TM.ID_RE.fullmatch(st["structural_id"]):
            bad("invalid_identities", f"{key}: invalid structural identity {st.get('structural_id')!r}")
        fs = m.get("fsm") or {}
        fs_art = art_by_path.get(fs.get("path"))
        if fs_art is None or fs_art.get("kind") != "fsm":
            bad("broken_references", f"{key}: FSM Analysis {fs.get('path')!r} not in the artifact inventory")
        elif fs_art.get("sha256") != fs.get("sha256"):
            bad("broken_references", f"{key}: FSM Analysis sha256 disagrees with the artifact inventory")
        else:
            try:
                fdoc = json.loads((root / fs["path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                fdoc = {}
            if fdoc.get("id") != fs.get("fsm_id"):
                bad("broken_references", f"{key}: FSM document identity disagrees with the FSM document")
        if fs.get("module_id") != m.get("module_id"):
            bad("broken_references", f"{key}: FSM Analysis module identity disagrees with the module")
        fsrc = fs.get("derived_from") or {}
        if fsrc.get("semantic_ir") != sem.get("path") or fsrc.get("semantic_ir_sha256") != sem.get("sha256") \
                or fsrc.get("behavior") != beh.get("path") or fsrc.get("behavior_sha256") != beh.get("sha256") \
                or fsrc.get("structural") != st.get("path") or fsrc.get("structural_sha256") != st.get("sha256"):
            bad("broken_references", f"{key}: FSM Analysis is not derived from the listed Semantic IR / "
                                     "Behavioral Semantics / Structural Analysis")
        if (fs.get("schema_version"), fs.get("identity_version"), fs.get("analyzer_version"), fs.get("provenance_version")) \
                != (FM.SCHEMA_VERSION, FM.IDENTITY_VERSION, FM.ANALYZER_VERSION, FM.PROVENANCE_VERSION):
            bad("schema", f"{key}: unsupported FSM Analysis schema/identity/analyzer/provenance version")
        if not isinstance(fs.get("fsm_id"), str) or not FM.ID_RE.fullmatch(fs["fsm_id"]):
            bad("invalid_identities", f"{key}: invalid FSM document identity {fs.get('fsm_id')!r}")
        pv = m.get("prompt_v2") or {}                                   # KF-DQ-012
        for pkey, skey, kind in (("path", "sha256", "prompt_v2"), ("sidecar_path", "sidecar_sha256", "prompt_v2_sidecar")):
            want_path = (PM.prompt_rel if kind == "prompt_v2" else PM.sidecar_rel)(m.get("ip"), m.get("module"))
            art = art_by_path.get(pv.get(pkey))
            if pv.get(pkey) != want_path:
                bad("broken_references", f"{key}: Prompt v2 {pkey} {pv.get(pkey)!r} is not {want_path}")
            elif art is None or art.get("kind") != kind:
                bad("broken_references", f"{key}: Prompt v2 {pkey} {pv.get(pkey)!r} not in the artifact inventory")
            elif art.get("sha256") != pv.get(skey):
                bad("broken_references", f"{key}: Prompt v2 {skey} disagrees with the artifact inventory")
        if (pv.get("schema_version"), pv.get("generator_version")) != (PM.SCHEMA_VERSION, PM.GENERATOR_VERSION):
            bad("schema", f"{key}: unsupported Prompt v2 schema / generator version")
        if pv.get("variant") not in PM.VARIANTS:
            bad("schema", f"{key}: unsupported Prompt v2 variant {pv.get('variant')!r}")
        if not isinstance(pv.get("identity"), str) or not PM.ID_RE.fullmatch(pv["identity"]):
            bad("invalid_identities", f"{key}: invalid Prompt v2 identity {pv.get('identity')!r}")
        elif pv.get("sidecar_path") and (root / pv["sidecar_path"]).is_file():
            try:
                side = json.loads((root / pv["sidecar_path"]).read_text(encoding="utf-8"))
            except ValueError:
                side = {}
            if side.get("identity") != pv.get("identity"):
                bad("broken_references", f"{key}: Prompt v2 identity disagrees with its sidecar")
        layers = {"semantic": (sem, "semantic_ir"), "behavior": (beh, "behavior"), "structural": (st, "structural"),
                  "fsm": (fs, "fsm")}
        for name, (entry, layer) in layers.items():
            ref = (pv.get("derived_from") or {}).get(name) or {}
            if ref.get("path") != entry.get("path") or ref.get("path") != upstream_rel(layer, m.get("ip"), m.get("module")) \
                    or ref.get("sha256") != entry.get("sha256"):
                bad("broken_references", f"{key}: Prompt v2 is not derived from the listed {name} document")
        ir_art = art_by_path.get((m.get("ir") or {}).get("path"))
        if ir_art and ir_art.get("sha256") != (m.get("ir") or {}).get("sha256"):
            bad("broken_references", f"{key}: IR sha256 disagrees with the artifact inventory")
        for rid in m.get("records", []):
            if rid not in rec_by_id:
                bad("broken_references", f"{key}: dataset record {rid} not in the record inventory")
    for r in records:
        m = mod_by_id.get(r.get("module_id"))
        if m is None or (m.get("ip"), m.get("module")) != (r.get("ip"), r.get("module")):
            bad("broken_references", f"record {r.get('record_id')}: module {r.get('module_id')} does not resolve")
        elif "structural" in r and r["structural"] != {"path": (m.get("structural") or {}).get("path"),
                                                        "sha256": (m.get("structural") or {}).get("sha256")}:
            bad("broken_references", f"record {r.get('record_id')}: structural dependency is not the module's "
                                     "current Structural Analysis artifact")
        if m is not None and "fsm" in r and r["fsm"] != {"path": (m.get("fsm") or {}).get("path"),
                                                         "sha256": (m.get("fsm") or {}).get("sha256")}:
            bad("broken_references", f"record {r.get('record_id')}: FSM dependency is not the module's "
                                     "current FSM Analysis artifact")

    # -- multi-task dataset (KF-DQ-013) ----------------------------------------------
    if manifest:
        from scripts.multitask import registry as MG
        mt = manifest.get("multitask") or {}
        if mt.get("dataset_schema") != MG.DATASET_SCHEMA or mt.get("task_registry") != MG.TASK_REGISTRY_VERSION \
                or mt.get("rtl_slice") != MG.RTL_SLICE_VERSION:
            bad("schema", "multitask: unsupported dataset schema / task registry / rtl-slice version")
        if not isinstance(mt.get("dataset_identity"), str) or not _ID["dataset_identity"].fullmatch(mt["dataset_identity"]):
            bad("invalid_identities", f"multitask: invalid dataset identity {mt.get('dataset_identity')!r}")
        n_modules = len(modules)
        for t in MG.TASKS:
            got = ((mt.get("tasks") or {}).get(t["id"]) or {})
            want = n_modules if t["status"] == MG.POPULATED else 0
            if (got.get("version"), got.get("status"), got.get("records")) != (t["version"], t["status"], want):
                bad("missing_expected" if got.get("records") != want else "schema",
                    f"multitask: task {t['id']} {got!r} (registry v{t['version']} {t['status']}, {want} records)")
        for rel in [f"{MG.OUTPUT_DIR}/{n}.jsonl" for n in MG.SPLITS] + [MG.REGISTRY_PATH]:
            a = art_by_path.get(rel)
            if a is None or a.get("status") != "generated":
                bad("broken_references", f"multitask: {rel} not a current generated artifact")

    # -- classification ------------------------------------------------------------
    for a in artifacts:
        hist = S.is_historical(a.get("path", ""))
        if hist and a.get("status") != "historical":
            bad("historical_misclassified", f"{a['path']}: historical artifact marked {a.get('status')!r}")
        if not hist and a.get("status") == "historical":
            bad("historical_misclassified", f"{a['path']}: active artifact marked historical")
    for s in sources:
        if s.get("status") == "canonical" and not str(s.get("path", "")).startswith("raw/rtl/original/"):
            bad("classification", f"{s.get('path')}: only raw/rtl/original can be a canonical source")

    # -- completeness / reproducibility (recompute from canonical inputs) ---------
    determinism = "not checked"
    try:
        rebuilt = build(root)
    except ManifestBlocked as exc:
        bad("stale_references", f"canonical inventory invalid (KF-DQ-006): {exc}")
        rebuilt = None
    if rebuilt is not None and manifest:
        for section, key in (("sources", "path"), ("artifacts", "path"), ("modules", "module_id"),
                             ("records", "record_id")):
            want = {e[key] for e in rebuilt[section]}
            have = {e.get(key) for e in manifest.get(section, [])}
            for k in sorted(want - have, key=str):
                bad("missing_expected", f"{section}: expected {k} missing from the manifest")
            for k in sorted(have - want, key=str):
                bad("unexpected", f"{section}: unexpected entry {k}")
        determinism = "reproducible" if dumps(rebuilt) == dumps(manifest) else "differs"
        if determinism == "differs" and not problems:
            bad("nondeterministic", "stored manifest differs from the manifest recomputed from canonical inputs")

    report = {
        "manifest_version": MANIFEST_VERSION,
        "records_checked": len(sources) + len(artifacts) + len(modules) + len(records),
        "sources": len(sources),
        "artifacts": len(artifacts),
        "modules": len(modules),
        "dataset_records": len(records),
        "prompt_v2": sum(1 for m in modules if (m.get("prompt_v2") or {}).get("sha256")),
        "prompt_v2_sidecars": sum(1 for m in modules if (m.get("prompt_v2") or {}).get("sidecar_sha256")),
        "compatibility_status": "compatible" if not any("compatibility" in x or "unsupported" in x for x in problems)
        else "incompatible",
        "leakage_classification": (manifest.get("leakage_classification") or {}).get("counts"),
        "multitask_records": (manifest.get("multitask") or {}).get("records"),
        "multitask_identity": (manifest.get("multitask") or {}).get("dataset_identity"),
        "missing_manifest": counters["missing_manifest"],
        "missing_expected": counters["missing_expected"],
        "unexpected": counters["unexpected"],
        "missing_artifacts": counters["missing_artifacts"],
        "duplicate_paths": counters["duplicate_paths"],
        "duplicate_identities": counters["duplicate_identities"],
        "invalid_hashes": counters["invalid_hashes"],
        "invalid_identities": counters["invalid_identities"],
        "invalid_paths": counters["invalid_paths"],
        "absolute_paths": counters["absolute_paths"],
        "path_traversal": counters["path_traversal"],
        "broken_references": counters["broken_references"],
        "stale_references": counters["stale_references"],
        "historical_misclassified": counters["historical_misclassified"],
        "classification_errors": counters["classification"],
        "schema_errors": counters["schema"],
        "determinism_status": determinism,
        "relocation_status": "location-independent" if not counters["absolute_paths"] else "location-dependent",
        "problems": problems[:200],
    }
    report["status"] = "FAIL" if problems else "PASS"
    return report


def format_report(report: dict) -> str:
    keys = ["records_checked", "sources", "artifacts", "modules", "dataset_records", "prompt_v2", "prompt_v2_sidecars",
            "compatibility_status", "multitask_records", "multitask_identity", "missing_expected",
            "unexpected", "missing_artifacts", "duplicate_paths", "duplicate_identities", "invalid_hashes",
            "invalid_identities", "absolute_paths", "path_traversal", "broken_references", "stale_references",
            "historical_misclassified", "classification_errors", "schema_errors", "determinism_status",
            "relocation_status"]
    lines = [f"Data manifest check: {report['status']}"]
    lines += [f"  {k.replace('_', ' '):24s}: {report[k]}" for k in keys]
    lines += [f"  [FAIL] {p}" for p in report["problems"][:30]]
    if len(report["problems"]) > 30:
        lines.append(f"  ... {len(report['problems']) - 30} more")
    return "\n".join(lines)


def write_report(data_root, report: dict) -> Path:
    path = Path(os.path.abspath(data_root)) / REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(report), encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="KF-DQ-007 canonical data manifest")
    parser.add_argument("--data-root", default=os.environ.get("KRITVA_FORGE_DATA_ROOT"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate the stored manifest (default; read-only)")
    mode.add_argument("--write", action="store_true", help="regenerate the manifest, then validate")
    parser.add_argument("--json", help="also write the validation report JSON here")
    args = parser.parse_args(argv)
    if not args.data_root:
        from scripts.core.paths import default_data_root
        args.data_root = str(default_data_root())
    if args.write:
        try:
            write(args.data_root)
        except ManifestBlocked as exc:
            print(f"[STOP] manifest generation refused (KF-DQ-006 gate): {exc}")
            return 1
        print(f"[INFO] wrote {MANIFEST_PATH}")
    report = check(args.data_root)
    print(format_report(report))
    if args.json:
        Path(args.json).write_text(dumps(report), encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    raise SystemExit(main())
