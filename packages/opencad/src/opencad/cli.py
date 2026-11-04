from __future__ import annotations

import argparse
import importlib
from contextlib import contextmanager
import json
import runpy
import sys
from pathlib import Path

from opencad.runtime import RuntimeContext, get_default_context, set_default_context
from opencad.kernel.core.backend_factory import BackendUnavailableError, create_backend
from opencad.turntable import (
    DEFAULT_DEFLECTION,
    DEFAULT_FPS,
    DEFAULT_FRAMES,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    SUPPORTED_FORMATS,
    TurntableOptions,
    resolve_format,
)


def _add_backend_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "occt", "analytic"],
        help="Geometry backend (auto prefers OCCT; STEP/STL export requires OCCT)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencad", description="OpenCAD headless CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Rebuild a feature tree JSON in-process")
    build_parser.add_argument("model", help="Input model JSON file")
    build_parser.add_argument("--output", help="Output JSON path")
    build_parser.add_argument("--continue-on-error", action="store_true", help="Continue rebuild after failed nodes")
    build_parser.add_argument(
        "--id-strategy",
        default="readable",
        choices=["readable", "uuid"],
        help="Shape ID strategy for rebuild-created shapes",
    )
    _add_backend_argument(build_parser)
    build_parser.set_defaults(func=_cmd_build)

    run_parser = subparsers.add_parser("run", help="Run a Python script with the opencad fluent API")
    run_parser.add_argument("script", help="Python script path")
    run_parser.add_argument("--export", help="Optional STEP, STP, or STL output path")
    run_parser.add_argument("--tree-output", help="Optional path to write resulting feature tree JSON")
    run_parser.add_argument(
        "--id-strategy",
        default="readable",
        choices=["readable", "uuid"],
        help="Shape ID strategy for script execution",
    )
    _add_turntable_arguments(run_parser)
    _add_backend_argument(run_parser)
    run_parser.set_defaults(func=_cmd_run)

    document_parser = subparsers.add_parser("document", help="Inspect, revise, review, and export portable design documents")
    commands = document_parser.add_subparsers(dest="document_command", required=True)
    schema = commands.add_parser("schema", help="Print the portable design document JSON Schema")
    schema.set_defaults(func=_cmd_document)
    create = commands.add_parser("create", help="Wrap an existing CAID v1 artifact with explicit operation bindings")
    create.add_argument("artifact")
    create.add_argument("--bindings", required=True, help="JSON file mapping parameter names to feature_id and field")
    create.add_argument("--output", required=True)
    create.set_defaults(func=_cmd_document)
    inspect = commands.add_parser("inspect", help="Validate integrity and inspect source metadata")
    inspect.add_argument("document")
    inspect.add_argument("--reference", help="Check a saved revision-pinned DesignReference JSON")
    inspect.set_defaults(func=_cmd_document)
    edit = commands.add_parser("edit", help="Revise explicitly bound operation parameters")
    edit.add_argument("document")
    edit.add_argument("--values", required=True, help="JSON file containing named parameter values")
    edit.add_argument("--expected-sha256", required=True)
    edit.add_argument("--output", required=True)
    edit.set_defaults(func=_cmd_document)
    review = commands.add_parser("review", help="Record a caller-declared human decision for this exact revision")
    review.add_argument("document")
    review.add_argument("--reviewer", required=True)
    review.add_argument("--decision", required=True, choices=["accepted", "rejected"])
    review.add_argument("--note", required=True)
    review.add_argument("--output", required=True)
    review.set_defaults(func=_cmd_document)
    export = commands.add_parser("export", help="Cold-rebuild and atomically export native STEP or STL")
    export.add_argument("document")
    export.add_argument("--feature", required=True)
    export.add_argument("--output", required=True)
    export.set_defaults(func=_cmd_document)
    return parser


def _cmd_document(args: argparse.Namespace) -> int:
    from opencad.design_artifact import load_design_artifact
    from opencad.document import (
        DesignDocument, check_design_reference, edit_design_document,
        export_design_document, load_design_document, review_design_document,
    )

    try:
        action = args.document_command
        if action == "schema":
            result = DesignDocument.model_json_schema()
        elif action == "create":
            document = DesignDocument.create(
                artifact=load_design_artifact(args.artifact),
                bindings=json.loads(Path(args.bindings).read_text(encoding="utf-8")))
            document.write_json(args.output)
            result = {"content_sha256": document.content_sha256, "revision": document.revision}
        else:
            document = load_design_document(args.document)
            if action == "inspect":
                result = {"artifact_id": document.artifact.artifact_id, "revision": document.revision,
                          "content_sha256": document.content_sha256, "parent_sha256": document.parent_sha256,
                          "bindings": {key: value.model_dump() for key, value in document.bindings.items()},
                          "review_status": document.review_status,
                          "feature_ids": list(document.artifact.feature_tree.nodes)}
                if args.reference:
                    result["reference_status"] = check_design_reference(
                        document, json.loads(Path(args.reference).read_text(encoding="utf-8")))
                    if result["reference_status"] != "current":
                        print(json.dumps(result, sort_keys=True))
                        return 1
            elif action == "edit":
                values = json.loads(Path(args.values).read_text(encoding="utf-8"))
                if not isinstance(values, dict):
                    raise ValueError("Named parameter values must be a JSON object.")
                updated = edit_design_document(document, values, expected_sha256=args.expected_sha256)
                updated.write_json(args.output)
                result = {"content_sha256": updated.content_sha256, "revision": updated.revision,
                          "parent_sha256": updated.parent_sha256, "review_status": updated.review_status}
            elif action == "review":
                updated = review_design_document(document, reviewer=args.reviewer,
                    decision=args.decision, note=args.note)
                updated.write_json(args.output)
                result = {"content_sha256": updated.content_sha256, "review_status": updated.review_status}
            else:
                result = export_design_document(document, args.output, feature_id=args.feature)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


def _add_turntable_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--turntable",
        help="Optional rotating preview output path (.gif or .mp4); requires the OCCT backend",
    )
    parser.add_argument(
        "--turntable-format",
        choices=list(SUPPORTED_FORMATS),
        help="Override the turntable format inferred from the output extension",
    )
    parser.add_argument(
        "--turntable-frames",
        type=int,
        default=DEFAULT_FRAMES,
        help=f"Frames in one full revolution (default: {DEFAULT_FRAMES})",
    )
    parser.add_argument(
        "--turntable-fps",
        type=int,
        default=DEFAULT_FPS,
        help=f"Playback rate in frames per second (default: {DEFAULT_FPS})",
    )
    parser.add_argument(
        "--turntable-size",
        default=f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT}",
        help=f"Frame size as WIDTHxHEIGHT (default: {DEFAULT_WIDTH}x{DEFAULT_HEIGHT})",
    )
    parser.add_argument(
        "--turntable-deflection",
        type=float,
        default=DEFAULT_DEFLECTION,
        help=f"Tessellation tolerance; lower is finer (default: {DEFAULT_DEFLECTION})",
    )


def _parse_size(value: str) -> tuple[int, int]:
    width, separator, height = value.lower().partition("x")
    if not separator or not width.strip().isdigit() or not height.strip().isdigit():
        raise ValueError(f"Invalid --turntable-size '{value}'. Use WIDTHxHEIGHT, for example 640x480.")
    return int(width), int(height)


def _turntable_options(args: argparse.Namespace) -> TurntableOptions:
    width, height = _parse_size(args.turntable_size)
    return TurntableOptions(
        frames=args.turntable_frames,
        fps=args.turntable_fps,
        width=width,
        height=height,
        deflection=args.turntable_deflection,
    )


def _cmd_build(args: argparse.Namespace) -> int:
    backend = create_backend(args.backend, id_strategy=args.id_strategy)
    context = RuntimeContext(id_strategy=args.id_strategy, backend=backend)
    context.load_tree_json(args.model)
    tree = context.rebuild_tree(continue_on_error=args.continue_on_error)

    output_path = args.output
    if not output_path:
        input_path = Path(args.model)
        output_path = str(input_path.with_suffix(".built.json"))

    context.save_tree_json(output_path)
    print(f"Rebuilt tree '{tree.root_id}' with {len(tree.nodes)} nodes -> {output_path}")
    return 0


@contextmanager
def _script_import_scope(directory: Path):
    """Resolve sibling imports from this model during repeated in-process runs.

    A previous model may have loaded a package with the same name. Preserve the
    host's modules and search path, rather than reusing that unrelated source or
    leaving this model's imports in later builds.
    """
    names = {path.stem if path.is_file() else path.name for path in directory.iterdir()
             if (path.is_dir() or path.suffix == ".py")}
    names = {name for name in names if name.isidentifier()}
    def belongs(name):
        return name.partition(".")[0] in names
    saved = {name: module for name, module in list(sys.modules.items()) if belongs(name)}
    previous_path = list(sys.path)
    for name in saved:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(directory))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        for name in list(sys.modules):
            if belongs(name):
                sys.modules.pop(name, None)
        sys.modules.update(saved)
        sys.path[:] = previous_path
        importlib.invalidate_caches()


def _cmd_run(args: argparse.Namespace) -> int:
    export_format = _export_format(args.export) if args.export else None

    # Resolve turntable settings before running the script so a bad extension
    # or size is reported immediately rather than after the model is built.
    turntable_format = resolve_format(args.turntable, args.turntable_format) if args.turntable else None
    turntable_options = _turntable_options(args) if args.turntable else None
    if args.turntable and args.backend == "analytic":
        raise BackendUnavailableError(
            "The analytic backend cannot tessellate geometry, so it cannot render a turntable. "
            "Use --backend occt and install it with: uv sync --extra occt"
        )

    backend = create_backend(
        args.backend,
        id_strategy=args.id_strategy,
        require_native=bool(args.export) or bool(args.turntable),
    )
    context = RuntimeContext(id_strategy=args.id_strategy, backend=backend)
    set_default_context(context)

    script_path = Path(args.script)
    if not script_path.exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    with _script_import_scope(script_path.resolve().parent):
        runpy.run_path(str(script_path), run_name="__main__")

    current = get_default_context()
    if args.export:
        if not current.last_shape_id:
            raise RuntimeError(f"No shape was produced by the script, cannot export {export_format.upper()}.")
        if export_format == "stl":
            current.export_stl(current.last_shape_id, args.export)
        else:
            current.export_step(current.last_shape_id, args.export)
        print(f"Exported {export_format.upper()} to {args.export}")

    if args.turntable:
        if not current.last_shape_id:
            raise RuntimeError("No shape was produced by the script, cannot render a turntable.")
        current.export_turntable(
            current.last_shape_id,
            args.turntable,
            fmt=turntable_format,
            options=turntable_options,
        )
        print(f"Rendered {turntable_format.upper()} turntable to {args.turntable}")

    if args.tree_output:
        current.save_tree_json(args.tree_output)
        print(f"Wrote tree JSON to {args.tree_output}")

    print(f"Script completed. Nodes: {len(current.tree.nodes)}")
    return 0


def _export_format(filepath: str) -> str:
    suffix = Path(filepath).suffix.lower()
    if suffix in {".step", ".stp"}:
        return "step"
    if suffix == ".stl":
        return "stl"
    raise ValueError("Unsupported export format. Use a .step, .stp, or .stl destination.")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
