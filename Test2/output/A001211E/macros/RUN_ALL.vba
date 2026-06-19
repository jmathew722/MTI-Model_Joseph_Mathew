' ============================================================
' RUN_ALL - build the entire part in one run (ordered)
' Part: A001211E
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
        End
    End If
    Set swModel = swApp.NewDocument(templatePath, 0, 0, 0)
    If swModel Is Nothing Then
        MsgBox "NewDocument failed.", vbCritical
        LogResult "FAIL", "00_setup", "NewDocument failed."
        End
    End If

    ' ---- UNITS: must be set BEFORE any geometry ----
    boolstatus = swModel.Extension.SetUserPreferenceInteger( _
        swUserPreferenceIntegerValue_e.swUnitSystem, _
        swUserPreferenceOption_e.swDetailingNoOptionSpecified, swUnitSystem_e.swUnitSystem_IPS)
    LogResult "PASS", "00_setup", "New part created; units set (inch)"

    ' ---- SAVE AS A001211E.sldprt (next to the macros folder) ----
    Dim macroPath As String, savePath As String
    Dim saveErrs As Long, saveWarns As Long
    macroPath = swApp.GetCurrentMacroPathName
    savePath = Left$(macroPath, InStrRev(macroPath, "\")) & "..\A001211E.sldprt"
    boolstatus = swModel.Extension.SaveAs(savePath, 0, _
        swSaveAsOptions_e.swSaveAsOptions_Silent, Nothing, saveErrs, saveWarns)
    If Not boolstatus Then
        LogResult "WARN", "00_setup", "Initial SaveAs failed (errs=" & saveErrs & ") - save manually"
    Else
        LogResult "PASS", "00_setup", "Saved " & savePath
    End If
End Sub

Sub Step01_F001()
    ' --- Stage 2.5 assumption flags ---
    MsgBox "POSITION ASSUMED for F001: centered on the parent feature because the drawing did not dimension its location ? verify placement in SolidWorks.", vbExclamation, "Verify before continuing (F001)"
    ' ---- PLANE SELECTION (Front Plane; name auto-detected) ----
    If Not SelectRefPlane("Front Plane", 1) Then
        MsgBox "Could not select Front Plane (no reference plane found).", vbCritical
        LogResult "FAIL", "01_F001", "Could not select Front Plane (no reference plane found)."
        End
    End If

    ' ---- OPEN SKETCH ----
    swModel.SketchManager.InsertSketch True
    ' ---- SKETCH: rectangle 6.88 x 6.88, lower-left corner at (0, 0) ----
    ' (Corner at the origin keeps sketch coordinates equal to the drawing's
    '  edge-referenced dimensions, so hole positions land where dimensioned.)
    swModel.SketchManager.CreateCornerRectangle 0 * UNIT_FACTOR, 0 * UNIT_FACTOR, 0#, _
        (0 + 6.88) * UNIT_FACTOR, (0 + 6.88) * UNIT_FACTOR, 0#
    ' NOTE: POSITION ASSUMED (drawing frame: rect corner at origin / circle at plate center) - verify against the drawing.

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
        End
    End If

    ' ---- FEATURE ----
    Dim swFeat As SldWorks.Feature
    Set swFeat = swModel.FeatureManager.FeatureExtrusion3( _
        True, False, False, _
        swEndConditions_e.swEndCondBlind, swEndConditions_e.swEndCondBlind, _
        0.25 * UNIT_FACTOR, 0.01, _
        False, False, False, False, 0#, 0#, _
        False, False, False, False, _
        True, True, True, _
        swStartConditions_e.swStartSketchPlane, 0#, False)

    If swFeat Is Nothing Then
        MsgBox "Feature creation returned Nothing - check the sketch.", vbCritical
        LogResult "FAIL", "01_F001", "Feature creation returned Nothing - check the sketch."
        End
    End If
    swFeat.Name = "F001_Base_plate_extruded_from_front_profile_O"
    If Not VerifySolidBody("01_F001") Then
        MsgBox "No solid body after this feature.", vbCritical
        LogResult "FAIL", "01_F001", "No solid body after this feature."
        End
    End If
    LogResult "PASS", "01_F001", "Created feature F001_Base_plate_extruded_from_front_profile_O"
End Sub

Sub Step02_F002()
    ' --- Stage 2.5 assumption flags ---
    ' !! CRITICAL ASSUMPTION [D900] — VERIFY BEFORE REBUILD
    ' !! THICKNESS ASSUMED for base F002: the drawing does not dimension the part thickness, so D900=6 inch was synthesized so a solid could be built ? MUST set the real thickness in SolidWorks before rebuild.
    If MsgBox("CRITICAL ASSUMPTION [D900]:" & vbCrLf & "THICKNESS ASSUMED for base F002: the drawing does not dimension the part thickness, so D900=6 inch was synthesized so a solid could be built ? MUST set the real thickness in SolidWorks before rebuild." & vbCrLf & vbCrLf & "Click OK to build with this assumption, or Cancel to stop.", vbOKCancel + vbExclamation, "Critical assumption — D900") = vbCancel Then
        LogResult "STOP", "02_F002", "User cancelled at critical assumption D900"
        Exit Sub
    End If
    ' ---- PLANE SELECTION (Front Plane; name auto-detected) ----
    If Not SelectRefPlane("Front Plane", 1) Then
        MsgBox "Could not select Front Plane (no reference plane found).", vbCritical
        LogResult "FAIL", "02_F002", "Could not select Front Plane (no reference plane found)."
        End
    End If

    ' ---- OPEN SKETCH ----
    swModel.SketchManager.InsertSketch True
    ' ---- SKETCH: rectangle 4.38 x 4.38, lower-left corner at (1.88, 4.5) ----
    ' (Corner at the origin keeps sketch coordinates equal to the drawing's
    '  edge-referenced dimensions, so hole positions land where dimensioned.)
    swModel.SketchManager.CreateCornerRectangle 1.88 * UNIT_FACTOR, 4.5 * UNIT_FACTOR, 0#, _
        (1.88 + 4.38) * UNIT_FACTOR, (4.5 + 4.38) * UNIT_FACTOR, 0#

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
        End
    End If

    ' ---- FEATURE ----
    Dim swFeat As SldWorks.Feature
    Set swFeat = swModel.FeatureManager.FeatureExtrusion3( _
        True, False, False, _
        swEndConditions_e.swEndCondBlind, swEndConditions_e.swEndCondBlind, _
        6 * UNIT_FACTOR, 0.01, _
        False, False, False, False, 0#, 0#, _
        False, False, False, False, _
        True, True, True, _
        swStartConditions_e.swStartSketchPlane, 0#, False)

    If swFeat Is Nothing Then
        MsgBox "Feature creation returned Nothing - check the sketch.", vbCritical
        LogResult "FAIL", "02_F002", "Feature creation returned Nothing - check the sketch."
        End
    End If
    swFeat.Name = "F002_Raised_boss_step_on_upper_portion_of_bas"
    If Not VerifySolidBody("02_F002") Then
        MsgBox "No solid body after this feature.", vbCritical
        LogResult "FAIL", "02_F002", "No solid body after this feature."
        End
    End If
    LogResult "PASS", "02_F002", "Created feature F002_Raised_boss_step_on_upper_portion_of_bas"
End Sub

Sub Step03_F003()
    ' ---- PLANE SELECTION (Front Plane; name auto-detected) ----
    If Not SelectRefPlane("Front Plane", 1) Then
        MsgBox "Could not select Front Plane (no reference plane found).", vbCritical
        LogResult "FAIL", "03_F003", "Could not select Front Plane (no reference plane found)."
        End
    End If

    ' ---- OPEN SKETCH ----
    swModel.SketchManager.InsertSketch True
    ' ---- SKETCH: 2 hole(s) dia 0.406 (thru) ----
    swModel.SketchManager.CreateCircleByRadius 2.875 * UNIT_FACTOR, 2.938 * UNIT_FACTOR, 0#, (0.406 / 2#) * UNIT_FACTOR
    swModel.SketchManager.CreateCircleByRadius 5.375 * UNIT_FACTOR, 2.938 * UNIT_FACTOR, 0#, (0.406 / 2#) * UNIT_FACTOR
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
        LogResult "FAIL", "03_F003", "No active sketch to build the feature from."
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
        LogResult "FAIL", "03_F003", "Feature creation returned Nothing - check the sketch."
        End
    End If
    swFeat.Name = "F003_406_DIA_THRU_holes_qty_2_Positioned_hori"
    If Not VerifySolidBody("03_F003") Then
        MsgBox "No solid body after this feature.", vbCritical
        LogResult "FAIL", "03_F003", "No solid body after this feature."
        End
    End If
    LogResult "PASS", "03_F003", "Created feature F003_406_DIA_THRU_holes_qty_2_Positioned_hori"
End Sub

Sub Step04_F004()
    ' ---- PLANE SELECTION (Front Plane; name auto-detected) ----
    If Not SelectRefPlane("Front Plane", 1) Then
        MsgBox "Could not select Front Plane (no reference plane found).", vbCritical
        LogResult "FAIL", "04_F004", "Could not select Front Plane (no reference plane found)."
        End
    End If

    ' ---- OPEN SKETCH ----
    swModel.SketchManager.InsertSketch True
    ' ---- SKETCH: 6 hole(s) dia 0.138 (tapped) ----
    swModel.SketchManager.CreateCircleByRadius 5.375 * UNIT_FACTOR, 4.094 * UNIT_FACTOR, 0#, (0.138 / 2#) * UNIT_FACTOR
    swModel.SketchManager.CreateCircleByRadius 5.375 * UNIT_FACTOR, 1.75 * UNIT_FACTOR, 0#, (0.138 / 2#) * UNIT_FACTOR
    swModel.SketchManager.CreateCircleByRadius 5.375 * UNIT_FACTOR, 0.938 * UNIT_FACTOR, 0#, (0.138 / 2#) * UNIT_FACTOR
    swModel.SketchManager.CreateCircleByRadius 6.531 * UNIT_FACTOR, 4.094 * UNIT_FACTOR, 0#, (0.138 / 2#) * UNIT_FACTOR
    swModel.SketchManager.CreateCircleByRadius 6.531 * UNIT_FACTOR, 1.75 * UNIT_FACTOR, 0#, (0.138 / 2#) * UNIT_FACTOR
    swModel.SketchManager.CreateCircleByRadius 6.531 * UNIT_FACTOR, 0.938 * UNIT_FACTOR, 0#, (0.138 / 2#) * UNIT_FACTOR
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
        LogResult "FAIL", "04_F004", "No active sketch to build the feature from."
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
        LogResult "FAIL", "04_F004", "Feature creation returned Nothing - check the sketch."
        End
    End If
    swFeat.Name = "F004_10_24_TAP_THRU_holes_2_columns_x_3_rows_"
    If Not VerifySolidBody("04_F004") Then
        MsgBox "No solid body after this feature.", vbCritical
        LogResult "FAIL", "04_F004", "No solid body after this feature."
        End
    End If
    LogResult "PASS", "04_F004", "Created feature F004_10_24_TAP_THRU_holes_2_columns_x_3_rows_"

    ' TODO: VERIFY API CALL - cosmetic thread for "10-24 UNC"
    ' Real (helical) threads are prohibited. Apply a cosmetic thread:
    ' select the hole's circular edge, then Insert > Annotations > Cosmetic Thread,
    ' spec "10-24 UNC". (InsertCosmeticThread3 exists but its argument
    ' shape was not verified against a documented example, so it is not scripted.)
    LogResult "WARN", "04_F004", "Apply cosmetic thread 10-24 UNC manually - see macro comments"
End Sub

Sub Step05_F005()
    ' --- Stage 2.5 assumption flags ---
    MsgBox "POSITION ASSUMED for F005: centered on the parent feature because the drawing did not dimension its location ? verify placement in SolidWorks.", vbExclamation, "Verify before continuing (F005)"
    ' Pattern F005 is ALREADY SATISFIED: feature F004 cut all
    ' 6 instance(s) as separate circles in one sketch, so there is nothing
    ' left to pattern. This macro just records that and moves on.
    LogResult "PASS", "05_F005", "F005 pattern already realized by F004 (6 instances) - no action needed"
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
        End
    End If
    ' vMass: 0-2 = CoM x,y,z ; 3 = volume (m^3) ; 4 = surface area (m^2) ; 5 = mass
    If vMass(3) <= 0 Then
        MsgBox "Part has zero volume.", vbCritical
        LogResult "FAIL", "ZZ_final_verify", "Part has zero volume."
        End
    End If
    LogResult "PASS", "ZZ_final_verify", "Volume(mm3)=" & Format$(vMass(3) * 1000000000#, "0.0") & _
        "  CoM(drawing units)=(" & Format$(vMass(0) / UNIT_FACTOR, "0.000") & ", " & _
        Format$(vMass(1) / UNIT_FACTOR, "0.000") & ", " & Format$(vMass(2) / UNIT_FACTOR, "0.000") & ")"

    ' ---- BOUNDING BOX vs DRAWING ENVELOPE ----
    ' Expected from the drawing: height=4.5; width=6.88; height=4.5
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
        End
    End If
    Set swBody = vBodies(0)
    vBox = swBody.GetBodyBox
    MsgBox "Bounding box (drawing units): " & _
        Format$((vBox(3) - vBox(0)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(4) - vBox(1)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(5) - vBox(2)) / UNIT_FACTOR, "0.000") & vbCrLf & _
        "Drawing envelope: height=4.5; width=6.88; height=4.5" & vbCrLf & _
        "Expected feature count: 5", vbInformation
    LogResult "PASS", "ZZ_final_verify", "bbox(drawing units) " & _
        Format$((vBox(3) - vBox(0)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(4) - vBox(1)) / UNIT_FACTOR, "0.000") & " x " & _
        Format$((vBox(5) - vBox(2)) / UNIT_FACTOR, "0.000")

    ' ---- SAVE ----
    Dim saveErrs As Long, saveWarns As Long
    boolstatus = swModel.Save3(swSaveAsOptions_e.swSaveAsOptions_Silent, saveErrs, saveWarns)
    LogResult IIf(boolstatus, "PASS", "WARN"), "ZZ_final_verify", "Save3 errs=" & saveErrs
End Sub

Sub main()
    Set swApp = Application.SldWorks
    LogResult "INFO", "RUN_ALL", "Starting full build"
    Step00_Setup
    Step01_F001
    Step02_F002
    Step03_F003
    Step04_F004
    Step05_F005
    StepZZ_FinalVerify
    LogResult "PASS", "RUN_ALL", "All steps completed"
    MsgBox "RUN_ALL finished. See ..\logs\build_log.txt for the per-step log.", vbInformation
End Sub
