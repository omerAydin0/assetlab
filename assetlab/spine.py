"""Cut the art a build packs for Spine, which Unity's own sprite records never see.

A skeletal-animation build ships its art twice over: as a packed page, and as a plain
text descriptor beside it naming every region on that page and where it sits. Unity
has no Sprite asset for any of them - the Spine runtime cuts them itself - so the
sprite stage walks straight past several thousand pieces of a build's best art. One
catalogue held 19,037 Sprite assets and could place 1,446 of them; the same export
carried 847 named regions in descriptors nobody read.

Two dialects are in the wild and both appear across these builds:

    adam.png                       Super_Thunder.png
    size: 256,64                   size:1906,586
    agiz01                         01_Alt
      rotate: false                bounds:702,297,142,63
      xy: 147, 2                   rotate:90
      size: 25, 16

The older one spells the rect as `xy` plus `size` and rotation as a flag; the newer
packs it into `bounds` and gives rotation in degrees. A page line is the one that
names an image file, which is what separates it from a region above the same keys.

Regions become sprites in the catalogue like any other, with the page recorded as
their atlas, so they get thumbnails, group into objects by name, and show their own
cuts on the sheet. The one conversion that matters is the origin: an atlas descriptor
measures down from the top-left and Unity measures up from the bottom-left, and the
rest of the pipeline speaks Unity.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from PIL import Image

from .core import dhash, image_stats, load_rgba

#: What a packed page may be stored as.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tga"}
#: A page line names an image; a region line names a region.
PAGE_RE = re.compile(r"\.(png|jpg|jpeg|webp|tga)$", re.I)
#: Every atlas key is `name:value`, with or without the space the old dialect uses.
FIELD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
#: What a region may be called on disk once its name has been made safe.
SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _numbers(value: str) -> list[int]:
    return [int(round(float(part))) for part in value.replace(" ", "").split(",")
            if part not in ("", "-")]


def parse_atlas(text: str) -> list[dict]:
    """Every region in one descriptor. -> [{page, page_w, page_h, name, x, y, w, h, turn}]

    ``turn`` is how far the region was turned when it was packed, in degrees, so a
    reader knows both the footprint it occupies on the page and how to set it back.
    """
    regions: list[dict] = []
    page: dict | None = None
    pending: str | None = None
    fields: dict[str, str] = {}

    def close() -> None:
        nonlocal pending, fields
        if pending is None or page is None:
            pending, fields = None, {}
            return
        if "bounds" in fields:
            box = _numbers(fields["bounds"])
        elif "xy" in fields and "size" in fields:
            box = _numbers(fields["xy"]) + _numbers(fields["size"])
        else:
            pending, fields = None, {}
            return
        if len(box) < 4:
            pending, fields = None, {}
            return
        spin = fields.get("rotate", "false").strip().lower()
        turn = 90 if spin == "true" else 0 if spin in ("false", "") else int(spin or 0)
        region = {"page": page["page"], "page_w": page["w"], "page_h": page["h"],
                  "name": pending, "x": box[0], "y": box[1],
                  "w": box[2], "h": box[3], "turn": turn % 360}
        # Where the packer stripped transparent margins: the region's original size and
        # where the kept part sat in it. A skeleton places the original, not the crop.
        offsets = _numbers(fields["offsets"]) if "offsets" in fields else []
        if len(offsets) >= 4:
            region["offset"], region["orig"] = tuple(offsets[:2]), tuple(offsets[2:4])
        elif "orig" in fields and "offset" in fields:
            region["orig"] = tuple(_numbers(fields["orig"])[:2])
            region["offset"] = tuple(_numbers(fields["offset"])[:2])
        regions.append(region)
        pending, fields = None, {}

    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        field = FIELD_RE.match(line)
        if field and pending is not None:
            fields[field.group(1)] = field.group(2)
            continue
        if field and pending is None:
            continue                      # a page's own keys, already taken below
        # A name line. It opens a page when it names an image and a size follows.
        if PAGE_RE.search(stripped):
            size = next((FIELD_RE.match(later) for later in lines[index + 1:index + 6]
                         if FIELD_RE.match(later)
                         and FIELD_RE.match(later).group(1) == "size"), None)
            if size:
                close()
                measures = _numbers(size.group(2))
                page = {"page": stripped, "w": measures[0], "h": measures[1]}
                continue
        close()
        pending = stripped
    close()
    return regions


def footprint(region: dict) -> tuple[int, int]:
    """The box a region occupies on the page, transposed if it was packed turned."""
    turned = region["turn"] in (90, 270)
    return (region["h"] if turned else region["w"],
            region["w"] if turned else region["h"])


def on_page(region: dict, size: tuple[int, int]) -> bool:
    """Whether the region is inside the page it claims to sit on."""
    wide, tall = footprint(region)
    return (region["x"] >= 0 and region["y"] >= 0
            and region["x"] + wide <= size[0] and region["y"] + tall <= size[1])


def image_index(assets_root: Path) -> dict[str, list[Path]]:
    """Every image in the export, by filename.

    Built once. Resolving each page with its own `rglob` walked a 40,000-file export
    sixty-seven times for one build.
    """
    found: dict[str, list[Path]] = {}
    for path in assets_root.rglob("*"):
        if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
            found.setdefault(path.name, []).append(path)
    return found


def page_image(descriptor: Path, page: str, index: dict[str, list[Path]],
               declared: tuple[int, int] | None = None) -> Path | None:
    """The packed page a descriptor names, wherever the export happened to put it.

    A page is named by bare filename, and a build is free to call one `01.png`. Taking
    the first match anywhere in the tree cut 158-pixel regions out of an unrelated
    256x256 image; the descriptor states the size it was written against, so that is
    what picks between candidates. A page half the declared size is still the page -
    the export downscaled it - but one at a different shape is a different picture.
    """
    candidates = list(index.get(page, ()))
    if not candidates:
        return None
    # The one beside the descriptor is the likeliest, and with no size to check
    # against it is the only reason to prefer any of them.
    beside = descriptor.parent / page
    candidates.sort(key=lambda path: path != beside)
    if declared is None:
        return candidates[0]
    fallback = None
    for candidate in candidates:
        try:
            with Image.open(candidate) as image:
                size = image.size
        except (OSError, ValueError):
            continue
        if size == declared:
            return candidate
        across = size[0] / max(1, declared[0])
        down = size[1] / max(1, declared[1])
        if abs(across - down) < 0.01 and fallback is None:
            fallback = candidate
    return fallback


def fit_to_page(regions: list[dict], size: tuple[int, int]) -> list[dict]:
    """Rescale a descriptor's rects onto the page as it was actually exported.

    A descriptor states the size it was written against. A build that ships the page
    at half that - one export declares 512x128 and stores 256x64 - has every rect at
    twice the coordinates the image can hold, and the twenty regions that produced
    were the only ones this stage could not cut. The image is the authority on its own
    size, so the rects are scaled to it.
    """
    if not regions:
        return regions
    wide, tall = size
    across = wide / max(1, regions[0]["page_w"])
    down = tall / max(1, regions[0]["page_h"])
    if abs(across - 1) < 0.01 and abs(down - 1) < 0.01:
        return regions
    # Only a uniform rescale is a rescale; anything else is a mismatched page, and
    # stretching a rect onto it would invent a layout.
    if abs(across - down) > 0.01:
        return regions
    scaled = []
    for region in regions:
        scaled.append({**region, "page_w": wide, "page_h": tall,
                       "x": round(region["x"] * across), "y": round(region["y"] * down),
                       "w": max(1, round(region["w"] * across)),
                       "h": max(1, round(region["h"] * down)),
                       **({"orig": (region["orig"][0] * across, region["orig"][1] * down),
                           "offset": (region["offset"][0] * across,
                                      region["offset"][1] * down)}
                          if "orig" in region else {})})
    return scaled


def cut(image: Image.Image, region: dict) -> Image.Image:
    """One region, turned back to the way it is drawn."""
    turned = region["turn"] in (90, 270)
    wide = region["h"] if turned else region["w"]
    tall = region["w"] if turned else region["h"]
    box = (region["x"], region["y"], region["x"] + wide, region["y"] + tall)
    crop = image.crop(box)
    if region["turn"] == 90:
        crop = crop.transpose(Image.Transpose.ROTATE_270)
    elif region["turn"] == 270:
        crop = crop.transpose(Image.Transpose.ROTATE_90)
    elif region["turn"] == 180:
        crop = crop.transpose(Image.Transpose.ROTATE_180)
    return crop


def read_text(path: Path) -> str:
    """Descriptors are UTF-8 where the artist typed ASCII and guesswork where not."""
    raw = path.read_bytes()
    for encoding in ("utf-8", "cp1254", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def record(conn: sqlite3.Connection, descriptor: Path, assets_root: Path,
           region: dict, target: Path, guid: str | None) -> None:
    """Put one cut region into the catalogue as a sprite like any other."""
    rel = f"{descriptor.relative_to(assets_root).as_posix()}#{region['name']}"
    with load_rgba(target) as piece:
        info = image_stats(piece)
        fingerprint = dhash(piece)
    # Updated in place rather than replaced. `INSERT OR REPLACE` deletes the row and
    # inserts a new one, which hands the region a new id and orphans every tag classify
    # wrote against the old - silently, and only on a second run of this stage.
    conn.execute(
        """INSERT INTO assets
             (guid, rel_path, name, unity_type, ext, size_bytes, width, height,
              has_alpha, alpha_ratio, is_grayscale, dominant_hex, dhash, image_path,
              origin)
           VALUES (NULL, :rel, :name, 'Sprite', 'atlas', :bytes, :width, :height,
                   :has_alpha, :alpha_ratio, :is_grayscale, :dominant_hex, :dhash,
                   :image, 'game')
           ON CONFLICT(rel_path) DO UPDATE SET
             name=excluded.name, unity_type=excluded.unity_type, ext=excluded.ext,
             size_bytes=excluded.size_bytes, width=excluded.width,
             height=excluded.height, has_alpha=excluded.has_alpha,
             alpha_ratio=excluded.alpha_ratio, is_grayscale=excluded.is_grayscale,
             dominant_hex=excluded.dominant_hex, dhash=excluded.dhash,
             image_path=excluded.image_path, origin=excluded.origin""",
        {"rel": rel, "name": region["name"], "bytes": target.stat().st_size,
         "image": f"sprites/{target.name}", "dhash": fingerprint, **info})
    if not guid:
        return
    asset_id = conn.execute("SELECT id FROM assets WHERE rel_path = ?",
                            (rel,)).fetchone()[0]
    # Unity measures a rect up from the bottom of the sheet and an atlas descriptor
    # measures down from the top; the rest of the pipeline speaks Unity.
    tall = footprint(region)[1]
    conn.execute(
        """INSERT OR REPLACE INTO sprites
             (asset_id, atlas_guid, x, y, w, h, rotated, sliced_path, ppu,
              anchor_x, anchor_y, border)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 100.0, 0.5, 0.5, NULL)""",
        (asset_id, guid, region["x"], region["page_h"] - region["y"] - tall,
         region["w"], region["h"], 1 if region["turn"] in (90, 270) else 0,
         f"sprites/{target.name}"))


def pose_skeleton(conn: sqlite3.Connection, descriptor: Path, assets_root: Path,
                  out_dir: Path, pieces: dict[str, dict], stats: dict[str, int]) -> None:
    """Draw the skeleton beside a descriptor in its setup pose, from the regions just cut."""
    import hashlib
    from .skeleton import draw_pose, read_skeleton, skeleton_beside
    source = skeleton_beside(descriptor)
    if source is None or not pieces:
        return
    stats["skeletons"] += 1
    try:
        skeleton = read_skeleton(source.read_bytes())
    except (OSError, ValueError):
        stats["unreadable"] += 1
        return
    rel = descriptor.relative_to(assets_root).as_posix()
    stem = SAFE_RE.sub("_", descriptor.name.split(".atlas")[0])[:80]
    target = out_dir / "poses" / f"spine_{stem}_{hashlib.sha1(rel.encode()).hexdigest()[:6]}.png"
    drawn = draw_pose(skeleton, pieces, target)
    if not drawn:
        return
    stats["posed"] += 1
    conn.execute("INSERT OR REPLACE INTO spine_poses VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (rel, f"poses/{target.name}", drawn["size"][0], drawn["size"][1],
                  skeleton["version"], drawn["pieces"], round(drawn["fill"], 3)))


def build(assets_root: Path, out_dir: Path, conn: sqlite3.Connection) -> dict[str, int]:
    """Cut every Spine region in the export and record it as a sprite."""
    sprite_dir = out_dir / "sprites"
    sprite_dir.mkdir(parents=True, exist_ok=True)
    descriptors = sorted(set(assets_root.rglob("*.atlas.txt")) |
                         set(assets_root.rglob("*.atlas")))
    stats = {"descriptors": len(descriptors), "regions": 0, "cut": 0,
             "no_page": 0, "out_of_bounds": 0, "reused": 0, "no_atlas": 0,
             "skeletons": 0, "posed": 0, "unreadable": 0}
    conn.execute("DELETE FROM spine_poses")
    if not descriptors:
        return stats
    index = image_index(assets_root)

    # The page is already in the catalogue as a texture, so a region can point at it
    # the way a sliced sprite points at its atlas.
    texture_guid = {}
    for row in conn.execute(
        "SELECT guid, rel_path FROM assets WHERE unity_type='Texture2D' "
        "AND guid IS NOT NULL"):
        texture_guid.setdefault(Path(row["rel_path"]).name, row["guid"])

    for descriptor in descriptors:
        try:
            regions = parse_atlas(read_text(descriptor))
        except OSError:
            continue
        stats["regions"] += len(regions)
        pieces: dict[str, dict] = {}
        by_page: dict[str, list[dict]] = {}
        for region in regions:
            by_page.setdefault(region["page"], []).append(region)
        for page, group in by_page.items():
            source = page_image(descriptor, page, index,
                                (group[0]["page_w"], group[0]["page_h"]))
            if source is None:
                stats["no_page"] += len(group)
                continue
            try:
                sheet = load_rgba(source)
            except (OSError, ValueError):
                stats["no_page"] += len(group)
                continue
            with sheet:
                group = fit_to_page(group, sheet.size)
                guid = texture_guid.get(source.name)
                stem = descriptor.name.split(".atlas")[0]
                for region in group:
                    # Checked before the file is either cut or reused. A stale piece
                    # left by an earlier run against the wrong page would otherwise be
                    # registered unexamined, which is how eighty-five wrong crops
                    # survived a re-run of this stage.
                    if not on_page(region, sheet.size):
                        stats["out_of_bounds"] += 1
                        continue
                    safe = SAFE_RE.sub("_", f"{stem}-{region['name']}")[:90]
                    target = sprite_dir / f"{safe}.png"
                    if target.exists():
                        stats["reused"] += 1
                    else:
                        cut(sheet, region).save(target, "PNG", optimize=True)
                    record(conn, descriptor, assets_root, region, target, guid)
                    pieces[region["name"]] = {"image": target, "w": region["w"],
                                              "h": region["h"], "orig": region.get("orig"),
                                              "offset": region.get("offset")}
                    stats["cut"] += 1
                    # Cut, but with no catalogued page to hang it on: the region is
                    # still art, and the atlas view will have nothing to show for it.
                    if not guid:
                        stats["no_atlas"] += 1
        pose_skeleton(conn, descriptor, assets_root, out_dir, pieces, stats)
    conn.commit()
    return stats


def main() -> None:
    import argparse

    from .core import connect

    parser = argparse.ArgumentParser(description="Cut Spine atlas regions.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    conn = connect(args.out / "assetlab.db")
    stats = build(args.export.resolve(), args.out.resolve(), conn)
    print(f"spine: {stats['cut']} regions cut from {stats['descriptors']} descriptors "
          f"({stats['reused']} reused, {stats['no_page']} without a page, "
          f"{stats['out_of_bounds']} out of bounds, "
          f"{stats['no_atlas']} with no catalogued page)")


if __name__ == "__main__":
    main()
