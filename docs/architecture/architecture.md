# Kritva Forge Architecture

## 1. Overview

Kritva Forge is a hardware design intelligence framework designed to transform RTL source code into structured design knowledge.

The architecture separates:

1. RTL parsing
2. RTL Intermediate Representation
3. Semantic analysis
4. Structural analysis
5. FSM analysis
6. Graph/dependency analysis
7. Dataset generation
8. RTL AI/LLM systems

The central architectural principle is:

> **Parse once, normalize once, analyze many ways.**

The parser produces a reusable RTL IR that becomes the common input to downstream analysis components.

---

# 2. High-Level Architecture

```text
                         RTL Source
                    Verilog / SystemVerilog
                              │
                              ▼
                    ┌──────────────────┐
                    │    RTL Parser    │
                    │                  │
                    │ PySlang / Slang  │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │     RTL IR       │
                    │                  │
                    │ Modules          │
                    │ Ports            │
                    │ Signals          │
                    │ Assignments      │
                    │ Processes        │
                    │ Cases            │
                    │ Expressions      │
                    └────────┬─────────┘
                             │
            ┌────────────────┼────────────────┐
            │                │                │
            ▼                ▼                ▼
     ┌────────────┐   ┌────────────┐   ┌────────────┐
     │ Semantic   │   │Structural  │   │  Process   │
     │ Analysis   │   │ Analysis   │   │  Analysis  │
     └──────┬─────┘   └──────┬─────┘   └──────┬─────┘
            │                │                │
            └────────────────┼────────────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │   FSM Engine     │
                    │                  │
                    │ Candidate        │
                    │ State            │
                    │ Transition       │
                    │ Action           │
                    │ Encoding         │
                    └────────┬─────────┘
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
             ┌────────────┐    ┌────────────┐
             │   Graph    │    │  Quality   │
             │  Analysis  │    │  Analysis  │
             └─────┬──────┘    └──────┬─────┘
                   │                  │
                   └────────┬─────────┘
                            ▼
                   ┌──────────────────┐
                   │ Design Knowledge │
                   └────────┬─────────┘
                            │
                 ┌──────────┴──────────┐
                 ▼                     ▼
          Dataset Generation      RTL AI / LLM
```

---

# 3. Repository Architecture

```text
kritva-forge/
│
├── docs/
│   └── architecture/
│
├── scripts/
│   ├── parser/
│   ├── rtl_ir/
│   ├── semantic/
│   ├── structural/
│   ├── fsm/
│   ├── analysis/
│   ├── dataset/
│   ├── evaluation/
│   └── debug/
│
├── tests/
│   ├── parser/
│   ├── rtl_ir/
│   ├── semantic/
│   ├── structural/
│   └── fsm/
│
├── examples/
│   └── rtl/
│
├── schemas/
│   └── rtl_ir/
│
├── configs/
│
└── data/
    └── kritva-forge-data/    # Git submodule
```

---

# 4. Parser Layer

## Responsibility

The parser converts Verilog/SystemVerilog source into a normalized representation suitable for downstream analysis.

The parser should be responsible for **syntax and source structure**, not higher-level interpretation.

### Primary responsibilities

* Module extraction
* Port extraction
* Parameter extraction
* Signal extraction
* Instance extraction
* Always/process extraction
* Assignment extraction
* Case statement extraction
* Conditional statement extraction
* Expression representation
* Source metadata
* Hierarchy information

### Parser interface

Conceptually:

```text
RTL source
    │
    ▼
Parser
    │
    ▼
Normalized RTL IR
```

The parser should not directly determine whether a signal is an FSM state register.

That belongs to structural/FSM analysis.

---

# 5. RTL Intermediate Representation

The RTL IR is the central data model of Kritva Forge.

It provides a normalized representation independent of downstream consumers.

A simplified module representation is:

```text
Module
├── name
├── parameters
├── ports
├── signals
├── instances
├── processes
├── assignments
├── case_statements
└── metadata
```

An assignment can contain information such as:

```text
Assignment
├── raw
├── lhs
├── rhs
├── operator
├── lhs_signal
├── is_blocking
├── is_nonblocking
├── is_array
└── is_hierarchical
```

A case statement contains:

```text
Case
├── expression
├── case_type
└── items
    ├── labels
    └── statements
```

The IR should remain serializable.

Raw parser AST objects should not be stored in persistent YAML/JSON representations.

## 5.1 Provenance and Stable Node Identity

Every persisted IR node carries portable provenance (KF-DQ-002) and a
deterministic content identity (KF-DQ-003):

```yaml
identity_version: 1                       # module-level identity model version
...
- node_id: n1:4fc1e05d2cb15eab            # stable content identity
  syntax_type: ParameterDeclarationStatementSyntax
  source_file: raw/rtl/original/uart/uart_tx.v   # top-level RTL file (repo-relative)
  offset: 342
  buffer: raw/rtl/original/uart/uart_tx.v        # file that physically holds the token
```

`node_id` is `n<version>:` plus the first 16 hex digits of SHA-256 over:

```text
"kf-node" | "v1" | source_file | syntax_kind | start location key | end location key
```

A location key is `file|offset`; for macro-expanded tokens it is
`expanded_file|expanded_offset|macro|spelling_file|spelling_offset|macro_offset`.
All files are repository-relative.

The identity never uses `id()`, Python `hash()`, UUIDs, timestamps, process,
host or user information, absolute paths, or pyslang `BufferID` allocation
numbers. It is therefore identical across repeated runs, relocated data roots
and parse order. Two nodes share an identity only if they are the same syntax
kind with exactly the same start and end token positions in the same source
file. The implementation and full specification live in
`scripts/core/identity.py`; any change to the payload must bump
`IDENTITY_VERSION`.

## 5.2 Semantic IR v2 (KF-DQ-008)

**Purpose.** The v1 module YAML (`normalized/ir/<ip>/modules/<module>.yaml`)
describes structure: ports, signals, parameters and instances. Its
behavioural fields (`always_blocks`, `assigns`, `num_*`) are always empty
because of a key mismatch: the parser stores `continuous_assigns` /
`processes`, while `yaml_generator` reads `assigns` / `always_blocks`.
Semantic IR v2 is a separate, versioned, source-level semantic
representation. It covers declarations, types, expressions, assignments,
conditions, cases, instances and symbol references. Downstream tasks
consume it through this documented contract instead of parser-specific
fields.

**Output location and versions.** There is exactly one canonical location:
`normalized/semantic_ir/v2/<ip>/<module>.json`, one document per canonical
module. The v1 IR is not touched. Both representations are independently
valid and independently tested. Consumers name the version they read
(`normalized/ir` v1 or `normalized/semantic_ir/v2`).

Every document carries:

- `schema: {name: kritva-forge-semantic-ir, version: 2}`
- `versions`:
  - `schema`: 2
  - `identity`: 1, the `sem1:` namespace
  - `node_identity`: the KF-DQ-003 `n1` version
  - `parser`, for example `pyslang 12.0.0`
  - `compatibility.normalized_ir`: 1

Serialisation is canonical and byte-deterministic: sorted keys, compact
separators, UTF-8 and a trailing newline. The corpus is about 32 MB; use
`python -m json.tool` to read a document. The contract is in
`scripts/semantic_ir/model.py`. The generator is `extractor.py`, called from
`rtl_parser_slang` and `yaml_generator.write_semantic_ir`. The validator is
`validator.py`.

**Schema** (document sections):

| Section | Content (criteria §6) |
|---|---|
| `module` | name, `id` (`sem1:`), `module_id` (`mod1:`, KF-DQ-005), `ip`, `is_top`, `source {path, sha256}`, `loc` |
| `parameters` | parameter / localparam / genvar: type, signedness, packed dimensions, `width` (null when untyped), default expression, constant `value`, `port_param`, `loc` |
| `ports` | in declaration order: direction (input/output/inout/ref/unknown), net/variable `kind`, type, `signed`, `packed` / `unpacked` dimensions, `width`, `default`, ANSI or non-ANSI style, `loc` |
| `signals` | nets and variables (implicit nets marked): `kind` and `net_type` distinguish wire/net from logic/reg/integer variables; same type fields as ports |
| `typedefs` | enum (members with values; anonymous enums have `name: null`), struct/union, alias |
| `subroutines` | functions and tasks: ports (the return value is a `return`-direction port), body |
| `assignments` | target and value expressions, `kind` (continuous / blocking / nonblocking / compound / declaration), operator, referenced symbols (`writes`, `reads`), `guards`, owner (`process` / `subroutine` / `generate`), scope, `loc` |
| `processes` | containers for procedural code: `kind` (keyword), `sensitivity` as written (`list` / `implicit` / `none`), `events` (edge + expression), statement `body`, assignment ids |
| `conditions` | one record per `if`: owner, unique/priority qualifier, `predicate_references`, `then` / `else` statement ids, `loc` |
| `cases` | one record per case: `case_kind` (case/casez/casex), qualifier, `selector_references`, `items` (label count, `label_references`, body), `default`, `loc` |
| `instances` | instance name, referenced `module`, `resolved` and `target_module_id` (null when unresolved), parameter overrides, connections (named/ordered/implicit/wildcard/empty, port, expression, direction), `loc` |
| `generates` | region / loop / if / case / block, `representation: source-level` (not elaborated) |
| `references` | one record per symbol **use** (see below) |
| `external` | compilation-unit declarations from `include`d headers |
| `unsupported` | every construct that is not modelled |
| `coverage`, `counts`, `extraction`, `generator` | accounting, and `complete` or `partial` |

**Expressions** are trees that preserve operand order:

- `ref`: `id`, name, `ref_kind`, and `target` (the defining entity, or null)
- `literal`: text, value, base, width, `has_xz`
- `unary`, `binary`, `ternary`
- `concat`, `replicate`
- `index`, `part_select` (`:`, `+:`, `-:`)
- `member`
- `call`: system or user function, with arguments
- `cast`
- `opaque`

Constant parameter expressions, including `$clog2`, and widths are evaluated
where determinable; otherwise they are null.

Statement trees inside processes and subroutines are made of:

- `assign`
- `if`
- `case`
- `block`
- `loop`
- `timing`
- `null`
- `return`
- `call`
- `declaration`
- `opaque`

**Declarations and references.** Declarations are the entities in
`parameters`, `ports`, `signals`, `typedefs` (and their enum members),
`subroutines`, genvars and block-local declarations. Each use of a symbol
is a `ref` expression node with its own identity. `references` indexes
every use with these fields:

| Field | Meaning |
|---|---|
| `symbol` | the name used |
| `ref_kind` | port, signal, parameter, genvar, local, enum_member, subroutine, type, external, hierarchical or unresolved |
| `target` | the defining entity |
| `source` | the referencing entity: an assignment, statement, process, instance, declaration or generate |
| `usage` | `read`, `write` (the written base of an assignment target) or `connect` (an instance port expression) |
| `loc` | the source span |

Names that cannot be resolved stay explicit as `unresolved` with a null
target. Nothing is guessed.

**Assignments and guards.** `guards` lists the enclosing control constructs,
outermost first. Each entry is `{statement, branch}`, where `branch` is
`then`, `else`, `item` (with the `item` index), `default` or `body`. It
records where an assignment sits without claiming execution semantics.

**Identity.** Every semantic entity has
`id = "sem1:" + sha256("kf-sem", "v1", category, n1, qualifier)[:16]`,
where:

- `n1` is the KF-DQ-003 node identity (repository-relative file, syntax kind, token positions);
- `category` is the entity kind;
- `qualifier` disambiguates several entities derived from one syntax node.

When one type is shared by several declarators, copies of its range
expressions get reference identities derived from (reference identity,
declarator). Identities are stable across runs, parse order and checkout
location, and change only when the RTL changes. Modules keep `mod1:`;
source identity is the content sha256.

**Provenance.** The module records `source {path, sha256}`. Every entity,
statement, reference and unsupported record carries `loc {file, line,
column, offset}`. The path is repository-relative and names the physical
buffer, so an `include`d header gives the header's path. For
macro-expanded tokens, `loc` is the fully original location. No
machine-local path is persisted.

**Unsupported constructs.** Unsupported constructs are explicit, never
dropped. Each one becomes an `opaque` statement, expression or member node
and is listed in `unsupported` with construct, level, reason, text and
`loc`. When anything is opaque, `extraction` is `partial`. Examples are
`wait`, `case … inside` and `force`.

**Validation (fail closed).** `scripts/semantic_ir/validator.py` (also
`make check-semantic`) runs two levels of checks.

Per-document checks:

- schema and identity versions
- required fields and enumerations
- missing, malformed or duplicate identities
- duplicate declarations
- provenance presence and portability (absolute paths, `..`)
- port directions
- widths (positive, and consistent with constant packed ranges)
- expression, statement, assignment, condition and case structure
- references: unresolvable targets, resolvable kinds without a definition, and index ↔ tree agreement
- instance resolution
- canonical ordering
- silent loss of unsupported constructs

Corpus checks:

- one document per canonical module
- duplicate module identities
- duplicate canonical output paths
- canonical bytes
- current source sha256

The corpus report is written to `analysis/reports/semantic_ir_report.json`
and includes `corpus_sha256`.

**Gates and data manifest.** The pipeline parses the RTL and writes the v1 IR
and Semantic IR v2. The gates then run in this order:

1. pre-dataset stale gate
2. Semantic IR v2 gate (no override)
3. datasets
4. provenance
5. post-run stale gate
6. data manifest gate

The stale gate classifies `normalized/semantic_ir/v2` documents. A document
is CURRENT only when its schema, `mod1:`, source path/sha256 and validation
are all current. A document for a non-canonical module is ORPHAN; any other
file there is UNMANAGED. The report's `corpus_sha256` is checked after the
run.

The data manifest (version 2) gives each module
`semantic_ir {path, sha256, schema_version, identity_version, status,
derived_from {source, source_sha256}}`, plus `versions.semantic_ir`,
`semantic_identity` and `semantic_parser_version`. Invalid Semantic IR is
never published. A version-1 manifest is rejected and regenerated.

**Compatibility and boundaries.**

- v1 IR, prompts, datasets, splits, provenance and all identities are
  unchanged. Semantic IR v2 is additive; nothing is overwritten.
- The v1 behavioural fields remain empty. They are superseded by
  `assignments`, `processes`, `conditions` and `cases`.
- Not in KF-DQ-008:
  - process roles, scheduling and complete behavioural semantics (KF-DQ-009)
  - driver, dependency and connectivity graphs (KF-DQ-010)
  - FSM extraction (KF-DQ-011)
  - elaboration of generates
  - timing / PPA and assertions

  KF-DQ-008 records the representation they build on: containers, guards,
  references and instances.

## 5.3 Behavioral Semantics v1 (KF-DQ-009)

**Purpose.** Semantic IR v2 (§5.2) records *what is written*: processes,
events, assignments, conditions, cases and references. Behavioral Semantics
v1 interprets those structures as hardware behaviour: process roles, clocks,
resets, registers, enables, holds, priority, combinational completeness,
latches and state candidates. It supplies the documented behavioural
contract that structural analysis (KF-DQ-010), FSM integration (KF-DQ-011)
and behaviour-aware datasets (KF-DQ-012/013) consume.

**Dependency and location.** The flow is RTL → Semantic IR v2 → Behavioral
Semantics v1.

- `scripts/behavior/analyzer.py` reads only the persisted Semantic IR v2 JSON.
  It does not parse RTL, does not use parser objects, and does not copy
  Semantic IR expression trees; records refer to `sem1:` identities instead.
- Semantic IR documents are never modified.
- Output is one document per canonical module at
  `normalized/behavior/v1/<ip>/<module>.json`, with
  `schema: {name: kritva-forge-behavioral-semantics, version: 1}`.
- Each document's `versions` block gives the behavioral schema (1), the
  behavioral identity (1, `beh1:`), the consumed Semantic IR schema (2) and
  the Semantic IR identity (1).
- `module` carries the `mod1:` module identity, the Semantic IR module
  identity, the RTL source path and sha256, and the path and sha256 of the
  analysed Semantic IR document.
- Serialisation is canonical: sorted keys, compact separators, trailing
  newline.

**Sections.**

| Section | Content |
|---|---|
| `processes` | one record per Semantic IR process: `role`, `confidence`, evidence, read/write summary (`reads`, `writes`, `conditional_reads`, `conditional_writes`, `registered_targets`, `combinational_targets`, `latch_targets`), clocks, resets, registers, enables, holds, and the process's conditions, cases and assignments |
| `clocks` | signal, edge, event index, status (confirmed / candidate / ambiguous), evidence, location |
| `resets` | signal, kind (async / sync), polarity (active_high / active_low), status, the `if` condition and branch, priority rank, per-target reset assignments, evidence |
| `registers` | register candidates of sequential processes: clock, resets, assignment kinds, ordered assignments with guards and `overridden_by`, `update`, `hold`, priority leaves, enables, holds, next values |
| `next_values` | each non-reset, non-hold update: assignment, guards, value kind (constant / expression), value references |
| `enables` | a condition whose one branch updates the register while the other branch only holds it: update/hold branch, explicit or implicit hold |
| `holds` | explicit holds (`q <= q`, including a default hold before a conditional update) and implicit holds (the missing branch) with their guard path |
| `combinational` | each target of a combinational or latch process: completeness (complete / conditional / incomplete / ambiguous), missing branches, latch status, ordered assignments |
| `latches` | targets whose latch status is explicit (`always_latch`), inferred (incomplete), possible (case without default) or ambiguous |
| `candidates` | `state_candidate` and `next_value_candidate` records. No FSM is built. |

**Process roles** are `sequential`, `combinational`, `latch`,
`initialization`, `generic`, `unknown` or `ambiguous`. They are derived from
the keyword, the event structure, the assignment kinds and the assignment
completeness:

| Process | Role | Confidence |
|---|---|---|
| `always_ff` with edge events | sequential | high when all assignments are nonblocking, otherwise medium |
| plain `always` with edge events | sequential | medium when all assignments are nonblocking, otherwise low |
| `always_comb` | combinational | high; medium when a target is not completely assigned |
| `always_latch` | latch | high |
| `@*`, or a level list that covers every signal read | combinational | medium; low when a target is only conditionally or ambiguously assigned |
| `@*` or complete level list with an incomplete target | latch (inferred) | medium |
| level list that misses a read signal | generic | low |
| no event control (procedural timing) | generic | low or unknown |
| mixed edge and level events, or keyword/event conflicts | ambiguous | unknown |
| `initial` / `final` | initialization | high |

Opaque (unsupported) statements lower confidence by one level. Confidence
reflects how complete the evidence is. Every non-unknown classification
carries `evidence`: `{code, refs}` items whose codes come from a fixed
vocabulary and whose refs are Semantic IR identities.

**Clocks and resets.** These come from event and control structure, never
from names.

- **Asynchronous reset:** an event signal tested by the leading `if` /
  `else if` chain, whose branch assigns only constants. It is **confirmed**
  when the edge agrees with the polarity (negedge ↔ `!rst`, posedge ↔
  `rst`), and **ambiguous** otherwise.
- **Clock:** the remaining edge-event signal. It is **confirmed** when it is
  not read in the process body.
- **Synchronous reset:** in a single-clock process, a leading `if` on a 1-bit
  non-clock signal with a constant-only branch and an `else` branch. It is
  always a **candidate**, because structure alone cannot tell it apart from
  a constant load.
- **Polarity** comes from the test form: `s`, `!s`, `~s`, `s == 0/1`,
  `s != 0/1`.

**Registers, priority, enables and holds.** Each register gets priority
*leaves* in if-else order:

- `reset` (under the reset branch),
- `update`,
- `hold_explicit`,
- `hold_implicit` (a branch that leaves the register unassigned).

Ranks preserve nested-condition priority, so `if (rst) … else if (en) …
else q <= q` gives reset > enable > hold. Reset leaves always rank first.

`update` is classified as unconditional, conditional, case, mixed or
reset_only. `hold` is none, explicit, implicit, mixed or ambiguous; it is
ambiguous when an opaque statement or loop prevents a decision.

**Combinational completeness** is computed over the control-path lattice
`none < partial < unknown < conditional < full`:

- an `if` without `else` makes a target incomplete;
- a `case` without `default` whose items all assign makes it conditional,
  because the selector coverage is unknown;
- slice writes and loops make it ambiguous;
- an earlier whole assignment covers later missing branches.

**State candidates** are conservative and never decided by name. All of the
following must hold:

- the register has a finite width of 2–64 bits;
- it has no arithmetic self-feedback (counters are excluded);
- **one of:**
  - **one-process:** the register selects (case selector or `if` predicate)
    updates of itself to at least two distinct constants;
  - **two-process:** the register's next value is a single combinational
    signal whose assignments are selected by the register and take at least
    two distinct constants. That signal becomes a `next_value_candidate`.

KF-DQ-009 does not build a transition graph, classify encodings or score
FSMs; that is KF-DQ-011.

**Naming guardrail.** Names such as `*_clk`, `*_rst`, `*_next` and `state`
only add a descriptive `name_hint` evidence item. The validator rejects any
classification whose only evidence is `name_hint`.

**Identity and provenance.** Every behavioral object has
`id = "beh1:" + sha256("kf-beh", "v1", category, anchor, qualifier)[:16]`,
where the anchor is a Semantic IR v2 identity (process, signal, statement or
assignment). Identities are therefore stable across runs and relocation.
Every record carries `loc` (repository-relative file:line:col) and, where
applicable, `semantic` (the `sem1:` identities it was derived from).

**Validation and gates.** `scripts/behavior/validator.py` (also
`make check-behavior`, run by `data-quality`) fails closed. Per document it
checks:

- schema and versions
- required fields and enumerations
- `beh1:` identities and duplicates
- provenance and portability
- canonical ordering
- evidence presence and the naming-only rule
- role/evidence consistency, for example: a sequential role needs edge
  evidence; high confidence needs `always_ff` and nonblocking assignments;
  a combinational process may have no edges
- clock/event agreement (index, edge, signal)
- reset agreement (signal, polarity and event-list membership against the
  `if` test)
- register / combinational / latch consistency with the process role
- the enable ↔ hold relationship and explicit holds being real
  self-assignments
- reset-first priority
- consistency with the Semantic IR document: module, source, sha256, the
  process set and every assignment and statement reference
- counts

Corpus checks:

- one document per canonical module
- canonical bytes
- re-analysis of the current Semantic IR reproduces the document byte for
  byte
- the Semantic IR input exists

The report is written to `analysis/reports/behavior_report.json` with a
`corpus_sha256`.

**Pipeline.**

1. The parse step writes v1 IR and Semantic IR v2.
2. **Behavioral analysis** runs.
3. The pre-dataset stale gate runs. A behavioral document is CURRENT only
   when it was derived from the current Semantic IR (sha256), re-analysis
   reproduces it, and it validates. Documents for non-canonical modules are
   ORPHAN, and any other file there is UNMANAGED.
4. The Semantic IR gate runs.
5. The **behavior gate** runs (no override).
6. Datasets, provenance, the post-run stale gate and the manifest gate run.

`make behavior` regenerates Behavioral Semantics v1 alone, and
`make pipeline` regenerates everything.

**Data manifest (version 3).** Each module gains
`behavior {path, sha256, schema_version, identity_version, module_id, status,
derived_from {semantic_ir, semantic_ir_sha256}}`, and the manifest's
`versions` gain `behavior` and `behavior_identity`.

**Boundaries.**

- **Not in KF-DQ-009:**
  - signal, process or transitive dependency graphs and structural
    connectivity (KF-DQ-010)
  - FSM extraction, transition graphs, encodings and FSM confidence
    (KF-DQ-011)
  - synthesis-equivalent scheduling
- **What KF-DQ-009 provides for those tasks:** the process read/write
  summaries, register / next-value / state candidates and evidence.
- **Unchanged:** Semantic IR v2, v1 IR, prompts, datasets and splits.

## 5.4 Structural Analysis v1 (KF-DQ-010)

**Purpose.** Behavioral Semantics (§5.3) says *what each process does*.
Structural Analysis v1 records *how the hardware is connected*: drivers and
loads of every signal, data / control dependencies, register boundaries,
fan-in / fan-out, cones, combinational cycles and hierarchy connectivity. It
answers questions such as "what drives X", "what controls register Q",
"what is the fan-in cone of Y" or "where are the combinational cycles", and
it exposes the structural evidence FSM integration (KF-DQ-011) needs without
implementing any FSM logic.

**Data flow.**

```text
Raw RTL -> Semantic IR v2 -> Behavioral Semantics v1 -> Structural Analysis v1 -> FSM Analysis v2 (§5.5) -> datasets
```

- `scripts/structural/analyzer.py` reads the persisted Semantic IR v2 and
  Behavioral Semantics v1 documents of a module and the canonical module
  inventory (to resolve instance children). It never parses RTL and never
  modifies `normalized/ir`, `normalized/semantic_ir/v2`,
  `normalized/behavior/v1` or raw RTL.
- Behavioral classifications (process roles and confidence, clocks, resets,
  registers, enables, holds, next values, state candidates) are consumed as
  they are; the validator rejects a document whose process role differs from
  the behavioral role (no silent override).
- The analysis refuses (fails closed) when the behavioral document was
  derived from another Semantic IR revision than the current one.
- Output: one document per canonical module at
  `normalized/structural/v1/<ip>/<module>.json`, schema
  `kritva-forge-structural-analysis` version 1, canonical JSON (sorted keys,
  compact separators, trailing newline).
- `scripts/structural/query.py` answers queries over a persisted document
  (cones, drivers, loads, controls, registers controlled by a signal, SCCs)
  without re-analysis.

**Schema.** Top level: `schema`, `versions` (schema 1, identity 1, analyzer
1, provenance 1, Semantic IR 2 / identity 1, Behavioral Semantics 1 /
identity 1), `generator`, `module` (ip, name, `mod1:` module identity, Semantic
IR module identity, location, source path and sha256, the exact Semantic IR
and Behavioral Semantics documents used: path, sha256, schema and identity
version), the sections below, `counts`, `notes`, `fingerprint` and `id`.

| Section (entity) | Content |
|---|---|
| `signals` (Port / Signal) | kind, direction, width, driver and load lists, `driver_status` (driven / undriven / external / unknown), `driver_units`, `multiple_drivers`, `possible_drivers`, `fan_in` (direct, expression, control, processes, drivers, signals), `fan_out` (processes, assignments, signals, outputs, instances, loads), structural `classes` with status and evidence, register |
| `processes` (Process) | behavioral role / confidence (copied), boundary, `reads`, `writes`, `read_write` ordering (read_first / write_first / nonblocking / mixed / unknown), assignments, drivers, predicates |
| `assignments` (Assignment) | kind, context, boundary, targets, partial write, data and control sources, guards (outer to inner), order in the process |
| `predicates` (Predicate / Case) | if / case / casez / casex statement, qualifier, predicate signals, case items and label signals, default, controlled targets, enclosing guard (`parent`), depth, role (control / reset / enable) |
| `drivers` (Driver) | one per (signal, driver unit); kinds continuous_assignment, net_declaration, procedural, initializer, instance_output, instance_inout, instance_unknown, module_input; assignments in order, connection, whole / partial write, status |
| `loads` (Load / Expression use) | one per Semantic IR read / connect reference plus one per module output: kind (assignment_value, ternary_condition, target_index, condition, case_expression, case_item, event, loop_condition, instance_input / inout / unknown / index / parameter, subroutine, statement, declaration, module_output), consumer, process, targets |
| `dependencies` (Dependency) | `source -> target` with kind (data / control / reset / enable / hold / clock), context (continuous_assignment, procedural_assignment, sequential_update, initialization, reset, hold, loop_control, condition, case_expression, case_item, ternary_condition, target_index, enable, clock_event), boundary (combinational / sequential / latch / initialization / unknown), anchoring construct `via`, process, assignments, Semantic IR references, behavioral anchors, `direct`, `partial`, status, hold kind |
| `registers` (Register) | behavioral register: clock (signal, edge, status), resets (signal, kind, polarity, status, reset assignments), enables, explicit / implicit holds, next values with source signals, priority leaves (reset / update / hold with guards), state candidate |
| `multiple_drivers` | signals with more than one driver unit: units, kinds, reasons (partial_writes, generate), status (confirmed / candidate) |
| `cycles` | combinational strongly connected components (Tarjan): signals, edges, status |
| `cones` | fan-in cones of registers and outputs, fan-out cones of registers and inputs |
| `instances` / `connections` / `hierarchy` (Instance / Connection / HierarchyEdge) | child module, resolution (resolved / unresolved with reason), parameters, connections with direction and flow (parent_to_child / child_to_parent / bidirectional / unknown), one `instantiates` edge per instance |

**Relationship rules.**

- *Data*: each signal read in an assignment value drives each assignment
  target. A ternary condition, a target index and every guard predicate is a
  *control* relationship, never an ordinary data input.
- *Reset / enable*: a guard edge becomes `reset` when its statement and
  signal are the behavioral reset of the process (status confirmed for
  asynchronous, candidate for synchronous resets), and `enable` when the
  statement is a behavioral enable of that register.
- *Clock*: the behavioral clock of each register (`clock_event`).
- *Hold*: explicit holds are the self-assignment (`q <= q`), implicit holds a
  behavioral hold (`q -> q`, no assignment); the two stay distinct.
- *Boundary*: edges anchored in a sequential process are `sequential`; cones
  stop at registers and record them, so register boundaries are explicit.
- *Hierarchy*: instances are not flattened. Inputs become loads, outputs
  become drivers of the parent signal; unknown directions (unresolved
  children) are `candidate`, and a signal driven only through them is
  `unknown`, never `undriven`. A child without canonical IR stays
  `unresolved` (`not_resolved_by_elaboration` or `no_canonical_child_ir`).
- *Cycles*: computed over combinational / latch edges; for-loop control
  (`loop_control`) and values written before they are read in the same
  process are excluded.

**Identity, evidence and provenance.**

- Object identity `str1:` = sha256("kf-str", "v1", category, anchor,
  qualifier)[:16]; the anchor is a `sem1:` or `beh1:` identity, so identities
  depend only on RTL content. The document `id` is the content hash of the
  document; `fingerprint` hashes an identity- and location-free projection
  (names, kinds, widths, relationships).
- Structural classes use the vocabulary data, control, reset, enable, hold,
  clock, hierarchy, port_connection, driver, load, sequential_boundary,
  unknown with status confirmed / candidate / ambiguous / unsupported. Names
  (`*_clk`, `*_rst`, `*_en`, `state`) add `name_hint` evidence only; the
  validator rejects a class supported by its name alone.
- Every record carries a repository-relative location and at least one
  Semantic IR or behavioral anchor. The corpus report counts objects with
  valid provenance, missing provenance and invalid references (target 100 %
  / 0).

**Ordering.** Every section is sorted by `id`; id lists are sorted; guard
lists and priority leaves keep source order. Ordering never depends on
traversal, dictionary or hash order.

**Validation.** `scripts/structural/validator.py` is fail closed: schema and
version block, required fields, vocabularies, identity derivation, duplicate
identities and relationships, Semantic IR / behavioral / driver / load
references, provenance, absolute paths, read / write consistency with the
loads, drivers and behavioral process, role conflicts, relationship kind /
context / boundary consistency, counts, fingerprint and document identity,
naming-only classes, ordering. The corpus check adds inventory, canonical
bytes, inputs, byte-identical re-analysis and the leakage check, and writes
`analysis/reports/structural_report.json` (`make check-structural`, part of
`data-quality`).

**Pipeline and stale integration.** Structural documents are written right
after behavioral analysis, before the pre-dataset stale gate; the structural
gate runs after the behavior gate, and again after the split is written
(with the leakage check, writing the report). A structural document is
CURRENT only when its schema and analyzer version are current, it was derived
from the current Semantic IR and Behavioral Semantics (sha256), re-analysis
reproduces it and it validates; stray files are ORPHAN / UNMANAGED. Any of
these blocks publication (no override); the remedy is regeneration
(`make structural` or `make pipeline`).

**Data manifest (version 4).** Each module gains `structural {path, sha256,
structural_id, schema_version, identity_version, analyzer_version,
provenance_version, module_id, status, derived_from {semantic_ir,
semantic_ir_sha256, behavior, behavior_sha256}}`; `versions` gain
`structural`, `structural_identity`, `structural_analyzer`. A dataset record
whose provenance declares a structural dependency is listed with
`structural {path, sha256}`, which must be the module's current artifact.

**Leakage (KF-DQ-004).** Modules that share a structural `fingerprint` across
splits are reported (soft, `cross_split_fingerprints`). A dataset record that
depends on structural data of such a module fails the structural gate. No
current dataset record consumes structural data; KF-DQ-012/013 must resolve
the reported cross-split structures before they do.

**Boundaries.** KF-DQ-010 provides the structural evidence for KF-DQ-011
(state candidates and registers, next-value dependencies, `case(state)`
predicates, state-dependent outputs through fan-out, register boundaries,
enables, holds, resets) but implements no FSM extraction, encoding or
transition logic, no timing / PPA, no elaboration of generates and no
flattening of the hierarchy.

## 5.5 FSM Analysis v2 (KF-DQ-011, KF-DQ-011.1)

**Purpose.** Structural Analysis (§5.4) records *how the hardware is
connected*. FSM Analysis decides, from that canonical evidence only,
*whether a register and its next-state logic form a finite-state machine* and,
where the evidence suffices, characterises it: state register, state set,
encoding, reset state, transitions with structured guard paths and priority,
holds, Moore / Mealy outputs, actions, graph reachability, coupling between
FSMs, quality and the explicit unknowns.

The boundary is **KF-DQ-010 = evidence, KF-DQ-011 = interpretation**:
Semantic IR v2 says what RTL constructs exist, Behavioral Semantics what
processes and registers do, Structural Analysis what hardware relationships
exist; FSM Analysis only decides whether those relationships constitute an
FSM and how to characterise it. It is not a second RTL parser.

**Data flow.**

```text
Raw RTL -> Semantic IR v2 -> Behavioral Semantics v1 -> Structural Analysis v1 -> FSM Analysis v2 -> datasets
```

- `scripts/fsm/analyzer.py` reads the persisted Semantic IR v2, Behavioral
  Semantics v1 and Structural Analysis v1 documents of a module. It never
  parses RTL, never modifies any upstream artifact and fails closed when the
  behavioral document was derived from another Semantic IR revision, or the
  structural document from another Semantic IR / behavioral revision.
- Output: one document per canonical module (also when it has no FSM) at
  `normalized/fsm/v2/<ip>/<module>.json`, schema `kritva-forge-fsm-analysis`
  version 2, canonical JSON. Version 1 (`normalized/fsm/v1`, KF-DQ-011) is
  obsolete: KF-DQ-011.1 replaced it and removed it from the data working tree
  (it remains in git history); any v1 file left in the tree is STALE and fails
  the FSM and stale gates.
- `scripts/fsm/validator.py` validates documents and the corpus;
  `scripts/fsm/query.py` answers queries (FSMs, states, transitions, incoming
  / outgoing transitions, guard paths, encoding, outputs, actions, quality,
  state register, reachability, couplings, rejections) without re-analysis.
- **Legacy.** The parser-integrated FSM path (`scripts/fsm/fsm_*.py`,
  `scripts/structural/fsm_structural.py`, the `fsm` field of normalized IR v1,
  §8 / §9) is legacy / deprecated. FSM Analysis neither imports nor reads
  it; the parser, IR v1 and prompts are unchanged. Removing it is a separate
  task.

**Identification.** For every behavioral register `R` of a sequential process
(`N` is `R` itself in a one-process FSM, or the combinational signal of
`R <= N` in a two-process FSM):

| Rule | Evidence |
|---|---|
| A | Behavioral Semantics `state_candidate` for `R` |
| B | a next-value assignment guarded by an `if` / `case` / ternary predicate over `R` |
| C | at least two distinct resolved state values over reset and functional updates (three-valued: unresolved is unknown, never false) |
| L | Structural Analysis control dependency `R -> N` through such a predicate and data dependency `N -> R` across the sequential boundary (two-process), or control `R -> R` (one-process) |

`confirmed` = (A or B) and C and L; `candidate` = (A or B) and L without C.
A 1-bit register whose two values are plain literals, without A and without
named state constants, stays a `candidate` (`trivial_state_domain`): its two
values are its whole domain, so C carries no evidence (flags / handshakes,
AC-016). Multiple drivers, an ambiguous behavioral update or opaque
statements give `ambiguous`; a function call or hierarchical reference as next
value gives `unsupported`. Registers that look interesting (A, B, two
constants or a name hint) but fail are listed in `rejected` with a reason:
`arithmetic_feedback` (counters), `no_state_predicate`, `name_only`,
`no_closed_loop`. Names never contribute to status (`name_hint` is
descriptive). There is no minimum width.

**Schema.** Top level: `schema`, `versions` (schema 2, identity 1, analyzer 2,
provenance 1; Semantic IR 2, Behavioral Semantics 1, Structural Analysis 1
with identity versions), `generator`, `module` (ip, name, `mod1:`, Semantic IR
module identity, location, source, the three input documents with path,
sha256 and versions, plus the `str1:` structural identity), `fsms`,
`couplings`, `rejected`, `notes`, `counts`, `fingerprint`, `id`.

| Record | Content |
|---|---|
| FSM | status (confirmed / candidate / ambiguous / unsupported), quality (high / medium / low / ambiguous / unsupported), style (one_process / two_process), register (signal, name, width, `beh1:` register, `str1:` register, process), next-state signal, clock, reset (kind, polarity, status, signal, reset state, assignments), enables, hold (none / explicit / implicit / mixed), encoding, states, transitions, outputs, actions, reachability, evidence, unknowns (one record per kind), shape `fingerprint` |
| State | encoded value, width, name and aliases, constant (parameter / localparam / enum_member / literal, `sem1:` reference, implicit enum value), declared / observed, reachability (graph_reachable / graph_unreachable / unknown), reset flag |
| Transition | source and target state (or `*reset`, `*unknown`, `*none`), kind (explicit / explicit_hold / implicit_hold / default / reset), structured guard path, priority, assignment or behavioral hold, process, status (confirmed / derived / unknown), non-canonical `rendered` predicate |
| Guard entry | Semantic IR statement, `str1:` predicate, kind (if / case / casez / casex / ternary / loop), branch (then / else / item / default / body), item, whether it tests the state register, the source states it selects, qualifier, role (control / reset / enable) |
| Output | output port, Moore / Mealy / ambiguous, `registered`, `state_sources`, `sampled_sources` (registered outputs: inputs / registers read by the update logic) and `other_sources` (non-state sources of the combinational output cone; `[]` for a registered output) |
| Action | assignment to another signal under a guard over the state register, per source state; kind output / register / control |
| Coupling | `predicate` (a guard of FSM B tests the register of FSM A) or `data` (A's register feeds B's next value); FSMs are never merged |

**Extraction rules.**

- *State domain* is built only from resolved constants: values written to `R`
  / `N`, reset values, `case` labels and equality tests over `R`, and the
  members of the register's enum type (typedef, or the anonymous enum linked
  through the members used). Enum members without an initializer take the
  IEEE 1800 §6.19 value (first 0, then previous + 1) and are marked
  `implicit`; nothing else is inferred. An unresolved constant makes the
  domain incomplete (`incomplete_domain`).
- *Guard paths* keep Semantic IR order (outermost first) and are the
  canonical identity of a transition predicate (statement, branch, item and
  structural predicate per level); `rendered` (e.g. `state == IDLE && start`)
  is a deterministic display string only. The reset branch is
  kept in the path but is not a source-state selector. An `else` / `default`
  selects the complement of the tested values only when the domain is
  complete, otherwise its source is `*unknown`; an empty complement (all
  values covered) gives source `*none` and no graph edge.
- *Priority* is the behavioral assignment order; a later unconditional
  assignment for a source state overrides an earlier one (two-process default
  `N = R` becomes an `implicit_hold` only where no later assignment applies).
- *Outputs*: an output port is state-dependent when its Structural Analysis
  fan-in cone contains the state register (or next-state signal). A
  combinational output is Moore when the cone holds no module input and no
  other register, Mealy when module inputs contribute, otherwise ambiguous.
  Names never decide.
- *Register boundary (KF-DQ-011.1)*: an output is `registered` when Structural
  Analysis has a register record for that signal (matched by `sem1:`
  identity) and every such record has a sequential boundary; a latch is not a
  boundary. A registered output is a temporal boundary: its combinational
  output cone ends at the register, so it is Moore and `other_sources` is
  `[]`. The update logic behind it is recorded as `sampled_sources`: module
  inputs and other registers reached through the data, control and enable
  dependencies into the register (an `enable` is the condition under which it
  updates) and the combinational cone behind them, stopping at registers;
  clock, reset, hold, the output itself, the state register and the
  next-state signal are excluded. Sampled sources never make an output Mealy.
  The Structural Analysis `cone()` query is unchanged (KF-DQ-010 contract); it
  still walks a register's own update logic when started at that register,
  so the FSM analyzer applies the boundary itself. Corpus (350 modules): 445
  outputs, Moore 333 / Mealy 92 / ambiguous 20; 313 registered (24 of them
  the state register).
- *Encoding* is classified from the actual resolved state values only (never
  names): gray (n >= 3 and every distinct-state transition flips one bit),
  binary (dense 0..n-1), one_hot (one bit per state and width =
  state count), otherwise custom; status `explicit` when all states are named
  constants, `inferred` with literals, `unknown` when the domain is
  incomplete.
- *Reachability* is graph reachability from the reset state over the
  extracted transitions (`known` only with a reset state, a complete domain
  and no unknown transition). It is never predicate feasibility; the query API
  reports `predicate_feasibility: not_analyzed`.
- *Quality*: high = confirmed, explicit encoding, reset state known and no
  unknown transition; medium = confirmed with gaps; low = candidate.

**Identity and determinism.** `fsm1:` = sha256("kf-fsm", "v1", category,
anchor, qualifier)[:16] with `sem1:` / `beh1:` anchors. The document `id`
hashes the canonical document without `id` and `fingerprint`; FSM
fingerprints hash a names-, values-, identity- and location-free shape
(status, style, encoding style, state count, transition topology, guard
structure, output kinds); the module fingerprint is the sorted set of FSM
shapes. Lists are sorted by identity (states by value, guard paths in source
order).

**Validation, pipeline, stale, manifest.** The validator is fail closed
(schema, versions, required fields, vocabularies, identity derivation,
duplicates, state / Semantic IR / behavioral / structural references,
provenance and paths, encoding recomputation, reachability closure,
status / quality / evidence consistency, naming-only FSMs, counts,
fingerprints, document identity); the corpus check adds inventory, canonical
bytes, byte-identical re-analysis and leakage and writes
`analysis/reports/fsm_report.json` (`make check-fsm`, part of
`data-quality`). FSM documents are written after structural analysis; the FSM
gate runs after the structural gate and again after the split (with leakage).
An FSM document is CURRENT only when derived from the current Semantic IR,
Behavioral Semantics and Structural Analysis (sha256), reproduced by
re-analysis and valid; stray files are ORPHAN / UNMANAGED (no override,
regenerate with `make fsm` or `make pipeline`). The validator checks
`registered` against the Structural Analysis register records, independently
of the analyzer's cone rule (Moore and `other_sources == []` for registered
outputs; no clock / reset / state signal in `sampled_sources`); the corpus
data test asserts the expected corpus classification. Data manifest version 5 adds
`modules[].fsm {path, sha256, fsm_id, versions, module_id, status,
derived_from {semantic_ir, behavior, structural + sha256}}`, `records[].fsm`
for records with an FSM dependency and `versions.fsm` / `fsm_identity` /
`fsm_analyzer` (2 / 1 / 2 since KF-DQ-011.1; the manifest schema stays 5).

**Leakage (KF-DQ-004).** FSM shapes occurring in modules of more than one
split are reported as soft findings; a dataset record depending on FSM data
of such a module fails the gate. The four KF-DQ-010 cross-split structural
groups are carried over unchanged (soft, not promoted); promotion and any
re-split remain a program-level decision.

**Reproducibility.** Output depends only on the content of the three input
documents: no timestamps, process ids, host / user names, random values,
object identities or absolute paths. Clean regenerations (A/B) and a
regeneration from a relocated checkout are byte-identical; the validator
re-analyses every document and rejects any byte difference.

**Known limitations.**

- Unsupported constructs (function calls or hierarchical references as next
  value) give `unsupported`; multiple drivers, ambiguous behavioral updates
  and opaque statements give `ambiguous`; none is ever confirmed.
- State values are only what resolves to a constant: package / external /
  undeclared names, wildcard `casez` / `casex` labels and non-literal enum
  initializers leave the domain incomplete (`incomplete_domain`, unknown
  sources, encoding `unknown`); values are never guessed.
- A reset is known only when Behavioral Semantics classifies it (a trailing
  `if (rst)` override at the end of a block is not, so such FSMs have no
  reset state and unknown reachability).
- Reachability is graph reachability; predicate feasibility (SAT / SMT) is not
  analysed. Hierarchy is not flattened: an FSM spread over instances, or an
  output whose cone crosses an unresolved instance, is not interpreted
  across the boundary.
- The Semantic IR width of an anonymous-enum signal is not the enum width; it
  is reported as unknown (`null`).

**Query API** (`scripts/fsm/query.py`): `fsms`, `fsm`, `state_register`,
`states`, `state`, `transitions`, `incoming`, `outgoing`, `guard`, `encoding`,
`outputs`, `actions`, `quality`, `reachability` (always with
`predicate_feasibility: not_analyzed`), `couplings`, `rejected`; results are
deterministic copies.

**Boundaries.** No RTL parsing, elaboration, SAT / SMT feasibility, timing,
synthesis, RTL generation or modification of upstream artifacts.

## 5.6 Prompt v2 — behavior-aware prompts (KF-DQ-012)

**Purpose.** Prompt v2 turns the four analysis layers of a module into a
natural-language description of its interface and behavior, for RTL
generation and completion tasks. It describes *what the hardware does*
(clocks and resets, registers, combinational logic, FSMs, hierarchy) without
reproducing the RTL that answers the task. It is an abstraction of the
canonical evidence, never a re-reading of the source.

**Data flow.**

```text
Semantic IR v2 + Behavioral Semantics v1 + Structural Analysis v1 + FSM Analysis v2 -> Prompt v2
```

- `scripts/prompt_v2/render.py` reads the four persisted documents of a module
  and checks them before rendering. It fails closed (`PromptError`) when:
  - a schema, identity or analyzer version differs (FSM Analysis must be v2,
    analyzer 2; FSM v1 is rejected);
  - the four module identities differ;
  - the provenance chain is broken: behavior ← Semantic IR; structural ←
    Semantic IR and behavior; FSM ← Semantic IR, behavior and structural
    (path and sha256);
  - the module source named by Semantic IR `module.source` is missing or its
    sha256 differs.
- Output layout: `generated/prompt/v2/<ip>/<module>.behavior_aware.txt` (prompt)
  and `.json` (sidecar), one pair per canonical module, zero-FSM modules
  included. `behavior_aware` is the only variant; any other variant name is
  UNMANAGED.
- **Prompt v1 is unchanged.** `generated/prompts/` (§11.2) keeps its
  generator, layout and dataset use. No dataset record consumes Prompt v2
  before KF-DQ-013 (the validator fails a record that does).

**Abstraction contract.** Sections, in fixed order:

1. Module (parameters, ports in Semantic IR order).
2. Clocks and resets.
3. Registers: width, clock edge, reset value, what the next value depends on,
   what controls it, hold.
4. Combinational logic.
5. State machines: status, quality, style and encoding; timing; states;
   reachability; outputs; transitions; actions; couplings.
6. Submodules.

Rendering rules:

- Guards use a fixed natural-language vocabulary: `is`, `is not`, `and`,
  `or`, `not`; for example `IDLE -> RUN when start is 1`. A test of the
  source state is absorbed into the transition source. An expression that
  cannot be rendered becomes `a condition on <signals>` and is counted in
  `abstraction.guard_fallbacks`.
- Unresolved references (Semantic IR `target: null`) are rendered from their
  source text only when it is a plain or dotted name. Index and part
  selects are put into words, from the inside out (KF-DQ-012.1):
  - `req_fifo[0].haddr` becomes `field haddr of element 0 of req_fifo`;
  - `a[3:0]` becomes `bits 3 to 0 of a`.

  Anything else becomes `an unresolved signal`. Raw source text with
  selects is never emitted.
- The prompt never contains HDL operators or keywords (`<=`, `==`, `&&`,
  `always`, `assign`, ...), index / select syntax (`x[0]`, rejected as
  `hdl_syntax`), source comments, parser metadata (`node_id`,
  `SyntaxKind`, offsets) or absolute paths. The validator enforces all of
  these.
- Uncertainty is never upgraded and uses a fixed vocabulary:
  - status markers: `status candidate / ambiguous / unsupported`, `(candidate)`;
  - origin markers: `[derived]`, `[unknown]`, `(unresolved)`;
  - unknown values: `an unknown state`, `unknown width`,
    `States: unresolved`, `priority unknown`.

  A candidate, ambiguous or unsupported FSM is never described as confirmed.
- Registered outputs follow FSM Analysis v2: they are Moore. `sampled_sources`
  are stated as what the register samples, never as a Mealy dependency.

**Size budget.** 32 KiB per prompt. When a prompt is larger, records are
dropped section by section in this order:

1. Submodules
2. Combinational logic
3. Registers
4. FSM transitions and actions

Each truncated section ends with `[truncated: <section> <emitted>/<total> records]`.
The module, clocks/resets and FSM header sections are mandatory: if they
alone exceed the budget, generation fails rather than emitting an incomplete
prompt. The sidecar records `truncated`, `truncated_sections`,
`original_bytes` and `budget_bytes`.

**Answer-leakage metric (frozen).** Every prompt is measured against its
completion, the module source file (sha256 verified):

| Version | Definition |
|---|---|
| tokenizer `sv-lex-v1` | SystemVerilog lexical tokens (sized/based literals, numbers, identifiers, multi-character operators, single characters) after removing comments and whitespace; the same tokenizer for prompt and completion |
| metric `leakage-v1` | *overlap*: distinct normalized prompt 4-grams found in the normalized completion, divided by distinct normalized prompt 4-grams. Normalization drops HDL keywords and punctuation and maps canonical identifiers (module, port, signal, parameter, enum member, instance and child-module names, child port names) to `ID`; all-`ID` 4-grams are ignored. *longest run*: the longest matching block of the full token streams (identifiers kept, `,` `;` removed) that contains at least one structural token (operator, bracket, literal, keyword or non-canonical identifier). A raw 4-gram overlap is kept as a diagnostic only. |
| thresholds `thresholds-v1` | overlap ≤ 0.20 and longest run ≤ 6 tokens |

Names alone are never leakage. Metric values are stored in the sidecar. A
failure fails closed, and prompts are never rewritten to pass. Changing the
metric after KF-DQ-012 needs a new versioned contract.

**Sidecar and identity.** The sidecar (schema `kritva-forge-prompt` version 2,
generator 1, identity 1; canonical JSON) records:

- the versions of every layer and of the leakage contract;
- module identity and variant;
- the four inputs (path, sha256, schema, identity) and the source (path, sha256);
- prompt path, sha256, bytes and section list, plus truncation data;
- guard fallbacks, uncertainty counts, FSM statistics and leakage metrics;
- `identity` = `pv2:` + the first 16 hex digits of the sha256 of the sidecar
  without `identity`.

Output is byte-identical across runs, hosts and checkout locations.

**Cross-split classification `rtl-sim-v1`.**
`scripts/prompt_v2/classify.py` recomputes groups from the persisted
documents, never from reports:

- *Groups*: modules sharing a Structural Analysis fingerprint or an FSM
  per-FSM fingerprint whose members span more than one split. Groups with
  the same members are merged; the id is `grp1:` + sha256(sorted module ids).
- *Score*: comment-stripped source tokens are alpha-renamed (`v0, v1, ...`,
  so renaming cannot hide a copy). The score is
  `difflib.SequenceMatcher(autojunk=False).ratio()`, maximised over member
  pairs in different splits.
- *Classes*:

  | Class | Score | Effect |
  |---|---|---|
  | `near_duplicate` | ≥ 0.70 | must be re-split before KF-DQ-013 consumes Prompt v2 (`kf_dq_013_entry` = blocked; `unresolved_near_duplicates` lists the groups) |
  | `structural_similarity` | 0.30 – 0.70 | soft finding, reviewed by KF-DQ-013 |
  | `informational` | < 0.30 | does not block |

- **KF-DQ-012 changes no split** (`splits_changed: false`). Prompt content
  and dataset task labels are not used for classification.  Split schema v2
  (KF-DQ-013.0, §11.1) groups every near-duplicate pair before the split is
  assigned, so the classification reports no `near_duplicate` group.
- Output: `analysis/reports/prompt_leakage_classification.json`. It is STALE
  when the split manifest or fingerprints change.

**Compatibility contract.** `scripts/core/compat.py` holds `REQUIRED`, the
single declaration of the versions this forge revision needs:

- manifest 6;
- Semantic IR 2/1, Behavioral Semantics 1/1, Structural Analysis 1/1/1,
  FSM Analysis 2/1/2, Prompt 2/1/1;
- tokenizer, metric, thresholds and classifier versions.

Each layer's own constants must equal `REQUIRED` (tested).

`make check-compat` compares `REQUIRED` with the data manifest (schema
version, `versions` block, recorded `compatibility.required`) and with every
sidecar's `versions`. It writes `analysis/reports/compatibility_report.json`.
Compatibility is decided by versions, never by commit hashes.

**Gates and reports.**

- The pipeline runs Prompt v2 after the FSM gate and before datasets
  (`write_prompt_v2`, `prompt_v2_gate`).
- After the split, the gate re-runs with classification and writes:
  - `analysis/reports/prompt_v2_report.json`: sizes, truncation, leakage
    distribution, uncertainty, FSM and provenance coverage;
  - the classification report.
- After the manifest, `compat_gate` checks compatibility. It also requires the
  corpus, compatibility, stale and classification reports.
- `KRITVA_FORGE_ALLOW_STALE` bypasses none of these gates.
- Stale integration:
  - a prompt or sidecar that differs from a regeneration is STALE;
  - a prompt without its sidecar is STALE;
  - a sidecar without its prompt, or a pair for a non-canonical module, is
    ORPHAN;
  - any other file under `generated/prompt/` is UNMANAGED.
- `make check-prompt-v2` runs the validator (with classification and reports).
  `make data-quality` includes `check-prompt-v2` and `check-compat`.

**Boundaries.** No RTL parsing, no new analysis, no modification of upstream
layers, Prompt v1, dataset records or splits, no LLM call, and no dataset
consumption of Prompt v2 (KF-DQ-013).

---

# 6. Semantic Analysis

Semantic analysis adds meaning to the normalized structural representation.

Examples include:

* Symbol resolution
* Signal classification
* Scope resolution
* Type information
* Assignment relationships
* Expression relationships
* Process relationships
* Signal roles

The semantic layer should provide information to downstream structural and FSM analysis without coupling those components directly to the parser.

---

# 7. Structural Analysis

Structural analysis identifies relationships in the RTL.

Examples:

```text
Signal
  │
  ├── assigned by Process
  ├── read by Process
  ├── used in Case
  ├── used in Condition
  └── drives another Signal
```

Structural analysis can build:

```text
Signal dependency graph
Assignment graph
Process graph
Control-flow relationships
Data-flow relationships
```

The structural layer is particularly important for FSM detection.

The implemented, versioned form of this layer is Structural Analysis v1
(KF-DQ-010, §5.4): `normalized/structural/v1`.

---

# 8. FSM Architecture

> **Legacy / deprecated.** §8 and §9 describe the parser-integrated FSM path
> (`scripts/fsm/fsm_*.py`, `scripts/structural/fsm_structural.py`, the IR v1
> `fsm` field). The canonical FSM interpretation is FSM Analysis v2 (§5.5,
> KF-DQ-011); the legacy path is kept unchanged until a separate removal task.

FSM analysis is a consumer of the RTL IR and structural information.

It should not depend primarily on signal naming conventions.

The FSM pipeline is:

```text
RTL IR
  │
  ▼
Candidate Detection
  │
  ▼
Candidate Validation
  │
  ▼
State Identification
  │
  ▼
Transition Recovery
  │
  ▼
Action Recovery
  │
  ▼
Encoding Resolution
  │
  ▼
FSM Quality Analysis
  │
  ▼
FSM Graph
```

---

## 8.1 Candidate Detection

Potential state registers can be identified using structural evidence.

Useful evidence includes:

* Used as a `case` expression
* Assigned in sequential logic
* Assigned from a next-state expression
* Reset behavior
* Limited number of distinct values
* Participation in control logic
* Relationship between current-state and next-state signals

Signal names can be used as hints but should not be the fundamental detection mechanism.

---

## 8.2 Candidate Validation

A candidate should be validated before being promoted to an FSM.

A strong candidate typically has evidence such as:

```text
State register
     │
     ├── sequential assignment
     │
     ├── reset behavior
     │
     ├── case(state)
     │
     └── next-state relationship
```

This prevents high-fanout datapath signals from being incorrectly classified as FSM state registers.

---

## 8.3 State Extraction

States may be recovered from:

* Case labels
* Enumerations
* Parameters
* Localparams
* Constant assignments
* Explicit state encodings

The system should preserve both:

```text
raw label
normalized label
resolved value
```

when available.

---

## 8.4 Transition Extraction

Transitions are recovered from next-state assignments within state-specific control paths.

Conceptually:

```text
Current State
      │
      ▼
   Condition
      │
      ▼
 Next State
```

A transition should contain information such as:

```text
Transition
├── source_state
├── target_state
├── condition
└── metadata
```

Multiple current states represented by a case label must be expanded into individual transition edges where appropriate.

---

## 8.5 Action Extraction

Actions describe behavior associated with states or transitions.

Examples include:

* Output assignments
* Register updates
* Control signal changes
* Datapath operations
* Interface operations

Actions should remain separate from transition structure.

---

# 9. FSM Quality Analysis

Once an FSM graph has been constructed, additional analysis can be performed.

Potential checks include:

```text
Reachability
Unreachable states
Dead-end states
Terminal states
Self-loops
Strongly connected components
Missing transitions
Default transitions
State coverage
Transition coverage
```

This allows the framework to move beyond:

> "I found an FSM."

toward:

> "I understand the structural quality of the recovered FSM."

---

# 10. Graph and Dependency Analysis

FSMs are one type of graph within RTL.

Kritva Forge should eventually represent:

```text
Module graph
Hierarchy graph
Signal dependency graph
Assignment graph
Process graph
FSM graph
Data-flow graph
Control-flow graph
```

These graphs can become an important foundation for hardware design intelligence.

---

# 11. Dataset Architecture

Dataset generation is downstream of RTL understanding.

The pipeline is:

```text
RTL
 │
 ▼
Parser
 │
 ▼
RTL IR
 │
 ▼
Analysis
 │
 ▼
Normalized Knowledge
 │
 ▼
Dataset Builder
 │
 ├── SFT dataset
 ├── Evaluation dataset
 ├── Golden dataset
 └── Benchmark dataset
```

The dataset generation code is public.

The actual RTL corpus and generated private datasets reside in:

```text
KritvaOS/kritva-forge-data
```

## 11.1 Leakage-Safe Splits

Train / validation / test assignment is deterministic and leakage-aware
(`scripts/dataset/leakage.py`, `LEAKAGE_SCHEMA_VERSION = 1`).  There is no
random seed; the split depends only on record content.

Every record carries versioned identities:

| Kind | Identities | Rule |
|------|------------|------|
| Hard | `source_rtl`, `module_body`, `normalized_ir`, `completion` | must never cross splits |
| Soft | `body_shape` (identifier-agnostic), `module_name` | reported only |
| Info | `ip` | reported only |

Records sharing any hard identity are joined into one leakage group.
`body_shape` also joins groups when the same shape appears in more than one
IP (cross-IP near-duplicates).  Groups spanning several IPs are shared
library code and are assigned to `train`; each IP's remaining records form
one atomic unit, placed largest first into the split furthest below its
target (70 / 15 / 15).

**Split schema v2 (`SPLIT_SCHEMA_VERSION = 2`, KF-DQ-013.0).**  The
`rtl-sim-v1` near-duplicate pairs are grouping edges too
(`scripts/dataset/near_duplicate.py`):

- Candidates are every pair of canonical modules sharing a Structural
  Analysis fingerprint or an FSM per-FSM shape fingerprint.  They are
  computed over the whole corpus before and independent of the split.
- A pair with similarity ≥ 0.70 (§5.6, the same single implementation as the
  KF-DQ-012 classification) joins every record of both modules into one
  group.  This applies to same-IP and cross-IP pairs alike.
- A resulting multi-IP group is a shared-library group (`train`).
- `structural_similarity` stays soft.
- There are no pins and no IP-family tables.
- Without edges, v2 reproduces the v1 assignment exactly.
- Missing documents or sources, or a source sha256 mismatch, fail closed.

The manifest records the edge document (`near_duplicate`: classifier,
tokenizer, threshold, sources, candidate count, sorted `{a, b, similarity}`
edges) and, per group, `joined_by` (`hard`, `body_shape`, `near_duplicate`).

The assignment is written to `splits/split_manifest.json` and the gate
report to `analysis/reports/split_leakage_report.json`.  Dataset generation
fails if any of the following holds:
- a hard group crosses splits;
- a record is duplicated;
- the manifest disagrees with the dataset files;
- the manifest's edge list differs from the edges recomputed from the data
  root;
- any near-duplicate edge spans two splits;
- the assignment is not reproducible from the identities and edges;
- the split schema is not 2.

`make check-leakage` re-runs the gate against an existing data checkout.
After KF-DQ-013.0 the KF-DQ-012 classification has no `near_duplicate`
group and `kf_dq_013_entry` is `open`.


## 11.2 RTL Provenance

Every canonical module is traceable from dataset record back to source RTL
(`scripts/core/provenance.py`, `PROVENANCE_VERSION = 1`):

```text
raw/rtl/original/<ip>/...              source file      sha256(file bytes)
  -> normalized/ir/<ip>/modules/<m>    normalized IR    sha256(YAML bytes)
  -> generated/prompts/<ip>/<m>.*      prompts          sha256(prompt bytes)
  -> datasets/pipeline/<split>.jsonl   dataset records  record_id (KF-DQ-004)
```

The canonical manifest is `manifests/provenance_manifest.json` in the data
repository, and the validation report is `analysis/reports/provenance_report.json`.

Each module entry records:

- `module_id`
- `source` (path and sha256)
- `contributing_sources` (the primary file plus include files, each with role and sha256)
- `unresolved_includes` and `external_includes`
- `normalized_ir`:
  - path
  - sha256
  - `ir_content`, the location-free KF-DQ-004 identity
- `module_body`
- `prompts`
- `artifacts` (the `normalized/ir/<ip>/rtl/` copy of the source)
- `dataset_records` (record_id and split for each record)
- `transformation` status

The manifest also has a `sources` index that lists every source file with its
sha256 and the modules it feeds. The pipeline and schema versions are recorded
at the top level.

Identities are kept distinct:

| Identity | Form | Meaning |
|---|---|---|
| source | 64-hex sha256 | Exact content of one RTL file |
| module | `mod1:` + 16 hex of sha256(`kf-module`, `v1`, ip, module) | Logical canonical module `<ip>/<module>` |
| module body | `m1:...` (KF-DQ-004) | Module text with comments and whitespace removed |
| normalized IR | sha256 of the YAML file; `ir_content` `n1:...` (KF-DQ-004) | Exact IR file; IR content without location fields |
| IR node | `n1:...` `node_id` (KF-DQ-003) | One IR node |
| dataset record | `r1:...` (KF-DQ-004) | One (ip, module, task, prompt_variant) record |

**Dataset records** carry a compact `provenance` block:

- `record_id`
- `module_id`
- source path and sha256
- IR path and sha256
- `generated_artifact`

The RTL text is not duplicated in this block. `generated_artifact` is `null` unless a curated RTL file replaced the completion.

`rtl_source` is `original`, or `curated` when a curated RTL file was used. It was previously mislabelled `generated`.

**Policies:**

- **Paths** are POSIX and relative to the data root (KF-DQ-002).
- **Hashes** are SHA-256 of raw bytes, with no mtime, PID, UUID, object id or absolute path.
- **Ordering:** all lists are sorted, and JSON is written with sorted keys.

The manifest is byte-identical across runs and checkout locations. Any change to the identity or manifest format bumps `PROVENANCE_VERSION`.

Canonical normalized IR is not changed by KF-DQ-005.

**Missing or external sources:**

- A missing primary source fails validation.
- An unconditional `` `include`` that cannot be resolved fails validation.
- A conditional include inside `` `ifdef``/`` `ifndef`` that is not part of the corpus is recorded under `external_includes` and reported, not failed. Macros are not evaluated. An example is yifive's optional `ycr_arch_custom.svh`.

Includes are resolved in this order:

1. The including file's directory.
2. The IP's `files.f` `+incdir+` directories.
3. A unique basename match inside the IP.

**Regeneration and validation:** `make pipeline` rewrites the manifest and fails if validation fails. `make check-provenance` (`python3 scripts/core/provenance.py --check`) recomputes the manifest from the data tree and fails on any of these:

- missing, orphan or duplicate records
- missing or duplicate module identities
- invalid or incorrect hashes
- absolute paths
- unresolved sources or includes
- invalid IR references
- a schema-version mismatch
- dataset records without traceability
- a stored manifest that differs from the recomputed one

`--write` regenerates the manifest in place.


## 11.3 Stale Generated Artifacts

`scripts/core/stale_artifacts.py` (`ARTIFACT_SCHEMA_VERSION = 1`) decides,
deterministically, whether each generated artifact is valid for the current
canonical data.

**Canonical** artifacts are the inputs and the canonical IR:

- `raw/rtl/original/`
- `normalized/ir/<ip>/{modules/*.yaml, hierarchy.yaml, summary.yaml, rtl/*}`

**Generated** artifacts are derived from them:

- `generated/prompts/`
- `analysis/reports/`
- `datasets/pipeline/`
- `splits/`
- `manifests/`
- `golden/`

Every file under the managed roots is in exactly one state:

| State | Meaning |
|---|---|
| `CURRENT` | In the expected inventory and verified against current inputs |
| `STALE` | Content, schema or identity no longer current, or a superseded duplicate of a canonical artifact (e.g. old `analysis/reports/<ip>/*.yaml`, `generated/rtl/pipeline/*` copies of raw RTL) |
| `ORPHAN` | Refers to a module, IP or record that no longer exists |
| `HISTORICAL` | Retained and never consumed: `*/legacy/`, `datasets/source/`, the KF-DQ-001 migration manifests, and quarantined files |
| `UNMANAGED` | Not part of the output contract, including symlinks and unknown top-level directories |

**Expected inventory.** The expected inventory is computed from three inputs:

- raw RTL
- the canonical IR
- the KF-DQ-005 provenance model, recomputed rather than read back

Existing outputs never define what should exist.

**Freshness** is based on identities and recomputation. Filesystem modification time is not used and is not sufficient. The checks are:

- **Prompts:** must equal `generate_module_prompt(<IR>)`.
- **Canonical IR:** stale when its source sha256 differs from the one recorded in the stored provenance manifest; orphan when its source is missing or no longer declares the module.
- **Dataset records:** must carry the current provenance version, `record_id`, `module_id`, source sha256 and IR sha256.
- **Split-manifest entries:** must reference current records.
- **Manifests and reports:** must carry the current schema version and match a recomputation.

**Outputs:**

- `manifests/artifact_inventory.json`: path, kind, state, ip, module, `module_id`, source sha256 and sha256 for each file.
- `analysis/reports/stale_artifact_report.json`: counters and the remediation plan.

Both are byte-identical across runs and checkout locations.

**Remediation:**

| Action | Applies to |
|---|---|
| `REGENERATE` | Rerun the pipeline: stale expected outputs and canonical IR |
| `REMOVE` | Orphans and superseded duplicates |
| `QUARANTINE` | Unmanaged files; moved to `generated/legacy/quarantine/<path>`, which makes them historical |
| `MANUAL` | Symlinks |

**Commands:**

| Command | Effect |
|---|---|
| `make check-stale` | Read-only gate (`stale_artifacts.py --check`) |
| `make clean-stale` | Dry-run plan |
| `make clean-stale APPLY=1` | Executes `REMOVE` and `QUARANTINE` and prints every action |

**Cleanup safety.** Cleanup only touches files under `generated/`, `analysis/reports/`, `datasets/pipeline/`, `splits/`, `manifests/` or `golden/`, outside the historical paths. It never touches `raw/` or `normalized/`. It rejects:

- absolute paths, `..` and symlinks
- anything that resolves outside the data root

All actions are validated before any is executed, and one unsafe entry aborts the cleanup.

**Pipeline gate:**

1. After IR and prompts are regenerated, a pre-dataset gate runs. If any stale, orphan or unmanaged artifact exists, it aborts before datasets, splits or provenance are written.
2. After all outputs are written, the inventory and report are written and the full gate runs again.

`KRITVA_FORGE_ALLOW_STALE=1` is an explicit override. It is recorded as `override: true` in the report, and the report status stays `FAIL`.

**Split, provenance and relocation.** The KF-DQ-004 split, the leakage gate and KF-DQ-005 provenance are consumed unchanged. A stale record or split entry fails the gate before it can reach a dataset. Paths are repository-relative, so moving the data repository changes no classification.


## 11.4 Canonical Data Manifest

`manifests/data_manifest.json` is the single authoritative inventory of the
data repository (`scripts/core/data_manifest.py`, schema
`kritva-forge-data-manifest` version 6: version 2 (KF-DQ-008) added the
Semantic IR v2 references, see §5.2; version 3 (KF-DQ-009) added the
Behavioral Semantics v1 references, see §5.3; version 4 (KF-DQ-010) added the
Structural Analysis v1 references and record structural traceability, see §5.4;
version 5 (KF-DQ-011) added the FSM Analysis references, see §5.5:
`modules[].fsm`, `records[].fsm`, `versions.fsm` / `fsm_identity` /
`fsm_analyzer` and the artifact kinds `fsm` / `fsm_report`. Since KF-DQ-011.1
these reference FSM Analysis v2 (`normalized/fsm/v2`, schema 2, analyzer 2)
without a manifest schema change; version 6 (KF-DQ-012) added the Prompt v2
references, see §5.6: `modules[].prompt_v2` (prompt and sidecar path / sha256,
identity, variant, `derived_from` the four layers), the prompt, tokenizer,
leakage-metric, threshold and classifier versions, `compatibility` (the forge
version contract), `leakage_classification` (group counts and unresolved
near-duplicates) and the artifact kinds `prompt_v2` / `prompt_v2_sidecar` /
`prompt_v2_report` / `prompt_leakage_classification`). It is written as JSON because every
other machine manifest in the repository is JSON, the standard library
serialises it byte-deterministically (`sort_keys`), and it parses much faster
than YAML at about 0.8 MB.

| Section | Content |
|---|---|
| `schema`, `repository`, `generator`, `versions` | Manifest, identity, provenance, leakage, split and artifact schema versions, plus parser and parser version. No timestamp, host, user or PID. |
| `sources` | Every file under `raw/`, with path, sha256, size, status, role and the modules it feeds. |
| `modules` | One entry per canonical module: `module_id` (`mod1:`), primary and contributing sources, IR path / sha256 / `ir_content` (`n1:`) / `module_body` (`m1:`), prompt, RTL copy, dataset records and split. |
| `artifacts` | Every file of the managed tree, with kind, status and sha256. |
| `records` | Every dataset record: `record_id` (`r1:`), split, ip, module, `module_id`, task, prompt variant. |
| `splits` | Reference to the split manifest, schema versions, counts and `split_identity` (`sp1:`, over the sorted record/split pairs). |
| `manifests` | The role of every manifest file. |

Statuses:

- `canonical`: raw RTL and file lists used by canonical modules, plus canonical IR.
- `generated`: current pipeline outputs.
- `historical`: KF-DQ-006 historical paths.
- `deprecated`: reserved; none exist at version 1.
- `excluded`: present but not a dataset input. This covers unreferenced RTL, documentation, `raw/rtl/curated` and `.gitkeep`.

Manifest roles:

- **Authoritative:** only `data_manifest.json`.
- **Components, incorporated by reference:** the KF-DQ-005 provenance manifest, the KF-DQ-006 artifact inventory and the KF-DQ-004 split manifest.
- **Non-canonical:** `datasets/pipeline/manifest.json` (change tracking).
- **Historical:** the KF-DQ-001 migration manifests.

The manifest is derived from canonical inputs, KF-DQ-005 provenance and the KF-DQ-006 classification. It never reads a previous manifest. Generation is refused while any artifact is STALE, ORPHAN or UNMANAGED, so stale data cannot be listed and removed artifacts disappear. Identity rules are not duplicated: the manifest reuses the KF-DQ-003/004/005 identities, and a source's identity is its content sha256. The manifest does not hash itself. Ordering: sources and artifacts by path, modules by (ip, module), records by `record_id`, keys sorted.

`make check-manifest` (`data_manifest.py --check`) is the **dataset publication gate**. It recomputes the manifest and fails on any of these:

- a missing manifest
- an unsupported schema or component version
- an absolute, `..` or non-POSIX path
- a duplicate path or identity
- an invalid identity format
- a missing artifact or a hash mismatch
- a broken source, IR, prompt or record reference
- a historical/active misclassification
- a missing or unexpected entry
- an invalid KF-DQ-006 inventory
- a stored manifest that differs from the recomputation

The pipeline writes and validates the manifest after the provenance and stale-artifact gates, writes `analysis/reports/data_manifest_report.json`, and fails on any problem. `KRITVA_FORGE_ALLOW_STALE` does not bypass this gate. `make data-quality` runs every gate.

## 11.5 Open-Source Reference Corpus (KF-DQ-012.2)

**Purpose.** A public regression corpus. The full pipeline and every gate
run on it without the private `kritva-forge-data` repository. It
complements the private corpus and never replaces it: the private corpus
stays authoritative for datasets, splits and published baselines.

**Sources.** Five Apache-2.0 repositories, pinned as git submodules under
`reference/sources/`:

| IP | Submodule | Pinned commit |
|---|---|---|
| `aes` | `security_core` | `de2e6a5` |
| `fpu` | `fpu` | `621d988` |
| `qspi` | `qspim` | `55f8c43` |
| `rtc` | `rtc` | `48943b7` |
| `ycr1` | `ycr1cr` | `a6c76b3` |

`ycr1` is not the private ycr2-based `yifive` IP. The public heads differ
from the private corpus files, so the reference corpus has its own
baseline.

**Contract.** `reference/corpus.yaml` (schema
`kritva-forge-reference-corpus` v1) is the single source of truth. Per IP
it declares:
- the submodule, repository, pinned commit and license;
- the source root and an explicit file list (no discovery);
- include directories;
- exclusions, each with a reason;
- documented external includes.

**Materialization.** `scripts/reference/materialize.py` builds
`build/reference/kritva-forge-data` (gitignored) from scratch. It writes
byte-identical copies and one `files.f` per IP.
- It refuses missing, dirty or wrongly-pinned submodules, missing listed
  files, and any unresolved `` `include `` that is not documented.
  Example: `qspim_top.sv` is excluded because it needs the parent SoC's
  `user_reg_map.svh`; no RTL is ever fabricated.
- It never writes into `reference/sources`.
- The evidence manifest `reference_manifest.json` and the marker
  `CORPUS_KIND` (`reference`) go beside the data root, not inside it,
  because the KF-DQ-006 stale gate treats other root files as UNMANAGED.

**Regression.** `make reference-regression` runs the production entry
points with `DATA_ROOT` set to the reference root:
1. `reference-data`;
2. `pipeline`;
3. `data-quality`;
4. compares `scripts/reference/summary.py` output with the committed
   `reference/expected/summary.json`, and fails on any differing key;
5. proves every submodule is unchanged.

The summary holds:
- per-layer counts and tree hashes;
- gate statuses;
- Prompt v2 size and leakage statistics;
- classification counts and split identity.

Corpus tests that assert private-corpus constants skip on a root marked
`CORPUS_KIND=reference`. `make reference-baseline` is the only way to
change the baseline, and the change is reviewed in a PR. The reference
regression is not yet part of the Kritva Forge Gate.

**Baseline (pinned commits).** 5 IPs and 149 modules (aes 25, fpu 15,
qspi 15, rtc 13, ycr1 81); all gates PASS. Prompt v2:
- maximum size 32 742 B, 3 truncated;
- leakage max overlap 0.077, longest run 5;
- no cross-split groups.

The corpus exposed the Prompt v2 select-name defect fixed in KF-DQ-012.1.

---

## 11.6 Multi-Task Dataset (KF-DQ-013)

`datasets/multitask/v2/` is a versioned hardware-intelligence dataset with these properties:
- schema `kritva-forge-dataset` v2;
- the task type of every record is explicit and versioned;
- every target is a deterministic view of persisted canonical artifacts (`scripts/multitask/`);
- no new analysis semantics, no RTL fact parsing, no LLM.

It coexists with the legacy Prompt v1 `rtl_generation` dataset `datasets/pipeline/`, which stays unchanged.

**Task registry** (`scripts/multitask/registry.py`, `task_registry` v1; serialized to
`datasets/multitask/v2/registry.json` with record counts). Each task has its own version. A
*populated* task has exactly one record per canonical module; a *declared* task has none.

| Task | Version | Status | Input | Target |
|---|---|---|---|---|
| `rtl_generation` | 2 | populated | Prompt v2 text | module RTL (`rtl-slice-v1`) |
| `rtl_understanding` | 1 | populated | module RTL | Prompt v2 text |
| `interface_extraction` | 1 | populated | module RTL | `interface-v1` JSON |
| `structural_extraction` | 1 | populated | module RTL | `structural-v1` JSON |
| `dependency_analysis` | 1 | populated | module RTL | `dependency-v1` JSON |
| `fsm_extraction` | 1 | populated | module RTL | `fsm-v1` JSON |
| `rtl_explanation`, `assertion_generation`, `rtl_repair`, `rtl_optimization` | 1 | declared | — | — |

`rtl_explanation` is not an alias of Prompt v2; its human-oriented target contract comes later.
A change of a task's input, target, projection or population rule needs a new task version; a
change of the task set needs a new registry version (the tests pin the registry digest).

**Record** — the fields are exact, including nested fields:
- `schema`;
- `record_id`: `r2:` + 16 hex of SHA-256 over `kf-record␟v2␟task␟version␟module_id`;
- `task {id, version}`;
- `module {ip, name, module_id}`;
- `split`;
- `input {kind, text}`;
- `target {kind, text}` or `{kind: json, projection, value}`;
- `sources [{layer, path, sha256}]`;
- flat `versions`.

Each split file is canonical JSON lines sorted by (task, ip, module). Records contain no ids,
locations, absolute paths or timestamps.

**`rtl-slice-v1`** is the canonical source text of exactly one module declaration, from
`module` / `macromodule` *name* to the next `endmodule`:
- it is located lexically, with comments and strings masked for matching only;
- the only normalization is CRLF / CR → LF;
- zero or several matches fail.

It is syntactic slicing, not analysis. Other modules of the same file are never included.

**Projections.** Each projection emits only its allowed fields, with names resolved through the
Prompt v2 name map:

| Projection | Layer | Content |
|---|---|---|
| `interface-v1` | Semantic IR v2 | `{module, parameters, ports}`. Module-scope `parameter`s and ports in declaration order. Dimension and default text are canonical Semantic IR text, the only HDL text a projection permits. |
| `structural-v1` | Structural v1 | `{module, registers, instances, processes, counts}`. Registers carry clock, enable signals and hold. Instances carry connections. Processes are counted per boundary. |
| `dependency-v1` | Structural v1 | `{module, targets}` for every output port and register (role `output` / `register` / `registered_output`). `inputs` = module inputs in the persisted fan-in cone; `registers` = registers in it; `sources` = direct dependencies with their persisted kind. Nothing is recomputed. |
| `fsm-v1` | FSM v2 | `{module, fsms}`: confirmed and candidate FSMs, each with state register, width, status, quality, style, encoding, clock, reset, states, non-reset transitions (natural-language condition from the Prompt v2 guard renderer, persisted kind / status) and outputs. Actions, couplings, evidence, reachability and unknowns are excluded (later `fsm-v2`). `fsms: []` is a valid negative example; a missing FSM document fails. |

Empty collections are explicit. Nulls and unresolved names are counted in the report.

**Split and leakage boundary.** Every record inherits its module's split from the split schema v2
manifest; the split algorithm is never re-run on task records. All tasks of a module are therefore
in one split, and the gate fails a module whose records span splits or differ from the manifest.

Similarity between a record's own input and target is intended and not gated; split isolation of
the module is the leakage boundary.

The build and the gate require all of the following for the current data:
- split schema 2;
- a passing split gate;
- the entry authorization `kf_dq_013_entry` = `open`, recomputed from the classification.

The authorization is not a task lifecycle state. The two retained `structural_similarity` groups
were reviewed (KF-DQ-012 AC-500) and stay soft; the dataset report lists them.

**Gate** (`make check-multitask`, part of `data-quality`; run by the pipeline after the final
Prompt v2 gate). It checks:
- schema, registry, keys and canonical form;
- record identity and order;
- per-task coverage;
- split inheritance;
- task provenance consistency (sources, projections, versions);
- id / location / path leaks;
- byte equality with a re-derivation from the persisted artifacts.

It writes `analysis/reports/dataset_v2_report.json`, which holds:
- counts and size statistics;
- FSM positives / negatives;
- nulls;
- soft findings;
- the dataset identity `ds2:`.

**Hardening (KF-DQ-013.1).**
- *Typed record contract.* `validator.record_type_problems` is the single definition of a record's
  typed shape. It is shared by the gate, the stale gate and the data-manifest summary:
  - exact key sets at every level;
  - strings / integers, with booleans never accepted as integers;
  - `target` either `json` {`projection`, `value` object} or a text kind (`rtl` / `prompt_v2`) {`text`};
  - `sources` a list of {`layer`, `path`, `sha256`} strings;
  - `versions` a flat object of strings / integers.
  The gate checks the shape before dereferencing anything. A malformed record is reported as
  `<file>:<line>: <field path>: …` and is then skipped for every later check.
- *Never-raise contract.* `check()` returns a report for every input:
  - invalid JSON, non-object values and non-UTF-8 files FAIL with a diagnostic;
  - the dataset identity is `null` unless every line is well typed;
  - an unexpected internal error becomes the separate generic problem
    `internal validation error: <type>`, with details on stderr only.
  The stale gate marks a malformed split file `STALE` ("malformed multi-task record"). The manifest
  summary gets a `null` identity, so the manifest check fails and the manifest build refuses.
- *Recoverable publication.* `build.write()` publishes the dataset in five steps:
  1. it stages the complete generation in `datasets/multitask/.v2.tmp-<pid>/`;
  2. it re-reads and verifies the staged generation (bytes, typed shape, registry);
  3. it renames `v2` to `.v2.old-<pid>`;
  4. it renames the staging directory to `v2`;
  5. it removes `.v2.old-<pid>`.

  Files are never replaced one by one. The only window in which `v2` is absent is between steps 3
  and 4, when the previous generation is held in `.v2.old-<pid>`.

  Every `write()` begins with recovery:
  - it removes leftover `.v2.tmp-*` directories;
  - if `v2` is absent, it restores a single `.v2.old-*`;
  - if `v2` exists, it removes leftover `.v2.old-*` directories. An existing `v2` is always a
    verified generation.
  - It stops with a `BuildError` when `v2` is absent and several old generations exist, or when
    the restore fails. In both cases it keeps every candidate.

  A failure in step 5 leaves the new `v2` published, with a warning. Until removed, leftovers are
  reported by the gate (unmanaged) and by the stale gate (`UNMANAGED`). The guarantee is
  recoverable publication, not crash-atomic transactionality.
- *CI coverage.* CI runs the public tests and the header check only. The private-corpus
  `data-quality` and the reference regression run on the device, and the task apply scripts
  record them as evidence.

**Integration.**
- The Prompt v2 consumption gate allows Prompt v2 only in `datasets/multitask/v2` `rtl_generation`
  v2 / `rtl_understanding` v1 records, while the entry authorization holds. A legacy record that
  consumes Prompt v2 fails.
- The stale gate manages `multitask_split` / `multitask_registry` / `dataset_v2_report`.
- Manifest v7 adds the `multitask` section.
- `REQUIRED` adds `dataset_schema` 2 / `task_registry` 1 as manifest-only keys. `CONTRACT_VERSION`
  stays 1 because the mechanism is unchanged.
- The reference regression adds the `datasets/multitask/v2` layer and counts (894 records:
  684 / 114 / 96).

# 12. Private Data Repository

The public repository should not contain private RTL.

The data repository is:

```text
KritvaOS/kritva-forge-data
```

and is mounted into the public project as:

```text
kritva-forge/data/
```

Conceptually:

```text
                 kritva-forge
                      │
                      │ submodule
                      ▼
              kritva-forge-data
                      │
       ┌──────────────┼──────────────┐
       ▼              ▼              ▼
      RTL          Datasets        Golden
    Corpus                         Data
```

This separation allows the public software to be released under an open-source license without exposing restricted data.

---

# 13. RTL AI / LLM Layer

The RTL AI layer consumes the structured information generated by Forge.

Potential tasks include:

```text
RTL understanding
RTL explanation
RTL summarization
RTL generation
RTL completion
RTL transformation
FSM reasoning
Verification generation
Assertion generation
Bug detection
Design knowledge extraction
```

The LLM should not be tightly coupled to the parser.

Instead:

```text
RTL → IR → Knowledge → Dataset → Model
```

This makes it possible to change the model without redesigning the RTL analysis infrastructure.

---

# 14. Pipeline Orchestration

The main pipeline should orchestrate the individual subsystems.

```text
run_pipeline.py
      │
      ├── Parser
      │
      ├── RTL IR
      │
      ├── Semantic
      │
      ├── Structural
      │
      ├── FSM
      │
      ├── Graph
      │
      ├── Quality
      │
      └── Dataset
```

Each subsystem should remain independently testable.

---

# 15. Testing Architecture

Testing should be organized by architectural layer.

```text
tests/
├── parser/
├── rtl_ir/
├── semantic/
├── structural/
└── fsm/
```

Tests should cover both:

### Unit tests

Individual functions and components.

### Integration tests

Complete RTL → IR → analysis flows.

### Regression tests

Known RTL examples whose expected results are maintained.

### Golden tests

Expected normalized IR/FSM representations for selected RTL designs.

---

# 16. Versioning

The RTL IR should have an explicit schema version.

For example:

```yaml
schema_version: "1.0"
parser_version: "0.1"
```

This is important because datasets generated from one version of the IR should remain identifiable when the parser evolves.

Node identity is versioned separately as `identity_version`
(see section 5.1).

---

# 17. Architectural Boundaries

The following boundaries should be maintained:

```text
Parser
  ↓
RTL IR
  ↓
Semantic
  ↓
Structural
  ↓
FSM / Graph / Analysis
  ↓
Dataset
  ↓
AI / LLM
```

Avoid:

```text
FSM → directly parse RTL
Dataset → directly inspect AST
LLM → directly depend on parser internals
Reports → modify analysis state
```

This separation keeps the architecture maintainable.

---

# 18. Future Expansion

The architecture is intentionally designed to support future hardware intelligence capabilities.

Potential future components include:

```text
Timing Intelligence
        │
PPA Intelligence
        │
Verification Intelligence
        │
DFT Intelligence
        │
Formal Intelligence
        │
Architecture Understanding
        │
RTL Generation
        │
       ...
        │
        ▼
Kritva Forge
```

The goal is for Kritva Forge to evolve from an RTL analysis framework into a broader **hardware design intelligence platform**.

---

# 19. Relationship with Kritva Nexus and Kritva Edge

Kritva Forge is architecturally independent from the Kritva hardware platforms.

```text
                 Kritva
                    │
       ┌────────────┼─────────────┐
       │            │             │
       ▼            ▼             ▼
   Nexus           Edge         Forge
   SoC             SoC        Design AI
       │            │             │
       │            │             │
       └────────────┼─────────────┘
                    │
             Hardware Ecosystem
```

Kritva Forge may eventually consume and analyze RTL from Kritva Nexus and Kritva Edge.

However:

> **Kritva Forge is not the RTL repository for Kritva Nexus or Kritva Edge.**

This distinction should remain explicit.

---

# 20. Long-Term Vision

Kritva Forge aims to build a machine-readable representation of hardware design intent.

The long-term flow is:

```text
                    Human Design
                         │
                         ▼
                        RTL
                         │
                         ▼
                  ┌──────────────┐
                  │Kritva Forge  │
                  └──────┬───────┘
                         │
              Hardware Design Knowledge
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
      Analysis       Verification      AI
          │              │              │
          └──────────────┼──────────────┘
                         ▼
                Better Hardware Design
```

The fundamental objective is:

> **Turn RTL from source code into structured, queryable, machine-understandable hardware design knowledge.**

