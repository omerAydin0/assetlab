"""Run the whole pipeline: index -> graph -> slice -> classify -> dedup -> levels -> browser."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import animations, browser, classify, dedup, graph, index, levels
from .core import connect


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build an AssetLab research catalog from an AssetRipper export.")
    parser.add_argument("--export", required=True, type=Path,
                        help="ExportedProject/Assets from an AssetRipper 'Unity Project' export")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("--primary-content", type=Path, default=None,
                        help="optional Primary Content Assets dir, adds AssetBundle provenance")
    parser.add_argument("--title", default="AssetLab")
    parser.add_argument("--rules", type=Path, default=None,
                        help="per-game labelling rules; defaults to rules/<out name>.json")
    parser.add_argument("--skip", nargs="*", default=[],
                        help="stages to skip: index graph slice classify dedup levels browser")
    args = parser.parse_args()

    if not args.export.is_dir():
        parser.error(f"export directory not found: {args.export}")
    export = args.export.resolve()
    out = args.out.resolve()
    primary = args.primary_content.resolve() if args.primary_content else None
    rules_path = args.rules or Path("rules") / f"{args.out.name}.json"
    if rules_path.is_file():
        print(f"rules      {rules_path}")
    conn = connect(out / "assetlab.db")

    stages = [
        ("index", lambda: index.build_index(export, conn)),
        ("graph", lambda: graph.build_graph(export, conn)),
        ("slice", lambda: slice_stage(export, out, conn)),
        # levels runs before classify: the obstacle layer names in the level corpus
        # are what let classify label obstacle art from evidence.
        ("levels", lambda: levels.scan_levels(export, conn)),
        ("animations", lambda: animations.build(export, conn)),
        ("classify", lambda: classify.classify(export, primary, conn, rules_path)),
        ("dedup", lambda: dedup.deduplicate(conn)),
        ("browser", lambda: browser.build(out, export, args.title, conn)),
    ]
    for name, run in stages:
        if name in args.skip:
            print(f"[{name}] skipped")
            continue
        started = time.time()
        print(f"[{name}] ...", flush=True)
        result = run()
        print(f"[{name}] done in {time.time() - started:.1f}s -> {result}")

    if "levels" not in args.skip:
        levels.report(conn, out)
    print(f"\nopen {out / 'browser.html'}")
    print(f"report {out / 'levels_report.md'}")
    conn.close()


def slice_stage(export: Path, out: Path, conn) -> dict:
    from .slice_sprites import slice_all
    return slice_all(export, out, conn)


if __name__ == "__main__":
    main()
