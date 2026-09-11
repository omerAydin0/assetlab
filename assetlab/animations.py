"""Stage 6b - reconstruct sprite-swap animations so clips can actually be watched.

An AnimationClip that animates `m_Sprite` stores one keyframe per frame, each a
time plus a Sprite GUID:

    m_PPtrCurves:
    - curve:
      - time: 0
        value: {fileID: 21300000, guid: 0a550cc0..., type: 2}
      attribute: m_Sprite
      path: AnimationSpriteRenderer

Every one of those GUIDs already has a cropped PNG from the slicing stage, so the
clip can be replayed frame by frame in the browser. Clips that only animate
transforms get a curve summary instead - there is nothing to show frame by frame.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from .core import connect

PPTR_BLOCK_RE = re.compile(r"^  m_PPtrCurves:\s*\n(.*?)(?=^  m_SampleRate:)", re.M | re.S)
# Each curve is a list item at two-space indent. Unity writes it either as
# `- serializedVersion: 2` or straight as `- curve:`, so split on the marker itself;
# keyframes sit deeper (`    - time:`) and are not affected.
CURVE_SPLIT_RE = re.compile(r"^  - ", re.M)
KEYFRAME_RE = re.compile(
    r"- time:\s*(-?[\d.eE+]+)\s*\n\s*value:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-f]{32})")
ATTRIBUTE_RE = re.compile(r"^\s*attribute:\s*(\S+)", re.M)
PATH_RE = re.compile(r"^\s*path:\s*(.*?)\s*$", re.M)
SAMPLE_RATE_RE = re.compile(r"^  m_SampleRate:\s*([\d.]+)", re.M)
CURVE_COUNT_RE = re.compile(
    r"^  m_(PositionCurves|RotationCurves|ScaleCurves|EulerCurves|FloatCurves):\s*(\[\])?", re.M)


TRANSFORM_BLOCK_RE = re.compile(
    r"^  m_(PositionCurves|EulerCurves|ScaleCurves):\s*\n(.*?)(?=^  m_[A-Z])", re.M | re.S)
# Unity writes small values in scientific notation (`1.8812716E-05`), so the
# exponent's sign has to be part of the pattern.
NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
XYZ_KEY_RE = re.compile(
    rf"- serializedVersion: \d+\s*\n\s*time:\s*({NUM})\s*\n\s*"
    rf"value:\s*\{{x:\s*({NUM}),\s*y:\s*({NUM}),\s*z:\s*({NUM})\}}")

# Rotation is written either as euler degrees or as quaternion keys, depending on
# how the clip was authored; a rig that uses the quaternion form has no euler block
# at all, so both have to be read or the part simply never turns.
QUAT_BLOCK_RE = re.compile(
    r"^  m_RotationCurves:\s*\n(.*?)(?=^  m_[A-Z])", re.M | re.S)
QUAT_KEY_RE = re.compile(
    rf"- serializedVersion: \d+\s*\n\s*time:\s*({NUM})\s*\n\s*"
    rf"value:\s*\{{x:\s*({NUM}),\s*y:\s*({NUM}),\s*z:\s*({NUM}),\s*w:\s*({NUM})\}}")


def parse_transform_curves(text: str) -> dict[str, dict[str, list]]:
    """object path -> {position|euler|scale: [[time, x, y, z], ...]}."""
    tracks: dict[str, dict[str, list]] = {}
    for kind, block in TRANSFORM_BLOCK_RE.findall(text):
        name = kind.replace("Curves", "").lower()
        for chunk in CURVE_SPLIT_RE.split(block):
            # z is kept because a 2D rotation lives entirely in the euler z channel.
            # Rounded because Unity writes ~9 significant digits, which is far more
            # precision than a preview needs and doubles the page payload.
            keys = [[round(float(t), 4), round(float(x), 4),
                     round(float(y), 4), round(float(z), 3)]
                    for t, x, y, z in XYZ_KEY_RE.findall(chunk)]
            if not keys:
                continue
            path = PATH_RE.search(chunk)
            tracks.setdefault((path.group(1) if path else "").strip(), {})[name] = keys

    block = QUAT_BLOCK_RE.search(text)
    for chunk in (CURVE_SPLIT_RE.split(block.group(1)) if block else ()):
        keys = [[round(float(t), 4), 0.0, 0.0,
                 round(quaternion_z_degrees(float(x), float(y), float(z), float(w)), 3)]
                for t, x, y, z, w in QUAT_KEY_RE.findall(chunk)]
        if not keys:
            continue
        # An angle read off a quaternion lies in (-180, 180], so a part turning past
        # half a turn jumps from 179 to -179 between two keys, and interpolating
        # that spins it the long way round. Each key takes the turn nearest the last.
        for previous, key in zip(keys, keys[1:]):
            while key[3] - previous[3] > 180:
                key[3] -= 360
            while key[3] - previous[3] < -180:
                key[3] += 360
            key[3] = round(key[3], 3)
        path = PATH_RE.search(chunk)
        tracks.setdefault((path.group(1) if path else "").strip(), {}).setdefault(
            "euler", keys)
    return tracks


FLOAT_BLOCK_RE = re.compile(r"^  m_FloatCurves:\s*\n(.*?)(?=^  m_[A-Z])", re.M | re.S)
SCALAR_KEY_RE = re.compile(
    rf"- serializedVersion: \d+\s*\n\s*time:\s*({NUM})\s*\n\s*value:\s*({NUM})")
IS_ACTIVE_RE = re.compile(r"^\s*m_IsActive:\s*(\d)", re.M)


def parse_float_curves(text: str) -> dict[str, dict[str, list]]:
    """object path -> {alpha|active: [[time, value], ...]}.

    Rigs hold several alternate parts at once - three eyelids, a smoke effect - and
    the clip fades or toggles them. Drawing every sprite at full opacity would show
    all the states stacked together.
    """
    tracks: dict[str, dict[str, list]] = {}
    block = FLOAT_BLOCK_RE.search(text)
    if not block:
        return tracks
    for chunk in CURVE_SPLIT_RE.split(block.group(1)):
        attribute = ATTRIBUTE_RE.search(chunk)
        if not attribute:
            continue
        name = attribute.group(1)
        kind = ("alpha" if name.endswith("m_Color.a")
                else "active" if name == "m_IsActive"
                else "ax" if name == "m_AnchoredPosition.x"
                else "ay" if name == "m_AnchoredPosition.y"
                else "enabled" if name == "m_Enabled" else None)
        if not kind:
            continue
        keys = [[round(float(t), 4), round(float(v), 4)]
                for t, v in SCALAR_KEY_RE.findall(chunk)]
        if not keys:
            continue
        path = PATH_RE.search(chunk)
        tracks.setdefault((path.group(1) if path else "").strip(), {})[kind] = keys
    return tracks


# Prefab parsing. A renderer only draws in the right place once its whole ancestor
# chain, its sprite's pivot and its sort key are known, so all three come from here.
# Class ids: 1 GameObject, 4 Transform, 210 SortingGroup, 212 SpriteRenderer,
# 224 RectTransform (a Canvas child - a different coordinate space, so it is skipped).
DOC_SPLIT_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)", re.M)
FIELD_NAME_RE = re.compile(r"^  m_Name:\s*(.*?)\s*$", re.M)
GAMEOBJECT_REF_RE = re.compile(r"^  m_GameObject:\s*\{fileID:\s*(-?\d+)\}", re.M)
LOCAL_POS_RE = re.compile(
    rf"^  m_LocalPosition:\s*\{{x:\s*({NUM}),\s*y:\s*({NUM}),\s*z:\s*({NUM})", re.M)
LOCAL_SCALE_RE = re.compile(rf"^  m_LocalScale:\s*\{{x:\s*({NUM}),\s*y:\s*({NUM})", re.M)
LOCAL_ROT_RE = re.compile(
    rf"^  m_LocalRotation:\s*\{{x:\s*({NUM}),\s*y:\s*({NUM}),\s*z:\s*({NUM}),\s*w:\s*({NUM})",
    re.M)
SPRITE_REF_RE = re.compile(r"^  m_Sprite:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-f]{32})", re.M)
# A SpriteMask (class 331) clips every renderer under it that asks to be visible
# inside a mask. Ignoring it lets a slot machine's reel - a 341 px rainbow burst
# behind a small window - cover the whole preview.
MASK_INTERACTION_RE = re.compile(r"^  m_MaskInteraction:\s*(\d)", re.M)
FATHER_RE = re.compile(r"^  m_Father:\s*\{fileID:\s*(-?\d+)\}", re.M)
CHILDREN_RE = re.compile(r"^  m_Children:\s*\n((?:\s*- \{fileID:\s*-?\d+\}\s*\n)*)", re.M)
CHILD_ID_RE = re.compile(r"fileID:\s*(-?\d+)")
SORTING_ORDER_RE = re.compile(r"^  m_SortingOrder:\s*(-?\d+)", re.M)
SORTING_LAYER_RE = re.compile(r"^  m_SortingLayer:\s*(-?\d+)", re.M)
# One shared scale for the preview, so sprites authored at different
# pixels-per-unit still sit at their true relative sizes.
RENDER_PPU = 100.0
ENABLED_RE = re.compile(r"^  m_Enabled:\s*(\d)", re.M)
FLIP_RE = re.compile(r"^  m_Flip(X|Y):\s*(\d)", re.M)
COLOR_RE = re.compile(
    rf"^  m_Color:\s*\{{r:\s*({NUM}),\s*g:\s*({NUM}),\s*b:\s*({NUM}),\s*a:\s*({NUM})\}}",
    re.M)
# A Sliced or Tiled renderer ignores the sprite's own size and draws into m_Size
# instead. Reading it back is what stops a nine-slice banner from rendering as the
# small blurred strip it is stored as.
DRAW_MODE_RE = re.compile(r"^  m_DrawMode:\s*(\d)", re.M)
DRAW_SIZE_RE = re.compile(rf"^  m_Size:\s*\{{x:\s*({NUM}),\s*y:\s*({NUM})\}}", re.M)

MAX_RIG_LAYERS = 400


def quaternion_z_degrees(x: float, y: float, z: float, w: float) -> float:
    """Resting rotation about z.

    A 2D part is often laid out pre-rotated in the prefab and only nudged by the
    clip, so ignoring this snaps it back upright and detaches it from the body.
    """
    return math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))


# A uGUI Image, recognised by the shape of its fields rather than by its script's name:
# nothing else carries a sprite, a fill method and a pixels-per-unit multiplier.
FILL_METHOD_RE = re.compile(r"^  m_FillMethod:", re.M)
IMAGE_TYPE_RE = re.compile(r"^  m_Type:\s*(\d)", re.M)
PRESERVE_RE = re.compile(r"^  m_PreserveAspect:\s*(\d)", re.M)
PPU_MULTIPLIER_RE = re.compile(rf"^  m_PixelsPerUnitMultiplier:\s*({NUM})", re.M)
REFERENCE_SIZE_RE = re.compile(
    rf"^  m_ReferenceResolution:\s*\{{x:\s*({NUM}),\s*y:\s*({NUM})\}}", re.M)
REFERENCE_PPU_RE = re.compile(rf"^  m_ReferencePixelsPerUnit:\s*({NUM})", re.M)
#: Where a prefab's UI is laid out against the whole canvas and names no reference
#: resolution of its own, a portrait phone screen is assumed.
DEFAULT_CANVAS = (1080.0, 1920.0)
#: uGUI draws after every sprite renderer, in hierarchy order.
UI_SORT = 10 ** 9


def _pair(field: str, body: str, default: float) -> tuple[float, float]:
    match = re.search(rf"^  {field}:\s*\{{x:\s*({NUM}),\s*y:\s*({NUM})", body, re.M)
    return (float(match.group(1)), float(match.group(2))) if match else (default, default)


def placed_size(record: dict, meta: dict) -> tuple[list[float], list[float]]:
    """How large a renderer draws its sprite, and where the pivot sits in it.

    A sprite renderer draws the sprite at its own size about the sprite's pivot, or
    stretched to m_Size when sliced or tiled. A uGUI Image fills its rect about the
    rect's pivot, and one that preserves aspect fits the sprite inside the rect,
    centred.
    """
    anchor = list(record.get("anchor") or meta["anchor"])
    if not record["draw"]:
        return list(meta["size"]), anchor
    size = [record["draw"][0] * RENDER_PPU, record["draw"][1] * RENDER_PPU]
    if record.get("aspect") and meta["size"][0] and meta["size"][1] and size[0] and size[1]:
        k = min(size[0] / meta["size"][0], size[1] / meta["size"][1])
        fitted = [meta["size"][0] * k, meta["size"][1] * k]
        anchor = [0.5 - (0.5 - anchor[0]) * size[0] / fitted[0],
                  0.5 - (0.5 - anchor[1]) * size[1] / fitted[1]]
        size = fitted
    return size, anchor


def is_sliced(record: dict, meta: dict) -> bool:
    if "slice" in record:
        return bool(record["slice"]) and bool(meta["border"])
    return bool(record["draw"]) and bool(meta["border"])


def border_scale(record: dict, meta: dict) -> float:
    """Rendered pixels per source pixel of a nine-slice border."""
    return meta["border_scale"] * record.get("border_factor", 1.0)


def ui_track(node: dict, floats: dict | None) -> list | None:
    """A RectTransform's position over a clip, from its anchored-position curves.

    uGUI animates m_AnchoredPosition, not m_LocalPosition, so the Transform curves a
    sprite rig moves by are simply absent - which is why one build's 170 clips drew
    nothing. The pivot sits at a fixed point set by the anchors plus the anchored
    position, so the curve is that fixed point plus the animated offset.
    """
    if not floats or "ui" not in node or not (floats.get("ax") or floats.get("ay")):
        return None
    fixed_x, fixed_y, rest_x, rest_y = node["ui"]
    xs, ys = floats.get("ax"), floats.get("ay")

    def at(keys: list | None, t: float, rest: float) -> float:
        if not keys:
            return rest
        if t <= keys[0][0]:
            return keys[0][1]
        for a, b in zip(keys, keys[1:]):
            if t <= b[0]:
                span = b[0] - a[0]
                return a[1] + (b[1] - a[1]) * ((t - a[0]) / span if span else 0.0)
        return keys[-1][1]

    times = sorted({k[0] for k in xs or []} | {k[0] for k in ys or []})
    return [[t, round((fixed_x + at(xs, t, rest_x)) / RENDER_PPU, 4),
             round((fixed_y + at(ys, t, rest_y)) / RENDER_PPU, 4), 0.0] for t in times]


def parse_prefab_rig(text: str) -> list[dict]:
    """One record per drawable SpriteRenderer, in back-to-front order.

    Each record carries the transform chain from the prefab root down to the
    renderer. Walking real transform ids rather than name paths keeps same-named
    siblings apart and keeps the root's own offset and scale, both of which a
    name-keyed dictionary silently drops.
    """
    parts = DOC_SPLIT_RE.split(text)
    docs: list[tuple[int, str, str]] = []
    for index in range(1, len(parts) - 2, 3):
        docs.append((int(parts[index]), parts[index + 1], parts[index + 2]))

    names: dict[str, str] = {}
    active: dict[str, bool] = {}
    transforms: dict[str, dict] = {}
    ui_transforms: set[str] = set()
    renderers: dict[str, dict] = {}
    masks: dict[str, str] = {}                   # gameObject fileID -> mask sprite guid
    groups: dict[str, tuple[int, int]] = {}      # gameObject fileID -> SortingGroup key
    rects: dict[str, dict] = {}                  # RectTransform fileID -> its layout
    images: dict[str, dict] = {}                 # gameObject fileID -> uGUI Image
    canvas: dict = {}                            # a CanvasScaler's reference, if any

    for class_id, file_id, body in docs:
        if class_id == 1:
            match = FIELD_NAME_RE.search(body)
            if match:
                names[file_id] = match.group(1)
            enabled = IS_ACTIVE_RE.search(body)
            active[file_id] = enabled.group(1) != "0" if enabled else True
        elif class_id in (4, 224):
            owner = GAMEOBJECT_REF_RE.search(body)
            if not owner:
                continue
            position = LOCAL_POS_RE.search(body)
            scale = LOCAL_SCALE_RE.search(body)
            rotation = LOCAL_ROT_RE.search(body)
            father = FATHER_RE.search(body)
            children = CHILDREN_RE.search(body)
            transforms[file_id] = {
                "go": owner.group(1),
                "father": father.group(1) if father else "0",
                "children": CHILD_ID_RE.findall(children.group(1)) if children else [],
                "x": float(position.group(1)) if position else 0.0,
                "y": float(position.group(2)) if position else 0.0,
                "z": float(position.group(3)) if position else 0.0,
                "sx": float(scale.group(1)) if scale else 1.0,
                "sy": float(scale.group(2)) if scale else 1.0,
                "rot": (quaternion_z_degrees(*(float(v) for v in rotation.groups()))
                        if rotation else 0.0),
            }
            if class_id == 224:
                ui_transforms.add(file_id)
                rects[file_id] = {"amin": _pair("m_AnchorMin", body, 0.5),
                                  "amax": _pair("m_AnchorMax", body, 0.5),
                                  "pos": _pair("m_AnchoredPosition", body, 0.0),
                                  "size": _pair("m_SizeDelta", body, 100.0),
                                  "pivot": _pair("m_Pivot", body, 0.5)}
        elif class_id == 114:
            owner = GAMEOBJECT_REF_RE.search(body)
            reference = REFERENCE_SIZE_RE.search(body)
            if reference:
                canvas["size"] = (float(reference.group(1)), float(reference.group(2)))
                ppu = REFERENCE_PPU_RE.search(body)
                if ppu:
                    canvas["ppu"] = float(ppu.group(1))
            sprite = SPRITE_REF_RE.search(body)
            if owner and sprite and FILL_METHOD_RE.search(body):
                enabled = ENABLED_RE.search(body)
                colour = COLOR_RE.search(body)
                kind = IMAGE_TYPE_RE.search(body)
                multiplier = PPU_MULTIPLIER_RE.search(body)
                images[owner.group(1)] = {
                    "guid": sprite.group(1),
                    "on": not (enabled and enabled.group(1) == "0"),
                    "rgb": ([round(float(colour.group(i)), 4) for i in (1, 2, 3)]
                            if colour else [1.0, 1.0, 1.0]),
                    "tint": float(colour.group(4)) if colour else 1.0,
                    "slice": bool(kind) and kind.group(1) == "1",
                    "aspect": bool(PRESERVE_RE.search(body))
                    and PRESERVE_RE.search(body).group(1) == "1",
                    "multiplier": float(multiplier.group(1)) if multiplier else 1.0}
        elif class_id == 331:
            owner = GAMEOBJECT_REF_RE.search(body)
            sprite = SPRITE_REF_RE.search(body)
            enabled = ENABLED_RE.search(body)
            if owner and sprite and not (enabled and enabled.group(1) == "0"):
                masks[owner.group(1)] = sprite.group(1)
        elif class_id == 210:
            owner = GAMEOBJECT_REF_RE.search(body)
            if owner:
                layer = SORTING_LAYER_RE.search(body)
                order = SORTING_ORDER_RE.search(body)
                groups[owner.group(1)] = (int(layer.group(1)) if layer else 0,
                                          int(order.group(1)) if order else 0)
        elif class_id == 212:
            owner = GAMEOBJECT_REF_RE.search(body)
            sprite = SPRITE_REF_RE.search(body)
            if not owner or not sprite:
                continue
            enabled = ENABLED_RE.search(body)
            if enabled and enabled.group(1) == "0":
                continue
            layer = SORTING_LAYER_RE.search(body)
            order = SORTING_ORDER_RE.search(body)
            colour = COLOR_RE.search(body)
            flip = dict(FLIP_RE.findall(body))
            mode = DRAW_MODE_RE.search(body)
            drawn = DRAW_SIZE_RE.search(body)
            interaction = MASK_INTERACTION_RE.search(body)
            renderers[owner.group(1)] = {
                "inside_mask": bool(interaction) and interaction.group(1) == "1",
                "draw": ([float(drawn.group(1)), float(drawn.group(2))]
                         if mode and mode.group(1) != "0" and drawn else None),
                "guid": sprite.group(1),
                "layer": int(layer.group(1)) if layer else 0,
                "order": int(order.group(1)) if order else 0,
                # Mirrored limbs are everywhere in these rigs; drawing one unflipped
                # points the arm the wrong way and detaches it from the body.
                "flip": (1 if flip.get("X") == "1" else 0)
                        | (2 if flip.get("Y") == "1" else 0),
                # The renderer multiplies the sprite by m_Color. Reading only the
                # alpha drew a black 95%-opacity dimmer as an opaque white slab.
                "rgb": ([round(float(colour.group(i)), 4) for i in (1, 2, 3)]
                        if colour else [1.0, 1.0, 1.0]),
                "tint": float(colour.group(4)) if colour else 1.0,
            }

    child_ids = {child for record in transforms.values() for child in record["children"]}
    roots = [file_id for file_id in transforms if file_id not in child_ids]

    # Unity decides masking by sorting range, not by hierarchy, and these prefabs
    # author the mask right beside what it clips as often as above it. Both shapes
    # are resolved: nearest mask ancestor first, then the nearest masked sibling.
    parent_of = {child: file_id for file_id, record in transforms.items()
                 for child in record["children"]}
    mask_siblings: dict[str, list[str]] = defaultdict(list)
    for file_id, record in transforms.items():
        if record["go"] in masks:
            mask_siblings[parent_of.get(file_id, "")].append(file_id)

    def mask_node(file_id: str, ancestors: list[dict]) -> dict:
        record = transforms[file_id]
        return {"guid": masks[record["go"]],
                "chain": ancestors + [{"path": "", "base": [record["x"], record["y"],
                                                            record["sx"], record["sy"]],
                                       "rot": record["rot"]}]}

    records: list[dict] = []
    counter = 0

    canvas_w, canvas_h = canvas.get("size", DEFAULT_CANVAS)
    reference_ppu = canvas.get("ppu", 100.0)

    def walk(file_id: str, segments: list[str], chain: list[dict],
             group: tuple[int, int] | None, visible: bool, seen: frozenset,
             mask: dict | None, ids: list[str],
             frame: tuple[float, float, float, float] | None = None) -> None:
        nonlocal counter
        record = transforms[file_id]
        gameobject = record["go"]
        node = {"path": "/".join(segments),
                "base": [record["x"], record["y"], record["sx"], record["sy"]],
                "rot": record["rot"]}
        own_frame = None
        rect = rects.get(file_id)
        if rect:
            # A RectTransform is laid out against its parent's rect: the anchors mark
            # two points on it, the pivot weighs a point between them, and the anchored
            # position offsets from there. Positions are canvas pixels, carried as
            # hundredths so the rig's pixels-per-unit brings them back to pixels.
            (min_x, min_y), (max_x, max_y) = rect["amin"], rect["amax"]
            if frame is None:
                parent_w, parent_h, parent_px, parent_py = canvas_w, canvas_h, 0.5, 0.5
            else:
                parent_w, parent_h, parent_px, parent_py = frame
            width = rect["size"][0] + (max_x - min_x) * parent_w
            height = rect["size"][1] + (max_y - min_y) * parent_h
            pivot_x, pivot_y = rect["pivot"]
            if frame is None:
                # The top of a UI tree: in a prefab of its own it is the canvas; below
                # a world transform it is a world-space canvas, whose scale turns its
                # pixels into world units.
                if record["father"] != "0":
                    node["base"][2] *= RENDER_PPU
                    node["base"][3] *= RENDER_PPU
                else:
                    node["base"][0] = node["base"][1] = 0.0
            else:
                fixed_x = -parent_px * parent_w + (min_x + (max_x - min_x) * pivot_x) * parent_w
                fixed_y = -parent_py * parent_h + (min_y + (max_y - min_y) * pivot_y) * parent_h
                node["base"][0] = (fixed_x + rect["pos"][0]) / RENDER_PPU
                node["base"][1] = (fixed_y + rect["pos"][1]) / RENDER_PPU
                node["ui"] = [fixed_x, fixed_y, rect["pos"][0], rect["pos"][1]]
            own_frame = (width, height, pivot_x, pivot_y)
        chain = chain + [node]
        visible = visible and active.get(gameobject, True)
        group = groups.get(gameobject, group)
        ids = ids + [file_id]
        if gameobject in masks:
            mask = {"guid": masks[gameobject], "chain": chain}
        renderer = renderers.get(gameobject)
        if renderer and renderer["inside_mask"] and mask is None:
            for depth in range(len(ids) - 1, -1, -1):
                siblings = mask_siblings.get(ids[depth])
                if siblings:
                    mask = mask_node(siblings[0], chain[:depth + 1])
                    break
        if renderer:
            counter += 1
            # Unity draws by sorting layer, then order in layer; a SortingGroup makes
            # its whole subtree sort as one unit. Ties fall back to camera distance
            # and then to hierarchy order, which is what the engine does and what
            # keeps two parts at the same order from swapping arbitrarily.
            records.append({**renderer, "path": chain[-1]["path"], "chain": chain,
                            "on": visible, "z": record["z"],
                            "mask": mask if renderer["inside_mask"] else None,
                            "sort": ((group or (renderer["layer"], renderer["order"])),
                                     renderer["layer"], renderer["order"],
                                     -record["z"], counter)})
        image = images.get(gameobject) if own_frame else None
        if image and image["on"]:
            counter += 1
            width, height, pivot_x, pivot_y = own_frame
            records.append({
                "guid": image["guid"], "path": chain[-1]["path"], "chain": chain,
                "draw": [width / RENDER_PPU, height / RENDER_PPU],
                # CSS measures the pivot down from the top; Unity measures it up.
                "anchor": [pivot_x, 1.0 - pivot_y], "slice": image["slice"],
                "aspect": image["aspect"],
                # A sliced Image scales its border by the canvas's reference pixels
                # per unit over the sprite's, divided by its own multiplier.
                "border_factor": reference_ppu / RENDER_PPU / (image["multiplier"] or 1.0),
                "flip": 0, "rgb": image["rgb"], "tint": image["tint"],
                "layer": 0, "order": 0, "inside_mask": False, "mask": None,
                "on": visible, "z": 0.0,
                "sort": ((UI_SORT, 0), UI_SORT, 0, 0.0, counter)})
        for child in record["children"]:
            if child in transforms and child not in seen:
                # A RectTransform under a plain Transform starts a UI tree of its own.
                walk(child, segments + [names.get(transforms[child]["go"], "")],
                     chain, group, visible, seen | {child}, mask, ids,
                     own_frame if child in rects else None)

    for root in roots:
        walk(root, [], [], None, True, frozenset({root}), None, [])
    records.sort(key=lambda record: record["sort"])
    return records


CONTROLLER_REF_RE = re.compile(
    r"^  m_Controller:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-f]{32})", re.M)
GUID_REF_RE = re.compile(r"guid:\s*([0-9a-f]{32})")


def prefab_animators(text: str) -> list[tuple[str, set[str]]]:
    """Where each animating component sits: (its object's rig path, what it plays).

    A clip's curve paths are relative to the object its Animator is on, not to the
    prefab root, and in these builds the Animator is almost never on the root -
    1,739 of 1,856 sit on a child. Guessing that object from the clip's own path
    names failed whenever the clip drove the Animator's object itself (path "") or
    sibling objects shared names, and then the clip moved the whole prefab: an
    elephant's sway rocked the map page it stands on, strip of ground and all. The
    path is in the rig records' form, the root being "", so it is the clip's prefix
    as it stands. An Animator names its controller; a legacy Animation its clips.
    """
    parts = DOC_SPLIT_RE.split(text)
    names: dict[str, str] = {}
    transform_of: dict[str, str] = {}
    owner_of: dict[str, str] = {}
    father: dict[str, str] = {}
    players: list[tuple[str, set[str]]] = []
    for index in range(1, len(parts) - 2, 3):
        class_id, file_id, body = int(parts[index]), parts[index + 1], parts[index + 2]
        if class_id == 1:
            match = FIELD_NAME_RE.search(body)
            if match:
                names[file_id] = match.group(1)
        elif class_id in (4, 224):
            owner = GAMEOBJECT_REF_RE.search(body)
            if owner:
                transform_of[owner.group(1)] = file_id
                owner_of[file_id] = owner.group(1)
                parent = FATHER_RE.search(body)
                father[file_id] = parent.group(1) if parent else "0"
        elif class_id in (95, 111):
            owner = GAMEOBJECT_REF_RE.search(body)
            if not owner:
                continue
            if class_id == 95:
                controller = CONTROLLER_REF_RE.search(body)
                plays = {controller.group(1)} if controller else set()
            else:
                plays = set(GUID_REF_RE.findall(body))
            if plays:
                players.append((owner.group(1), plays))

    found = []
    for gameobject, plays in players:
        transform = transform_of.get(gameobject)
        if transform is None:
            continue
        segments, seen = [], {transform}
        while father.get(transform, "0") != "0":
            segments.append(names.get(owner_of[transform], ""))
            transform = father[transform]
            if transform in seen or transform not in owner_of:
                break
            seen.add(transform)
        found.append(("/".join(reversed(segments)), plays))
    return found


def joined(prefix: str, path: str) -> str:
    """A clip path under an animator root. An empty clip path is the root itself."""
    if not prefix:
        return path
    return f"{prefix}/{path}" if path else prefix


def parse_clip(text: str) -> dict | None:
    """Return sprite tracks and a curve summary for one .anim file."""
    summary: dict[str, bool] = {}
    for name, empty in CURVE_COUNT_RE.findall(text):
        summary[name.replace("Curves", "").lower()] = not empty

    sample = SAMPLE_RATE_RE.search(text)
    sample_rate = float(sample.group(1)) if sample else None

    block = PPTR_BLOCK_RE.search(text)
    tracks: list[dict] = []
    if block and "curve:" in block.group(1):
        for chunk in CURVE_SPLIT_RE.split(block.group(1)):
            frames = [(float(t), guid) for t, guid in KEYFRAME_RE.findall(chunk)]
            if not frames:
                continue
            attribute = ATTRIBUTE_RE.search(chunk)
            path = PATH_RE.search(chunk)
            tracks.append({
                "attribute": attribute.group(1) if attribute else None,
                "path": (path.group(1) if path else "") or "",
                "frames": frames,
            })
    return {"sample_rate": sample_rate, "tracks": tracks,
            "curves": sorted(k for k, present in summary.items() if present),
            "transforms": parse_transform_curves(text),
            "floats": parse_float_curves(text)}


def load_sprite_meta(conn: sqlite3.Connection) -> tuple[dict[str, str], dict[str, dict]]:
    """Every drawable sprite: its image, and its size, pivot and nine-slice at render scale."""
    sprite_image: dict[str, str] = {}
    sprite_meta: dict[str, dict] = {}
    for row in conn.execute(
        """SELECT a.guid, a.image_path, a.width, a.height,
                  s.ppu, s.anchor_x, s.anchor_y, s.border
             FROM assets a LEFT JOIN sprites s ON s.asset_id = a.id
            WHERE a.unity_type='Sprite' AND a.guid IS NOT NULL
              AND a.image_path IS NOT NULL"""):
        sprite_image[row["guid"]] = row["image_path"]
        # Sizes are converted to one common scale. These games author at 140 pixels
        # per unit, so drawing atlas pixels against unit-based offsets made every
        # sprite 40% too large for the rig it belongs to.
        ppu = row["ppu"] or 100.0
        anchor_x = 0.5 if row["anchor_x"] is None else row["anchor_x"]
        anchor_y = 0.5 if row["anchor_y"] is None else row["anchor_y"]
        sprite_meta[row["guid"]] = {
            "img": row["image_path"],
            "size": [round((row["width"] or 0) * RENDER_PPU / ppu, 2),
                     round((row["height"] or 0) * RENDER_PPU / ppu, 2)],
            "anchor": [round(anchor_x, 4), round(anchor_y, 4)],
            # [left, bottom, right, top] in source pixels, plus the same widths at
            # render scale, which is what a CSS nine-slice needs.
            "border": (json.loads(row["border"]) if row["border"] else None),
            "border_scale": RENDER_PPU / ppu,
        }
    return sprite_image, sprite_meta


def build(assets_root: Path, conn: sqlite3.Connection) -> dict[str, int]:
    sprite_image, sprite_meta = load_sprite_meta(conn)
    clips = conn.execute(
        "SELECT id, guid, rel_path FROM assets WHERE unity_type='AnimationClip' AND ext='anim'"
    ).fetchall()

    # Which prefabs use each clip, so the animated object names can be resolved to
    # the sprites the prefab actually puts on them.
    holders: dict[int, list[str]] = defaultdict(list)
    prefab_id_of: dict[str, int] = {}
    for row in conn.execute(
        """SELECT u.asset_id, h.rel_path, h.id FROM used_by u
             JOIN assets h ON h.guid = u.holder_guid
            WHERE h.unity_type='Prefab'"""):
        holders[row["asset_id"]].append(row["rel_path"])
        prefab_id_of[row["rel_path"]] = row["id"]
    prefab_cache: dict[str, tuple[list[dict], list[tuple[str, set[str]]]]] = {}

    # What each controller plays, so a clip is bound to the Animator that actually
    # plays it. An override controller plays its base controller's clips as well as
    # the ones it substitutes, so one step further through the refs is followed.
    controllers = {row[0] for row in conn.execute(
        "SELECT guid FROM assets WHERE lower(ext) IN ('controller', 'overridecontroller') "
        "AND guid IS NOT NULL")}
    plays: dict[str, set[str]] = defaultdict(set)
    for src, dst in conn.execute("SELECT src_guid, dst_guid FROM refs"):
        if src in controllers:
            plays[src].add(dst)
    for guid in list(plays):
        for base in [dst for dst in plays[guid] if dst in controllers]:
            plays[guid] |= plays.get(base, set())

    rows, stats = [], {"clips": len(clips), "with_sprites": 0, "frames": 0,
                       "unresolved": 0, "rigged": 0, "layers": 0,
                       "bound": 0, "guessed": 0}
    for clip in clips:
        try:
            text = (assets_root / clip["rel_path"]).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        parsed = parse_clip(text)
        if parsed is None:
            continue
        sprite_tracks = [t for t in parsed["tracks"] if t["attribute"] == "m_Sprite"]
        best, frames = None, []
        if sprite_tracks:
            best = max(sprite_tracks, key=lambda t: len(t["frames"]))
            for time, guid in best["frames"]:
                image = sprite_image.get(guid)
                if image:
                    frames.append([round(time, 5), image])
                else:
                    stats["unresolved"] += 1
        if frames:
            stats["with_sprites"] += 1
            stats["frames"] += len(frames)
        # Transform-only clips animate named child objects; pair each with the sprite
        # its prefab shows so the rig can be replayed instead of listed as text.
        layers: list[dict] = []
        node_pool: list[dict] = []
        mask_pool: list[dict] = []
        chosen: str | None = None
        # uGUI moves by float curves alone, so a clip with no Transform curve can still
        # be a rig.
        if not frames and (parsed["transforms"] or parsed["floats"]):
            # Several prefabs may share a clip. Since every sprite in the rig is
            # drawn, take the single prefab that best explains the animated paths
            # rather than merging them and pulling in art from a different object.
            clip_paths = set(parsed["transforms"]) | set(parsed["floats"])
            records: list[dict] = []
            animator_root: str | None = None
            best_score: tuple = ()
            for prefab_path in holders.get(clip["id"], [])[:8]:
                if prefab_path not in prefab_cache:
                    try:
                        source = (assets_root / prefab_path).read_text(
                            encoding="utf-8", errors="ignore")
                        prefab_cache[prefab_path] = (parse_prefab_rig(source),
                                                     prefab_animators(source))
                    except OSError:
                        prefab_cache[prefab_path] = ([], [])
                candidate, animators = prefab_cache[prefab_path]
                paths = {node["path"] for record in candidate
                         for node in record["chain"]}
                # The Animator that plays this clip, where the prefab holds one,
                # fixes the prefix outright. Matching the clip's path names is left
                # for prefabs whose Animator is not in this file.
                roots = [path for path, sources in animators
                         if clip["guid"] in sources
                         or any(clip["guid"] in plays.get(g, ()) for g in sources)]
                for root in roots or [None]:
                    score = ((1, sum(1 for c in clip_paths if joined(root, c) in paths))
                             if root is not None else (0, len(clip_paths & paths)))
                    if score > best_score:
                        records, animator_root, best_score, chosen = (
                            candidate, root, score, prefab_path)
            prefab_paths = {node["path"] for record in records
                            for node in record["chain"]}

            # Clip paths are relative to the animator root, which need not be the
            # prefab root, so a clip path is a *suffix* of the prefab path. Matching
            # on the bare leaf name instead let one clip path bind to several prefab
            # objects, which is how the same sprite ended up drawn twice. Recovering
            # the one shared prefix that explains the most paths keeps it exact.
            if animator_root is not None:
                prefix = animator_root
                if records:
                    stats["bound"] += 1
            else:
                candidates = {""}
                for clip_path in clip_paths:
                    if not clip_path:
                        continue
                    suffix = "/" + clip_path
                    for prefab_path in prefab_paths:
                        if prefab_path.endswith(suffix):
                            candidates.add(prefab_path[:-len(suffix)])
                prefix = max(candidates, key=lambda p: (
                    sum(1 for c in clip_paths if joined(p, c) in prefab_paths), -len(p)))
                if records:
                    stats["guessed"] += 1
            # Everything the clip drives lives under its animator root, so art
            # outside that subtree belongs to some other animator - a shelf's price
            # tag sitting at the prefab root would otherwise float over the doors.
            if prefix:
                records = [record for record in records
                           if record["path"] == prefix
                           or record["path"].startswith(prefix + "/")]

            curves_at: dict[str, dict] = {}
            for clip_path, curves in parsed["transforms"].items():
                curves_at.setdefault(joined(prefix, clip_path), curves)
            floats_at: dict[str, dict] = {}
            for clip_path, values in parsed["floats"].items():
                floats_at.setdefault(joined(prefix, clip_path), values)

            mask_pool: list[dict] = []
            mask_seen: dict[str, int] = {}
            node_seen: dict[str, int] = {}

            def node_id(node: dict) -> int:
                key = json.dumps(node, sort_keys=True, separators=(",", ":"))
                if key not in node_seen:
                    node_seen[key] = len(node_pool)
                    node_pool.append(node)
                return node_seen[key]

            # Every sprite in the rig is drawn, not only the animated ones: a
            # shopping cart's body never moves relative to the cart, so keying only
            # on animated paths rendered the wheels, eyes and cargo with no cart.
            def chain_ids(nodes: list[dict]) -> list[int]:
                ids = []
                for node in nodes:
                    curves = curves_at.get(node["path"]) or {}
                    # Only the fields that carry information are written: a null or
                    # a zero on every node costs more page weight than the whole
                    # animation payload of a small game.
                    entry = {"base": [round(value, 4) for value in node["base"]]}
                    if round(node["rot"], 3):
                        entry["rot0"] = round(node["rot"], 3)
                    for key, name in (("pos", "position"), ("rot", "euler"),
                                      ("scale", "scale")):
                        if curves.get(name):
                            entry[key] = curves[name]
                    moved = ui_track(node, floats_at.get(node["path"]))
                    if moved:
                        entry["pos"] = moved
                    ids.append(node_id(entry))
                return ids

            def mask_id(mask: dict) -> int | None:
                meta = sprite_meta.get(mask["guid"])
                if not meta:
                    return None
                entry = {"chain": chain_ids(mask["chain"]), "size": meta["size"]}
                if meta["anchor"] != [0.5, 0.5]:
                    entry["anch"] = meta["anchor"]
                key = json.dumps(entry, sort_keys=True, separators=(",", ":"))
                if key not in mask_seen:
                    mask_seen[key] = len(mask_pool)
                    mask_pool.append(entry)
                return mask_seen[key]

            for record in records[:MAX_RIG_LAYERS]:
                meta = sprite_meta.get(record["guid"])
                if not meta:
                    continue
                # Walk root-to-leaf so playback can compose parent transforms; an
                # ancestor with no curves still contributes its resting offset.
                chain = []
                toggles = []
                for node in record["chain"]:
                    curves = curves_at.get(node["path"]) or {}
                    # Siblings share their ancestors, so the same node would be
                    # written once per layer; pool them and store an index instead.
                    # Only the fields that carry information are written: a null or
                    # a zero on every node costs more page weight than the whole
                    # animation payload of a small game.
                    entry = {"base": [round(value, 4) for value in node["base"]]}
                    if round(node["rot"], 3):
                        entry["rot0"] = round(node["rot"], 3)
                    for key, name in (("pos", "position"), ("rot", "euler"),
                                      ("scale", "scale")):
                        if curves.get(name):
                            entry[key] = curves[name]
                    moved = ui_track(node, floats_at.get(node["path"]))
                    if moved:
                        entry["pos"] = moved
                    chain.append(node_id(entry))
                    # An ancestor being switched off hides everything beneath it.
                    toggle = (floats_at.get(node["path"]) or {}).get("active")
                    if toggle:
                        toggles.append(toggle)
                # A component switched off by the clip hides its own renderer.
                own = (floats_at.get(record["path"]) or {}).get("enabled")
                if own:
                    toggles.append(own)
                size, anchor = placed_size(record, meta)
                layer = {"name": record["path"], "img": meta["img"],
                         "size": [round(size[0], 2), round(size[1], 2)], "chain": chain}
                if is_sliced(record, meta):
                    left, bottom, right, top = meta["border"]
                    scale = border_scale(record, meta)
                    layer["bord"] = [
                        [round(v, 2) for v in (top, right, bottom, left)],
                        [round(v * scale, 2) for v in (top, right, bottom, left)]]
                if anchor != [0.5, 0.5]:
                    layer["anch"] = [round(anchor[0], 4), round(anchor[1], 4)]
                if record["flip"]:
                    layer["flip"] = record["flip"]
                if record["mask"]:
                    index = mask_id(record["mask"])
                    if index is not None:
                        layer["mk"] = index
                if record["tint"] < 0.999:
                    layer["tint"] = round(record["tint"], 3)
                if min(record["rgb"]) < 0.99:
                    layer["rgb"] = record["rgb"]
                alpha = (floats_at.get(record["path"]) or {}).get("alpha")
                if alpha:
                    layer["alpha"] = alpha
                if toggles:
                    layer["acts"] = toggles
                elif not record["on"]:
                    layer["off"] = True   # disabled in the prefab and never enabled
                layers.append(layer)
            # A prefab whose parts are *all* disabled is one the game fills in at
            # runtime; honouring the flag there leaves an empty box where the art
            # should be, so the default state is ignored rather than the clip lost.
            if layers and all(layer.get("off") for layer in layers):
                for layer in layers:
                    layer.pop("off")
            # A single keyframe is a pose, not motion. Clips where nothing actually
            # moves would show as a still image pretending to be an animation.
            # A fade or a switch is a change too: a dialog appearing moves nothing.
            moves = any(len(node.get(kind) or []) >= 2
                        for node in node_pool for kind in ("pos", "rot", "scale"))
            changes = any(len({key[1] for key in layer.get("alpha") or []}) >= 2
                          or layer.get("acts") for layer in layers)
            if not (moves or changes):
                layers = []
            if layers:
                stats["rigged"] += 1
                stats["layers"] += len(layers)

        clip_times = [t for t, _ in (best["frames"] if best else [])]
        for node in node_pool:
            for kind in ("pos", "rot", "scale"):
                clip_times += [key[0] for key in (node.get(kind) or [])]
        rows.append({
            "asset_id": clip["id"],
            "duration": max(clip_times, default=None),
            "sample_rate": parsed["sample_rate"],
            "frame_count": len(frames),
            "track_count": len(sprite_tracks),
            "track_path": best["path"] if best else None,
            "frames": json.dumps(frames) if frames else None,
            "curve_summary": ", ".join(parsed["curves"]) or None,
            "layers": json.dumps(layers, separators=(",", ":")) if layers else None,
            "nodes": json.dumps(node_pool, separators=(",", ":")) if layers else None,
            "masks": json.dumps(mask_pool, separators=(",", ":")) if mask_pool else None,
            "layer_count": len(layers),
            # The prefab this clip plays in, so the object view can offer the clip
            # beside the prefab it animates.
            "holder_id": prefab_id_of.get(chosen) if layers else None,
        })

    conn.executemany(
        """INSERT OR REPLACE INTO animations
           (asset_id, duration, sample_rate, frame_count, track_count, track_path,
            frames, curve_summary, layers, nodes, masks, layer_count, holder_id)
           VALUES (:asset_id, :duration, :sample_rate, :frame_count, :track_count,
                   :track_path, :frames, :curve_summary, :layers, :nodes, :masks,
                   :layer_count, :holder_id)""", rows)
    conn.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct sprite-swap animations.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    stats = build(args.export.resolve(), conn)
    print(f"clips={stats['clips']}  sprite_swap={stats['with_sprites']} "
          f"({stats['frames']} frames)  rigged={stats['rigged']} "
          f"({stats['layers']} layers)  unresolved={stats['unresolved']}  "
          f"bound to their Animator={stats['bound']}  guessed from names={stats['guessed']}")
    for row in conn.execute(
        """SELECT a.name, n.frame_count, n.duration FROM animations n
             JOIN assets a ON a.id = n.asset_id
            WHERE n.frame_count > 0 ORDER BY n.frame_count DESC LIMIT 5"""):
        print(f"  {row['name'][:44]:<46} {row['frame_count']} frames, {row['duration']}s")
    conn.close()


if __name__ == "__main__":
    main()
