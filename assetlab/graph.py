"""Stage 2 - extract the GUID reference graph.

Unity YAML uses custom tags (``%TAG !u!``, ``--- !u!114 &123``) that break strict
YAML parsers, so references are pulled with a regex. For dependency edges that is
both sufficient and far more robust than a full parse.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from .core import REFERENCING_EXT, STREAM_ABOVE, connect, scan_guids


def build_graph(assets_root: Path, conn: sqlite3.Connection) -> int:
    known: set[str] = {
        row[0] for row in conn.execute("SELECT guid FROM assets WHERE guid IS NOT NULL")
    }
    sources = conn.execute(
        "SELECT guid, rel_path FROM assets WHERE guid IS NOT NULL AND ext IS NOT NULL"
    ).fetchall()

    edges: set[tuple[str, str]] = set()
    scanned = 0
    for row in sources:
        rel_path = row["rel_path"]
        if "." + (Path(rel_path).suffix.lower().lstrip(".")) not in REFERENCING_EXT:
            continue
        path = assets_root / rel_path
        scanned += 1
        source_guid = row["guid"]
        # Streamed for every file: a scene assembled from a prop library runs to
        # hundreds of megabytes, and reading one whole costs more than the stage.
        for target in scan_guids(path):
            if target != source_guid and target in known:
                edges.add((source_guid, target))
        if scanned % 500 == 0:
            print(f"  scanned {scanned} referencing assets, {len(edges)} edges", flush=True)

    conn.executemany("INSERT OR IGNORE INTO refs (src_guid, dst_guid) VALUES (?, ?)",
                     sorted(edges))
    conn.commit()
    return len(edges)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the GUID reference graph.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    total = build_graph(args.export.resolve(), conn)
    print(f"reference edges: {total}")
    for row in conn.execute(
        """SELECT a.unity_type, COUNT(*) n
             FROM refs r JOIN assets a ON a.guid = r.src_guid
            GROUP BY a.unity_type ORDER BY n DESC LIMIT 8"""
    ):
        print(f"  from {row['unity_type']:<20} {row['n']}")
    conn.close()


if __name__ == "__main__":
    main()
