# Portable design workflow

The supported design workflow uses an inspectable **CAID design document v1**:
create source → inspect → edit named dimensions → rebuild → export → reopen.
The document contains the existing CAID design artifact v1, explicit operation
bindings, revision ancestry and a SHA-256 of the complete source. JSON is the
editable source; STEP/STL are derived deliverables. Nothing in this workflow
executes model-authored Python from a saved document or requires an account,
model provider, HTTP server or web interface.

## Create and reuse source through Python

```python
from opencad import (
    DesignDocument, Part, RuntimeContext, edit_design_document,
    export_design_document, load_design_document,
)
from opencad.design_artifact import build_design_artifact
from opencad.kernel.core.backend_factory import create_backend

context = RuntimeContext(backend=create_backend("occt"))
plate = Part(context=context).box(20, 10, 4)
artifact = build_design_artifact(
    artifact_id="plate",
    feature_tree=context.tree,
    parameters={"length": {"value": 20, "unit": "mm", "feature_id": plate.feature_id}},
)
document = DesignDocument.create(
    artifact=artifact,
    bindings={"length": {"feature_id": plate.feature_id, "field": "length"}},
)
document.write_json("plate.design.json")

# This can run later in a new process; native geometry is rebuilt from source.
saved = load_design_document("plate.design.json")
revised = edit_design_document(saved, {"length": 30}, expected_sha256=saved.content_sha256)
revised.write_json("plate-r2.design.json")
receipt = export_design_document(revised, "plate-r2.step", feature_id=plate.feature_id)
print(receipt)
```

`artifact_id` identifies a design. `revision` and `parent_sha256` describe its
local ancestry. `content_sha256` covers its exact source and revision, excluding
only the checksum itself and the review receipt. Saving and loading unchanged
source preserves the hash. Timestamps and saved kernel cache metadata inside
the artifact are part of that exact source. This hash is an integrity check,
not a signature or a claim of globally serialized revision history.

Every public persistence, edit, reference and execution operation revalidates
its source hash, including mutations made to nested Python models. Document writes publish complete new files atomically and fail if the output
already exists. The CLI always uses this immutable-file behavior. The SDK can
explicitly request `write_json(path, overwrite=True)` for atomic replacement of
a general snapshot, which does not provide a concurrent compare-and-swap guard.
Native exports replace their destination only after compilation and export
succeed. Separate output paths may fork; this local file interface is not a
shared database transaction service.

## Deterministic editing and explicit reference limits

A named parameter changes geometry only when it has one explicit operation
binding. Binding values must initially agree with their operation values.
Supported scalar bindings are existing primitive dimensions, extrusion distance,
fillet/chamfer/shell/offset dimensions in **mm**, and draft/revolve angles in
**deg**. No implicit unit conversion occurs. File paths, geometry IDs, derived
parameters, unrecognized fields and conflicting bindings are rejected. Other
CAID v1 named parameters remain inspectable metadata.

An edit creates a new revision and invalidates the feature and its dependents.
The kernel validates geometry when rebuilding; a structurally valid document
can still fail native compilation. No export is delivered as successful after
an incomplete rebuild.

`document.reference(feature_id)` produces an explicit artifact/revision/hash
pin. `check_design_reference(document, reference)` returns `current`, `stale`
or `missing`. A pin identifies source feature identity, not a persistent BRep
face or a certificate that a body has been built. Every source edit retires the
old pin conservatively. Callers deliberately author new references after
reviewing the revision.

Existing face/edge enumeration IDs do not establish identity across topology
changes. Upstream document edits with dependent saved face/edge selections
fail with an explicit reselection diagnostic. Stable topological naming remains
separate kernel work; this workflow cannot silently rebind a constraint to a
new face with the same index.

This version accepts reconstructible procedural feature trees. Imported STEP
or STL nodes, including inactive branches, require separately packaged and
hash-verified source bytes; external import paths are rejected by the portable
document boundary. Existing CAID v1 files and raw tree loading remain available
for their existing uses. Their format is unchanged.

## Human authority and engineering evidence

Export is available before review so a person can inspect CAD in another tool.
Native rebuild/export proves only the supported compilation checks. It does not
establish load capacity, fit, fabrication readiness, simulation validity or
physical qualification. Native exchange files can include exporter metadata or
timestamps, so repeated rebuilds do not promise byte-identical STEP output.
Each export receipt hashes the actual delivered bytes.

`review_design_document(document, reviewer=..., decision="accepted"|"rejected",
note=...)` records a **caller-declared** decision for the exact source hash.
The CLI requires those values explicitly. It does not authenticate the reviewer
or establish who supplied the declaration; applications must enforce their own
human authorization boundary. A review never changes validation outcomes.
An edited source retains its previous receipt as `stale`, and export receipts
report `unreviewed`, `stale`, `accepted` or `rejected` explicitly. Engineers own
acceptance of evidence and decisions to fabricate or deploy a design.

## Open interfaces

The SDK above and the CLI below call the same document functions. MCP and other
transports should validate their caller, then delegate to these SDK functions;
they should not implement another compiler or infer field bindings from names.

```bash
# Existing artifact JSON, plus {"length":{"feature_id":"feat-0001","field":"length"}}
opencad document create artifact.json --bindings bindings.json --output design.json
opencad document inspect design.json
opencad document schema > design-document.schema.json

# values.json contains {"length":30}; use the inspected source checksum.
opencad document edit design.json --values values.json --expected-sha256 CHECKSUM --output revision-2.json
opencad document export revision-2.json --feature feat-0001 --output plate.step

# Record an explicit declaration after engineering review.
opencad document review revision-2.json --reviewer "Engineering reviewer" --decision accepted --note "Reviewed the source and evidence for the stated use." --output reviewed.json
```

The documented [JSON Schema](schemas/caid-design-document-v1.schema.json) is also
available from `DesignDocument.model_json_schema()` and the installed CLI.
Inspecting a saved reference uses `opencad document inspect design.json
--reference reference.json`; stale/missing references exit 1. Contract, stale-edit
and native export failures also exit 1 with a JSON error on stderr.

## Verification and installation

Install the core with its native extra, for example `pip install "opencad[occt]"`.
For a source checkout use the repository's isolated native setup with
`scripts/occt-constraints.txt`. Native source environments and releases must
install mutually compatible OpenCAD core, agent and backend versions.

The `Portable design workflow` GitHub workflow builds the core wheel and tests
that installed wheel on Linux and Windows. The contract suite covers hash
tampering, unit/field boundaries, immutable edits and review/reference staleness.
The native acceptance creates source, opens it in new CLI processes, edits its
actual operation dimensions, exports STEP, independently imports the solid and
measures its dimensions and volume. A no-skip gate prevents absent native
libraries from appearing as successful coverage. These checks demonstrate the
bounded workflow; their results do not certify physical engineering properties.
