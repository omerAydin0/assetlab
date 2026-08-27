"""CLI: python -m assetlab.ingest --input <apk|apkm|dir> --out <staging>"""

from __future__ import annotations

import argparse
from pathlib import Path

from .stage import ingest, print_summary


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m assetlab.ingest",
        description="Stage an Android Unity build into an AssetRipper input tree.")
    parser.add_argument("--input", required=True, type=Path,
                        help="an .apk, an .apkm/.apks/.xapk/.zip, or a directory of them")
    parser.add_argument("--out", required=True, type=Path, help="staging directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="detect and write manifest.json without extracting")
    args = parser.parse_args()

    manifest = ingest(args.input, args.out, dry_run=args.dry_run)
    print_summary(manifest)
    print(f"\nmanifest {args.out.resolve() / 'manifest.json'}")
    if not args.dry_run and manifest.get("staged_count"):
        print(f"\nNext: run AssetRipper on {manifest['staged_root']}")
        print("  export as Unity Project with SpriteExportMode=Yaml, "
              "BundledAssetsExportMode=DirectExport,")
        print("  ImageExportFormat=Png, AudioExportFormat=Default")
    raise SystemExit(1 if manifest.get("errors") else 0)


if __name__ == "__main__":
    main()
