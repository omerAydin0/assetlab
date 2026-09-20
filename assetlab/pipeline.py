"""One command from an Android package to a catalogue, with a gate at each seam.

    package -> discovery -> staging -> [gate] -> AssetRipper -> [gate] -> analysis

The seams are where this has failed quietly before. A build was once exported
without its IL2CPP metadata and nothing objected: the export succeeded, the stages
ran, and the catalogue came out with two mechanics where a comparable title had a
hundred and ninety. Nothing in the chain had been asked to compare what came out
against what went in. The gates here exist to ask exactly that, and a stage that a
human has to remember to run is treated as a stage that does not exist.

Each step is resumable. Staging and exporting are the expensive parts, so both are
reused when their output is already present and consistent, and `--restage` is the
way to say otherwise.

Nothing here knows what game it is looking at. Every decision comes from file
signatures and structure, recorded with its evidence in discovery.json and
staging.json so a surprising result can be traced rather than guessed at.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from . import hub, ripper
from .core import connect
from .doctor import (BLOCKED, LEVELS_ABSENT, LEVELS_PARSED, LEVELS_UNREADABLE, PARTIAL,
                     READY, Diagnosis, diagnose_export, diagnose_outcome,
                     diagnose_staging, level_verdict, staged_tree)
from .ingest.stage import ingest
from .run import analyse

#: Written into an export directory once AssetRipper has finished with it.
#: Nothing reuses an export that does not carry it.
EXPORT_DONE = ".assetlab-export-complete"


def export_has_content(export_dir: Path) -> bool:
    """Whether anything was written into an export tree."""
    assets = export_dir / "ExportedProject" / "Assets"
    return assets.is_dir() and any(assets.iterdir())


def export_is_reusable(export_dir: Path) -> bool:
    """Whether an existing export may be used instead of exporting again.

    Presence alone was the old test and it is not enough: an export that stopped
    part way leaves a tree that looks finished and is missing whatever had not been
    written. Every stage downstream then reports confident numbers about half a
    build, which is the failure this pipeline exists to make impossible.
    """
    return export_has_content(export_dir) and (export_dir / EXPORT_DONE).is_file()


@dataclass
class Step:
    """One stage of the chain, and what it cost."""
    name: str
    status: str                       # ok / reused / skipped / failed
    detail: str = ""
    seconds: float = 0.0
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "seconds": round(self.seconds, 1), **({"data": self.data} if self.data else {})}


def split_reports(manifest: dict, out: Path) -> None:
    """Write discovery.json and staging.json.

    One file answers "what is in this package" and the other "what did we hand to
    AssetRipper, and why". Keeping them apart matters because the first is a
    finding about the build and the second is a decision about our own run - and
    when a result looks wrong, knowing which of the two to distrust is most of the
    work.
    """
    discovery = {
        "created_utc": manifest.get("created_utc"),
        "input": manifest.get("input"),
        "packaging": manifest.get("packaging"),
        "unity": manifest.get("unity"),
        "available_abis": manifest.get("available_abis"),
        "packages": [{k: package[k] for k in
                      ("name", "path", "size", "source", "abis", "roles")}
                     for package in manifest.get("packages", [])],
        "role_counts": manifest.get("role_counts"),
        "unity_data_roots": manifest.get("unity_data_roots"),
        "content_roots": manifest.get("content_roots"),
        "il2cpp": manifest.get("il2cpp"),
        "resource_streams": manifest.get("resource_streams"),
        "nested_archives": manifest.get("nested_archives"),
        "unknown_binaries": manifest.get("unknown_binaries"),
        "unknown_binary_total": manifest.get("unknown_binary_total"),
    }
    staging = {
        "created_utc": manifest.get("created_utc"),
        "staged_root": manifest.get("staged_root"),
        "chosen_abi": manifest.get("chosen_abi"),
        "staged_count": manifest.get("staged_count"),
        "decisions": [{k: package[k] for k in ("name", "source", "included", "reason")}
                      for package in manifest.get("packages", [])],
        "files": manifest.get("staged_files"),
        "warnings": manifest.get("warnings"),
        "errors": manifest.get("errors"),
    }
    (out / "discovery.json").write_text(
        json.dumps(discovery, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "staging.json").write_text(
        json.dumps(staging, indent=2, ensure_ascii=False), encoding="utf-8")


def capabilities(staging: Diagnosis, export: Diagnosis | None) -> dict[str, str]:
    """What this run can and cannot offer, in the reader's terms rather than ours."""
    def said(report: Diagnosis | None, needle: str) -> str | None:
        if not report:
            return None
        for check in report.checks:
            if needle in check.message:
                return check.status
        return None

    levels_text = {LEVELS_PARSED: "parsed",
                   LEVELS_UNREADABLE: "detected, format unsupported",
                   LEVELS_ABSENT: "none found",
                   None: "unknown"}[level_verdict(export)]
    return {
        "unity assets": "available" if export and not export.blockers else "unavailable",
        "asset graph": "available" if said(export, ".meta files") == "ok" else "unavailable",
        "sprite slicing": "available" if said(export, "sampled sprites") == "ok"
                          else "degraded" if said(export, "sampled sprites") == "warn"
                          else "unavailable",
        "il2cpp metadata": "available" if said(staging, "global-metadata.dat staged") == "ok"
                           else "unavailable",
        "script vocabulary": "available" if said(export, ".cs files ->") == "ok"
                             else "unavailable",
        "level parser": levels_text,
    }


PACKAGE_SUFFIXES = (".apk", ".apkm", ".xapk", ".apks", ".zip")


def fingerprint(input_path: Path, exe: Path | None) -> dict:
    """SHA-256 of every package read and of the AssetRipper that read them.

    A path and a size say which file was probably used; a hash says which one was.
    """
    from .core import sha256_file
    from .ripper import find_exe
    source = Path(input_path)
    files = ([source] if source.is_file() else
             sorted(p for p in source.rglob("*") if p.suffix.lower() in PACKAGE_SUFFIXES))
    packages = {}
    for path in files:
        try:
            packages[path.name] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        except OSError:
            packages[path.name] = None
    ripper = find_exe(exe)
    tool = None
    if ripper and ripper.is_file():
        tool = {"path": str(ripper), "bytes": ripper.stat().st_size,
                "sha256": sha256_file(ripper)}
    return {"packages": packages, "assetripper": tool}


def run_pipeline(input_path: Path, out: Path, title: str,
                 staging: Path | None = None, export_dir: Path | None = None,
                 exe: Path | None = None, port: int = 5599,
                 primary: Path | None = None, rules_path: Path | None = None,
                 skip: tuple[str, ...] = (), restage: bool = False,
                 build_hub: bool = True, primary_export: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    staging = (staging or Path("staging") / out.name).resolve()
    export_dir = (export_dir or Path("exports") / out.name).resolve()
    steps: list[Step] = []
    started_all = time.time()

    # ---------------------------------------------------------- 1. discovery
    started = time.time()
    manifest_path = staging / "manifest.json"
    reusable = not restage and manifest_path.is_file()
    if reusable:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # A manifest from an older build of this tool lacks the fields the gates
        # read, and reusing it would report absences that are ours, not the game's.
        reusable = (bool(manifest.get("staged_count")) and "packaging" in manifest
                    and staged_tree(staging, manifest) is not None)
    if reusable:
        steps.append(Step("discovery", "reused",
                          f"{manifest['staged_count']} files already staged in {staging}",
                          time.time() - started))
    else:
        print(f"[discovery] reading {input_path}", flush=True)
        manifest = ingest(input_path, staging)
        detail = (f"{manifest.get('staged_count', 0)} files staged from "
                  f"{len([p for p in manifest['packages'] if p['included']])} package(s)")
        steps.append(Step("discovery", "failed" if manifest.get("errors") else "ok",
                          "; ".join(manifest["errors"]) if manifest.get("errors") else detail,
                          time.time() - started))
    split_reports(manifest, out)
    # What the package's Addressables catalogue declares against what it ships. Read
    # from the staged files only: a bundle behind an address is named, never fetched.
    from .addressables import describe, write_report
    try:
        declared = write_report(staged_tree(staging, manifest) or staging, out)
    except OSError as problem:
        declared = None
        print(f"[discovery] Addressables catalogue not read: {problem}")
    if declared:
        print(f"[discovery] Addressables: {describe(declared)}")

    unity = manifest.get("unity") or {}
    shape = manifest.get("packaging") or {}
    print(f"[discovery] {shape.get('shape', '?')}, "
          f"Unity {unity.get('version') or 'unknown'}, ABI {manifest.get('chosen_abi')}")

    # ------------------------------------------------------- 2. staging gate
    started = time.time()
    staging_report = diagnose_staging(staging)
    steps.append(Step("staging gate", "failed" if staging_report.blockers else "ok",
                      staging_report.status, time.time() - started))
    print(staging_report.render())
    if staging_report.blockers:
        return finish(out, title, input_path, manifest, steps, staging_report, None,
                      {}, started_all, BLOCKED)

    # -------------------------------------------------------- 3. assetripper
    started = time.time()
    exported = export_dir / "ExportedProject" / "Assets"
    # A present directory is not a finished export. An export that was interrupted -
    # the machine slept, the session ended, AssetRipper was killed - leaves a tree
    # that looks complete and is missing whatever had not been written yet. Every
    # stage downstream then reports confident numbers about half a build. The marker
    # is written last, so its absence is the one honest signal that it stopped early.
    export_project = restage or not export_is_reusable(export_dir)
    if not export_project:
        steps.append(Step("assetripper", "reused", f"export already at {exported}",
                          time.time() - started))
    elif export_has_content(export_dir):
        print(f"WARN  {exported} exists but no {EXPORT_DONE}: the last export did not "
              f"finish, so it is being redone", flush=True)
    # The opt-in second pass: a Primary Content export of the same load, read only for
    # the bundle records whose `m_Container` gives classification its provenance. A
    # path passed as --primary-content is used as it is instead.
    primary_dir = export_dir.with_name(f"{export_dir.name}_primary")
    export_primary = False
    if primary_export and primary is None:
        if not restage and (primary_dir / "Assets").is_dir():
            primary = primary_dir / "Assets"
            steps.append(Step("primary export", "reused", f"export already at {primary}"))
        else:
            export_primary = True
    if export_project or export_primary:
        found_exe = ripper.find_exe(exe)
        hint = ("" if found_exe else
                f" No AssetRipper executable was found; set {ripper.EXE_ENV} or pass "
                f"--exe, or start one yourself on port {port}.")
        stage = "assetripper" if export_project else "primary export"
        try:
            with ripper.loaded(staged_tree(staging, manifest), found_exe, port) as loaded:
                if export_project:
                    print(f"exporting  {export_dir}", flush=True)
                    exported = loaded.export_unity_project(export_dir)
                    (export_dir / EXPORT_DONE).write_text(
                        datetime.now(timezone.utc).isoformat(), encoding="utf-8")
                    steps.append(Step("assetripper", "ok", str(exported),
                                      time.time() - started))
                    stage, started = "primary export", time.time()
                if export_primary:
                    print(f"exporting  {primary_dir} (primary content)", flush=True)
                    primary = loaded.export_primary_content(primary_dir)
                    steps.append(Step("primary export", "ok", str(primary),
                                      time.time() - started))
        except ripper.RipperError as error:
            steps.append(Step(stage, "failed", f"{error}{hint}", time.time() - started))
            if stage == "assetripper":
                print(f"FAIL  AssetRipper: {error}{hint}")
                return finish(out, title, input_path, manifest, steps, staging_report,
                              None, {}, started_all, BLOCKED)
            # Provenance adds to a catalogue; it is not a condition of one.
            print(f"WARN  primary content export: {error}{hint} "
                  f"- bundle provenance is off for this run")

    # -------------------------------------------------------- 4. export gate
    started = time.time()
    export_report = diagnose_export(exported, primary, manifest)
    steps.append(Step("export gate", "failed" if export_report.blockers else "ok",
                      export_report.status, time.time() - started))
    print(export_report.render())
    if export_report.blockers:
        # Nothing downstream can work without .meta files or sprite YAML, and
        # running the stages anyway would fill a catalogue with confident gaps.
        return finish(out, title, input_path, manifest, steps, staging_report,
                      export_report, {}, started_all, BLOCKED)

    # ----------------------------------------------------------- 5. analysis
    started = time.time()
    conn = connect(out / "assetlab.db")
    provenance = {
        "input": str(input_path), "title": title,
        "packaging": shape, "unity": unity,
        "chosen_abi": manifest.get("chosen_abi"),
        "available_abis": manifest.get("available_abis"),
        "packages": [p["name"] for p in manifest["packages"] if p["included"]],
        "staged_root": manifest.get("staged_root"),
        "export": str(exported),
        "primary_content": str(primary) if primary else None,
        "il2cpp_metadata": bool((manifest.get("il2cpp") or {}).get("metadata")),
        "primary_export": bool(primary_export),
        "fingerprint": fingerprint(input_path, exe),
    }
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('provenance', ?)",
                 (json.dumps(provenance, ensure_ascii=False),))
    conn.commit()
    if rules_path and rules_path.is_file():
        print(f"rules      {rules_path} (optional overrides)")
    results = analyse(exported, out, title, primary, rules_path, skip, conn)
    conn.close()
    steps.append(Step("analysis", "ok", f"{len(results)} stages", time.time() - started))

    # The last gate reads what the run produced rather than what went into it. It
    # never blocks: the catalogue already exists by now, and withholding it would
    # remove the evidence a reader needs to judge the finding.
    started = time.time()
    outcome_report = diagnose_outcome(out)
    steps.append(Step("outcome gate", "ok", outcome_report.status, time.time() - started))
    print(outcome_report.render())

    # The combined hub indexes every catalogue beside this one, so a new game has
    # to reach it or the page a reader opens keeps showing yesterday's set.
    if build_hub:
        started = time.time()
        gallery = out.parent
        names = sorted(entry.name for entry in gallery.iterdir()
                       if (entry / "assetlab.db").is_file())
        try:
            stats = hub.build(gallery.resolve(), names)
            steps.append(Step("hub", "ok", f"{stats['games']} games in {stats['path']}",
                              time.time() - started))
        except Exception as error:                      # noqa: BLE001 - reported, not raised
            # A finished catalogue must not be lost to a failure in the index of it.
            steps.append(Step("hub", "failed", f"{type(error).__name__}: {error}",
                              time.time() - started))
            print(f"WARN  hub not rebuilt: {error}")

    reports = (staging_report, export_report, outcome_report)
    status = PARTIAL if any(r.warnings or r.gaps for r in reports) else READY
    return finish(out, title, input_path, manifest, steps, staging_report,
                  export_report, results, started_all, status, outcome_report)


def finish(out: Path, title: str, input_path: Path, manifest: dict, steps: list[Step],
           staging_report: Diagnosis, export_report: Diagnosis | None,
           results: dict, started_all: float, status: str,
           outcome_report: Diagnosis | None = None) -> dict:
    """Write diagnostics.json and print the verdict, whether or not we got there."""
    able = capabilities(staging_report, export_report)
    report = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "input": str(input_path), "out": str(out), "title": title,
        "status": status,
        "seconds": round(time.time() - started_all, 1),
        "packaging": manifest.get("packaging"),
        "unity": manifest.get("unity"),
        "chosen_abi": manifest.get("chosen_abi"),
        "capabilities": able,
        "steps": [step.to_dict() for step in steps],
        "gates": {"staging": staging_report.to_dict(),
                  "export": export_report.to_dict() if export_report else None,
                  "outcome": outcome_report.to_dict() if outcome_report else None},
        "analysis": results,
    }
    (out / "diagnostics.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print()
    print(f"OVERALL  {status}")
    width = max(len(name) for name in able)
    for name, state in able.items():
        print(f"  {name:<{width}}  {state}")
    print()
    for step in steps:
        print(f"  {step.status:<8} {step.name:<14} {step.seconds:6.1f}s  {step.detail}")
    print()
    if status == BLOCKED:
        print("REASON")
        for check in (export_report or staging_report).blockers:
            print(f"  {check.message}")
            if check.remedy:
                print(f"    -> {check.remedy}")
    else:
        # Gaps are failures the run survived. They belong in the verdict, not
        # buried in the scroll-back above it.
        gaps = (staging_report.gaps + (export_report.gaps if export_report else [])
                + (outcome_report.gaps if outcome_report else []))
        if gaps:
            print("GAPS")
            for check in gaps:
                print(f"  {check.message}")
                if check.remedy:
                    print(f"    -> {check.remedy}")
            print()
        print(f"open   {out / 'browser.html'}")
    print(f"detail {out / 'diagnostics.json'}")
    return report
