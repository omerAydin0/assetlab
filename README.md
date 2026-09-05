# AssetLab

Turns an Android Unity build into a searchable research library: individual sprites
cut out of their atlases, roles derived from Unity's own reference graph, rigged
animations reassembled from their prefabs, and any Tiled level corpus parsed into a
difficulty curve.

It works out what kind of build it is looking at first, and reads a build whose art
is geometry through its meshes and materials instead of through sprites it does not
have.

```
APK / APKM ──▶ ingest ──▶ staged input ──▶ ripper ──▶ Unity Project export ──▶ run ──▶ browser.html
             (assetlab.ingest)         (assetlab.ripper)                  (assetlab.run)
```

Three layers, each usable on its own: **ingestion** (`assetlab/ingest/`) understands
Android packaging, **ripper** (`assetlab/ripper.py`) drives AssetRipper headlessly,
**analysis** (`assetlab/*.py`) understands a Unity export. None of them knows anything
about a particular game.

## Scope

Personal reference and analysis tooling, for studying how shipped games are built.
It reads builds you supply and writes a local catalogue. Inputs are only ever read;
nothing is written back into them.

It deliberately does not: redistribute or upload extracted assets, decompile or
reconstruct game source, modify or re-sign packages, or touch anything server-side.
The catalogue it produces is for looking at, on the machine that produced it.

MIT licensed. The licence covers this code and nothing it reads: what a build
contains belongs to whoever made it, and extracting something does not change that.

## Requirements

Python 3.12, Pillow, numpy. Everything else is standard library — the catalogue is
SQLite and the browser is one self-contained HTML file per build.

AssetRipper is needed only for the export step, and only if you are starting from a
package rather than an existing export.

## Analysing a build

```bash
python -m assetlab.run --input <apk|apks|apkm|xapk|folder> --out out/<name> --title "<name>"
```

That is the whole thing. Discovery, staging, AssetRipper, three verification gates,
every analysis stage, and the combined hub page — one command, and nothing to
remember between steps. Staging goes to `staging/<name>` and the export to
`exports/<name>` unless `--staging` / `--export-dir` say otherwise; both are reused
on a second run, so re-analysing costs minutes rather than an hour. `--restage`
forces the expensive halves to run again.

It ends with a verdict rather than a wall of output:

```
OVERALL  PARTIAL
  unity assets       available
  asset graph        available
  sprite slicing     available
  il2cpp metadata    unavailable
  script vocabulary  unavailable
  level parser       detected, format unsupported
```

`READY` means nothing was missing. `PARTIAL` means the catalogue is real but named
things are absent. `BLOCKED` means a gate refused to continue, and says why and what
to do about it — the run stops there rather than filling a catalogue with confident
gaps.

Three reports land in `out/<name>/`: `discovery.json` (what is in the package),
`staging.json` (what was handed to AssetRipper, and why each file was chosen), and
`diagnostics.json` (every gate, every stage, timings, and the verdict). The
catalogue itself records its own provenance — source packages, packaging shape,
Unity version, ABI, whether IL2CPP metadata was available.

### Starting from an export you already have

```bash
python -m assetlab.run --export exports/<name>/ExportedProject/Assets --out out/<name>
```

## The steps on their own

Each stage is still a module, for when one of them is what you actually want.

```bash
python -m assetlab.ingest --input <package> --out staging/<name>
```

Resolves the shapes Android builds arrive in — a single APK, a base APK plus
split/ABI/asset-pack APKs, `.apkm`/`.xapk` archives containing those splits, or a
directory with any of that inside it — into one staged tree with a `manifest.json`
recording what was chosen and why.

Selection is by evidence, not filename: files are identified by magic bytes
(`UnityFS`, `\xaf\x1b\xb1\xfa` for il2cpp metadata, `\x7fELF`, `FSB5`, `PK\x03\x04`),
and each decision carries a confidence and the reason it was made. The Unity version
is read from the containers themselves, and when they disagree the larger payload
wins and the disagreement is reported. When several ABIs are present, one is picked
and the choice is recorded.

```bash
python -m assetlab.ripper --input staging/<name>/input --out exports/<name>
```

Drives AssetRipper's headless HTTP API and enforces the six settings the analysis
depends on: `BundledAssetsExportMode=DirectExport`, `SpriteExportMode=Yaml`,
`ImageExportFormat=Png`, `AudioExportFormat=Default`, `ScriptExportMode=Decompiled`,
`ScriptContentLevel=Level2`. Settings lock once a build is loaded, so the driver
resets first, then configures, then loads.

The last two matter more than they look. On `ScriptExportMode=Hybrid` - the
installation default - an export succeeds, Cpp2IL resolves every method, and not one
`.cs` file is written; classification then has no enum or type vocabulary to read and
the catalogue comes out looking merely sparse. One build in this corpus was exported
that way, and nothing in the chain objected until the outcome gate existed.

```bash
python -m assetlab.doctor --staging staging/<name> --export exports/<name>/ExportedProject/Assets
```

The gates, on demand. The staging gate checks what is about to be handed over;
the export gate checks what came back *against what went in* — an export that ran
without the IL2CPP metadata sitting in its own package produces no `Scripts/` tree
and a catalogue that merely looks thin, which is a failure worth being told about
rather than discovering weeks later. Add `--json <path>` for the machine-readable
form.

The third gate runs after the analysis, on the catalogue itself:

```bash
python -m assetlab.doctor --catalogue out/<name>
```

It reports where a build sits among its peers and asserts exactly one thing, because
exactly one comparison separates cleanly. Most cross-build numbers vary for honest
reasons - a game that animates in code has few clips, plain art classifies a smaller
share - so they are shown and left to a reader. The assertion is internal to the
build: a mechanic vocabulary tiny beside the feature vocabulary the same run
recovered. Across seven catalogues the sound builds sit between 0.134 and 0.463 and
the one whose script types never resolved sits at 0.007, so the cut at 0.05 accuses
neither a small game nor a plain one.

### The whole corpus at once

```bash
python -m assetlab.doctor --all
```

Re-runs every gate over every catalogue on disk and prints one table. This is the
regression that matters: the builds here share almost nothing about how they were
packaged, so the same detectors answering for all of them is the evidence that they
are general rather than tuned to whatever was analysed most recently. Each catalogue
is paired with the staging and export it came from using the provenance it records;
older catalogues are matched by folding case and separators in the directory name.
Add `--json <path>` to keep the result.

Levels get three answers, not two: parsed, detected in a format the parser does not
read, or genuinely absent. "Zero levels parsed" has meant all three at different
times, and they are not the same finding — one build in this corpus really does ship
no levels at all, because it fetches them from a server.

Two formats are read. Tiled tilemaps are parsed directly. FlatBuffers corpora — one
build ships 5,154 numbered `.bytes` files and another 14,650, with no header, no
magic and no field names — are read using the schema the build itself carries: an
IL2CPP export decompiles the generated accessor classes, and FlatBuffers assigns
vtable slots in declaration order, so `FTiledLevel`'s properties recover the layout.
That inference is checked rather than trusted; a schema is accepted only when it
fits the bytes, which is why reading one build's levels with another's table fails.

The obstacle vocabulary then falls out of the schema. A field is named for what it
holds, so the fields a level populates are that level's obstacle list — `Curtains`,
`ConveyorBelts`, `AncientVaults` at the top level, and per-cell flags like `Ice`,
`Chain` and `Ivy` one table down.

## Modules

Ingestion (`assetlab/ingest/`), independent of everything below it:

| module | what it does |
|---|---|
| `containers.py` | resolves an apk/apkm/folder into APK files on disk, de-duplicated |
| `detect.py` | magic sniffing and role detection, with evidence and confidence |
| `stage.py` | package/ABI selection, faithful extraction, `manifest.json` |

Export: `ripper.py` drives AssetRipper headlessly over its HTTP API.

Analysis, in the order `run.py` executes them:

| stage | module | what it does |
|---|---|---|
| 0 | `profile.py` | decides whether the art is sprites or geometry, from evidence |
| 1 | `index.py` | reads every `.meta` for guid↔path, measures images and audio |
| 2 | `graph.py` | pulls `guid:` references out of prefabs/scenes/materials into a graph |
| 3 | `slice_sprites.py` | crops each sprite out of its atlas, with pivot and nine-slice |
| 3b | `models.py` | maps mesh → material → texture/colour, for builds with 3D art |
| 4 | `levels.py` | parses a Tiled level corpus into a difficulty report |
| 5 | `animations.py` | reassembles clips into playable rigs from their prefabs |
| 6 | `classify.py` | assigns role/feature/mechanic from evidence, strongest source first |
| 7 | `dedup.py` | sha256 + difference hash, bucketed by dimensions |
| 8 | `browser.py` | thumbnails, media previews and a self-contained filterable browser |

`levels` runs before `classify` on purpose: the obstacle names it finds are what let
classification label obstacle art.

`hub.py` builds one page holding every catalogue at once.
`publish.py` assembles a portable copy for another machine.
`doctor.py` checks a staging tree and/or an export before you run.
`python -m assetlab.selftest` runs 102 analysis checks and 85 ingestion checks —
unit-level format sniffing, rig geometry, and three end-to-end synthetic package
layouts.

### Why sprite slicing matters

An export stores sprites as YAML metadata pointing into atlas pages, so browsing it
raw shows you atlas sheets, not art. Each sprite's `m_RD.textureRect` and texture GUID
locate it inside its page; slicing turns tens of thousands of metadata records into
individual images. Unity rects are bottom-left origin and Pillow is top-left, so the
crop box is `(x, H - y - h, x + w, H - y)`, and `settingsRaw` bits 2–5 carry a packing
rotation that has to be undone.

### Working out what kind of build it is

Every stage after the first one asks whether its work applies. A build whose art is
geometry has no atlas to cut and no sprite layers to compose, so the sprite stages
report zero — which reads as a broken input rather than a tool that does not fit.

The verdict is measured, and the obvious signals do not survive contact with real
builds. Camera projection is useless: a stylised 3D game renders through an
orthographic camera as readily as a 2D one, and the 3D build measured here has two
orthographic cameras and no perspective camera at all. Sprite-to-mesh asset ratio is
weak: the closest 2D build sits at 15 sprites per mesh and the 3D one at 11.

What separates them, over seven shipped builds:

| signal | 2D builds | the 3D build |
|---|---|---|
| average mesh size | 5–79 KB | 440 KB |
| materials on a lit shader | 0–2% | 88% |
| mesh renderers as a share of all renderers | 0–16% | 29% |

The shader signal is the decisive one and took two wrong turns to find. Counting
materials that *declare* a `_BumpMap` slot measures nothing — Unity serialises the
slot for every material on a lit-capable shader whether or not a texture is bound,
so both a 2D and a 3D build read as 100%. Counting materials with a normal map
actually bound measures nothing either: across all seven builds, including the 3D
one, that count is zero. That build gets its look from lit shaders and flat colour,
not from normal maps.

Matching shader names needs care in both directions. `lit` hides inside `Unlit`,
`Blit` and `Split`; `Standard` hides inside `2DxFX_Standard_GrayScale`, which is a
sprite effect. Flat is tested first so a name carrying both words lands on the side
that describes it.

Every signal is reported with the number behind it, and the score and verdict are
stored in the catalogue, so a page can say what it is looking at rather than leaving
an empty view unexplained.

### What a 3D build is read through

Where a 2D build has a sprite, a 3D build has a chain:

```
prefab ─▶ MeshFilter / SkinnedMeshRenderer ─▶ Mesh
            └──────▶ MeshRenderer ─▶ Material ─▶ Texture
                                        └─────▶ colour, when nothing is bound
```

That last branch is not an edge case. In the build this was written against, half
the models carry a lit shader, a base colour and no texture at all — the chairs, the
flowers and the ground are colour. A view that showed only textures would show half
the art.

Geometry itself is not rendered; the browser shows the surface a model is drawn
with, which is what a reference library is for. Rigged geometry
(`SkinnedMeshRenderer`) is marked as such.

### Why classification uses the graph

Filename tokens are the weakest possible evidence and a catalogue built on them ends
up half `Unknown`. Sources are consulted strongest first:

1. **Bundle provenance** — `m_Container` in the AssetBundle manifests gives the
   developer's own project path, which names the feature outright.
2. **Class ids in prefabs** — `--- !u!224` is a RectTransform and means UI, `198/199`
   is a ParticleSystem and means VFX, `212` a SpriteRenderer, `23/33` a mesh.
3. **Graph propagation** — roles and mechanics flow from a prefab down to everything
   it references, and `used_by` answers "which prefabs is this sprite in".
4. **Filename tokens** — last, and marked low confidence.

Across five builds this leaves 0.0–0.1% unclassified.

### Boosters, obstacles and other mechanics

Where a build's scripts decompile to C#, the design vocabulary is written down in
enums. Board pieces live in the item and goal enums, power-ups in a booster enum, and
reading those beats guessing from art names — the game already recorded which of its
pieces is a blocker and which is a power-up.

Two things make this work in practice. Enums that describe the interface rather than
the board — shop rows, tooltip icons, reward types — share the same naming suffix and
have to be filtered out, or menu art becomes obstacles. And enum members are compound
words, so matching one filename token at a time finds the generic half and never the
specific one; runs of adjacent tokens are tried longest-first instead.

Art whose own name says nothing — packed onto an atlas named after something else — is
named by the prefabs that use it, and only when every one of them agrees. A sprite
shared by two different blockers belongs to neither.

### Animations

An `AnimationClip` is a set of curves against object paths. Reassembling one into a
picture means resolving those paths against the prefab that uses the clip, and then
being right about the geometry:

- **Transform chains**, walked by fileID rather than name path, so two children with
  the same name stay distinct and the root's own offset and scale survive.
- **Pivot and pixels-per-unit** — a renderer puts the sprite's *pivot* on the
  transform and scales by `1/m_PixelsToUnits`. Assuming centre pivots and the default
  of 100 misplaces every off-centre sprite and draws the whole rig at the wrong size.
  One build authors at 140, and 6,392 of its sprites are not centre-pivoted.
- **Draw order** — sorting layer, then order in layer, then camera distance, then
  hierarchy order, with `SortingGroup` sorting a subtree as one unit.
- **Sliced and tiled renderers**, which ignore the sprite's own size and stretch a
  nine-slice into `m_Size`.
- **Flips, colour tint, `m_IsActive` toggles and `m_Color.a` fades**, so alternate
  states and hidden parts are not all drawn at once. Reading only the alpha of
  `m_Color` renders a black 95%-opacity dimmer as an opaque white slab.
- **SpriteMask**, resolved from the nearest mask above or beside a renderer, so a reel
  far larger than the window it shows through gets clipped.
- **Animator-root scoping** — a clip only draws art beneath its own animator root.
  Without it a prefab's neighbours bleed into the picture.

Motion is sampled linearly between keyframes; Unity eases with bezier tangents, so
this is an approximation. Additive blending, shaders and particle systems are not
reproduced.

Across five builds: 3,548 clips, of which 384 are sprite-swap sequences and 1,955
compose into rigs totalling 36,235 layers.

### Obstacles

The browser has a second view listing obstacles as families rather than loose sprites
— 398 families over five builds, 10,356 pieces. Opening one shows two things:

- **Final form** — the composed rig: the object assembled with every sprite in place,
  which no single image in the set shows. Clips are ranked so an idle or tap state is
  preferred over an explosion, and a clip carrying a small fraction of the object's
  art is rejected however it is named.
- **Pieces** — the sprites that rig draws, which is by definition what the object is
  made of. Evidence, rather than a guess about names.

Raw atlas pages are excluded from both: they are the sheet the art was cut from, and
being the largest images in a set they would otherwise headline every card.

### What the browser shows

| kind | preview |
|---|---|
| image | full-resolution sprite or atlas page |
| audio | player, with duration / sample rate / channels |
| animation | frame sequence, or a composed rig with play/stop |
| text | inlined excerpt (`.json`, `.bytes`, `.cs`, `.shader`, `.prefab`, `.mat`, …) |
| font | sample text rendered in the actual typeface |
| model | the materials a mesh is drawn with: textures, or the colour when there is none |

Live search, faceted filters whose counts reflect every other active filter, a lazy
grid, and a detail panel listing which prefabs and scenes use the asset. The header
states the detected profile, and the models tab appears only for a build that has a
mesh chain behind it — an empty view is worse than an absent one.

### One page for every build

```bash
python -m assetlab.hub --out out
```

One page holding every catalogue with a tick box each. It carries only previewable
media — text excerpts and usage lists stay in the per-build pages, which remain the
complete view — which keeps five builds at 33,374 assets in 28 MB and about two
seconds to load, so cross-build comparison is one click.

### Per-build labelling

`rules/<name>.json` can rename, group or drop labels for one build, and add design
vocabulary a build's scripts do not spell out. It is optional and additive: rules
never invent a classification, they only adjust what the evidence produced.
`rules/example.json` documents the shape.

## Using it on another build

Nothing above is specific to any title. The stages key off Unity's own formats:
`.meta` guids, `guid:` references, `m_RD.textureRect`, class ids, `m_Container`,
Tiled JSON. A different build changes the numbers, not the method.

Practically: ingest, export, `doctor`, run. If the build ships no Tiled corpus, stage
4 reports zero and everything else is unaffected.

### Known limits

- Sprites the game streams after install have no atlas in the package. They are
  counted as `no_atlas` rather than guessed at.
- UI built on `RectTransform` lays out in screen space against a rect, not in world
  units, so it is excluded from rig composition rather than composed wrongly.
- A handful of SpriteMasks are matched by sorting range rather than hierarchy and are
  left unclipped.
- Bezier easing, additive blending, shaders and particles are not reproduced.
- Mesh geometry is catalogued and described, not rendered. A model is shown
  through its materials.

## Output

```
out/<name>/
  assetlab.db        catalogue: assets, refs, tags, used_by, sprites, levels,
                     animations, piece_groups, models, bundles, meta (profile)
  sprites/           one image per sprite, cut from its atlas
  thumbs/  media/    grid thumbnails; audio and fonts copied for playback
  browser.html       the library, self-contained
  levels_report.md   difficulty curve, when the build ships a corpus
out/hub.html         every build in one page
```

## Results

Five Android builds, taken from package to browser:

| | total |
|---|---|
| assets catalogued | 102,476 |
| sprites cut from atlases | 28,437 |
| reference edges | 125,253 |
| usage links | 125,799 |
| animation clips | 3,548 — 384 sprite-swap, 1,955 rigged |
| rig layers composed | 36,235 |
| obstacle families | 398, holding 10,356 pieces |
| levels parsed | 8,855, across the two builds that ship a Tiled corpus |
| unclassified | 0.0 – 0.1% |

A full run is about five minutes per build, most of it image work in slicing and
thumbnails.

One build's corpus ships three A/B variants of the same 205 levels. Across the first
180: depth layers ramp 3.3 → 6.2 while distinct item types *drop* 5.95 → 4.0, obstacle
tiles climb 17 → 57, and the time limit flattens near 160 s after level 20. Levels
carry placement and distribution seeds, so that build generates procedurally too.

## Publishing to another machine

`out/` is not portable: its per-build pages link a few thousand images straight into
the machine's export tree. `publish` bakes those into the bundle and rewrites the
links.

```bash
python -m assetlab.publish --out out --dist dist
```

`dist/` is self-contained — pages, sprites, thumbnails, previews, audio and fonts; no
database and no code. Copy it and open `hub.html`; no server, no install. The linked
originals are 1,161 MB of atlas sheets, so each is baked to a 1024 px WebP instead:
39 MB. Re-running after a rebuild copies only what changed.

Move it with something that keeps the files on hardware you own — a sync tool between
your own devices, a network share, an external drive.
