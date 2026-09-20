"""Stage 4 - classify by evidence instead of filename guessing.

Sources are applied strongest-first so a weak guess can never overwrite a fact:

1. project_dir  - the developer's own folders survived the rip (``_Studio/Gameplay/Items/CrateItem``)
2. bundle       - Addressables bundle names are developer labels (``ui_orderjourney``)
3. classid      - Unity component class ids in prefabs/scenes give the true role
4. graph        - roles propagate down GUID references to the sprites a prefab uses
5. filename     - naming-convention prefix and keyword tokens, last resort only
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
import sqlite3
from collections import defaultdict, deque
from pathlib import Path

from .core import FOLDER_TYPE, connect

# Unity component class ids are stable across versions.
CLASS_ROLE = {
    224: "UI",      # RectTransform - strongest UI signal
    222: "UI",      # CanvasRenderer
    223: "UI",      # Canvas
    198: "VFX",     # ParticleSystem
    199: "VFX",     # ParticleSystemRenderer
    23: "3D",       # MeshRenderer
    33: "3D",       # MeshFilter
    212: "World2D",  # SpriteRenderer
}
ROLE_PRIORITY = ["UI", "VFX", "World2D", "3D"]
CLASS_ID_RE = re.compile(rb"^--- !u!(\d+) &", re.MULTILINE)
NAME_PREFIX_RE = re.compile(r"^([a-z][a-z0-9]{2,})_")

# Feature prefixes seen in this corpus map 1:1 onto the Addressables bundles.
GENERIC_PREFIXES = {"common", "gameplay", "icon", "sprite", "texture", "img", "image", "new",
                    "level", "levelset", "atlas", "sheet", "temp", "test"}
HEX_NAME_RE = re.compile(r"^[0-9a-f]{8,}$")
# `level4`, `stage12`, and terse codes like `m139` are numbering, not feature names.
NUMBERED_PREFIX_RE = re.compile(
    r"^(?:(level|levelset|stage|scene|chapter|part|page)\d*|[a-z]\d+)$")

SUBCATEGORY_RULES = [
    ("Button", ("button", "btn")),
    ("Popup", ("popup", "dialog", "modal")),
    ("Panel", ("panel", "window", "frame", "container", "bg_", "background")),
    ("Icon", ("icon", "badge")),
    ("Progress", ("progress", "_bar", "meter", "slider", "fill")),
    ("Currency", ("coin", "currency", "cash", "money", "gold", "gem")),
    ("Reward", ("reward", "chest", "gift", "prize", "giftbox")),
    ("Booster", ("booster", "hammer", "rocket", "bomb", "shufle", "shuffle")),
    ("Particle", ("particle", "fx_", "spark", "shine", "glow", "smoke", "twinkle", "confetti")),
    ("Text", ("text", "label", "font", "title")),
    ("Decoration", ("deco", "decor", "border", "ribbon", "star", "ring")),
]

# Types whose role is settled by what they are, whatever references them. Data-heavy
# games ship thousands of TextAssets and ScriptableObjects; leaving those unlabelled
# was the single largest gap measured across the builds this was tested on.
TYPE_ROLE = {
    "AudioClip": "Audio",
    "Font": "Font",
    "Script": "Script",
    "Assembly": "Script",
    "Shader": "Shader",
    "ShaderVariantCollection": "Shader",
    "Material": "Material",
    "AnimationClip": "Animation",
    "AnimatorController": "Animation",
    "Mesh": "Mesh",
    "TextAsset": "Data",
    "MonoBehaviour": "Data",
}


# Gameplay-mechanic detection. The level corpus already names which layers are
# obstacles (Paper, Package, Safe, Window...), and those names line up with the
# developer's own art folders (Gameplay/Items/CrateItem), so obstacle art can be
# labelled from evidence instead of guessed at.
MECHANIC_DIRS = {"items", "item", "obstacles", "obstacle", "shelves", "shelf",
                 "boosters", "booster", "goal", "goals"}
# Last-resort words, used only when no level data or item folders exist.
OBSTACLE_WORDS = ("obstacle", "blocker", "crate", "chain", "cage", "vine", "curtain",
                  "blind", "cobweb", "wrapped")


# Split on separators and camelCase: `Blocks-Curtain-curtain_sheet_1` -> Blocks,
# Curtain, curtain, sheet, 1. Naming styles differ per studio, so the obstacle
# vocabulary from the level corpus is matched against every token, not just the first.
TOKEN_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def name_tokens(name: str) -> list[str]:
    return [token for token in TOKEN_SPLIT_RE.split(name) if token]


def normalise_mechanic(name: str) -> str:
    """`CrateItem`, `crateitem_icon`, `Crate3` -> `crate` so sources can be matched."""
    text = re.sub(r"[^a-z0-9]+", "", name.lower())
    text = re.sub(r"\d+$", "", text)
    for suffix in ("prefabs", "prefab", "views", "view", "items", "item"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    # One mechanic is routinely named twice over: a build's per-cell flag is
    # singular (`Curtain`) while the vector that places them is plural
    # (`Curtains`), and an enum and a folder disagree the same way. Folding a
    # trailing `s` merges those; `ss`, `us`, `is` and `as` are left alone so
    # `glass`, `status` and `canvas` keep their last letter.
    if len(text) > 4 and text.endswith("s") and not text.endswith(("ss", "us", "is", "as")):
        text = text[:-1]
    return text


GENERIC_MECHANIC_NAMES = {"view", "views", "prefab", "prefabs", "common", "base",
                          "default", "shared", "sprites", "textures", "art",
                          "config", "configs", "data", "settings", "resources"}


def mechanic_from_project_dir(rel_path: str) -> str | None:
    """`_Studio/Gameplay/Items/CrateItem/x.prefab` -> `CrateItem`."""
    parts = Path(rel_path).parts[:-1]
    for index, part in enumerate(parts[:-1]):
        if part.lower() in MECHANIC_DIRS:
            candidate = parts[index + 1].lstrip("_")
            if candidate and candidate.lower() not in GENERIC_MECHANIC_NAMES:
                return candidate
            return None
    return None


# IL2CPP games decompile to C# where the design vocabulary is written down as enums:
# `BoosterType` lists the boosters, and the board pieces live in the item/goal enums.
# Reading those beats guessing from art names, and the patterns are C# convention
# rather than any one game.
#
# Scanning only `StaticItemType`-style names badly undercounts: one build keeps its
# 20 cell overlays there but its 113 board pieces in `ItemType` and 105 clearable
# targets in `GoalType`, so a game with a new blocker every twenty levels looked
# like it had fifteen. The board vocabulary is the union of all three.
#: Enough of a file to tell whether it declares an enum at all.
HEAD_SCAN = 200_000
ENUM_RE = re.compile(r"\benum\s+(\w+)\s*\{(.*?)\}", re.S)
ENUM_MEMBER_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:=\s*-?\d+)?\s*,?\s*$", re.M)
BOOSTER_ENUM_RE = re.compile(r"booster", re.I)
BOARD_ENUM_RE = re.compile(
    r"(?:item|obstacle|static|overlay|blocker|goal|brick|block|cell)\w*type$", re.I)
# Every build also names menus, shops and telemetry with the same `...ItemType`
# suffix; those enums describe the interface, not the board, and letting them in
# turns shop rows and tooltip icons into obstacles.
NON_BOARD_ENUM_RE = re.compile(
    r"audio|haptic|reward|offer|shop|dialog|tooltip|icon|view|panel|button|section|"
    r"invite|metric|easing|bundle|event|feature|config|origin|sort|stat|mission|"
    r"package|privacy|term|scroll|card|tutorial|theme|music|particle|border|"
    r"currency|purchase|store|notification|analytic|log|error|state|anim|tween|"
    r"profile|inventory|sale|chat|team|player|row|daily|deal|warning|content|flow|"
    r"source|activation|atlas|mask|position|group|slot|fortune|modifier|special|"
    r"generated|layer|fade|admin|debug|test|"
    # `GoalUpdateType` is how a counter changes and `BlockUseType` is where a piece
    # is being shown; both end in a board word and describe neither.
    r"updatetype|usetype",
    re.I)

#: A single trailing letter on an enum member marks an orientation or colour variant
#: of one thing - `PAINT_BOTTLE_R/U/D/L`, `ROCKET_H/V`, `DEFAULT_R/P/Y/G/B`. Real
#: suffixes in the same vocabularies are never that short (`SPIKE_SUB`, `COP_BIT`).
VARIANT_SUFFIX_RE = re.compile(r"_[A-Za-z]$")
# Geometry and bookkeeping members carry no design meaning, and a directional name
# would otherwise swallow every `*_left` / `*_top` art file in the build.
IGNORED_ENUM_MEMBERS = {"none", "count", "max", "min", "default", "unknown", "obsolete",
                        "left", "right", "top", "bottom", "up", "down", "center",
                        "middle", "horizontal", "vertical", "first", "last", "all",
                        "normal", "small", "big", "large", "single", "double",
                        # The board's own colours are the match pieces, not blockers.
                        "match", "blue", "green", "orange", "red", "pink", "yellow",
                        "purple", "cyan", "white", "black", "brown", "grey", "gray",
                        "rainbow", "color", "colour", "random", "empty", "any",
                        "pair", "block", "item", "items", "cell", "tile", "goal"}



MAX_VOCABULARY_SPAN = 3


def match_vocabulary(name: str, vocabulary: dict[str, str]) -> str | None:
    """Key of the longest run of adjacent name tokens that spells a design word.

    The normalised key is returned rather than the text that spelled it, because
    `BirdNest`, `BirdNestItem` and `bird_nest_02` all name the same piece and the
    caller wants one answer for all three.
    """
    tokens = name_tokens(name)
    for span in range(min(MAX_VOCABULARY_SPAN, len(tokens)), 0, -1):
        for start in range(len(tokens) - span + 1):
            key = normalise_mechanic("".join(tokens[start:start + span]))
            if key in vocabulary:
                return key
    return None

def scan_design_enums(assets_root: Path) -> dict[str, dict[str, str]]:
    """Return {'Booster': {normalised: display}, 'Obstacle': {...}} from decompiled C#."""
    found: dict[str, dict[str, str]] = {"Booster": {}, "Obstacle": {}}
    scripts = assets_root / "Scripts"
    if not scripts.is_dir():
        return found
    # Every source file, because the enum's own name is what selects it and a build
    # is free to declare `ItemType` inside `Board.cs`. The substring test costs
    # nothing next to the read and skips the great majority.
    for path in scripts.rglob("*.cs"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "enum " not in text:
            continue
        for name, body in ENUM_RE.findall(text):
            if BOOSTER_ENUM_RE.search(name):
                bucket = "Booster"
            elif BOARD_ENUM_RE.search(name) and not NON_BOARD_ENUM_RE.search(name):
                bucket = "Obstacle"
            else:
                continue
            for member in ENUM_MEMBER_RE.findall(body):
                stem = VARIANT_SUFFIX_RE.sub("", member) if len(member) > 3 else member
                key = normalise_mechanic(stem)
                if (key and stem.lower() not in IGNORED_ENUM_MEMBERS
                        and member.lower() not in IGNORED_ENUM_MEMBERS
                        and len(key) > 2):
                    found[bucket].setdefault(key, stem)
    return found


# Not every build writes its design vocabulary into enums. One measured here has no
# `ItemType` at all - its nineteen `*Type*.cs` files are Adapty, haptics and
# notifications - and states its mechanics as folders and classes instead:
#
#     Scenes/Game/Mechanics/FrozenGroup/     LockAndKey/     MysteryGroup/
#     Scenes/Game/Mechanics/Booster/ChairBoosterStrategy.cs, KickBoosterStrategy.cs
#
# So the code tree is read as a second source. It is consulted after the enums and
# only adds names they did not already carry, because an enum is a declaration and
# a folder name is an inference.
MECHANIC_CONTAINER_RE = re.compile(
    r"(?:^|/)(Mechanics|Items|Blocks|Blockers|Obstacles|Boosters?|Powerups|"
    r"GameItems|BoardItems)$", re.I)
MECHANIC_CLASS_RE = re.compile(
    r"(Strategy|Group|Item|Blocker|Obstacle|Booster|Mechanic|View)$")
INTERFACE_RE = re.compile(r"^I[A-Z]")

# Plumbing that lives beside the mechanics without being one.
CODE_VOCABULARY_SKIP = {
    "config", "configs", "base", "common", "shared", "util", "utils", "utilities",
    "view", "views", "data", "intro", "intros", "factory", "manager", "controller",
    "state", "states", "board", "boosterinfo", "interfaces", "features",
    "animations", "property", "properties", "conditions", "groupconditions",
    "resources", "customitemresources", "checkpoint", "info", "selection",
    "handler", "helpers", "extensions", "blockmanager", "possiblematch", "match",
    "layout", "layouts", "queue", "boosters", "boosterselection",
}


#: An enum earns its place when this many of its members name something the build
#: ships. Measured over four builds: the real vocabularies land between 8 and 67
#: hits, and below six the list fills with incidental matches.
ENUM_EVIDENCE_HITS = 6


def own_assembly(scripts: Path) -> str | None:
    """The directory holding the project's own code, found by weight not by name.

    Unity compiles a project's own scripts into one assembly and every third-party
    package into its own, so the game's code is the largest single root - it is
    `Assembly-CSharp` in most builds and whatever the developer named it in others.
    Guessing the name would fail on the second kind; counting files does not.
    """
    counts: dict[str, int] = {}
    for path in scripts.rglob("*.cs"):
        parts = path.relative_to(scripts).parts
        if parts:
            counts[parts[0]] = counts.get(parts[0], 0) + 1
    return max(counts, key=counts.get) if counts else None


def asset_vocabulary(conn: sqlite3.Connection) -> set[str]:
    """Every word the build's own asset names are made of."""
    words: set[str] = set()
    try:
        rows = conn.execute("SELECT name FROM assets WHERE name IS NOT NULL")
    except sqlite3.OperationalError:
        return words
    for (name,) in rows:
        for part in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", name):
            key = normalise_mechanic(part)
            if len(key) > 2:
                words.add(key)
    return words


def scan_evidence_enums(assets_root: Path, conn: sqlite3.Connection) -> dict[str, str]:
    """Design enums recognised by their members naming the build's own art.

    Read only from the project's own assembly, because an SDK's enums match asset
    names too - `MarkupTag`, `JsonToken` and `PrimitiveTypeCode` all outscored one
    build's real vocabulary until the third-party code was excluded.
    """
    scripts = assets_root / "Scripts"
    if not scripts.is_dir():
        return {}
    root = own_assembly(scripts)
    if not root:
        return {}
    words = asset_vocabulary(conn)
    if not words:
        return {}

    found: dict[str, str] = {}
    for path in (scripts / root).rglob("*.cs"):
        try:
            with path.open("rb") as handle:
                if b"enum " not in handle.read(HEAD_SCAN):
                    continue
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, body in ENUM_RE.findall(source):
            if NON_BOARD_ENUM_RE.search(name):
                continue
            members = [member for member in ENUM_MEMBER_RE.findall(body)
                       if member.lower() not in IGNORED_ENUM_MEMBERS]
            keyed = [(normalise_mechanic(m), m) for m in members]
            hits = [(key, shown) for key, shown in keyed
                    if len(key) > 2 and key in words]
            if len(hits) >= ENUM_EVIDENCE_HITS:
                for key, shown in hits:
                    found.setdefault(key, shown)
    return found


def scan_code_mechanics(assets_root: Path) -> dict[str, dict[str, str]]:
    """Return {'Booster': {...}, 'Obstacle': {...}} read from the gameplay code tree."""
    found: dict[str, dict[str, str]] = {"Booster": {}, "Obstacle": {}}
    scripts = assets_root / "Scripts"
    if not scripts.is_dir():
        return found

    def offer(name: str) -> None:
        key = normalise_mechanic(name)
        if not key or len(key) < 3 or key in CODE_VOCABULARY_SKIP:
            return
        bucket = "Booster" if "booster" in key else "Obstacle"
        if key == "booster":
            return                      # the container, not a booster
        found[bucket].setdefault(key, name)

    for directory in scripts.rglob("*"):
        if not directory.is_dir():
            continue
        parent = str(directory.parent.relative_to(scripts)).replace("\\", "/")
        if MECHANIC_CONTAINER_RE.search(parent):
            offer(directory.name)

    for source in scripts.rglob("*.cs"):
        if INTERFACE_RE.match(source.stem):
            continue
        relative = str(source.parent.relative_to(scripts)).replace("\\", "/")
        if not (MECHANIC_CONTAINER_RE.search(relative)
                or MECHANIC_CONTAINER_RE.search(relative.rsplit("/", 1)[0])):
            continue
        match = MECHANIC_CLASS_RE.search(source.stem)
        if match and match.start() > 2:
            offer(source.stem[:match.start()])
    return found


# Layer names a Tiled board always has, whatever the game puts on them.
STRUCTURAL_LAYER_RE = re.compile(
    r"^(tile ?layer|layer|set|grid|board|base|background|bg|item|items|spawner|"
    r"spawn|objectives?|nonobjectives?|goal|goals|shelf|shelves|tutorial|"
    r"blueprint|drop ?zone|portal ?in|portal ?out)\s*\d*$", re.I)


def scan_level_vocabulary(conn: sqlite3.Connection) -> dict[str, str]:
    """Obstacle names the level files declare, as {normalised: display}.

    A designer who names a layer `BrickWall2` has declared an obstacle as plainly as
    an enum entry would. The trailing index is a placement counter - `BrickWall1` and
    `BrickWall2` are two walls in one level, not two kinds of wall - so it is folded
    away, and the structural layers every Tiled board carries are dropped.
    """
    found: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT obstacle_layers, object_layers FROM levels").fetchall()
    except sqlite3.OperationalError:
        return found

    seen: Counter[str] = Counter()
    display: dict[str, str] = {}
    for row in rows:
        for column in ("obstacle_layers", "object_layers"):
            for name in (row[column] or "").split(","):
                name = name.strip()
                if not name or STRUCTURAL_LAYER_RE.match(name):
                    continue
                stem = re.sub(r"[\s_-]*\d+$", "", name).strip()
                key = normalise_mechanic(stem)
                if len(key) < 3:
                    continue
                seen[key] += 1
                display.setdefault(key, stem)

    # One level naming a layer oddly is a typo; a name used across levels is a
    # mechanic. Two is enough to separate them without losing a rare obstacle.
    for key, count in seen.items():
        if count >= 2:
            found[key] = display[key]
    return found


def obstacle_families(conn: sqlite3.Connection) -> dict[str, str]:
    """normalised name -> display name, taken from the parsed level corpus."""
    families: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT obstacle_layers FROM levels WHERE obstacle_layers <> ''").fetchall()
    except sqlite3.OperationalError:
        return families
    for row in rows:
        for raw in row[0].split(","):
            display = re.sub(r"\d+$", "", raw.strip())
            key = normalise_mechanic(display)
            if key:
                families.setdefault(key, display)
    return families


def load_rules(path: Path | None) -> dict:
    """Optional per-game labelling layer.

    Everything above this point is derived from evidence in the build. A rules file
    only adds human knowledge on top - readable names for a feature, which features
    belong to the same part of the game, obstacles the level data does not name. It
    lives in a data file so the code stays free of per-game branches.

    Recognised keys: feature_aliases, feature_groups, mechanic_aliases,
    extra_obstacles, ignore_features.
    """
    if path is None or not Path(path).is_file():
        return {}
    try:
        rules = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"  warning: could not read rules {path}: {error}")
        return {}
    rules["_feature_alias_map"] = {
        key.lower(): value for key, value in (rules.get("feature_aliases") or {}).items()}
    rules["_mechanic_alias_map"] = {
        normalise_mechanic(key): value
        for key, value in (rules.get("mechanic_aliases") or {}).items()}
    rules["_group_of"] = {
        member.lower(): group
        for group, members in (rules.get("feature_groups") or {}).items()
        for member in members}
    rules["_ignore"] = {name.lower() for name in (rules.get("ignore_features") or [])}
    return rules


# Multi-part obstacle art is authored as edge and corner pieces that only make sense
# assembled: `paper_icon_top` is a 1217x60 strip on its own. Grouping them by their
# shared stem lets the browser lay the set out in its 3x3 arrangement.
POSITION_WORDS = {
    "topleft": "tl", "lefttop": "tl", "topright": "tr", "righttop": "tr",
    "bottomleft": "bl", "leftbottom": "bl", "bottomright": "br", "rightbottom": "br",
    "tl": "tl", "tr": "tr", "bl": "bl", "br": "br",
    "top": "t", "up": "t", "upper": "t", "bottom": "b", "down": "b", "lower": "b",
    "left": "l", "right": "r", "side": "l",
    "center": "c", "centre": "c", "middle": "c", "mid": "c",
}
VARIANT_TOKEN_RE = re.compile(r"^\d+$")
# A stem this generic describes the shape of a piece, not which obstacle it belongs
# to, so grouping on it merges unrelated art from all over the build.
GENERIC_STEMS = {"corner", "pin", "part", "parts", "edge", "bar", "line", "dot",
                 "piece", "pieces", "icon", "bg", "frame", "border", "side", "cap"}


def piece_of(name: str) -> tuple[str, str] | None:
    """('paper_icon_top', ...) -> (stem, 't'); None when the name has no position."""
    tokens = name_tokens(name)
    position, stem = None, []
    for index, token in enumerate(tokens):
        lowered = token.lower()
        # `top_left` written as two tokens should read as one corner.
        if position is None and index + 1 < len(tokens):
            pair = lowered + tokens[index + 1].lower()
            if pair in POSITION_WORDS and lowered in POSITION_WORDS:
                position = POSITION_WORDS[pair]
                continue
        if position is None and lowered in POSITION_WORDS:
            position = POSITION_WORDS[lowered]
            continue
        if lowered in POSITION_WORDS and position is not None:
            continue
        stem.append(lowered)
    # Only trailing numbers are variant markers (`..._part_3`). A leading one is an
    # identifier - `00300_A_left_arm` and `03300_A_right_arm` are different assets.
    while stem and VARIANT_TOKEN_RE.match(stem[-1]):
        stem.pop()
    return ("_".join(stem), position) if position and stem else None


def build_piece_groups(rows: list[sqlite3.Row], mechanics: dict[int, str]) -> list[dict]:
    buckets: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        found = piece_of(row["name"] or "")
        if not found:
            continue
        stem, position = found
        key = f"{mechanics.get(row['id']) or '-'}|{stem}"
        buckets[key].append((row["id"], position))
    groups = []
    for key, members in buckets.items():
        mechanic, stem = key.split("|", 1)
        # One piece is not a set, and neither is the same position repeated.
        if len(members) < 2 or len({position for _, position in members}) < 2:
            continue
        if len(stem) < 4 or (mechanic == "-" and stem in GENERIC_STEMS):
            continue
        groups.extend({"group_key": key, "asset_id": asset_id, "position": position}
                      for asset_id, position in members)
    return groups


def add(tags: list[dict], asset_id: int, kind: str, value: str, confidence: str, source: str) -> None:
    if value:
        tags.append({"asset_id": asset_id, "kind": kind, "value": value,
                     "confidence": confidence, "source": source})


# AssetRipper groups ripped assets into type-named folders; anything else at the
# top level is a folder the developer authored and therefore a real label.
ENGINE_ROOTS = set(FOLDER_TYPE) | {
    "scripts", "plugins", "resources", "editor", "packages", "streamingassets",
    "textmesh pro", "textmeshpro", "standard assets", "assetbundle", "prefabinstance",
    "shadervariantcollection", "spriteatlas", "lightingdatta", "navmeshdata",
}
GENERIC_DIRS = {"prefab", "prefabs", "sprites", "sprite", "textures", "texture",
                "assets", "materials", "material", "atlas", "atlases", "art",
                "images", "ui", "graphics", "gfx"}


def feature_from_project_dir(rel_path: str) -> tuple[str | None, str | None]:
    """`_Studio/Gameplay/Items/CrateItem/x.prefab` -> ('CrateItem', 'Items')."""
    parts = Path(rel_path).parts
    if len(parts) < 2 or parts[0].lower() in ENGINE_ROOTS:
        return None, None
    meaningful = [part for part in parts[1:-1] if part.lower() not in GENERIC_DIRS]
    if not meaningful:
        # A developer folder with no sub-structure still names the feature.
        stripped = parts[0].lstrip("_")
        return (stripped, None) if stripped else (None, None)
    return meaningful[-1], (meaningful[-2] if len(meaningful) > 1 else None)


def bundle_records(primary_content: Path | None) -> list[tuple[str, str]]:
    """(bundle name, project path) for every entry every bundle record names."""
    if not primary_content:
        return []
    bundle_dir = primary_content / "AssetBundle"
    if not bundle_dir.is_dir():
        return []
    records: list[tuple[str, str]] = []
    # A record is named after its bundle, `<bundle name>.json`; `.bundle.json` was only
    # ever one build's bundle naming, and matching it read nothing from any other.
    for path in sorted(bundle_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("m_AssetBundleName") or path.stem
        records.extend((name, project_path) for project_path in (data.get("m_Container") or {}))
    return records


def load_bundle_map(primary_content: Path | None) -> dict[str, tuple[str, str]]:
    """basename (lowercase) -> (bundle name, original project path).

    Bundle names in the Primary Content export are content hashes, so the useful
    label is the developer path recorded in ``m_Container``
    (``Assets/_Studio/LiveOps/SummerEvent/Assets/SummerEventAtlas.spriteatlas``).
    """
    mapping: dict[str, tuple[str, str]] = {}
    for name, project_path in bundle_records(primary_content):
        mapping[Path(project_path).stem.lower()] = (name, project_path)
    return mapping


#: How far a bundle's label is carried down from the asset its manifest names. Measured
#: on one build: four steps reach 546 of the 845 sprites nothing else labels, and the
#: count had stopped rising at five.
BUNDLE_DEPTH = 4


#: A SpriteAtlas asset, as the Unity 2020 packer and the V2 packer save it.
ATLAS_EXTENSIONS = (".spriteatlas", ".spriteatlasv2")


def bundle_reach(conn: sqlite3.Connection,
                 bundle_map: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """Carry each bundle's container path from the assets it names to what they use.

    A bundle manifest names only its top-level entries - an atlas, a dialog prefab - so
    matching by name labels the container and none of its contents. On one build that
    tagged 207 assets and left every one of the 845 unlabelled sprites unlabelled.

    Two edges carry the label down. One is the GUID references the asset graph already
    holds. The other is the page a sprite was packed on: Unity names a SpriteAtlas's
    pages `sactx-<page>-<size>-<format>-<AtlasName>-<hash>`, so an atlas the manifest
    names reaches every sprite on its pages even though the atlas asset itself is not in
    the export. That is Unity's naming, not a studio's. The nearest root wins, so an
    asset two bundles both reach takes the label of the one that holds it most directly.
    """
    if not bundle_map:
        return {}
    labels: dict[str, tuple[str, str]] = {}
    for row in conn.execute("SELECT guid, name FROM assets WHERE guid IS NOT NULL"):
        hit = bundle_map.get((row["name"] or "").lower())
        if hit:
            labels[row["guid"]] = hit
    atlases = {stem: value for stem, value in bundle_map.items()
               if value[1].lower().endswith(ATLAS_EXTENSIONS)}
    if atlases:
        for row in conn.execute(
                "SELECT guid, name FROM assets WHERE unity_type='Texture2D' "
                "AND name LIKE 'sactx-%' AND guid IS NOT NULL"):
            lowered = row["name"].lower()
            for stem, value in atlases.items():
                if f"-{stem}-" in lowered:
                    labels.setdefault(row["guid"], value)
                    break

    below: dict[str, list[str]] = defaultdict(list)
    for src, dst in conn.execute("SELECT src_guid, dst_guid FROM refs"):
        below[src].append(dst)
    for guid, page in conn.execute(
            "SELECT a.guid, s.atlas_guid FROM sprites s JOIN assets a ON a.id = s.asset_id "
            "WHERE a.guid IS NOT NULL AND s.atlas_guid IS NOT NULL"):
        below[page].append(guid)

    queue = deque((guid, 0) for guid in labels)
    while queue:
        guid, depth = queue.popleft()
        if depth >= BUNDLE_DEPTH:
            continue
        for child in below.get(guid, ()):
            if child not in labels:
                labels[child] = labels[guid]
                queue.append((child, depth + 1))
    return labels


def feature_from_container_path(project_path: str) -> str | None:
    """`Assets/_Studio/LiveOps/SummerEvent/Assets/x.spriteatlas` -> 'SummerEvent'."""
    skip = {"assets", "prefab", "prefabs", "sprites", "textures", "materials",
            "atlas", "atlases", "ui", "art", "resources"}
    parts = [part for part in Path(project_path).parts[:-1]
             if part.lower() not in skip and not part.startswith("_")]
    return parts[-1] if parts else None


def scan_prefab_roles(assets_root: Path, conn: sqlite3.Connection) -> dict[str, list[str]]:
    roles: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT guid, rel_path FROM assets WHERE unity_type IN ('Prefab','Scene') AND guid IS NOT NULL"
    ).fetchall()
    for row in rows:
        # Streamed, not read whole: a generated scene holds the same class ids as
        # a prefab and several hundred megabytes of them.
        found = {CLASS_ROLE[int(cid)]
                 for cid in scan_class_ids(assets_root / row["rel_path"])
                 if int(cid) in CLASS_ROLE}
        if found:
            roles[row["guid"]] = [role for role in ROLE_PRIORITY if role in found]
    return roles


def propagate(conn: sqlite3.Connection, holder_roles: dict[str, list[str]],
              max_depth: int = 6) -> tuple[dict[str, set[str]], list[dict]]:
    """Walk GUID references from each prefab/scene down to the assets it uses."""
    adjacency: dict[str, list[str]] = defaultdict(list)
    for src, dst in conn.execute("SELECT src_guid, dst_guid FROM refs"):
        adjacency[src].append(dst)

    holder_info = {
        row["guid"]: (row["name"], row["unity_type"])
        for row in conn.execute(
            "SELECT guid, name, unity_type FROM assets WHERE unity_type IN ('Prefab','Scene')"
        )
    }
    asset_id_by_guid = {
        row["guid"]: row["id"]
        for row in conn.execute("SELECT id, guid FROM assets WHERE guid IS NOT NULL")
    }

    inherited: dict[str, set[str]] = defaultdict(set)
    usage: list[dict] = []
    for holder, roles in holder_roles.items():
        seen = {holder}
        queue = deque((child, 1) for child in adjacency.get(holder, ()))
        name, holder_type = holder_info.get(holder, (None, None))
        while queue:
            guid, depth = queue.popleft()
            if guid in seen:
                continue
            seen.add(guid)
            inherited[guid].update(roles)
            if guid in asset_id_by_guid:
                usage.append({"asset_id": asset_id_by_guid[guid], "holder_guid": holder,
                              "holder_name": name, "holder_type": holder_type})
            if depth < max_depth:
                queue.extend((child, depth + 1) for child in adjacency.get(guid, ()))
    return inherited, usage



def mechanics_from_holders(usage: list[dict],
                           vocabulary: dict[str, str]) -> dict[int, str]:
    """asset id -> the one design word every prefab that uses it agrees on.

    Assets whose holders disagree are left alone: a shared glow or white pixel
    belongs to no single obstacle, and guessing one would scatter it across the
    obstacle list.
    """
    if not vocabulary:
        return {}
    word_of: dict[str, str] = {}
    votes: dict[int, set[str]] = defaultdict(set)
    for link in usage:
        holder = link.get("holder_name")
        if not holder:
            continue
        if holder not in word_of:
            word_of[holder] = match_vocabulary(holder, vocabulary) or ""
        if word_of[holder]:
            votes[link["asset_id"]].add(word_of[holder])
    return {asset_id: next(iter(words))
            for asset_id, words in votes.items() if len(words) == 1}

# Unity and its render pipeline packages ship art of their own, and an export cannot
# tell it apart from the studio's by folder - it all lands in Texture2D/ and Sprite/
# together. In the build measured here that is 33 dither tiles, a Bayer matrix and
# the Rendering Debugger's checkbox set: 45 of 405 previewable assets, all of them
# blank or near-blank grey squares, and all of them at the front of the grid because
# they sort small. Removing them is not tidying. It is the difference between a
# catalogue of a game and a catalogue of a game plus its engine.
#
# Two signals decide it, in order of strength:
#
#   1. **Who holds it.** The reference graph already knows that `UICheckMark` appears
#      only in `DebugUI*` prefabs and that `seperator` appears in `ShopDialog`. An
#      asset held exclusively by engine prefabs is engine art, whatever it is called.
#   2. **What it is called**, for the ones nothing holds at all - a dither tile is
#      bound by a shader, and a shader binds by property name, not by a guid the
#      graph can see.
#: Families the engine numbers, matched on the prefix - `LDR_LLL1_0` through
#: `LDR_LLL1_32` are one lookup table, not 33 assets worth listing separately.
ENGINE_PREFIX_RE = re.compile(
    r"^(LDR_LLL|BlueNoise|NoiseTex|OwenScrambled|ScrambleNoise|Default-|DebugUI|"
    r"FrameSettings|unity_builtin|UIFoldout|UISprite)", re.I)

#: Whole names, where a prefix test would catch the studio's own work: a game may
#: legitimately ship a sprite called `Background`, but not one called exactly `1x1`.
ENGINE_NAME_RE = re.compile(
    r"^(BayerMatrix|UIElement\d+px|UICheckMark|White1px|1x1|InputFieldBackground|"
    r"UIMask|DropdownArrow|Knob|Checkmark)$", re.I)

#: Prefabs that belong to a package rather than to the game.
ENGINE_HOLDER_RE = re.compile(r"^(DebugUI|SceneView|EditorOnly|Unity)", re.I)


def mark_origin(conn: sqlite3.Connection) -> dict[str, int]:
    """Label every asset 'game' or 'engine', and record how many things hold it."""
    holders: dict[int, list[str]] = {}
    for row in conn.execute("SELECT asset_id, holder_name FROM used_by"):
        holders.setdefault(row["asset_id"], []).append(row["holder_name"] or "")

    updates, counts = [], {"game": 0, "engine": 0, "unheld": 0}
    for row in conn.execute("SELECT id, name FROM assets"):
        held = holders.get(row["id"], [])
        name = row["name"] or ""

        if held:
            # Held only by engine prefabs, and by at least one of them.
            engine = all(ENGINE_HOLDER_RE.match(h) for h in held)
        else:
            engine = False
            counts["unheld"] += 1
        if not engine and (ENGINE_NAME_RE.match(name)
                           or ENGINE_PREFIX_RE.match(name)):
            engine = True

        origin = "engine" if engine else "game"
        counts[origin] += 1
        updates.append((origin, len(held), row["id"]))

    conn.executemany("UPDATE assets SET origin = ?, hold_count = ? WHERE id = ?",
                     updates)
    conn.commit()
    return counts


def classify(assets_root: Path, primary_content: Path | None,
             conn: sqlite3.Connection, rules_path: Path | None = None) -> dict[str, int]:
    bundle_map = load_bundle_map(primary_content)
    # The records themselves, queryable, and rebuilt with the tags they justify: which
    # bundle ships which project path. Tags alone said an asset came from a bundle
    # without the record to check it against.
    conn.execute("DELETE FROM bundles")
    conn.executemany("INSERT OR IGNORE INTO bundles (bundle_name, project_path) VALUES (?, ?)",
                     bundle_records(primary_content))
    rules = load_rules(rules_path)
    families = obstacle_families(conn)
    enums = scan_design_enums(assets_root)
    families.update(enums["Obstacle"])
    boosters = dict(enums["Booster"])

    # Third source, for a build that ships neither enums nor code: the level files.
    from_levels = scan_level_vocabulary(conn)
    for key, name in from_levels.items():
        families.setdefault(key, name)

    # Fourth source: enums the named rule does not recognise, kept only when the
    # build's own art names their members. Adds, never replaces.
    from_evidence = scan_evidence_enums(assets_root, conn)
    added = [name for key, name in from_evidence.items() if key not in families]
    for key, name in from_evidence.items():
        families.setdefault(key, name)
    if added:
        print(f"  evidence enums: {len(added)} more from the project's own assembly "
              f"({', '.join(sorted(added)[:8])})")

    # Second source, for builds whose vocabulary is in the code rather than an enum.
    from_code = scan_code_mechanics(assets_root)
    for key, display in from_code["Booster"].items():
        boosters.setdefault(key, display)
    for key, display in from_code["Obstacle"].items():
        if key not in boosters:
            families.setdefault(key, display)
    for extra in (rules.get("extra_obstacles") or []):
        families.setdefault(normalise_mechanic(extra), extra)
    for extra in (rules.get("extra_boosters") or []):
        boosters.setdefault(normalise_mechanic(extra), extra)
    if enums["Booster"] or enums["Obstacle"]:
        print(f"  design enums: {len(enums['Booster'])} boosters, "
              f"{len(enums['Obstacle'])} obstacles")
    if from_levels:
        print(f"  level layers: {len(from_levels)} obstacles "
              f"({', '.join(sorted(from_levels.values())[:8])})")
    if from_code["Booster"] or from_code["Obstacle"]:
        print(f"  gameplay code: {len(from_code['Booster'])} boosters, "
              f"{len(from_code['Obstacle'])} obstacles")
    holder_roles = scan_prefab_roles(assets_root, conn)
    inherited, usage = propagate(conn, holder_roles)
    carried = bundle_reach(conn, bundle_map)
    if bundle_map:
        print(f"  bundle provenance: {len(bundle_map)} container entries, carried to "
              f"{len(carried)} assets")
    vocabulary = {**families, **boosters}
    mechanic_by_graph = mechanics_from_holders(usage, vocabulary)

    tags: list[dict] = []
    primary_rows: list[dict] = []
    assets = conn.execute(
        "SELECT id, guid, rel_path, name, unity_type, ext FROM assets").fetchall()
    resolved = 0

    for row in assets:
        asset_id, guid = row["id"], row["guid"]
        rel_path, name = row["rel_path"], row["name"] or ""
        unity_type = row["unity_type"]
        lowered = name.lower()

        # 1. developer project folders
        primary_feature: str | None = None
        feature, group = feature_from_project_dir(rel_path)
        if feature:
            add(tags, asset_id, "feature", feature, "high", "project_dir")
            primary_feature = feature
            if group:
                add(tags, asset_id, "group", group, "high", "project_dir")

        # 2. Addressables bundle provenance: named by the manifest, or carried down to
        #    it from an asset the manifest names.
        bundle_role: str | None = None
        bundle_confidence = "high"
        entry = bundle_map.get(lowered)
        if not entry and guid:
            entry = carried.get(guid)
            bundle_confidence = "medium"
        if entry:
            bundle, project_path = entry
            add(tags, asset_id, "bundle", bundle, bundle_confidence, "bundle")
            add(tags, asset_id, "container_path", project_path, bundle_confidence, "bundle")
            container_feature = feature_from_container_path(project_path)
            if container_feature:
                add(tags, asset_id, "feature", container_feature, bundle_confidence,
                    "bundle")
                primary_feature = primary_feature or container_feature
            lowered_path = project_path.lower()
            if "/ui/" in lowered_path or "/liveops/" in lowered_path:
                bundle_role = "UI"
            elif "particle" in lowered_path or "/vfx/" in lowered_path:
                bundle_role = "VFX"

        # 3. filename convention, weakest evidence
        prefix = NAME_PREFIX_RE.match(lowered)
        if prefix:
            candidate = prefix.group(1)
            if (candidate not in GENERIC_PREFIXES and not HEX_NAME_RE.match(candidate)
                    and not NUMBERED_PREFIX_RE.match(candidate)):
                add(tags, asset_id, "feature", candidate, "medium", "filename")
                primary_feature = primary_feature or candidate
        for subcategory, tokens in SUBCATEGORY_RULES:
            if any(token in lowered for token in tokens):
                add(tags, asset_id, "subcategory", subcategory, "low", "filename")
                break

        # 3b. gameplay mechanic, and whether the level data calls it an obstacle
        primary_mechanic: str | None = None
        mechanic = mechanic_from_project_dir(rel_path)
        mechanic_source, mechanic_confidence = "project_dir", "high"
        if not mechanic and vocabulary:
            # Enum members are compounds - `DynamiteBox`, `BirdNest`, `IceCrusher` -
            # so a single token never matches them and only the plainest one-word
            # blockers were ever found. Runs of adjacent tokens are tried longest
            # first, so `dynamite_box_icon` binds to DynamiteBox and not to Box.
            found = match_vocabulary(name, vocabulary)
            if found:
                mechanic, mechanic_source, mechanic_confidence = (
                    found, "filename+design", "medium")
            elif asset_id in mechanic_by_graph:
                # An obstacle's art is usually packed onto an atlas named after
                # something else entirely, so its own name says nothing. What does
                # know is the prefab that uses it.
                mechanic, mechanic_source, mechanic_confidence = (
                    vocabulary[mechanic_by_graph[asset_id]], "graph", "medium")
        if mechanic:
            key = normalise_mechanic(mechanic)
            display = vocabulary.get(key, mechanic)
            add(tags, asset_id, "mechanic", display, mechanic_confidence, mechanic_source)
            primary_mechanic = display
            # A booster and a blocker are different things; the game's own enums say
            # which is which, so neither is inferred from art names when they exist.
            if key in boosters:
                add(tags, asset_id, "category", "Booster", "high", "enums")
            elif key in enums["Obstacle"]:
                add(tags, asset_id, "category", "Obstacle", "high", "enums")
            elif key in families:
                add(tags, asset_id, "category", "Obstacle", "high", "levels")
        elif not vocabulary and any(word in lowered for word in OBSTACLE_WORDS):
            add(tags, asset_id, "category", "Obstacle", "low", "filename")

        # 4. role, strongest evidence first. An asset whose Unity type already
        #    determines its role (audio, font, script...) must not be relabelled
        #    UI just because a UI prefab happens to reference it.
        role: str | None = TYPE_ROLE.get(unity_type)
        if role:
            add(tags, asset_id, "role", role, "high", "type")
        elif rel_path.startswith("Resources/levelset"):
            role = "Level"
            add(tags, asset_id, "role", role, "high", "project_dir")
        elif guid and guid in holder_roles:
            for value in holder_roles[guid]:
                add(tags, asset_id, "role", value, "high", "classid")
            role = holder_roles[guid][0]
        elif bundle_role:
            role = bundle_role
            add(tags, asset_id, "role", role, bundle_confidence, "bundle")
        elif guid and guid in inherited:
            ordered = [value for value in ROLE_PRIORITY if value in inherited[guid]]
            for value in ordered:
                add(tags, asset_id, "role", value, "medium", "graph")
            role = ordered[0] if ordered else None
        # An image nothing else placed is left unplaced. This used to call it UI and
        # file the guess as filename evidence, which made a catalogue read as fully
        # classified while up to half of one build's art had no evidence behind its
        # label at all. Unknown is an honest answer; a guess presented as a finding
        # is not.
        if role is None and (row["ext"] or "").lower() in {"json", "xml", "csv", "tsv", "txt"}:
            # Catalogs indexed before loose data files were typed still land here.
            role = "Data"
            add(tags, asset_id, "role", role, "medium", "type")
        if role is None and unity_type in {"Prefab", "Scene"}:
            # No renderer component anywhere in it: still a prefab, just not a visual
            # one (spawners, controllers, data holders).
            role = "Logic"
            add(tags, asset_id, "role", role, "medium", "classid")
        if role:
            resolved += 1
        # 5. optional per-game rules, applied last so they can only rename or group
        #    what the evidence already found.
        if rules:
            if primary_feature and primary_feature.lower() in rules["_ignore"]:
                primary_feature = None
            if primary_feature:
                primary_feature = rules["_feature_alias_map"].get(
                    primary_feature.lower(), primary_feature)
                group = rules["_group_of"].get(primary_feature.lower())
                if group:
                    add(tags, asset_id, "feature_group", group, "high", "rules")
            if primary_mechanic:
                primary_mechanic = rules["_mechanic_alias_map"].get(
                    normalise_mechanic(primary_mechanic), primary_mechanic)

        primary_rows.append({"id": asset_id, "primary_role": role,
                             "primary_feature": primary_feature,
                             "primary_mechanic": primary_mechanic})

    if rules:
        # Rewrite in place rather than adding alongside, so a renamed feature does
        # not show up twice and an ignored one really disappears.
        rewritten, seen = [], set()
        for tag in tags:
            if tag["kind"] == "feature":
                lowered = tag["value"].lower()
                if lowered in rules["_ignore"]:
                    continue
                tag["value"] = rules["_feature_alias_map"].get(lowered, tag["value"])
            elif tag["kind"] == "mechanic":
                tag["value"] = rules["_mechanic_alias_map"].get(
                    normalise_mechanic(tag["value"]), tag["value"])
            key = (tag["asset_id"], tag["kind"], tag["value"], tag["source"])
            if key not in seen:
                seen.add(key)
                rewritten.append(tag)
        tags = rewritten

    conn.execute("DELETE FROM tags")
    conn.execute("DELETE FROM used_by")
    conn.executemany(
        """UPDATE assets SET primary_role=:primary_role, primary_feature=:primary_feature,
               primary_mechanic=:primary_mechanic WHERE id=:id""",
        primary_rows)

    mechanic_of = {row["id"]: row["primary_mechanic"] for row in primary_rows}
    pieces = build_piece_groups(
        conn.execute("""SELECT id, name FROM assets
                         WHERE image_path IS NOT NULL AND name IS NOT NULL""").fetchall(),
        mechanic_of)
    conn.execute("DELETE FROM piece_groups")
    conn.executemany(
        """INSERT OR REPLACE INTO piece_groups (group_key, asset_id, position)
           VALUES (:group_key, :asset_id, :position)""", pieces)
    conn.executemany(
        """INSERT OR IGNORE INTO tags (asset_id, kind, value, confidence, source)
           VALUES (:asset_id, :kind, :value, :confidence, :source)""", tags)
    conn.executemany(
        """INSERT OR IGNORE INTO used_by (asset_id, holder_guid, holder_name, holder_type)
           VALUES (:asset_id, :holder_guid, :holder_name, :holder_type)""", usage)
    conn.commit()

    # Needs the finished used_by table: an asset's origin is decided by who holds it.
    origin = mark_origin(conn)
    return {"tags": len(tags), "usage_links": len(usage), "resolved": resolved,
            "total": len(assets), "prefabs_scanned": len(holder_roles),
            "engine": origin["engine"], "game": origin["game"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify assets by evidence.")
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--primary-content", type=Path, default=None,
                        help="Optional Primary Content Assets dir (for AssetBundle provenance)")
    parser.add_argument("--rules", type=Path, default=None,
                        help="per-game labelling rules; defaults to rules/<out name>.json")
    args = parser.parse_args()

    rules_path = args.rules or Path("rules") / f"{args.out.name}.json"
    if rules_path.is_file():
        print(f"rules      {rules_path}")
    conn = connect(args.out / "assetlab.db")
    stats = classify(args.export.resolve(),
                     args.primary_content.resolve() if args.primary_content else None,
                     conn, rules_path)
    print(f"origin     game={stats['game']}  engine={stats['engine']}")
    unresolved = stats["total"] - stats["resolved"]
    print(f"tags={stats['tags']}  usage_links={stats['usage_links']}  "
          f"prefabs_scanned={stats['prefabs_scanned']}")
    print(f"unclassified: {unresolved}/{stats['total']} "
          f"({unresolved / max(stats['total'], 1):.1%})")
    print("roles:")
    for row in conn.execute(
        """SELECT value, COUNT(DISTINCT asset_id) n FROM tags WHERE kind='role'
           GROUP BY value ORDER BY n DESC"""):
        print(f"  {row['value']:<12} {row['n']}")
    print("top features:")
    for row in conn.execute(
        """SELECT value, COUNT(DISTINCT asset_id) n FROM tags WHERE kind='feature'
           GROUP BY value ORDER BY n DESC LIMIT 12"""):
        print(f"  {row['value']:<20} {row['n']}")
    mechanics = conn.execute(
        """SELECT value, COUNT(DISTINCT asset_id) n FROM tags WHERE kind='mechanic'
           GROUP BY value ORDER BY n DESC LIMIT 20""").fetchall()
    if mechanics:
        obstacles = {row["value"] for row in conn.execute(
            """SELECT DISTINCT m.value FROM tags m
                 JOIN tags c ON c.asset_id = m.asset_id AND c.kind='category'
                                AND c.value='Obstacle'
                WHERE m.kind='mechanic'""")}
        print("mechanics (* = obstacle per the level corpus):")
        for row in mechanics:
            mark = "*" if row["value"] in obstacles else " "
            print(f"  {mark} {row['value']:<20} {row['n']}")
    conn.close()


if __name__ == "__main__":
    main()
