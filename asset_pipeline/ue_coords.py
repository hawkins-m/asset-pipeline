"""Blender (right-handed, Z up, metres) -> Unreal Engine (left-handed, Z up, centimetres).

Verified live in UE 5.8.3: Interchange imports a Blender-exported glTF so that Blender +X ->
UE +X, Blender +Y -> UE -Y, Blender +Z -> UE +Z, metres -> cm. Meshes therefore arrive
mirrored in Y, and every placement must be mirrored the same way: a Blender transform M
becomes C·M·C with C = diag(1, -1, 1), and its translation is scaled by 100. Conjugation
keeps composition (C(AB)C = CAC·CBC), so relative transforms convert the same way.

UE rotators: unreal.Rotator(roll, pitch, yaw) in degrees. The matrix convention below is
UE's FRotationMatrix with the axes as columns (X forward, Y right, Z up).
"""
import math

import numpy as np

C = np.diag([1.0, -1.0, 1.0])
M_TO_CM = 100.0


def rotator_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """3x3 rotation (columns = the rotated X, Y, Z axes) of a UE rotator in degrees."""
    r, p, y = (math.radians(v) for v in (roll, pitch, yaw))
    sr, cr, sp, cp, sy, cy = math.sin(r), math.cos(r), math.sin(p), math.cos(p), math.sin(y), math.cos(y)
    x_axis = [cp * cy, cp * sy, sp]
    y_axis = [sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp]
    z_axis = [-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp]
    return np.array([x_axis, y_axis, z_axis]).T


def rotator(rot: np.ndarray) -> list[float]:
    """[roll, pitch, yaw] in degrees of a 3x3 rotation (FMatrix::Rotator)."""
    x, y, z = rot[:, 0], rot[:, 1], rot[:, 2]
    pitch = math.atan2(x[2], math.hypot(x[0], x[1]))
    yaw = math.atan2(x[1], x[0])
    sy_axis = np.array([-math.sin(yaw), math.cos(yaw), 0.0])     # Y axis of (pitch, yaw, roll 0)
    roll = math.atan2(float(z @ sy_axis), float(y @ sy_axis))
    return [round(math.degrees(v), 6) + 0.0 for v in (roll, pitch, yaw)]


def _split(m: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """4x4 -> rotation, scale (per axis, positive), translation."""
    lin = m[:3, :3]
    scale = np.linalg.norm(lin, axis=0)
    rot = lin / np.where(scale == 0, 1, scale)
    if np.linalg.det(rot) < 0:  # a mirrored transform: carry the flip in X's scale
        rot[:, 0] *= -1
        scale[0] *= -1
    return rot, scale, m[:3, 3]


def to_ue(m) -> dict:
    """A Blender 4x4 (world or relative) -> UE {"loc" cm, "rot" [roll, pitch, yaw], "scale"}."""
    m = np.asarray(m, dtype=float)
    rot, scale, t = _split(m)
    return {"loc": [round(float(v), 4) for v in C @ t * M_TO_CM],
            "rot": rotator(C @ rot @ C),
            "scale": [round(float(v), 6) for v in scale]}


def camera_to_ue(m) -> dict:
    """A Blender camera's world 4x4 -> UE transform for a CineCameraActor. Blender cameras
    look down their local -Z with +Y up; UE cameras look along +X with +Z up."""
    m = np.asarray(m, dtype=float)
    rot, _, t = _split(m)
    fwd, up = C @ -rot[:, 2], C @ rot[:, 1]
    fwd, up = fwd / np.linalg.norm(fwd), up / np.linalg.norm(up)
    right = np.cross(up, fwd)
    return {"loc": [round(float(v), 4) for v in C @ t * M_TO_CM],
            "rot": rotator(np.stack([fwd, right, up], axis=1)), "scale": [1.0, 1.0, 1.0]}


def from_ue(t: dict) -> np.ndarray:
    """UE {"loc", "rot", "scale"} -> Blender 4x4 (inverse of to_ue)."""
    rot = C @ rotator_matrix(*t["rot"]) @ C
    m = np.eye(4)
    m[:3, :3] = rot * np.asarray(t.get("scale", [1, 1, 1]), dtype=float)
    m[:3, 3] = C @ np.asarray(t["loc"], dtype=float) / M_TO_CM
    return m


def relative(parent, child) -> np.ndarray:
    """child expressed in parent's frame (both Blender 4x4 world matrices)."""
    return np.linalg.inv(np.asarray(parent, dtype=float)) @ np.asarray(child, dtype=float)
