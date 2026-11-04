"""Portable contract checks exercise stale/misleading source, not cache flags."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from opencad import (
    DesignDocument, Part, RuntimeContext, check_design_reference, edit_design_document,
    load_design_document, review_design_document,
)
from opencad.design_artifact import build_design_artifact
from opencad.kernel.core.analytic_backend import AnalyticBackend


def make_document():
    context = RuntimeContext(backend=AnalyticBackend())
    part = Part(context=context).box(20, 10, 4)
    artifact = build_design_artifact(artifact_id="plate", feature_tree=context.tree,
        parameters={"length": {"value": 20, "unit": "mm", "feature_id": part.feature_id}})
    return DesignDocument.create(artifact=artifact,
        bindings={"length": {"feature_id": part.feature_id, "field": "length"}}), part.feature_id


def test_roundtrip_keeps_exact_source_and_rejects_tampering(tmp_path):
    document, feature = make_document()
    path = tmp_path / "design.json"
    document.write_json(path)
    restored = load_design_document(path)
    assert restored == document
    payload = json.loads(path.read_text())
    payload["artifact"]["feature_tree"]["nodes"][feature]["parameters"]["length"] = 99
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_design_document(path)


def test_edit_updates_source_revision_and_invalidates_review_and_reference():
    document, feature = make_document()
    reference = document.reference(feature)
    reviewed = review_design_document(document, reviewer="Engineer", decision="accepted",
                                      note="Checked requirements and source dimensions.")
    assert reviewed.content_sha256 == document.content_sha256
    assert reviewed.review_status == "accepted"
    updated = edit_design_document(reviewed, {"length": 30}, expected_sha256=document.content_sha256)
    assert updated.artifact.parameters["length"].value == 30
    assert updated.artifact.feature_tree.nodes[feature].parameters["length"] == 30
    assert document.artifact.feature_tree.nodes[feature].parameters["length"] == 20
    assert updated.artifact.feature_tree.nodes[feature].status == "stale"
    assert updated.revision == 2 and updated.parent_sha256 == document.content_sha256
    assert updated.review_status == "stale"
    assert check_design_reference(updated, reference) == "stale"
    assert check_design_reference(updated, updated.reference(feature)) == "current"
    with pytest.raises(ValueError, match="Stale edit"):
        edit_design_document(updated, {"length": 40}, expected_sha256=document.content_sha256)


def test_unbound_parameter_cannot_pretend_to_edit_geometry():
    document, _ = make_document()
    with pytest.raises(ValueError, match="no explicit operation binding"):
        edit_design_document(document, {"width": 20}, expected_sha256=document.content_sha256)


@pytest.mark.parametrize("value", [True, "30", {}, math.nan, math.inf])
def test_invalid_values_are_rejected_atomically(value):
    document, feature = make_document()
    with pytest.raises(ValueError):
        edit_design_document(document, {"length": value}, expected_sha256=document.content_sha256)
    assert document.artifact.feature_tree.nodes[feature].parameters["length"] == 20


def test_binding_rejects_wrong_units_and_duplicate_ownership():
    document, feature = make_document()
    artifact = document.artifact.model_copy(deep=True)
    artifact.parameters["length"].unit = "m"
    with pytest.raises(ValueError, match="declared unit"):
        DesignDocument.create(artifact=artifact, bindings=document.bindings)
    artifact = document.artifact.model_copy(deep=True)
    artifact.parameters["other_length"] = artifact.parameters["length"].model_copy(update={"name": "other_length"})
    with pytest.raises(ValueError, match="Two named parameters"):
        DesignDocument.create(artifact=artifact, bindings={
            "length": {"feature_id": feature, "field": "length"},
            "other_length": {"feature_id": feature, "field": "length"},
        })


def test_mutation_cannot_bypass_hash_at_persistence_boundary(tmp_path):
    document, feature = make_document()
    path = tmp_path / "document.json"
    document.write_json(path)
    before = path.read_bytes()
    document.artifact.feature_tree.nodes[feature].parameters["length"] = 50
    with pytest.raises(ValueError):
        document.write_json(path)
    assert path.read_bytes() == before


def test_external_file_import_is_rejected_even_on_inactive_branch():
    document, feature = make_document()
    artifact = document.artifact.model_copy(deep=True)
    node = artifact.feature_tree.nodes[feature].model_copy(update={"operation": "import_step", "parameters": {"filepath": "/tmp/source.step"}})
    artifact.feature_tree.branch_snapshots["external"] = {**artifact.feature_tree.nodes, feature: node}
    with pytest.raises(ValueError, match="separately packaged"):
        DesignDocument.create(artifact=artifact)


def test_upstream_edit_rejects_unproven_face_edge_rebinding():
    from opencad.tree.models import FeatureNode
    from opencad.tree.service import FeatureTreeService
    document, feature = make_document()
    artifact = document.artifact.model_copy(deep=True)
    artifact.feature_tree = FeatureTreeService.add_feature(artifact.feature_tree, FeatureNode(
        id="finish", name="Finish", operation="fillet_edges", parent_id=feature,
        parameters={"shape_id": feature, "edge_ids": ["box-0001:edge:0"], "radius": 1}))
    document = DesignDocument.create(artifact=artifact, bindings=document.bindings)
    with pytest.raises(ValueError, match="explicit reselection.*finish"):
        edit_design_document(document, {"length": 30}, expected_sha256=document.content_sha256)


def test_schema_requires_checksum_and_forbids_unknown_envelope_fields():
    schema = DesignDocument.model_json_schema()
    assert "content_sha256" in schema["required"]
    assert schema["additionalProperties"] is False
    document, _ = make_document()
    with pytest.raises(ValueError):
        DesignDocument.model_validate({**document.model_dump(), "autofabricate": True})


@pytest.mark.parametrize("storage", ["typed", "derived"])
def test_upstream_edit_rejects_typed_and_derived_subshape_selections(storage):
    from opencad.tree.models import FeatureNode, ParameterBinding, TypedParameter
    from opencad.tree.service import FeatureTreeService
    document, feature = make_document()
    artifact = document.artifact.model_copy(deep=True)
    node = FeatureNode(id="finish", name="Finish", operation="fillet_edges", parent_id=feature,
                       parameters={"shape_id": feature, "radius": 1})
    if storage == "typed":
        node.typed_parameters["edge_ids"] = TypedParameter(type="json", value=["box-0001:edge:0"])
    else:
        artifact.feature_tree.nodes[feature].parameters["selected_edges"] = ["box-0001:edge:0"]
        node.parameter_bindings = [ParameterBinding(parameter="edge_ids", source="node",
            source_key=feature, source_path="parameters.selected_edges")]
    artifact.feature_tree = FeatureTreeService.add_feature(artifact.feature_tree, node)
    document = DesignDocument.create(artifact=artifact, bindings=document.bindings)
    with pytest.raises(ValueError, match="explicit reselection.*finish"):
        edit_design_document(document, {"length": 30}, expected_sha256=document.content_sha256)


def test_document_rejects_boolean_schema_version_and_nonobject_bindings():
    document, _ = make_document()
    with pytest.raises(ValueError, match="excluding booleans"):
        DesignDocument.model_validate({**document.model_dump(), "schema_version": True})
    with pytest.raises(ValueError, match="bindings must be a JSON object"):
        DesignDocument.create(artifact=document.artifact, bindings=[])


def test_revision_publication_is_create_once_under_concurrent_writers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    document, _ = make_document()
    path = tmp_path / "revision.json"
    def publish(_):
        try:
            document.write_json(path)
            return "created"
        except FileExistsError:
            return "exists"
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(publish, range(24)))
    assert outcomes.count("created") == 1
    assert outcomes.count("exists") == 23
    assert load_design_document(path) == document
    assert list(tmp_path.iterdir()) == [path]


def test_cli_cannot_overwrite_source_with_a_stale_review_or_edit(tmp_path):
    from opencad.cli import main
    document, _ = make_document()
    path = tmp_path / "design.json"
    document.write_json(path)
    before = path.read_bytes()
    assert main(["document", "review", str(path), "--reviewer", "Engineer", "--decision",
                 "accepted", "--note", "Reviewed", "--output", str(path)]) == 1
    assert path.read_bytes() == before
    values = tmp_path / "values.json"
    values.write_text('{"length":30}')
    assert main(["document", "edit", str(path), "--values", str(values), "--expected-sha256",
                 document.content_sha256, "--output", str(path)]) == 1
    assert path.read_bytes() == before


def test_inactive_branch_cannot_hide_a_missing_root():
    document, _ = make_document()
    artifact = document.artifact.model_copy(deep=True)
    artifact.feature_tree.branch_snapshots["broken"] = {}
    with pytest.raises(ValueError, match="Every saved branch"):
        DesignDocument.create(artifact=artifact, bindings=document.bindings)
