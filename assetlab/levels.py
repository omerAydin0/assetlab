"""Stage 6 - parse the reference game's level corpus.

Some builds ship their whole level corpus as Tiled tilemaps - one of the builds
this was written against carries 615 across three A/B sets. Filed by extension they
look like anonymous JSON; read properly they are a complete design record:

  Shelf        - shelf tiles, value = shelf variant
  Layer1..N    - depth layers, i.e. how deeply items are buried
  Paper1..N    - obstacle overlays
  properties   - per-level config (time limit, item-type count, piggy bank, ...)

That gives a real-world difficulty curve to compare a generator against.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
from collections import Counter
from pathlib import Path

from . import flatbuf
from .core import connect

LEVEL_NO_RE = re.compile(r"(\d+)")
# Names games give the playfield itself; everything else that carries tiles is an
# obstacle overlay.
BOARD_LAYER_RE = re.compile(r"^(layer\d*|grid|board|tiles?|main|items?|cells?)$", re.I)
SHELF_LAYER_RE = re.compile(r"^shelf", re.I)
TIME_KEYS = ("time", "duration", "timelimit", "time_limit")
MOVE_KEYS = ("maxmoves", "moves", "movecount", "move_limit", "movelimit")


def iter_properties(raw) -> list[tuple[str, object]]:
    """Tiled changed shape at 1.0: a `{key: value}` map became `[{name, value}]`."""
    if isinstance(raw, dict):
        return [(str(key), value) for key, value in raw.items()]
    if isinstance(raw, list):
        pairs = []
        for item in raw:
            if isinstance(item, dict):
                pairs.append((str(item.get("name")), item.get("value")))
        return pairs
    return []


def parse_level(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return None

    depth_layers, obstacle_layers, object_layers = 0, [], []
    shelf_tiles, shelf_types = 0, 0
    item_tiles, obstacle_tiles, object_groups = 0, 0, 0
    item_values: Counter[int] = Counter()
    properties: dict[str, object] = {}
    tile_layers: list[tuple[str, list[int]]] = []

    for layer in data.get("layers", []):
        name = str(layer.get("name") or "")
        for key, value in iter_properties(layer.get("properties")):
            properties[key] = value
        if layer.get("type") != "tilelayer":
            # Spawners, drop zones and similar carry no board tiles - but they do
            # carry the designer's name for what they place, which is the only
            # statement of the vocabulary some builds ship at all.
            object_groups += 1
            if name:
                object_layers.append(name)
            continue
        tile_layers.append((name, [value for value in layer.get("data", []) if value]))

    # Which layers hold the playfield differs by game: a stacking title builds
    # Layer1..LayerN, a single-grid title has just `grid`. Anything else that carries
    # tiles is an obstacle overlay, whatever it is called.
    board_names = {name for name, _ in tile_layers if BOARD_LAYER_RE.match(name)}
    if not board_names and tile_layers:
        widest = max(tile_layers, key=lambda item: len(item[1]))
        board_names = {widest[0]}

    for name, values in tile_layers:
        if SHELF_LAYER_RE.match(name):
            shelf_tiles += len(values)
            shelf_types = len(set(values))
        elif name in board_names:
            depth_layers += 1
            item_tiles += len(values)
            item_values.update(values)
        else:
            obstacle_layers.append(name)
            obstacle_tiles += len(values)

    for key, value in iter_properties(data.get("properties")):
        properties[key] = value

    def limit(keys: tuple[str, ...]) -> int | None:
        lowered = {key.lower(): value for key, value in properties.items()}
        for key in keys:
            if key in lowered:
                try:
                    return int(float(lowered[key]))
                except (TypeError, ValueError):
                    continue
        return None

    return {
        "grid_w": data.get("width"),
        "grid_h": data.get("height"),
        "layer_count": len(data.get("layers", [])),
        "depth_layers": depth_layers,
        "obstacle_layers": ",".join(obstacle_layers),
        "object_layers": ",".join(dict.fromkeys(object_layers)),
        "obstacle_tiles": obstacle_tiles,
        "shelf_tiles": shelf_tiles,
        "shelf_types": shelf_types,
        "item_tiles": item_tiles,
        "distinct_items": len(item_values),
        "time_limit": limit(TIME_KEYS),
        "move_limit": limit(MOVE_KEYS),
        "object_groups": object_groups,
        "properties": json.dumps(properties, ensure_ascii=False, sort_keys=True),
    }


def looks_like_tiled(path: Path) -> bool:
    """Cheap content sniff so level sets are found wherever a game keeps them.

    Tiled writes its keys alphabetically, so `layers` sits near the top while
    `orientation`/`tiledversion` land after the (large) tile data - hence the
    head *and* tail probe rather than one read.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
            try:
                handle.seek(-512, 2)
            except OSError:
                handle.seek(0)
            tail = handle.read(512)
    except OSError:
        return False
    if b'"layers"' not in head:
        return False
    both = head + tail
    return b'"tiledversion"' in both or b'"orientation"' in both


#: Extensions a designer's own data actually ships in. Unity's own asset
#: extensions are deliberately absent: an export is full of .asset and .prefab
#: files, and counting those would report a level corpus in every build ever made.
DATA_SUFFIXES = {".json", ".bytes", ".txt", ".xml", ".csv", ".tsv", ".dat",
                 ".yaml", ".yml", ".ini", ".lvl", ".map"}
#: Below this a directory is a handful of config files, not a corpus.
MIN_CORPUS = 12
INDEX_RE = re.compile(r"\d+")


def corpus_candidates(root: Path, limit: int = 6,
                      files: list[Path] | None = None) -> list[dict]:
    """Directories holding something shaped like a set of levels.

    The shape is repetition: many files, one extension, one naming pattern with a
    number varying inside it. Every level corpus looks like that whatever format it
    is written in, which is the whole point - the parser reads Tiled only, but the
    detector must not go blind the moment a build ships something else. Finding
    nothing and finding something unreadable are different answers, and reporting
    them identically has already hidden one build's entire corpus.
    """
    # A caller that has already walked the tree passes what it found; walking a
    # large export again to ask a second question about the same files is the
    # slowest thing this module could do.
    if files is None:
        files = [path for path in root.rglob("*")
                 if path.suffix.lower() in DATA_SUFFIXES and path.is_file()]
    groups: dict[tuple[Path, str], list[str]] = {}
    for path in files:
        groups.setdefault((path.parent, path.suffix.lower()), []).append(path.name)

    found: list[dict] = []
    for (directory, suffix), names in groups.items():
        if len(names) < MIN_CORPUS:
            continue
        # `level_1`, `level_2`, ... all normalise to `level_#`; a directory of
        # unrelated config files does not collapse onto one pattern at all.
        patterns = Counter(INDEX_RE.sub("#", Path(name).stem) for name in names)
        pattern, hits = patterns.most_common(1)[0]
        if hits < MIN_CORPUS or "#" not in pattern:
            continue
        try:
            shown = directory.relative_to(root).as_posix()
        except ValueError:
            shown = directory.as_posix()
        found.append({"directory": shown or ".", "extension": suffix, "count": hits,
                      "pattern": pattern + suffix, "files_in_directory": len(names),
                      "example": sorted(names)[0]})
    found.sort(key=lambda entry: -entry["count"])
    return found[:limit]


#: Fields every level of a build carries, describing the board rather than what is
#: on it. They are structure, not difficulty, and counting them as obstacles would
#: put "Grid" and "Colors" in the mechanic list of every game that ships one.
STRUCTURAL_FIELDS = {
    "name", "move", "moves", "grid", "sets", "colors", "colours", "predefined",
    "counts", "background", "type", "goals", "targets", "limits", "board", "boards",
    "cells", "items", "width", "height", "potioncolors", "lowmatch",
    "lightbulbcolororder", "magicgemcolororder", "id", "index", "version",
}
#: Cell fields that place a piece rather than describe an obstacle on it.
STRUCTURAL_CELL_FIELDS = {"filltype", "ispredefined", "boardid", "x", "y", "id",
                          "index", "itemid", "type", "color"}
#: How many levels are read to decide which table a corpus was written from. The
#: schema either fits the format or it does not; a handful settles it, and the
#: spread guards against the first few levels being unusually plain tutorials.
SCHEMA_SAMPLE = 8


def flat_corpora(assets_root: Path) -> dict[Path, list[Path]]:
    """Directories holding a numbered `.bytes` set, keyed by directory."""
    grouped: dict[Path, list[Path]] = {}
    for path in assets_root.rglob("*.bytes"):
        if path.is_file() and LEVEL_NO_RE.search(path.stem):
            grouped.setdefault(path.parent, []).append(path)
    return {where: files for where, files in grouped.items()
            if len(files) >= MIN_CORPUS}


def cell_obstacles(buffer: flatbuf.Buffer, grid_at: int, grid: flatbuf.Schema,
                   schemas: dict[str, flatbuf.Schema]) -> tuple[list[str], int]:
    """Obstacles a build writes onto individual cells, and how many cells carry them.

    A cell only records what is true of it, so its populated slots are its
    obstacles. Fields that place a piece rather than obstruct it - the fill type,
    the board it belongs to, its coordinates - are structure and are left out.
    """
    cells_field = next((entry for entry in grid.fields
                        if entry.name.lower() == "cells"), None)
    if cells_field is None:
        return [], 0
    cell_schema = schemas.get(cells_field.type_name)
    if cell_schema is None:
        return [], 0

    by_slot = {entry.slot: entry.name for entry in cell_schema.fields
               if entry.name.lower() not in STRUCTURAL_CELL_FIELDS}
    found: dict[str, int] = {}
    try:
        cells = buffer.elements(grid_at, cells_field)
    except Exception:                            # noqa: BLE001
        return [], 0
    for cell in cells:
        try:
            slots = buffer.populated(cell)
        except Exception:                        # noqa: BLE001
            continue
        for slot in slots:
            name = by_slot.get(slot)
            if name:
                found[name] = found.get(name, 0) + 1
    return list(found), sum(found.values())


def parse_flat_level(buffer: flatbuf.Buffer, schema: flatbuf.Schema,
                     schemas: dict[str, flatbuf.Schema]) -> dict | None:
    """One FlatBuffers level, in the same shape a Tiled level produces."""
    try:
        table = buffer.root()
        populated = set(buffer.populated(table))
    except Exception:                            # noqa: BLE001 - a bad file, not a bug
        return None

    width = height = 0
    obstacles: list[str] = []
    obstacle_tiles = 0
    item_tiles = 0
    moves = 0

    for entry in schema.fields:
        if entry.slot not in populated:
            continue
        try:
            value = buffer.read(table, entry)
        except Exception:                        # noqa: BLE001
            continue
        lowered = entry.name.lower()

        if lowered in ("move", "moves") and isinstance(value, int):
            moves = value
        elif lowered == "grid" and isinstance(value, int):
            grid = schemas.get(entry.type_name)
            if grid:
                for side in grid.fields:
                    if side.name.lower() in ("width", "height"):
                        try:
                            measured = buffer.read(value, side) or 0
                        except Exception:        # noqa: BLE001
                            measured = 0
                        if side.name.lower() == "width":
                            width = int(measured)
                        else:
                            height = int(measured)
                found, tiles = cell_obstacles(buffer, value, grid, schemas)
                obstacles.extend(found)
                obstacle_tiles += tiles
        elif lowered == "predefined" and isinstance(value, int):
            item_tiles = value
        elif (entry.kind == flatbuf.VECTOR and isinstance(value, int) and value > 0
              and lowered not in STRUCTURAL_FIELDS):
            # A populated, non-structural vector is this level using that mechanic.
            obstacles.append(entry.name)
            obstacle_tiles += value
        elif (entry.kind == flatbuf.SCALAR and isinstance(value, (int, float))
              and value and lowered.endswith("count")
              and lowered not in STRUCTURAL_FIELDS):
            obstacles.append(entry.name[:-len("Count")] or entry.name)
            obstacle_tiles += int(value)

    return {
        "grid_w": width, "grid_h": height,
        "layer_count": len(populated),
        "depth_layers": 0,
        "obstacle_layers": "",
        "object_layers": ",".join(dict.fromkeys(obstacles)),
        "obstacle_tiles": obstacle_tiles,
        "shelf_tiles": 0, "shelf_types": 0,
        "item_tiles": item_tiles,
        "distinct_items": 0,
        "time_limit": 0,
        "move_limit": moves,
        "object_groups": len(obstacles),
        "properties": "",
    }


def flat_corpus_fits(assets_root: Path) -> list[dict]:
    """Which FlatBuffers corpora this build's own schemas explain.

    Detection only. Loading the tables and testing a handful of levels takes a
    second or two; reading fourteen thousand of them takes minutes, and a gate has
    no business doing that.
    """
    corpora = flat_corpora(assets_root)
    if not corpora:
        return []
    schemas = flatbuf.load_schemas(assets_root / "Scripts")
    results: list[dict] = []
    for where, files in sorted(corpora.items()):
        step = max(1, len(files) // SCHEMA_SAMPLE)
        samples = []
        for path in sorted(files)[::step][:SCHEMA_SAMPLE]:
            try:
                samples.append(path.read_bytes())
            except OSError:
                continue
        schema, score = flatbuf.choose_schema(schemas, samples, hint="level") \
            if schemas else (None, 0.0)
        results.append({"directory": where.name, "count": len(files),
                        "schema": schema.name if schema else None,
                        "fit": round(score, 3)})
    results.sort(key=lambda entry: -entry["count"])
    return results


def scan_flat_levels(assets_root: Path) -> tuple[list[dict], list[str]]:
    """Every FlatBuffers level corpus in the export, with what it was read as."""
    corpora = flat_corpora(assets_root)
    if not corpora:
        return [], []
    schemas = flatbuf.load_schemas(assets_root / "Scripts")
    if not schemas:
        return [], []

    rows: list[dict] = []
    notes: list[str] = []
    for where, files in sorted(corpora.items()):
        files.sort(key=lambda path: int(LEVEL_NO_RE.search(path.stem).group(1)))
        step = max(1, len(files) // SCHEMA_SAMPLE)
        samples = [path.read_bytes() for path in files[::step][:SCHEMA_SAMPLE]]
        schema, score = flatbuf.choose_schema(schemas, samples, hint="level")
        if not schema or score < 0.9:
            notes.append(f"{where.name}: {len(files)} files, no schema fits "
                         f"(best {score:.2f})")
            continue
        notes.append(f"{where.name}: {len(files)} levels read as "
                     f"{schema.name} (fit {score:.2f})")
        for path in files:
            try:
                buffer = flatbuf.Buffer(path.read_bytes())
            except OSError:
                continue
            parsed = parse_flat_level(buffer, schema, schemas)
            if not parsed:
                continue
            rows.append({
                "levelset": where.name,
                "level_no": int(LEVEL_NO_RE.search(path.stem).group(1)),
                "rel_path": path.relative_to(assets_root).as_posix(),
                **parsed,
            })
    return rows, notes


def scan_levels(assets_root: Path, conn: sqlite3.Connection) -> int:
    rows = []
    for path in sorted(assets_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".bytes"}:
            continue
        match = LEVEL_NO_RE.search(path.stem)
        if not match or not looks_like_tiled(path):
            continue
        parsed = parse_level(path)
        if not parsed:
            continue
        rows.append({
            "levelset": path.parent.name,
            "level_no": int(match.group(1)),
            "rel_path": path.relative_to(assets_root).as_posix(),
            **parsed,
        })

    if not rows:
        # Nothing Tiled here. A build may still ship its corpus as FlatBuffers,
        # which is unreadable only until its own schema is recovered.
        rows, notes = scan_flat_levels(assets_root)
        for note in notes:
            print(f"  flatbuffers {note}")

    conn.execute("DELETE FROM levels")
    conn.executemany(
        """INSERT OR REPLACE INTO levels
           (levelset, level_no, rel_path, grid_w, grid_h, layer_count, depth_layers,
            obstacle_layers, object_layers, obstacle_tiles, shelf_tiles, shelf_types,
            item_tiles,
            distinct_items, time_limit, move_limit, object_groups, properties)
           VALUES (:levelset, :level_no, :rel_path, :grid_w, :grid_h, :layer_count,
                   :depth_layers, :obstacle_layers, :object_layers, :obstacle_tiles,
                   :shelf_tiles,
                   :shelf_types, :item_tiles, :distinct_items, :time_limit,
                   :move_limit, :object_groups, :properties)""",
        rows,
    )
    conn.commit()
    return len(rows)


def report(conn: sqlite3.Connection, out_dir: Path) -> str:
    lines = ["# Level corpus", ""]
    sets = [row[0] for row in conn.execute("SELECT DISTINCT levelset FROM levels ORDER BY levelset")]
    if not sets:
        text = "# Level corpus\n\nNo Tiled-format level files found in this export.\n"
        (out_dir / "levels_report.md").write_text(text, encoding="utf-8")
        return text
    # Profile the largest level set rather than assuming a particular name.
    main_set = conn.execute(
        "SELECT levelset FROM levels GROUP BY levelset ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]

    def mean(rows: list, column: str, digits: int = 2) -> str:
        values = [row[column] for row in rows if row[column] is not None]
        return f"{statistics.mean(values):.{digits}f}" if values else "-"

    # A game limits play by time or by moves; report whichever it actually uses.
    uses_moves = conn.execute(
        "SELECT COUNT(*) FROM levels WHERE move_limit IS NOT NULL").fetchone()[0] > 0
    limit_column, limit_label = (("move_limit", "moves") if uses_moves
                                 else ("time_limit", "time"))

    lines += ["## Level sets", "",
              f"| level set | levels | grid (min-max) | board layers | items/level | "
              f"distinct items | obstacle tiles | {limit_label} |",
              "|---|---|---|---|---|---|---|---|"]
    for name in sets:
        rows = conn.execute(
            "SELECT * FROM levels WHERE levelset=? ORDER BY level_no", (name,)).fetchall()
        grids = [f"{row['grid_w']}x{row['grid_h']}" for row in rows]
        lines.append(
            f"| `{name}` | {len(rows)} | {grids[0]} - {grids[-1]} | "
            f"{mean(rows, 'depth_layers')} | {mean(rows, 'item_tiles', 1)} | "
            f"{mean(rows, 'distinct_items')} | {mean(rows, 'obstacle_tiles', 1)} | "
            f"{mean(rows, limit_column, 0)} |")

    rows = conn.execute(
        "SELECT * FROM levels WHERE levelset=? ORDER BY level_no", (main_set,)).fetchall()
    block_size = max(20, len(rows) // 20)
    lines += ["", f"## Difficulty curve (`{main_set}`, {block_size}-level blocks)", "",
              f"| levels | board layers | items | distinct items | obstacle tiles | "
              f"{limit_label} |", "|---|---|---|---|---|---|"]
    for start in range(0, len(rows), block_size):
        block = rows[start:start + block_size]
        lines.append(
            f"| {block[0]['level_no']}-{block[-1]['level_no']} | "
            f"{mean(block, 'depth_layers')} | {mean(block, 'item_tiles', 1)} | "
            f"{mean(block, 'distinct_items')} | {mean(block, 'obstacle_tiles', 1)} | "
            f"{mean(block, limit_column, 0)} |")

    obstacles: Counter[str] = Counter()
    for row in conn.execute("SELECT obstacle_layers FROM levels WHERE obstacle_layers <> ''"):
        for name in row[0].split(","):
            obstacles[re.sub(r"\d+$", "", name).strip()] += 1
    if obstacles:
        lines += ["", "## Obstacle layer families", ""]
        lines += [f"- `{name}`: {count} level-layers" for name, count in obstacles.most_common()]

    keys: Counter[str] = Counter()
    for row in conn.execute("SELECT properties FROM levels"):
        keys.update(json.loads(row[0]).keys())
    lines += ["", "## Level config keys", ""]
    lines += [f"- `{key}`: set on {count} levels" for key, count in keys.most_common()]

    text = "\n".join(lines) + "\n"
    (out_dir / "levels_report.md").write_text(text, encoding="utf-8")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse the reference game's level corpus.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    total = scan_levels(args.export.resolve(), conn)
    print(f"parsed {total} levels")
    print(report(conn, args.out.resolve()))
    conn.close()


if __name__ == "__main__":
    main()
