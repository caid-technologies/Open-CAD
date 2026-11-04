# opencad-backend

Requires Python **3.11 or newer**.

FastAPI transport over the OpenCAD core. This is the only distribution in the
repository that depends on a web framework — `opencad` and `opencad-agent`
carry no FastAPI, Starlette, httpx, or uvicorn.

## Run

```bash
uv run --package opencad-backend --extra occt \
  python -m uvicorn opencad_server.app:app --reload --port 8000
```

`opencad_server.app` mounts every service router in one process:

| Prefix | Router | Service |
|--------|--------|---------|
| `/kernel` | `opencad_server.kernel_router` | Geometry operations, topology, mesh |
| `/solver` | `opencad_server.solver_router` | 2-D constraint solving and diagnostics |
| `/tree` | `opencad_server.tree_router` | Feature DAG CRUD, rebuild, branching |
| `/agent` | `opencad_server.agent_router` | Natural-language modelling |

Each router module also exposes `create_app()` and a module-level `app`, so any
service can run standalone — for example `opencad_server.kernel_router:app`.

## Modules

- `app.py` — aggregate app, mounts every router
- `api_app.py` — shared FastAPI factory with production CORS/docs defaults
- `http_kernel_client.py` — the only outbound HTTP client to the kernel;
  implements `opencad.kernel.client.KernelClient`
- `*_router.py` — one router per service

## Kernel transport

Core packages depend on the `KernelClient` protocol, never on a transport. This
app chooses the implementation at startup: `HttpKernelClient` when
`OPENCAD_TREE_LIVE_KERNEL` / `OPENCAD_AGENT_LIVE_KERNEL` are true, otherwise the
shared in-process kernel used by `/kernel` mesh and export endpoints. False
does not enable mock geometry. Standalone agent/tree deployments should enable
the corresponding flag and configure `OPENCAD_KERNEL_URL` to the aggregate
backend kernel endpoint. Run the aggregate backend with one worker: kernel
shapes are in memory and are not shared between worker processes.

## Tests

```bash
OPENCAD_KERNEL_BACKEND=analytic pytest
```

`tests/test_core_boundary.py` parses every core module and fails if any of them
imports FastAPI, Starlette, httpx, uvicorn, python-dotenv, or `opencad_server`.
