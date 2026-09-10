"""Diagnose a staging tree and an AssetRipper export, as structured results.

The expensive mistake is exporting the wrong way: a Primary Content export has no
.meta files, so there are no GUIDs, no reference graph and no sprite slicing. The
quiet mistake is worse - an export that succeeds while missing something it was
handed the inputs for, which is how a build with IL2CPP metadata sitting in its
package ends up with no Scripts/ folder and a catalogue that merely looks sparse.

Every check returns a verdict rather than printing one, so the same code answers
both `python -m assetlab.doctor` and the gates inside the pipeline. A diagnosis
carries the questions worth asking of any build: what did we detect, what is
missing, why does it matter, and can the run continue anyway.

Run: python -m assetlab.doctor --staging <dir> --export <ExportedProject/Assets>
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import sqlite3
import statistics

from .ingest.detect import serialized_file_version
from .levels import (DATA_SUFFIXES, SCANNED_SUFFIXES, corpus_candidates,
                     flat_corpus_fits, looks_like_tiled)
from .slice_sprites import parse_sprite

SPRITE_SAMPLE = 60
#: A build that resolved its script types emits `.cs` files in proportion to its
#: size: across the builds measured here, between 14% and 44% of the `.meta` count.
#: A build whose metadata was staged but silently ignored emitted one file against
#: 31,000 - 0.003%. This cut sits an order of magnitude below the healthy floor.
SCRIPT_RATIO_FLOOR = 0.01
#: Below this many assets the ratio is noise, and absence is judged on zero alone.
SCRIPT_RATIO_MIN_ASSETS = 500

#: A build's mechanic vocabulary against the feature vocabulary from the same run.
#: Measured over seven catalogues: six sound builds fall between 0.134 and 0.463,
#: and the one whose script types never resolved sits at 0.007. The cut is set
#: below the healthy floor by a factor of two and above the failure by seven.
MECHANIC_VOCABULARY_FLOOR = 0.05
#: How many of a build's Sprite assets should end up with a rect on a page. Where the
#: sprite carries its own texture this is near enough all of them - four builds land
#: between 0.90 and 1.00. A build that packs with Unity's SpriteAtlas keeps the packed
#: position in the atlas asset instead, and where the export omits that asset the
#: sprites cannot be placed at all: one catalogue placed 1,446 of 19,037.
SPRITE_PLACEMENT_FLOOR = 0.5
SPRITE_PLACEMENT_MIN = 200
#: Vocabularies smaller than this are too small for a ratio to mean anything.
MECHANIC_MIN_FEATURES = 60
#: How many files may be opened while looking for a Tiled corpus. Candidate groups
#: are probed first, so a real corpus is found in the first handful; the rest of the
#: budget covers a corpus whose file names carry no index to group on.
TILED_PROBE_LIMIT = 3000

OK, WARN, FAIL = "ok", "warn", "fail"

#: What a diagnosis adds up to. BLOCKED means the next stage cannot run at all;
#: PARTIAL means it can, with named gaps; READY means nothing is missing.
READY, PARTIAL, BLOCKED = "READY", "PARTIAL", "BLOCKED"

#: The three answers about a level corpus. They are constants because
#: "DETECTED BUT UNSUPPORTED" contains "SUPPORTED": anything testing for one of
#: these by substring quietly answers the other.
LEVELS_PARSED = "SUPPORTED"
LEVELS_UNREADABLE = "DETECTED BUT UNSUPPORTED"
LEVELS_ABSENT = "NOT DETECTED"


def level_verdict(report: "Diagnosis | None") -> str | None:
    """Which of the three answers a diagnosis gave about levels."""
    for check in (report.checks if report else []):
        if not check.message.startswith("level corpus:"):
            continue
        for verdict in (LEVELS_UNREADABLE, LEVELS_ABSENT, LEVELS_PARSED):
            if verdict in check.message:
                return verdict
    return None


@dataclass
class Check:
    """One question asked of a build, and what the answer means."""
    status: str
    message: str
    remedy: str | None = None
    #: Whether a failure here stops the run. Only meaningful when status is FAIL:
    #: a build can be badly wrong in ways that still leave most of it catalogueable.
    blocking: bool = True

    def render(self) -> str:
        head = {OK: "OK   ", WARN: "WARN ",
                FAIL: "FAIL " if self.blocking else "GAP  "}[self.status]
        text = f"{head} {self.message}"
        if self.remedy:
            text += "\n      -> " + self.remedy.replace("\n", "\n         ")
        return text


@dataclass
class Diagnosis:
    subject: str
    path: str
    checks: list[Check] = field(default_factory=list)

    def add(self, status: str, message: str, remedy: str | None = None,
            blocking: bool = True) -> None:
        self.checks.append(Check(status, message, remedy, blocking))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def blockers(self) -> list[Check]:
        """The failures that make the next step impossible rather than poorer."""
        return [c for c in self.checks if c.status == FAIL and c.blocking]

    @property
    def gaps(self) -> list[Check]:
        """Failures the run can survive, and must still say out loud."""
        return [c for c in self.checks if c.status == FAIL and not c.blocking]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def status(self) -> str:
        if self.blockers:
            return BLOCKED
        return PARTIAL if (self.gaps or self.warnings) else READY

    def to_dict(self) -> dict:
        return {"subject": self.subject, "path": self.path, "status": self.status,
                "checks": [{"status": c.status, "message": c.message,
                            "remedy": c.remedy, "blocking": c.blocking}
                           for c in self.checks]}

    def render(self) -> str:
        lines = [f"--- {self.subject}: {self.path}"]
        lines += [check.render() for check in self.checks]
        lines.append(f"STATUS {self.status}")
        return "\n".join(lines)


# --------------------------------------------------------------------- staging

def staged_tree(staging: Path, manifest: dict | None) -> Path | None:
    """Where the staged build actually is, or None if it is not anywhere.

    The manifest records an absolute path, which stops being true the moment the
    directory is renamed or the library is moved to another disk. The copy beside
    the manifest is the fallback, because that is where staging puts one.
    """
    recorded = (manifest or {}).get("staged_root")
    if recorded and Path(recorded).is_dir():
        return Path(recorded)
    beside = staging / "input"
    if beside.is_dir():
        return beside
    return None


def diagnose_staging(staging: Path) -> Diagnosis:
    """Check the tree that is about to be handed to AssetRipper."""
    result = Diagnosis("staging", str(staging))
    manifest_path = staging / "manifest.json"
    root = staging / "input" if (staging / "input").is_dir() else staging

    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # A build read where it lies is not under this directory at all, and a
        # recorded path stops being true as soon as anything is renamed.
        recorded = manifest.get("staged_root")
        found = staged_tree(staging, manifest)
        if found is not None and found != root:
            root = found
            result.path = f"{staging} -> {root}"
        missing_tree = manifest.get("dry_run") or found is None
        included = [p for p in manifest.get("packages", []) if p["included"]]
        shape = (manifest.get("packaging") or {}).get("shape", "?")
        result.add(OK, f"{len(included)} package(s) staged - {shape}, "
                       f"ABI {manifest.get('chosen_abi')}")
        unity = manifest.get("unity") or {}
        if unity.get("version") and unity.get("others"):
            result.add(WARN, f"Unity {unity['version']} declared by {unity['sources']} "
                             f"container(s); also present: {', '.join(unity['others'])}",
                       "content built by more than one editor version is normal for "
                       "engine resources, but worth knowing if the export behaves oddly")
        elif unity.get("version"):
            result.add(OK, f"Unity {unity['version']} declared by "
                           f"{unity['sources']} container(s)")
        else:
            result.add(WARN, "no container declared a Unity version")
        for warning in manifest.get("warnings", []):
            result.add(WARN, warning)
        for error in manifest.get("errors", []):
            result.add(FAIL, error)
        if manifest.get("unknown_binary_total"):
            result.add(WARN, f"{manifest['unknown_binary_total']} unrecognised files "
                             f"under assets/ were staged anyway",
                       "listed in manifest.json under 'unknown_binaries'")
        if missing_tree:
            # Nothing to walk. Saying so once beats reporting every consequence of
            # it as though the build itself were at fault.
            where = f" (recorded at {recorded})" if recorded else ""
            result.add(WARN,
                       f"no staged tree on disk{where}"
                       + (" - this manifest is from a dry run"
                          if manifest.get("dry_run") else ""),
                       "re-stage before exporting: python -m assetlab.ingest "
                       f"--input <package> --out {staging}")
            return result
    else:
        result.add(WARN, "no manifest.json; checking the tree only")

    # One walk, then every question below is asked of the list rather than the disk.
    every: list[Path] = []
    directories: list[Path] = []
    for path in root.rglob("*"):
        (directories if path.is_dir() else every).append(path)

    data_dirs = [p for p in directories if p.name == "Data" and p.parent.name == "bin"]
    if not data_dirs:
        result.add(FAIL, "no assets/bin/Data directory",
                   "AssetRipper needs the Unity player data. Check that the package "
                   "carrying it was included, not only the ABI splits.")
    for data_dir in data_dirs:
        payload = [p for p in every if p.parent == data_dir]
        # Games that ship loose player data fill this folder with thousands of
        # hash-named files, so name the recognisable ones and count the rest.
        notable = sorted(p.name for p in payload
                         if p.suffix.lower() in {".unity3d", ".resource", ".assets"}
                         or p.name.lower().startswith(("globalgamemanagers", "level",
                                                       "resources", "sharedassets",
                                                       "unity default", "boot.config")))
        shown = ", ".join(notable[:5]) if notable else "no recognisable payload names"
        extra = len(payload) - len(notable[:5])
        result.add(OK, f"{data_dir.relative_to(root).as_posix()} -> {shown}"
                       + (f" (+{extra} more files)" if extra > 0 else ""))

    metadata = [p for p in every if p.name == "global-metadata.dat"]
    if metadata:
        result.add(OK, "global-metadata.dat staged -> script types resolvable")
    else:
        result.add(WARN, "no global-metadata.dat staged",
                   "IL2CPP script types stay unresolved, so the export will have no "
                   "Scripts/ tree and no enum vocabulary to classify from")

    shared_objects = [p for p in every if p.name.startswith("lib") and p.suffix == ".so"]
    libs = sorted({p.parent.name for p in shared_objects})
    if len(libs) > 1:
        result.add(FAIL, f"native libraries for several ABIs staged ({libs})",
                   "AssetRipper must see exactly one ABI; re-stage from the packages "
                   "rather than merging trees by hand")
    elif libs:
        il2cpp = [p for p in shared_objects if p.name == "libil2cpp.so"]
        result.add(OK, f"native libraries for {libs[0]}"
                       f"{' incl. libil2cpp.so' if il2cpp else ' (no libil2cpp.so)'}")
    else:
        result.add(WARN, "no native libraries staged")

    # A build ships either UnityFS bundles or loose SerializedFiles; counting only
    # the former makes a perfectly good SerializedFile game look empty.
    bundles = serialized = 0
    for path in every:
        try:
            if path.stat().st_size <= 64:
                continue
            with path.open("rb") as handle:
                head = handle.read(16)
        except OSError:
            continue
        if head.startswith(b"UnityFS"):
            bundles += 1
        elif serialized_file_version(head) is not None:
            serialized += 1
        if bundles + serialized > 600:
            break
    parts = []
    if bundles:
        parts.append(f"{bundles} UnityFS bundle(s)")
    if serialized:
        parts.append(f"{serialized} loose SerializedFile(s)")
    if parts:
        more = "+" if bundles + serialized > 600 else ""
        result.add(OK, f"{' + '.join(parts)}{more} in the staged tree")
    else:
        result.add(FAIL, "no Unity content found in the staged tree",
                   "the packages were read but nothing in them is Unity data; this "
                   "may not be a Unity build")
    return result


# ---------------------------------------------------------------------- export

def diagnose_export(export: Path, primary: Path | None = None,
                    manifest: dict | None = None) -> Diagnosis:
    """Check what AssetRipper produced, against what it was given."""
    result = Diagnosis("export", str(export))
    if not export.is_dir():
        result.add(FAIL, f"export directory not found: {export}")
        return result
    if export.name.lower() != "assets":
        result.add(WARN, f"expected the export's 'Assets' folder, got '{export.name}'",
                   "point --export at <output>/ExportedProject/Assets")

    counts: Counter[str] = Counter()
    sprite_assets: list[Path] = []
    data_files: list[Path] = []
    for path in export.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        counts[suffix] += 1
        if suffix == ".asset":
            sprite_assets.append(path)
            data_files.append(path)
        elif suffix in DATA_SUFFIXES:
            data_files.append(path)
    # A large export holds thousands of non-sprite .asset files (MonoBehaviour and
    # friends) that sort ahead of Sprite/, so look there first instead of sampling
    # whatever the walk happened to reach.
    sprite_assets.sort(key=lambda p: 0 if "sprite" in
                       {part.lower() for part in p.parts[:-1]} else 1)

    if counts[".meta"] == 0:
        result.add(FAIL, "no .meta files - this is a Primary Content export",
                   "re-export from AssetRipper as Unity Project and point --export at "
                   "<output>/ExportedProject/Assets")
    else:
        result.add(OK, f"{counts['.meta']} .meta files -> GUID graph available")

    prefabs, scenes = counts[".prefab"], counts[".unity"]
    if prefabs == 0 and scenes == 0:
        result.add(FAIL, "no .prefab or .unity files",
                   "role detection and 'used by' links need them")
    else:
        result.add(OK, f"{prefabs} prefabs, {scenes} scenes -> roles and usage links")

    result.add(OK, f"{sum(counts[e] for e in ('.png', '.jpg', '.jpeg', '.tga'))} images")

    # The gate that was missing. Metadata went in, so scripts should have come out;
    # without this check an export that quietly ran without its IL2CPP metadata
    # looks fine and just produces a thin catalogue nobody can explain.
    scripts, metas = counts[".cs"], counts[".meta"]
    staged_metadata = bool((manifest or {}).get("il2cpp", {}).get("metadata"))
    # Proportion, not presence. One script out of a 31,000-asset export is the same
    # failure as none, and it is the shape the real miss actually took.
    too_few = scripts < metas * SCRIPT_RATIO_FLOOR and metas >= SCRIPT_RATIO_MIN_ASSETS
    if staged_metadata and (not scripts or too_few):
        measured = (f"only {scripts} .cs files against {metas} assets"
                    if scripts else "no .cs files")
        result.add(FAIL, f"global-metadata.dat was staged but the export has {measured}",
                   "AssetRipper did not use the IL2CPP metadata. Check that "
                   "libil2cpp.so for the staged ABI sits beside it, then re-export; "
                   "without scripts the obstacle vocabulary is unavailable. Every "
                   "other stage still works, so the run continues.",
                   blocking=False)
    elif scripts:
        result.add(OK, f"{scripts} .cs files -> script types and enum vocabulary")
    else:
        result.add(WARN, "no .cs files, and no IL2CPP metadata was staged",
                   "classification falls back to level files and asset names")

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
        _, guid, rect, rotation, *_placement = parse_sprite(text)
        if rect:
            with_rect += 1
        if guid:
            with_atlas += 1
        if rotation:
            rotated += 1

    if checked == 0:
        result.add(FAIL, "no sprite YAML found",
                   "if this build has sprites, set SpriteExportMode=Yaml in "
                   "AssetRipper and re-export - otherwise they cannot be cut out of "
                   "their atlases and you browse sheets. A build whose art is "
                   "geometry has none to find, and the profile stage tells them apart.",
                   blocking=False)
    elif with_rect < checked:
        result.add(WARN, f"{checked - with_rect}/{checked} sampled sprites have no crop rect")
    elif with_atlas < checked:
        # `texture: {fileID: 0}` means the atlas is genuinely absent from the build,
        # typically content the game streams after install. Nothing to slice.
        result.add(WARN, f"{checked - with_atlas}/{checked} sampled sprites reference no "
                         f"atlas texture (content not shipped in the package); "
                         f"the rest slice fine")
    else:
        result.add(OK, f"{checked}/{checked} sampled sprites carry an atlas rect "
                       f"-> slicing works")
    if rotated:
        result.add(WARN, f"{rotated}/{checked} sampled sprites are packed rotated",
                   "slicing un-rotates them, but eyeball one afterwards - the "
                   "90-degree direction is untested against real data")

    if primary:
        bundle_dir = primary / "AssetBundle"
        bundles = len(list(bundle_dir.glob("*.json"))) if bundle_dir.is_dir() else 0
        if bundles:
            result.add(OK, f"{bundles} AssetBundle manifests -> Addressables provenance")
        else:
            result.add(WARN, "no AssetBundle/*.json records in --primary-content",
                       "optional; it only adds bundle labels")

    result.checks.extend(diagnose_levels(export, data_files))
    return result


def diagnose_levels(export: Path, files: list[Path] | None = None) -> list[Check]:
    """Separate 'no levels here' from 'levels here that we cannot read'.

    Reporting those two the same way has already hidden one build's entire level
    corpus, so they get different answers even though neither produces a parse.
    """
    if files is None:
        files = [path for path in export.rglob("*")
                 if path.suffix.lower() in SCANNED_SUFFIXES and path.is_file()]

    # Grouping is pure path arithmetic, so it runs before any file is opened.
    candidates = corpus_candidates(export, limit=8, files=files)
    corpus_dirs = {entry["directory"] for entry in candidates}

    def in_a_candidate(path: Path) -> bool:
        try:
            return path.parent.relative_to(export).as_posix() in corpus_dirs
        except ValueError:
            return False

    # Files inside a candidate group first: that is where a level corpus lives, and
    # on a build that has one this matches within the first few opens.
    readable = [path for path in files if path.suffix.lower() in {".json", ".bytes"}]
    readable.sort(key=lambda path: 0 if in_a_candidate(path) else 1)
    for opened, path in enumerate(readable):
        if opened >= TILED_PROBE_LIMIT:
            break
        if looks_like_tiled(path):
            return [Check(OK, f"level corpus: {LEVELS_PARSED} - Tiled-style files found")]

    # A corpus in a binary format is still readable when the build ships the
    # schema for it, which is the usual case for FlatBuffers in an IL2CPP export.
    fitted = [entry for entry in flat_corpus_fits(export) if entry["fit"] >= 0.9]
    if fitted:
        listed = ", ".join(f"{entry['directory']} ({entry['count']} x "
                           f"{entry['schema']})" for entry in fitted[:3])
        return [Check(OK, f"level corpus: {LEVELS_PARSED} - FlatBuffers, "
                          f"read with the build's own schema: {listed}")]

    corpora = [entry for entry in candidates if entry.get("corpus")]
    if corpora:
        top = corpora[0]
        listed = ", ".join(f"{c['directory']} ({c['count']} x {c['extension']})"
                           for c in corpora[:3])
        return [Check(WARN, f"level corpus: {LEVELS_UNREADABLE} - {listed}",
                      f"{top['count']} files that look like one level set, in a format "
                      f"the parser does not read (example: {top['example']}). That is "
                      f"not the same finding as the game having no levels.")]

    if candidates:
        # Below the corpus threshold, but not nothing. A build can keep its design
        # data in many small indexed sets - waves, live-ops, tuning tables - and
        # answering "no likely level data" about those is how a reader concludes a
        # game has no missions.
        top = candidates[0]
        named = ", ".join(entry["pattern"] for entry in candidates[:4])
        return [Check(OK, f"level corpus: {LEVELS_ABSENT} - no single corpus, but "
                          f"{len(candidates)} indexed data group(s): {named}",
                      f"the largest is {top['count']} files in {top['directory']}. "
                      f"This build spreads its design data across small sets rather "
                      f"than one numbered corpus; nothing is parsed from them.")]

    return [Check(OK, f"level corpus: {LEVELS_ABSENT} - no likely level data "
                      f"in the export")]


# --------------------------------------------------------------------- outcome

#: What is read from a finished catalogue, and how it is normalised. Counts are
#: divided by the asset total so a small build is not mistaken for a broken one.
OUTCOME_METRICS = {
    "mechanics": ("SELECT COUNT(DISTINCT value) FROM tags WHERE kind='mechanic'", 1),
    "features": ("SELECT COUNT(DISTINCT value) FROM tags WHERE kind='feature'", 1),
    "sprites per asset": ("SELECT COUNT(*) FROM sprites", "assets"),
    "refs per asset": ("SELECT COUNT(*) FROM refs", "assets"),
    "clips per asset": ("SELECT COUNT(*) FROM animations", "assets"),
    "classified share": ("""SELECT COUNT(DISTINCT asset_id) FROM tags
                             WHERE kind IN ('category','mechanic','feature')""", "assets"),
}


def catalogue_metrics(database: Path) -> dict[str, float] | None:
    """Read one catalogue's numbers, or None if it is not one."""
    if not database.is_file():
        return None
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        def scalar(sql: str) -> float:
            try:
                value = conn.execute(sql).fetchone()[0]
            except sqlite3.OperationalError:
                return 0.0
            return float(value or 0)

        assets = scalar("SELECT COUNT(*) FROM assets")
        if not assets:
            return None
        found = {"assets": assets,
                 "sprite_assets": scalar(
                     "SELECT COUNT(*) FROM assets WHERE unity_type='Sprite'"),
                 "sprite_rects": scalar("SELECT COUNT(*) FROM sprites")}
        for name, (sql, divisor) in OUTCOME_METRICS.items():
            found[name] = scalar(sql) / (assets if divisor == "assets" else 1)
        return found
    finally:
        conn.close()


def diagnose_outcome(out: Path, peers: list[Path] | None = None) -> Diagnosis:
    """Compare a finished catalogue with the others beside it.

    The comparison is reported, not judged. Across real builds these numbers vary
    for honest reasons - a game that animates in code has few clips, a game with
    plain art classifies a smaller share - and a threshold on any of them would
    accuse healthy builds. What the reader gets is where this build sits; what the
    tool asserts is only the one ratio that separates cleanly.
    """
    result = Diagnosis("outcome", str(out))
    mine = catalogue_metrics(out / "assetlab.db")
    if not mine:
        result.add(WARN, "no catalogue to compare")
        return result

    if peers is None:
        peers = [entry for entry in out.parent.iterdir()
                 if entry.is_dir() and entry != out and (entry / "assetlab.db").is_file()]
    others = [m for m in (catalogue_metrics(p / "assetlab.db") for p in peers) if m]

    # The one assertion: a build whose mechanic vocabulary is tiny next to the
    # feature vocabulary it recovered from the same files has lost something.
    features, mechanics = mine["features"], mine["mechanics"]
    if features >= MECHANIC_MIN_FEATURES:
        ratio = mechanics / features
        if ratio < MECHANIC_VOCABULARY_FLOOR:
            result.add(FAIL,
                       f"{mechanics:.0f} mechanics against {features:.0f} features "
                       f"({ratio:.3f}) - the vocabulary is inconsistent with itself",
                       "this build named plenty of features but almost no mechanics, "
                       "which is what a missing script vocabulary looks like. Check "
                       "the export gate for IL2CPP metadata, and the level corpus "
                       "for a format the parser cannot read.",
                       blocking=False)
        else:
            result.add(OK, f"{mechanics:.0f} mechanics against {features:.0f} features "
                           f"({ratio:.3f}) - vocabulary is self-consistent")

    # The second assertion: art the build ships that the catalogue could not place.
    declared, placed = mine.get("sprite_assets", 0), mine.get("sprite_rects", 0)
    if declared >= SPRITE_PLACEMENT_MIN:
        share = placed / declared
        if share < SPRITE_PLACEMENT_FLOOR:
            result.add(FAIL,
                       f"{placed:.0f} of {declared:.0f} sprites have a rect on a page "
                       f"({share:.2f}) - most of this build's art is not placed",
                       "the sprites name an atlas but carry no texture of their own, "
                       "which is what Unity's SpriteAtlas packing looks like: the "
                       "packed position lives in the atlas asset, and this export "
                       "does not contain it. The pages themselves are catalogued, and "
                       "any Spine descriptors beside them are read; the rest cannot "
                       "be recovered from these files.",
                       blocking=False)
        else:
            result.add(OK, f"{placed:.0f} of {declared:.0f} sprites placed "
                           f"({share:.2f})")

    if len(others) < 3:
        result.add(OK, f"{len(others)} peer catalogue(s) - too few to compare against")
        return result

    lines = []
    for name in ("assets", *OUTCOME_METRICS):
        values = sorted(m[name] for m in others)
        median = statistics.median(values)
        share = mine[name] / median if median else 0.0
        shown = f"{mine[name]:,.0f}" if name in ("assets", "mechanics", "features")             else f"{mine[name]:.3f}"
        middle = f"{median:,.0f}" if name in ("assets", "mechanics", "features")             else f"{median:.3f}"
        lines.append(f"{name} {shown} (peers {middle}, {share:.2f}x)")
    result.add(OK, f"against {len(others)} peers: " + "; ".join(lines))
    return result


# ---------------------------------------------------------------------- survey

def normalised(name: str) -> str:
    """Fold case and separators, so `SeatSquad` and `seat_squad` compare equal."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def provenance_of(database: Path) -> dict:
    """What a catalogue records about where it came from, or nothing."""
    if not database.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return {}
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='provenance'").fetchone()
        return json.loads(row[0]) if row else {}
    except (sqlite3.OperationalError, json.JSONDecodeError):
        return {}
    finally:
        conn.close()


def locate(name: str, root: Path) -> Path | None:
    """The directory under `root` whose name is this one, however it was spelled."""
    if not root.is_dir():
        return None
    wanted = normalised(name)
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and normalised(entry.name) == wanted:
            return entry
    return None


def survey(out_root: Path, staging_root: Path, exports_root: Path) -> list[dict]:
    """Re-run every gate over every catalogue on disk."""
    rows: list[dict] = []
    for directory in sorted(out_root.iterdir()):
        if not directory.is_dir() or not (directory / "assetlab.db").is_file():
            continue
        provenance = provenance_of(directory / "assetlab.db")

        staged = provenance.get("staged_root")
        staging = (Path(staged).parent
                   if staged and Path(staged).name == "input" and Path(staged).is_dir()
                   else locate(directory.name, staging_root))
        exported = provenance.get("export")
        export = Path(exported) if exported and Path(exported).is_dir() else None
        if export is None:
            beside = locate(directory.name, exports_root)
            export = beside / "ExportedProject" / "Assets" if beside else None

        manifest = None
        if staging and (staging / "manifest.json").is_file():
            manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))

        reports = []
        if staging and staging.is_dir():
            reports.append(diagnose_staging(staging))
        if export and export.is_dir():
            reports.append(diagnose_export(export, None, manifest))
        reports.append(diagnose_outcome(directory))

        unity = (manifest or {}).get("unity") or {}
        rows.append({
            "name": directory.name,
            "reports": reports,
            "status": BLOCKED if any(r.blockers for r in reports)
                      else PARTIAL if any(r.gaps or r.warnings for r in reports)
                      else READY,
            "abi": (manifest or {}).get("chosen_abi") or "-",
            "unity": unity.get("version") or "-",
            "shape": ((manifest or {}).get("packaging") or {}).get("shape", "-"),
            "levels": level_verdict(next((r for r in reports
                                          if r.subject == "export"), None)) or "-",
            "blockers": [c.message for r in reports for c in r.blockers],
            "gaps": [c.message for r in reports for c in r.gaps],
        })
    return rows


def render_survey(rows: list[dict]) -> str:
    if not rows:
        return "no catalogues found"
    width = max(len(row["name"]) for row in rows)
    lines = [f"{'build':<{width}}  {'status':<8} {'abi':<12} {'unity':<12} levels",
             "-" * (width + 48)]
    for row in rows:
        lines.append(f"{row['name']:<{width}}  {row['status']:<8} {row['abi']:<12} "
                     f"{row['unity']:<12} {row['levels']}")
    lines.append("")
    for row in rows:
        for message in row["blockers"]:
            lines.append(f"BLOCKED  {row['name']}: {message}")
        for message in row["gaps"]:
            lines.append(f"GAP      {row['name']}: {message}")
    blocked = [row for row in rows if row["status"] == BLOCKED]
    lines.append("")
    lines.append(f"{len(rows)} builds, {len(blocked)} blocked, "
                 f"{sum(len(row['gaps']) for row in rows)} gap(s)")
    return "\n".join(lines)


# ------------------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check an ingestion staging tree and/or an AssetRipper export.")
    parser.add_argument("--export", type=Path, default=None,
                        help="ExportedProject/Assets from a Unity Project export")
    parser.add_argument("--staging", type=Path, default=None,
                        help="an assetlab.ingest output directory (pre-AssetRipper)")
    parser.add_argument("--primary-content", type=Path, default=None)
    parser.add_argument("--catalogue", type=Path, default=None,
                        help="a finished out/<name> directory, compared with its peers")
    parser.add_argument("--all", action="store_true",
                        help="re-run every gate over every catalogue on disk, as a "
                             "regression over builds that share no packaging")
    parser.add_argument("--out-root", type=Path, default=Path("out"))
    parser.add_argument("--staging-root", type=Path, default=Path("staging"))
    parser.add_argument("--exports-root", type=Path, default=Path("exports"))
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the diagnosis here as JSON")
    args = parser.parse_args()

    if args.all:
        rows = survey(args.out_root.resolve(), args.staging_root.resolve(),
                      args.exports_root.resolve())
        print(render_survey(rows))
        if args.json:
            args.json.write_text(json.dumps(
                [{k: v for k, v in row.items() if k != "reports"} |
                 {"gates": [r.to_dict() for r in row["reports"]]} for row in rows],
                indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\nwritten {args.json}")
        raise SystemExit(1 if any(row["status"] == BLOCKED for row in rows) else 0)

    if not args.export and not args.staging and not args.catalogue:
        parser.error("pass --staging, --export, --catalogue or --all")

    reports: list[Diagnosis] = []
    manifest = None
    if args.staging:
        if not args.staging.is_dir():
            print(f"FAIL  staging directory not found: {args.staging}")
            raise SystemExit(1)
        manifest_path = args.staging / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        reports.append(diagnose_staging(args.staging))
    if args.export:
        reports.append(diagnose_export(args.export, args.primary_content, manifest))
    if args.catalogue:
        reports.append(diagnose_outcome(args.catalogue.resolve()))

    for report in reports:
        print(report.render())
        print()
    if args.json:
        args.json.write_text(json.dumps([r.to_dict() for r in reports], indent=2),
                             encoding="utf-8")
        print(f"written {args.json}")
    raise SystemExit(1 if any(r.failures for r in reports) else 0)


if __name__ == "__main__":
    main()
