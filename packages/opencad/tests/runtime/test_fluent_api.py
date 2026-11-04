from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

from opencad import Part, Sketch, RuntimeContext, get_default_context, reset_default_context, set_default_context


def test_fluent_sketch_extrude_fillet_rejects_analytic_step_export(tmp_path: Path) -> None:
    reset_default_context()

    sketch = Sketch().rect(10, 20).circle(3, subtract=True)
    # This tests analytic STEP rejection, not geometric edge selection. The
    # analytic backend cannot certify a top rim; that is covered by native tests.
    part = Part().extrude(sketch, depth=5).fillet(edges="all", radius=0.5)

    output = tmp_path / "output.step"
    with pytest.raises(RuntimeError, match="analytic backend cannot export STEP"):
        part.export(str(output))

    ctx = get_default_context()
    assert not output.exists()
    assert part.shape_id is not None
    assert sketch.feature_id is not None
    assert len(ctx.tree.nodes) >= 3  # root + sketch + feature chain
    extrude = next(node for node in ctx.tree.nodes.values() if node.operation == "extrude")
    assert extrude.parent_id is None
    assert extrude.sketch_id == sketch.feature_id
    assert extrude.depends_on == [sketch.feature_id]


def test_fluent_boolean_chain_records_dependencies() -> None:
    reset_default_context()
    left = Part().box(10, 10, 10)
    right = Part().cylinder(3, 10)

    left.cut(right)

    ctx = get_default_context()
    assert left.feature_id is not None
    node = ctx.tree.nodes[left.feature_id]
    assert node.operation == "boolean_cut"
    assert len(node.depends_on) == 2


def test_fluent_box_is_centered_at_modeling_origin() -> None:
    reset_default_context()
    part = Part().box(10, 5, 3)

    ctx = get_default_context()
    assert part.shape_id is not None
    box = ctx.kernel.store.get(part.shape_id)
    assert box is not None
    assert box.bbox.min_x == pytest.approx(-5.0)
    assert box.bbox.min_y == pytest.approx(-2.5)
    assert box.bbox.min_z == pytest.approx(-1.5)
    assert box.bbox.max_x == pytest.approx(5.0)
    assert box.bbox.max_y == pytest.approx(2.5)
    assert box.bbox.max_z == pytest.approx(1.5)


def test_fluent_translate_positions_shape_and_records_dependency() -> None:
    reset_default_context()
    part = Part().box(2, 3, 4).translate((10.0, -2.0, 5.0))

    ctx = get_default_context()
    assert part.feature_id is not None
    assert part.shape_id is not None
    translated = ctx.kernel.store.get(part.shape_id)
    assert translated is not None
    assert translated.bbox.min_x == pytest.approx(9.0)
    assert translated.bbox.min_y == pytest.approx(-3.5)
    assert translated.bbox.min_z == pytest.approx(3.0)
    assert translated.bbox.max_x == pytest.approx(11.0)
    assert translated.bbox.max_y == pytest.approx(-0.5)
    assert translated.bbox.max_z == pytest.approx(7.0)
    node = ctx.tree.nodes[part.feature_id]
    assert node.operation == "translate"
    assert node.depends_on == ["feat-0001"]

    rebuilt_tree = ctx.rebuild_tree()
    rebuilt_node = rebuilt_tree.nodes[part.feature_id]
    assert rebuilt_node.status == "built"
    assert rebuilt_node.shape_id is not None
    rebuilt = ctx.kernel.store.get(rebuilt_node.shape_id)
    assert rebuilt is not None
    assert rebuilt.bbox.min_x == pytest.approx(9.0)
    assert rebuilt.bbox.min_y == pytest.approx(-3.5)
    assert rebuilt.bbox.min_z == pytest.approx(3.0)


def test_fluent_part_exports_stl_by_extension(tmp_path: Path) -> None:
    reset_default_context()
    output = tmp_path / "box.stl"

    Part().box(4, 3, 2).export(str(output))

    assert output.read_text(encoding="utf-8").startswith("solid opencad")


def test_fluent_part_rejects_unknown_export_extension(tmp_path: Path) -> None:
    reset_default_context()

    with pytest.raises(ValueError, match=".step, .stp, or .stl"):
        Part().box(1, 1, 1).export(str(tmp_path / "box.obj"))


@pytest.mark.skipif(
    importlib.util.find_spec("OCP") is None, reason="CadQuery/OCP not installed"
)
def test_fluent_translate_moves_a_real_occt_shape() -> None:
    from opencad.kernel.core.backend_factory import create_backend

    ctx = RuntimeContext(backend=create_backend("occt", require_native=True))
    set_default_context(ctx)
    try:
        part = Part().box(2.0, 3.0, 4.0).translate((10.0, -2.0, 5.0))

        assert part.shape_id is not None
        translated = ctx.kernel.store.get(part.shape_id)
        assert translated is not None
        assert translated.bbox.min_x == pytest.approx(9.0)
        assert translated.bbox.min_y == pytest.approx(-3.5)
        assert translated.bbox.min_z == pytest.approx(3.0)
        assert translated.bbox.max_x == pytest.approx(11.0)
        assert translated.bbox.max_y == pytest.approx(-0.5)
        assert translated.bbox.max_z == pytest.approx(7.0)
    finally:
        reset_default_context()


def test_fluent_sketch_writes_profile_order_metadata() -> None:
    reset_default_context()
    sketch = Sketch(name="Ordered").rect(4, 3).circle(1.0, center=(2.0, 1.5), subtract=True)
    Part().extrude(sketch, depth=2)

    ctx = get_default_context()
    assert sketch.feature_id is not None
    node = ctx.tree.nodes[sketch.feature_id]

    entities = node.parameters.get("entities", {})
    profile_order = node.parameters.get("profile_order", [])

    assert isinstance(entities, dict)
    assert isinstance(profile_order, list)
    assert len(profile_order) == len(entities)
    assert any(bool(v.get("subtract")) for v in entities.values() if v.get("type") == "circle")


# ── Revolve / loft / sweep ──────────────────────────────────────────


def test_fluent_revolve_records_its_sketch() -> None:
    reset_default_context()
    profile = Sketch(name="Ring section", plane="XZ").rect(5, 8, origin=(10.0, 0.0))
    part = Part().revolve(profile, axis_direction=(0.0, 0.0, 1.0), angle=360.0)

    ctx = get_default_context()
    assert part.feature_id is not None
    node = ctx.tree.nodes[part.feature_id]

    assert node.operation == "revolve"
    assert node.sketch_id == profile.feature_id
    assert node.parameters["shape_id"] == profile.feature_id
    assert node.parameters["angle"] == 360.0


def test_fluent_loft_records_every_profile() -> None:
    reset_default_context()
    bottom = Sketch(name="Bottom").circle(5.0)
    top = Sketch(name="Top", origin=(0.0, 0.0, 10.0)).circle(2.0)
    part = Part().loft([bottom, top])

    ctx = get_default_context()
    assert part.feature_id is not None
    node = ctx.tree.nodes[part.feature_id]

    assert node.operation == "loft"
    assert node.parameters["profile_ids"] == [bottom.feature_id, top.feature_id]
    assert node.depends_on == [bottom.feature_id, top.feature_id]


def test_fluent_loft_rejects_a_single_profile() -> None:
    reset_default_context()
    with pytest.raises(ValueError, match="at least two profiles"):
        Part().loft([Sketch().circle(5.0)])


def test_fluent_sweep_records_profile_and_path() -> None:
    reset_default_context()
    profile = Sketch(name="Profile").circle(1.0)
    path = Sketch(name="Path", plane="XZ").line((0.0, 0.0), (0.0, 20.0))
    part = Part().sweep(profile, path)

    ctx = get_default_context()
    assert part.feature_id is not None
    node = ctx.tree.nodes[part.feature_id]

    assert node.operation == "sweep"
    assert node.parameters["profile_id"] == profile.feature_id
    assert node.parameters["path_id"] == path.feature_id
    assert node.depends_on == [profile.feature_id, path.feature_id]


@pytest.mark.skipif(
    importlib.util.find_spec("OCP") is None, reason="CadQuery/OCP not installed"
)
def test_fluent_revolve_builds_a_real_annulus() -> None:
    """A rectangle offset from the axis sweeps into a tube, so the result is
    checkable against pi * (ro^2 - ri^2) * h."""
    import cadquery as cq

    from opencad.kernel.core.backend_factory import create_backend
    from opencad.runtime import RuntimeContext, set_default_context

    ctx = RuntimeContext(backend=create_backend("occt", require_native=True))
    set_default_context(ctx)
    try:
        inner, thickness, height = 10.0, 5.0, 8.0
        profile = Sketch(name="Tube section", plane="XZ").rect(
            thickness, height, origin=(inner, 0.0)
        )
        part = Part().revolve(profile)

        assert part.shape_id is not None
        native = ctx.registry.kernel.get_native_shape(part.shape_id)
        outer = inner + thickness
        expected = math.pi * (outer**2 - inner**2) * height
        assert cq.Shape(native).Volume() == pytest.approx(expected, rel=0.001)
    finally:
        reset_default_context()


# ── Sketch arcs ─────────────────────────────────────────────────────


def test_fluent_sketch_records_an_arc_entity() -> None:
    reset_default_context()
    sketch = (
        Sketch(name="Half disc")
        .line((-10.0, 0.0), (10.0, 0.0))
        .arc((10.0, 0.0), (-10.0, 0.0), center=(0.0, 0.0), radius=10.0)
    )
    Part().extrude(sketch, depth=5)

    ctx = get_default_context()
    assert sketch.feature_id is not None
    entities = ctx.tree.nodes[sketch.feature_id].parameters.get("entities", {})

    arcs = [entity for entity in entities.values() if entity.get("type") == "arc"]
    assert len(arcs) == 1
    assert arcs[0]["center"] == (0.0, 0.0)
    assert arcs[0]["radius"] == 10.0


@pytest.mark.skipif(
    importlib.util.find_spec("OCP") is None, reason="CadQuery/OCP not installed"
)
def test_fluent_arc_closes_a_half_disc_profile() -> None:
    """A diameter plus its arc is the smallest closed profile that needs an arc,
    so the extruded volume pins down that the arc bows the right way."""
    import cadquery as cq

    from opencad.kernel.core.backend_factory import create_backend
    from opencad.runtime import RuntimeContext, set_default_context

    ctx = RuntimeContext(backend=create_backend("occt", require_native=True))
    set_default_context(ctx)
    try:
        radius, depth = 10.0, 5.0
        sketch = (
            Sketch(name="Half disc")
            .line((-radius, 0.0), (radius, 0.0))
            .arc((radius, 0.0), (-radius, 0.0), center=(0.0, 0.0), radius=radius)
        )
        part = Part().extrude(sketch, depth=depth)

        assert part.shape_id is not None
        native = ctx.registry.kernel.get_native_shape(part.shape_id)
        expected = math.pi * radius**2 / 2.0 * depth
        assert cq.Shape(native).Volume() == pytest.approx(expected, rel=0.001)
    finally:
        reset_default_context()
