"""Stage 5b - what a 3D build draws with: mesh, material, texture, colour.

The sprite stages have nothing to say about a build whose art is geometry. There is
no atlas to cut and no sprite layer to compose, so those stages report zero and the
library looks empty even though the export is full of art.

What stands in for a sprite here is the chain a renderer actually walks:

    prefab -> MeshFilter/SkinnedMeshRenderer -> Mesh
                     \\-> MeshRenderer -> Material -> Texture
                                              \\-> colour, when no texture is bound

That last branch matters more than it sounds. A stylised build colours most of its
surfaces flat and binds no texture at all - in the build this was written against,
the chair, the flowers and the ground are lit shaders with a base colour and
nothing else. A view that only showed textures would show almost nothing.

Runs only when the profile says the build has 3D art; see ``assetlab.profile``.
"""

from __future__ import annotations

import argparse
import json
import re

import numpy as np
from PIL import Image
import sqlite3
from pathlib import Path

from .core import connect
from .mesh import Piece, parse_mesh, render

DOC_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)", re.M)
NAME_RE = re.compile(r"^  m_Name:\s*(.*?)\s*$", re.M)
GAMEOBJECT_RE = re.compile(r"^  m_GameObject:\s*\{fileID:\s*(-?\d+)\}", re.M)
FATHER_RE = re.compile(r"^  m_Father:\s*\{fileID:\s*(-?\d+)\}", re.M)
CHILDREN_RE = re.compile(r"^  m_Children:\s*\n((?:\s*- \{fileID:\s*-?\d+\}\s*\n)*)", re.M)
CHILD_ID_RE = re.compile(r"fileID:\s*(-?\d+)")
ROOT_BONE_RE = re.compile(r"^  m_RootBone:\s*\{fileID:\s*(-?\d+)\}", re.M)
FIRST_BONE_RE = re.compile(
    r"^  m_Bones:\s*\n  - \{fileID:\s*(-?\d+)\}", re.M)
MESH_REF_RE = re.compile(r"^  m_Mesh:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-f]{32})", re.M)
MATERIALS_RE = re.compile(r"^  m_Materials:\s*\n((?:\s*- \{fileID:[^\n]*\n)*)", re.M)
GUID_RE = re.compile(r"guid:\s*([0-9a-f]{32})")

# Material texture slots: the name, then the reference. fileID 0 means the slot
# exists on the shader but nothing is bound to it, which is most of them.
TEX_SLOT_RE = re.compile(
    r"^      _(\w+):\s*\n\s*m_Texture:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*([0-9a-f]{32}))?",
    re.M)
# Colours sit under m_Colors, one per line and without a list dash. _BaseColor is
# the URP name, _Color the built-in one; a material usually carries both.
COLOR_RE = re.compile(
    r"^      _(BaseColor|Color|TintColor|MainColor):\s*\{r:\s*([\d.eE+-]+),\s*"
    r"g:\s*([\d.eE+-]+),\s*b:\s*([\d.eE+-]+),\s*a:\s*([\d.eE+-]+)\}", re.M)

LOCAL_POS_RE = re.compile(
    r"^  m_LocalPosition:\s*\{x:\s*([-\d.eE+]+),\s*y:\s*([-\d.eE+]+),\s*z:\s*([-\d.eE+]+)\}", re.M)
LOCAL_ROT_RE = re.compile(
    r"^  m_LocalRotation:\s*\{x:\s*([-\d.eE+]+),\s*y:\s*([-\d.eE+]+),"
    r"\s*z:\s*([-\d.eE+]+),\s*w:\s*([-\d.eE+]+)\}", re.M)
LOCAL_SCALE_RE = re.compile(
    r"^  m_LocalScale:\s*\{x:\s*([-\d.eE+]+),\s*y:\s*([-\d.eE+]+),\s*z:\s*([-\d.eE+]+)\}", re.M)

MESH_FILTER = 33
MESH_RENDERER = 23
SKINNED_MESH_RENDERER = 137
GAME_OBJECT = 1
TRANSFORM = 4


def local_matrix(body: str) -> np.ndarray:
    """The 4x4 a Transform document describes: translate * rotate * scale."""
    position = LOCAL_POS_RE.search(body)
    rotation = LOCAL_ROT_RE.search(body)
    scale = LOCAL_SCALE_RE.search(body)

    matrix = np.eye(4, dtype=np.float32)
    if rotation:
        x, y, z, w = (float(v) for v in rotation.groups())
        matrix[:3, :3] = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ], dtype=np.float32)
    if scale:
        matrix[:3, :3] = matrix[:3, :3] @ np.diag(
            np.array([float(v) for v in scale.groups()], dtype=np.float32))
    if position:
        matrix[:3, 3] = [float(v) for v in position.groups()]
    return matrix


def project_is_linear(assets_root: Path) -> bool:
    """Whether the build renders in linear space (`m_ActiveColorSpace: 1`)."""
    settings = assets_root.parent / "ProjectSettings" / "ProjectSettings.asset"
    try:
        text = settings.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    found = re.search(r"^  m_ActiveColorSpace: (\d+)", text, re.M)
    return bool(found) and found.group(1) == "1"


def to_display(value: float, linear: bool) -> int:
    """One channel, as a screen would show it."""
    value = max(0.0, min(1.0, value))
    if linear:
        # sRGB transfer. Skipping it is what made every swatch read too dark.
        value = (1.055 * value ** (1 / 2.4) - 0.055) if value > 0.0031308 \
            else value * 12.92
    return max(0, min(255, round(value * 255)))


def parse_material(text: str, linear: bool = False) -> dict:
    """Bound textures and base colour of one material."""
    textures = []
    for slot, file_id, guid in TEX_SLOT_RE.findall(text):
        if file_id != "0" and guid:
            textures.append({"slot": slot, "guid": guid})

    colour = None
    found = COLOR_RE.search(text)
    if found:
        red, green, blue, alpha = (float(found.group(i)) for i in range(2, 6))
        channels = [to_display(c, linear) for c in (red, green, blue)]
        colour = {
            "hex": "#{:02x}{:02x}{:02x}".format(*channels),
            "rgb": channels,
            "alpha": round(alpha, 3),
        }
    return {"textures": textures, "colour": colour}


def parse_prefab_models(text: str) -> list[dict]:
    """Every mesh a prefab draws, with the materials bound to it."""
    parts = DOC_RE.split(text)
    docs = [(int(parts[i]), parts[i + 1], parts[i + 2])
            for i in range(1, len(parts) - 2, 3)]

    names: dict[str, str] = {}
    transforms: dict[str, dict] = {}
    meshes: dict[str, str] = {}          # gameObject -> mesh guid
    materials: dict[str, list[str]] = {}  # gameObject -> material guids
    skinned: set[str] = set()
    root_bones: dict[str, str] = {}      # gameObject of the renderer -> bone fileID

    for class_id, _file_id, body in docs:
        if class_id == GAME_OBJECT:
            found = NAME_RE.search(body)
            if found:
                names[_file_id] = found.group(1)
        elif class_id == TRANSFORM:
            owner = GAMEOBJECT_RE.search(body)
            if owner:
                children = CHILDREN_RE.search(body)
                father = FATHER_RE.search(body)
                transforms[_file_id] = {
                    "go": owner.group(1),
                    "father": father.group(1) if father else "0",
                    "children": (CHILD_ID_RE.findall(children.group(1))
                                 if children else []),
                    "local": local_matrix(body),
                }
        elif class_id in (MESH_FILTER, SKINNED_MESH_RENDERER):
            owner = GAMEOBJECT_RE.search(body)
            mesh = MESH_REF_RE.search(body)
            if owner and mesh:
                meshes[owner.group(1)] = mesh.group(1)
            if owner and class_id == SKINNED_MESH_RENDERER:
                skinned.add(owner.group(1))
                # A SkinnedMeshRenderer's own Transform does not place its mesh -
                # Unity drives the vertices from the bones, and the geometry is
                # stored in bind space. Here the renderer node carries a -90 deg X
                # rotation and the armature root carries +90 deg, so honouring the
                # renderer laid every character on its side.
                # The first bone is the one the first bind pose belongs to, so
                # the pair reconstructs the mesh's own local-to-world exactly.
                bone = FIRST_BONE_RE.search(body) or ROOT_BONE_RE.search(body)
                if bone and bone.group(1) != "0":
                    root_bones[owner.group(1)] = bone.group(1)
        if class_id in (MESH_RENDERER, SKINNED_MESH_RENDERER):
            owner = GAMEOBJECT_RE.search(body)
            block = MATERIALS_RE.search(body)
            if owner and block:
                materials.setdefault(owner.group(1), []).extend(
                    GUID_RE.findall(block.group(1)))

    # Object paths, so a model can be found again in the prefab it came from.
    child_ids = {c for r in transforms.values() for c in r["children"]}
    paths: dict[str, str] = {}
    world: dict[str, np.ndarray] = {}
    world_by_transform: dict[str, np.ndarray] = {}

    def walk(file_id: str, segments: list[str], parent: np.ndarray,
             seen: frozenset) -> None:
        record = transforms[file_id]
        here = parent @ record["local"]
        paths[record["go"]] = "/".join(segments)
        world[record["go"]] = here
        world_by_transform[file_id] = here
        for child in record["children"]:
            if child in transforms and child not in seen:
                walk(child, segments + [names.get(transforms[child]["go"], "")],
                     here, seen | {child})

    identity = np.eye(4, dtype=np.float32)
    for root in (f for f in transforms if f not in child_ids):
        walk(root, [], identity, frozenset({root}))

    found = []
    for gameobject, mesh_guid in meshes.items():
        found.append({
            "path": paths.get(gameobject, ""),
            "name": names.get(gameobject, ""),
            "mesh_guid": mesh_guid,
            "skinned": gameobject in skinned,
            "material_guids": materials.get(gameobject, []),
            "matrix": (world_by_transform.get(root_bones[gameobject])
                       if gameobject in root_bones
                       else world.get(gameobject)),
            "bone_placed": gameobject in root_bones,
        })
    return found


#: Sampling a 2048-square atlas per pixel is wasted work at thumbnail size, and a
#: build can hold dozens of them at once.
TEXTURE_SAMPLE = 512

#: A mesh with no material at all. Neutral rather than grey-green, so a flat-colour
#: model beside it still reads as coloured.
DEFAULT_COLOUR = (150, 155, 165)

#: Above this a prefab is a whole environment rather than a prop, and the rasteriser
#: costs more than the picture is worth.
SCENE_TRIANGLE_CAP = 400_000


class Renderer:
    """Draws meshes, caching the two expensive things: geometry and atlases."""

    def __init__(self, assets_root: Path, out_dir: Path):
        self.assets_root = assets_root
        self.dir = out_dir / "renders"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meshes: dict[str, object] = {}
        self._textures: dict[str, object] = {}

    def mesh(self, rel_path: str | None):
        if not rel_path:
            return None
        if rel_path not in self._meshes:
            self._meshes[rel_path] = parse_mesh(self.assets_root / rel_path)
        return self._meshes[rel_path]

    def texture(self, image_path: str | None):
        """The bound atlas, shrunk to sampling size. None when it cannot be read."""
        if not image_path:
            return None
        if image_path not in self._textures:
            candidate = Path(image_path)
            if not candidate.is_absolute():
                candidate = self.assets_root / image_path
            image = None
            try:
                if candidate.is_file():
                    image = Image.open(candidate).convert("RGB")
                    if max(image.size) > TEXTURE_SAMPLE:
                        image.thumbnail((TEXTURE_SAMPLE, TEXTURE_SAMPLE),
                                        Image.BILINEAR)
            except (OSError, ValueError):
                image = None
            self._textures[image_path] = image
        return self._textures[image_path]

    def piece(self, mesh, details: list[dict], matrix=None) -> Piece | None:
        """Bind a parsed mesh to the first texture or colour its materials offer."""
        if mesh is None:
            return None
        texture, colour = None, DEFAULT_COLOUR
        for detail in details:
            for bound in detail.get("textures") or ():
                # BaseMap/MainTex is the albedo; the slot is recorded without the
                # leading underscore Unity writes. A normal or mask map painted onto
                # the surface would be worse than no texture at all.
                if bound["slot"].lstrip("_") in ("MainTex", "BaseMap"):
                    texture = self.texture(bound.get("src") or bound.get("img"))
                    if texture is not None:
                        break
            if texture is not None:
                break
            if detail.get("colour", {}).get("rgb"):
                colour = tuple(detail["colour"]["rgb"])
        return Piece(mesh=mesh, transform=matrix, colour=colour, texture=texture)

    def draw(self, pieces: list, name: str, size: int = 320) -> str | None:
        pieces = [x for x in pieces if x is not None]
        if not pieces:
            return None
        try:
            image = render(pieces, size=size)
        except (ValueError, IndexError, MemoryError):
            return None
        if image is None:
            return None
        target = self.dir / f"{name}.png"
        image.save(target, optimize=True)
        return f"renders/{target.name}"


def build(assets_root: Path, conn: sqlite3.Connection,
          out_dir: Path | None = None) -> dict[str, int]:
    by_guid = {row["guid"]: dict(row) for row in conn.execute(
        "SELECT guid, id, name, rel_path, image_path, size_bytes FROM assets "
        "WHERE guid IS NOT NULL")}

    linear = project_is_linear(assets_root)
    material_cache: dict[str, dict] = {}

    def material_detail(guid: str) -> dict | None:
        if guid in material_cache:
            return material_cache[guid]
        asset = by_guid.get(guid)
        if not asset:
            return None
        try:
            parsed = parse_material(
                (assets_root / asset["rel_path"]).read_text(encoding="utf-8",
                                                            errors="ignore"), linear)
        except OSError:
            return None
        textures = []
        for entry in parsed["textures"]:
            texture = by_guid.get(entry["guid"])
            if texture and texture["image_path"]:
                # The id travels with the texture so a page that cannot reach the
                # export tree can fall back to the thumbnail it already generated.
                textures.append({"slot": entry["slot"], "name": texture["name"],
                                 "id": texture["id"], "img": texture["image_path"],
                                 "src": texture["rel_path"]})
        detail = {"name": asset["name"], "colour": parsed["colour"],
                  "textures": textures}
        material_cache[guid] = detail
        return detail

    prefabs = conn.execute(
        "SELECT id, name, rel_path FROM assets WHERE unity_type = 'Prefab'").fetchall()

    renderer = Renderer(assets_root, out_dir) if out_dir else None
    # One picture per distinct mesh, not per placement: the same chair leg appears
    # four times in a prefab and dozens of times across the build.
    drawn_meshes: dict[str, str | None] = {}

    rows, scenes = [], []
    stats = {"prefabs": len(prefabs), "models": 0, "skinned": 0,
             "textured": 0, "flat_colour": 0, "rendered": 0, "scenes": 0}
    for prefab in prefabs:
        try:
            text = (assets_root / prefab["rel_path"]).read_text(encoding="utf-8",
                                                                errors="ignore")
        except OSError:
            continue

        scene_pieces, scene_triangles = [], 0
        for model in parse_prefab_models(text):
            mesh = by_guid.get(model["mesh_guid"])
            details = [d for d in (material_detail(g) for g in model["material_guids"])
                       if d]
            if not mesh and not details:
                continue
            has_texture = any(d["textures"] for d in details)
            stats["models"] += 1
            stats["skinned"] += 1 if model["skinned"] else 0
            stats["textured"] += 1 if has_texture else 0
            stats["flat_colour"] += 1 if not has_texture and any(
                d["colour"] for d in details) else 0

            render_path, triangles, vertices = None, None, None
            if renderer and mesh:
                geometry = renderer.mesh(mesh["rel_path"])
                if geometry is not None:
                    triangles, vertices = len(geometry.triangles), len(geometry.vertices)
                    key = model["mesh_guid"]
                    if key not in drawn_meshes:
                        alone = renderer.piece(geometry, details)
                        drawn_meshes[key] = renderer.draw([alone], f"m{key[:12]}")
                        stats["rendered"] += 1 if drawn_meshes[key] else 0
                    render_path = drawn_meshes[key]
                    placement = model.get("matrix")
                    if (model.get("bone_placed") and placement is not None
                            and geometry.bindpose is not None):
                        placement = placement @ geometry.bindpose
                    placed = renderer.piece(geometry, details, placement)
                    if placed is not None:
                        scene_pieces.append(placed)
                        scene_triangles += triangles

            rows.append({
                "prefab_id": prefab["id"],
                "prefab_name": prefab["name"],
                "path": model["path"],
                "object_name": model["name"],
                "mesh_guid": model["mesh_guid"],
                "mesh_name": mesh["name"] if mesh else None,
                "mesh_bytes": mesh["size_bytes"] if mesh else None,
                "skinned": 1 if model["skinned"] else 0,
                "materials": json.dumps(details, separators=(",", ":")),
                "matrix": (json.dumps([round(float(v), 5) for v in
                                       model["matrix"].reshape(-1)])
                           if model.get("matrix") is not None else None),
                "render_path": render_path,
                "tri_count": triangles,
                "vert_count": vertices,
            })

        # A single-mesh prefab is already covered by that mesh's own picture.
        if renderer and len(scene_pieces) > 1 and scene_triangles <= SCENE_TRIANGLE_CAP:
            path = renderer.draw(scene_pieces, f"s{prefab['id']}", size=420)
            if path:
                scenes.append({"prefab_id": prefab["id"],
                               "prefab_name": prefab["name"], "render_path": path,
                               "part_count": len(scene_pieces),
                               "tri_count": scene_triangles})
                stats["scenes"] += 1

    conn.execute("DELETE FROM models")
    conn.executemany(
        """INSERT INTO models (prefab_id, prefab_name, path, object_name, mesh_guid,
                               mesh_name, mesh_bytes, skinned, materials, matrix,
                               render_path, tri_count, vert_count)
           VALUES (:prefab_id, :prefab_name, :path, :object_name, :mesh_guid,
                   :mesh_name, :mesh_bytes, :skinned, :materials, :matrix,
                   :render_path, :tri_count, :vert_count)""", rows)
    conn.execute("DELETE FROM scenes")
    conn.executemany(
        """INSERT INTO scenes (prefab_id, prefab_name, render_path, part_count,
                               tri_count)
           VALUES (:prefab_id, :prefab_name, :render_path, :part_count,
                   :tri_count)""", scenes)
    conn.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Map the mesh/material/texture chain of a 3D build.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    stats = build(args.export.resolve(), conn, args.out)
    print("models: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    conn.close()


if __name__ == "__main__":
    main()
