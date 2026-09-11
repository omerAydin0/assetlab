"""Combined browser: every catalogued game in one page, filtered by tick box.

Embedding five full catalogues would be ~50 MB of JSON and slow to parse, so the
hub carries only the previewable assets (images, audio, animations, fonts) and
drops text excerpts and usage lists. Those stay in each game's own browser.html,
which remains the complete view.

Media paths are rewritten to point at each game's output folder, so the hub must
sit next to them - `out/hub.html` alongside `out/<game>/`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .browser import MODEL_VIEW, RIG_ENGINE, SHARED_VIEWS, web_source
from .core import connect
from .objects import attach_spine_poses, group_objects, prefab_objects

MEDIA_KINDS = {"image", "audio", "animation", "font"}

PAGE = web_source("hub.html")


def collect(out_dir: Path, name: str) -> tuple[list[dict], dict] | None:
    """Pull the previewable rows out of one game's catalogue."""
    database = out_dir / name / "assetlab.db"
    if not database.is_file():
        return None
    conn = connect(database)
    conn.row_factory = sqlite3.Row

    obstacles = {row[0] for row in conn.execute(
        "SELECT asset_id FROM tags WHERE kind='category' AND value='Obstacle'")}
    # The pages sprites were cut from. They are the largest images in the catalogue,
    # so an obstacle set that keeps them shows its atlas sheet instead of its art.
    atlas_pages: set[str] = set()
    packed: dict[str, int] = {}
    sprite_rect: dict[int, tuple] = {}
    rect_columns = {row[1] for row in conn.execute("PRAGMA table_info(sprites)")}
    rotated = "rotated" if "rotated" in rect_columns else "0 AS rotated"
    for row in conn.execute(
        f"SELECT asset_id, atlas_guid, x, y, w, h, {rotated} FROM sprites "
        "WHERE atlas_guid IS NOT NULL"):
        atlas_pages.add(row["atlas_guid"])
        packed[row["atlas_guid"]] = packed.get(row["atlas_guid"], 0) + 1
        sprite_rect[row["asset_id"]] = (row["atlas_guid"], row["x"], row["y"],
                                        row["w"], row["h"], row["rotated"] or 0)
    clips: dict[int, dict] = {}
    try:
        for row in conn.execute(
            "SELECT asset_id, frames, layers, nodes, masks, duration FROM animations"):
            if row["frames"] or row["layers"]:
                clips[row["asset_id"]] = {
                    "fr": json.loads(row["frames"]) if row["frames"] else None,
                    "layers": json.loads(row["layers"]) if row["layers"] else None,
                    "nodes": json.loads(row["nodes"]) if row["nodes"] else None,
                    "masks": json.loads(row["masks"]) if row["masks"] else None,
                    "dur": row["duration"]}
    except sqlite3.OperationalError:
        pass

    prefix = f"{name}/"
    # Multi-part obstacle art carries its whole set, so it can be shown assembled.
    piece_sets: dict[str, list[dict]] = {}
    piece_key: dict[int, str] = {}
    try:
        for row in conn.execute(
            """SELECT p.group_key, p.position, a.id, a.name, a.image_path, a.width, a.height
                 FROM piece_groups p JOIN assets a ON a.id = p.asset_id
                WHERE a.image_path LIKE 'sprites/%'
                ORDER BY p.group_key, p.position, a.name"""):
            piece_key[row["id"]] = row["group_key"]
            piece_sets.setdefault(row["group_key"], []).append({
                "n": row["name"], "pos": row["position"], "w": row["width"],
                "h": row["height"], "img": prefix + row["image_path"]})
    except sqlite3.OperationalError:
        pass

    records = []
    row_guid: list[str | None] = []
    row_id: list[int] = []
    # Art first: sorted by type alone the grid opens on rows that have no picture.
    for row in conn.execute(
        """SELECT id, guid, name, rel_path, unity_type, ext, width, height, image_path,
                  primary_role, primary_feature, primary_mechanic,
                  duration_seconds, thumbnail, origin
             FROM assets
            ORDER BY (image_path IS NULL), unity_type, name"""):
        ext = (row["ext"] or "").lower()
        clip = clips.get(row["id"])
        if row["image_path"]:
            kind = "image"
        elif clip:
            kind = "animation"
        elif ext in {"ogg", "wav", "mp3", "m4a", "aac", "flac"}:
            kind = "audio"
        elif ext in {"ttf", "otf", "woff", "woff2"}:
            kind = "font"
        else:
            continue                      # text and everything else stays per-game
        if kind not in MEDIA_KINDS:
            continue

        thumb = (out_dir / name / "thumbs" / f"{row['id']}.jpg")
        record = {
            "g": name, "n": row["name"], "p": row["rel_path"], "type": row["unity_type"],
            "w": row["width"], "h": row["height"], "kind": kind,
            "role": row["primary_role"], "feature": row["primary_feature"],
            "mechanic": row["primary_mechanic"], "obstacle": row["id"] in obstacles,
            "at": 1 if row["guid"] in atlas_pages else None,
            "ac": packed.get(row["guid"]),
            "eng": 1 if row["origin"] == "engine" else None,
            # Everything in one atlas descriptor is one skeleton's art; the build said
            # so itself when it wrote them into the same file.
            "set": row["rel_path"].split("#")[0] if "#" in row["rel_path"] else None,
            "pg": piece_sets.get(piece_key.get(row["id"])),
            "t": f"{prefix}thumbs/{row['id']}.jpg" if thumb.is_file() else None,
        }
        if kind == "image" and row["image_path"]:
            record["img"] = (prefix + row["image_path"]
                             if row["image_path"].startswith("sprites/") else None)
            if not record["img"]:
                record["img"] = record["t"]
        elif kind == "audio":
            record["href"] = f"{prefix}media/{row['id']}.{ext}"
            record["dur"] = row["duration_seconds"]
        elif kind == "font":
            record["href"] = f"{prefix}media/{row['id']}.{ext}"
        elif kind == "animation":
            record["fr"] = ([[t, prefix + p] for t, p in clip["fr"]] if clip["fr"] else None)
            record["layers"] = ([dict(layer, img=prefix + layer["img"])
                                 for layer in clip["layers"]] if clip["layers"] else None)
            record["nodes"] = clip["nodes"]
            record["masks"] = clip["masks"]
            record["clipdur"] = clip["dur"]
        records.append(record)
        row_guid.append(row["guid"])
        row_id.append(row["id"])

    # Where each sprite was cut from, and the copy of that sheet the per-game build
    # left beside its catalogue. The full texture is a file:// path the hub cannot
    # reach, and the square thumbnail beside it is padded, so outlines drawn over it
    # would land in the wrong place - without the copy the sheet is simply not shown.
    place_of_guid = {guid: position for position, guid in enumerate(row_guid) if guid}
    for position, asset_id in enumerate(row_id):
        rect = sprite_rect.get(asset_id)
        page = place_of_guid.get(rect[0]) if rect else None
        if page is None:
            continue
        records[position]["ax"] = page
        records[position]["r"] = list(rect[1:])
    for position, guid in enumerate(row_guid):
        if not packed.get(guid) or packed[guid] < 2:
            continue
        if (out_dir / name / "atlas" / f"{row_id[position]}.jpg").is_file():
            records[position]["sheet"] = f"{prefix}atlas/{row_id[position]}.jpg"

    assembled = prefab_objects(conn, records, row_id, prefix)
    objects = assembled + group_objects(records, [
        (position, [layer["img"] for layer in record["layers"]])
        for position, record in enumerate(records) if record.get("layers")])

    attach_spine_poses(conn, objects, records, prefix)

    # The mesh chain, for a build that has one. Texture paths get the same game
    # prefix as sprites, so a model's surface loads from that build's folder.
    models: list[dict] = []
    try:
        for row in conn.execute(
            """SELECT prefab_id, prefab_name, path, object_name, mesh_name,
                      mesh_bytes, skinned, materials, render_path, tri_count
                 FROM models ORDER BY skinned DESC, tri_count DESC, mesh_name"""):
            entry = {
                "g": name, "prefab_id": row["prefab_id"],
                "prefab_name": row["prefab_name"], "path": row["path"],
                "object_name": row["object_name"], "mesh_name": row["mesh_name"],
                "mesh_bytes": row["mesh_bytes"], "skinned": bool(row["skinned"]),
                "render": prefix + row["render_path"] if row["render_path"] else None,
                "tris": row["tri_count"],
                "materials": json.loads(row["materials"]) if row["materials"] else [],
            }
            for material in entry["materials"]:
                kept = []
                for texture in material.get("textures", []):
                    image = texture.get("img") or ""
                    if image.startswith("sprites/"):
                        texture["img"] = prefix + image
                    else:
                        # A loose texture lives in the export tree, which the hub
                        # cannot reach. Its thumbnail is already next to the page,
                        # so the surface is shown at reduced size rather than lost.
                        thumb = out_dir / name / "thumbs" / f"{texture.get('id')}.jpg"
                        if not thumb.is_file():
                            continue
                        texture["img"] = f"{prefix}thumbs/{texture['id']}.jpg"
                        texture["reduced"] = True
                    kept.append(texture)
                material["textures"] = kept
            models.append(entry)
    except sqlite3.OperationalError:
        pass                       # models stage not run for this catalogue

    scenes: list[dict] = []
    try:
        for row in conn.execute(
            """SELECT prefab_id, prefab_name, render_path, part_count, tri_count
                 FROM scenes ORDER BY tri_count DESC"""):
            scenes.append({"g": name, "id": row["prefab_id"],
                           "name": row["prefab_name"],
                           "render": prefix + row["render_path"],
                           "parts": row["part_count"], "tris": row["tri_count"]})
    except sqlite3.OperationalError:
        pass

    stored = conn.execute("SELECT value FROM meta WHERE key = 'profile'").fetchone()
    profile = json.loads(stored["value"]) if stored else None

    conn.close()
    return records, {"id": name, "title": name, "short": name,
                     "count": len(records), "profile": profile}, models, scenes, objects


def build(out_dir: Path, names: list[str]) -> dict:
    data, games, models, scenes, objects = [], [], [], [], []
    for name in names:
        result = collect(out_dir, name)
        if result is None:
            print(f"  skipped {name}: no catalogue")
            continue
        records, meta, found, assembled, grouped = result
        # Each build is grouped on its own - two games that ship a sprite by the same
        # name are not one object - so the indices arrive local and are shifted onto
        # the combined list here.
        base = len(data)
        # Every index into DATA is shifted, the sheet an individual sprite points at
        # included - the object view compares the two to know which cuts belong on
        # the page it is drawing, and one of them left unshifted matches nothing.
        for record in records:
            if record.get("ax") is not None:
                record["ax"] += base
        for entry in grouped:
            entry["w"] = None if entry["w"] is None else entry["w"] + base
            entry["p"] = [index + base for index in entry["p"]]
            entry["c"] = None if entry["c"] is None else entry["c"] + base
            entry["a"] = None if entry["a"] is None else entry["a"] + base
            if entry.get("cs"):
                entry["cs"] = [index + base for index in entry["cs"]]
        objects.extend(grouped)
        data.extend(records)
        games.append(meta)
        models.extend(found)
        scenes.extend(assembled)
        verdict = (meta.get("profile") or {}).get("verdict", "?").upper()
        extra = f", {len(found)} models" if found else ""
        print(f"  {name:<14} {len(records):>7} previewable assets  [{verdict}]{extra}")

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    page = (PAGE.replace("__RIG_ENGINE__", RIG_ENGINE)
                .replace("__MODEL_VIEW__", MODEL_VIEW)
                .replace("__SHARED_VIEWS__", SHARED_VIEWS)
                .replace("__DATA__", payload)
                .replace("__OBJECTS__", json.dumps(objects, ensure_ascii=False,
                                                   separators=(",", ":")))
                .replace("__MODELS__", json.dumps(models, ensure_ascii=False,
                                                  separators=(",", ":")))
                .replace("__SCENES__", json.dumps(scenes, ensure_ascii=False,
                                                  separators=(",", ":")))
                .replace("__GAMES__", json.dumps(games, ensure_ascii=False)))
    target = out_dir / "hub.html"
    target.write_text(page, encoding="utf-8")
    return {"games": len(games), "assets": len(data), "models": len(models),
            "objects": len(objects), "bytes": len(page), "path": target}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a combined multi-game browser.")
    parser.add_argument("--out", type=Path, default=Path("out"),
                        help="directory holding the per-game catalogues")
    parser.add_argument("--games", nargs="*", default=None,
                        help="catalogue folder names; defaults to every folder in --out")
    args = parser.parse_args()

    names = args.games or sorted(
        p.name for p in args.out.iterdir() if (p / "assetlab.db").is_file())
    stats = build(args.out.resolve(), names)
    print(f"\nhub.html: {stats['games']} games, {stats['assets']} assets, "
          f"{stats['bytes']/1e6:.1f} MB")
    print(f"open: {stats['path']}")


if __name__ == "__main__":
    main()
