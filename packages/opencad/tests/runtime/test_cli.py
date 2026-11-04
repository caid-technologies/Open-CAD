from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from opencad import Part, Sketch, get_default_context, reset_default_context
from opencad.cli import main
from opencad.kernel.core.backend_factory import BackendUnavailableError


def test_cli_build_round_trip(tmp_path: Path) -> None:
    reset_default_context()
    sketch = Sketch().rect(6, 6)
    Part().extrude(sketch, depth=4)

    ctx = get_default_context()
    model_path = tmp_path / "model.json"
    built_path = tmp_path / "model.built.json"
    model_path.write_text(ctx.serialize_tree(), encoding="utf-8")

    code = main(["build", str(model_path), "--output", str(built_path)])

    assert code == 0
    assert built_path.exists()
    built = json.loads(built_path.read_text(encoding="utf-8"))
    assert built["root_id"] == "root"


def test_cli_run_export_and_tree(tmp_path: Path) -> None:
    if importlib.util.find_spec("cadquery") is None or importlib.util.find_spec("OCP") is None:
        pytest.skip("CadQuery/OCP not installed")

    import cadquery as cq

    script_path = tmp_path / "model.py"
    step_path = tmp_path / "result.step"
    tree_path = tmp_path / "result-tree.json"
    script_path.write_text(
        "from opencad import Part, Sketch\n"
        "sk = (Sketch(name='Bracket Profile').rect(80, 30)\n"
        "      .circle(3, center=(8, 8), subtract=True)\n"
        "      .circle(3, center=(72, 8), subtract=True)\n"
        "      .circle(3, center=(8, 22), subtract=True)\n"
        "      .circle(3, center=(72, 22), subtract=True)\n"
        "      .circle(5, center=(40, 15), subtract=True))\n"
        "Part(name='Mounting Bracket').extrude(sk, depth=4).fillet(edges='top', radius=0.75)\n",
        encoding="utf-8",
    )

    code = main([
        "run",
        str(script_path),
        "--export",
        str(step_path),
        "--tree-output",
        str(tree_path),
        "--backend",
        "occt",
    ])

    assert code == 0
    assert step_path.exists()
    assert tree_path.exists()
    assert step_path.read_text(encoding="utf-8").startswith("ISO-10303-21;")
    assert not cq.importers.importStep(str(step_path)).val().isNull()


def test_cli_run_stl_export(tmp_path: Path) -> None:
    if importlib.util.find_spec("cadquery") is None or importlib.util.find_spec("OCP") is None:
        pytest.skip("CadQuery/OCP not installed")

    script_path = tmp_path / "model.py"
    stl_path = tmp_path / "result.stl"
    script_path.write_text("from opencad import Part\nPart().box(10, 8, 3)\n", encoding="utf-8")

    code = main(["run", str(script_path), "--export", str(stl_path), "--backend", "occt"])

    assert code == 0
    assert stl_path.exists()
    assert stl_path.stat().st_size > 84


@pytest.mark.parametrize("extension", ["step", "stl"])
def test_cli_rejects_analytic_cad_export(tmp_path: Path, extension: str) -> None:
    script_path = tmp_path / "model.py"
    script_path.write_text("from opencad import Part\nPart().box(1, 1, 1)\n", encoding="utf-8")

    with pytest.raises(BackendUnavailableError, match="cannot export a real STEP or STL file"):
        main([
            "run",
            str(script_path),
            "--export",
            str(tmp_path / f"invalid.{extension}"),
            "--backend",
            "analytic",
        ])


def test_cli_auto_step_export_requires_occt_install(tmp_path: Path) -> None:
    if importlib.util.find_spec("cadquery") is not None and importlib.util.find_spec("OCP") is not None:
        pytest.skip("OCCT is installed")

    script_path = tmp_path / "model.py"
    script_path.write_text("from opencad import Part\nPart().box(1, 1, 1)\n", encoding="utf-8")

    with pytest.raises(BackendUnavailableError, match="CadQuery/OCP is not installed"):
        main([
            "run",
            str(script_path),
            "--export",
            str(tmp_path / "result.step"),
        ])


def test_cli_tree_only_run_supports_analytic_backend(tmp_path: Path) -> None:
    components_dir = tmp_path / "components"
    components_dir.mkdir()
    (components_dir / "__init__.py").write_text("", encoding="utf-8")
    (components_dir / "base.py").write_text(
        "from opencad import Part\n\n"
        "def make_base():\n"
        "    return Part(name='Base').box(1, 1, 1)\n",
        encoding="utf-8",
    )
    script_path = tmp_path / "model.py"
    tree_path = tmp_path / "tree.json"
    script_path.write_text(
        "from components.base import make_base\n"
        "make_base()\n",
        encoding="utf-8",
    )

    code = main([
        "run",
        str(script_path),
        "--tree-output",
        str(tree_path),
        "--backend",
        "analytic",
    ])

    assert code == 0
    assert tree_path.exists()


def test_cli_rejects_unknown_export_extension(tmp_path: Path) -> None:
    script_path = tmp_path / "model.py"
    script_path.write_text("from opencad import Part\nPart().box(1, 1, 1)\n", encoding="utf-8")

    with pytest.raises(ValueError, match=".step, .stp, or .stl"):
        main(["run", str(script_path), "--export", str(tmp_path / "result.obj")])


def test_repeated_cli_runs_use_their_own_sibling_source(tmp_path):
    from opencad import get_default_context
    import sys
    previous = {key: value for key, value in sys.modules.items() if key == "components" or key.startswith("components.")}
    for length in (2, 7):
        directory = tmp_path / str(length)
        package = directory / "components"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "model.py").write_text(f"from opencad import Part\ndef build(): return Part().box({length}, 3, 4)\n")
        script = directory / "model.py"
        script.write_text("from components.model import build\nbuild()\n")
        assert main(["run", str(script), "--backend", "analytic"]) == 0
        context = get_default_context()
        assert context.tree.nodes[context.last_feature_id].parameters["length"] == length
    current = {key: value for key, value in sys.modules.items() if key == "components" or key.startswith("components.")}
    assert current == previous
