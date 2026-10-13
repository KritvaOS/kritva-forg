# Kritva Forge Data

The public `kritva-forge` repository is code-only. Runtime RTL, normalized IR,
analysis artifacts, golden data, prompts, and datasets belong in the private
`KritvaOS/kritva-forge-data` repository.

Expected local layout:

```text
~/workarea/kritvaos/
├── kritva-forge/
└── kritva-forge-data/
    ├── raw/rtl/
    ├── normalized/ir/
    ├── analysis/
    ├── golden/
    ├── generated/
    ├── datasets/
    ├── splits/
    └── manifests/
```

The Makefile defaults to the sibling data repository:

```bash
make pipeline
```

Override it when needed:

```bash
make pipeline DATA_ROOT=/path/to/kritva-forge-data
```

Splits use split schema v2 (KF-DQ-013.0). Records are grouped by hard
content identities, cross-IP `body_shape` copies and `rtl-sim-v1`
near-duplicate pairs (similarity ≥ 0.70 among modules that share a structural
or FSM fingerprint). No near-duplicate pair spans two splits; see
`docs/architecture/architecture.md` §11.1. A data repository written with split
schema 1 must be regenerated with `make pipeline`.

The public repository must not contain proprietary RTL, private datasets,
generated golden corpora, model checkpoints, or restricted training data.

## Multi-task dataset (KF-DQ-013)

`datasets/multitask/v2/` holds the multi-task hardware-intelligence dataset:
- schema `kritva-forge-dataset` v2;
- task registry `registry.json`;
- one `train` / `validation` / `test` JSON-lines file each.

It has six populated tasks: `rtl_generation` v2 (Prompt v2 → RTL), `rtl_understanding`,
`interface_extraction`, `structural_extraction`, `dependency_analysis` and `fsm_extraction`. Four more
tasks are declared without records. Every record keeps its module's split.

It is rebuilt by `make pipeline`, or by `make multitask` on an existing data root, and validated by
`make check-multitask`. The legacy `datasets/pipeline/` (Prompt v1 `rtl_generation`) is unchanged and
coexists with it. See `docs/architecture/architecture.md` §11.6.

The dataset is published as a whole directory: the new generation is staged and verified, then
swapped in. If a build is interrupted, `datasets/multitask/` can hold a hidden `.v2.tmp-<pid>/` or
`.v2.old-<pid>/` directory. `make check-multitask` reports it as unmanaged. The next
`make multitask` or `make pipeline` recovers automatically:
- it deletes the staging directory;
- it restores `.v2.old-<pid>` if `v2` is missing.

If `v2` is missing and several `.v2.old-*` directories exist, the build stops. In that case, rename
the intended one to `v2` by hand.

## Open-source reference corpus (KF-DQ-012.2)

For a local regression without the private repository, `reference/` pins five
open-source Apache-2.0 RTL repositories as git submodules. It materializes
them into the same layout under `build/reference/kritva-forge-data`:

```bash
make reference-init
make reference-regression
```

It is a separate corpus and does not replace `kritva-forge-data`. See
`reference/README.md`.

## Canonical normalized IR layout (KF-DQ-001)

```text
normalized/ir/<ip>/
├── hierarchy.yaml
├── summary.yaml
├── modules/
│   └── <module>.yaml      # the only canonical module representation
└── rtl/                   # RTL copies; location to be revisited in KF-DQ-006
```

Consumers must enumerate module IR with
`scripts.core.paths.iter_module_yamls()`; any other `*.yaml` directly under
`<ip>/` is non-canonical. Check a data checkout with:

```bash
make check-layout
make test-data
```

