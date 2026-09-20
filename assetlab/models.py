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

from .core import connect, stream_documents
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


def _collect(documents) -> tuple:
    """Read a stream of YAML documents into the hierarchy they describe.

    Taken whole, this is what both a prefab and a scene are: named GameObjects,
    Transforms that place them, and renderers that say what to draw. The only
    difference is size, which is why the documents arrive as an iterator.
    """
    names: dict[str, str] = {}
    transforms: dict[str, dict] = {}
    meshes: dict[str, str] = {}          # gameObject -> mesh guid
    materials: dict[str, list[str]] = {}  # gameObject -> material guids
    skinned: set[str] = set()
    root_bones: dict[str, str] = {}      # gameObject of the renderer -> bone fileID

    for class_id, _file_id, body in documents:
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

    return names, transforms, meshes, materials, skinned, root_bones


def _place(names: dict, transforms: dict) -> tuple:
    """Walk the hierarchy from its roots, recording each node's world matrix.

    Iterative throughout. The recursive version this replaces was fine on prefabs
    and took the interpreter down with it on a scene: a build that assembles a
    level from a prop library nests two hundred thousand transforms, far past the
    stack.
    """
    child_ids = {c for r in transforms.values() for c in r["children"]}
    roots = [f for f in transforms if f not in child_ids]
    paths: dict[str, str] = {}
    world: dict[str, np.ndarray] = {}
    world_by_transform: dict[str, np.ndarray] = {}
    kids: dict[str, list[str]] = {}

    identity = np.eye(4, dtype=np.float32)
    seen: set[str] = set()
    for root in roots:
        if root in seen:
            continue
        stack = [(root, [], identity)]
        seen.add(root)
        while stack:
            file_id, segments, parent = stack.pop()
            record = transforms[file_id]
            here = parent @ record["local"]
            paths[record["go"]] = "/".join(segments)
            world[record["go"]] = here
            world_by_transform[file_id] = here
            children = [c for c in record["children"]
                        if c in transforms and c not in seen]
            kids[file_id] = children
            for child in children:
                seen.add(child)
                stack.append(
                    (child, segments + [names.get(transforms[child]["go"], "")], here))
    return paths, world, world_by_transform, kids, roots


def _models(gathered: tuple, placed: tuple, only: set | None = None) -> list[dict]:
    """The drawn nodes of a hierarchy, or of one subtree of it."""
    names, transforms, meshes, materials, skinned, root_bones = gathered
    paths, world, world_by_transform, _kids, _roots = placed
    found = []
    # Iterating the subtree, not the whole hierarchy: a scene holds tens of
    # thousands of meshes and is cut into thousands of objects, so filtering the
    # full set per object is the difference between seconds and an afternoon.
    wanted = meshes.items() if only is None else (
        (go, meshes[go]) for go in only if go in meshes)
    for gameobject, mesh_guid in wanted:
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


def parse_prefab_models(text: str) -> list[dict]:
    """Every mesh a prefab draws, with the materials bound to it."""
    parts = DOC_RE.split(text)
    documents = [(int(parts[i]), parts[i + 1], parts[i + 2])
                 for i in range(1, len(parts) - 2, 3)]
    gathered = _collect(documents)
    return _models(gathered, _place(gathered[0], gathered[1]))


#: A subtree drawing at most this many meshes may stand as one object even when
#: nothing else in the scene looks like it. The repetition rule below does the real
#: work; this only stops a one-off corner of a scene from being reported whole.
SCENE_SEGMENT_CAP = 16


def parse_scene_objects(path: Path, cap: int = SCENE_SEGMENT_CAP) -> list[dict]:
    """Objects recovered from a scene, one entry per distinct thing it draws.

    A prefab is one file, so the file is the object. A scene holds every object at
    once and the authored boundaries are gone: the build inlines each placed prefab
    into the scene's own hierarchy. What survives is repetition. A prefab exists
    because it is used more than once, so the subtree shape that occurs again is
    the prefab, and the node where that shape starts is the boundary.

    The shape of a subtree is what it draws - the meshes and the materials bound to
    them - and deliberately not where it sits. Placement was in the key at first and
    the match rate collapsed to nothing: a build that scatters props jitters every
    instance, so two copies of a rock never agree on position.

    A size cap is kept for the remainder. Where nothing repeats - a scene's one-off
    corner, a small hand-built level - it stops the whole branch being reported as a
    single object.
    """
    gathered = _collect(stream_documents(path))
    names, transforms, meshes = gathered[0], gathered[1], gathered[2]
    materials = gathered[3]
    placed = _place(names, transforms)
    node_paths, _world, _world_by_transform, kids, roots = placed

    # Depth-first order, so a node can be settled after everything beneath it.
    order: list[str] = []
    stack = list(roots)
    while stack:
        file_id = stack.pop()
        order.append(file_id)
        stack.extend(kids.get(file_id, ()))

    # Bottom-up in one pass each: how much a subtree draws, and a hash standing for
    # what it draws. The hash folds in the children's, so equal hashes mean equal
    # subtrees - the same trick a content-addressed tree uses, and the reason this
    # costs one pass rather than one comparison per pair.
    drawn: dict[str, int] = {}
    shape: dict[str, int] = {}
    for file_id in reversed(order):
        gameobject = transforms[file_id]["go"]
        total = 1 if gameobject in meshes else 0
        for kid in kids.get(file_id, ()):
            total += drawn[kid]
        drawn[file_id] = total
        shape[file_id] = hash((
            meshes.get(gameobject), tuple(materials.get(gameobject, ())),
            tuple(sorted(shape[kid] for kid in kids.get(file_id, ())))))

    seen_shape: dict[int, int] = {}
    for file_id in order:
        if drawn[file_id]:
            seen_shape[shape[file_id]] = seen_shape.get(shape[file_id], 0) + 1

    # Does anything below this node repeat? If so the boundary is further down.
    below: dict[str, bool] = {}
    for file_id in reversed(order):
        below[file_id] = any(
            below[kid] or (drawn[kid] and seen_shape.get(shape[kid], 0) > 1)
            for kid in kids.get(file_id, ()))

    cuts: list[str] = []
    stack = list(roots)
    while stack:
        file_id = stack.pop()
        if not drawn[file_id]:
            continue                       # draws nothing: a marker, a spawn point
        repeated = seen_shape.get(shape[file_id], 0) > 1
        if repeated or (drawn[file_id] <= cap and not below[file_id]):
            cuts.append(file_id)
        else:
            stack.extend(kids.get(file_id, ()))

    groups: dict[int, list[str]] = {}
    for file_id in cuts:
        groups.setdefault(shape[file_id], []).append(file_id)

    objects = []
    for key, members in groups.items():
        # Unity names a duplicate `Bench (1)`; the shortest name in the group is the
        # one that was not renamed, so that member speaks for the rest.
        first = min(members, key=lambda f: (len(names.get(transforms[f]["go"], "")),
                                            names.get(transforms[f]["go"], "")))
        subtree, inner = set(), [first]
        while inner:
            node = inner.pop()
            subtree.add(transforms[node]["go"])
            inner.extend(kids.get(node, ()))
        label = names.get(transforms[first]["go"], "")
        models = _models(gathered, placed, only=subtree)
        objects.append({
            "name": label or "(unnamed)",
            "path": node_paths.get(transforms[first]["go"], ""),
            "placements": len(members),
            "models": models,
            "parts": len(models),
        })
    objects.sort(key=lambda entry: (-entry["placements"], entry["name"]))
    return objects


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


#: Assembled prefabs are drawn larger than individual meshes; there are two orders
#: of magnitude fewer of them, so the cost is small and the gain is where a reader
#: spends their attention.
SCENE_SIZE = 640


#: How much scene YAML one run will read. Sized from measurement, not taste: a
#: generated level is around half a gigabyte and twenty of them repeat the same prop
#: library, so the first few buy nearly every distinct object and the rest buy
#: minutes. Raise it to read a build exhaustively.
SCENE_READ_BUDGET = 2 << 30


def build(assets_root: Path, conn: sqlite3.Connection,
          out_dir: Path | None = None,
          scene_budget: int = SCENE_READ_BUDGET) -> dict[str, int]:
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
    # Smallest first. A build that generates its levels writes half a gigabyte of
    # YAML per level and ships twenty of them; reading every one costs more than the
    # rest of the pipeline together and adds almost nothing, because the levels are
    # assembled from the same prop library. The budget is spent where it buys the
    # most distinct objects, and what it could not reach is reported rather than
    # quietly dropped.
    scene_rows = conn.execute(
        "SELECT id, name, rel_path FROM assets WHERE unity_type = 'Scene'").fetchall()
    scene_files = []
    for row in scene_rows:
        try:
            scene_files.append((( assets_root / row["rel_path"]).stat().st_size, row))
        except OSError:
            continue
    scene_files.sort(key=lambda pair: pair[0])

    renderer = Renderer(assets_root, out_dir) if out_dir else None
    # One picture per distinct mesh, not per placement: the same chair leg appears
    # four times in a prefab and dozens of times across the build.
    drawn_meshes: dict[str, str | None] = {}

    rows, scenes = [], []
    stats = {"prefabs": len(prefabs), "models": 0, "skinned": 0,
             "textured": 0, "flat_colour": 0, "rendered": 0, "scenes": 0,
             "scene_files": 0, "scene_files_skipped": 0, "scene_objects": 0,
             "scene_placements": 0}
    def sources():
        """Every object to draw: one per prefab file, then one per scene object."""
        for prefab in prefabs:
            try:
                text = (assets_root / prefab["rel_path"]).read_text(
                    encoding="utf-8", errors="ignore")
            except OSError:
                continue
            yield prefab["id"], prefab["name"], None, 1, parse_prefab_models(text)

        budget = scene_budget
        for size, row in scene_files:
            if size > budget:
                stats["scene_files_skipped"] += 1
                continue
            budget -= size
            try:
                objects = parse_scene_objects(assets_root / row["rel_path"])
            except (OSError, MemoryError):
                stats["scene_files_skipped"] += 1
                continue
            stats["scene_files"] += 1
            for entry in objects:
                stats["scene_objects"] += 1
                stats["scene_placements"] += entry["placements"]
                yield (row["id"], row["name"], entry["name"], entry["placements"],
                       entry["models"])

    for holder_id, holder_name, object_label, placements, parsed in sources():
        scene_pieces, scene_triangles = [], 0
        for model in parsed:
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
                "prefab_id": holder_id,
                "prefab_name": holder_name,
                "placements": placements,
                "path": model["path"],
                "object_name": object_label or model["name"],
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

        # A single-mesh object is already covered by that mesh's own picture.
        if renderer and len(scene_pieces) > 1 and scene_triangles <= SCENE_TRIANGLE_CAP:
            # An assembled prefab is the picture a reader actually studies - it
            # carries the whole object rather than one of its parts - so it gets
            # the larger canvas. Single meshes stay small: there are thousands of
            # them, and at 320 they already read as what they are.
            key = f"s{holder_id}" if object_label is None \
                else f"s{holder_id}_{len(scenes)}"
            path = renderer.draw(scene_pieces, key, size=SCENE_SIZE)
            if path:
                scenes.append({"prefab_id": holder_id,
                               "prefab_name": holder_name,
                               "object_name": object_label,
                               "placements": placements, "render_path": path,
                               "part_count": len(scene_pieces),
                               "tri_count": scene_triangles})
                stats["scenes"] += 1

    conn.execute("DELETE FROM models")
    conn.executemany(
        """INSERT INTO models (prefab_id, prefab_name, path, object_name, mesh_guid,
                               mesh_name, mesh_bytes, skinned, materials, matrix,
                               render_path, tri_count, vert_count, placements)
           VALUES (:prefab_id, :prefab_name, :path, :object_name, :mesh_guid,
                   :mesh_name, :mesh_bytes, :skinned, :materials, :matrix,
                   :render_path, :tri_count, :vert_count, :placements)""", rows)
    conn.execute("DELETE FROM scenes")
    conn.executemany(
        """INSERT INTO scenes (prefab_id, prefab_name, object_name, placements,
                               render_path, part_count, tri_count)
           VALUES (:prefab_id, :prefab_name, :object_name, :placements,
                   :render_path, :part_count, :tri_count)""", scenes)
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
