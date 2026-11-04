"""Portable, revisioned design source shared by SDK and command-line clients.

The existing CAID v1 artifact remains the payload. A document adds explicit
operation bindings and integrity/revision guards; it never executes Python from
an artifact, silently refreshes references, or treats a review as certification.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opencad.design_artifact import DesignArtifact
from opencad.tree.graph import descendants, topological_order
from opencad.tree.service import FeatureTreeService

if TYPE_CHECKING:
    from opencad.runtime import RuntimeContext

_SHA = r"^[0-9a-f]{64}$"
# These are the existing kernel's scalar parameter units. New operations must
# declare their units here before document editing can expose their fields.
_BINDABLE_UNITS = {
    "create_box": {"length": "mm", "width": "mm", "height": "mm"},
    "create_cylinder": {"radius": "mm", "height": "mm"},
    "add_cylinder": {"radius": "mm", "height": "mm"},
    "create_sphere": {"radius": "mm"},
    "create_cone": {"radius1": "mm", "radius2": "mm", "height": "mm"},
    "create_torus": {"major_radius": "mm", "minor_radius": "mm"},
    "extrude": {"distance": "mm"},
    "fillet_edges": {"radius": "mm"}, "fillet": {"radius": "mm"},
    "chamfer_edges": {"distance": "mm"}, "shell": {"thickness": "mm"},
    "offset_shape": {"distance": "mm"},
    "draft": {"angle": "deg"}, "revolve": {"angle": "deg"},
}


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class OperationParameterBinding(ContractModel):
    feature_id: str = Field(min_length=1)
    field: str = Field(min_length=1)


class DesignReference(ContractModel):
    artifact_id: str = Field(min_length=1)
    revision: int = Field(ge=1, strict=True)
    content_sha256: str = Field(pattern=_SHA)
    feature_id: str = Field(min_length=1)


class ReviewReceipt(ContractModel):
    """Caller-declared engineering decision; reviewer identity is not authenticated."""
    content_sha256: str = Field(pattern=_SHA)
    reviewer: str = Field(min_length=1)
    decision: Literal["accepted", "rejected"]
    note: str = Field(min_length=1)


class DesignDocument(ContractModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False,
        json_schema_extra={"$id": "https://caid-technologies.com/schemas/caid-design-document-v1.schema.json"})
    format: Literal["caid.design-document"] = "caid.design-document"
    schema_version: Literal[1] = 1
    revision: int = Field(default=1, ge=1, strict=True)
    parent_sha256: str | None = Field(default=None, pattern=_SHA)
    artifact: DesignArtifact
    bindings: dict[str, OperationParameterBinding] = Field(default_factory=dict)
    content_sha256: str = Field(pattern=_SHA)
    review: ReviewReceipt | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_schema_version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("Design document schema_version must be an integer, excluding booleans.")
        return value

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        if not self.artifact.artifact_id.strip():
            raise ValueError("A design artifact requires a non-empty identity.")
        if (self.revision == 1) != (self.parent_sha256 is None):
            raise ValueError("Revision 1 has no parent; later revisions require a parent SHA-256.")
        tree = self.artifact.feature_tree
        if tree.root_id not in tree.nodes:
            raise ValueError("The feature tree root is missing.")
        for nodes in [tree.nodes, *tree.branch_snapshots.values()]:
            root = nodes.get(tree.root_id)
            if root is None or root.operation != "seed" or root.depends_on:
                raise ValueError("Every saved branch requires its dependency-free seed root.")
            if any(key != node.id for key, node in nodes.items()):
                raise ValueError("Feature map keys must match feature identities.")
            topological_order(nodes)
            for node in nodes.values():
                if node.operation in {"import_step", "import_stl"}:
                    raise ValueError("Imported geometry requires a separately packaged, hash-verified source; external file paths are not portable design source.")
        targets: set[tuple[str, str]] = set()
        for name, binding in self.bindings.items():
            parameter = self.artifact.parameters.get(name)
            node = tree.nodes.get(binding.feature_id)
            if parameter is None or node is None:
                raise ValueError(f"Binding '{name}' requires an existing parameter and feature.")
            if parameter.feature_id is not None and parameter.feature_id != binding.feature_id:
                raise ValueError(f"Binding '{name}' conflicts with its parameter's feature identity.")
            field = binding.field
            units = _BINDABLE_UNITS.get(node.operation, {})
            if field not in units or parameter.unit != units[field]:
                raise ValueError(f"Binding '{name}' requires a supported operation field and its declared unit ({units.get(field, 'unsupported')}).")
            if type(parameter.value) not in {int, float}:
                raise ValueError(f"Binding '{name}' requires a numeric value, excluding booleans.")
            if field in {"filepath", "shape_id", "sketch_id"} or field.endswith(("_id", "_ids")):
                raise ValueError(f"Binding '{name}' cannot edit file paths or geometry identities.")
            if field not in node.parameters or not _scalar(node.parameters[field]):
                raise ValueError(f"Binding '{name}' requires an existing scalar operation field.")
            if field in node.typed_parameters or any(b.parameter == field for b in node.parameter_bindings):
                raise ValueError(f"Binding '{name}' conflicts with a typed or derived operation parameter.")
            if not _equal_value(parameter.value, node.parameters[field]):
                raise ValueError(f"Binding '{name}' disagrees with its operation value; units are explicit and no implicit conversion is performed.")
            target = (binding.feature_id, field)
            if target in targets:
                raise ValueError("Two named parameters cannot own the same operation field.")
            targets.add(target)
        calculated = _digest(self.source_payload())
        if self.content_sha256 != calculated:
            raise ValueError("Design document SHA-256 mismatch; the source or revision was changed.")
        return self

    def source_payload(self) -> dict[str, Any]:
        """Canonical exact source, excluding the checksum and review attestation."""
        return self.model_dump(mode="json", exclude={"content_sha256", "review"})

    @classmethod
    def create(cls, *, artifact: DesignArtifact, bindings: dict[str, Any] | None = None) -> Self:
        if bindings is not None and not isinstance(bindings, dict):
            raise ValueError("Operation bindings must be a JSON object keyed by parameter name.")
        artifact = DesignArtifact.model_validate(artifact)
        payload = {"format": "caid.design-document", "schema_version": 1,
                   "revision": 1, "parent_sha256": None,
                   "artifact": artifact.model_dump(mode="json"),
                   "bindings": {key: OperationParameterBinding.model_validate(value).model_dump()
                                for key, value in (bindings or {}).items()}}
        return cls.model_validate({**payload, "content_sha256": _digest(payload)})

    def checked(self) -> Self:
        """Revalidate mutable nested models at every persistence/execution boundary."""
        return type(self).model_validate(self.model_dump(mode="json"))

    def write_json(self, path: str | Path, *, overwrite: bool = False) -> None:
        """Publish a complete immutable revision; replacement requires explicit opt-in."""
        document = self.checked()
        _atomic_write(Path(path), (_canonical(document.model_dump(mode="json")) + "\n").encode("utf-8"),
                      overwrite=overwrite)

    def reference(self, feature_id: str) -> DesignReference:
        document = self.checked()
        if feature_id not in document.artifact.feature_tree.nodes:
            raise ValueError(f"Unknown feature '{feature_id}'.")
        return DesignReference(artifact_id=document.artifact.artifact_id, revision=document.revision,
                               content_sha256=document.content_sha256, feature_id=feature_id)

    @property
    def review_status(self) -> Literal["unreviewed", "stale", "accepted", "rejected"]:
        if self.review is None:
            return "unreviewed"
        if self.review.content_sha256 != self.content_sha256:
            return "stale"
        return self.review.decision


def _has_subshape_selection(node) -> bool:
    selection_fields = {"edge_ids", "edge_selection", "face_ids"}
    parameters = {**node.parameters,
                  **{key: value.value for key, value in node.typed_parameters.items()}}
    return (any(parameters.get(key) for key in selection_fields)
            or any(binding.parameter in selection_fields for binding in node.parameter_bindings))


def _scalar(value: Any) -> bool:
    return type(value) in {bool, int, float, str}


def _equal_value(left: Any, right: Any) -> bool:
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    return left == right


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            temporary.replace(path)
        else:
            # Atomic create-if-absent on the same filesystem. No check-then-write
            # race and no partially written immutable revision can be observed.
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_design_document(path: str | Path) -> DesignDocument:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("content_sha256"):
        raise ValueError("A saved design document requires its content SHA-256.")
    # Check raw bytes' parsed source before typed parsing can discard fields
    # unknown to the legacy v1 artifact model.
    source = {key: value for key, value in payload.items() if key not in {"content_sha256", "review"}}
    if _digest(source) != payload["content_sha256"]:
        raise ValueError("Design document SHA-256 mismatch; the source or revision was changed.")
    return DesignDocument.model_validate(payload)


def edit_design_document(document: DesignDocument, values: dict[str, Any], *, expected_sha256: str) -> DesignDocument:
    document = document.checked()
    if expected_sha256 != document.content_sha256:
        raise ValueError("Stale edit: expected SHA-256 does not match the current design document.")
    if not isinstance(values, dict) or not values:
        raise ValueError("An edit requires at least one bound parameter.")
    artifact = document.artifact.model_copy(deep=True)
    for name, value in values.items():
        binding = document.bindings.get(name)
        if binding is None:
            raise ValueError(f"Parameter '{name}' has no explicit operation binding.")
        # Saved face/edge enumeration IDs cannot be certified across an edit.
        # Fail before changing source instead of silently selecting new geometry.
        affected = descendants(artifact.feature_tree.nodes, binding.feature_id)
        invalidated = [feature_id for feature_id in affected
                       if _has_subshape_selection(artifact.feature_tree.nodes[feature_id])]
        if invalidated:
            raise ValueError("Upstream edit requires explicit reselection of face/edge references in: " + ", ".join(sorted(invalidated)))
        if type(value) not in {int, float}:
            raise ValueError(f"Parameter '{name}' requires a compatible scalar value.")
        artifact.parameters[name].value = value
        artifact.feature_tree = FeatureTreeService.edit_feature(
            artifact.feature_tree, binding.feature_id, {binding.field: value})
    payload = document.source_payload()
    payload.update(revision=document.revision + 1, parent_sha256=document.content_sha256,
                   artifact=artifact.model_dump(mode="json"))
    return DesignDocument.model_validate({**payload, "content_sha256": _digest(payload),
                                         "review": document.review})


def check_design_reference(document: DesignDocument, reference: DesignReference | dict[str, Any]) -> Literal["current", "stale", "missing"]:
    document = document.checked()
    reference = DesignReference.model_validate(reference)
    if reference.artifact_id != document.artifact.artifact_id or reference.feature_id not in document.artifact.feature_tree.nodes:
        return "missing"
    if reference.revision != document.revision or reference.content_sha256 != document.content_sha256:
        return "stale"
    return "current"


def review_design_document(document: DesignDocument, *, reviewer: str, decision: Literal["accepted", "rejected"], note: str) -> DesignDocument:
    document = document.checked()
    receipt = ReviewReceipt(content_sha256=document.content_sha256, reviewer=reviewer, decision=decision, note=note)
    return document.model_copy(update={"review": receipt})


def rebuild_design_document(document: DesignDocument, *, backend: str = "occt") -> RuntimeContext:
    """Rebuild checked source in a fresh runtime, never trusting saved built flags."""
    from opencad.kernel.core.backend_factory import create_backend
    from opencad.runtime import RuntimeContext

    document = document.checked()
    context = RuntimeContext(backend=create_backend(backend, require_native=backend == "occt"))
    context.adopt_tree(document.artifact.feature_tree.model_copy(deep=True))
    tree = context.rebuild_tree()
    invalid = [node.id for node in tree.nodes.values()
               if not node.suppressed and node.operation != "seed" and node.status != "built"]
    if invalid:
        raise ValueError("Design rebuild failed or left unresolved features: " + ", ".join(invalid))
    return context


def export_design_document(document: DesignDocument, path: str | Path, *, feature_id: str) -> dict[str, Any]:
    """Atomically export the explicitly selected feature using native OCCT.

    Export is allowed before human review so engineers can inspect it elsewhere.
    The receipt reports the review state and does not claim physical qualification.
    """
    document = document.checked()
    destination = Path(path)
    extension = destination.suffix.lower()
    if extension not in {".step", ".stp", ".stl"}:
        raise ValueError("Use a .step, .stp, or .stl export destination.")
    node = document.artifact.feature_tree.nodes.get(feature_id)
    if node is None or node.operation == "seed" or node.suppressed:
        raise ValueError("Export requires an existing, active geometry feature.")
    context = rebuild_design_document(document)
    shape_id = context.tree.nodes[feature_id].shape_id
    if not shape_id:
        raise ValueError("Selected feature has no rebuilt geometry.")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=extension, delete=False) as stream:
            temporary = Path(stream.name)
        if extension == ".stl":
            context.export_stl(shape_id, str(temporary))
        else:
            context.export_step(shape_id, str(temporary))
        data = temporary.read_bytes()
        if not data:
            raise ValueError("Native CAD export produced an empty file.")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"artifact_id": document.artifact.artifact_id, "revision": document.revision,
            "content_sha256": document.content_sha256, "feature_id": feature_id,
            "export_sha256": hashlib.sha256(data).hexdigest(), "format": extension.lstrip("."),
            "review_status": document.review_status,
            "limitations": ["Native export is not evidence of physical qualification or manufacturing suitability."]}
