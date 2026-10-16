"""A heightfield grid as mesh arrays (numpy only: imported inside Blender by blender_greybox.py
and blender_npz_glb.py, and by the tests).

Rows run south -> north and columns west -> east, so the quads (i, j), (i, j+1), (i+1, j+1),
(i+1, j) wind counter-clockwise seen from above: normals up.
"""
import numpy as np


def grid_mesh(h, x0, y0, cell, keep=None, skirt=0.0):
    """(verts [k, 3], quads [q, 4]) of the cells of h whose four corners are finite (and in
    keep, a bool mask over the vertices, if given).

    skirt > 0 adds a curtain hanging `skirt` metres down from every boundary edge, facing out of
    the mesh. Where another mesh meets this one along a boundary at a coarser or finer vertex
    spacing, the two edges differ slightly between shared vertices; the curtain closes the crack
    a viewer would otherwise see under this mesh's edge."""
    h = np.asarray(h, float)
    n, m = h.shape
    X, Y = np.meshgrid(x0 + np.arange(m) * cell, y0 + np.arange(n) * cell)
    ok = np.isfinite(h) if keep is None else np.isfinite(h) & keep
    vid = np.full(h.shape, -1, np.int64)
    vid[ok] = np.arange(int(ok.sum()))
    verts = np.stack([X[ok], Y[ok], h[ok]], 1)
    a, b, c, d = vid[:-1, :-1], vid[:-1, 1:], vid[1:, 1:], vid[1:, :-1]
    good = (a >= 0) & (b >= 0) & (c >= 0) & (d >= 0)
    quads = np.stack([a[good], b[good], c[good], d[good]], 1)
    if skirt <= 0 or not len(quads):
        return verts, quads
    P = np.zeros((n + 1, m + 1), bool)          # quad cells, padded: P[i+1, j+1] = cell (i, j)
    P[1:n, 1:m] = good
    edges = []                                  # (vertex p, vertex q, outward dx, dy)
    below, above = P[0:n, 1:m], P[1:n + 1, 1:m]  # edges (i, j)-(i, j+1): cells i-1 and i
    for i, j in zip(*np.nonzero(below != above)):
        edges.append((vid[i, j], vid[i, j + 1], 0.0, 1.0 if below[i, j] else -1.0))
    left, right = P[1:n, 0:m], P[1:n, 1:m + 1]  # edges (i, j)-(i+1, j): cells j-1 and j
    for i, j in zip(*np.nonzero(left != right)):
        edges.append((vid[i, j], vid[i + 1, j], 1.0 if left[i, j] else -1.0, 0.0))
    # The curtain gets its own copies of the edge vertices: shared ones would average its
    # vertical faces into the surface's smooth normals and shade a dark band along the edge.
    ends = np.unique(np.array([[p, q] for p, q, _, _ in edges]).ravel())
    top = {int(v): len(verts) + k for k, v in enumerate(ends)}
    low = {int(v): len(verts) + len(ends) + k for k, v in enumerate(ends)}
    bottom = verts[ends].copy()
    bottom[:, 2] -= skirt
    sk = []
    for p, q, ux, uy in edges:
        e = verts[q, :2] - verts[p, :2]
        if e[0] * uy - e[1] * ux < 0:           # normal (q - p) x down must point outward (ux, uy)
            p, q = q, p
        sk.append((top[int(p)], top[int(q)], low[int(q)], low[int(p)]))
    return np.vstack([verts, verts[ends], bottom]), np.vstack([quads, np.array(sk, np.int64)])
