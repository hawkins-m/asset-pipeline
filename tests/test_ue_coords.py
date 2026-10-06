"""Blender -> UE transform conversion (conventions verified live in UE 5.8.3)."""
import math

import numpy as np
import pytest

from asset_pipeline import ue_coords as uc


def blender(yaw=0.0, loc=(0, 0, 0), pitch_x=0.0):
    """Blender world matrix: rotation about Z by yaw (deg), then about X; translation."""
    a, b = math.radians(yaw), math.radians(pitch_x)
    rz = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, math.cos(b), -math.sin(b)], [0, math.sin(b), math.cos(b)]])
    m = np.eye(4)
    m[:3, :3] = rz @ rx
    m[:3, 3] = loc
    return m


def test_translation_mirrors_y_and_scales_to_cm():
    """The live probe: Blender (1,0,0) -> UE (100,0,0); (0,2,0) -> (0,-200,0); (0,0,3) -> (0,0,300)."""
    for loc, want in [((1, 0, 0), [100, 0, 0]), ((0, 2, 0), [0, -200, 0]), ((0, 0, 3), [0, 0, 300])]:
        assert uc.to_ue(blender(loc=loc))["loc"] == pytest.approx(want)


def test_blender_yaw_becomes_negative_ue_yaw():
    t = uc.to_ue(blender(yaw=30))
    assert t["rot"] == pytest.approx([0, 0, -30], abs=1e-6)


@pytest.mark.parametrize("rot", [(0, 0, 0), (10, 20, 30), (-45, 60, 170), (90, -30, -120), (5, 89, 45)])
def test_rotator_matrix_roundtrip(rot):
    assert uc.rotator(uc.rotator_matrix(*rot)) == pytest.approx(list(rot), abs=1e-6)


def test_ue_rotator_matrix_matches_ue_conventions():
    """FRotationTranslationMatrix: yaw +90 turns +X into +Y, pitch +90 turns +X up (+Z),
    roll +90 turns +Y into -Z (M[1][2] = -SR*CP)."""
    assert uc.rotator_matrix(0, 0, 90)[:, 0] == pytest.approx([0, 1, 0], abs=1e-9)
    assert uc.rotator_matrix(0, 90, 0)[:, 0] == pytest.approx([0, 0, 1], abs=1e-9)
    assert uc.rotator_matrix(90, 0, 0)[:, 1] == pytest.approx([0, 0, -1], abs=1e-9)


def test_points_map_like_the_mesh_import():
    """A mesh vertex p placed by M in Blender ends up at C(M p) in UE when the mesh is
    imported mirrored (C p) and placed by to_ue(M)."""
    m = blender(yaw=37, loc=(5, -2, 1), pitch_x=20)
    t = uc.to_ue(m)
    p = np.array([0.3, 0.7, -0.2])
    mesh_vertex_ue = uc.C @ p * 100                          # how Interchange imports the vertex
    placed = uc.rotator_matrix(*t["rot"]) @ (mesh_vertex_ue * t["scale"]) + t["loc"]
    expected = uc.C @ (m[:3, :3] @ p + m[:3, 3]) * 100
    assert placed == pytest.approx(expected, abs=1e-6)


def test_from_ue_inverts_to_ue_and_relative_composes():
    parent, child = blender(yaw=40, loc=(10, 5, 2)), blender(yaw=95, loc=(12, 7, 3), pitch_x=10)
    assert uc.from_ue(uc.to_ue(child)) == pytest.approx(child, abs=1e-6)
    rel = uc.relative(parent, child)
    assert parent @ rel == pytest.approx(child)
    # composing in UE space gives the same world transform
    pu, ru = uc.from_ue(uc.to_ue(parent)), uc.from_ue(uc.to_ue(rel))
    assert pu @ ru == pytest.approx(child, abs=1e-6)


def test_camera_looks_where_the_blender_camera_looks():
    """A Blender camera at (0,-10,2) looking at the origin (+Y) -> UE camera at (0,1000,200)
    facing UE -Y (yaw -90), level."""
    # Blender: looking along +Y with +Z up -> camera local -Z = +Y, local +Y = +Z
    m = np.eye(4)
    m[:3, :3] = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]])   # columns: X, Y(up=+Z), Z(back=-Y)
    m[:3, 3] = [0, -10, 2]
    t = uc.camera_to_ue(m)
    assert t["loc"] == pytest.approx([0, 1000, 200])
    assert t["rot"] == pytest.approx([0, 0, -90], abs=1e-6)
    fwd = uc.rotator_matrix(*t["rot"])[:, 0]
    target = uc.C @ np.array([0, 0, 2]) * 100
    assert fwd == pytest.approx((target - t["loc"]) / np.linalg.norm(target - np.array(t["loc"])), abs=1e-6)
