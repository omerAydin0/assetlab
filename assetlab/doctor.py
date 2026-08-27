"""Preflight check for a new export, before spending minutes on a full run.

The expensive mistake is exporting the wrong way: a Primary Content export has no
.meta files, so there are no GUIDs, no reference graph and no sprite slicing. This
catches that in seconds and says what to change.

Run: python -m assetlab.doctor --export <ExportedProject/Assets> [--primary-content <dir>]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .ingest.detect import serialized_file_version
from .levels import looks_like_tiled
from .slice_sprites import parse_sprite

SPRITE_SAMPLE = 60


def check_staging(staging: Path) -> int:
    """Diagnose an ingestion staging tree, i.e. the input handed to AssetRipper."""
    print(f"--- staging: {staging}")
    problems = 0
    manifest_path = staging / "manifest.json"
    root = staging / "input" if (staging / "input").is_dir() else staging

    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        included = [p for p in manifest.get("packages", []) if p["included"]]
        print(f"OK    manifest lists {len(included)} staged package(s), "
              f"ABI {manifest.get('chosen_abi')}")
        for warning in manifest.get("warnings", []):
            print(f"      WARN {warning}")
        for error in manifest.get("errors", []):
            print(f"FAIL  {error}")
            problems += 1
        if manifest.get("unknown_binary_total"):
            print(f"      WARN {manifest['unknown_binary_total']} unrecognised files under "
                  f"assets/ were staged anyway (see manifest 'unknown_binaries')")
    else:
        print("      WARN no manifest.json; checking the tree only")

    data_dirs = [p for p in root.rglob("Data") if p.is_dir() and p.parent.name == "bin"]
    if not data_dirs:
        print("FAIL  no assets/bin/Data directory. AssetRipper needs the Unity player data.")
        problems += 1
    for data_dir in data_dirs:
        payload = [p for p in data_dir.iterdir() if p.is_file()]
        # Games that ship loose player data fill this folder with thousands of
        # hash-named files, so name the recognisable ones and count the rest.
        notable = sorted(p.name for p in payload
                         if p.suffix.lower() in {".unity3d", ".resource", ".assets"}
                         or p.name.lower().startswith(("globalgamemanagers", "level",
                                                       "resources", "sharedassets",
                                                       "unity default", "boot.config")))
        shown = ", ".join(notable[:5]) if notable else "no recognisable payload names"
        extra = len(payload) - len(notable[:5])
        print(f"OK    {data_dir.relative_to(root).as_posix()} -> {shown}"
              + (f" (+{extra} more files)" if extra > 0 else ""))

    metadata = list(root.rglob("global-metadata.dat"))
    print(("OK    " if metadata else "      WARN no ") +
          f"global-metadata.dat{' found' if metadata else ' (IL2CPP script types unresolved)'}")

    libs = sorted({p.parent.name for p in root.rglob("lib*.so")})
    if len(libs) > 1:
        print(f"FAIL  native libraries for several ABIs staged ({libs}); "
              f"AssetRipper must see exactly one.")
        problems += 1
    elif libs:
        il2cpp = list(root.rglob("libil2cpp.so"))
        print(f"OK    native libraries for {libs[0]}"
              f"{' incl. libil2cpp.so' if il2cpp else ' (no libil2cpp.so)'}")
    else:
        print("      WARN no native libraries staged")

    # A build ships either UnityFS bundles or loose SerializedFiles; counting only
    # the former makes a perfectly good SerializedFile game look empty.
    bundles = serialized = 0
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_size > 64:
            try:
                head = path.open("rb").read(16)
            except OSError:
                continue
            if head.startswith(b"UnityFS"):
                bundles += 1
            elif serialized_file_version(head) is not None:
                serialized += 1
        if bundles + serialized > 600:
            break
    total = bundles + serialized
    parts = []
    if bundles:
        parts.append(f"{bundles} UnityFS bundle(s)")
    if serialized:
        parts.append(f"{serialized} loose SerializedFile(s)")
    if parts:
        print(f"OK    {' + '.join(parts)}{'+' if total > 600 else ''} in the staged tree")
    else:
        print("FAIL  no Unity content found in the staged tree")
        problems += 1

    print()
    print("staging looks usable" if not problems else "fix the FAIL items")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check an ingestion staging tree and/or an AssetRipper export.")
    parser.add_argument("--export", type=Path, default=None,
                        help="ExportedProject/Assets from a Unity Project export")
    parser.add_argument("--staging", type=Path, default=None,
                        help="an assetlab.ingest output directory (pre-AssetRipper)")
    parser.add_argument("--primary-content", type=Path, default=None)
    args = parser.parse_args()

    if not args.export and not args.staging:
        parser.error("pass --staging and/or --export")

    staging_problems = 0
    if args.staging:
        if not args.staging.is_dir():
            print(f"FAIL  staging directory not found: {args.staging}")
            raise SystemExit(1)
        staging_problems = check_staging(args.staging)
        if args.export:
            print()
    if not args.export:
        raise SystemExit(1 if staging_problems else 0)
    print(f"--- export: {args.export}")

    root = args.export
    if not root.is_dir():
        print(f"FAIL  export directory not found: {root}")
        raise SystemExit(1)
    if root.name.lower() != "assets":
        print(f"WARN  expected the export's 'Assets' folder, got '{root.name}'. "
              f"Point --export at ExportedProject/Assets.")

    counts: Counter[str] = Counter()
    sprite_assets: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        counts[suffix] += 1
        if suffix == ".asset":
            sprite_assets.append(path)
    # A large export holds thousands of non-sprite .asset files (MonoBehaviour and
    # friends) that sort ahead of Sprite/, so look there first instead of sampling
    # whatever the walk happened to reach.
    sprite_assets.sort(key=lambda p: 0 if "sprite" in
                       {part.lower() for part in p.parts[:-1]} else 1)

    problems, notes = [], []

    metas = counts[".meta"]
    if metas == 0:
        problems.append(
            "No .meta files. This is a Primary Content export, which has no GUIDs.\n"
            "      Re-export from AssetRipper as **Unity Project** and point --export at\n"
            "      <output>/ExportedProject/Assets.")
    else:
        notes.append(f"{metas} .meta files -> GUID graph available")

    prefabs, scenes = counts[".prefab"], counts[".unity"]
    if prefabs == 0 and scenes == 0:
        problems.append("No .prefab or .unity files. Role detection and 'used by' need them.")
    else:
        notes.append(f"{prefabs} prefabs, {scenes} scenes -> roles and usage links available")

    images = sum(counts[ext] for ext in (".png", ".jpg", ".jpeg", ".tga"))
    notes.append(f"{images} images")

    # Are sprites exported as YAML (sliceable) or already flattened?
    checked = rotated = with_rect = with_atlas = 0
    for path in sprite_assets:
        if checked >= SPRITE_SAMPLE:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "m_RD:" not in text or "--- !u!213" not in text[:200]:
            continue
        checked += 1
        _, guid, rect, rotation = parse_sprite(text)
        if rect:
            with_rect += 1
        if guid:
            with_atlas += 1
        if rotation:
            rotated += 1

    if checked == 0:
        problems.append(
            "No sprite YAML found. Set **SpriteExportMode=Yaml** in AssetRipper and\n"
            "      re-export, otherwise sprites cannot be cut out of their atlases and\n"
            "      you will only browse atlas sheets.")
    elif with_rect < checked:
        notes.append(f"WARN {checked - with_rect}/{checked} sampled sprites have no crop rect")
    elif with_atlas < checked:
        # `texture: {fileID: 0}` means the atlas is genuinely absent from the build,
        # typically content the game streams after install. Nothing to slice.
        notes.append(f"WARN {checked - with_atlas}/{checked} sampled sprites reference no "
                     f"atlas texture (content not shipped in the package); the rest slice fine")
    else:
        notes.append(f"{checked}/{checked} sampled sprites carry an atlas rect -> slicing works")
    if rotated:
        notes.append(f"WARN {rotated}/{checked} sampled sprites are packed rotated. "
                     f"Slicing un-rotates them, but eyeball one afterwards - "
                     f"the 90-degree direction is untested against real data.")

    if args.primary_content:
        bundle_dir = args.primary_content / "AssetBundle"
        bundles = len(list(bundle_dir.glob("*.bundle.json"))) if bundle_dir.is_dir() else 0
        if bundles:
            notes.append(f"{bundles} AssetBundle manifests -> Addressables provenance available")
        else:
            notes.append("WARN no AssetBundle/*.bundle.json in --primary-content "
                         "(optional; only adds bundle labels)")

    tiled = 0
    for path in root.rglob("*"):
        if path.suffix.lower() in {".json", ".bytes"} and looks_like_tiled(path):
            tiled += 1
            if tiled > 3:
                break
    notes.append(f"Tiled-style level files: {'found' if tiled else 'none (stage 6 will be empty)'}")

    for note in notes:
        print(("      " if note.startswith("WARN") else "OK    ") + note)
    for problem in problems:
        print("FAIL  " + problem)
    print()
    print("ready to run" if not problems else "fix the FAIL items and re-export")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
