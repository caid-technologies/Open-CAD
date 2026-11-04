"""Real native document acceptance: reopen, edit source, export and reimport."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from opencad import (
    DesignDocument, Part, edit_design_document, export_design_document,
    load_design_document, rebuild_design_document,
)
from opencad.design_artifact import build_design_artifact


def create_document(context):
    part = Part(context=context).box(20, 10, 4)
    feature = part.feature_id
    part.translate((5, 0, 0))
    artifact = build_design_artifact(artifact_id="portable-plate", feature_tree=context.tree,
        parameters={"length": {"value": 20, "unit": "mm", "feature_id": feature}})
    return DesignDocument.create(artifact=artifact,
        bindings={"length": {"feature_id": feature, "field": "length"}}), part.feature_id


def invoke(*args):
    result = subprocess.run([sys.executable, "-c",
        "from opencad.cli import main; import sys; sys.exit(main(sys.argv[1:]))", "document", *map(str, args)],
        text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_design_document_cli_fresh_process_edit_export_and_native_reimport(context, tmp_path):
    import cadquery as cq

    document, feature = create_document(context)
    artifact = tmp_path / "artifact.json"
    document.artifact.write_json(artifact)
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps({key: value.model_dump() for key, value in document.bindings.items()}))
    source = tmp_path / "document.json"
    created = invoke("create", artifact, "--bindings", bindings, "--output", source)
    assert created["content_sha256"] == document.content_sha256
    review = tmp_path / "reviewed.json"
    invoke("review", source, "--reviewer", "Acceptance engineer", "--decision", "accepted",
           "--note", "Reviewed declared dimensions; no physical testing.", "--output", review)
    values = tmp_path / "values.json"
    values.write_text('{"length":30}')
    revised = tmp_path / "revised.json"
    edit = invoke("edit", review, "--values", values, "--expected-sha256", document.content_sha256,
                  "--output", revised)
    assert edit["revision"] == 2 and edit["review_status"] == "stale"
    inspected = invoke("inspect", revised)
    assert inspected["parent_sha256"] == document.content_sha256
    output = tmp_path / "revised.step"
    receipt = invoke("export", revised, "--feature", feature, "--output", output)
    assert receipt["content_sha256"] == edit["content_sha256"]
    shape = cq.importers.importStep(str(output)).val()
    assert shape.isValid() and len(shape.Solids()) == 1
    assert shape.Volume() == pytest.approx(1200, abs=1e-6)
    assert shape.BoundingBox().xlen == pytest.approx(30, abs=1e-6)
    assert shape.Center().x == pytest.approx(5, abs=1e-6)
    # A second fresh runtime repeats source reconstruction, without stored BReps.
    restored = load_design_document(revised)
    fresh = rebuild_design_document(restored)
    result = fresh.kernel.get_native_shape(fresh.tree.nodes[feature].shape_id)
    assert cq.Shape.cast(result).Volume() == pytest.approx(1200, abs=1e-6)


def test_design_document_failed_native_rebuild_preserves_existing_export(context, tmp_path):
    document, feature = create_document(context)
    invalid = edit_design_document(document, {"length": -30}, expected_sha256=document.content_sha256)
    output = tmp_path / "existing.step"
    output.write_bytes(b"existing deliverable")
    with pytest.raises((ValueError, RuntimeError), match="failed|Failed"):
        export_design_document(invalid, output, feature_id=feature)
    assert output.read_bytes() == b"existing deliverable"


def test_design_document_export_rejects_missing_feature_without_changing_source(context, tmp_path):
    document, _ = create_document(context)
    before = document.model_dump_json()
    with pytest.raises(ValueError, match="existing, active"):
        export_design_document(document, tmp_path / "missing.step", feature_id="absent")
    assert document.model_dump_json() == before
