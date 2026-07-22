"""Fillets on the inside edges of extrude cuts (2026-07-22).

`_cut_interior_vertical_edges` selects a cut's interior (concave) vertical corner
edges — the edges shared by TWO of the cut's own created faces — using only edge-
endpoint fingerprints (no SolidWorks). These tests stub the minimal COM surface
(Feature.GetFaces → Face.GetEdges → Edge.GetStartVertex/GetEndVertex.GetPoint) so
the topological rule is verified headlessly: a closed pocket yields its 4 corner
edges, an open notch yields only its 2 closed-end corners (the mouth is excluded),
and rim/horizontal edges are never chosen.
"""
from pipeline.solidworks_builder import (
    _cut_interior_vertical_edges,
    _edge_is_vertical,
    _edge_key,
)


class _Vertex:
    def __init__(self, p): self._p = p
    def GetPoint(self): return self._p


class _Edge:
    def __init__(self, start, end, tag=""):
        self._s, self._e, self.tag = start, end, tag
    def GetStartVertex(self): return _Vertex(self._s)
    def GetEndVertex(self): return _Vertex(self._e)


class _Face:
    def __init__(self, edges): self._edges = edges
    def GetEdges(self): return self._edges


class _Feat:
    def __init__(self, faces): self._faces = faces
    def GetFaces(self): return self._faces


def _vedge(x, y, tag, t=0.28):
    return _Edge((x, y, 0.0), (x, y, t), tag)


def _hedge(x1, y1, x2, y2, z, tag):
    return _Edge((x1, y1, z), (x2, y2, z), tag)


# --------------------------------------------------------------------------- #
# Primitive helpers
# --------------------------------------------------------------------------- #
def test_vertical_detection():
    assert _edge_is_vertical(((0, 0, 0), (0, 0, 0.28)))
    assert not _edge_is_vertical(((0, 0, 0.28), (1, 0, 0.28)))   # top rim (horizontal)
    assert not _edge_is_vertical(((0, 0, 0), (1, 0, 0)))          # bottom rim


def test_edge_key_is_order_independent():
    a = _edge_key(((0, 0, 0), (0, 0, 0.28)))
    b = _edge_key(((0, 0, 0.28), (0, 0, 0)))
    assert a == b


# --------------------------------------------------------------------------- #
# Closed pocket → 4 interior vertical corner edges
# --------------------------------------------------------------------------- #
def test_closed_pocket_selects_four_corner_edges():
    V00, V10, V11, V01 = (_vedge(0, 0, "V00"), _vedge(1, 0, "V10"),
                          _vedge(1, 1, "V11"), _vedge(0, 1, "V01"))
    wallS = _Face([V00, V10, _hedge(0, 0, 1, 0, 0.28, "topS"), _hedge(0, 0, 1, 0, 0, "botS")])
    wallE = _Face([V10, V11, _hedge(1, 0, 1, 1, 0.28, "topE"), _hedge(1, 0, 1, 1, 0, "botE")])
    wallN = _Face([V11, V01, _hedge(1, 1, 0, 1, 0.28, "topN"), _hedge(1, 1, 0, 1, 0, "botN")])
    wallW = _Face([V01, V00, _hedge(0, 1, 0, 0, 0.28, "topW"), _hedge(0, 1, 0, 0, 0, "botW")])
    feat = _Feat([wallS, wallE, wallN, wallW])
    got = _cut_interior_vertical_edges(feat)
    assert {e.tag for e in got} == {"V00", "V10", "V11", "V01"}


# --------------------------------------------------------------------------- #
# Open notch → only the 2 closed-end corners (mouth excluded)
# --------------------------------------------------------------------------- #
def test_open_notch_excludes_the_mouth_edges():
    Vbl, Vbr = _vedge(0, 0, "Vbl"), _vedge(0, 1, "Vbr")   # back-wall corners
    Vml, Vmr = _vedge(1, 0, "Vml"), _vedge(1, 1, "Vmr")   # mouth (open-side) corners
    wallBack = _Face([Vbl, Vbr, _hedge(0, 0, 0, 1, 0.28, "topB"), _hedge(0, 0, 0, 1, 0, "botB")])
    wallLeft = _Face([Vbl, Vml, _hedge(0, 0, 1, 0, 0.28, "topL"), _hedge(0, 0, 1, 0, 0, "botL")])
    wallRight = _Face([Vbr, Vmr, _hedge(0, 1, 1, 1, 0.28, "topR"), _hedge(0, 1, 1, 1, 0, "botR")])
    # The mouth vertical edges also live on the part's OUTER wall — but that face
    # is NOT one of the cut's created faces, so it is not in GetFaces().
    feat = _Feat([wallBack, wallLeft, wallRight])
    got = _cut_interior_vertical_edges(feat)
    assert {e.tag for e in got} == {"Vbl", "Vbr"}          # mouth Vml/Vmr excluded


# --------------------------------------------------------------------------- #
# Degenerate inputs never crash
# --------------------------------------------------------------------------- #
def test_no_faces_returns_empty():
    assert _cut_interior_vertical_edges(_Feat([])) == []
    assert _cut_interior_vertical_edges(_Feat(None)) == []


def test_only_rim_edges_selects_nothing():
    # A face whose edges are all horizontal (no vertical corners shared) yields none.
    f = _Face([_hedge(0, 0, 1, 0, 0.28, "t"), _hedge(0, 0, 1, 0, 0, "b")])
    assert _cut_interior_vertical_edges(_Feat([f])) == []
