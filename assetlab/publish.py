"""Stage 8 - assemble a portable copy of the catalogue for other machines.

``out/`` is not portable. The pages there link ~4500 images straight into this
machine's AssetRipper export with ``file://`` URLs, so on any other computer those
panels come up empty. The linked originals are 1.1 GB of atlas sheets, far too much
to carry, so this stage bakes a 1024 px WebP of each one into the bundle and rewrites
the links to point at it - 39 MB instead of 1161.

What lands in ``dist/`` is only what a browser needs: the pages, the sliced sprites,
the thumbnails, the audio and fonts. No database, no export, no code. Copy the folder
to another machine and open ``hub.html``.

Re-running is incremental: files are copied only when the source is newer or a
different size, so an update after a rebuild moves the pages and whatever art
actually changed.

This is a personal research library. Keeping it on your own machines is the point -
putting extracted third-party art on a public host is redistribution, which this tool
does not do for you.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

from PIL import Image

from .core import connect

MIRROR = ("sprites", "thumbs", "media")
PREVIEW_SIDE = 1024
PREVIEW_QUALITY = 80
# `as_uri()` percent-encodes, and that is exactly what the pages contain.
FILE_URL_RE = re.compile(r"file:///[^\"'\s>]+")


def mirror(source: Path, target: Path) -> tuple[int, int]:
    """Copy a directory, skipping files that are already identical. -> (copied, kept)"""
    if not source.is_dir():
        return 0, 0
    target.mkdir(parents=True, exist_ok=True)
    copied = kept = 0
    for path in source.iterdir():
        if not path.is_file():
            continue
        destination = target / path.name
        info = path.stat()
        if destination.exists():
            existing = destination.stat()
            if existing.st_size == info.st_size and existing.st_mtime >= info.st_mtime:
                kept += 1
                continue
        shutil.copy2(path, destination)
        copied += 1
    return copied, kept


def build_previews(conn: sqlite3.Connection, assets_root: Path,
                   target: Path) -> dict[str, str]:
    """Bake a viewable copy of every image the pages link to externally.

    Returns the ``file://`` URL of each original mapped to its path inside the
    bundle, which is what the link rewrite needs.
    """
    target.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    made = kept = missing = 0
    rows = conn.execute(
        """SELECT id, image_path FROM assets
            WHERE image_path IS NOT NULL AND image_path NOT LIKE 'sprites/%'""").fetchall()
    for row in rows:
        source = assets_root / row["image_path"]
        destination = target / f"{row['id']}.webp"
        try:
            info = source.stat()
        except OSError:
            missing += 1
            continue
        mapping[source.as_uri()] = f"preview/{destination.name}"
        if destination.exists() and destination.stat().st_mtime >= info.st_mtime:
            kept += 1
            continue
        try:
            with Image.open(source) as image:
                preview = image.convert("RGBA")
                preview.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), Image.Resampling.LANCZOS)
                preview.save(destination, "WEBP", quality=PREVIEW_QUALITY, method=4)
            made += 1
        except (OSError, ValueError):
            mapping.pop(source.as_uri(), None)
            missing += 1
    print(f"  previews: {made} new, {kept} unchanged, {missing} unreadable")
    return mapping


def portable_page(page: Path, target: Path, previews: dict[str, str]) -> dict[str, int]:
    """Rewrite the page's links into the bundle. -> counts of what happened."""
    text = page.read_text(encoding="utf-8")
    stats = {"to_preview": 0, "dropped": 0}

    def replace(match: re.Match) -> str:
        url = match.group(0)
        local = previews.get(url)
        if local:
            stats["to_preview"] += 1
            return local
        # Prefabs, scenes and scripts are the source build, not the library. They stay
        # on the workstation; the page keeps the inlined excerpt and loses the link.
        stats["dropped"] += 1
        return ""

    target.write_text(FILE_URL_RE.sub(replace, text), encoding="utf-8")
    return stats


def publish(out_dir: Path, dist_dir: Path) -> dict:
    dist_dir.mkdir(parents=True, exist_ok=True)
    games, totals = [], {"copied": 0, "kept": 0}
    for database in sorted(out_dir.glob("*/assetlab.db")):
        name = database.parent.name
        print(f"[{name}]", flush=True)
        conn = connect(database)
        conn.row_factory = sqlite3.Row
        root = conn.execute("SELECT value FROM meta WHERE key='assets_root'").fetchone()
        assets_root = Path(root["value"]) if root else None

        for folder in MIRROR:
            copied, kept = mirror(database.parent / folder, dist_dir / name / folder)
            totals["copied"] += copied
            totals["kept"] += kept
            if copied or kept:
                print(f"  {folder}: {copied} copied, {kept} unchanged")

        previews = (build_previews(conn, assets_root, dist_dir / name / "preview")
                    if assets_root and assets_root.is_dir() else {})
        page = database.parent / "browser.html"
        if page.is_file():
            stats = portable_page(page, dist_dir / name / "browser.html", previews)
            print(f"  browser.html: {stats['to_preview']} links to previews, "
                  f"{stats['dropped']} source links dropped")
        games.append({"game": name, "previews": len(previews),
                      "assets": conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]})
        conn.close()

    hub = out_dir / "hub.html"
    if hub.is_file():
        # The hub already falls back to thumbnails, so it carries no external links.
        shutil.copy2(hub, dist_dir / "hub.html")
        shutil.copy2(hub, dist_dir / "index.html")

    size = sum(path.stat().st_size for path in dist_dir.rglob("*") if path.is_file())
    manifest = {"built": time.strftime("%Y-%m-%d %H:%M:%S"), "games": games,
                "bytes": size}
    (dist_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    (dist_dir / "README.txt").write_text(
        "AssetLab catalogue - portable copy\n"
        f"built {manifest['built']}\n\n"
        "Open hub.html for all games, or <Game>/browser.html for one.\n"
        "No install, no server, no code: the pages are self-contained and read the\n"
        "image folders next to them.\n\n"
        "Personal research copy. Keep it on your own machines.\n", encoding="utf-8")
    return {**totals, "games": len(games), "megabytes": round(size / 1048576)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble a portable copy of the catalogue for other machines.")
    parser.add_argument("--out", type=Path, default=Path("out"),
                        help="the catalogue built by run.py")
    parser.add_argument("--dist", type=Path, default=Path("dist"),
                        help="folder to publish into; re-running updates it in place")
    args = parser.parse_args()

    if not args.out.is_dir():
        parser.error(f"catalogue not found: {args.out}")
    stats = publish(args.out.resolve(), args.dist.resolve())
    print(f"\n{stats['games']} games, {stats['megabytes']} MB in {args.dist}")
    print(f"{stats['copied']} files copied, {stats['kept']} already current")
    print(f"open {args.dist.resolve() / 'hub.html'}")


if __name__ == "__main__":
    main()
