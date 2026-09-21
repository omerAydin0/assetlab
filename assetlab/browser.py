"""Stage 7 - thumbnails plus a self-contained research browser.

The data is embedded directly into the HTML because browsers block fetch() from
file:// URLs; images stay as separate files, which load fine from disk. Open
``browser.html`` directly, no server needed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

from PIL import Image

from .core import connect, load_rgba, make_thumbnail
from .objects import attach_spine_poses, group_objects, prefab_objects

AUDIO_EXT = {"ogg", "wav", "mp3", "m4a", "aac", "flac"}
FONT_EXT = {"ttf", "otf", "woff", "woff2"}
TEXT_EXT = {"json", "txt", "bytes", "xml", "csv", "cs", "shader", "cginc", "hlsl",
            "asset", "mat", "prefab", "unity", "anim", "controller", "spriteatlas",
            "shadervariants", "config"}
# Excerpts are inlined into the page, so both the per-file slice and the total are
# bounded; anything larger stays a link.
EXCERPT_CHARS = 1400
EXCERPT_MAX_BYTES = 64 * 1024
EXCERPT_BUDGET = 1_500_000
#: Width of the atlas copy kept beside the page. The outlines are drawn in
#: percentages, so scale costs detail and nothing else, and a sheet is looked at
#: to see where a sprite sits on it rather than to read the sprite.
SHEET_MAX = 1024


def save_sheet(source: Path, target: Path, size: int = SHEET_MAX) -> None:
    """A fitted copy of an atlas page, with no padding.

    The outlines over it are positioned in percentages of the image box, so the copy
    has to keep the sheet's own proportions exactly - a square letterboxed canvas
    would put every cut in the wrong place.
    """
    with load_rgba(source) as image:
        scale = min(1.0, size / max(1, image.width, image.height))
        view = image if scale == 1 else image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS)
        # Most of a sheet is transparent; JPEG has no alpha, so it is laid on the same
        # ground the panel draws behind it.
        canvas = Image.new("RGB", view.size, (14, 16, 20))
        canvas.paste(view, mask=view.getchannel("A"))
        target.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(target, "JPEG", quality=82, optimize=True)


def media_kind(ext: str, has_image: bool) -> str | None:
    if has_image:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in FONT_EXT:
        return "font"
    if ext in TEXT_EXT:
        return "text"
    return None

#: The page is written as a page. Keeping several hundred lines of JavaScript inside
#: Python string literals cost a working day of escaping accidents - a backslash eaten
#: by a heredoc, an escape that became a real newline - and hid the code from every
#: tool that reads JavaScript. These are read at build time and substituted as before.
WEB = Path(__file__).parent / "web"


def web_source(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


#: The rig: sampling a clip's curves, composing a sprite onto its transform chain,
#: framing what it reaches and drawing it. Shared with the hub, which had its own copy
#: identical to the line until a one-line fix landed in only one of them.
RIG_ENGINE = web_source("rig.js")
#: The object, animation and obstacle-panel views, shared with the hub for the same
#: reason.
SHARED_VIEWS = web_source("views.js")
#: The mesh chain a 3D build is read through, shared for the same reason.
MODEL_VIEW = web_source("models.js")
PAGE = web_source("browser.html")



def build(out_dir: Path, assets_root: Path, title: str, conn: sqlite3.Connection) -> dict[str, int]:
    thumbs_dir = out_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    usage: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute(
        "SELECT asset_id, holder_name FROM used_by WHERE holder_name IS NOT NULL"):
        if len(usage[row["asset_id"]]) < 25:
            usage[row["asset_id"]].append(row["holder_name"])

    subcategory = {
        row["asset_id"]: row["value"]
        for row in conn.execute("SELECT asset_id, value FROM tags WHERE kind='subcategory'")
    }
    obstacles = {
        row[0] for row in conn.execute(
            "SELECT asset_id FROM tags WHERE kind='category' AND value='Obstacle'")
    }
    # The pages sprites were cut from. They are the largest images in the catalogue,
    # so an obstacle set that keeps them shows its atlas sheet instead of its art.
    atlas_pages: set[str] = set()
    # Where each sprite was cut from. The page is worth showing beside the pieces -
    # it is the one view that says which art the studio chose to pack together.
    sprite_rect: dict[int, tuple] = {}
    packed: dict[str, int] = {}
    rect_columns = {row[1] for row in conn.execute("PRAGMA table_info(sprites)")}
    rotated = "rotated" if "rotated" in rect_columns else "0 AS rotated"
    for row in conn.execute(
        f"SELECT asset_id, atlas_guid, x, y, w, h, {rotated} FROM sprites "
        "WHERE atlas_guid IS NOT NULL"):
        atlas_pages.add(row["atlas_guid"])
        packed[row["atlas_guid"]] = packed.get(row["atlas_guid"], 0) + 1
        sprite_rect[row["asset_id"]] = (row["atlas_guid"], row["x"], row["y"],
                                        row["w"], row["h"], row["rotated"] or 0)
    # Multi-part obstacle art only reads assembled, so every member of a piece set
    # carries the whole set with it.
    piece_sets: dict[str, list[dict]] = defaultdict(list)
    piece_key: dict[int, str] = {}
    try:
        for row in conn.execute(
            """SELECT p.group_key, p.position, a.id, a.name, a.image_path, a.width, a.height
                 FROM piece_groups p JOIN assets a ON a.id = p.asset_id
                WHERE a.image_path IS NOT NULL
                ORDER BY p.group_key, p.position, a.name"""):
            piece_key[row["id"]] = row["group_key"]
            piece_sets[row["group_key"]].append({
                "n": row["name"], "pos": row["position"],
                "img": (row["image_path"] if row["image_path"].startswith("sprites/")
                        else (assets_root / row["image_path"]).as_uri()),
                "w": row["width"], "h": row["height"]})
    except sqlite3.OperationalError:
        pass

    animations: dict[int, dict] = {}
    try:
        for row in conn.execute(
            """SELECT asset_id, frames, layers, nodes, masks, duration, frame_count,
                      curve_summary FROM animations"""):
            animations[row["asset_id"]] = {
                "frames": json.loads(row["frames"]) if row["frames"] else None,
                "layers": json.loads(row["layers"]) if row["layers"] else None,
                "nodes": json.loads(row["nodes"]) if row["nodes"] else None,
                "masks": json.loads(row["masks"]) if row["masks"] else None,
                "duration": row["duration"], "count": row["frame_count"],
                "curves": row["curve_summary"],
            }
    except sqlite3.OperationalError:
        pass   # animations stage not run for this catalog

    # Art first. Sorted by type alone the grid opens on AnimationClip and
    # AnimatorController - a screen of empty placeholders - and a build's 5,000
    # pictures sit behind 9,000 rows that have nothing to show.
    rows = conn.execute(
        """SELECT id, guid, name, rel_path, unity_type, ext, width, height, size_bytes,
                  image_path, duplicate_group, primary_role, primary_feature,
                  primary_mechanic, duration_seconds, sample_rate, channels, origin
             FROM assets
            ORDER BY (image_path IS NULL), unity_type, name"""
    ).fetchall()

    records, made = [], 0
    row_guid: list[str | None] = []
    row_id: list[int] = []
    row_image: list[str | None] = []
    excerpt_budget = EXCERPT_BUDGET
    for row in rows:
        thumb = None
        if row["image_path"]:
            source = out_dir / row["image_path"] if row["image_path"].startswith("sprites/") \
                else assets_root / row["image_path"]
            target = thumbs_dir / f"{row['id']}.jpg"
            if not target.exists():
                try:
                    with load_rgba(source) as image:
                        make_thumbnail(image, target)
                    made += 1
                except (OSError, ValueError):
                    target = None
            if target and target.exists():
                thumb = f"thumbs/{row['id']}.jpg"
        image_href = None
        if row["image_path"]:
            image_href = (row["image_path"] if row["image_path"].startswith("sprites/")
                          else (assets_root / row["image_path"]).as_uri())

        ext = (row["ext"] or "").lower()
        kind = media_kind(ext, bool(row["image_path"]))
        clip = animations.get(row["id"])
        if clip and (clip["frames"] or clip["layers"]):
            # Watchable either as a frame sequence or as a transform-driven rig; the
            # first sprite stands in as the thumbnail.
            kind = "animation"
            thumb = thumb or (clip["frames"][0][1] if clip["frames"]
                              else clip["layers"][0]["img"])
        source = assets_root / row["rel_path"]
        href = image_href
        if kind in {"audio", "font"}:
            # Copied next to the page so playback works both from file:// and from a
            # local server; a file:// src is blocked when the page is served over http.
            target = media_dir / f"{row['id']}.{ext}"
            if not target.exists():
                try:
                    shutil.copyfile(source, target)
                except OSError:
                    target = None
            href = f"media/{target.name}" if target and target.exists() else source.as_uri()
        elif kind == "text":
            href = source.as_uri()
        excerpt = None
        if kind == "text" and (row["size_bytes"] or 0) <= EXCERPT_MAX_BYTES \
                and excerpt_budget > 0:
            try:
                excerpt = source.read_text(encoding="utf-8", errors="replace")[:EXCERPT_CHARS]
                excerpt_budget -= len(excerpt)
            except OSError:
                excerpt = None

        records.append({
            "n": row["name"], "p": row["rel_path"], "type": row["unity_type"],
            "w": row["width"], "h": row["height"], "b": row["size_bytes"],
            "img": image_href, "t": thumb, "kind": kind, "href": href, "ex": excerpt,
            "dup": row["duplicate_group"], "role": row["primary_role"],
            "feature": row["primary_feature"], "mechanic": row["primary_mechanic"],
            "obstacle": row["id"] in obstacles, "sub": subcategory.get(row["id"]),
            "at": 1 if row["guid"] in atlas_pages else None,
            "ac": packed.get(row["guid"]),
            # A region cut from an atlas descriptor is addressed `<descriptor>#<name>`,
            # and everything in one descriptor is one skeleton's art. The build said so
            # itself, which beats any reading of the names.
            "set": row["rel_path"].split("#")[0] if "#" in row["rel_path"] else None,
            "eng": 1 if row["origin"] == "engine" else None,
            "u": usage.get(row["id"], []),
            "dur": row["duration_seconds"], "rate": row["sample_rate"],
            "ch": row["channels"],
            "fr": clip["frames"] if clip else None,
            "layers": clip["layers"] if clip else None,
            "nodes": clip["nodes"] if clip else None,
            "masks": clip["masks"] if clip else None,
            "pg": piece_sets.get(piece_key.get(row["id"])) if row["id"] in piece_key else None,
            "clipdur": clip["duration"] if clip else None,
            "curves": clip["curves"] if clip else None,
        })
        row_guid.append(row["guid"])
        row_id.append(row["id"])
        row_image.append(row["image_path"])
        if made and made % 400 == 0:
            print(f"  {made} thumbnails", flush=True)

    # The atlas is addressed the way everything else on the page is - by its position
    # in DATA - so the browser can open the sheet a sprite came from without a lookup
    # table of its own.
    place_of_guid = {guid: position for position, guid in enumerate(row_guid) if guid}
    for position, asset_id in enumerate(row_id):
        rect = sprite_rect.get(asset_id)
        if not rect:
            continue
        page = place_of_guid.get(rect[0])
        if page is None:
            continue
        records[position]["ax"] = page
        records[position]["r"] = list(rect[1:])

    # The build's own objects first: each prefab that assembles two or more sprites.
    # Naming then groups only what no prefab puts together.
    assembled = prefab_objects(conn, records, row_id)
    grouped = assembled + group_objects(records, [
        (position, [layer["img"] for layer in record["layers"]])
        for position, record in enumerate(records) if record.get("layers")])

    attach_spine_poses(conn, grouped, records)

    # The sheet, as the page can actually show it. A Texture2D is addressed by a
    # file:// URI, which a browser refuses to load once the catalogue is served over
    # HTTP - and being able to read it from a phone is why it is served at all. Only
    # pages that hold more than one sprite are copied: everything else is a sprite's
    # own texture, which the pieces strip already shows.
    sheet_dir = out_dir / "atlas"
    sheets = 0
    for page in {entry["a"] for entry in grouped if entry["a"] is not None}:
        record = records[page]
        if not record.get("ac") or record["ac"] < 2 or not row_image[page]:
            continue
        target = sheet_dir / f"{row_id[page]}.jpg"
        if not target.exists():
            stored = row_image[page]
            source = (out_dir / stored if stored.startswith("sprites/")
                      else assets_root / stored)
            try:
                save_sheet(source, target)
            except (OSError, ValueError):
                continue
            sheets += 1
        record["sheet"] = f"atlas/{target.name}"

    # The mesh chain, for builds that have one. Textures are addressed the same way
    # sprites are, so a model's surface loads from the same folders as everything
    # else on the page.
    models: list[dict] = []
    try:
        for row in conn.execute(
            """SELECT prefab_id, prefab_name, path, object_name, mesh_name,
                      mesh_bytes, skinned, materials, render_path, tri_count,
                      placements, object_key
                 FROM models ORDER BY placements DESC, skinned DESC, tri_count DESC,
                          mesh_name"""):
            entry = {
                "prefab_id": row["prefab_id"],
                "prefab_name": row["prefab_name"], "path": row["path"],
                "object_name": row["object_name"], "mesh_name": row["mesh_name"],
                "mesh_bytes": row["mesh_bytes"], "skinned": bool(row["skinned"]),
                "render": row["render_path"], "tris": row["tri_count"],
                "placements": row["placements"], "key": row["object_key"],
                "materials": json.loads(row["materials"]) if row["materials"] else [],
            }
            for material in entry["materials"]:
                for texture in material.get("textures", []):
                    path = texture.get("img")
                    if path and not path.startswith("sprites/"):
                        texture["img"] = (assets_root / path).as_uri()
            models.append(entry)
    except sqlite3.OperationalError:
        pass                      # models stage not run for this catalogue

    # An assembled prefab, drawn whole. This is the one view that shows the game
    # rather than its parts, so it leads the 3D tab.
    scenes: list[dict] = []
    try:
        for row in conn.execute(
            """SELECT prefab_id, prefab_name, object_name, placements, object_key,
                      render_path, part_count, tri_count
                 FROM scenes ORDER BY placements DESC, tri_count DESC"""):
            scenes.append({"id": row["prefab_id"], "name": row["object_name"]
                           or row["prefab_name"],
                           "from": row["prefab_name"], "key": row["object_key"],
                           "placements": row["placements"],
                           "render": row["render_path"], "parts": row["part_count"],
                           "tris": row["tri_count"]})
    except sqlite3.OperationalError:
        pass

    stored = conn.execute(
        "SELECT value FROM meta WHERE key = 'profile'").fetchone()
    profile = json.loads(stored["value"]) if stored else None

    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    page = (PAGE.replace("__RIG_ENGINE__", RIG_ENGINE)
                .replace("__MODEL_VIEW__", MODEL_VIEW)
                .replace("__SHARED_VIEWS__", SHARED_VIEWS)
                .replace("__DATA__", payload)
                .replace("__MODELS__", json.dumps(models, ensure_ascii=False,
                                                  separators=(",", ":")))
                .replace("__SCENES__", json.dumps(scenes, ensure_ascii=False,
                                                  separators=(",", ":")))
                .replace("__OBJECTS__", json.dumps(grouped, ensure_ascii=False,
                                                   separators=(",", ":")))
                .replace("__PROFILE__", json.dumps(profile))
                .replace("__TITLE__", title))
    (out_dir / "browser.html").write_text(page, encoding="utf-8")
    return {"assets": len(records), "thumbnails_created": made,
            "with_thumb": sum(1 for r in records if r["t"]), "models": len(models),
            "objects": len(grouped), "sheets": sheets}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the research browser.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--title", default="AssetLab")
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    stats = build(args.out.resolve(), args.export.resolve(), args.title, conn)
    print(f"browser.html: {stats['assets']} assets, {stats['with_thumb']} with thumbnails "
          f"({stats['thumbnails_created']} new)")
    print(f"open: {(args.out.resolve() / 'browser.html')}")
    conn.close()


if __name__ == "__main__":
    main()
