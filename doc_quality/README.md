# doc_quality

Documentation Quality Evaluation and Targeted Maintenance for MapCoDoc.

`doc_quality` is a self-contained extension to the MapCoDoc traceability
pipeline. It evaluates the *structured* documentation
(`DBMember.doc_format == 'structured'`) of every public API member captured
by MapCoDoc and produces:

1. A per-member `EvaluationReport` with scores across four quality
   dimensions and a list of pinpointed `Issue` records.
2. A library-wide aggregate report (JSON, CSV, or HTML).
3. A `MaintenanceCandidate` per member with proposed surgical patches
   that fix issues whose corrective values can be sourced deterministically
   from the database or AST.
4. An auditable artifact trail (originals, evaluations, candidates,
   applied versions, append-only log) that supports rollback.

The package operates exclusively on structured documentation. Raw-format
docs are surfaced as "skipped" but not evaluated; run them through the
existing structuring step in `doc_processor/` first.

---

## Quality Dimensions

| Dimension | What it measures | Strategy |
|---|---|---|
| **Completeness** | Are all required schema fields populated and informative? | Rule-based |
| **Accuracy** | Do documented signatures, parameters, types, and defaults match the code? | Rule-based |
| **Readability** | FK/Coleman-Liau/Gunning Fog text indices; Scalabrino-adapted code-example metrics | Rule-based |
| **Maintainability** | Use of cross-references and hyperlinks vs inline restatement | Rule-based |

LLM-driven metrics (semantic accuracy, prose rewrites, generated descriptions)
are designed-in but deferred to v1.x; the strategy enum and patcher seam
are already wired.

---

## Installation

The package depends on three pure-Python libraries:

```bash
pip install -r doc_quality/requirements.txt
```

All three are treated as optional - the package imports them lazily and
silently skips dependent metrics when they are missing. The minimum
runtime requirement is just MapCoDoc's existing dependency set.

---

## Quick Start

### Evaluate a Library

```bash
python -m doc_quality evaluate \
    --db-path mapcodoc_output/sklearn_1.8.0.db \
    --library sklearn \
    --version 1.8.0 \
    --module-prefix sklearn.linear_model
```

The evaluator writes one JSON file per member to:

```
doc_quality/artifacts/sklearn/1.8.0/
├── original/<api_name>.json      # snapshot at evaluation time
├── evaluation/<api_name>.json    # full EvaluationReport
└── log.jsonl                      # append-only operation log
```

### Build Maintenance Candidates

```bash
python -m doc_quality maintain \
    --db-path mapcodoc_output/sklearn_1.8.0.db \
    --library sklearn \
    --version 1.8.0 \
    --module-prefix sklearn.linear_model \
    --strategies db_query,ast_derived \
    --min-severity medium
```

Candidates with proposed patches are written to
`doc_quality/artifacts/sklearn/1.8.0/maintained/`. **No DB writes occur at
this stage.**

### Review and Apply

```bash
# Inspect the unified diff between original and candidate.
python -m doc_quality diff \
    --library sklearn --version 1.8.0 \
    --api-name sklearn.linear_model.LogisticRegression

# Apply an approved candidate to the database.
python -m doc_quality apply \
    --db-path mapcodoc_output/sklearn_1.8.0.db \
    --library sklearn --version 1.8.0 \
    --api-name sklearn.linear_model.LogisticRegression
```

If you change your mind:

```bash
python -m doc_quality rollback \
    --db-path mapcodoc_output/sklearn_1.8.0.db \
    --library sklearn --version 1.8.0 \
    --api-name sklearn.linear_model.LogisticRegression
```

### Generate a Report

```bash
python -m doc_quality report \
    --library sklearn --version 1.8.0 \
    --format html \
    --output-file reports/sklearn_quality.html
```

---

## Programmatic Use

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from doc_quality import DocQualityEvaluator
from doc_quality.artifacts.store import ArtifactStore
from doc_quality.config import EvaluatorConfig
from mapcodoc_db.query import QueryManager

# Open the DB.
engine = create_engine("sqlite:///mapcodoc_output/sklearn_1.8.0.db")
session = sessionmaker(bind=engine)()
qm = QueryManager(session)

# Configure and run the evaluator.
config = EvaluatorConfig()  # or EvaluatorConfig.from_yaml(Path("..."))
store = ArtifactStore(Path("doc_quality/artifacts"), "sklearn", "1.8.0")
evaluator = DocQualityEvaluator(qm, config=config, artifact_store=store)

reports = evaluator.evaluate_library("sklearn.linear_model")
for r in reports:
    print(f"{r.member_api_name:<60s} {r.overall_score:.2f}")
```

---

## Architecture

```
doc_quality/
├── models.py                  # Issue, EvaluationReport, MaintenancePatch
├── issue_types.py             # Closed enum: every issue the evaluator can emit
├── presence.py                # is_present() predicate (N/A handling)
├── doc_views.py               # ClassDocView vs CallableDocView
├── type_normalizer.py         # Type-string canonicalization
├── class_member_lister.py     # Enumerate class methods/attributes
├── code_truth_resolver.py     # Direct vs inherited code-truth dispatch
├── config.py                  # EvaluatorConfig (thresholds + weights)
├── evaluator/
│   ├── completeness.py        # COMP_*  rule-based checks
│   ├── accuracy.py            # ACC_*   rule-based checks
│   ├── readability_text.py    # FK / Coleman-Liau / Gunning Fog
│   ├── readability_code.py    # Scalabrino-adapted example metrics
│   ├── readability.py         # text & code aggregator
│   ├── maintainability.py     # MAINT_* hyperlink/cross-ref checks
│   └── evaluator.py           # DocQualityEvaluator orchestrator
├── maintainer/
│   ├── db_patcher.py          # DB_QUERY strategy
│   ├── ast_patcher.py         # AST_DERIVED strategy
│   ├── llm_patcher.py         # LLM strategy stub (v1.x)
│   ├── patch_applicator.py    # JSONPath patch application
│   ├── approval.py            # Manual approval + DB writeback
│   └── maintainer.py          # DocQualityMaintainer orchestrator
├── artifacts/store.py         # Filesystem artifact store
├── reporting/                 # Aggregator and JSON/CSV/HTML formatters
├── cli/main.py                # argparse subcommands
└── tests/                     # 92 unit tests
```

The evaluator and maintainer share a single contract: `IssueType`. Every
issue ever emitted is a value of that enum, and the maintainer's strategy
dispatch tables are keyed off the same enum. Adding a new check involves
adding an `IssueType`, an evaluator emission site, and (optionally) a
patcher entry.

---

## Configuration

Most thresholds and weights are in `config.py`. They can be overridden
via a YAML file:

```yaml
# my_config.yaml
type_fuzzy_threshold: 0.85
fk_grade_high: 16.0
weights_overall:
  completeness: 0.45
  accuracy: 0.45
  readability: 0.05
  maintainability: 0.05
enabled_strategies:
  - db_query
  - ast_derived
min_severity_for_maintenance: high
```

```bash
python -m doc_quality evaluate \
    --db-path ... --library ... --version ... \
    --config my_config.yaml
```

---

## Testing

```bash
python -m pytest doc_quality/tests/ -o addopts=""
```

The `-o addopts=""` clears project-level pytest options that may not be
relevant to this subpackage. The suite is fully self-contained: no DB
fixture is required, and each module is exercised through hand-built
input fixtures rather than mocks.

---

## Limitations and Future Work

* **Up-to-dateness as a separate dimension** is intentionally folded into
  Completeness and Accuracy in v1 because the DB does not track multiple
  versions. A future extension could integrate `pydriller` for git-history
  staleness detection.
* **External inherited members** (those whose original definition is not
  in the analyzed codebase) are skipped. Their structured docs exist but
  no code-side ground truth is reachable.
* **LLM strategy is a stub.** The v1.x extension would re-use the OpenAI
  client wired in `doc_processor/structured_doc_extracter.py`. Targeted
  prompts (one issue, one field) keep the design tractable; see
  `maintainer/llm_patcher.py` for the seam.
* **Per-library doc-base URL.** The `MAINT_TYPE_NOT_LINKED` patch can
  resolve internal cross-references in v1.x once a per-library doc URL
  template is added to `EvaluatorConfig`.
* **Calibration set.** The current weights and thresholds are reasonable
  defaults; tuning against a manually-labelled set of ~50 stratified
  members per library is recommended before treating the absolute scores
  as authoritative.
