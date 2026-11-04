# opencad

Requires Python **3.11 or newer**.

The OpenCAD core: geometry kernel, constraint solver, feature tree, and the
fluent modelling API — with no web framework and no network dependency.

Install:

```bash
pip install opencad          # pydantic + numpy only
pip install "opencad[occt]"  # add the OCCT B-rep backend
pip install "opencad[full]"  # OCCT + scipy solver + networkx graph analysis
```

Use:

```python
from opencad import Part, Sketch

profile = Sketch(name="Plate").rect(80, 30).circle(3, center=(8, 8), subtract=True)
plate = Part().extrude(profile, depth=4, name="Plate")
plate.fillet(edges="all", radius=1.0)
```

Every fluent call appends a node to a rebuildable feature DAG, which you can
serialize, edit, and replay. See the repository `ARCHITECTURE.md` for the
kernel/solver/tree design and `TOPOLOGY.md` for topological naming.

## Packages

| Module | Responsibility |
|--------|----------------|
| `opencad` | Fluent `Part`/`Sketch` API, `RuntimeContext`, CLI, design artifacts |
| `opencad.kernel` | B-rep operations, OCCT and analytic backends, `KernelClient` |
| `opencad.solver` | 2-D constraint solving (NumPy/SciPy and SolveSpace backends) |
| `opencad.tree` | Feature DAG, incremental rebuild, branching, expressions |
| `opencad.kinematics` | Rigid fixed/revolute/prismatic joints and assembly pose evaluation |
| `opencad.gears` | Bounded involute spur-gear profiles and separately addressable solids |

## Coupled gear motion

`spur_gear(SpurGearSpec(teeth=20), center_mm=(-30, 0, 0))` creates an
OpenCAD solid with a sampled involute profile. Gear specifications validate
tooth counts, bore clearance and backlash. Both gears in a pair use the same
module and pressure angle. Root transitions are simplified, not hob-generated.

`GearCoupling(driver_joint_id="a", driven_joint_id="b", driver_teeth=20,
driven_teeth=40)` enforces `b = phase_radians - a * 20 / 40`. Pass couplings to
`evaluate_assembly_pose(joints, {"a": progress}, gear_couplings=[coupling])`.
Use `resolve_gear_progress` when the joint values are also needed. For a cycle
of two driver turns, set driver limits to `[0, 4*pi]` and driven limits to
`[-2*pi, 0]`. Both axes must point the same way in a shared parent frame.

Couplings reject cycles, duplicate drivers, unknown/non-revolute joints,
conflicting inputs, and driven angles outside the joint limits. Serialize the
typed coupling alongside the joints using `model_dump(mode="json")` and reload
with `GearCoupling.model_validate`. This models prescribed rigid motion;
contact dynamics and manufacturing qualification remain separate.

## Related distributions

- `opencad-agent` — natural-language modelling on top of this package
- `opencad-backend` — FastAPI HTTP service exposing all of the above

## Tests

```bash
pytest
```

The suite runs against this package alone. A few integration tests exercise
repository files (`examples/`, `docs/schemas/`) and skip automatically when
the package is tested outside a monorepo checkout.
