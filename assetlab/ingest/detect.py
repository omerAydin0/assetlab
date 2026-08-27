"""Format sniffing and role detection for Android package members.

Everything here decides *what a file is* from its bytes and its path, never from
the game it came from. Extensionless files are common in Unity asset packs, so a
file only counts as an AssetBundle when its magic says so.

Observed magics (verified against real builds):
    UnityFS\\0            data.unity3d, datapack.unity3d, assets/android/* bundles
    \\xaf\\x1b\\xb1\\xfa      global-metadata.dat (0xFAB11BAF)
    \\x7fELF              libil2cpp.so, libunity.so
    FSB5                 .resource holding an FMOD sound bank
"""

from __future__ import annotations

import posixpath
import re
import struct
from dataclasses import dataclass, field

# Roles a member can take in an AssetRipper input tree.
UNITY_DATA = "unity_data"            # bin/Data payload: data.unity3d, datapack.unity3d, ...
ASSET_BUNDLE = "asset_bundle"        # a standalone UnityFS bundle (asset pack content)
RESOURCE_STREAM = "resource_stream"  # .resource/.resS companion streams
IL2CPP_METADATA = "il2cpp_metadata"  # global-metadata.dat
NATIVE_LIB = "native_lib"            # lib/<abi>/*.so
MANAGED = "managed"                  # bin/Data/Managed/**
CATALOG = "catalog"                  # Addressables / asset-pack json descriptors
UNITY_SUPPORT = "unity_support"      # other bin/Data files (boot.config, resS, ...)
NESTED_ARCHIVE = "nested_archive"    # zip/gzip under assets/ - AssetRipper won't open it
UNKNOWN_BINARY = "unknown_binary"    # under assets/ but unrecognised - reported, not hidden
OTHER = "other"                      # dex, res, META-INF, ad SDK files

STAGEABLE = {UNITY_DATA, ASSET_BUNDLE, RESOURCE_STREAM, IL2CPP_METADATA,
             NATIVE_LIB, MANAGED, CATALOG, UNITY_SUPPORT, NESTED_ARCHIVE, UNKNOWN_BINARY}

BUNDLE_MAGICS = (b"UnityFS", b"UnityWeb", b"UnityRaw", b"UnityArchive")
METADATA_MAGIC = b"\xaf\x1b\xb1\xfa"
ELF_MAGIC = b"\x7fELF"

# Preference order when a build ships several ABI splits; only one may be staged
# or AssetRipper sees two different libil2cpp.so.
ABI_PREFERENCE = ("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
ABI_RE = re.compile(r"(?:^|/)lib/([^/]+)/")
# Matched against an already-lowercased path, so the pattern is lowercase too.
DATA_DIR_RE = re.compile(r"(?:^|/)bin/data(?:/|$)")

MAGIC_NAMES = [
    (BUNDLE_MAGICS, "UnityFS"),
    ((METADATA_MAGIC,), "il2cpp-metadata"),
    ((ELF_MAGIC,), "ELF"),
    ((b"FSB5",), "FMOD-FSB5"),
    ((b"PK\x03\x04",), "zip"),
    ((b"\x1f\x8b",), "gzip"),
    ((b"OggS",), "ogg"),
    ((b"\x89PNG",), "png"),
]


SPLIT_PART_RE = re.compile(r"\.split\d+$")


def serialized_file_version(head: bytes) -> int | None:
    """Unity SerializedFile (`globalgamemanagers`, `level0`, `sharedassets0.assets`).

    These carry no ASCII magic: the header is four big-endian uint32s, and from
    version 22 the size fields move to 64-bit further in, leaving the first eight
    bytes zero. Games that ship loose player data instead of one `data.unity3d`
    are entirely made of these.
    """
    if len(head) < 16:
        return None
    metadata_size, file_size, version, data_offset = struct.unpack(">IIII", head[:16])
    if not 6 <= version <= 40:
        return None
    if version >= 22 and (metadata_size or file_size or data_offset):
        return None
    return version


def sniff_magic(head: bytes) -> str | None:
    for magics, label in MAGIC_NAMES:
        if any(head.startswith(magic) for magic in magics):
            return label
    stripped = head.lstrip()
    if stripped[:1] in (b"{", b"["):
        return "json"
    return None


def abi_of(member: str) -> str | None:
    match = ABI_RE.search(member)
    return match.group(1) if match else None


@dataclass
class Member:
    """One file inside a package, with why we think it is what it is."""
    package: str
    path: str
    size: int
    role: str
    magic: str | None = None
    abi: str | None = None
    evidence: list[str] = field(default_factory=list)
    confidence: str = "medium"

    @property
    def stageable(self) -> bool:
        return self.role in STAGEABLE


def classify(package: str, member: str, size: int, head: bytes) -> Member:
    """Decide a member's role from its path and leading bytes."""
    lowered = member.lower()
    name = posixpath.basename(lowered)
    magic = sniff_magic(head)
    evidence: list[str] = []
    if magic:
        evidence.append(f"magic:{magic}")

    in_data_dir = bool(DATA_DIR_RE.search(lowered))
    if in_data_dir:
        evidence.append("path:bin/Data")

    # Native libraries and IL2CPP metadata: AssetRipper needs both together to
    # resolve script types. We never decompile them.
    abi = abi_of(lowered)
    if abi:
        evidence.append(f"abi:{abi}")
        if magic == "ELF" or lowered.endswith(".so"):
            return Member(package, member, size, NATIVE_LIB, magic, abi,
                          evidence, "high" if magic == "ELF" else "medium")

    if name == "global-metadata.dat" or magic == "il2cpp-metadata":
        evidence.append("name:global-metadata.dat" if name == "global-metadata.dat"
                        else "magic:0xFAB11BAF")
        return Member(package, member, size, IL2CPP_METADATA, magic, abi, evidence, "high")

    if in_data_dir and "/managed/" in lowered:
        evidence.append("path:bin/Data/Managed")
        return Member(package, member, size, MANAGED, magic, abi, evidence, "high")

    if magic == "UnityFS":
        # A UnityFS file inside bin/Data is the player's own data; anywhere else
        # it is asset-pack content (assets/android/..., assets/aa/..., *.ab).
        role = UNITY_DATA if in_data_dir else ASSET_BUNDLE
        return Member(package, member, size, role, magic, abi, evidence, "high")

    # Loose player data: no ASCII magic, so the header has to be decoded.
    version = serialized_file_version(head)
    if version is not None and in_data_dir:
        evidence.append(f"serialized-file:v{version}")
        return Member(package, member, size, UNITY_DATA, "SerializedFile", abi,
                      evidence, "high")
    if SPLIT_PART_RE.search(lowered) and in_data_dir:
        # `sharedassets0.assets.split1` - only part 0 carries the header.
        evidence.append("split part of a serialized file")
        return Member(package, member, size, UNITY_DATA, magic, abi, evidence, "high")

    if magic in {"zip", "gzip"} and lowered.startswith("assets/"):
        evidence.append("archive nested inside the package")
        return Member(package, member, size, NESTED_ARCHIVE, magic, abi, evidence, "high")

    if lowered.endswith((".resource", ".ress", ".resS".lower())):
        evidence.append("ext:resource-stream")
        return Member(package, member, size, RESOURCE_STREAM, magic, abi, evidence, "high")

    if magic == "json" and (in_data_dir or "/aa/" in lowered or "/android/" in lowered
                            or name.endswith(("catalog.json", "settings.json"))
                            or "config" in name):
        evidence.append("json descriptor next to Unity content")
        return Member(package, member, size, CATALOG, magic, abi, evidence, "medium")

    if in_data_dir:
        # boot.config, unity default resources, sharedassets, ScriptingAssemblies...
        return Member(package, member, size, UNITY_SUPPORT, magic, abi, evidence, "high")

    if lowered.startswith("assets/"):
        if magic in (None, "FMOD-FSB5"):
            evidence.append("under assets/ but no Unity signature")
            return Member(package, member, size, UNKNOWN_BINARY, magic, abi, evidence, "low")
        return Member(package, member, size, OTHER, magic, abi, evidence, "medium")

    return Member(package, member, size, OTHER, magic, abi, evidence, "high")


@dataclass
class PackageReport:
    """What one APK contributes, used to decide whether to stage it at all."""
    name: str
    path: str
    size: int
    members: list[Member]

    def count(self, role: str) -> int:
        return sum(1 for member in self.members if member.role == role)

    @property
    def abis(self) -> set[str]:
        return {m.abi for m in self.members if m.role == NATIVE_LIB and m.abi}

    @property
    def unity_relevant(self) -> int:
        return sum(1 for member in self.members
                   if member.role in {UNITY_DATA, ASSET_BUNDLE, RESOURCE_STREAM,
                                      IL2CPP_METADATA, MANAGED, UNITY_SUPPORT})

    def summary(self) -> dict[str, int]:
        roles: dict[str, int] = {}
        for member in self.members:
            roles[member.role] = roles.get(member.role, 0) + 1
        return dict(sorted(roles.items()))


def choose_abi(available: set[str]) -> str | None:
    for abi in ABI_PREFERENCE:
        if abi in available:
            return abi
    return next(iter(sorted(available)), None)
