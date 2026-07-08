' ============================================================
' RUN_ALL - build the entire part in one run (ordered)
' Part: A050211E
' Paste this whole file into a new SolidWorks macro (Alt+F11) and press F5
' ONCE. It runs every step in build order; a failing step stops the run
' and reports which step failed (see ..\logs\build_log.txt).
' SolidWorks API works in METERS: values are written as value * UNIT_FACTOR.
' ============================================================
Option Explicit

Const UNIT_FACTOR As Double = 0.0254

Dim swApp As SldWorks.SldWorks
Dim swModel As SldWorks.ModelDoc2
Dim boolstatus As Boolean

' --- Append a PASS/FAIL line to ..\logs\build_log.txt next to the macros folder ---
Sub LogResult(status As String, step As String, detail As String)
    On Error Resume Next
    Dim macroPath As String, logPath As String, f As Integer
    macroPath = swApp.GetCurrentMacroPathName
    logPath = Left$(macroPath, InStrRev(macroPath, "\")) & "..\logs\build_log.txt"
    f = FreeFile
    Open logPath For Append As #f
    Print #f, Format$(Now, "yyyy-mm-dd hh:nn:ss") & "  [" & status & "]  " & step & _
        IIf(Len(detail) > 0, "  -- " & detail, "")
    Close #f
    On Error GoTo 0
End Sub

' --- Verify a solid body exists; log and report its bounding box ---
Function VerifySolidBody(step As String) As Boolean
    Dim swPart As SldWorks.PartDoc
    Dim vBodies As Variant
    Set swPart = swModel
    vBodies = swPart.GetBodies2(swBodyType_e.swSolidBody, True)
    If IsEmpty(vBodies) Then
        VerifySolidBody = False
        LogResult "FAIL", step, "No solid body present after feature"
    Else
        ' Bounding box read from the solid body itself (IBody2::GetBodyBox) -
        ' ModelDoc2 exposes no whole-model bounding-box call in VBA.
        Dim swBody As SldWorks.Body2
        Dim vBox As Variant
        Set swBody = vBodies(0)
        vBox = swBody.GetBodyBox
        LogResult "PASS", step, "Solid body OK; bbox(drawing units) " & _
            Format$((vBox(3) - vBox(0)) / UNIT_FACTOR, "0.000") & " x " & _
            Format$((vBox(4) - vBox(1)) / UNIT_FACTOR, "0.000") & " x " & _
            Format$((vBox(5) - vBox(2)) / UNIT_FACTOR, "0.000")
        VerifySolidBody = True
    End If
End Function

' --- Append a machine-readable result line to ..\logs\macro_result.json (JSON Lines) ---
' Every feature-creation outcome is recorded here (feature name -> success/fail)
' so the web UI / FastAPI side can surface the EXACT failing feature instead of a
' generic pipeline exit code.
Sub WriteMacroResult(featureName As String, status As String, detail As String)
    On Error Resume Next
    Dim macroPath As String, p As String, f As Integer, q As String
    q = Chr$(34)
    macroPath = swApp.GetCurrentMacroPathName
    p = Left$(macroPath, InStrRev(macroPath, "\")) & "..\logs\macro_result.json"
    f = FreeFile
    Open p For Append As #f
    Print #f, "{" & q & "feature" & q & ": " & q & featureName & q & ", " & _
        q & "status" & q & ": " & q & status & q & ", " & _
        q & "detail" & q & ": " & q & Replace(Replace(detail, "\", "/"), q, "'") & q & "}"
    Close #f
    On Error GoTo 0
End Sub

' --- Create a circular pattern with the exact selection contract the API requires ---
' Signature pulled from the INSTALLED SolidWorks type library (sldworks.tlb,
' IFeatureManager::FeatureCircularPattern5, dispid 261; see the local API help
' topic "FeatureCircularPattern5 Method (IFeatureManager)"):
'   FeatureCircularPattern5(Number As Long, Spacing As Double, FlipDirection As Boolean,
'     DName As String, GeometryPattern As Boolean, EqualSpacing As Boolean,
'     VaryInstance As Boolean, SyncSubAssemblies As Boolean, BDir2 As Boolean,
'     BSymmetric As Boolean, Number2 As Long, Spacing2 As Double, DName2 As String,
'     EqualSpacing2 As Boolean)
' Conventions asserted ONCE here, never re-interpreted downstream:
'   * Number (totalInstances) INCLUDES the seed: 6 = seed + 5 copies.
'   * Spacing is the TOTAL angle in RADIANS when EqualSpacing=True.
' Selection contract (a wrong/missing mark = silent Nothing return):
'   pattern axis  -> SelectByID2 ... Mark:=1
'   seed feature  -> SelectByID2 ... Mark:=4 (type "BODYFEATURE", exact tree name)
Function CreateCircularPatternSafe(axisName As String, seedName As String, _
        totalInstances As Integer, totalAngleDeg As Double, reverseDir As Boolean, _
        geometryPattern As Boolean, varySketch As Boolean, newName As String, _
        stepName As String) As Boolean
    Dim swFeat As SldWorks.Feature
    Dim spacingRad As Double
    spacingRad = totalAngleDeg * 4# * Atn(1#) / 180#
    swModel.ClearSelection2 True
    If Not swModel.Extension.SelectByID2(axisName, "AXIS", 0, 0, 0, False, 1, Nothing, 0) Then
        LogResult "FAIL", stepName, "Could not select pattern axis '" & axisName & "' (Mark=1)"
        Exit Function
    End If
    If Not swModel.Extension.SelectByID2(seedName, "BODYFEATURE", 0, 0, 0, True, 4, Nothing, 0) Then
        LogResult "FAIL", stepName, "Could not select seed feature '" & seedName & "' (Mark=4)"
        Exit Function
    End If
    On Error Resume Next
    Set swFeat = swModel.FeatureManager.FeatureCircularPattern5( _
        totalInstances, spacingRad, reverseDir, "NULL", geometryPattern, True, varySketch, _
        False, False, False, 1, spacingRad, "NULL", False)
    On Error GoTo 0
    If swFeat Is Nothing Then
        ' Older-release fallback: FeatureCircularPattern4 (same leading 7 arguments).
        On Error Resume Next
        Set swFeat = swModel.FeatureManager.FeatureCircularPattern4( _
            totalInstances, spacingRad, reverseDir, "NULL", geometryPattern, True, varySketch)
        On Error GoTo 0
    End If
    If swFeat Is Nothing Then Exit Function
    ' Name the pattern feature immediately so downstream selections never depend
    ' on SolidWorks' auto-numbering (CirPattern1 vs CirPattern2 drift).
    swFeat.Name = newName
    CreateCircularPatternSafe = True
End Function

' --- Select a reference plane robustly (plane names vary by template / language) ---
Function SelectRefPlane(planeName As String, planeIndex As Integer) As Boolean
    Dim tries As Variant, i As Integer
    swModel.ClearSelection2 True
    tries = Array(planeName, Replace(planeName, " Plane", ""), "Plane" & planeIndex)
    For i = LBound(tries) To UBound(tries)
        If swModel.Extension.SelectByID2(CStr(tries(i)), "PLANE", 0, 0, 0, False, 0, Nothing, 0) Then
            SelectRefPlane = True
            Exit Function
        End If
    Next i
    ' Fallback: planeIndex-th reference plane in the feature tree (template order).
    Dim feat As SldWorks.Feature, n As Integer
    Set feat = swModel.FirstFeature
    Do While Not feat Is Nothing
        If feat.GetTypeName2 = "RefPlane" Then
            n = n + 1
            If n = planeIndex Then
                swModel.ClearSelection2 True
                SelectRefPlane = feat.Select2(False, 0)
                Exit Function
            End If
        End If
        Set feat = feat.GetNextFeature
    Loop
    SelectRefPlane = False
End Function

' --- Find a Part template (.prtdot): configured folders first, then standard locations ---
Function FindPartTemplate(app As SldWorks.SldWorks) As String
    Dim dirs As String, parts() As String, i As Integer, p As String, hit As String
    ' Configured document-template folders (semicolon-separated), then common defaults.
    dirs = app.GetUserPreferenceStringValue(swUserPreferenceStringValue_e.swFileLocationsDocumentTemplates)
    dirs = dirs & ";C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2024\templates" & _
                  ";C:\ProgramData\SolidWorks\SOLIDWORKS 2024\templates" & _
                  ";C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2025\templates" & _
                  ";C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2023\templates"
    parts = Split(dirs, ";")
    For i = LBound(parts) To UBound(parts)
        p = Trim$(parts(i))
        If Len(p) > 0 Then
            If Right$(p, 1) <> "\" Then p = p & "\"
            If Dir(p & "Part.prtdot") <> "" Then
                FindPartTemplate = p & "Part.prtdot"
                Exit Function
            End If
            hit = Dir(p & "*.prtdot")
            If hit <> "" Then
                FindPartTemplate = p & hit
                Exit Function
            End If
        End If
    Next i
    FindPartTemplate = ""
End Function

Sub Step00_Setup()
    ' ---- CREATE NEW PART from a part template ----
    ' Prefer the configured default; if unset (common on fresh installs / VDI),
    ' auto-discover a Part.prtdot from the template folders.
    Dim templatePath As String
    templatePath = swApp.GetUserPreferenceStringValue(swUserPreferenceStringValue_e.swDefaultTemplatePart)
    If Len(templatePath) = 0 Or Dir(templatePath) = "" Then
        templatePath = FindPartTemplate(swApp)
    End If
    If Len(templatePath) = 0 Then
        MsgBox "No part template found - set Tools > Options > Default Templates > Parts.", vbCritical
        LogResult "FAIL", "00_setup", "No part template found - set Tools > Options > Default Templates > Parts."
        WriteMacroResult "00_setup", "FAIL", "No part template found - set Tools > Options > Default Templates > Parts."
        End
    End If
    Set swModel = swApp.NewDocument(templatePath, 0, 0, 0)
    If swModel Is Nothing Then
        MsgBox "NewDocument failed.", vbCritical
        LogResult "FAIL", "00_setup", "NewDocument failed."
        WriteMacroResult "00_setup", "FAIL", "NewDocument failed."
        End
    End If

    ' ---- UNITS: must be set BEFORE any geometry ----
    boolstatus = swModel.Extension.SetUserPreferenceInteger( _
        swUserPreferenceIntegerValue_e.swUnitSystem, _
        swUserPreferenceOption_e.swDetailingNoOptionSpecified, swUnitSystem_e.swUnitSystem_IPS)
    LogResult "PASS", "00_setup", "New part created; units set (inch)"

    ' ---- SAVE AS A050211E.sldprt (next to the macros folder) ----
    Dim macroPath As String, savePath As String
    Dim saveErrs As Long, saveWarns As Long
    macroPath = swApp.GetCurrentMacroPathName
    savePath = Left$(macroPath, InStrRev(macroPath, "\")) & "..\A050211E.sldprt"
    boolstatus = swModel.Extension.SaveAs(savePath, 0, _
        swSaveAsOptions_e.swSaveAsOptions_Silent, Nothing, saveErrs, saveWarns)
    If Not boolstatus Then
        LogResult "WARN", "00_setup", "Initial SaveAs failed (errs=" & saveErrs & ") - save manually"
    Else
        LogResult "PASS", "00_setup", "Saved " & savePath
    End If
End Sub

Sub Step01_F001()
    ' ---- PLANE SELECTION (Front Plane; name auto-detected) ----
    If Not SelectRefPlane("Front Plane", 1) Then
        MsgBox "Could not select Front Plane (no reference plane found).", vbCritical
        LogResult "FAIL", "01_F001", "Could not select Front Plane (no reference plane found)."
        WriteMacroResult "01_F001", "FAIL", "Could not select Front Plane (no reference plane found)."
        End
    End If

    ' ---- OPEN SKETCH ----
    swModel.SketchManager.InsertSketch True
    ' ---- SKETCH: circle dia 7.5 at (3.75, 3.75) drawing units ----
    swModel.SketchManager.CreateCircleByRadius 3.75 * UNIT_FACTOR, 3.75 * UNIT_FACTOR, 0#, (7.5 / 2#) * UNIT_FACTOR

    ' ---- FINALIZE SKETCH ----
    ' The feature call below consumes the ACTIVE sketch - this is exactly what
    ' SolidWorks' own macro recorder emits (ClearSelection2 then the feature
    ' call, sketch left open). No closing, no name-based reselection.
    On Error Resume Next
    swModel.SketchManager.FullyDefineSketch True, True, 0, True, 1, Nothing, 1, Nothing, 0, 0
    On Error GoTo 0
    swModel.ClearSelection2 True
    If swModel.SketchManager.ActiveSketch Is Nothing Then
        MsgBox "No active sketch to build the feature from.", vbCritical
        LogResult "FAIL", "01_F001", "No active sketch to build the feature from."
        WriteMacroResult "01_F001", "FAIL", "No active sketch to build the feature from."
        End
    End If

    ' ---- FEATURE ----
    Dim swFeat As SldWorks.Feature
    Set swFeat = swModel.FeatureManager.FeatureExtrusion3( _
        True, False, False, _
        swEndConditions_e.swEndCondBlind, swEndConditions_e.swEndCondBlind, _
        0.5 * UNIT_FACTOR, 0.01, _
        False, False, False, False, 0#, 0#, _
        False, False, False, False, _
        True, True, True, _
        swStartConditions_e.swStartSketchPlane, 0#, False)

    If swFeat Is Nothing Then
        MsgBox "Feature creation returned Nothing - check the sketch.", vbCritical
        LogResult "FAIL", "01_F001", "Feature creation returned Nothing - check the sketch."
        WriteMacroResult "01_F001", "FAIL", "Feature creation returned Nothing - check the sketch."
        End
    End If
    swFeat.Name = "F001_Base_round_plate"
    If Not VerifySolidBody("01_F001") Then
        MsgBox "No solid body after this feature.", vbCritical
        LogResult "FAIL", "01_F001", "No solid body after this feature."
        WriteMacroResult "01_F001", "FAIL", "No solid body after this feature."
        End
    End If
    LogResult "PASS", "01_F001", "Created feature F001_Base_round_plate"
    WriteMacroResult "F001_Base_round_plate", "PASS", ""
End Sub

Sub Step02_F002()
    ' ---- PLANE SELECTION (Front Plane; name auto-detected) ----
    If Not SelectRefPlane("Front Plane", 1) Then
        MsgBox "Could not select Front Plane (no reference plane found).", vbCritical
        LogResult "FAIL", "02_F002", "Could not select Front Plane (no reference plane found)."
        WriteMacroResult "02_F002", "FAIL", "Could not select Front Plane (no reference plane found)."
        End
    End If

    ' ---- OPEN SKETCH ----
    swModel.SketchManager.InsertSketch True
    ' ---- SKETCH: 1 hole(s) dia 3.88 (thru) ----
    swModel.SketchManager.CreateCircleByRadius 3.75 * UNIT_FACTOR, 3.75 * UNIT_FACTOR, 0#, (3.88 / 2#) * UNIT_FACTOR
    ' NOTE: Hole positions read from drawing.

    ' ---- FINALIZE SKETCH ----
    ' The feature call below consumes the ACTIVE sketch - this is exactly what
    ' SolidWorks' own macro recorder emits (ClearSelection2 then the feature
    ' call, sketch left open). No closing, no name-based reselection.
    On Error Resume Next
    swModel.SketchManager.FullyDefineSketch True, True, 0, True, 1, Nothing, 1, Nothing, 0, 0
    On Error GoTo 0
    swModel.ClearSelection2 True
    If swModel.SketchManager.ActiveSketch Is Nothing Then
        MsgBox "No active sketch to build the feature from.", vbCritical
        LogResult "FAIL", "02_F002", "No active sketch to build the feature from."
        WriteMacroResult "02_F002", "FAIL", "No active sketch to build the feature from."
        End
    End If

    ' ---- CUT ----
    Dim swFeat As SldWorks.Feature
    Set swFeat = swModel.FeatureManager.FeatureCut4( _
        True, False, False, _
        swEndConditions_e.swEndCondThroughAllBoth, swEndConditions_e.swEndCondBlind, _
        0#, 0.01, _
        False, False, False, False, 0#, 0#, _
        False, False, False, False, False, _
        True, True, True, True, False, _
        swStartConditions_e.swStartSketchPlane, 0#, False, False)
    If swFeat Is Nothing Then
        ' The cut may have missed the material (body on the other side of the
        ' sketch plane) - restore the profile sketch and retry, direction flipped.
        If swModel.SketchManager.ActiveSketch Is Nothing Then
            ' Sketch was consumed/closed by the failed attempt: select the most
            ' recent sketch feature in the tree (type "ProfileFeature") by object,
            ' never by name.
            Dim featRswFeat As SldWorks.Feature, lastSkswFeat As SldWorks.Feature
            Set featRswFeat = swModel.FirstFeature
            Do While Not featRswFeat Is Nothing
                If featRswFeat.GetTypeName2 = "ProfileFeature" Then Set lastSkswFeat = featRswFeat
                Set featRswFeat = featRswFeat.GetNextFeature
            Loop
            swModel.ClearSelection2 True
            If Not lastSkswFeat Is Nothing Then lastSkswFeat.Select2 False, 0
        End If
        Set swFeat = swModel.FeatureManager.FeatureCut4( _
            True, False, True, _
            swEndConditions_e.swEndCondThroughAll, swEndConditions_e.swEndCondBlind, _
            0#, 0.01, _
            False, False, False, False, 0#, 0#, _
            False, False, False, False, False, _
            True, True, True, True, False, _
            swStartConditions_e.swStartSketchPlane, 0#, False, False)
    End If

    If swFeat Is Nothing Then
        MsgBox "Feature creation returned Nothing - check the sketch.", vbCritical
        LogResult "FAIL", "02_F002", "Feature creation returned Nothing - check the sketch."
        WriteMacroResult "02_F002", "FAIL", "Feature creation returned Nothing - check the sketch."
        End
    End If
    swFeat.Name = "F002_Center_bore"
    If Not VerifySolidBody("02_F002") Then
        MsgBox "No solid body after this feature.", vbCritical
        LogResult "FAIL", "02_F002", "No solid body after this feature."
        WriteMacroResult "02_F002", "FAIL", "No solid body after this feature."
        End
    End If
    LogResult "PASS", "02_F002", "Created feature F002_Center_bore"
    WriteMacroResult "F002_Center_bore", "PASS", ""
End Sub

Sub Step03_F003_Seed()
    ' ---- PLANE SELECTION (Front Plane; name auto-detected) ----
    If Not SelectRefPlane("Front Plane", 1) Then
        MsgBox "Could not select Front Plane (no reference plane found).", vbCritical
        LogResult "FAIL", "03_F003", "Could not select Front Plane (no reference plane found)."
        WriteMacroResult "03_F003", "FAIL", "Could not select Front Plane (no reference plane found)."
        End
    End If

    ' ---- OPEN SKETCH ----
    swModel.SketchManager.InsertSketch True
    ' ---- SKETCH: SEED hole dia 0.406 at (2.31004, 1.25592) drawing units ----
    ' 0.406 in dia -> radius 0.005156 m ; seed center -> (0.058675, 0.0319) m
    swModel.SketchManager.CreateCircleByRadius 2.31004 * UNIT_FACTOR, 1.25592 * UNIT_FACTOR, 0#, (0.406 / 2#) * UNIT_FACTOR

    ' ---- FINALIZE SKETCH ----
    ' The feature call below consumes the ACTIVE sketch - this is exactly what
    ' SolidWorks' own macro recorder emits (ClearSelection2 then the feature
    ' call, sketch left open). No closing, no name-based reselection.
    On Error Resume Next
    swModel.SketchManager.FullyDefineSketch True, True, 0, True, 1, Nothing, 1, Nothing, 0, 0
    On Error GoTo 0
    swModel.ClearSelection2 True
    If swModel.SketchManager.ActiveSketch Is Nothing Then
        MsgBox "No active sketch to build the feature from.", vbCritical
        LogResult "FAIL", "03_F003", "No active sketch to build the feature from."
        WriteMacroResult "03_F003", "FAIL", "No active sketch to build the feature from."
        End
    End If

    ' ---- SEED CUT ----
    Dim swFeat As SldWorks.Feature
    Set swFeat = swModel.FeatureManager.FeatureCut4( _
        True, False, False, _
        swEndConditions_e.swEndCondThroughAllBoth, swEndConditions_e.swEndCondBlind, _
        0#, 0.01, _
        False, False, False, False, 0#, 0#, _
        False, False, False, False, False, _
        True, True, True, True, False, _
        swStartConditions_e.swStartSketchPlane, 0#, False, False)
    If swFeat Is Nothing Then
        ' The cut may have missed the material (body on the other side of the
        ' sketch plane) - restore the profile sketch and retry, direction flipped.
        If swModel.SketchManager.ActiveSketch Is Nothing Then
            ' Sketch was consumed/closed by the failed attempt: select the most
            ' recent sketch feature in the tree (type "ProfileFeature") by object,
            ' never by name.
            Dim featRswFeat As SldWorks.Feature, lastSkswFeat As SldWorks.Feature
            Set featRswFeat = swModel.FirstFeature
            Do While Not featRswFeat Is Nothing
                If featRswFeat.GetTypeName2 = "ProfileFeature" Then Set lastSkswFeat = featRswFeat
                Set featRswFeat = featRswFeat.GetNextFeature
            Loop
            swModel.ClearSelection2 True
            If Not lastSkswFeat Is Nothing Then lastSkswFeat.Select2 False, 0
        End If
        Set swFeat = swModel.FeatureManager.FeatureCut4( _
            True, False, True, _
            swEndConditions_e.swEndCondThroughAll, swEndConditions_e.swEndCondBlind, _
            0#, 0.01, _
            False, False, False, False, 0#, 0#, _
            False, False, False, False, False, _
            True, True, True, True, False, _
            swStartConditions_e.swStartSketchPlane, 0#, False, False)
    End If

    If swFeat Is Nothing Then
        MsgBox "Feature creation returned Nothing - check the sketch.", vbCritical
        LogResult "FAIL", "03_F003", "Feature creation returned Nothing - check the sketch."
        WriteMacroResult "03_F003", "FAIL", "Feature creation returned Nothing - check the sketch."
        End
    End If
    swFeat.Name = "F003_SeedHoleCut"
    If Not VerifySolidBody("03_F003") Then
        MsgBox "No solid body after this feature.", vbCritical
        LogResult "FAIL", "03_F003", "No solid body after this feature."
        WriteMacroResult "03_F003", "FAIL", "No solid body after this feature."
        End
    End If
    LogResult "PASS", "03_F003", "Created feature F003_SeedHoleCut"
    WriteMacroResult "F003_SeedHoleCut", "PASS", ""
End Sub

Sub Step04_F003_Axis()
    ' ---- REFERENCE AXIS "PatternAxis1" through the center bore's cylindrical face ----
    ' Bore: radius 1.94 in -> 0.049276 m, center (3.75, 3.75) in -> (0.09525, 0.09525) m
    ' Named references are deterministic; the axis is created ONCE here and
    ' selected by name ("PatternAxis1") from then on. The bore face is found
    ' GEOMETRICALLY (cylinder radius + axis location), with an exact-coordinate
    ' probe as fallback — never a blind coordinate pick.
    Dim axOk As Boolean
    Dim swPartAx As SldWorks.PartDoc, vBodiesAx As Variant, swBodyAx As SldWorks.Body2
    Dim vFacesAx As Variant, iF As Integer
    Dim swFaceAx As SldWorks.Face2, swSurfAx As SldWorks.Surface, vParamsAx As Variant
    Set swPartAx = swModel
    vBodiesAx = swPartAx.GetBodies2(swBodyType_e.swSolidBody, True)
    If Not IsEmpty(vBodiesAx) Then
        Set swBodyAx = vBodiesAx(0)
        vFacesAx = swBodyAx.GetFaces
        For iF = LBound(vFacesAx) To UBound(vFacesAx)
            Set swFaceAx = vFacesAx(iF)
            Set swSurfAx = swFaceAx.GetSurface
            If swSurfAx.IsCylinder Then
                ' CylinderParams: (origin x,y,z, axis x,y,z, radius) in meters.
                vParamsAx = swSurfAx.CylinderParams
                If Abs(vParamsAx(6) - 0.049276) < 0.00002 And _
                   Sqr((vParamsAx(0) - 0.09525) ^ 2 + (vParamsAx(1) - 0.09525) ^ 2) < 0.0005 Then
                    swModel.ClearSelection2 True
                    If swFaceAx.Select4(False, Nothing) Then
                        If swModel.InsertAxis2(True) Then
                            axOk = True
                            Exit For
                        End If
                    End If
                End If
            End If
        Next iF
    End If
    If Not axOk Then
        ' Fallback: exact generated wall point (5.69, 3.75) drawing units.
        Dim zTry As Variant, iAx As Integer
        zTry = Array(-0.00635, 0.00635, 0#)
        For iAx = LBound(zTry) To UBound(zTry)
            swModel.ClearSelection2 True
            If swModel.Extension.SelectByID2("", "FACE", 5.69 * UNIT_FACTOR, 3.75 * UNIT_FACTOR, CDbl(zTry(iAx)), False, 0, Nothing, 0) Then
                If swModel.InsertAxis2(True) Then
                    axOk = True
                    Exit For
                End If
            End If
        Next iAx
    End If
    If Not axOk Then
        MsgBox "Could not create reference axis PatternAxis1 from the bore face.", vbCritical
        LogResult "FAIL", "04_F003_axis", "Could not create reference axis PatternAxis1 from the bore face."
        WriteMacroResult "04_F003_axis", "FAIL", "Could not create reference axis PatternAxis1 from the bore face."
        End
    End If
    ' Rename the newest RefAxis feature to the deterministic name.
    Dim featAx As SldWorks.Feature, lastAx As SldWorks.Feature
    Set featAx = swModel.FirstFeature
    Do While Not featAx Is Nothing
        If featAx.GetTypeName2 = "RefAxis" Then Set lastAx = featAx
        Set featAx = featAx.GetNextFeature
    Loop
    If lastAx Is Nothing Then
        MsgBox "InsertAxis2 succeeded but no RefAxis feature found.", vbCritical
        LogResult "FAIL", "04_F003_axis", "InsertAxis2 succeeded but no RefAxis feature found."
        WriteMacroResult "04_F003_axis", "FAIL", "InsertAxis2 succeeded but no RefAxis feature found."
        End
    End If
    lastAx.Name = "PatternAxis1"
    swModel.ClearSelection2 True
    LogResult "PASS", "04_F003_axis", "Reference axis PatternAxis1 created from the bore cylindrical face"
    WriteMacroResult "PatternAxis1", "PASS", ""
End Sub

Sub Step05_F003_Pattern()
    ' ---- CIRCULAR PATTERN F003: 6 instances (n INCLUDES the seed = seed + 5 copies) ----
    ' Bolt circle radius 2.87991 drawing units, seed at -120 deg,
    ' equal spacing over 360 deg about axis "PatternAxis1".
    If Not CreateCircularPatternSafe("PatternAxis1", "F003_SeedHoleCut", 6, 360, False, False, False, "F003_CircularPattern", "05_F003_pattern") Then
        WriteMacroResult "F003_CircularPattern", "FAIL", "FeatureCircularPattern returned Nothing - check marks/axis"
        LogResult "FAIL", "05_F003_pattern", "FeatureCircularPattern returned Nothing - check marks/axis"
        swApp.SendMsgToUser2 "PATTERN FAILED at F003 (F003)", swMessageBoxIcon_e.swMbStop, swMessageBoxBtn_e.swMbOk
        End
    End If
    If Not VerifySolidBody("05_F003_pattern") Then
        MsgBox "No solid body after the circular pattern.", vbCritical
        LogResult "FAIL", "05_F003_pattern", "No solid body after the circular pattern."
        WriteMacroResult "05_F003_pattern", "FAIL", "No solid body after the circular pattern."
        End
    End If
    LogResult "PASS", "05_F003_pattern", "Circular pattern F003_CircularPattern created (6 instances)"
    WriteMacroResult "F003_CircularPattern", "PASS", "6 instances about PatternAxis1"
End Sub

Sub Step06_F004()
    ' ---- PLANE SELECTION (Front Plane; name auto-detected) ----
    If Not SelectRefPlane("Front Plane", 1) Then
        MsgBox "Could not select Front Plane (no reference plane found).", vbCritical
        LogResult "FAIL", "06_F004", "Could not select Front Plane (no reference plane found)."
        WriteMacroResult "06_F004", "FAIL", "Could not select Front Plane (no reference plane found)."
        End
    End If

    ' ---- OPEN SKETCH ----
    swModel.SketchManager.InsertSketch True
    ' ---- SKETCH: circle dia 1.25 at (3.75, 0.81) drawing units ----
    swModel.SketchManager.CreateCircleByRadius 3.75 * UNIT_FACTOR, 0.81 * UNIT_FACTOR, 0#, (1.25 / 2#) * UNIT_FACTOR

    ' ---- FINALIZE SKETCH ----
    ' The feature call below consumes the ACTIVE sketch - this is exactly what
    ' SolidWorks' own macro recorder emits (ClearSelection2 then the feature
    ' call, sketch left open). No closing, no name-based reselection.
    On Error Resume Next
    swModel.SketchManager.FullyDefineSketch True, True, 0, True, 1, Nothing, 1, Nothing, 0, 0
    On Error GoTo 0
    swModel.ClearSelection2 True
    If swModel.SketchManager.ActiveSketch Is Nothing Then
        MsgBox "No active sketch to build the feature from.", vbCritical
        LogResult "FAIL", "06_F004", "No active sketch to build the feature from."
        WriteMacroResult "06_F004", "FAIL", "No active sketch to build the feature from."
        End
    End If

    ' ---- FEATURE ----
    Dim swFeat As SldWorks.Feature
    Set swFeat = swModel.FeatureManager.FeatureCut4( _
        True, False, False, _
        swEndConditions_e.swEndCondThroughAllBoth, swEndConditions_e.swEndCondBlind, _
        0#, 0.01, _
        False, False, False, False, 0#, 0#, _
        False, False, False, False, False, _
        True, True, True, True, False, _
        swStartConditions_e.swStartSketchPlane, 0#, False, False)
    If swFeat Is Nothing Then
        ' The cut may have missed the material (body on the other side of the
        ' sketch plane) - restore the profile sketch and retry, direction flipped.
        If swModel.SketchManager.ActiveSketch Is Nothing Then
            ' Sketch was consumed/closed by the failed attempt: select the most
            ' recent sketch feature in the tree (type "ProfileFeature") by object,
            ' never by name.
            Dim featRswFeat As SldWorks.Feature, lastSkswFeat As SldWorks.Feature
            Set featRswFeat = swModel.FirstFeature
            Do While Not featRswFeat Is Nothing
                If featRswFeat.GetTypeName2 = "ProfileFeature" Then Set lastSkswFeat = featRswFeat
                Set featRswFeat = featRswFeat.GetNextFeature
            Loop
            swModel.ClearSelection2 True
            If Not lastSkswFeat Is Nothing Then lastSkswFeat.Select2 False, 0
        End If
        Set swFeat = swModel.FeatureManager.FeatureCut4( _
            True, False, True, _
            swEndConditions_e.swEndCondThroughAll, swEndConditions_e.swEndCondBlind, _
            0#, 0.01, _
            False, False, False, False, 0#, 0#, _
            False, False, False, False, False, _
            True, True, True, True, False, _
            swStartConditions_e.swStartSketchPlane, 0#, False, False)
    End If

    If swFeat Is Nothing Then
        MsgBox "Feature creation returned Nothing - check the sketch.", vbCritical
        LogResult "FAIL", "06_F004", "Feature creation returned Nothing - check the sketch."
        WriteMacroResult "06_F004", "FAIL", "Feature creation returned Nothing - check the sketch."
        End
    End If
    swFeat.Name = "F004_Offset_circular_cut"
    If Not VerifySolidBody("06_F004") Then
        MsgBox "No solid body after this feature.", vbCritical
        LogResult "FAIL", "06_F004", "No solid body after this feature."
        WriteMacroResult "06_F004", "FAIL", "No solid body after this feature."
        End
    End If
    LogResult "PASS", "06_F004", "Created feature F004_Offset_circular_cut"
    WriteMacroResult "F004_Offset_circular_cut", "PASS", ""
End Sub

Sub StepZZ_FinalVerify()
    ' ---- FORCE REBUILD ----
    boolstatus = swModel.ForceRebuild3(False)
    If Not boolstatus Then
        LogResult "WARN", "ZZ_final_verify", "ForceRebuild3 reported failure - check the feature tree"
    End If

    ' ---- MASS PROPERTIES (proves a solid body exists) ----
    Dim vMass As Variant
    Dim mpStatus As Long
    vMass = swModel.Extension.GetMassProperties2(1, mpStatus, False)
    If IsEmpty(vMass) Then
        MsgBox "GetMassProperties2 returned nothing - no solid body?", vbCritical
        LogResult "FAIL", "ZZ_final_verify", "GetMassProperties2 returned nothing - no solid body?"
        WriteMacroResult "ZZ_final_verify", "FAIL", "GetMassProperties2 returned nothing - no solid body?"
        End
    End If
    ' vMass: 0-2 = CoM x,y,z ; 3 = volume (m^3) ; 4 = surface area (m^2) ; 5 = mass
    If vMass(3) <= 0 Then
        MsgBox "Part has zero volume.", vbCritical
        LogResult "FAIL", "ZZ_final_verify", "Part has zero volume."
        WriteMacroResult "ZZ_final_verify", "FAIL", "Part has zero volume."
        End
    End If
    LogResult "PASS", "ZZ_final_verify", "Volume(mm3)=" & Format$(vMass(3) * 1000000000#, "0.0") & _
        "  CoM(drawing units)=(" & Format$(vMass(0) / UNIT_FACTOR, "0.000") & ", " & _
        Format$(vMass(1) / UNIT_FACTOR, "0.000") & ", " & Format$(vMass(2) / UNIT_FACTOR, "0.000") & ")"

    ' ---- BOUNDING BOX vs DRAWING ENVELOPE ----
    ' Expected from the drawing: none extracted
    ' Box read from the solid body (IBody2::GetBodyBox) - ModelDoc2 exposes
    ' no whole-model bounding-box call in VBA.
    Dim swPart As SldWorks.PartDoc
    Dim vBodies As Variant
    Dim swBody As SldWorks.Body2
    Dim vBox As Variant
    Set swPart = swModel
    vBodies = swPart.GetBodies2(swBodyType_e.swSolidBody, True)
    If IsEmpty(vBodies) Then
        MsgBox "No solid body to measure.", vbCritical
        LogResult "FAIL", "ZZ_final_verify", "No solid body to measure."
        WriteMacroResult "ZZ_final_verify", "FAIL", "No solid body to measure."
        End
    End If
    Set swBody = vBodies(0)
    vBox = swBody.GetBodyBox
    MsgBox "Bounding box (drawing units): " & _
        Format$((vBox(3) - vBox(0)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(4) - vBox(1)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(5) - vBox(2)) / UNIT_FACTOR, "0.000") & vbCrLf & _
        "Drawing envelope: none extracted" & vbCrLf & _
        "Expected feature count: 6", vbInformation
    LogResult "PASS", "ZZ_final_verify", "bbox(drawing units) " & _
        Format$((vBox(3) - vBox(0)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(4) - vBox(1)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(5) - vBox(2)) / UNIT_FACTOR, "0.000")

    ' ---- SAVE ----
    Dim saveErrs As Long, saveWarns As Long
    boolstatus = swModel.Save3(swSaveAsOptions_e.swSaveAsOptions_Silent, saveErrs, saveWarns)
    LogResult IIf(boolstatus, "PASS", "WARN"), "ZZ_final_verify", "Save3 errs=" & saveErrs
End Sub

Sub StepZZZ_ExportStl()
    ' ---- EXPORT STL (beside the .sldprt, same base name) ----
    Dim stlPath As String
    stlPath = swModel.GetPathName
    If stlPath = "" Then
        MsgBox "Part has not been saved yet - run 00_setup / ZZ_final_verify first.", vbCritical
        LogResult "FAIL", "ZZZ_export_stl", "No saved path - cannot derive STL name"
        End
    End If
    Dim dotPos As Long
    dotPos = InStrRev(stlPath, ".")
    If dotPos > 0 Then stlPath = Left$(stlPath, dotPos - 1)
    stlPath = stlPath & ".stl"
    boolstatus = swModel.SaveAs3(stlPath, 0, 0)
    LogResult IIf(boolstatus, "PASS", "WARN"), "ZZZ_export_stl", "STL -> " & stlPath
End Sub

Sub main()
    Set swApp = Application.SldWorks
    LogResult "INFO", "RUN_ALL", "Starting full build"
    Step00_Setup
    Step01_F001
    Step02_F002
    Step03_F003_Seed
    Step04_F003_Axis
    Step05_F003_Pattern
    Step06_F004
    StepZZ_FinalVerify
    StepZZZ_ExportStl
    LogResult "PASS", "RUN_ALL", "All steps completed"
    MsgBox "RUN_ALL finished. See ..\logs\build_log.txt for the per-step log.", vbInformation
End Sub
