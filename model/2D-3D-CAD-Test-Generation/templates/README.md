# Part template

The builder does **not** require a custom template — by default it uses
SolidWorks' configured default part template (resolved at runtime via
`swDefaultTemplatePart`).

A real `.sldprt`/`.prtdot` is a proprietary binary that can only be created by
SolidWorks itself, so none is committed here.

To use a custom blank template:

1. In SolidWorks 2024: **File → New → Part**, then **File → Save As** a part
   template (`.prtdot`), e.g. `part_template.prtdot`.
2. Drop it in this folder, or point `SOLIDWORKS_TEMPLATE_PATH` in `.env` at it.

The builder picks it up via `create_new_part(sw_app, template_path=...)`.
