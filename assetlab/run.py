"""Run the analysis stages, adapting to what kind of build the export turns out to be.

The stage order is not arbitrary. `profile` runs early because the stages after it
ask whether their work applies at all: a build whose art is geometry has no atlas
to cut and no sprite layers to compose, and `models` exists to cover it instead.
`levels` runs before `classify` because the obstacle names it finds are what let
classification label obstacle art from evidence.

Two ways in. `--export` analyses an AssetRipper export that already exists.
`--input` takes the Android package itself and runs the whole chain - discovery,
staging, AssetRipper, then these stages - which is the same work with nothing left
for a human to remember.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from . import (animations, browser, classify, dedup, graph, index, levels, models,
               prefabs, profile, skin, spine)
from .core import connect, run_record

STAGE_NAMES = ("profile", "index", "graph", "slice", "spine", "models", "skin", "levels",
               "animations", "prefabs", "classify", "dedup", "browser")


def analyse(export: Path, out: Path, title: str = "AssetLab",
            primary: Path | None = None, rules_path: Path | None = None,
            skip: tuple[str, ...] = (), conn=None) -> dict:
    """Run every analysis stage over one export. Returns each stage's result.

    The connection is optional so a caller that has already written provenance into
    the catalogue can hand the same one over rather than reopening it.
    """
    close_after = conn is None
    conn = conn or connect(out / "assetlab.db")
    # Recorded on every run, not only by the index stage. A catalogue rebuilt from a
    # later stage kept the export path its index stage once saw, and after the export
    # folders were renamed five catalogues pointed at directories that no longer existed.
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('assets_root', ?)", (str(export),))
    import json as _json
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('run', ?)", (_json.dumps(
        {**run_record(), "export": str(export),
         "primary_content": str(primary) if primary else None,
         "skipped": sorted(skip)}, ensure_ascii=False),))
    conn.commit()
    detected: dict = {}
    results: dict = {}

    def profile_stage() -> dict:
        detected.update(profile.detect(export, conn))
        print(profile.describe(detected))
        return {"verdict": detected["verdict"], "score": detected["score"]}

    def models_stage() -> dict:
        # A 2D build has no mesh chain worth walking; saying so beats a row of zeros.
        if detected.get("verdict") == "2d":
            return {"skipped": "2D build, no mesh art to map"}
        return models.build(export, conn, out)

    def slice_stage() -> dict:
        from .slice_sprites import slice_all
        return slice_all(export, out, conn)

    stages = [
        # First, so the stages after it know whether they apply.
        ("profile", profile_stage),
        ("index", lambda: index.build_index(export, conn)),
        ("graph", lambda: graph.build_graph(export, conn)),
        ("slice", slice_stage),
        # After slice, so a Spine region can point at a page the catalogue already
        # holds; before classify, so the art it recovers is labelled with the rest.
        ("spine", lambda: spine.build(export, out, conn)),
        ("models", models_stage),
        # Rigged clips have no sprite to show, so they are posed and drawn. Runs
        # after models so the mesh and material caches are already warm.
        ("skin", lambda: skin.build(export, conn, out)),
        # levels runs before classify: the obstacle layer names in the level corpus
        # are what let classify label obstacle art from evidence.
        ("levels", lambda: levels.scan_levels(export, conn)),
        ("animations", lambda: animations.build(export, conn)),
        # Each prefab drawn as the build assembles it: an object's final form.
        ("prefabs", lambda: prefabs.build(export, out, conn)),
        ("classify", lambda: classify.classify(export, primary, conn, rules_path)),
        ("dedup", lambda: dedup.deduplicate(conn)),
        ("browser", lambda: browser.build(out, export, title, conn)),
    ]
    for name, run_stage in stages:
        if name in skip:
            print(f"[{name}] skipped")
            results[name] = {"skipped": "asked to skip"}
            continue
        started = time.time()
        print(f"[{name}] ...", flush=True)
        result = run_stage()
        elapsed = time.time() - started
        print(f"[{name}] done in {elapsed:.1f}s -> {result}")
        results[name] = {"result": result, "seconds": round(elapsed, 1)}

    if "levels" not in skip:
        # A report, not a stage: whatever goes wrong in it must not take the run's own
        # bookkeeping down with it.
        try:
            levels.report(conn, out)
        except (ValueError, KeyError, sqlite3.Error) as problem:
            print(f"[levels] report not written: {problem}")
    if close_after:
        conn.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build an AssetLab research catalog from a package or an export.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, default=None,
                        help="an .apk/.xapk/.apks/.apkm or a directory of them; runs "
                             "discovery, staging and AssetRipper before analysing")
    source.add_argument("--export", type=Path, default=None,
                        help="ExportedProject/Assets from an AssetRipper Unity Project export")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("--primary-content", type=Path, default=None,
                        help="optional Primary Content Assets dir, adds AssetBundle provenance")
    parser.add_argument("--title", default=None,
                        help="display name for the catalogue; defaults to the out folder")
    parser.add_argument("--rules", type=Path, default=None,
                        help="optional labelling overrides; defaults to rules/<out name>.json "
                             "if that file happens to exist. Nothing requires it.")
    parser.add_argument("--skip", nargs="*", default=[],
                        help="stages to skip: " + " ".join(STAGE_NAMES))
    # Only meaningful with --input.
    parser.add_argument("--staging", type=Path, default=None,
                        help="where to stage the package (default: staging/<out name>)")
    parser.add_argument("--export-dir", type=Path, default=None,
                        help="where AssetRipper writes (default: exports/<out name>)")
    parser.add_argument("--exe", type=Path, default=None,
                        help="AssetRipper.GUI.Free.exe; ignored if one already runs")
    parser.add_argument("--port", type=int, default=5599)
    parser.add_argument("--no-hub", action="store_true",
                        help="skip rebuilding the combined hub page at the end")
    parser.add_argument("--restage", action="store_true",
                        help="re-stage and re-export even if previous output is present")
    parser.add_argument("--primary-export", action="store_true",
                        help="also have AssetRipper write a Primary Content export to "
                             "<export dir>_primary and read its bundle records for "
                             "provenance; a second export of the whole build, so off "
                             "unless asked for. --primary-content wins if both are given")
    args = parser.parse_args()

    out = args.out.resolve()
    title = args.title or args.out.name
    rules_path = args.rules or Path("rules") / f"{args.out.name}.json"
    primary = args.primary_content.resolve() if args.primary_content else None

    if args.primary_export and not args.input:
        parser.error("--primary-export needs --input; an existing export takes "
                     "--primary-content instead")
    if args.input:
        from .pipeline import run_pipeline
        report = run_pipeline(
            args.input.resolve(), out, title,
            staging=args.staging.resolve() if args.staging else None,
            export_dir=args.export_dir.resolve() if args.export_dir else None,
            exe=args.exe, port=args.port, primary=primary,
            rules_path=rules_path, skip=tuple(args.skip), restage=args.restage,
            build_hub=not args.no_hub, primary_export=args.primary_export)
        raise SystemExit(0 if report["status"] != "BLOCKED" else 1)

    if not args.export.is_dir():
        parser.error(f"export directory not found: {args.export}")
    if rules_path.is_file():
        print(f"rules      {rules_path}")
    analyse(args.export.resolve(), out, title, primary, rules_path, tuple(args.skip))
    print(f"\nopen {out / 'browser.html'}")
    print(f"report {out / 'levels_report.md'}")


if __name__ == "__main__":
    main()
