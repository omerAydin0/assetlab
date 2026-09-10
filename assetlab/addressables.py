"""What a build's Addressables catalogue declares, and how much of it the package holds.

A build that loads content through Addressables ships a catalogue beside it, in
`assets/aa/catalog.json`, listing every bundle the game will ever ask for. Some of those
are in the package; others sit behind an address and are fetched at run time. A library
built from the package can only cover the first kind, and without saying so a reader
takes it for the whole game. One build declares nineteen bundles and ships fifteen; the
four it does not ship hold a timed event's dialogs, a second event's offer and eight
gameplay backgrounds - twenty-two addressable entries in all, none of them in these
files. The catalogue lists each background twice; the report counts it once.

This reads the catalogue and reports that difference. It never follows an address: the
remote bundles are named by file only, and no address is written into the report. What
is not in the package is outside what this tool reads.

The catalogue's JSON holds its structure in three base64 blobs. `m_EntryDataString` is a
count followed by seven 32-bit fields per entry - internal id, provider, dependency key,
dependency hash, data index, primary key, resource type - and `m_BucketDataString` gives,
for each key, the entries it resolves to. An entry's dependency key therefore names the
bundles it needs, and an entry that needs a bundle the package lacks cannot load from it.
"""
from __future__ import annotations

import argparse
import base64
import json
import struct
from pathlib import Path

#: How a catalogue addresses the package's own streaming assets folder.
RUNTIME_PATH = "{UnityEngine.AddressableAssets.Addressables.RuntimePath}"
#: Fields per entry in the entry blob, each a little-endian int32.
ENTRY_FIELDS = 7
ENTRY_BYTES = 4 * ENTRY_FIELDS
#: Enough of the unreachable entries to act on, without a report that runs to pages.
UNREACHABLE_LIMIT = 200


def _expand(value: str, prefixes: list[str]) -> str:
    """Internal ids may be stored as `<prefix index>#<rest>` to save space."""
    head, separator, tail = value.partition("#")
    if separator and head.isdigit() and int(head) < len(prefixes):
        return prefixes[int(head)] + tail
    return value


def decode(catalog: dict) -> tuple[list[str], list[tuple], list[list[int]]]:
    """-> (internal ids, entries, buckets). Entries or buckets that do not decode cleanly
    come back empty rather than half-read, so a caller can tell the difference."""
    prefixes = catalog.get("m_InternalIdPrefixes") or []
    ids = [_expand(value, prefixes) for value in (catalog.get("m_InternalIds") or [])]

    entries: list[tuple] = []
    raw = base64.b64decode(catalog.get("m_EntryDataString") or "")
    if len(raw) >= 4:
        count = struct.unpack_from("<i", raw, 0)[0]
        if count > 0 and 4 + ENTRY_BYTES * count <= len(raw):
            entries = [struct.unpack_from(f"<{ENTRY_FIELDS}i", raw, 4 + ENTRY_BYTES * k)
                       for k in range(count)]

    buckets: list[list[int]] = []
    raw = base64.b64decode(catalog.get("m_BucketDataString") or "")
    if len(raw) >= 4:
        count = struct.unpack_from("<i", raw, 0)[0]
        offset = 4
        for _ in range(max(0, count)):
            if offset + 8 > len(raw):
                buckets = []
                break
            _, members = struct.unpack_from("<2i", raw, offset)
            offset += 8
            if members < 0 or offset + 4 * members > len(raw):
                buckets = []
                break
            buckets.append(list(struct.unpack_from(f"<{members}i", raw, offset)))
            offset += 4 * members
    return ids, entries, buckets


def bundle_file(internal_id: str) -> str:
    return internal_id.replace("\\", "/").rsplit("/", 1)[-1]


def bundle_group(file_name: str) -> str:
    """`ui_shop_assets_all_<hash>.bundle` -> `ui_shop`: Addressables names a bundle after
    its group, then `_assets_`, then what it packs."""
    stem = file_name.rsplit(".", 1)[0]
    return stem.split("_assets_")[0] if "_assets_" in stem else stem


def bundle_name(file_name: str) -> str:
    """What a reader would call the bundle.

    A group packed together is one bundle, `ui_shop_assets_all_<hash>`, named by its
    group. A group packed separately is one bundle per entry,
    `remotegroup_assets_background_<hash>`, and there the group name is the same for
    all of them - reporting four missing bundles as `remotegroup` four times says less
    than `remotegroup/background`, `remotegroup/cupcaketime` and the rest.
    """
    stem = file_name.rsplit(".", 1)[0]
    if "_assets_" not in stem:
        return stem
    group, rest = stem.split("_assets_", 1)
    label = rest.rsplit("_", 1)[0] if "_" in rest else rest
    return group if label in ("", "all") else f"{group}/{label}"


def read_catalog(path: Path, package_root: Path) -> dict:
    """One catalogue, measured against the folder it ships in."""
    catalog = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    ids, entries, buckets = decode(catalog)
    aa = path.parent

    bundles: list[dict] = []
    by_id: dict[int, dict] = {}
    for position, value in enumerate(ids):
        if not value.lower().endswith(".bundle"):
            continue
        local = value.startswith("{") and "RuntimePath" in value
        name = bundle_file(value)
        shipped = False
        if local:
            relative = value.split("}", 1)[1].lstrip("/\\")
            shipped = (aa / relative).is_file()
        record = {"file": name, "name": bundle_name(name), "group": bundle_group(name),
                  "location": "local" if local else "remote", "shipped": shipped}
        bundles.append(record)
        by_id[position] = record

    unreachable: list[str] = []
    decoded = bool(entries) and bool(buckets)
    if decoded:
        missing_entries = {index for index, entry in enumerate(entries)
                           if entry[0] in by_id and not by_id[entry[0]]["shipped"]}
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            if index in missing_entries or entry[0] in by_id:
                continue
            key = entry[2]
            if not 0 <= key < len(buckets):
                continue
            if set(buckets[key]) & missing_entries:
                value = ids[entry[0]] if 0 <= entry[0] < len(ids) else f"entry {index}"
                if value not in seen:
                    seen.add(value)
                    unreachable.append(value)

    try:
        where = path.relative_to(package_root).as_posix()
    except ValueError:
        where = path.name
    return {"catalog": where, "entries": len(entries), "decoded": decoded,
            "bundles": bundles, "unreachable": unreachable[:UNREACHABLE_LIMIT],
            "unreachable_total": len(unreachable)}


def find_catalogs(root: Path) -> tuple[list[Path], list[Path]]:
    """Every Addressables catalogue under a package: (JSON ones, binary ones).

    Addressables 2 can write its catalogue as binary. That format is not read here, and
    it is reported as unread rather than passed over as though no catalogue existed.
    """
    json_ones, binary_ones = [], []
    for path in root.rglob("catalog*"):
        if path.parent.name.lower() != "aa" or not path.is_file():
            continue
        if path.suffix.lower() == ".json":
            json_ones.append(path)
        elif path.suffix.lower() == ".bin":
            binary_ones.append(path)
    return sorted(json_ones), sorted(binary_ones)


def report(package_root: Path) -> dict:
    """Everything the package's catalogues declare, and the difference from what it holds."""
    json_ones, binary_ones = find_catalogs(package_root)
    catalogs = []
    for path in json_ones:
        try:
            catalogs.append(read_catalog(path, package_root))
        except (OSError, ValueError, struct.error) as problem:
            catalogs.append({"catalog": path.name, "error": str(problem)})
    bundles = [bundle for catalog in catalogs for bundle in catalog.get("bundles", [])]
    missing = [bundle for bundle in bundles if not bundle["shipped"]]
    return {
        "package": package_root.name,
        "catalogs": catalogs,
        "binary_catalogs": [path.name for path in binary_ones],
        "summary": {
            "catalogs": len(catalogs), "binary_catalogs": len(binary_ones),
            "bundles": len(bundles),
            "shipped": sum(1 for bundle in bundles if bundle["shipped"]),
            "remote": sum(1 for bundle in bundles if bundle["location"] == "remote"),
            "local_missing": sum(1 for bundle in missing if bundle["location"] == "local"),
            "missing_bundles": sorted({bundle["name"] for bundle in missing}),
            "unreachable_entries": sum(catalog.get("unreachable_total", 0)
                                       for catalog in catalogs),
        },
    }


def write_report(package_root: Path | None, out_dir: Path) -> dict | None:
    """Write `addressables.json` beside the catalogue. -> its summary, or None."""
    if not package_root or not Path(package_root).is_dir():
        return None
    data = report(Path(package_root))
    if not data["catalogs"] and not data["binary_catalogs"]:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "addressables.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data["summary"]


def describe(summary: dict) -> str:
    if not summary["bundles"]:
        return (f"{summary['catalogs']} Addressables catalogue(s), no bundles declared "
                f"- everything it lists loads from the package itself")
    text = (f"{summary['bundles']} bundles declared, {summary['shipped']} in the package")
    if summary["remote"] or summary["local_missing"]:
        text += (f"; {summary['remote']} remote and {summary['local_missing']} local "
                 f"missing ({', '.join(summary['missing_bundles'])}), "
                 f"{summary['unreachable_entries']} entries depend on them")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report what a package's Addressables catalogue declares against "
                    "what the package ships. Reads local files only.")
    parser.add_argument("--package", required=True, type=Path,
                        help="the unpacked package, or the staged input folder")
    parser.add_argument("--out", required=True, type=Path,
                        help="the catalogue folder to write addressables.json into")
    args = parser.parse_args()
    summary = write_report(args.package.resolve(), args.out.resolve())
    if summary is None:
        print("no Addressables catalogue under", args.package)
        return
    print(describe(summary))
    print("wrote", args.out / "addressables.json")


if __name__ == "__main__":
    main()
