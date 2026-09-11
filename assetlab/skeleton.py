"""A Spine skeleton's setup pose, drawn from the regions its atlas was cut into.

A Spine character ships as three files: the atlas page, a descriptor saying where each
region sits on the page, and a skeleton saying where each region goes. spine.py cuts
the regions; this reads the skeleton and puts them back together - bones posed as set
up, each slot showing its setup attachment, region quads and mesh triangles textured
from the cut regions, clipped where a clipping attachment says so. Without it a
character was its largest piece: a cape, and a head with holes where the eyes go,
because the eyes are separate regions that only the skeleton places on it.

Binary skeletons of Spine 3.8, 4.0 and 4.1 are read, and JSON skeletons of any version.
Across five builds that is 125 of 140 binary skeletons; one build's fifteen are Spine
2.1, whose binary format predates these and is not read. Only what the setup pose needs
is kept: events and animations are skipped, and constraints are read past rather than
applied, so an IK chain shows its bones as set up, not as solved. Additive slots - glows
and flashes - are left out of the still, where plain blending turns them into blots.
"""
from __future__ import annotations

import json
import math
import re
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

#: Longest side of a pose in pixels, how far a small skeleton may be enlarged, margin.
POSE_MAX = 512
POSE_UPSCALE = 4.0
PAD = 6
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
KINDS = ("region", "boundingbox", "mesh", "linkedmesh", "path", "point", "clipping")
MODES = {"normal": 0, "onlyTranslation": 1, "noRotationOrReflection": 2, "noScale": 3,
         "noScaleOrReflection": 4}
ADDITIVE = 1
DRAWN = ("region", "mesh", "linkedmesh")
#: Names a skeleton file takes beside its atlas descriptor.
SKELETON_SUFFIXES = (".skel.bytes", ".skel", ".json", ".json.txt")


# ------------------------------------------------------------------------ binary
class _Reader:
    """Spine's binary encoding: big-endian floats and ints, variable-length ints."""

    def __init__(self, data: bytes):
        self.data, self.pos, self.strings = data, 0, []
        self.major = self.minor = 0

    def byte(self) -> int:
        value = self.data[self.pos]
        self.pos += 1
        return value

    def boolean(self) -> bool:
        return self.byte() != 0

    def int32(self) -> int:
        value = struct.unpack_from(">i", self.data, self.pos)[0]
        self.pos += 4
        return value

    def floats(self, count: int) -> list[float]:
        values = list(struct.unpack_from(f">{count}f", self.data, self.pos))
        self.pos += 4 * count
        return values

    def varint(self) -> int:
        value = shift = 0
        while True:
            b = self.byte()
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7

    def string(self) -> str | None:
        count = self.varint()
        if count == 0:
            return None
        raw = self.data[self.pos:self.pos + count - 1]
        self.pos += count - 1
        return raw.decode("utf-8", "replace")

    def ref(self) -> str | None:
        index = self.varint()
        return None if index == 0 else self.strings[index - 1]

    def shorts(self) -> list[int]:
        count = self.varint()
        values = list(struct.unpack_from(f">{count}H", self.data, self.pos))
        self.pos += 2 * count
        return values

    def skip_indices(self) -> None:
        for _ in range(self.varint()):
            self.varint()


def _vertices(r: _Reader, count: int) -> dict:
    if not r.boolean():
        return {"weighted": False, "xy": r.floats(count * 2)}
    vertices = []
    for _ in range(count):
        bones = []
        for _ in range(r.varint()):
            bone = r.varint()
            x, y, weight = r.floats(3)
            bones.append((bone, x, y, weight))
        vertices.append(bones)
    return {"weighted": True, "bones": vertices}


def _sequence(r: _Reader) -> dict | None:
    """A region shown as one of numbered frames; written from Spine 4.1 on."""
    if (r.major, r.minor) < (4, 1) or not r.boolean():
        return None
    return {"count": r.varint(), "start": r.varint(), "digits": r.varint(),
            "setup": r.varint()}


def _attachment(r: _Reader, key: str, nonessential: bool) -> dict:
    name = r.ref() or key
    kind = KINDS[r.byte()]
    if kind == "region":
        path = r.ref() or name
        rotation, x, y, sx, sy, w, h = r.floats(7)
        color = r.int32()
        return {"type": kind, "path": path, "rotation": rotation, "x": x, "y": y,
                "sx": sx, "sy": sy, "w": w, "h": h, "color": color,
                "sequence": _sequence(r)}
    if kind == "mesh":
        path = r.ref() or name
        color = r.int32()
        count = r.varint()
        uvs = r.floats(count * 2)
        triangles = r.shorts()
        vertices = _vertices(r, count)
        r.varint()                                     # hull length
        sequence = _sequence(r)
        if nonessential:
            r.shorts()                                 # edges
            r.floats(2)                                # width, height
        return {"type": kind, "path": path, "color": color, "uvs": uvs,
                "triangles": triangles, "vertices": vertices, "sequence": sequence}
    if kind == "linkedmesh":
        path = r.ref() or name
        color = r.int32()
        skin, parent = r.ref(), r.ref()
        r.boolean()                                    # inherits deform/timelines
        sequence = _sequence(r)
        if nonessential:
            r.floats(2)
        return {"type": kind, "path": path, "color": color, "skin": skin,
                "parent": parent, "sequence": sequence}
    if kind == "clipping":
        end = r.varint()
        count = r.varint()
        vertices = _vertices(r, count)
        if nonessential:
            r.int32()
        return {"type": kind, "end": end, "count": count, "vertices": vertices}
    if kind == "boundingbox":
        _vertices(r, r.varint())
    elif kind == "path":
        r.boolean()
        r.boolean()
        count = r.varint()
        _vertices(r, count)
        r.floats(count // 3)
    elif kind == "point":
        r.floats(3)
    if nonessential:
        r.int32()
    return {"type": kind}


def _skin(r: _Reader, default: bool, nonessential: bool) -> dict | None:
    if default:
        count = r.varint()
        if count == 0:
            return None
        name = "default"
    else:
        name = r.ref()
        for _ in range(4):                             # bones, ik, transform, path
            r.skip_indices()
        count = r.varint()
    attachments = {}
    for _ in range(count):
        slot = r.varint()
        for _ in range(r.varint()):
            key = r.ref()
            attachments[(slot, key)] = _attachment(r, key, nonessential)
    return {"name": name, "attachments": attachments}


def read_binary(data: bytes) -> dict:
    r = _Reader(data)
    # 4.x opens with an 8-byte hash, 3.x with a hash string; the version string that
    # follows says which, and a hash that happens to start with a digit is no version.
    r.pos = 8
    version = r.string()
    if not version or not VERSION_RE.fullmatch(version):
        r.pos = 0
        r.string()
        version = r.string()
    if not version or not VERSION_RE.fullmatch(version):
        raise ValueError("not a Spine binary skeleton")
    r.major, r.minor = (int(part) for part in version.split(".")[:2])
    if (r.major, r.minor) < (3, 8):
        raise ValueError(f"Spine {version} binary skeletons are not read")
    r.floats(4)                                        # x, y, width, height
    nonessential = r.boolean()
    if nonessential:
        r.floats(1)
        r.string()
        r.string()
    r.strings = [r.string() for _ in range(r.varint())]

    bones = []
    for index in range(r.varint()):
        name = r.string()
        parent = None if index == 0 else r.varint()
        rotation, x, y, sx, sy, shx, shy, _length = r.floats(8)
        mode = r.varint()
        r.boolean()                                    # skin required
        if nonessential:
            r.int32()
        bones.append({"name": name, "parent": parent, "rotation": rotation, "x": x,
                      "y": y, "sx": sx, "sy": sy, "shx": shx, "shy": shy, "mode": mode})
    slots = []
    for _ in range(r.varint()):
        name = r.string()
        bone = r.varint()
        color = r.int32()
        r.int32()                                      # dark colour
        attachment = r.ref()
        blend = r.varint()
        slots.append({"name": name, "bone": bone, "color": color,
                      "attachment": attachment, "blend": blend})
    for _ in range(r.varint()):                        # IK constraints
        r.string(), r.varint(), r.boolean()
        r.skip_indices()
        r.varint()
        r.floats(2)
        r.byte(), r.boolean(), r.boolean(), r.boolean()
    for _ in range(r.varint()):                        # transform constraints
        r.string(), r.varint(), r.boolean()
        r.skip_indices()
        r.varint(), r.boolean(), r.boolean()
        r.floats(6 + (6 if r.major >= 4 else 4))
    for _ in range(r.varint()):                        # path constraints
        r.string(), r.varint(), r.boolean()
        r.skip_indices()
        r.varint(), r.varint(), r.varint(), r.varint()
        r.floats(3 + (3 if r.major >= 4 else 2))
    skins = []
    default = _skin(r, True, nonessential)
    if default:
        skins.append(default)
    for _ in range(r.varint()):
        skins.append(_skin(r, False, nonessential))
    return {"version": version, "bones": bones, "slots": slots, "skins": skins}


# -------------------------------------------------------------------------- JSON
def _hex(value: str | None) -> int:
    if not value:
        return -1                                      # 0xffffffff: white, opaque
    value = value.strip().lstrip("#")
    if len(value) == 6:
        value += "ff"
    number = int(value[:8], 16)
    return number - (1 << 32) if number >= 1 << 31 else number


def _json_vertices(values: list, count: int) -> dict:
    if len(values) == count * 2:
        return {"weighted": False, "xy": [float(v) for v in values]}
    vertices, i = [], 0
    while i < len(values):
        bones = []
        for _ in range(int(values[i])):
            bones.append((int(values[i + 1]), float(values[i + 2]), float(values[i + 3]),
                          float(values[i + 4])))
            i += 4
        i += 1
        vertices.append(bones)
    return {"weighted": True, "bones": vertices}


def read_json(text: str) -> dict:
    data = json.loads(text)
    version = str((data.get("skeleton") or {}).get("spine") or "")
    bones, bone_index = [], {}
    for bone in data.get("bones", []):
        mode = MODES.get(bone.get("transform", "normal"), 0)
        if ("transform" not in bone and bone.get("inheritRotation") is False
                and bone.get("inheritScale") is False):
            mode = 1                                   # Spine 2.x spelled it this way
        bone_index[bone["name"]] = len(bones)
        bones.append({"name": bone["name"], "parent": bone_index.get(bone.get("parent")),
                      "rotation": float(bone.get("rotation", 0)),
                      "x": float(bone.get("x", 0)), "y": float(bone.get("y", 0)),
                      "sx": float(bone.get("scaleX", 1)), "sy": float(bone.get("scaleY", 1)),
                      "shx": float(bone.get("shearX", 0)), "shy": float(bone.get("shearY", 0)),
                      "mode": mode})
    slots, slot_index = [], {}
    for slot in data.get("slots", []):
        slot_index[slot["name"]] = len(slots)
        slots.append({"name": slot["name"], "bone": bone_index.get(slot.get("bone"), 0),
                      "color": _hex(slot.get("color")), "attachment": slot.get("attachment"),
                      "blend": ADDITIVE if slot.get("blend") == "additive" else 0})
    raw = data.get("skins") or {}
    listed = raw if isinstance(raw, list) else [{"name": key, "attachments": value}
                                                for key, value in raw.items()]
    skins = []
    for skin in listed:
        attachments = {}
        for slot_name, entries in (skin.get("attachments") or {}).items():
            slot = slot_index.get(slot_name)
            if slot is None:
                continue
            for key, found in entries.items():
                kind = found.get("type", "region")
                kind = "mesh" if kind in ("skinnedmesh", "weightedmesh") else kind
                entry = {"type": kind, "path": found.get("path") or found.get("name") or key,
                         "color": _hex(found.get("color")), "sequence": None}
                if kind == "region":
                    entry.update(rotation=float(found.get("rotation", 0)),
                                 x=float(found.get("x", 0)), y=float(found.get("y", 0)),
                                 sx=float(found.get("scaleX", 1)),
                                 sy=float(found.get("scaleY", 1)),
                                 w=float(found.get("width", 0)), h=float(found.get("height", 0)))
                elif kind == "mesh":
                    uvs = [float(v) for v in found.get("uvs", [])]
                    entry.update(uvs=uvs, triangles=[int(v) for v in found.get("triangles", [])],
                                 vertices=_json_vertices(found.get("vertices", []), len(uvs) // 2))
                elif kind == "linkedmesh":
                    entry.update(parent=found.get("parent"), skin=found.get("skin"))
                elif kind == "clipping":
                    count = int(found.get("vertexCount", 0))
                    entry.update(end=slot_index.get(found.get("end"), slot), count=count,
                                 vertices=_json_vertices(found.get("vertices", []), count))
                attachments[(slot, key)] = entry
        skins.append({"name": skin.get("name"), "attachments": attachments})
    return {"version": version, "bones": bones, "slots": slots, "skins": skins}


def read_skeleton(data: bytes) -> dict:
    """A skeleton file of either encoding. Raises ValueError for one it cannot read."""
    if data.lstrip()[:1] == b"{":
        return read_json(data.decode("utf-8", "replace"))
    try:
        return read_binary(data)
    except (IndexError, struct.error, UnicodeDecodeError) as problem:
        raise ValueError(f"unreadable skeleton: {problem}") from problem


def skeleton_beside(descriptor: Path) -> Path | None:
    """The skeleton that shares an atlas descriptor's name, if the build ships one."""
    stem = re.sub(r"\.atlas(\.txt|\.bytes)?$", "", descriptor.name, flags=re.I).lower()
    for candidate in sorted(descriptor.parent.iterdir()):
        name = candidate.name.lower()
        if candidate.is_file() and any(name == stem + suffix for suffix in SKELETON_SUFFIXES):
            return candidate
    return None


# ---------------------------------------------------------------------- the pose
def world_bones(bones: list[dict]) -> list[tuple]:
    """Each bone's setup-pose world transform (a, b, c, d, x, y), in Spine's y-up space."""
    world: list[tuple] = []
    for bone in bones:
        r, shx, shy = bone["rotation"], bone["shx"], bone["shy"]
        la = math.cos(math.radians(r + shx)) * bone["sx"]
        lb = math.cos(math.radians(r + 90 + shy)) * bone["sy"]
        lc = math.sin(math.radians(r + shx)) * bone["sx"]
        ld = math.sin(math.radians(r + 90 + shy)) * bone["sy"]
        if bone["parent"] is None:
            world.append((la, lb, lc, ld, bone["x"], bone["y"]))
            continue
        pa, pb, pc, pd, px, py = world[bone["parent"]]
        x = pa * bone["x"] + pb * bone["y"] + px
        y = pc * bone["x"] + pd * bone["y"] + py
        if bone["mode"] == 1:                          # translation only
            world.append((la, lb, lc, ld, x, y))
        else:
            world.append((pa * la + pb * lc, pa * lb + pb * ld,
                          pc * la + pd * lc, pc * lb + pd * ld, x, y))
    return world


def _points(vertices: dict, count: int, bone: tuple, world: list[tuple]) -> list[tuple]:
    if not vertices["weighted"]:
        a, b, c, d, bx, by = bone
        xy = vertices["xy"]
        return [(a * xy[2 * i] + b * xy[2 * i + 1] + bx, c * xy[2 * i] + d * xy[2 * i + 1] + by)
                for i in range(count)]
    points = []
    for bones in vertices["bones"]:
        wx = wy = 0.0
        for index, vx, vy, weight in bones:
            a, b, c, d, bx, by = world[index]
            wx += (a * vx + b * vy + bx) * weight
            wy += (c * vx + d * vy + by) * weight
        points.append((wx, wy))
    return points


def _rgba(value: int) -> tuple:
    value &= 0xFFFFFFFF
    return tuple(((value >> shift) & 255) / 255 for shift in (24, 16, 8, 0))


def region_name(attachment: dict) -> str:
    sequence = attachment.get("sequence")
    if not sequence:
        return attachment["path"]
    frame = sequence["start"] + sequence["setup"]
    return f"{attachment['path']}{str(frame).zfill(sequence['digits'])}"


def pieces(skeleton: dict, regions: dict) -> list[dict]:
    """What the setup pose draws, back to front: textured triangles, and clip polygons.

    `regions` maps a region name to {"image", "w", "h", "orig", "offset"}: the cut
    image, its size, and - where the packer stripped whitespace - the region's
    original size and where the kept part sat in it.
    """
    world = world_bones(skeleton["bones"])
    lookup: dict = {}
    for skin in reversed(skeleton["skins"]):
        lookup.update(skin["attachments"])
    drawn: list[dict] = []
    for index, slot in enumerate(skeleton["slots"]):
        if not slot["attachment"]:
            continue
        attachment = lookup.get((index, slot["attachment"]))
        if not attachment:
            continue
        bone = world[slot["bone"]]
        if attachment["type"] == "clipping":
            drawn.append({"slot": index, "end": attachment["end"],
                          "clip": _points(attachment["vertices"], attachment["count"],
                                          bone, world)})
            continue
        if attachment["type"] not in DRAWN or slot["blend"] == ADDITIVE:
            continue
        geometry = attachment
        if attachment["type"] == "linkedmesh":
            geometry = lookup.get((index, attachment["parent"]))
            if not geometry or geometry["type"] != "mesh":
                continue
        region = regions.get(region_name(attachment))
        if not region:
            continue
        w, h = region["w"], region["h"]
        ow, oh = region.get("orig") or (w, h)
        ox, oy = region.get("offset") or (0.0, 0.0)
        colour = tuple(p * q for p, q in zip(_rgba(slot["color"]), _rgba(attachment["color"])))
        if attachment["type"] == "region":
            a, b, c, d, bx, by = bone
            scale_x = attachment["w"] / ow * attachment["sx"]
            scale_y = attachment["h"] / oh * attachment["sy"]
            x1 = -attachment["w"] / 2 * attachment["sx"] + ox * scale_x
            y1 = -attachment["h"] / 2 * attachment["sy"] + oy * scale_y
            x2, y2 = x1 + w * scale_x, y1 + h * scale_y
            cos = math.cos(math.radians(attachment["rotation"]))
            sin = math.sin(math.radians(attachment["rotation"]))
            corners = []
            for lx, ly in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):      # BL BR TR TL
                px = lx * cos - ly * sin + attachment["x"]
                py = lx * sin + ly * cos + attachment["y"]
                corners.append((a * px + b * py + bx, c * px + d * py + by))
            source = [(0, h), (w, h), (w, 0), (0, 0)]
            triangles = [((source[0], source[1], source[2]), (corners[0], corners[1], corners[2])),
                         ((source[2], source[3], source[0]), (corners[2], corners[3], corners[0]))]
        else:
            count = len(geometry["uvs"]) // 2
            points = _points(geometry["vertices"], count, bone, world)
            uvs = geometry["uvs"]
            # UVs span the region as it was before stripping, measured down from the top.
            source = [(uvs[2 * i] * ow - ox, uvs[2 * i + 1] * oh - (oh - oy - h))
                      for i in range(count)]
            order = geometry["triangles"]
            triangles = [((source[order[k]], source[order[k + 1]], source[order[k + 2]]),
                          (points[order[k]], points[order[k + 1]], points[order[k + 2]]))
                         for k in range(0, len(order) - 2, 3)]
        drawn.append({"slot": index, "image": region["image"], "colour": colour,
                      "triangles": triangles})
    return drawn


def _paint(drawn: list[dict], bounds: tuple) -> tuple[Image.Image, float]:
    x0, y0, x1, y1 = bounds                            # in canvas orientation, y down
    span = max(x1 - x0, y1 - y0)
    scale = min(POSE_MAX / span, POSE_UPSCALE)
    width = math.ceil((x1 - x0) * scale) + 2 * PAD
    height = math.ceil((y1 - y0) * scale) + 2 * PAD
    canvas = Image.new("RGBA", (width, height))
    images: dict = {}
    clip, clip_end = None, -1

    def place(x: float, y: float) -> tuple[float, float]:
        return (x - x0) * scale + PAD, (-y - y0) * scale + PAD

    for item in drawn:
        if clip is not None and item["slot"] > clip_end:
            clip = None
        if "clip" in item:
            clip = Image.new("L", (width, height), 0)
            ImageDraw.Draw(clip).polygon([place(x, y) for x, y in item["clip"]], fill=255)
            clip_end = item["end"]
            continue
        key = (item["image"], item["colour"])
        if key not in images:
            try:
                with Image.open(item["image"]) as opened:
                    image = opened.convert("RGBA")
            except (OSError, ValueError):
                image = None
            if image is not None and min(item["colour"]) < 0.999:
                image = Image.merge("RGBA", [band.point(lambda v, k=k: round(v * k))
                                             for band, k in zip(image.split(), item["colour"])])
            images[key] = image
        image = images[key]
        if image is None:
            continue
        # Each attachment is painted into a layer of its own and composited once. Two
        # triangles that share an edge both cover its pixels, and blending each straight
        # onto the canvas doubled the alpha there: a soft shadow came out ruled with
        # dark lines along its mesh.
        layer = Image.new("RGBA", (width, height))
        for source, target in item["triangles"]:
            corners = [place(x, y) for x, y in target]
            # A triangle thinner than a pixel covers no pixel centre on a GPU. Filled
            # as a polygon it is still a one-pixel line, which is how a shading mesh
            # ruled a stray line from a cannon's wheel to the edge of its picture.
            (ax, ay), (bx, by), (cx, cy) = corners
            longest = max(math.dist(corners[0], corners[1]), math.dist(corners[1], corners[2]),
                          math.dist(corners[2], corners[0]))
            if not longest or abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) / longest < 1.0:
                continue
            system = np.array([[x, y, 1.0] for x, y in corners])
            try:
                ux = np.linalg.solve(system, [p[0] for p in source])
                uy = np.linalg.solve(system, [p[1] for p in source])
            except np.linalg.LinAlgError:
                continue
            left = max(0, math.floor(min(p[0] for p in corners)))
            top = max(0, math.floor(min(p[1] for p in corners)))
            wide = min(width, math.ceil(max(p[0] for p in corners)) + 1) - left
            tall = min(height, math.ceil(max(p[1] for p in corners)) + 1) - top
            if wide <= 0 or tall <= 0:
                continue
            piece = image.transform(
                (wide, tall), Image.AFFINE,
                (ux[0], ux[1], ux[2] + ux[0] * left + ux[1] * top,
                 uy[0], uy[1], uy[2] + uy[0] * left + uy[1] * top),
                resample=Image.BICUBIC)
            mask = Image.new("L", (wide, tall), 0)
            ImageDraw.Draw(mask).polygon([(x - left, y - top) for x, y in corners], fill=255)
            layer.paste(piece, (left, top), mask)
        if clip is not None:
            layer.putalpha(Image.composite(layer.getchannel("A"),
                                           Image.new("L", (width, height), 0), clip))
        canvas.alpha_composite(layer)
    return canvas, scale


def _seen(canvas: Image.Image) -> tuple | None:
    return canvas.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()


def draw_pose(skeleton: dict, regions: dict, target: Path) -> dict | None:
    """Draw the setup pose into target. -> {"size", "fill", "pieces"}, or None."""
    drawn = pieces(skeleton, regions)
    points = [p for item in drawn if "triangles" in item
              for _, triangle in item["triangles"] for p in triangle]
    if not points:
        return None
    bounds = (min(p[0] for p in points), min(-p[1] for p in points),
              max(p[0] for p in points), max(-p[1] for p in points))
    if max(bounds[2] - bounds[0], bounds[3] - bounds[1]) <= 0:
        return None
    canvas, scale = _paint(drawn, bounds)
    seen = _seen(canvas)
    if not seen:
        return None
    inner = (canvas.width - 2 * PAD) * (canvas.height - 2 * PAD)
    if (seen[2] - seen[0]) * (seen[3] - seen[1]) < 0.8 * inner:
        bounds = (bounds[0] + (seen[0] - PAD) / scale, bounds[1] + (seen[1] - PAD) / scale,
                  bounds[0] + (seen[2] - PAD) / scale, bounds[1] + (seen[3] - PAD) / scale)
        canvas, scale = _paint(drawn, bounds)
        seen = _seen(canvas) or (0, 0, canvas.width, canvas.height)
    canvas = canvas.crop((max(0, seen[0] - PAD), max(0, seen[1] - PAD),
                          min(canvas.width, seen[2] + PAD), min(canvas.height, seen[3] + PAD)))
    solid = canvas.getchannel("A").point(lambda v: 255 if v > 24 else 0).histogram()[255]
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target)
    return {"size": canvas.size, "fill": solid / (canvas.width * canvas.height),
            "pieces": sum(1 for item in drawn if "triangles" in item)}
