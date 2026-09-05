"""Stage 0 - work out what kind of game this export is, before analysing it.

The rest of the pipeline was written for 2D match-3 builds: it cuts sprites out of
atlases and rebuilds animations by composing sprite layers. Point it at a build that
draws with meshes and it finds almost nothing, reports zero, and gives no reason -
which reads as a broken input rather than a tool that does not apply.

So the export is measured first. No single signal settles it, and the ones that look
obvious do not survive contact with real builds:

* **Camera projection is useless.** A stylised 3D game renders its models through an
  orthographic camera as readily as a 2D one does; the build that prompted this has
  two orthographic cameras and no perspective camera at all.
* **Sprite-to-mesh asset ratio is weak.** The closest 2D build measures 15 sprites
  per mesh and the 3D one measures 11 - overlapping ranges.

What does separate them, measured across seven shipped builds:

| signal                         | 2D builds | the 3D build |
|--------------------------------|-----------|--------------|
| average mesh size              | 5-79 KB   | 440 KB       |
| materials on a lit shader      | 0%        | 63%          |
| mesh renderers / all renderers | 4-16%     | 29%          |

The shader signal is the decisive one and took two wrong turns to find. Counting
materials that *declare* a `_BumpMap` slot measures nothing: Unity serialises the
slot for every material on a lit-capable shader whether or not a texture is bound,
so both a 2D and a 3D build read as 100%. Counting materials with a normal map
actually bound measures nothing either - across all seven builds, including the 3D
one, that count is zero. The 3D build gets its look from lit shaders and baked
colour, not from normal maps.

What the material does point at reliably is its shader, and there the split is
total: the 3D build runs 63% of its materials through `Universal Render
Pipeline/Lit`, while the six 2D builds run theirs through Sprites, TextMeshPro and
Spine shaders and use no lit shader at all.

Each is reported alongside the verdict, because a number that can be checked is
worth more than a label that cannot.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .core import connect

CLASS_ID_RE = re.compile(rb"^--- !u!(\d+) &", re.M)
SHADER_REF_RE = re.compile(rb"m_Shader:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-f]{32})")
META_GUID_RE = re.compile(rb"guid:\s*([0-9a-f]{32})")

# A shader that lights a surface, matched by name. The boundaries are not
# decoration: `lit` appears inside `Unlit`, `Blit` and `Split`, and `Standard`
# appears in `2DxFX_Standard_GrayScale`, which is a 2D sprite effect. Flat is
# tested first, so a name carrying both words lands on the side describing it.
LIT_SHADER_RE = re.compile(
    r"(?<![a-z])lit(?![a-z])|soft ?lit|(?<![a-z])standard(?![a-z])|diffuse|"
    r"specular|(?<![a-z])pbr(?![a-z])|toon|surface|3dmaterial", re.I)
FLAT_SHADER_RE = re.compile(
    r"sprite|2dxfx|textmeshpro|spine|particle|unlit|skybox|font|distance ?field|"
    r"blur|mask|gradient|blit|^hidden|^legacy|^ui[_ ]|[_ ]ui[_ ]", re.I)

SPRITE_RENDERER = 212
MESH_RENDERER = 23
SKINNED_MESH_RENDERER = 137

#: How many files each signal reads before it stops. A verdict does not get better
#: by scanning ten thousand prefabs, and the analysis proper is what should take
#: the time.
SAMPLE_LIMIT = 800


@dataclass
class Signal:
    """One measurement, its reading on a 0-1 scale, and the number behind it."""

    name: str
    score: float          # 0 = clearly 2D, 1 = clearly 3D
    weight: float
    detail: str


def _curve(value: float, low: float, high: float) -> float:
    """Map a measurement onto 0-1, flat outside the range the builds actually span."""
    if value <= low:
        return 0.0
    if value >= high:
        return 1.0
    return (value - low) / (high - low)


def mesh_detail(assets_root: Path) -> Signal:
    """Average mesh size. UI quads are kilobytes; sculpted geometry is not."""
    folder = assets_root / "Mesh"
    meshes = list(folder.glob("*.asset")) if folder.is_dir() else []
    if not meshes:
        return Signal("mesh detail", 0.0, 0.25, "no meshes at all")
    total = sum(m.stat().st_size for m in meshes)
    average = total / len(meshes) / 1024
    # 20 KB is a card or a quad; 300 KB is a character or a prop.
    return Signal("mesh detail", _curve(math.log10(max(average, 1)),
                                        math.log10(20), math.log10(300)), 0.25,
                  f"{len(meshes)} meshes, {average:.0f} KB average")


def _shader_names(assets_root: Path) -> dict[str, str]:
    """guid -> shader name, read from the .meta beside each exported shader."""
    folder = assets_root / "Shader"
    names: dict[str, str] = {}
    if not folder.is_dir():
        return names
    for shader in folder.glob("*.shader"):
        meta = shader.with_suffix(".shader.meta")
        try:
            found = META_GUID_RE.search(meta.read_bytes())
        except OSError:
            continue
        if found:
            names[found.group(1).decode()] = shader.stem
    return names


def lit_shaders(assets_root: Path) -> Signal:
    """Share of materials running on a shader that lights a surface."""
    folder = assets_root / "Material"
    materials = list(folder.glob("*.mat"))[:SAMPLE_LIMIT] if folder.is_dir() else []
    if not materials:
        return Signal("lit shaders", 0.0, 0.40, "no materials")

    names = _shader_names(assets_root)
    lit = flat = 0
    for path in materials:
        try:
            found = SHADER_REF_RE.search(path.read_bytes())
        except OSError:
            continue
        if not found:
            continue
        name = names.get(found.group(1).decode())
        if not name:
            continue                      # a built-in shader the export did not name
        if FLAT_SHADER_RE.search(name):
            flat += 1
        elif LIT_SHADER_RE.search(name):
            lit += 1
    named = lit + flat
    if not named:
        return Signal("lit shaders", 0.0, 0.40, "no named shaders to judge")
    share = lit / named
    return Signal("lit shaders", _curve(share, 0.05, 0.40), 0.40,
                  f"{lit} lit vs {flat} sprite/text shaders ({share:.0%} lit)")


def renderer_mix(assets_root: Path) -> Signal:
    """How the prefabs actually draw: mesh renderers against sprite renderers."""
    prefabs = list(assets_root.rglob("*.prefab"))[:SAMPLE_LIMIT]
    sprite = mesh = 0
    for path in prefabs:
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        for raw in CLASS_ID_RE.findall(blob):
            class_id = int(raw)
            if class_id == SPRITE_RENDERER:
                sprite += 1
            elif class_id in (MESH_RENDERER, SKINNED_MESH_RENDERER):
                mesh += 1
    total = sprite + mesh
    if not total:
        return Signal("renderer mix", 0.0, 0.25, "no renderers found")
    share = mesh / total
    return Signal("renderer mix", _curve(share, 0.05, 0.45), 0.25,
                  f"{mesh} mesh vs {sprite} sprite renderers ({share:.0%} mesh)")


def sprite_density(assets_root: Path) -> Signal:
    """Sprites per mesh. Weak on its own, useful as a tie-breaker.

    Counted from the export tree rather than the catalogue, because the profile has
    to be known before the indexing stage runs - the stages after it ask what kind
    of build this is.
    """
    def count(folder: str) -> int:
        target = assets_root / folder
        return sum(1 for _ in target.glob("*.asset")) if target.is_dir() else 0

    sprites, meshes = count("Sprite"), count("Mesh")
    if not meshes:
        return Signal("sprite density", 0.0, 0.10, f"{sprites} sprites, no meshes")
    ratio = sprites / meshes
    # Inverted: many sprites per mesh means 2D.
    return Signal("sprite density", 1.0 - _curve(math.log10(max(ratio, 1)),
                                                 math.log10(12), math.log10(120)),
                  0.10, f"{sprites} sprites / {meshes} meshes = {ratio:.0f}:1")


def detect(assets_root: Path, conn: sqlite3.Connection) -> dict:
    """Return the profile: a verdict, a score, and the evidence for both."""
    signals = [mesh_detail(assets_root), lit_shaders(assets_root),
               renderer_mix(assets_root), sprite_density(assets_root)]
    total_weight = sum(s.weight for s in signals)
    score = sum(s.score * s.weight for s in signals) / total_weight

    if score >= 0.55:
        verdict, note = "3d", "meshes and lit materials carry the art"
    elif score <= 0.30:
        verdict, note = "2d", "sprites carry the art"
    else:
        verdict, note = "mixed", "sprite and mesh art in comparable amounts"

    profile = {
        "verdict": verdict,
        "score": round(score, 3),
        "note": note,
        "signals": [{"name": s.name, "score": round(s.score, 3),
                     "weight": s.weight, "detail": s.detail} for s in signals],
    }
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('profile', ?)",
                 (json.dumps(profile),))
    conn.commit()
    return profile


def load(conn: sqlite3.Connection) -> dict | None:
    """Read a stored profile back, for stages that need to adapt to it."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'profile'").fetchone()
    return json.loads(row[0]) if row else None


def describe(profile: dict) -> str:
    lines = [f"profile   {profile['verdict'].upper()}  "
             f"(score {profile['score']:.2f} - {profile['note']})"]
    for signal in profile["signals"]:
        lines.append(f"    {signal['name']:<16} {signal['score']:.2f}  "
                     f"{signal['detail']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify an export as 2D or 3D.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    print(describe(detect(args.export.resolve(), conn)))
    conn.close()


if __name__ == "__main__":
    main()
