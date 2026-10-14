# =============================================================================
# Copyright (c) 2026 KritvaOS
# SPDX-License-Identifier: Apache-2.0
#
# File        : test_multitask.py
# Description : KF-DQ-013 multi-task dataset - registry, rtl-slice, projections, records, split, gates
#
# Component   : Kritva Forge
# Module      : tests/multitask
# Layer       : Test
#
# Author      : Kritva Forge Team
# Created     : 02-10-2026
# =============================================================================
"""Multi-task dataset (KF-DQ-013 AC-074 .. AC-084).

A tiny data repository is processed by the production pipeline once per test
module: IP ``ipa`` holds a 3-state FSM (``ctrl``) and a parameterised wrapper
instantiating it (``wrap``); three more IPs hold plain modules.  Golden
projection targets of ``ctrl`` / ``wrap`` are byte-compared.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.core import compat as C
from scripts.core import data_manifest as DM
from scripts.core import stale_artifacts as S
from scripts.core.paths import ForgeDataPaths
from scripts.multitask import build as B
from scripts.multitask import projections as PJ
from scripts.multitask import registry as G
from scripts.multitask import rtl_slice as RS
from scripts.multitask import validator as V
from scripts.prompt_v2 import validator as PV

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = Path(__file__).resolve().parent / "golden"

# -----------------------------------------------------------------------------
# Registry (AC-006 .. AC-012, AC-074)
# -----------------------------------------------------------------------------

REGISTRY_DIGEST = "7604c06c53e4583f5682c14b2ce37959ed2f700035cecf415cf899d7c88061ba"


def test_registry_task_table():
    table = [(t["id"], t["version"], t["status"], t["input_kind"], t["target_kind"], t["projection"]) for t in G.TASKS]
    assert table == [
        ("rtl_generation", 2, "populated", "prompt_v2", "rtl", None),
        ("rtl_understanding", 1, "populated", "rtl", "prompt_v2", None),
        ("interface_extraction", 1, "populated", "rtl", "json", "interface-v1"),
        ("structural_extraction", 1, "populated", "rtl", "json", "structural-v1"),
        ("dependency_analysis", 1, "populated", "rtl", "json", "dependency-v1"),
        ("fsm_extraction", 1, "populated", "rtl", "json", "fsm-v1"),
        ("rtl_explanation", 1, "declared", None, None, None),
        ("assertion_generation", 1, "declared", None, None, None),
        ("rtl_repair", 1, "declared", None, None, None),
        ("rtl_optimization", 1, "declared", None, None, None),
    ]
    assert G.TASK_REGISTRY_VERSION == 1 and G.DATASET_SCHEMA == {"name": "kritva-forge-dataset", "version": 2}
    assert all(set(t) == {"id", "version", "status", "input_kind", "target_kind", "source_layers", "projection",
                          "description", "population_rule"} for t in G.TASKS)
    assert "prompt_v2" not in json.dumps(G.BY_ID["rtl_explanation"])           # AC-010: not an alias
    assert set(PJ.PROJECTORS) == set(G.PROJECTIONS)


def test_registry_digest_pinned():
    assert G.digest() == REGISTRY_DIGEST, "task registry changed: bump the task / registry version (AC-012)"


def test_record_id_preimage():
    import hashlib
    pre = "kf-record\x1fv2\x1ffsm_extraction\x1f1\x1fmod1:0123456789abcdef"
    assert G.record_id("fsm_extraction", 1, "mod1:0123456789abcdef") == "r2:" + hashlib.sha256(pre.encode()).hexdigest()[:16]
    assert G.record_id("rtl_generation", 2, "mod1:0") != G.record_id("rtl_generation", 1, "mod1:0")


def test_compatibility_contract_keys():
    assert C.REQUIRED["manifest"] == 7 and C.REQUIRED["dataset_schema"] == 2 and C.REQUIRED["task_registry"] == 1
    assert C.CONTRACT_VERSION == 1
    assert not set(C.MANIFEST_ONLY_KEYS) & set(C.SIDECAR_KEYS)
    assert C.declared() == C.REQUIRED and DM.MANIFEST_VERSION == 7


# -----------------------------------------------------------------------------
# rtl-slice-v1 (AC-020 .. AC-022, AC-075)
# -----------------------------------------------------------------------------

SRC = """// header comment
`timescale 1ns/1ps
module a (input x, output y);
  assign y = x; // endmodule in a comment
  initial $display("module b endmodule");
endmodule
/* module b (); endmodule */
macromodule b (input p, output q);\r\n  assign q = ~p;\r\nendmodule
module a_ext (input z); endmodule
"""


def test_rtl_slice_single_and_multi_module_files():
    assert RS.module_slice(SRC, "a").startswith("module a (input x")
    assert RS.module_slice(SRC, "a").endswith("endmodule") and "// endmodule in a comment" in RS.module_slice(SRC, "a")
    assert '$display("module b endmodule")' in RS.module_slice(SRC, "a")       # strings kept, masked for matching
    assert RS.module_slice(SRC, "b") == "macromodule b (input p, output q);\n  assign q = ~p;\nendmodule"
    assert RS.module_slice(SRC, "a_ext") == "module a_ext (input z); endmodule"
    assert "timescale" not in RS.module_slice(SRC, "a") and "a_ext" not in RS.module_slice(SRC, "a")


def test_rtl_slice_zero_or_two_matches_fail():
    with pytest.raises(RS.SliceError, match="0 declarations"):
        RS.module_slice(SRC, "missing")
    with pytest.raises(RS.SliceError, match="2 declarations"):
        RS.module_slice(SRC + "\nmodule a (); endmodule\n", "a")


# -----------------------------------------------------------------------------
# Fixture corpus
# -----------------------------------------------------------------------------

FSM_RTL = """// controller with a 3-state FSM
module ctrl (input clk, input rst_n, input go, output reg busy, output wire done);
  localparam IDLE = 2'd0, RUN = 2'd1, STOP = 2'd2;
  reg [1:0] state;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) state <= IDLE;
    else case (state)
      IDLE: if (go) state <= RUN;
      RUN: state <= STOP;
      STOP: state <= IDLE;
      default: state <= IDLE;
    endcase
  always @(posedge clk or negedge rst_n)
    if (!rst_n) busy <= 1'b0;
    else busy <= (state == RUN);
  assign done = (state == STOP);
endmodule

/* note: module fake (); endmodule is only a comment */
module wrap #(parameter W = 8) (input clk, input rst_n, input go, input [W-1:0] din, output busy, output done,
                                output reg [W-1:0] q);
  ctrl u_ctrl (.clk(clk), .rst_n(rst_n), .go(go), .busy(busy), .done(done));
  always @(posedge clk) if (go) q <= din;
endmodule
"""
BODIES = {
    "ipb": "input clk, input en, output reg [7:0] q); always @(posedge clk) if (en) q <= q + 8'd1;",
    "ipc": "input [1:0] s, input [7:0] d0, input [7:0] d1, output [7:0] y); assign y = s[0] ? d1 : (s[1] ? ~d0 : 8'hff);",
    "ipd": "input clk, input rst, input din, output reg [15:0] sr); always @(posedge clk) if (rst) sr <= 16'h0; "
           "else sr <= {sr[14:0], din};",
}


def _make_data_root(base):
    root = base / "kritva-forge-data"
    o = root / "raw" / "rtl" / "original"
    (o / "ipa").mkdir(parents=True)
    (o / "ipa" / "ctrl.sv").write_text(FSM_RTL)
    (o / "ipa" / "files.f").write_text("ctrl.sv\n")
    for name, n in (("ipb", 4), ("ipc", 3), ("ipd", 2)):
        (o / name).mkdir()
        (o / name / f"{name}.sv").write_text("".join(f"module {name}_m{i} ({BODIES[name]} endmodule\n" for i in range(n)))
        (o / name / "files.f").write_text(f"{name}.sv\n")
    return root


def _run(root):
    from scripts.pipeline.run_pipeline import run_pipeline

    data = ForgeDataPaths.from_root(root)
    run_pipeline(str(data.raw_rtl / "original"), str(data.normalized_ir), data_root=str(root),
                 prompt_root=str(data.prompts), reports_root=str(data.reports),
                 datasets_root=str(data.pipeline_datasets))
    return data


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    return _run(_make_data_root(tmp_path_factory.mktemp("multitask")))


@pytest.fixture
def scratch(corpus, tmp_path):
    dst = tmp_path / "copy" / "kritva-forge-data"
    shutil.copytree(corpus.root, dst)
    return dst


def _records(root):
    out = {}
    for s in G.SPLITS:
        out[s] = [json.loads(x) for x in (Path(root) / G.OUTPUT_DIR / f"{s}.jsonl").read_text().splitlines()]
    return out


def _rewrite(root, split, mutate):
    path = Path(root) / G.OUTPUT_DIR / f"{split}.jsonl"
    recs = [json.loads(x) for x in path.read_text().splitlines()]
    mutate(recs)
    path.write_text("".join(G.dumps_record(r) for r in recs))


def _record(root, task, module):
    for s, recs in _records(root).items():
        for r in recs:
            if r["task"]["id"] == task and r["module"]["name"] == module:
                return s, r
    raise KeyError((task, module))


# -----------------------------------------------------------------------------
# Pipeline, records and projections (AC-013 .. AC-033, AC-076 .. AC-078, AC-082)
# -----------------------------------------------------------------------------

def test_pipeline_builds_valid_dataset(corpus):
    rep = json.loads((corpus.root / G.REPORT_PATH).read_text())
    assert rep["status"] == "PASS", rep["problems"]
    n = len(list((corpus.root / "normalized/semantic_ir/v2").rglob("*.json")))
    assert rep["counts"]["records"] == 6 * n
    assert all(rep["tasks"][t["id"]]["records"] == (n if t["status"] == "populated" else 0) for t in G.TASKS)
    assert rep["fsm_extraction"] == {"positive": 1, "negative": n - 1, "fsms": 1}
    assert rep["authorization"]["kf_dq_013_entry"] == "open"
    assert V.check(corpus.root)["status"] == "PASS"
    reg = json.loads((corpus.root / G.REGISTRY_PATH).read_text())
    assert [t["records"] for t in reg["tasks"]] == [n] * 6 + [0] * 4


@pytest.mark.parametrize("proj,module", [(p, m) for p in ("interface", "structural", "dependency", "fsm")
                                         for m in ("ctrl", "wrap")])
def test_projection_golden(corpus, proj, module):
    task = G.PROJECTIONS[f"{proj}-v1"]["task"]
    _, rec = _record(corpus.root, task, module)
    assert rec["target"]["projection"] == f"{proj}-v1"
    got = json.dumps(rec["target"]["value"], indent=2, sort_keys=True) + "\n"
    assert got == (GOLDEN / f"{proj}_{module}.json").read_text()


def test_record_schema_identity_and_canonical_form(corpus):
    for split, recs in _records(corpus.root).items():
        lines = (corpus.root / G.OUTPUT_DIR / f"{split}.jsonl").read_text().splitlines(keepends=True)
        keys = [(r["task"]["id"], r["module"]["ip"], r["module"]["name"]) for r in recs]
        assert keys == sorted(keys)
        for r, line in zip(recs, lines):
            assert G.dumps_record(r) == line
            assert sorted(r) == sorted(G.RECORD_KEYS) and r["split"] == split
            assert r["record_id"] == G.record_id(r["task"]["id"], r["task"]["version"], r["module"]["module_id"])
            assert r["module"]["module_id"].startswith("mod1:")
            meta = json.dumps({k: v for k, v in r.items() if k not in ("input", "target")})
            assert not re.search(r"(sem1|beh1|str1|fsm1|n1):[0-9a-f]{16}", meta + json.dumps(r["target"].get("value")))
            assert r["versions"]["dataset_schema"] == 2 and r["versions"]["task_registry"] == 1
            assert r["versions"]["rtl_slice"] == "rtl-slice-v1"


def test_text_tasks_use_prompt_v2_and_rtl_slice(corpus):
    from scripts.prompt_v2 import model as P
    _, gen = _record(corpus.root, "rtl_generation", "ctrl")
    _, und = _record(corpus.root, "rtl_understanding", "ctrl")
    prompt = (corpus.root / P.prompt_rel("ipa", "ctrl")).read_text()
    assert gen["input"] == {"kind": "prompt_v2", "text": prompt} and und["target"] == {"kind": "prompt_v2", "text": prompt}
    assert gen["target"]["text"] == und["input"]["text"] == RS.module_slice(FSM_RTL, "ctrl")
    assert "module wrap" not in gen["target"]["text"] and "module fake" not in gen["target"]["text"]
    assert {s["layer"] for s in gen["sources"]} == {"prompt_v2", "prompt_v2_sidecar", "source"}
    _, iface = _record(corpus.root, "interface_extraction", "ctrl")
    assert {s["layer"] for s in iface["sources"]} == {"semantic_ir", "source"}       # JSON tasks: no Prompt v2


def test_fsm_conditions_equal_prompt_v2_rendering(corpus):
    from scripts.prompt_v2 import model as P
    _, rec = _record(corpus.root, "fsm_extraction", "ctrl")
    prompt = (corpus.root / P.prompt_rel("ipa", "ctrl")).read_text()
    for t in rec["target"]["value"]["fsms"][0]["transitions"]:
        if t["condition"]:
            assert f"{t['from']} -> {t['to']} when {t['condition']}" in prompt


def test_split_inheritance(corpus):
    sm = json.loads((corpus.root / "splits/split_manifest.json").read_text())
    want = {(e["ip"], e["module"]): s for s in G.SPLITS for e in sm[s]}
    for split, recs in _records(corpus.root).items():
        for r in recs:
            assert want[(r["module"]["ip"], r["module"]["name"])] == split


def test_deterministic_and_relocatable(corpus, tmp_path):
    b = _run(_make_data_root(tmp_path / "elsewhere" / "deep"))
    for rel in [f"{G.OUTPUT_DIR}/{s}.jsonl" for s in G.SPLITS] + [G.REGISTRY_PATH, G.REPORT_PATH]:
        assert (corpus.root / rel).read_bytes() == (b.root / rel).read_bytes(), rel
    B.write(b.root)                                                         # idempotent rebuild
    for rel in [f"{G.OUTPUT_DIR}/{s}.jsonl" for s in G.SPLITS]:
        assert (corpus.root / rel).read_bytes() == (b.root / rel).read_bytes(), rel


def test_hash_seed_independent(corpus):
    code = ("import sys; from scripts.multitask import build as B; r=B.build(sys.argv[1], authorize=False); "
            "f=B.serialize(r); print(''.join(f[k] for k in sorted(f)))")
    outs = []
    for seed in ("0", "777"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(ROOT))
        outs.append(subprocess.run([sys.executable, "-c", code, str(corpus.root)], cwd=ROOT, env=env,
                                   capture_output=True, text=True, check=True).stdout)
    assert outs[0] == outs[1]


# -----------------------------------------------------------------------------
# Gate negatives (AC-040 .. AC-043, AC-079, AC-080)
# -----------------------------------------------------------------------------

def _fails(root, needle):
    rep = V.check(root, authorize=False)
    assert rep["status"] == "FAIL" and any(needle in p for p in rep["problems"]), rep["problems"][:5]


def test_gate_module_split_mismatch(scratch):
    split, rec = _record(scratch, "fsm_extraction", "ctrl")
    other = next(s for s in G.SPLITS if s != split)

    def move(recs):
        for r in recs:
            if r["record_id"] == rec["record_id"]:
                r["split"] = other
    _rewrite(scratch, split, move)
    _fails(scratch, "declares split")


def test_gate_module_spanning_splits(scratch):
    split, rec = _record(scratch, "fsm_extraction", "ctrl")
    other = next(s for s in G.SPLITS if s != split)
    _rewrite(scratch, split, lambda recs: recs.remove(next(r for r in recs if r["record_id"] == rec["record_id"])))
    moved = dict(rec, split=other)
    _rewrite(scratch, other, lambda recs: (recs.append(moved), recs.sort(key=lambda r: (r["task"]["id"], r["module"]["ip"], r["module"]["name"]))))
    _fails(scratch, "has records in splits")


def test_gate_source_sha_mismatch(scratch):
    split, rec = _record(scratch, "interface_extraction", "ctrl")

    def bad(recs):
        for r in recs:
            if r["record_id"] == rec["record_id"]:
                r["sources"][0]["sha256"] = "0" * 64
    _rewrite(scratch, split, bad)
    _fails(scratch, "stale or missing source")


def test_gate_projection_and_task_errors(scratch):
    split, rec = _record(scratch, "dependency_analysis", "ctrl")

    def bad(recs):
        for r in recs:
            if r["record_id"] == rec["record_id"]:
                r["target"]["projection"] = "dependency-v0"
    _rewrite(scratch, split, bad)
    _fails(scratch, "target projection")


def test_gate_unknown_and_declared_tasks(scratch):
    split, rec = _record(scratch, "fsm_extraction", "wrap")

    def add(recs):
        x = json.loads(json.dumps(rec))
        x["task"] = {"id": "rtl_repair", "version": 1}
        x["record_id"] = G.record_id("rtl_repair", 1, x["module"]["module_id"])
        y = json.loads(json.dumps(rec))
        y["task"] = {"id": "made_up", "version": 1}
        recs.extend([x, y])
        recs.sort(key=lambda r: (r["task"]["id"], r["module"]["ip"], r["module"]["name"]))
    _rewrite(scratch, split, add)
    rep = V.check(scratch, authorize=False)
    assert any("declared task rtl_repair" in p for p in rep["problems"])
    assert any("unknown task" in p for p in rep["problems"])


def test_gate_id_leak_and_extra_field(scratch):
    split, rec = _record(scratch, "structural_extraction", "ctrl")

    def bad(recs):
        for r in recs:
            if r["record_id"] == rec["record_id"]:
                r["module"]["loc"] = "sem1:0123456789abcdef"
    _rewrite(scratch, split, bad)
    _fails(scratch, "module fields")
    _fails(scratch, "leaks an internal id")


def test_gate_rederivation_detects_edit(scratch):
    split, rec = _record(scratch, "fsm_extraction", "ctrl")

    def bad(recs):
        for r in recs:
            if r["record_id"] == rec["record_id"]:
                r["target"]["value"]["fsms"][0]["states"][0]["name"] = "BOOT"
    _rewrite(scratch, split, bad)
    _fails(scratch, "re-derivation")


def test_gate_unmanaged_file(scratch):
    (Path(scratch) / G.OUTPUT_DIR / "extra.jsonl").write_text("")
    _fails(scratch, "unmanaged files under datasets/multitask")


# -----------------------------------------------------------------------------
# Fail closed (AC-067 .. AC-073, AC-077)
# -----------------------------------------------------------------------------

def test_missing_fsm_document_fails_not_negative(scratch):
    from scripts.prompt_v2.render import upstream_rel
    (Path(scratch) / upstream_rel("fsm", "ipa", "wrap")).unlink()
    with pytest.raises(B.BuildError, match="missing fsm"):
        B.build(scratch, authorize=False)


def test_split_schema_1_refused(scratch):
    p = Path(scratch) / "splits/split_manifest.json"
    sm = json.loads(p.read_text())
    sm["split_schema_version"] = 1
    p.write_text(json.dumps(sm))
    with pytest.raises(B.BuildError, match="split schema 2 required"):
        B.build(scratch, authorize=False)


def test_entry_authorization_blocked_refused(scratch, monkeypatch):
    from scripts.prompt_v2 import classify as PC
    real = PC.build

    def blocked(root):
        rep = real(root)
        rep["kf_dq_013_entry"] = "blocked until every near_duplicate group is re-split"
        rep["counts"]["near_duplicate"] = 1
        return rep
    monkeypatch.setattr(PC, "build", blocked)
    with pytest.raises(B.BuildError, match="entry authorization not granted"):
        B.build(scratch)
    probs = PV.consumption(scratch)
    assert any("entry authorization does not hold" in m for _, m in probs)


def test_slice_failure_and_failed_build_writes_nothing(scratch):
    before = {s: (Path(scratch) / G.OUTPUT_DIR / f"{s}.jsonl").read_bytes() for s in G.SPLITS}
    src = Path(scratch) / "raw/rtl/original/ipb/ipb.sv"
    src.write_text(src.read_text() + "module ipb_m0 (); endmodule\n")
    with pytest.raises(B.BuildError):
        B.write(scratch)
    assert {s: (Path(scratch) / G.OUTPUT_DIR / f"{s}.jsonl").read_bytes() for s in G.SPLITS} == before
    assert not list((Path(scratch) / G.OUTPUT_DIR).glob("*.tmp"))


# -----------------------------------------------------------------------------
# Consumption boundary (AC-046 / AC-047, AC-081)
# -----------------------------------------------------------------------------

def test_consumption_allowed_for_multitask_text_tasks(corpus):
    assert PV.consumption(corpus.root, "open") == []


def test_consumption_rejects_other_tasks_and_legacy(scratch):
    split, rec = _record(scratch, "interface_extraction", "ctrl")

    def bad(recs):
        for r in recs:
            if r["record_id"] == rec["record_id"]:
                r["sources"].append({"layer": "prompt_v2", "path": "generated/prompt/v2/ipa/ctrl.behavior_aware.txt",
                                     "sha256": "0" * 64})
    _rewrite(scratch, split, bad)
    assert any("may not consume Prompt v2" in m for _, m in PV.consumption(scratch, "open"))
    p = Path(scratch) / "datasets/pipeline/train.jsonl"
    lines = p.read_text().splitlines()
    first = json.loads(lines[0])
    first["provenance"]["prompt_v2"] = "generated/prompt/v2/x.txt"
    p.write_text("\n".join([json.dumps(first)] + lines[1:]) + "\n")
    assert any("legacy record" in m for _, m in PV.consumption(scratch, "open"))


# -----------------------------------------------------------------------------
# Stale gate / manifest v7 (AC-050 .. AC-052, AC-083)
# -----------------------------------------------------------------------------

def test_stale_gate_classifies_multitask(corpus):
    result = S.classify(corpus.root)
    kinds = {e["path"]: (e["kind"], e["state"]) for e in result["entries"]}
    for s in G.SPLITS:
        assert kinds[f"{G.OUTPUT_DIR}/{s}.jsonl"] == ("multitask_split", "CURRENT")
    assert kinds[G.REGISTRY_PATH] == ("multitask_registry", "CURRENT")
    assert kinds[G.REPORT_PATH] == ("dataset_v2_report", "CURRENT")


def test_stale_gate_flags_stale_and_unmanaged(scratch):
    src = Path(scratch) / "raw/rtl/original/ipa/ctrl.sv"
    src.write_text(src.read_text() + "\n")
    (Path(scratch) / G.OUTPUT_DIR / "notes.txt").write_text("x")
    kinds = {e["path"]: (e["kind"], e["state"]) for e in S.classify(scratch)["entries"]}
    assert kinds[f"{G.OUTPUT_DIR}/notes.txt"][1] == "UNMANAGED"
    assert any(kinds[f"{G.OUTPUT_DIR}/{s}.jsonl"][1] == "STALE" for s in G.SPLITS)


def test_manifest_v7_multitask_section(corpus):
    m = json.loads((corpus.root / DM.MANIFEST_PATH).read_text())
    assert m["schema"]["version"] == 7 and m["compatibility"]["required"] == C.REQUIRED
    mt = m["multitask"]
    n = len(m["modules"])
    assert mt["dataset_schema"] == G.DATASET_SCHEMA and mt["task_registry"] == 1 and mt["rtl_slice"] == "rtl-slice-v1"
    assert mt["records"] == 6 * n and sum(mt["splits"].values()) == 6 * n
    assert re.fullmatch(r"ds2:[0-9a-f]{16}", mt["dataset_identity"])
    assert mt["dataset_identity"] == json.loads((corpus.root / G.REPORT_PATH).read_text())["dataset_identity"]
    assert m["versions"]["dataset_schema"] == 2 and m["versions"]["task_registry"] == 1
    assert DM.check(corpus.root)["status"] == "PASS"
