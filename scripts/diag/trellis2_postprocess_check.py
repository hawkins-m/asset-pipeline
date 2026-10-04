"""Step through o_voxel.to_glb's geometry steps on a saved raw mesh and report the shape
after each, plus a cuBVH unsigned-distance check against trimesh (CPU, exact).

    source scripts/rocm_build_env.sh
    HIP_VISIBLE_DEVICES=1 $AP_ROOT/envs/trellis2/bin/python \
        scripts/diag/trellis2_postprocess_check.py RAW.pt
"""
import sys

import cumesh
import numpy as np
import torch
import trimesh


def stats(name, v, f):
    v = v.float().cpu()
    size = (v.max(0).values - v.min(0).values).tolist()
    print(f"{name:<28} V={len(v):>8} F={len(f):>8} bbox={[round(s, 3) for s in size]}", flush=True)


def bvh_check(v, f, n=400):
    """cuBVH unsigned distance + closest face vs trimesh on points near the surface."""
    rng = np.random.default_rng(0)
    vn, fn = v.cpu().numpy(), f.cpu().numpy()
    tm = trimesh.Trimesh(vn, fn, process=False)
    pts = tm.sample(n, seed=0) + rng.normal(scale=0.01, size=(n, 3))
    pts_t = torch.tensor(pts, dtype=torch.float32, device="cuda")
    bvh = cumesh.cuBVH(v.cuda(), f.cuda())
    d_gpu, face_id, _ = bvh.unsigned_distance(pts_t, return_uvw=True)
    _, d_ref, _ = trimesh.proximity.closest_point(tm, pts)
    err = np.abs(d_gpu.cpu().numpy() - d_ref)
    print(f"cuBVH vs trimesh on {n} pts: max|err|={err.max():.2e} mean|err|={err.mean():.2e} "
          f"bad(>1e-4)={(err > 1e-4).sum()}  face_id range=[{int(face_id.min())}, {int(face_id.max())}] "
          f"of {len(f)}", flush=True)


def main():
    raw = torch.load(sys.argv[1], weights_only=False)
    v, f = raw["vertices"].cuda(), raw["faces"].cuda()
    stats("raw", v, f)

    mesh = cumesh.CuMesh()
    mesh.init(v, f)
    mesh.fill_holes(max_hole_perimeter=3e-2)
    v1, f1 = mesh.read()
    stats("after fill_holes", v1, f1)

    bvh_check(v1, f1)

    aabb = torch.tensor([[-0.5] * 3, [0.5] * 3], device="cuda")
    vs = raw["voxel_size"]
    vs = torch.as_tensor(vs, dtype=torch.float32, device="cuda").reshape(-1).expand(3)
    resolution = ((aabb[1] - aabb[0]) / vs).round().int().max().item()
    band, scale = 1, 1.0
    bvh = cumesh.cuBVH(v1, f1)
    v2, f2 = cumesh.remeshing.remesh_narrow_band_dc(
        v1, f1, center=aabb.mean(0), scale=(resolution + 3 * band) / resolution * scale,
        resolution=resolution, band=band, project_back=0, verbose=True, bvh=bvh)
    stats(f"after remesh (res={resolution})", v2, f2)

    m2 = cumesh.CuMesh()
    m2.init(v2, f2)
    m2.simplify(1_000_000, verbose=False)
    stats("after simplify(1M)", *m2.read())

    # The non-remesh branch of to_glb, for comparison.
    m3 = cumesh.CuMesh()
    m3.init(v1, f1)
    m3.simplify(3_000_000, verbose=False)
    m3.remove_duplicate_faces()
    m3.repair_non_manifold_edges()
    m3.remove_small_connected_components(1e-5)
    stats("no-remesh cleanup", *m3.read())


if __name__ == "__main__":
    main()
