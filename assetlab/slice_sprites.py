"""Stage 3 - cut individual sprites out of their atlas textures.

The export stores 2683 sprites as YAML metadata pointing into 728 atlas PNGs, so
a raw browse only ever shows atlas sheets. This turns each sprite into a real
image: the single biggest usability win in the pipeline.

Unity sprite rects are bottom-left origin while Pillow is top-left, so the crop
box is (x, H - y - h, x + w, H - y). Every sprite in this export has packing
rotation 0 (settingsRaw bits 3-6 clear), so an axis-aligned crop is correct.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from PIL import Image

from .core import connect, dhash, image_stats, load_rgba

NAME_RE = re.compile(r"^  m_Name:\s*(.*?)\s*$", re.MULTILINE)
TEXTURE_RE = re.compile(r"texture:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-f]{32})")
# Unity may insert a `serializedVersion:` line between the key and its fields, and
# writes small values in scientific notation, so the exponent sign must be matched.
_NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_RECT_FIELDS = (
    r"(?:\s*\n\s+serializedVersion:\s*\d+)?"
    rf"\s*\n\s+x:\s*({_NUM})\s*\n\s+y:\s*({_NUM})"
    rf"\s*\n\s+width:\s*({_NUM})\s*\n\s+height:\s*({_NUM})"
)
RECT_RE = re.compile(r"textureRect:" + _RECT_FIELDS)
FALLBACK_RECT_RE = re.compile(r"^  m_Rect:" + _RECT_FIELDS, re.MULTILINE)
SETTINGS_RE = re.compile(r"settingsRaw:\s*(\d+)")
# Placement data. A renderer puts the sprite's pivot on the transform origin and
# scales it by 1/m_PixelsToUnits, so a preview that centres every image at 100 px
# per unit draws off-pivot art in the wrong place and the whole rig at the wrong
# size - these games use 140, and 18% of sprites are not centre-pivoted.
PIVOT_RE = re.compile(rf"^  m_Pivot:\s*\{{x:\s*({_NUM}),\s*y:\s*({_NUM})\}}", re.MULTILINE)
PPU_RE = re.compile(rf"^  m_PixelsToUnits:\s*({_NUM})", re.MULTILINE)
RECT_OFFSET_RE = re.compile(
    rf"textureRectOffset:\s*\{{x:\s*({_NUM}),\s*y:\s*({_NUM})\}}")
# Nine-slice margins, so a renderer set to Sliced can be stretched at its own size
# without smearing the corners.
BORDER_RE = re.compile(
    rf"^  m_Border:\s*\{{x:\s*({_NUM}),\s*y:\s*({_NUM}),"
    rf"\s*z:\s*({_NUM}),\s*w:\s*({_NUM})\}}", re.MULTILINE)

# Unity packs sprite settings into one int:
#   bit 0    packed
#   bit 1    packing mode
#   bits 2-5 packing rotation
#   bit 6    mesh type
ROTATION_NONE, ROTATION_FLIP_H, ROTATION_FLIP_V, ROTATION_180, ROTATION_90 = range(5)


def packing_rotation(settings_raw: int) -> int:
    return (settings_raw >> 2) & 0xF


def unrotate(image: Image.Image, rotation: int) -> Image.Image:
    """Undo the transform Unity applied when packing the sprite into the atlas."""
    if rotation == ROTATION_FLIP_H:
        return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if rotation == ROTATION_FLIP_V:
        return image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if rotation == ROTATION_180:
        return image.transpose(Image.Transpose.ROTATE_180)
    if rotation == ROTATION_90:
        return image.transpose(Image.Transpose.ROTATE_270)
    return image


def anchor(rect, tex_rect, offset, pivot):
    """Pivot as a fraction of the sliced image, measured from its top-left.

    The pivot is expressed against `m_Rect`, but the image we cut is `textureRect`,
    which tight packing may have shrunk by `textureRectOffset`. Unity's y grows
    upward and CSS's downward, so the vertical fraction is flipped here.
    """
    if not rect or not tex_rect or tex_rect[2] <= 0 or tex_rect[3] <= 0:
        return 0.5, 0.5
    x = (pivot[0] * rect[2] - offset[0]) / tex_rect[2]
    y = (pivot[1] * rect[3] - offset[1]) / tex_rect[3]
    return round(x, 5), round(1.0 - y, 5)


def parse_sprite(text: str):
    """Return (name, atlas guid, rect, rotation, ppu, anchor) from the m_RD block."""
    name_match = NAME_RE.search(text)
    name = name_match.group(1) if name_match else None

    # m_AtlasRD always follows m_RD, and its texture reference is empty, so the
    # slice before it isolates the render data that actually points at an atlas.
    head = text.split("\n  m_AtlasRD:", 1)[0]
    start = head.find("\n  m_RD:")
    block = head[start:] if start >= 0 else head

    texture_match = TEXTURE_RE.search(block)
    rect_match = RECT_RE.search(block) or FALLBACK_RECT_RE.search(text)
    rect = None
    if rect_match:
        x, y, w, h = (int(round(float(value))) for value in rect_match.groups())
        rect = (x, y, w, h)
    settings_match = SETTINGS_RE.search(block)
    rotation = packing_rotation(int(settings_match.group(1))) if settings_match else 0

    ppu_match = PPU_RE.search(text)
    ppu = float(ppu_match.group(1)) if ppu_match else 100.0
    pivot_match = PIVOT_RE.search(text)
    pivot = ((float(pivot_match.group(1)), float(pivot_match.group(2)))
             if pivot_match else (0.5, 0.5))
    base_match = FALLBACK_RECT_RE.search(text)
    base_rect = tuple(float(v) for v in base_match.groups()) if base_match else rect
    offset_match = RECT_OFFSET_RE.search(block)
    offset = ((float(offset_match.group(1)), float(offset_match.group(2)))
              if offset_match else (0.0, 0.0))
    border_match = BORDER_RE.search(text)
    border = ([round(float(v), 2) for v in border_match.groups()]
              if border_match else [0.0, 0.0, 0.0, 0.0])
    # A 90-degree packed sprite is un-rotated after cropping, so the anchor is
    # measured against the upright rect either way.
    return (name, (texture_match.group(1) if texture_match else None), rect, rotation,
            ppu or 100.0, anchor(base_rect, rect, offset, pivot), border)


def slice_all(assets_root: Path, out_dir: Path, conn: sqlite3.Connection) -> dict[str, int]:
    sprite_dir = out_dir / "sprites"
    sprite_dir.mkdir(parents=True, exist_ok=True)

    atlas_path: dict[str, str] = {
        row["guid"]: row["rel_path"]
        for row in conn.execute(
            "SELECT guid, rel_path FROM assets WHERE unity_type='Texture2D' AND guid IS NOT NULL"
        )
    }

    rows = conn.execute(
        "SELECT id, rel_path FROM assets WHERE unity_type='Sprite' AND ext='asset'"
    ).fetchall()

    by_atlas: dict[str, list[tuple]] = defaultdict(list)
    stats = {"parsed": 0, "no_rect": 0, "no_atlas": 0, "sliced": 0, "out_of_bounds": 0,
             "rotated": 0, "off_centre_pivot": 0, "reused": 0}

    for row in rows:
        try:
            text = (assets_root / row["rel_path"]).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        name, guid, rect, rotation, ppu, pivot, border = parse_sprite(text)
        if rect is None:
            stats["no_rect"] += 1
            continue
        if not guid or guid not in atlas_path:
            stats["no_atlas"] += 1
            continue
        stats["parsed"] += 1
        if rotation:
            stats["rotated"] += 1
        if abs(pivot[0] - 0.5) > 0.01 or abs(pivot[1] - 0.5) > 0.01:
            stats["off_centre_pivot"] += 1
        by_atlas[guid].append((row["id"], name or f"sprite_{row['id']}", rect, rotation,
                               ppu, pivot, border))

    # Cutting an atlas is the slow part of the whole pipeline, and the crop only
    # depends on the rect. When every sprite of an atlas is already on disk with its
    # measurements recorded, the placement data is refreshed without opening the PNG.
    cached = {
        row["id"]: row["image_path"]
        for row in conn.execute(
            """SELECT id, image_path FROM assets WHERE unity_type='Sprite'
                 AND image_path IS NOT NULL AND dhash IS NOT NULL AND width IS NOT NULL""")
    }

    updates, sprite_rows = [], []
    for number, (guid, entries) in enumerate(sorted(by_atlas.items()), start=1):
        if all(entry[0] in cached
               and (out_dir / cached[entry[0]]).is_file() for entry in entries):
            for asset_id, _name, (x, y, w, h), _rotation, ppu, pivot, border in entries:
                sprite_rows.append({
                    "asset_id": asset_id, "atlas_guid": guid,
                    "x": x, "y": y, "w": w, "h": h,
                    "rotated": 1 if rotation == ROTATION_90 else 0,
                    "sliced_path": cached[asset_id],
                    "ppu": ppu, "anchor_x": pivot[0], "anchor_y": pivot[1],
                    "border": json.dumps(border) if any(border) else None,
                })
                stats["sliced"] += 1
                stats["reused"] += 1
            continue
        try:
            atlas = load_rgba(assets_root / atlas_path[guid])
        except (OSError, ValueError):
            stats["no_atlas"] += len(entries)
            continue
        width, height = atlas.size
        for asset_id, name, (x, y, w, h), rotation, ppu, pivot, border in entries:
            # A 90-degree packed sprite occupies a swapped footprint in the atlas.
            box_w, box_h = (h, w) if rotation == ROTATION_90 else (w, h)
            left, upper, right, lower = x, height - y - box_h, x + box_w, height - y
            if box_w <= 0 or box_h <= 0 or left < 0 or upper < 0 or right > width or lower > height:
                left, upper = max(0, left), max(0, upper)
                right, lower = min(width, right), min(height, lower)
                if right <= left or lower <= upper:
                    stats["out_of_bounds"] += 1
                    continue
                stats["out_of_bounds"] += 1
            crop = unrotate(atlas.crop((left, upper, right, lower)), rotation)
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]
            target = sprite_dir / f"{safe}__{asset_id}.png"
            crop.save(target, "PNG", optimize=True)
            info = image_stats(crop)
            updates.append({
                "id": asset_id,
                "image_path": f"sprites/{target.name}",
                "dhash": dhash(crop),
                **info,
            })
            sprite_rows.append({
                "asset_id": asset_id, "atlas_guid": guid,
                "x": x, "y": y, "w": w, "h": h,
                "rotated": 1 if rotation == ROTATION_90 else 0,
                "sliced_path": f"sprites/{target.name}",
                "ppu": ppu, "anchor_x": pivot[0], "anchor_y": pivot[1],
                "border": json.dumps(border) if any(border) else None,
            })
            stats["sliced"] += 1
        atlas.close()
        if number % 50 == 0:
            print(f"  {number}/{len(by_atlas)} atlases, {stats['sliced']} sprites", flush=True)

    conn.executemany(
        """UPDATE assets SET image_path=:image_path, dhash=:dhash, width=:width,
               height=:height, has_alpha=:has_alpha, alpha_ratio=:alpha_ratio,
               is_grayscale=:is_grayscale, dominant_hex=:dominant_hex
             WHERE id=:id""",
        updates,
    )
    conn.executemany(
        """INSERT OR REPLACE INTO sprites
             (asset_id, atlas_guid, x, y, w, h, rotated, sliced_path, ppu,
              anchor_x, anchor_y, border)
           VALUES (:asset_id, :atlas_guid, :x, :y, :w, :h, :rotated, :sliced_path,
                   :ppu, :anchor_x, :anchor_y, :border)""",
        sprite_rows,
    )
    conn.commit()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Slice atlas sprites into individual images.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    stats = slice_all(args.export.resolve(), args.out.resolve(), conn)
    print("sprite slicing:", ", ".join(f"{key}={value}" for key, value in stats.items()))
    conn.close()


if __name__ == "__main__":
    main()
