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
                else "active" if name == "m_IsActive" else None)
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

    def walk(file_id: str, segments: list[str], chain: list[dict],
             group: tuple[int, int] | None, visible: bool, seen: frozenset,
             mask: dict | None, ids: list[str]) -> None:
        nonlocal counter
        record = transforms[file_id]
        gameobject = record["go"]
        chain = chain + [{"path": "/".join(segments),
                          "base": [record["x"], record["y"], record["sx"], record["sy"]],
                          "rot": record["rot"]}]
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
        for child in record["children"]:
            # A Canvas subtree lays out in screen space against a rect, not in world
            # units, so composing it into a world rig would scatter its parts.
            if child in transforms and child not in seen and child not in ui_transforms:
                walk(child, segments + [names.get(transforms[child]["go"], "")],
                     chain, group, visible, seen | {child}, mask, ids)

    for root in roots:
        if root not in ui_transforms:
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


def build(assets_root: Path, conn: sqlite3.Connection) -> dict[str, int]:
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
    clips = conn.execute(
        "SELECT id, guid, rel_path FROM assets WHERE unity_type='AnimationClip' AND ext='anim'"
    ).fetchall()

    # Which prefabs use each clip, so the animated object names can be resolved to
    # the sprites the prefab actually puts on them.
    holders: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute(
        """SELECT u.asset_id, h.rel_path FROM used_by u
             JOIN assets h ON h.guid = u.holder_guid
            WHERE h.unity_type='Prefab'"""):
        holders[row["asset_id"]].append(row["rel_path"])
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
        if not frames and parsed["transforms"]:
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
                        records, animator_root, best_score = candidate, root, score
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
                    chain.append(node_id(entry))
                    # An ancestor being switched off hides everything beneath it.
                    toggle = (floats_at.get(node["path"]) or {}).get("active")
                    if toggle:
                        toggles.append(toggle)
                size = meta["size"]
                if record["draw"]:
                    size = [round(record["draw"][0] * RENDER_PPU, 2),
                            round(record["draw"][1] * RENDER_PPU, 2)]
                layer = {"name": record["path"], "img": meta["img"],
                         "size": size, "chain": chain}
                if record["draw"] and meta["border"]:
                    left, bottom, right, top = meta["border"]
                    layer["bord"] = [
                        [round(v, 2) for v in (top, right, bottom, left)],
                        [round(v * meta["border_scale"], 2)
                         for v in (top, right, bottom, left)]]
                if meta["anchor"] != [0.5, 0.5]:
                    layer["anch"] = meta["anchor"]
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
            if not any(len(node.get(kind) or []) >= 2
                       for node in node_pool for kind in ("pos", "rot", "scale")):
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
        })

    conn.executemany(
        """INSERT OR REPLACE INTO animations
           (asset_id, duration, sample_rate, frame_count, track_count, track_path,
            frames, curve_summary, layers, nodes, masks, layer_count)
           VALUES (:asset_id, :duration, :sample_rate, :frame_count, :track_count,
                   :track_path, :frames, :curve_summary, :layers, :nodes, :masks,
                   :layer_count)""", rows)
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
