"""Stage 5c - play a rigged character's animation as pictures.

Sprite animation is a slideshow: the clip names a sprite per frame and the catalogue
shows them in order. A rigged character has no frames to show. Its clip names bones
and gives each one a curve, and what moves is geometry - so the only way to see it is
to do the work the engine does: pose the skeleton at a moment in time, push the
vertices through it, and draw the result.

    clip curves -> local TRS per bone at time t
                -> world matrix per bone, down the prefab's own hierarchy
                -> skin matrix = boneWorld @ bindPose
                -> vertex = sum of its four weighted skin matrices
                -> raster

That is the whole of linear blend skinning, and it is enough: these clips carry no
blend shapes and no root motion.

Before this stage the build measured here showed one playable animation out of six.
The other five were rigged - `Idle`, `EatingLoop`, `TapAnimation` - and were listed
with an empty preview, which reads as a broken extraction rather than as a format the
tool had not learned.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

from .core import connect
from .mesh import Piece, parse_mesh, render
from .models import Renderer, local_matrix, parse_material, project_is_linear

CURVE_BLOCK_RE = re.compile(
    r"^  (m_PositionCurves|m_RotationCurves|m_ScaleCurves|m_EulerCurves):\s*$",
    re.M)
ENTRY_RE = re.compile(r"^  - curve:\s*$", re.M)
PATH_RE = re.compile(r"^    (?:path|attribute): (.*?)\s*$", re.M)
KEY_RE = re.compile(
    r"^        time: ([-\d.eE+]+)\s*\n"
    r"        value: \{x: ([-\d.eE+]+), y: ([-\d.eE+]+), z: ([-\d.eE+]+)"
    r"(?:, w: ([-\d.eE+]+))?\}", re.M)
CONTROLLER_RE = re.compile(
    r"^  m_Controller:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-f]{32})", re.M)
# Indented inside m_AnimationClipSettings, not at the document's top level.
STOP_TIME_RE = re.compile(r"^\s+m_StopTime: ([-\d.eE+]+)", re.M)
SAMPLE_RATE_RE = re.compile(r"^\s+m_SampleRate: ([-\d.eE+]+)", re.M)
GUID_RE = re.compile(r"guid:\s*([0-9a-f]{32})")

#: More than this and the strip is long to build and long to scroll; the motion in a
#: looping idle is legible well before it.
MAX_FRAMES = 16
FRAME_SIZE = 260


# ------------------------------------------------------------------ clip reading


def parse_clip(text: str) -> dict:
    """Curves keyed by object path: {path: {'pos'|'rot'|'scale'|'euler': [(t, v)]}}."""
    tracks: dict[str, dict[str, list]] = {}
    kinds = {"m_PositionCurves": "pos", "m_RotationCurves": "rot",
             "m_ScaleCurves": "scale", "m_EulerCurves": "euler"}

    marks = [(m.start(), m.group(1)) for m in CURVE_BLOCK_RE.finditer(text)]
    marks.append((len(text), None))
    for index, (start, name) in enumerate(marks[:-1]):
        kind = kinds.get(name)
        if not kind:
            continue
        block = text[start:marks[index + 1][0]]
        for entry in ENTRY_RE.split(block)[1:]:
            path = PATH_RE.search(entry)
            if not path:
                continue
            keys = []
            for time, x, y, z, w in KEY_RE.findall(entry):
                value = [float(x), float(y), float(z)]
                if w:
                    value.append(float(w))
                keys.append((float(time), np.array(value, dtype=np.float32)))
            if keys:
                tracks.setdefault(path.group(1), {})[kind] = keys

    stop = STOP_TIME_RE.search(text)
    rate = SAMPLE_RATE_RE.search(text)
    return {"tracks": tracks,
            "duration": float(stop.group(1)) if stop else 0.0,
            "rate": float(rate.group(1)) if rate else 30.0}


def sample(keys: list, time: float, rotation: bool = False) -> np.ndarray:
    """Value at `time`, interpolated between the keys either side of it."""
    if time <= keys[0][0]:
        return keys[0][1]
    if time >= keys[-1][0]:
        return keys[-1][1]
    for index in range(1, len(keys)):
        if keys[index][0] >= time:
            before_t, before = keys[index - 1]
            after_t, after = keys[index]
            span = after_t - before_t
            blend = 0.0 if span <= 0 else (time - before_t) / span
            if rotation:
                # Take the short way round; the sign of a quaternion is free.
                if float(before @ after) < 0:
                    after = -after
                mixed = before * (1 - blend) + after * blend
                length = np.linalg.norm(mixed)
                return mixed / length if length else before
            return before * (1 - blend) + after * blend
    return keys[-1][1]


def trs(position, rotation, scale) -> np.ndarray:
    """A 4x4 from the three sampled components."""
    matrix = np.eye(4, dtype=np.float32)
    if rotation is not None:
        x, y, z, w = rotation
        matrix[:3, :3] = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ], dtype=np.float32)
    if scale is not None:
        matrix[:3, :3] = matrix[:3, :3] @ np.diag(scale.astype(np.float32))
    if position is not None:
        matrix[:3, 3] = position
    return matrix


# ------------------------------------------------------------- prefab skeletons


DOC_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)", re.M)
NAME_RE = re.compile(r"^  m_Name:\s*(.*?)\s*$", re.M)
GAMEOBJECT_RE = re.compile(r"^  m_GameObject:\s*\{fileID:\s*(-?\d+)\}", re.M)
CHILDREN_RE = re.compile(r"^  m_Children:\s*\n((?:\s*- \{fileID:\s*-?\d+\}\s*\n)*)",
                         re.M)
CHILD_ID_RE = re.compile(r"fileID:\s*(-?\d+)")
MESH_REF_RE = re.compile(
    r"^  m_Mesh:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-f]{32})", re.M)
BONES_RE = re.compile(r"^  m_Bones:\s*\n((?:\s*- \{fileID:\s*-?\d+\}\s*\n)*)", re.M)
MATERIALS_RE = re.compile(r"^  m_Materials:\s*\n((?:\s*- \{fileID:[^\n]*\n)*)", re.M)

#: The component that plays the clip. Its GameObject is the root every path in the
#: clip is written relative to.
ANIMATOR = 95


def parse_rigs(text: str) -> list[dict]:
    """Every SkinnedMeshRenderer in a prefab, with the skeleton that drives it."""
    parts = DOC_RE.split(text)
    docs = [(int(parts[i]), parts[i + 1], parts[i + 2])
            for i in range(1, len(parts) - 2, 3)]

    names: dict[str, str] = {}
    transforms: dict[str, dict] = {}
    animator_objects: set[str] = set()
    renderers = []
    for class_id, file_id, body in docs:
        if class_id == 1:
            found = NAME_RE.search(body)
            if found:
                names[file_id] = found.group(1)
        elif class_id == 4:
            owner = GAMEOBJECT_RE.search(body)
            if owner:
                children = CHILDREN_RE.search(body)
                transforms[file_id] = {
                    "go": owner.group(1),
                    "children": (CHILD_ID_RE.findall(children.group(1))
                                 if children else []),
                    "local": local_matrix(body),
                }
        elif class_id == ANIMATOR:
            owner = GAMEOBJECT_RE.search(body)
            if owner:
                animator_objects.add(owner.group(1))
        elif class_id == 137:
            mesh = MESH_REF_RE.search(body)
            bones = BONES_RE.search(body)
            materials = MATERIALS_RE.search(body)
            if mesh and bones:
                renderers.append({
                    "mesh_guid": mesh.group(1),
                    "bones": CHILD_ID_RE.findall(bones.group(1)),
                    "material_guids": (GUID_RE.findall(materials.group(1))
                                       if materials else []),
                })

    # An AnimationClip addresses objects relative to the GameObject that carries the
    # Animator, not to the prefab's root. Here the Animator sits four levels down, on
    # `PenguinOptimizedCap`, so the clip says `Armature/Bone` where a root-relative
    # walk says `View/PenguinView/PenguinOptimizedCap/Armature/Bone`. Measured from
    # the wrong root not one of the fifteen tracks matched, and every clip silently
    # rendered its bind pose instead - the same picture for every clip in the build.
    paths: dict[str, str] = {}
    child_ids = {c for r in transforms.values() for c in r["children"]}

    def walk(file_id: str, segments: list[str], seen: frozenset) -> None:
        paths[file_id] = "/".join(segments)
        for child in transforms[file_id]["children"]:
            if child in transforms and child not in seen:
                walk(child, segments + [names.get(transforms[child]["go"], "")],
                     seen | {child})

    animator_roots = [f for f, r in transforms.items()
                      if r["go"] in animator_objects]
    roots = animator_roots or [f for f in transforms if f not in child_ids]
    for root in roots:
        walk(root, [], frozenset({root}))

    for renderer in renderers:
        renderer["paths"] = paths
        renderer["transforms"] = transforms
    return renderers


def pose(transforms: dict, paths: dict, clip: dict, time: float) -> dict:
    """World matrix per Transform with the clip's curves applied at `time`."""
    tracks = clip["tracks"]
    world: dict[str, np.ndarray] = {}
    child_ids = {c for r in transforms.values() for c in r["children"]}

    def walk(file_id: str, parent: np.ndarray, seen: frozenset) -> None:
        record = transforms[file_id]
        track = tracks.get(paths.get(file_id, ""))
        if track:
            local = trs(
                sample(track["pos"], time) if "pos" in track else record["local"][:3, 3],
                sample(track["rot"], time, rotation=True) if "rot" in track else None,
                sample(track["scale"], time) if "scale" in track else None)
            if "rot" not in track:
                # Keep the authored orientation and scale for a channel the clip
                # does not touch, rather than resetting it to identity.
                local[:3, :3] = record["local"][:3, :3]
            elif "scale" not in track:
                authored = record["local"][:3, :3]
                factors = np.linalg.norm(authored, axis=0)
                local[:3, :3] = local[:3, :3] @ np.diag(factors)
        else:
            local = record["local"]

        here = parent @ local
        world[file_id] = here
        for child in record["children"]:
            if child in transforms and child not in seen:
                walk(child, here, seen | {child})

    identity = np.eye(4, dtype=np.float32)
    for root in (f for f in transforms if f not in child_ids):
        walk(root, identity, frozenset({root}))
    return world


def deform(mesh, bone_matrices: np.ndarray) -> np.ndarray:
    """Linear blend skinning: each vertex follows its four bones by their weights."""
    count = len(mesh.vertices)
    homogeneous = np.concatenate(
        [mesh.vertices, np.ones((count, 1), np.float32)], axis=1)

    limit = len(bone_matrices) - 1
    indices = np.clip(mesh.skin_bones, 0, limit)
    weights = mesh.skin_weights

    moved = np.zeros((count, 3), dtype=np.float32)
    for slot in range(4):
        weight = weights[:, slot:slot + 1]
        if not np.any(weight):
            continue
        matrices = bone_matrices[indices[:, slot]]           # (count, 4, 4)
        contribution = np.einsum("nij,nj->ni", matrices, homogeneous)[:, :3]
        moved += weight * contribution
    return moved


# --------------------------------------------------------------------- the stage


def build(assets_root: Path, conn: sqlite3.Connection, out_dir: Path) -> dict[str, int]:
    by_guid = {row["guid"]: dict(row) for row in conn.execute(
        "SELECT guid, id, name, rel_path, image_path FROM assets WHERE guid IS NOT NULL")}
    linear = project_is_linear(assets_root)
    renderer = Renderer(assets_root, out_dir)
    frame_dir = out_dir / "renders"

    # Which clips a prefab can play, through its Animator's controller.
    clips_for: dict[int, list[str]] = {}
    for prefab in conn.execute(
            "SELECT id, rel_path FROM assets WHERE unity_type = 'Prefab'"):
        try:
            text = (assets_root / prefab["rel_path"]).read_text(encoding="utf-8",
                                                               errors="ignore")
        except OSError:
            continue
        found = CONTROLLER_RE.search(text)
        if not found:
            continue
        controller = by_guid.get(found.group(1))
        if not controller:
            continue
        try:
            body = (assets_root / controller["rel_path"]).read_text(
                encoding="utf-8", errors="ignore")
        except OSError:
            continue
        clips_for[prefab["id"]] = [
            guid for guid in dict.fromkeys(GUID_RE.findall(body))
            if by_guid.get(guid, {}).get("rel_path", "").endswith(".anim")]

    stats = {"rigs": 0, "clips": 0, "poses": 0, "frames": 0}
    for prefab_id, clip_guids in clips_for.items():
        if not clip_guids:
            continue
        prefab = conn.execute("SELECT rel_path, name FROM assets WHERE id = ?",
                              (prefab_id,)).fetchone()
        text = (assets_root / prefab["rel_path"]).read_text(encoding="utf-8",
                                                            errors="ignore")
        rigs = [r for r in parse_rigs(text) if r["mesh_guid"] in by_guid]
        if not rigs:
            continue

        prepared = []
        for rig in rigs:
            asset = by_guid[rig["mesh_guid"]]
            mesh = parse_mesh(assets_root / asset["rel_path"])
            if (mesh is None or mesh.skin_weights is None
                    or mesh.bindposes is None):
                continue
            details = []
            for guid in rig["material_guids"]:
                material = by_guid.get(guid)
                if not material:
                    continue
                try:
                    parsed = parse_material(
                        (assets_root / material["rel_path"]).read_text(
                            encoding="utf-8", errors="ignore"), linear)
                except OSError:
                    continue
                textures = []
                for entry in parsed["textures"]:
                    texture = by_guid.get(entry["guid"])
                    if texture:
                        textures.append({"slot": entry["slot"],
                                         "src": texture["rel_path"],
                                         "img": texture["image_path"]})
                details.append({"name": material["name"], "colour": parsed["colour"],
                                "textures": textures})
            prepared.append((rig, mesh, details))
        if not prepared:
            continue
        stats["rigs"] += 1

        for clip_guid in clip_guids:
            asset = by_guid[clip_guid]
            try:
                clip = parse_clip((assets_root / asset["rel_path"]).read_text(
                    encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            if not clip["tracks"]:
                continue

            # A clip one frame long is a pose. Sampling it sixteen times would
            # produce sixteen identical pictures and call the result an animation.
            span = clip["duration"] * clip["rate"]
            is_pose = span <= 1.5
            steps = 1 if is_pose else max(2, min(MAX_FRAMES, int(round(span))))
            frames = []
            for step in range(steps):
                time = clip["duration"] * step / steps if steps > 1 else 0.0
                pieces = []
                for rig, mesh, details in prepared:
                    world = pose(rig["transforms"], rig["paths"], clip, time)
                    matrices = np.stack([
                        world.get(bone, np.eye(4, dtype=np.float32))
                        @ mesh.bindposes[min(index, len(mesh.bindposes) - 1)]
                        for index, bone in enumerate(rig["bones"])])
                    posed = mesh.__class__(
                        mesh.name, deform(mesh, matrices), None, mesh.uvs,
                        mesh.triangles, mesh.submesh_ranges)
                    piece = renderer.piece(posed, details)
                    if piece:
                        pieces.append(piece)
                if not pieces:
                    break
                name = f"a{asset['id']}_{prefab_id}_{step}"
                path = renderer.draw(pieces, name, size=FRAME_SIZE)
                if path:
                    frames.append([round(time, 4), path])
            if not frames:
                continue

            stats["poses" if is_pose else "clips"] += 1
            stats["frames"] += len(frames)
            conn.execute(
                """UPDATE animations
                      SET frames = ?, duration = ?, sample_rate = ?, frame_count = ?,
                          track_path = ?, curve_summary = ?
                    WHERE asset_id = ?""",
                (json.dumps(frames, separators=(",", ":")), clip["duration"],
                 clip["rate"], len(frames), prefab["name"],
                 f"rigged {'pose' if is_pose else 'motion'} · "
                 f"{len(clip['tracks'])} bones", asset["id"]))
    conn.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render rigged animation clips as frame sequences.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    stats = build(args.export.resolve(), conn, args.out)
    print("skin: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    conn.close()


if __name__ == "__main__":
    main()
