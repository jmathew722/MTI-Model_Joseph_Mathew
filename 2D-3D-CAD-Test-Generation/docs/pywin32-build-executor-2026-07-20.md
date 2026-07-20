# pywin32 direct-COM build executor (2026-07-20, `automation/`)

Additive, experimental build path added under the **MTI_Python** workstream. It
introduces a context-managed SolidWorks COM session, a single source of truth for
VARIANT/SAFEARRAY marshalling, and a feature-flagged A/B entry point — **without**
removing or modifying the existing VBA or `--engine com` paths.

## Why this is an *envelope*, not a rewrite

The originating spec assumed the current build stage generated **VBA macro text**
that SolidWorks executed via COM, and asked to "replace that with direct pywin32
automation." In this codebase that premise does not hold:

- Parts are already built by **direct COM** in `pipeline/solidworks_builder.py`
  (`--engine com`, ~1800 lines, verified against SolidWorks 2024).
- The VBA macros (`pipeline/macro_generator.py`) and the COM build are two
  **parallel artifacts** of the same build plan; the default `--engine vba` run
  emits the macros *and* COM-builds the `.sldprt`.

So the new `automation/` package reuses the proven geometry engine and adds the
pieces the spec actually wanted around it:

1. `SolidWorksSession` — a context manager that keeps SolidWorks open across
   builds (`__exit__` releases only the COM reference; it never calls `ExitApp`).
2. `marshalling.py` — the one place point/array VARIANTs are constructed with
   explicit element-type flags (the classic SAFEARRAY gotcha).
3. `build_executor.py` — primitive ops for direct/unit testing plus
   `run(model, …)`, which drives a full part through the proven engine inside a
   managed session and returns a structured `BuildReport`.
4. `config.py` — the `BUILD_EXECUTOR_MODE` flag (`vba` default | `pywin32`).
5. `compare.py` + `POST /debug/compare-build/{session}/{part}` — build a model
   both ways and diff feature-tree counts + STL bounding boxes for parity.

## Early binding reality

The spec asked for `gencache.EnsureDispatch("SldWorks.Application")` (early
binding). The `SldWorks.Application` IDispatch does not implement `GetTypeInfo()`,
so `EnsureDispatch` fails with *"This COM object can not automate the makepy
process"* — documented at length in `solidworks_builder.py`. `SolidWorksSession`
therefore warms the early-bound **constants** type library (named enums +
generated signatures — what early binding actually provides) and connects to the
application object late-bound. The stale-cache rebuild the spec requested is
honoured against that constants type library.

## Feature flag

```
BUILD_EXECUTOR_MODE = vba      # default — existing macro + COM build, unchanged
BUILD_EXECUTOR_MODE = pywin32  # route the .sldprt COM build through automation.build_executor
```

Wired in `main.py`'s `--engine vba` build block: when `pywin32` is selected the
`.sldprt` build goes through `automation.build_executor.run` (VBA macros are still
generated); otherwise the existing `build_sldprt_for_part` path runs untouched.

## `SolidWorksComError` — lessons-ledger schema

Every COM failure re-raised by this package carries a structured payload.
`.as_lesson()` returns a dict accepted verbatim by
`pipeline.must_meet.append_lesson(lessons_path, record)` (which stamps
`timestamp`), so a failure can be appended to `lessons_learned.jsonl` directly.

| Field | Meaning |
| --- | --- |
| `source` | Always `"pywin32_build_executor"`. |
| `kind` | Always `"com_error"`. |
| `method` | The SolidWorks API method that failed (e.g. `FeatureCut4`). |
| `args` | JSON-safe repr of the call arguments (COM objects stringify). |
| `hresult` | `0x`-prefixed HRESULT when it was a `pywintypes.com_error`, else `null`. |
| `feature_id` | The owning build-plan feature when known, else `null`. |
| `message` | Underlying error text. |

Example:

```json
{
  "source": "pywin32_build_executor",
  "kind": "com_error",
  "method": "FeatureCut4",
  "args": [true, false, 0.5],
  "hresult": "0x80020005",
  "feature_id": "F003",
  "message": "returned Nothing"
}
```

## Tests

| File | Requires SolidWorks? | Notes |
| --- | --- | --- |
| `tests/test_marshalling.py` | No (pywin32 only) | VARIANT flags/values; skips without pywin32. |
| `tests/test_build_executor.py` | No | Flag, error payload, `BuildReport`, bbox diff, import cleanliness. |
| `tests/test_build_executor_live.py` | **Yes** | Gated on `SOLIDWORKS_LIVE_TEST=1`: extruded-rectangle STL dims; named fillet feature. |

## Promotion criteria

Keep the VBA path as default until `POST /debug/compare-build` shows `parity:true`
on at least **10 real build plans** from past runs.
