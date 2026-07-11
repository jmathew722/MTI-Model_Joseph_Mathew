# SolidWorks 2024 API — reference-geometry method signatures (authoritative)

The exact signatures our generated macros use for Workstream 3 (datum
skeleton). `macro_audit.py`'s whitelist is updated to accept exactly these.
Sources: SolidWorks 2024 API Help (help.solidworks.com) + idioms mined from
xarial/codestack (`third_party/codestack`, MIT) — patterns copied into our
generator templates with attribution; codestack code is NOT called at runtime.

## IFeatureManager::InsertRefPlane

```
Feature InsertRefPlane(
    long FirstConstraint,  double FirstRefData,
    long SecondConstraint, double SecondRefData,
    long ThirdConstraint,  double ThirdRefData)
```
- Pre-select the reference entity/entities (plane/face/edge/axis/point) with the
  appropriate selection Mark before calling.
- `*Constraint` values are `swRefPlaneReferenceConstraints_e` (OR-able):
  `swRefPlaneReferenceConstraint_Distance = 8`,
  `swRefPlaneReferenceConstraint_Angle = 16`,
  `swRefPlaneReferenceConstraint_Coincident = 2`,
  `swRefPlaneReferenceConstraint_Parallel = ?`,
  `swRefPlaneReferenceConstraint_Perpendicular`, `swRefPlaneReferenceConstraint_Project`.
- Most datum planes are an OFFSET from a standard plane:
  select `"Front Plane"`, then
  `InsertRefPlane(swRefPlaneReferenceConstraint_Distance, offset_m, 0, 0, 0, 0)`.
- Coincident-to-a-standard-plane (a datum that IS the bottom/left face) uses
  `swRefPlaneReferenceConstraint_Coincident` with offset 0, or simply reuses the
  standard plane by name.

## IModelDoc2::InsertAxis2

```
Feature InsertAxis2(boolean IsSingleDirection)
```
- Pre-select the defining entities, then call. Two common forms:
  - Two planes selected -> axis at their intersection (part centerline).
  - One cylindrical face selected -> its axis (hole/bore centerline).
- Already used in the circular-pattern trio (`solidworks_builder.build_circular_pattern_holes`).

## ISketchManager::SketchUseEdge3 (Convert Entities)

```
void SketchUseEdge3(boolean Chain, boolean InnerCallout)
```
- With an edge/face pre-selected and a sketch active, projects that entity into
  the current sketch — the child profile is then PARAMETRICALLY tied to the
  parent (move the parent, the child follows). `Chain=True` grabs the connected
  loop. (codestack `solidworks-api/document/sketch/...` uses `SketchUseEdge2/3`.)

## Selection helper

```
boolean IModelDocExtension::SelectByID2(
    string Name, string Type, double X, double Y, double Z,
    boolean Append, long Mark, Dispatch Callout, long SelectOption)
```
- Named reference geometry is selected by its NAME + Type `"PLANE"`/`"AXIS"`/
  `"DATUMPOINT"` — never by graphical index. This is what makes the reference
  skeleton a set of stable selection handles for the deferred-retry loop.
