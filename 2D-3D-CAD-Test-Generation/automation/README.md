# `automation/` — pywin32 direct-COM build executor (experimental, additive)

This package is the **MTI_Python** workstream: a self-contained layer that drives
SolidWorks through the Windows COM API with explicit VARIANT/SAFEARRAY marshalling
and a context-managed session, gated behind a feature flag so it can be A/B tested
against the existing build path before either is retired.

## Important context (read before extending)

The originating spec assumed the current build stage was *"VBA macro text executed
via COM."* **In this repo that is not the case.** Builds already happen by direct
COM in [`pipeline/solidworks_builder.py`](../pipeline/solidworks_builder.py) (the
`--engine com` path, ~1800 lines, verified against SolidWorks 2024). The VBA
macros ([`pipeline/macro_generator.py`](../pipeline/macro_generator.py)) and the
COM build are two **parallel artifacts** of the same build plan.

So this package does **not** fork the geometry engine. It adds the envelope the
spec actually wanted:

| Module | Role |
| --- | --- |
| `com_client.py` | `SolidWorksSession` context manager (keeps SW open across builds) + structured `SolidWorksComError`. |
| `marshalling.py` | The single source of truth for point/array VARIANTs (`to_point_variant`, `to_double_array_variant`, `to_dispatch_array_variant`, `create_sw_point`). |
| `build_executor.py` | `BuildExecutor` primitive ops (`new_part`/`insert_sketch`/`add_line`/`add_circle`/`extrude`/`add_fillet`/`export_stl`) + `run(model, …)` that builds a full part via the proven engine inside a managed session and returns a `BuildReport`. |
| `config.py` | The `BUILD_EXECUTOR_MODE` feature flag (`vba` default \| `pywin32`). |
| `compare.py` | `compare_build()` — builds a model both ways and diffs feature-tree counts + STL bounding boxes for parity. |

### Early binding vs late binding

The spec asked for early-bound `gencache.EnsureDispatch("SldWorks.Application")`.
The `SldWorks.Application` IDispatch does **not** implement `GetTypeInfo()`, so
`EnsureDispatch` raises *"This COM object can not automate the makepy process."*
`SolidWorksSession` therefore warms the early-bound **constants** type library
(which is what early binding actually buys — named enums + generated signatures)
and connects to the app object late-bound. This matches the documented reality in
`solidworks_builder.py` and is why the connection code looks the way it does.

## Usage

```powershell
# Opt into the pywin32 build path for a run (VBA macros are still generated):
$env:BUILD_EXECUTOR_MODE = "pywin32"
python main.py --views-folder ..\test_drawings\Test2 --output ..\test_drawings\Test2\output
```

```python
# Primitive ops (what the live tests exercise):
from automation.build_executor import BuildExecutor
from automation.com_client import SolidWorksSession

with SolidWorksSession() as sw:      # does NOT close SolidWorks on exit
    ex = BuildExecutor(sw, part_name="demo")
    ex.new_part()
    ex.insert_sketch("front")
    ex.add_line(0, 0, 0.1, 0); ex.add_line(0.1, 0, 0.1, 0.05)
    ex.add_line(0.1, 0.05, 0, 0.05); ex.add_line(0, 0.05, 0, 0)
    ex.extrude(0.02)
    ex.export_stl("out/")
```

```
# A/B parity check via the debug endpoint (Windows + SolidWorks):
POST /debug/compare-build/{session}/{part}
```

## `SolidWorksComError` payload schema

Every COM failure re-raised by this package carries a structured payload; the
`.as_lesson()` method returns a dict ready for
`pipeline.must_meet.append_lesson(lessons_path, record)` (which adds `timestamp`).

```json
{
  "source":     "pywin32_build_executor",
  "kind":       "com_error",
  "method":     "FeatureCut4",
  "args":       [true, false, 0.5],
  "hresult":    "0x80020005",
  "feature_id": "F003",
  "message":    "returned Nothing"
}
```

- `method` — the SolidWorks API method that failed.
- `args` — JSON-safe repr of the call arguments (COM objects stringify).
- `hresult` — `0x`-prefixed HRESULT when the failure was a `pywintypes.com_error`, else `null`.
- `feature_id` — the owning build-plan feature when known, else `null`.
- `message` — the underlying error text.

## Tests

- `tests/test_marshalling.py` — standalone VARIANT flags/values (real pywin32, no SolidWorks).
- `tests/test_build_executor.py` — config flag, error payload, build report, bbox diff (any OS).
- `tests/test_build_executor_live.py` — live geometry, **gated** on `SOLIDWORKS_LIVE_TEST=1`.

## Status

**Experimental / additive.** Default is `vba`; nothing changes until a run sets
`BUILD_EXECUTOR_MODE=pywin32`. Do not retire the VBA path until `/debug/compare-build`
shows parity on ≥10 real build plans from past runs.
