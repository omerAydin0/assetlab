"""Each prefab drawn as the build places it: the assembled form of an object.

An object in a Unity build is a prefab. It names the sprites it is made of and puts
each one somewhere, and that is the whole of what "assembled" means - no reading of
names and no animation is needed to know it. The objects view was first built the
other way round, grouping sprites by name and borrowing a clip to show them together,
and across five builds 17,492 of its 18,284 objects were a single sprite while 1,809
prefabs each assemble two or more.

This draws every prefab whose renderers show at least two sprites, in its authored
pose, with the geometry the page's rig engine uses: the transform chain from the root,
each sprite's pivot and pixels-per-unit, flips, tint, draw order, nine-slicing, a
SpriteMask's window, and parts the prefab ships switched off left off. The picture is
the object's face on the page, and its sprites are its pieces. Prefabs that draw the
same picture - a variant that only renames - are recorded once.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from functools import lru_cache
from pathlib import Path

from PIL import Image

from .animations import (MAX_RIG_LAYERS, RENDER_PPU, border_scale, is_sliced,
                         load_sprite_meta, parse_prefab_rig, placed_size)
from .core import connect

#: One sprite on show is already on the page as itself; an object has at least two.
MIN_PARTS = 2
#: Longest side of a pose in pixels, and how far a small prefab may be enlarged.
POSE_MAX = 512
POSE_UPSCALE = 4.0
#: A part is never resampled larger than this, whatever a mask lets it spill to.
PART_MAX = 4096
PAD = 6
#: Boxes closer than this share of the prefab's span still count as touching.
TOUCH = 0.02
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _matrix(node: dict) -> tuple:
    """One transform as the page's rig engine reads it; Unity's y up becomes down."""
    x, y, sx, sy = node["base"]
    rad = -math.radians(node.get("rot") or 0.0)
    cos, sin = math.cos(rad), math.sin(rad)
    return (cos * sx, sin * sx, -sin * sy, cos * sy, x * RENDER_PPU, -y * RENDER_PPU)


def _mul(m: tuple, n: tuple) -> tuple:
    return (m[0] * n[0] + m[2] * n[1], m[1] * n[0] + m[3] * n[1],
            m[0] * n[2] + m[2] * n[3], m[1] * n[2] + m[3] * n[3],
            m[0] * n[4] + m[2] * n[5] + m[4], m[1] * n[4] + m[3] * n[5] + m[5])


def _chain(nodes: list[dict]) -> tuple:
    m = IDENTITY
    for node in nodes:
        m = _mul(m, _matrix(node))
    return m


def _local(size: list[float], anchor: list[float], flip: int) -> tuple:
    """The sprite's unit square (top-left origin) onto its transform: the pivot lands
    on the transform, and a flip mirrors about the pivot."""
    w, h = size
    fx = -1.0 if flip & 1 else 1.0
    fy = -1.0 if flip & 2 else 1.0
    return (fx * w, 0.0, 0.0, fy * h, -fx * anchor[0] * w, -fy * anchor[1] * h)


def _box(m: tuple) -> tuple:
    """Axis-aligned box of the unit square under m."""
    xs = [m[0] * u + m[2] * v + m[4] for u, v in ((0, 0), (1, 0), (0, 1), (1, 1))]
    ys = [m[1] * u + m[3] * v + m[5] for u, v in ((0, 0), (1, 0), (0, 1), (1, 1))]
    return min(xs), min(ys), max(xs), max(ys)


def _clipped(layer: dict) -> tuple:
    """The layer's box on the canvas, cut to its SpriteMask's window if it has one."""
    box = _box(layer["m"])
    if layer["window"]:
        w = layer["window"]
        box = (max(box[0], w[0]), max(box[1], w[1]), min(box[2], w[2]), min(box[3], w[3]))
    return box


def figures(layers: list[dict]) -> tuple[int, float]:
    """How many separate figures a prefab draws, and the largest one's share of its parts.

    An object's parts overlap - that is what assembling them means. A map page or a
    backdrop puts separate things apart on one canvas, and ranked among the objects by
    part count it led the tab: one build's first nine cards were map pages, each a dark
    field with a few specks on it. Parts are joined when their boxes overlap or all but
    touch; nothing here reads a name.
    """
    boxes = [box for box in map(_clipped, layers) if box[2] > box[0] and box[3] > box[1]]
    if not boxes:
        return 0, 0.0
    span = max(max(b[2] for b in boxes) - min(b[0] for b in boxes),
               max(b[3] for b in boxes) - min(b[1] for b in boxes))
    gap = span * TOUCH
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(boxes):
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            if (a[0] - gap <= b[2] and b[0] - gap <= a[2]
                    and a[1] - gap <= b[3] and b[1] - gap <= a[3]):
                parent[find(i)] = find(j)
    sizes = Counter(find(i) for i in range(len(boxes)))
    return len(sizes), max(sizes.values()) / len(boxes)


def pose_layers(records: list[dict], sprite_meta: dict) -> list[dict]:
    """What a prefab draws when placed, back to front."""
    layers = []
    for record in records[:MAX_RIG_LAYERS]:
        meta = sprite_meta.get(record["guid"])
        if not meta or not meta["size"][0] or not meta["size"][1]:
            continue
        size, anchor = placed_size(record, meta)
        window = None
        if record["mask"]:
            mask = sprite_meta.get(record["mask"]["guid"])
            if mask:
                window = _box(_mul(_chain(record["mask"]["chain"]),
                                   _local(mask["size"], mask["anchor"], 0)))
        layers.append({
            "guid": record["guid"], "img": meta["img"], "size": size,
            "m": _mul(_chain(record["chain"]), _local(size, anchor, record["flip"])),
            "sliced": is_sliced(record, meta),
            "border": meta["border"], "border_scale": border_scale(record, meta),
            "rgb": record["rgb"], "tint": record["tint"], "on": record["on"],
            "window": window})
    # A prefab whose every part is switched off is one the game fills in at run time;
    # honouring the flag would draw nothing, so it is drawn as authored - the reading
    # the animation view takes of the same prefabs.
    if layers and not any(layer["on"] for layer in layers):
        return layers
    return [layer for layer in layers if layer["on"]]


@lru_cache(maxsize=256)
def _source(path: str) -> Image.Image | None:
    try:
        with Image.open(path) as image:
            return image.convert("RGBA")
    except (OSError, ValueError):
        return None


def _nine_slice(image: Image.Image, border: list[float], out_w: int, out_h: int,
                scale: float) -> Image.Image:
    """Corners kept at their own size (times scale); edges and middle stretched."""
    left, bottom, right, top = border
    width, height = image.size
    sx = [0, round(left), round(width - right), width]
    sy = [0, round(top), round(height - bottom), height]
    ox = [0, round(left * scale), out_w - round(right * scale), out_w]
    oy = [0, round(top * scale), out_h - round(bottom * scale), out_h]
    if ox[1] > ox[2] or oy[1] > oy[2] or sx[1] > sx[2] or sy[1] > sy[2]:
        return image.resize((out_w, out_h), Image.LANCZOS)
    out = Image.new("RGBA", (out_w, out_h))
    for i in range(3):
        for j in range(3):
            w, h = ox[i + 1] - ox[i], oy[j + 1] - oy[j]
            if sx[i + 1] <= sx[i] or sy[j + 1] <= sy[j] or w <= 0 or h <= 0:
                continue
            piece = image.crop((sx[i], sy[j], sx[i + 1], sy[j + 1]))
            out.paste(piece.resize((w, h), Image.LANCZOS), (ox[i], oy[j]))
    return out


def _tinted(image: Image.Image, rgb: list[float], alpha: float) -> Image.Image:
    """A renderer multiplies its sprite by m_Color."""
    if min(rgb) >= 0.999 and alpha >= 0.999:
        return image
    channels = image.split()
    factors = list(rgb) + [alpha]
    return Image.merge("RGBA", [channel.point(lambda v, k=k: round(v * k))
                                for channel, k in zip(channels, factors)])


def render(layers: list[dict], art_root: Path, assets_root: Path,
           target: Path) -> tuple[tuple[int, int], str] | None:
    """Draw the layers into target. -> ((width, height), digest), or None."""
    boxes = [_clipped(layer) for layer in layers]
    live = [box for box in boxes if box[2] > box[0] and box[3] > box[1]]
    if not live:
        return None
    x0, y0 = min(b[0] for b in live), min(b[1] for b in live)
    x1, y1 = max(b[2] for b in live), max(b[3] for b in live)
    span = max(x1 - x0, y1 - y0)
    if span <= 0:
        return None
    scale = min(POSE_MAX / span, POSE_UPSCALE)
    width = math.ceil((x1 - x0) * scale) + 2 * PAD
    height = math.ceil((y1 - y0) * scale) + 2 * PAD
    canvas = Image.new("RGBA", (width, height))
    view = (scale, 0.0, 0.0, scale, PAD - x0 * scale, PAD - y0 * scale)

    for layer, box in zip(layers, boxes):
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        path = (art_root / layer["img"] if layer["img"].startswith("sprites/")
                else assets_root / layer["img"])
        image = _source(str(path))
        if image is None:
            continue
        m = _mul(view, layer["m"])                      # unit square -> canvas
        if abs(m[0] * m[3] - m[1] * m[2]) < 1e-9:
            continue                                    # scaled to nothing
        # Resized near the size it is drawn at first: an affine resample that shrinks
        # a sprite eight times over aliases, and a Lanczos resize does not.
        out_w = min(PART_MAX, max(1, round(math.hypot(m[0], m[1]))))
        out_h = min(PART_MAX, max(1, round(math.hypot(m[2], m[3]))))
        if layer["sliced"]:
            per_render_px = out_w / max(layer["size"][0], 1e-6)
            source = _nine_slice(image, layer["border"], out_w, out_h,
                                 layer["border_scale"] * per_render_px)
        elif (out_w, out_h) != image.size:
            source = image.resize((out_w, out_h), Image.LANCZOS)
        else:
            source = image
        source = _tinted(source, layer["rgb"], layer["tint"])
        sw, sh = source.size
        a = (m[0] / sw, m[1] / sw, m[2] / sh, m[3] / sh, m[4], m[5])   # source px -> canvas
        det = a[0] * a[3] - a[1] * a[2]
        i11, i12, i21, i22 = a[3] / det, -a[2] / det, -a[1] / det, a[0] / det
        rx = max(0, math.floor(box[0] * scale + view[4]))
        ry = max(0, math.floor(box[1] * scale + view[5]))
        rw = min(width, math.ceil(box[2] * scale + view[4])) - rx
        rh = min(height, math.ceil(box[3] * scale + view[5])) - ry
        if rw <= 0 or rh <= 0:
            continue
        piece = source.transform(
            (rw, rh), Image.AFFINE,
            (i11, i12, i11 * (rx - a[4]) + i12 * (ry - a[5]),
             i21, i22, i21 * (rx - a[4]) + i22 * (ry - a[5])),
            resample=Image.BICUBIC)
        canvas.alpha_composite(piece, (rx, ry))

    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target)
    return (width, height), hashlib.sha1(canvas.tobytes()).hexdigest()


def build(assets_root: Path, out_dir: Path, conn: sqlite3.Connection) -> dict[str, int]:
    _, sprite_meta = load_sprite_meta(conn)
    sprite_id = {row[0]: row[1] for row in conn.execute(
        "SELECT guid, id FROM assets WHERE unity_type='Sprite' AND guid IS NOT NULL")}
    conn.execute("DELETE FROM prefab_poses")
    stats = {"prefabs": 0, "drawn": 0, "same": 0, "one_sprite": 0, "nothing": 0}
    seen: dict[str, int] = {}
    rows = []
    for prefab_id, rel_path in conn.execute(
            "SELECT id, rel_path FROM assets WHERE unity_type='Prefab' "
            "ORDER BY rel_path").fetchall():
        stats["prefabs"] += 1
        try:
            text = (assets_root / rel_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        layers = pose_layers(parse_prefab_rig(text), sprite_meta)
        parts = list(dict.fromkeys(layer["guid"] for layer in layers))
        if len(parts) < MIN_PARTS:
            stats["one_sprite" if parts else "nothing"] += 1
            continue
        target = out_dir / "poses" / f"{prefab_id}.png"
        drawn = render(layers, out_dir, assets_root, target)
        if drawn is None:
            stats["nothing"] += 1
            continue
        (width, height), digest = drawn
        count, main = figures(layers)
        first = seen.setdefault(digest, prefab_id)
        if first != prefab_id:
            target.unlink(missing_ok=True)
            stats["same"] += 1
        else:
            stats["drawn"] += 1
        rows.append((prefab_id, json.dumps([sprite_id[g] for g in parts if g in sprite_id]),
                     len(layers), width, height, f"poses/{first}.png",
                     None if first == prefab_id else first, count, round(main, 3)))
    conn.executemany(
        "INSERT INTO prefab_poses (prefab_id, sprites, layer_count, width, height, pose, "
        "same_as, figures, main) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    _source.cache_clear()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Draw each prefab as it assembles.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    conn = connect(args.out / "assetlab.db")
    print(build(args.export.resolve(), args.out.resolve(), conn))
    conn.close()


if __name__ == "__main__":
    main()
