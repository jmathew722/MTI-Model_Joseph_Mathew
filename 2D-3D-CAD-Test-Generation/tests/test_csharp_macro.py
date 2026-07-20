"""C# macro output (pipeline/csharp_macro.py).

The macros_csharp/ companion package is generated from the SAME BuildStep data
as the VBA, deterministic, self-echo-checked, and never touches macros/ (the
golden snapshot stays byte-identical). No SolidWorks needed — these tests only
inspect the emitted source text.
"""
import pytest

from pipeline.csharp_macro import (
    CSharpEmitError,
    _self_echo_check,
    generate_csharp_package,
)
from pipeline.macro_generator import generate_macro_package
from pipeline.validator import format_verification_report, run_verification
from tests.test_golden_macros import _golden_drawing


@pytest.fixture()
def built(tmp_path):
    data = _golden_drawing()
    model, report = run_verification(data)
    assert report.ok, str(report)
    pkg = generate_macro_package(model, data, format_verification_report(model, report), tmp_path)
    return model, pkg


def test_package_files_written(built):
    model, pkg = built
    cs_dir = pkg.root / "macros_csharp"
    assert cs_dir.is_dir()
    names = sorted(p.name for p in cs_dir.iterdir())
    assert names == ["BuildPart.csproj", "Program.cs", "README.md", "SwBuildHelpers.cs"]
    # The VBA macros folder is untouched by the C# emission (golden safety).
    assert not list(pkg.macros_dir.glob("*.cs"))


def test_program_carries_the_same_geometry_literals(built):
    """Every hole center and the base-plate profile from the build plan must be
    emitted into Program.cs (dual-output agreement)."""
    model, pkg = built
    text = (pkg.root / "macros_csharp" / "Program.cs").read_text(encoding="utf-8")
    # Golden mounting holes: 4 centers, dia 0.25, at y=1 spaced 1.0 in x.
    for cx in ("0.5", "1.5", "2.5", "3.5"):
        assert f"sw.CreateCircle({cx}, 1, 0.25);" in text
    # Base plate: 4.0 x 2.0 rectangle, corner at the origin.
    assert "sw.CreateCornerRect(0, 0, 4, 2);" in text
    # Holes are thru → ThroughAll cut; boss carries its 0.5 depth.
    assert "true, 0)" in text or "true, 0);" in text  # CutFeature(..., thru, 0)
    assert "sw.BossFeature(" in text and "0.5" in text


def test_manual_steps_stay_manual_never_silent(built):
    model, pkg = built
    text = (pkg.root / "macros_csharp" / "Program.cs").read_text(encoding="utf-8")
    # The golden shell is prohibited → logged MANUAL step, and the fillet step
    # is interactive → logged WARN; neither disappears from the C# output.
    assert text.count("MANUAL STEP") >= 1
    assert 'sw.Log("WARN"' in text
    assert "F004" in text  # the shell feature is named, not dropped


def test_program_structure_and_helpers(built):
    model, pkg = built
    cs_dir = pkg.root / "macros_csharp"
    program = (cs_dir / "Program.cs").read_text(encoding="utf-8")
    helpers = (cs_dir / "SwBuildHelpers.cs").read_text(encoding="utf-8")
    # Balanced braces in both files (cheap structural sanity for generated C#).
    assert program.count("{") == program.count("}")
    assert helpers.count("{") == helpers.count("}")
    # Late binding, not interop references.
    assert 'Type.GetTypeFromProgID("SldWorks.Application")' in helpers
    assert "SolidWorks.Interop" not in helpers
    # The verified feature calls and their enum conventions are present.
    assert "FeatureExtrusion3" in helpers
    assert "FeatureCut4" in helpers
    assert "FeatureCircularPattern5" in helpers
    assert "throughAll ? 1 : 0" in helpers  # swEndCondThroughAll : swEndCondBlind
    # Inch part → UNIT_FACTOR 0.0254 baked into the helpers.
    assert "UNIT_FACTOR = 0.0254" in helpers
    # csproj targets .NET Framework 4.8 with the dynamic-binder reference.
    csproj = (cs_dir / "BuildPart.csproj").read_text(encoding="utf-8")
    assert "<TargetFramework>net48</TargetFramework>" in csproj
    assert '<Reference Include="Microsoft.CSharp" />' in csproj
    # README names the canonical path.
    readme = (cs_dir / "README.md").read_text(encoding="utf-8")
    assert "canonical" in readme


def test_emission_is_deterministic(built, tmp_path):
    model, pkg = built
    first = (pkg.root / "macros_csharp" / "Program.cs").read_text(encoding="utf-8")
    generate_csharp_package(model, pkg, 0.0254)
    second = (pkg.root / "macros_csharp" / "Program.cs").read_text(encoding="utf-8")
    assert first == second


def test_self_echo_rejects_dropped_position(built):
    model, pkg = built
    program = (pkg.root / "macros_csharp" / "Program.cs").read_text(encoding="utf-8")
    # Remove one hole circle line → the echo check must name the drop.
    broken = program.replace("sw.CreateCircle(2.5, 1, 0.25);", "")
    with pytest.raises(CSharpEmitError, match="F002"):
        _self_echo_check(pkg, broken)
    # And the intact program passes with all positions checked.
    assert _self_echo_check(pkg, program) >= 4
