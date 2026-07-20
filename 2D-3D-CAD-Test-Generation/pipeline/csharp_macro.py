"""C# macro output — SolidWorks-compatible companion to the VBA package (2026-07-16).

Emits ``macros_csharp/`` beside every ``macros/`` folder: a self-contained C#
console program (late-bound COM, no interop DLLs required) that executes the
SAME build plan the VBA macros execute, in the same canonical order, logging to
``../logs/build_log_cs.txt`` / ``macro_result.json`` exactly like the VBA path.

Design rules:

* **Generated from the build plan, never transpiled from VBA.** Every geometry
  literal comes from the same :class:`~pipeline.macro_generator.BuildStep`
  fields the VBA and ``build_plan.json`` are generated from (positions_xy,
  dimensions, depth_type), so the two outputs cannot disagree on numbers.
* **Late binding via ``dynamic``** (``Type.GetTypeFromProgID("SldWorks.Application")``)
  — compiles with only the .NET Framework 4.8 reference assemblies, works with
  any installed SolidWorks version, no ``SolidWorks.Interop.*`` references.
  Enum values are therefore emitted as documented numeric constants (the same
  ones ``pipeline/solidworks_builder.py`` uses: swEndCondBlind=0,
  swEndCondThroughAll=1, swINCHES=3, …).
* **API calls mirror the verified paths**: ``FeatureExtrusion3`` /
  ``FeatureCut4`` argument lists copied from the proven COM builder (including
  the direction-flip retry), ``FeatureCircularPattern5`` with the Mark=1 axis /
  Mark=4 seed selection contract, ``SaveAs3(path, 0, 0)`` for the STL export.
* **Honest parity**: steps that are interactive in VBA (fillet/chamfer edge
  picking, slot corner fillets, the reference-axis-from-bore-face macro,
  MANUAL prohibited features) are emitted as logged WARN/MANUAL steps in C#
  too — never silently skipped, never pretend-built.
* **Self-echo check**: after emission, every in-scope step's position literals
  are asserted to appear in ``Program.cs`` (same philosophy as
  :mod:`pipeline.macro_echo`); a miss raises :class:`CSharpEmitError` at
  generation time.

The VBA package remains the canonical build path; ``macros_csharp/README.md``
says so in the output itself.

Public entry point: :func:`generate_csharp_package`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

log = get_logger()

CSHARP_DIR_NAME = "macros_csharp"

# swLengthUnit_e values (same map as solidworks_builder.set_document_units).
_LENGTH_UNIT = {"mm": 0, "cm": 1, "inch": 3}


class CSharpEmitError(Exception):
    """The emitted C# does not carry a build-plan literal it must carry."""


def _num(v: float) -> str:
    """A C# double literal, same %.6g precision as the VBA emitter."""
    s = f"{float(v):.6g}"
    return s


def _cs_str(text: str, limit: int = 160) -> str:
    """A safe C# string literal body (quotes/backslashes escaped, one line)."""
    t = (text or "").replace("\\", "/").replace('"', "'")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def _method_name(step) -> str:
    stem = Path(step.macro_file).stem if step.macro_file else f"{step.seq:02d}_{step.feature_id}"
    return "Step_" + re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_")


# --------------------------------------------------------------------------- #
# Per-step method bodies
# --------------------------------------------------------------------------- #
def _emit_setup(step, model, part_name: str) -> str:
    unit = _LENGTH_UNIT.get(str(model.units.value if hasattr(model.units, "value")
                                else model.units).lower(), 3)
    return f"""        // New part, units, save-as (mirrors 00_setup.vba).
        if (!sw.NewPart()) {{ sw.Fail("00_setup", "Could not create a new part document."); return; }}
        sw.SetLengthUnit({unit});  // swLengthUnit_e: 0=mm 1=cm 3=inch
        sw.SavePartAs("{_cs_str(part_name)}.sldprt");
"""


def _emit_profile(step) -> tuple[str, bool]:
    """Sketch-profile lines from the step's own dimensions/positions.
    Returns (code, ok)."""
    dims = step.dimensions or {}
    pos = step.positions_xy or []
    diameter = dims.get("diameter") or dims.get("hole_diameter")
    length = dims.get("length") or dims.get("width")
    width = dims.get("width") or dims.get("length")
    if diameter and step.feature_type in ("hole", "thread"):
        if not pos:
            return "", False
        lines = [f"        sw.CreateCircle({_num(x)}, {_num(y)}, {_num(diameter)});"
                 for x, y in ((p[0], p[1]) for p in pos)]
        return "\n".join(lines) + "\n", True
    if diameter:
        cx, cy = (pos[0][0], pos[0][1]) if pos else (0.0, 0.0)
        return f"        sw.CreateCircle({_num(cx)}, {_num(cy)}, {_num(diameter)});\n", True
    if length and width:
        cx, cy = (pos[0][0], pos[0][1]) if pos else (0.0, 0.0)
        return (f"        sw.CreateCornerRect({_num(cx)}, {_num(cy)}, "
                f"{_num(length)}, {_num(width)});\n"), True
    return "", False


def _emit_solid_step(step, is_cut: bool) -> str:
    name = _cs_str(f"{step.seq:02d}_{step.feature_id}", 60)
    profile, ok = _emit_profile(step)
    if not ok:
        return _emit_manual(step, "no scriptable profile (diameter or length+width) "
                                  "in the extracted data — build manually")
    plane = _cs_str(step.sketch_plane or "Front Plane", 40)
    depth = (step.dimensions or {}).get("depth")
    thru = (step.depth_type == "through_all") or (is_cut and depth is None)
    depth_expr = _num(depth) if depth is not None else "0"
    feat_name = _cs_str(f"{step.feature_id}_{Path(step.macro_file).stem.split('_', 2)[-1]}", 60)
    op = (f"sw.CutFeature(\"{name}\", \"{feat_name}\", {str(thru).lower()}, {depth_expr})"
          if is_cut else
          f"sw.BossFeature(\"{name}\", \"{feat_name}\", {depth_expr})")
    return f"""        // {_cs_str(step.description)}
        if (!sw.SelectPlane("{plane}", 1)) {{ sw.Fail("{name}", "Could not select {plane}."); return; }}
        sw.InsertSketch();
{profile}        sw.CloseSketch("{name}");
        if (!{op}) {{ sw.Fail("{name}", "Feature creation returned null — check the sketch."); return; }}
"""


def _emit_slot_rect(step) -> str:
    name = _cs_str(f"{step.seq:02d}_{step.feature_id}", 60)
    corners = step.positions_xy_meters or []
    if len(corners) < 4:
        return _emit_manual(step, "slot rectangle has no 4-corner record — build manually")
    plane = _cs_str(step.sketch_plane or "Front Plane", 40)
    lines = []
    for i in range(4):
        x1, y1 = corners[i][0], corners[i][1]
        x2, y2 = corners[(i + 1) % 4][0], corners[(i + 1) % 4][1]
        lines.append(f"        sw.CreateLineMeters({_num(x1)}, {_num(y1)}, "
                     f"{_num(x2)}, {_num(y2)});")
    thru = step.depth_type != "blind"
    depth_m = (step.dimensions_meters or {}).get("depth", 0.0)
    feat_name = _cs_str(f"{step.feature_id}_slot_rect", 60)
    return f"""        // {_cs_str(step.description)} (canonical slot rectangle; meters literals)
        if (!sw.SelectPlane("{plane}", 1)) {{ sw.Fail("{name}", "Could not select {plane}."); return; }}
        sw.InsertSketch();
{chr(10).join(lines)}
        sw.CloseSketch("{name}");
        if (!sw.CutFeatureMeters("{name}", "{feat_name}", {str(thru).lower()}, {_num(depth_m)}))
        {{ sw.Fail("{name}", "Slot rectangle cut returned null."); return; }}
"""


def _emit_circular_pattern(step) -> str:
    cp = step.circular_pattern or {}
    name = _cs_str(f"{step.seq:02d}_{step.feature_id}", 60)
    axis = _cs_str((cp.get("pattern_axis") or {}).get("axis_name", ""), 40)
    seed = _cs_str(cp.get("seed_feature_name", ""), 60)
    total = int(cp.get("total_instances", 0) or 0)
    angle = float(cp.get("total_angle_deg", 360.0) or 360.0)
    if not axis or not seed or total < 2:
        return _emit_manual(step, "incomplete circular-pattern spec — apply manually")
    return f"""        // {_cs_str(step.description)}
        // total_instances INCLUDES the seed ({total} = seed + {total - 1} copies).
        // The named axis "{axis}" is created by the VBA reference-axis macro (bore-face
        // selection is interactive-grade); create it first if it does not exist yet.
        if (!sw.CircularPattern("{name}", "{axis}", "{seed}", {total}, {_num(angle)},
            "{_cs_str(step.feature_id, 40)}_Pattern"))
        {{ sw.Fail("{name}", "Circular pattern returned null (axis/seed selection)."); return; }}
"""


def _emit_manual(step, reason: str) -> str:
    name = _cs_str(f"{step.seq:02d}_{step.feature_id}", 60)
    return (f"        // MANUAL STEP — no geometry is created here.\n"
            f"        sw.Log(\"WARN\", \"{name}\", \"{_cs_str(step.description)} — "
            f"{_cs_str(reason)}\");\n")


def _emit_verify(step, part_name: str) -> str:
    return f"""        sw.Rebuild();
        sw.VerifySolidBody("ZZ_final_verify");
        sw.SavePartAs("{_cs_str(part_name)}.sldprt");
"""


def _emit_export(step) -> str:
    return """        sw.ExportStl("ZZZ_export_stl");
"""


def _emit_step_method(step, model, part_name: str) -> tuple[str, str]:
    """(method_name, full method text) for one build step."""
    m = _method_name(step)
    ft = step.feature_type
    if step.status == "skipped_prohibited":
        body = _emit_manual(step, "prohibited/unsupported feature type "
                                  "(same MANUAL contract as the VBA package)")
    elif ft == "setup":
        body = _emit_setup(step, model, part_name)
    elif ft == "reference_geometry":
        body = _emit_manual(step, "datum skeleton is built by 01a_reference_geometry.vba "
                                  "(named planes/axes/points); additive landmarks only")
    elif ft == "reference_axis":
        body = _emit_manual(step, "reference axis from the bore's cylindrical face — run "
                                  "the numbered VBA axis macro (face traversal) first")
    elif ft in ("extrude_boss",):
        body = _emit_solid_step(step, is_cut=False)
    elif ft in ("extrude_cut",):
        body = _emit_solid_step(step, is_cut=True)
    elif ft in ("hole", "thread"):
        body = _emit_solid_step(step, is_cut=True)
    elif ft == "slot_rect_cut":
        body = _emit_slot_rect(step)
    elif ft == "circular_pattern":
        body = _emit_circular_pattern(step)
    elif ft in ("slot_corner_fillet", "fillet", "chamfer", "fillet/chamfer"):
        body = _emit_manual(step, "interactive edge selection (values baked into the "
                                  "matching VBA macro) — apply in SolidWorks")
    elif ft == "verify":
        body = _emit_verify(step, part_name)
    elif ft == "export":
        body = _emit_export(step)
    else:  # revolve/mirror/pattern skeletons and any future types: never silent
        body = _emit_manual(step, f"'{ft}' step is not scripted in the C# output — "
                                  "see the matching VBA macro")
    text = (f"    // ---- {step.macro_file or m}: {_cs_str(step.description)} ----\n"
            f"    private static void {m}(SwBuild sw)\n    {{\n{body}    }}\n")
    return m, text


# --------------------------------------------------------------------------- #
# File assembly
# --------------------------------------------------------------------------- #
def _program_cs(model, pkg, part_name: str) -> str:
    methods: list[str] = []
    calls: list[str] = []
    seen: set[str] = set()
    for step in pkg.steps:
        if step.feature_type == "run_all":
            continue  # Program.Main IS the run-all
        m, text = _emit_step_method(step, model, part_name)
        if m in seen:  # one method per macro file (skipped fillet twins share files)
            continue
        seen.add(m)
        methods.append(text)
        calls.append(f"            {m}(sw);")
    nl = "\n"
    return f"""// ============================================================
// MTI 2D->3D pipeline — C# build program for part {part_name}
// Generated from the SAME build plan as the VBA macros (macros/);
// the VBA package remains the canonical build path.
// Late-bound COM: no SolidWorks interop references required.
// Run from this folder so ../logs and the part save path resolve.
// ============================================================
using System;

internal static class Program
{{
    private static int Main(string[] args)
    {{
        SwBuild sw;
        try {{ sw = SwBuild.Connect(); }}
        catch (Exception ex)
        {{
            Console.Error.WriteLine("Could not connect to SolidWorks: " + ex.Message);
            return 2;
        }}
        try
        {{
{nl.join(calls)}
        }}
        catch (Exception ex)
        {{
            sw.Log("FAIL", "RUN_ALL", ex.Message);
            return 1;
        }}
        return sw.HasFailure ? 1 : 0;
    }}

{nl.join(methods)}}}
"""


def _helpers_cs(unit_factor: float) -> str:
    return f"""// SwBuild — late-bound SolidWorks helpers shared by every step.
// Mirrors the VBA helper block (LogResult / WriteMacroResult / VerifySolidBody /
// SelectRefPlane / CreateCircularPatternSafe) and the verified COM-builder calls
// in pipeline/solidworks_builder.py. Enum values are documented numerics:
//   swEndConditions_e: 0=Blind 1=ThroughAll     swBodyType_e: 0=Solid
//   swUserPreferenceStringValue_e: 8=DefaultTemplatePart
using System;
using System.IO;
using System.Runtime.InteropServices;

internal sealed class SwBuild
{{
    // SolidWorks API works in METERS: drawing values are written value * UNIT_FACTOR.
    public const double UNIT_FACTOR = {unit_factor!r};

    private readonly dynamic _app;
    private dynamic _model;
    public bool HasFailure {{ get; private set; }}

    private SwBuild(dynamic app) {{ _app = app; }}

    public static SwBuild Connect()
    {{
        dynamic app;
        try {{ app = Marshal.GetActiveObject("SldWorks.Application"); }}
        catch (COMException)
        {{
            Type t = Type.GetTypeFromProgID("SldWorks.Application");
            if (t == null) throw new InvalidOperationException("SolidWorks is not installed.");
            app = Activator.CreateInstance(t);
        }}
        app.Visible = true;
        return new SwBuild(app);
    }}

    // ---- logging: same files the VBA macros write, under ../logs ----
    private static string LogPath(string name)
    {{
        string dir = Path.GetFullPath(Path.Combine(Environment.CurrentDirectory, "..", "logs"));
        Directory.CreateDirectory(dir);
        return Path.Combine(dir, name);
    }}

    public void Log(string status, string step, string detail)
    {{
        try
        {{
            File.AppendAllText(LogPath("build_log_cs.txt"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  [" + status + "]  "
                + step + (detail.Length > 0 ? "  -- " + detail : "") + Environment.NewLine);
        }}
        catch (IOException) {{ }}
        Console.WriteLine("[" + status + "] " + step + "  " + detail);
    }}

    public void WriteMacroResult(string feature, string status, string detail)
    {{
        try
        {{
            File.AppendAllText(LogPath("macro_result.json"),
                "{{\\"feature\\": \\"" + feature + "\\", \\"status\\": \\"" + status
                + "\\", \\"detail\\": \\"" + detail.Replace('\\\\', '/').Replace('"', '\\'')
                + "\\"}}" + Environment.NewLine);
        }}
        catch (IOException) {{ }}
    }}

    public void Fail(string step, string detail)
    {{
        HasFailure = true;
        Log("FAIL", step, detail);
        WriteMacroResult(step, "FAIL", detail);
    }}

    // ---- document lifecycle ----
    public bool NewPart()
    {{
        string template = _app.GetUserPreferenceStringValue(8); // swDefaultTemplatePart
        if (string.IsNullOrEmpty(template)) return false;
        _model = _app.NewDocument(template, 0, 0, 0);
        return _model != null;
    }}

    public void SetLengthUnit(int lengthUnit)
    {{
        // SetUnits(lengthUnit, thousandsDelimiter, decimalPlaces, fractionDenominator, roundToFraction)
        try {{ _model.SetUnits(lengthUnit, 0, 4, 2, false); }}
        catch (COMException) {{ Log("WARN", "units", "SetUnits rejected — continuing."); }}
    }}

    public void SavePartAs(string fileName)
    {{
        string path = Path.GetFullPath(Path.Combine(Environment.CurrentDirectory, "..", fileName));
        int ok = _model.SaveAs3(path, 0, 0);
        Log(ok == 0 ? "PASS" : "WARN", "save", "SaveAs3 -> " + path + " (code " + ok + ")");
    }}

    public void Rebuild()
    {{
        try {{ _model.EditRebuild3(); }} catch (COMException) {{ }}
    }}

    // ---- sketching (drawing units unless noted) ----
    public bool SelectPlane(string planeName, int planeIndex)
    {{
        _model.ClearSelection2(true);
        string[] tries = {{ planeName, planeName.Replace(" Plane", ""), "Plane" + planeIndex }};
        foreach (string name in tries)
        {{
            try
            {{
                if ((bool)_model.Extension.SelectByID2(name, "PLANE", 0, 0, 0, false, 0, null, 0))
                    return true;
            }}
            catch (COMException) {{ }}
        }}
        // Fallback: planeIndex-th reference plane in the tree (template order),
        // selected by object — needs no name and no Callout argument.
        dynamic feat = _model.FirstFeature();
        int n = 0;
        while (feat != null)
        {{
            if ((string)feat.GetTypeName2() == "RefPlane" && ++n == planeIndex)
            {{
                _model.ClearSelection2(true);
                return (bool)feat.Select2(false, 0);
            }}
            feat = feat.GetNextFeature();
        }}
        return false;
    }}

    public void InsertSketch() {{ _model.SketchManager.InsertSketch(true); }}

    public void CreateCircle(double cx, double cy, double dia)
    {{
        _model.SketchManager.CreateCircleByRadius(
            cx * UNIT_FACTOR, cy * UNIT_FACTOR, 0.0, (dia / 2.0) * UNIT_FACTOR);
    }}

    public void CreateCornerRect(double cx, double cy, double len, double wid)
    {{
        _model.SketchManager.CreateCornerRectangle(
            cx * UNIT_FACTOR, cy * UNIT_FACTOR, 0.0,
            (cx + len) * UNIT_FACTOR, (cy + wid) * UNIT_FACTOR, 0.0);
    }}

    public void CreateLineMeters(double x1, double y1, double x2, double y2)
    {{
        _model.SketchManager.CreateLine(x1, y1, 0.0, x2, y2, 0.0);
    }}

    public void CloseSketch(string step)
    {{
        try {{ _model.SketchManager.FullyDefineSketch(true, true, 0, true, 1, null, 1, null, 0, 0); }}
        catch (COMException) {{ }}
        _model.ClearSelection2(true);
    }}

    // ---- features (argument lists mirror pipeline/solidworks_builder.py) ----
    public bool BossFeature(string step, string featureName, double depthDrawing)
    {{
        _model.SketchManager.InsertSketch(true); // close the sketch
        double depth = depthDrawing * UNIT_FACTOR;
        dynamic feat = _model.FeatureManager.FeatureExtrusion3(
            true, false, false, 0, 0, depth, 0.01,
            false, false, false, false, 0.0, 0.0,
            false, false, false, false,
            true, true, true, 0, 0, false);
        return FinishFeature(step, featureName, feat);
    }}

    public bool CutFeature(string step, string featureName, bool throughAll, double depthDrawing)
    {{
        return CutFeatureMeters(step, featureName, throughAll, depthDrawing * UNIT_FACTOR);
    }}

    public bool CutFeatureMeters(string step, string featureName, bool throughAll, double depthMeters)
    {{
        int end = throughAll ? 1 : 0; // swEndCondThroughAll : swEndCondBlind
        dynamic feat = Cut4(true, end, depthMeters);
        if (feat == null) feat = Cut4(false, end, depthMeters); // direction-flip retry
        return FinishFeature(step, featureName, feat);
    }}

    private dynamic Cut4(bool flip, int end, double depthMeters)
    {{
        try
        {{
            return _model.FeatureManager.FeatureCut4(
                true, false, flip, end, 0, depthMeters, 0.01,
                false, false, false, false, 0.0, 0.0,
                false, false, false, false, false,
                true, true, true, true, false, 0, 0, false, false);
        }}
        catch (COMException) {{ return null; }}
    }}

    public bool CircularPattern(string step, string axisName, string seedName,
                                int totalInstances, double totalAngleDeg, string newName)
    {{
        double spacingRad = totalAngleDeg * Math.PI / 180.0;
        _model.ClearSelection2(true);
        // Selection contract: axis Mark=1, seed feature Mark=4 ("BODYFEATURE").
        if (!(bool)_model.Extension.SelectByID2(axisName, "AXIS", 0, 0, 0, false, 1, null, 0))
        {{ Log("FAIL", step, "Could not select pattern axis '" + axisName + "' (Mark=1)."); return false; }}
        if (!(bool)_model.Extension.SelectByID2(seedName, "BODYFEATURE", 0, 0, 0, true, 4, null, 0))
        {{ Log("FAIL", step, "Could not select seed feature '" + seedName + "' (Mark=4)."); return false; }}
        dynamic feat = null;
        try
        {{
            feat = _model.FeatureManager.FeatureCircularPattern5(
                totalInstances, spacingRad, false, "NULL", false, true, false,
                false, false, false, 1, spacingRad, "NULL", false);
        }}
        catch (COMException) {{ }}
        if (feat == null)
        {{
            try
            {{
                feat = _model.FeatureManager.FeatureCircularPattern4(
                    totalInstances, spacingRad, false, "NULL", false, true, false);
            }}
            catch (COMException) {{ }}
        }}
        return FinishFeature(step, newName, feat);
    }}

    private bool FinishFeature(string step, string featureName, dynamic feat)
    {{
        if (feat == null) return false;
        try {{ feat.Name = featureName; }} catch (COMException) {{ }}
        if (!VerifySolidBody(step)) return false;
        Log("PASS", step, "Created feature " + featureName);
        WriteMacroResult(featureName, "PASS", "");
        return true;
    }}

    public bool VerifySolidBody(string step)
    {{
        object bodies = _model.GetBodies2(0, true); // swSolidBody = 0
        var arr = bodies as object[];
        if (arr == null || arr.Length == 0)
        {{
            Log("FAIL", step, "No solid body present after feature");
            return false;
        }}
        return true;
    }}

    public void ExportStl(string step)
    {{
        string path = (string)_model.GetPathName();
        if (string.IsNullOrEmpty(path))
        {{
            Fail(step, "No saved path - cannot derive STL name");
            return;
        }}
        int dot = path.LastIndexOf('.');
        string stl = (dot > 0 ? path.Substring(0, dot) : path) + ".stl";
        int ok = _model.SaveAs3(stl, 0, 0);
        Log(ok == 0 ? "PASS" : "WARN", step, "STL -> " + stl);
    }}
}}
"""


_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <!-- Late-bound COM build program: no SolidWorks interop references needed. -->
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net48</TargetFramework>
    <LangVersion>latest</LangVersion>
    <RootNamespace>MtiBuild</RootNamespace>
    <AssemblyName>BuildPart</AssemblyName>
  </PropertyGroup>
  <ItemGroup>
    <Reference Include="Microsoft.CSharp" />
  </ItemGroup>
</Project>
"""

_README = """# C# build program — {name}

C# companion to the VBA macros in `../macros/` (which remain the **canonical**
build path). Both are generated from the same build plan, so every coordinate
and dimension literal is identical between the two outputs.

## Run

Requires Windows, SolidWorks, and the .NET SDK (or Visual Studio):

```powershell
cd macros_csharp
dotnet build
dotnet run          # or run bin\\Debug\\net48\\BuildPart.exe FROM THIS FOLDER
```

Run it **from this folder**: log files (`../logs/build_log_cs.txt`,
`../logs/macro_result.json`) and the part save path (`../{name}.sldprt`)
resolve relative to the working directory, exactly like the VBA macros.

## What is and is not scripted

* Base extrudes, profile cuts, holes/threads, slot rectangles, and circular
  patterns are fully scripted (late-bound COM, mirrors the pipeline's proven
  `FeatureExtrusion3` / `FeatureCut4` / `FeatureCircularPattern5` calls).
* Interactive steps stay interactive, same as VBA: fillets/chamfers, slot
  corner fillets, the reference-geometry datum skeleton, and the
  bore-face-derived pattern axis are logged as WARN/MANUAL steps — run the
  matching numbered VBA macro or apply them in SolidWorks.
* No SolidWorks interop DLLs are referenced; the program late-binds to
  `SldWorks.Application`, so it works with any installed SolidWorks version.
"""


# --------------------------------------------------------------------------- #
# Self-echo: emitted C# must carry every in-scope position literal
# --------------------------------------------------------------------------- #
_ECHO_TYPES = frozenset({"hole", "thread", "extrude_boss", "extrude_cut"})


def _self_echo_check(pkg, program_text: str) -> int:
    """Every in-scope step's positions must appear in Program.cs as the exact
    literals the emitter formats. Raises :class:`CSharpEmitError` on a miss."""
    checked = 0
    for step in pkg.steps:
        if step.feature_type not in _ECHO_TYPES or step.status != "generated":
            continue
        profile_ok = _emit_profile(step)[1]
        if not profile_ok:
            continue  # emitted as a logged MANUAL step; no literals expected
        for p in (step.positions_xy or []):
            checked += 1
            if _num(p[0]) not in program_text or _num(p[1]) not in program_text:
                raise CSharpEmitError(
                    f"{step.feature_id}: planned position ({p[0]}, {p[1]}) was never "
                    f"emitted into Program.cs — C# output would drop geometry.")
    return checked


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def generate_csharp_package(model, pkg, unit_factor: float) -> Path:
    """Emit the ``macros_csharp/`` companion package next to ``macros/``.

    Deterministic (no timestamps/absolute paths), written from the same
    :class:`BuildStep` data as the VBA. Returns the directory written."""
    out_dir = Path(pkg.root) / CSHARP_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.cs"):  # same stale-file dedup rule as macros/
        try:
            old.unlink()
        except OSError:
            pass

    part_name = Path(pkg.build_plan_json).name.replace("_build_plan.json", "")
    program = _program_cs(model, pkg, part_name)
    checked = _self_echo_check(pkg, program)

    (out_dir / "Program.cs").write_text(program, encoding="utf-8")
    (out_dir / "SwBuildHelpers.cs").write_text(_helpers_cs(unit_factor), encoding="utf-8")
    (out_dir / "BuildPart.csproj").write_text(_CSPROJ, encoding="utf-8")
    (out_dir / "README.md").write_text(_README.format(name=part_name), encoding="utf-8")
    log.info("C# macro package written to %s (%d position literal(s) echo-checked)",
             out_dir, checked)
    return out_dir
