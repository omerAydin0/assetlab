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

    depth_layers, obstacle_layers = 0, []
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
            # Spawners, drop zones and similar carry no board tiles.
            object_groups += 1
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

    conn.execute("DELETE FROM levels")
    conn.executemany(
        """INSERT OR REPLACE INTO levels
           (levelset, level_no, rel_path, grid_w, grid_h, layer_count, depth_layers,
            obstacle_layers, obstacle_tiles, shelf_tiles, shelf_types, item_tiles,
            distinct_items, time_limit, move_limit, object_groups, properties)
           VALUES (:levelset, :level_no, :rel_path, :grid_w, :grid_h, :layer_count,
                   :depth_layers, :obstacle_layers, :obstacle_tiles, :shelf_tiles,
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
