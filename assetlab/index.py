"""Stage 1 - build the GUID backbone from the AssetRipper Unity Project export.

Reads every .meta file to map guid <-> path, then records one row per real asset
with image/audio measurements. The export tree is only ever read.
"""

from __future__ import annotations

import argparse
import struct
import sqlite3
import wave
from pathlib import Path
from typing import Any

from .core import (AUDIO_EXT, VISUAL_EXT, connect, dhash, image_stats, infer_type,
                   load_rgba, read_meta_guid, sha256_file)


def ogg_info(path: Path) -> dict[str, Any]:
    """Read Ogg/Vorbis stream metadata without decoding audio."""
    data = path.read_bytes()
    offset, max_granule, channels, rate = 0, 0, None, None
    while offset + 27 <= len(data):
        marker = data.find(b"OggS", offset)
        if marker < 0 or marker + 27 > len(data):
            break
        segments = data[marker + 26]
        table_end = marker + 27 + segments
        if table_end > len(data):
            break
        body_end = table_end + sum(data[marker + 27:table_end])
        if body_end > len(data):
            break
        granule = struct.unpack_from("<Q", data, marker + 6)[0]
        if granule != 0xFFFFFFFFFFFFFFFF:
            max_granule = max(max_granule, granule)
        body = data[table_end:body_end]
        sig = body.find(b"\x01vorbis")
        if sig >= 0 and sig + 16 <= len(body):
            channels = body[sig + 11]
            rate = struct.unpack_from("<I", body, sig + 12)[0]
        offset = body_end
    return {
        "duration_seconds": round(max_granule / rate, 4) if rate and max_granule else None,
        "sample_rate": rate,
        "channels": channels,
        "audio_codec": "Ogg/Vorbis",
    }


def audio_info(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".ogg":
            return ogg_info(path)
        if suffix == ".wav":
            with wave.open(str(path), "rb") as audio:
                rate, frames = audio.getframerate(), audio.getnframes()
                return {
                    "duration_seconds": round(frames / rate, 4) if rate else None,
                    "sample_rate": rate,
                    "channels": audio.getnchannels(),
                    "audio_codec": "WAV",
                }
    except (OSError, ValueError, struct.error, wave.Error):
        pass
    return {"duration_seconds": None, "sample_rate": None, "channels": None,
            "audio_codec": suffix.lstrip(".").upper()}


def build_index(assets_root: Path, conn: sqlite3.Connection) -> tuple[int, int]:
    guid_by_path: dict[Path, str] = {}
    files: list[Path] = []
    for path in assets_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".meta":
            target = path.with_suffix("")
            guid = read_meta_guid(path)
            if guid:
                guid_by_path[target] = guid
        else:
            files.append(path)

    rows, images = [], 0
    for number, path in enumerate(sorted(files), start=1):
        rel = path.relative_to(assets_root)
        ext = path.suffix.lower()
        record: dict[str, Any] = {
            "guid": guid_by_path.get(path),
            "rel_path": rel.as_posix(),
            "name": path.stem,
            "unity_type": infer_type(rel, path),
            "ext": ext.lstrip("."),
            "size_bytes": path.stat().st_size,
            "width": None, "height": None, "has_alpha": None, "alpha_ratio": None,
            "is_grayscale": None, "dominant_hex": None,
            "sha256": None, "dhash": None,
            "duration_seconds": None, "sample_rate": None, "channels": None,
            "audio_codec": None,
            "image_path": None,
        }
        try:
            record["sha256"] = sha256_file(path)
        except OSError:
            pass
        if ext in VISUAL_EXT:
            try:
                image = load_rgba(path)
                record.update(image_stats(image))
                record["dhash"] = dhash(image)
                record["image_path"] = rel.as_posix()
                images += 1
            except (OSError, ValueError):
                pass
        elif ext in AUDIO_EXT:
            record.update(audio_info(path))
        rows.append(record)
        if number % 500 == 0:
            print(f"  indexed {number}/{len(files)}", flush=True)

    # Updated in place rather than replaced. `INSERT OR REPLACE` deletes the row and
    # inserts a new one, which hands every asset a new id and orphans the tags,
    # sprites, used_by, animations and piece_groups written against the old - on
    # every re-run of this stage.
    conn.executemany(
        """INSERT INTO assets
           (guid, rel_path, name, unity_type, ext, size_bytes, width, height,
            has_alpha, alpha_ratio, is_grayscale, dominant_hex, sha256, dhash,
            duration_seconds, sample_rate, channels, audio_codec, image_path)
           VALUES (:guid, :rel_path, :name, :unity_type, :ext, :size_bytes, :width,
                   :height, :has_alpha, :alpha_ratio, :is_grayscale, :dominant_hex,
                   :sha256, :dhash, :duration_seconds, :sample_rate, :channels,
                   :audio_codec, :image_path)
           ON CONFLICT(rel_path) DO UPDATE SET
             guid=excluded.guid, name=excluded.name, unity_type=excluded.unity_type,
             ext=excluded.ext, size_bytes=excluded.size_bytes, width=excluded.width,
             height=excluded.height, has_alpha=excluded.has_alpha,
             alpha_ratio=excluded.alpha_ratio, is_grayscale=excluded.is_grayscale,
             dominant_hex=excluded.dominant_hex, sha256=excluded.sha256,
             dhash=excluded.dhash, duration_seconds=excluded.duration_seconds,
             sample_rate=excluded.sample_rate, channels=excluded.channels,
             audio_codec=excluded.audio_codec, image_path=excluded.image_path""",
        rows,
    )
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('assets_root', ?)", (str(assets_root),))
    conn.commit()
    return len(rows), images


def main() -> None:
    parser = argparse.ArgumentParser(description="Index an AssetRipper Unity Project export.")
    parser.add_argument("--export", required=True, type=Path,
                        help="ExportedProject/Assets directory (read-only)")
    parser.add_argument("--out", required=True, type=Path, help="AssetLab output directory")
    args = parser.parse_args()
    if not args.export.is_dir():
        parser.error(f"export directory not found: {args.export}")

    conn = connect(args.out / "assetlab.db")
    total, images = build_index(args.export.resolve(), conn)
    with_guid = conn.execute("SELECT COUNT(*) FROM assets WHERE guid IS NOT NULL").fetchone()[0]
    print(f"indexed {total} assets ({images} images, {with_guid} with GUID)")
    for row in conn.execute(
        "SELECT unity_type, COUNT(*) n FROM assets GROUP BY unity_type ORDER BY n DESC LIMIT 12"
    ):
        print(f"  {row['unity_type']:<24} {row['n']}")
    conn.close()


if __name__ == "__main__":
    main()
