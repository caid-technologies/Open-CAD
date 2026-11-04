# opencad-agent

Requires Python **3.11 or newer**.

Natural-language CAD modelling on top of the [`opencad`](../opencad) core.

The agent generates OpenCAD Python, validates it against an isolated analytic
kernel, repairs it once if validation fails, then executes it — returning the
generated code, the operations executed, and the updated feature tree.

Install:

```bash
pip install "opencad-agent[llm]"   # [llm] pulls LiteLLM for code generation
```

Use it against an existing runtime:

```python
from opencad import reset_default_context
from opencad_agent import run_chat

ctx = reset_default_context()
response, operations = run_chat(ctx, "Create a mounting bracket with 4 standoffs")
```

Or drive the service directly:

```python
from opencad_agent import OpenCadAgentService
from opencad_agent.models import ChatRequest

service = OpenCadAgentService()
result = service.chat(ChatRequest(message="Build a cog", tree_state=tree))
```

Configure the model with `OPENCAD_LLM_MODEL` and, when the provider needs it,
`OPENCAD_LLM_PROVIDER`.

## Runtime context ownership

Agent validation and execution temporarily bind their own runtime and restore
the caller's previous binding, including when validation or execution fails. After
`run_chat(ctx, ...)`, fluent `Part()` and `Sketch()` calls continue using the
same default context as before the call. Validation uses a separate runtime;
`run_chat` executes the accepted code against `ctx.kernel_client` and adopts
the resulting tree.

For explicit scoped fluent work:

```python
from opencad import Part, RuntimeContext, use_default_context

with use_default_context(RuntimeContext()) as ctx:
    Part().box(10, 10, 10)
# The previous default context is restored here, even if the block raises.
```

Bindings are local to the current thread/async context. Child async tasks inherit
bindings, so independent tasks should each bind a fresh runtime. Binding isolation
does not make concurrent mutation of the same `RuntimeContext` or shared kernel
safe. HTTP and in-process callers retain their separate service entry points.

## Kernel access

The agent never speaks HTTP. It takes a `KernelClient`
(`opencad.kernel.client`) and the caller picks the transport — an in-process
`LocalKernelClient`, or the HTTP client that `opencad-backend` supplies.

## Tests

```bash
pytest
```

`tests/agent/test_runtime_chat.py` requires a configured live LLM; the rest of
the suite runs offline with an injected completion function.
