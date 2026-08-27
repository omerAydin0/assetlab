# AssetLab

Turns an Android Unity build into a searchable research library: individual sprites
cut out of their atlases, roles derived from Unity's own reference graph, rigged
animations reassembled from their prefabs, and any Tiled level corpus parsed into a
difficulty curve.

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

No licence is granted. This is published as a record of the engineering.

## Requirements

Python 3.12, Pillow, numpy. Everything else is standard library — the catalogue is
SQLite and the browser is one self-contained HTML file per build.

AssetRipper is needed only for the export step, and only if you are starting from a
package rather than an existing export.

## Step 1 — ingest the Android build

```bash
python -m assetlab.ingest --input <apk|apkm|xapk|folder> --stage staging/<name>
```

Resolves the three shapes Android builds arrive in — a single APK, a base APK plus
split/ABI/asset-pack APKs, and `.apkm`/`.xapk` archives containing those splits — into
one staged tree with a `manifest.json` recording what was chosen and why.

Selection is by evidence, not filename: files are identified by magic bytes
(`UnityFS`, `\xaf\x1b\xb1\xfa` for il2cpp metadata, `\x7fELF`, `FSB5`, `PK\x03\x04`),
and each decision carries a confidence and the reason it was made. When several ABIs
are present, one is picked and the choice is recorded.

## Step 2 — export with AssetRipper

```bash
python -m assetlab.ripper --input staging/<name> --out exports/<name>
```

Drives AssetRipper's headless HTTP API and enforces the four settings the analysis
depends on: `BundledAssetsExportMode=DirectExport`, `SpriteExportMode=Yaml`,
`ImageExportFormat=Png`, `AudioExportFormat=Default`. Settings lock once a build is
loaded, so the driver resets first, then configures, then loads.

## Step 3 — check before you run

```bash
python -m assetlab.doctor --stage staging/<name> --export exports/<name>
```

A five-minute run is a long way to travel to find out the export was made with the
wrong sprite mode. `doctor` samples the tree and reports what it found — sprite YAML
with usable rects, atlas textures, prefabs, a level corpus — before you commit.

## Step 4 — analyse

```bash
python -m assetlab.run --export exports/<name>/ExportedProject/Assets --out out/<name>
```

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
| 1 | `index.py` | reads every `.meta` for guid↔path, measures images and audio |
| 2 | `graph.py` | pulls `guid:` references out of prefabs/scenes/materials into a graph |
| 3 | `slice_sprites.py` | crops each sprite out of its atlas, with pivot and nine-slice |
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
`python -m assetlab.selftest` runs 54 analysis checks and 43 ingestion checks —
unit-level format sniffing, rig geometry, and three end-to-end synthetic package
layouts.

### Why sprite slicing matters

An export stores sprites as YAML metadata pointing into atlas pages, so browsing it
raw shows you atlas sheets, not art. Each sprite's `m_RD.textureRect` and texture GUID
locate it inside its page; slicing turns tens of thousands of metadata records into
individual images. Unity rects are bottom-left origin and Pillow is top-left, so the
crop box is `(x, H - y - h, x + w, H - y)`, and `settingsRaw` bits 2–5 carry a packing
rotation that has to be undone.

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

Live search, faceted filters whose counts reflect every other active filter, a lazy
grid, and a detail panel listing which prefabs and scenes use the asset.

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

## Output

```
out/<name>/
  assetlab.db        catalogue: assets, refs, tags, used_by, sprites, levels,
                     animations, piece_groups, bundles
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
