"""What counts as one object, and which sprites it is made of.

The browser used to split an obstacle's art into "whole" and "pieces" with a list of
words - *frag*, *shard*, *spark*. That list is a guess about vocabulary, and it reads
a balloon dog's ear as a whole object because nobody called it a fragment. Measured
on one build it put 72 of Balloon's 85 sprites on the "whole" side, every one of them
a limb, and left DragonToy with no whole at all.

Two kinds of evidence replace it, and neither needs a word list.

The first is the artists' own naming. A part's name extends the whole's: `TB_dog`
owns `TB_dogEar_1`, `TB_dogNose`, `TB_dogTail`. Compared as token lists rather than
raw strings, so `coin` does not swallow `coinage`.

The second is the animation. Where the build ships no sprite for the assembled thing
- DragonToy is thirteen loose limbs and no dragon - a clip that draws those thirteen
at once has already stated they are one object, more reliably than any name could.

Sprites that neither own parts nor belong to anything are objects on their own. That
is the honest answer for a row of coins: nine variants, not nine pieces of one coin.

Most of the work is in refusing the shapes that only look like decompositions, and
each rule below was written against a case a real build produced:

* A stem must be long enough to be a name. `d1_E_c` starts with the token `d`, and a
  build that ships the letter D as a sprite had it claiming 643 unrelated sprites.
* A numbered run names one thing many times - `Puzzle_Cube_01` to `_48` - so its
  members must reduce to more than one kind.
* A variant matrix names a few things many times over. 225 puzzle sprites reduce to
  thirteen kinds; twelve dog parts reduce to nine. Real parts appear about once each.
* A whole is assembled from its parts, so it is not smaller than any of them. `Gem`
  at 57x56 does not contain `gem_item_AO` at 256x256, and `Top` at 47x95 does not
  contain `TopBar_MainPanel` at 1458x250. The art says so; no vocabulary is involved.
"""
from __future__ import annotations

import re
from collections import defaultdict

#: The seams an artist actually types: a separator, the join between a lower-case run
#: and a capital, and the join between a letter and a digit.
WORD_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])")


#: A name has to carry enough to be one. Splitting at the letter/digit seam leaves
#: `d1_E_c` starting with the token `d`, and a build that ships the letter D as a
#: sprite then had it claiming 643 unrelated sprites as its parts.
MIN_STEM = 3

#: A clip drawing more sprites than this is animating a place, not a thing. Two
#: measurements that looked promising did not survive contact with the builds: the
#: pieces of a scene are no more spread out than an object's, because a scene's
#: backdrop is itself one of the pieces and sets the scale; and a scene's largest
#: piece is not reliably larger relative to its median either - one build's furnished
#: room scores 19 where a cocktail glass scores 14. What does separate them is size.
#: Across six builds 1,998 rigged clips draw 49 distinct sprites or fewer and 20 draw
#: more, with the histogram falling from 59 clips in the 40s to 4 in the 50s. A
#: dragon is thirteen limbs; a district's fountain is 338 sprites of bridge, water and
#: lotuses laid out across a map, and no one looks at that as one object.
MAX_PIECES = 49


def name_tokens(name: str) -> tuple[str, ...]:
    """`Blocks-Balloon-blue-blueTB_dogEar_1` -> blocks balloon blue blue tb dog ear 1."""
    return tuple(part.lower() for part in WORD_RE.split(name or "") if part)


def _shared_name(names: list[str]) -> str:
    """The label a headless set shares.

    Taken from most of the set rather than all of it. One outlier - a backdrop filed
    beside sixteen leaves - drags a strict common prefix back to the folder name, so
    a bush at stage 2B and the koala in front of it both end up called `Blocks-Koala`.
    """
    if not names:
        return "object"
    if len(names) == 1:
        return names[0]
    quorum = max(2, len(names) * 3 // 5)
    best = 0
    for pivot in names[:40]:
        lengths = []
        for other in names:
            limit = 0
            while (limit < len(pivot) and limit < len(other)
                   and pivot[limit] == other[limit]):
                limit += 1
            lengths.append(limit)
        lengths.sort(reverse=True)
        share = lengths[quorum - 1]
        if share > best:
            best, stem = share, pivot
    if not best:
        return min(names, key=len)
    return stem[:best].rstrip("-_. (") or min(names, key=len)


def _kinds(words: list[tuple[str, ...]]) -> set[tuple[str, ...]]:
    """Children reduced to what they are, with the copy number dropped."""
    kinds = set()
    for word in words:
        trimmed = list(word)
        while trimmed and trimmed[-1].isdigit():
            trimmed.pop()
        kinds.add(tuple(trimmed))
    return kinds


def group_objects(records: list[dict], rigs: list[tuple[int, list[str]]]) -> list[dict]:
    """Group a catalogue's sprites into objects.

    ``records`` is the browser's asset list; ``rigs`` pairs a clip's index with the
    image paths its layers draw. Returns one entry per object::

        {"n": label, "w": whole index or None, "p": [part indices],
         "a": atlas record index or None, "c": clip index or None, "fam": label}

    Ordering is by how much art the object accounts for, so the sets worth looking at
    come first and the singletons trail behind.
    """
    by_image = {}
    for index, record in enumerate(records):
        if record.get("img") and record["img"] not in by_image:
            by_image[record["img"]] = index
    sprites = [index for index, record in enumerate(records)
               if record.get("img") and record.get("kind") == "image"
               and not record.get("at")]
    tokens = {index: name_tokens(records[index]["n"]) for index in sprites}

    # Longest name wins a tie, so `dogEar_1` attaches to `dog` and not to `` - and a
    # duplicate name cannot claim itself as its own parent.
    owner_of: dict[tuple[str, ...], int] = {}
    for index in sorted(sprites, key=lambda i: records[i]["n"]):
        if len("".join(tokens[index])) >= MIN_STEM:
            owner_of.setdefault(tokens[index], index)

    parent: dict[int, int] = {}
    for index in sprites:
        word = tokens[index]
        for cut in range(len(word) - 1, 0, -1):
            owner = owner_of.get(word[:cut])
            if owner is not None and owner != index:
                parent[index] = owner
                break

    # A chain is flattened to its root. `dogNoseBall` extends `dogNose`, which extends
    # `dog`, but a nose ball is not an object with a nose ball in it - it is one more
    # piece of the dog, and nesting it away leaves the dog missing two of its twelve.
    def root(index: int) -> int:
        seen = {index}
        while index in parent and parent[index] not in seen:
            index = parent[index]
            seen.add(index)
        return index

    children: dict[int, list[int]] = defaultdict(list)
    for child in parent:
        owner = root(child)
        if owner != child:
            children[owner].append(child)

    # A run of variants is not a decomposition. Two things separate them, and both
    # are needed. A decomposition names several different things - the dog has a nose,
    # a neck and a tail, where `Puzzle_Cube_01` to `_48` name one cube forty-eight
    # times. And it names each of them about once: the dog's twelve parts reduce to
    # nine kinds, while a build's 225 puzzle sprites reduce to thirteen, because they
    # are one cube in five colours across many states rather than 225 pieces of one
    # puzzle. Either test alone lets the other case through.
    def area(index: int) -> int:
        return (records[index].get("w") or 0) * (records[index].get("h") or 0)

    for owner in list(children):
        kinds = _kinds([tokens[index] for index in children[owner]])
        if len(kinds) < 2 or len(children[owner]) > 3 * len(kinds):
            del children[owner]
            continue
        # A whole is assembled from its parts, so it is not smaller than any of them.
        # A name that merely happens to sit in front of others fails this at once -
        # `Gem` (57x56) does not contain `gem_item_AO` (256x256), and `Top` (47x95)
        # does not contain `TopBar_MainPanel` (1458x250). No word list says why; the
        # art does.
        whole = area(owner)
        if whole and any(area(index) > whole for index in children[owner]):
            del children[owner]

    objects: list[dict] = []
    home: dict[int, int] = {}          # sprite index -> objects[] position
    for whole in sorted(children, key=lambda i: records[i]["n"]):
        parts = sorted(children[whole], key=lambda i: records[i]["n"])
        home[whole] = len(objects)
        for part in parts:
            home[part] = len(objects)
        objects.append({"n": records[whole]["n"], "w": whole, "p": parts,
                        "c": None, "fam": records[whole].get("mechanic")
                        or records[whole].get("feature")})

    # A clip either names the final form of an object already found, or - where the
    # build ships no sprite for the assembled thing - defines one.
    for clip, images in rigs:
        drawn = [by_image[path] for path in dict.fromkeys(images) if path in by_image]
        drawn = [index for index in drawn if index in tokens]
        if not 2 <= len(drawn) <= MAX_PIECES:
            continue
        seats = defaultdict(int)
        for index in drawn:
            if index in home:
                seats[home[index]] += 1
        if seats:
            best = max(seats, key=seats.get)
            # Half the art it draws must belong to that object, or the clip is a
            # scene that happens to include it rather than a picture of it.
            if seats[best] * 2 >= len(drawn):
                if objects[best]["c"] is None:
                    objects[best]["c"] = clip
                continue
        loose = [index for index in drawn if index not in home]
        if len(loose) < 2:
            continue
        for index in loose:
            home[index] = len(objects)
        # Where the pieces share no name worth printing - a set whose members are
        # `0`, `02`, `07` - the clip's own name is what the studio called the thing.
        label = _shared_name([records[i]["n"] for i in loose])
        if len(label.strip("-_. ")) < MIN_STEM:
            label = records[clip]["n"] or label
        objects.append({"n": label,
                        "w": None, "p": sorted(loose, key=lambda i: records[i]["n"]),
                        "c": clip, "fam": records[loose[0]].get("mechanic")
                        or records[loose[0]].get("feature")})

    for index in sprites:
        if index in home:
            continue
        home[index] = len(objects)
        objects.append({"n": records[index]["n"], "w": index, "p": [], "c": None,
                        "fam": records[index].get("mechanic")
                        or records[index].get("feature")})

    # The sheet an object was cut from: whichever one holds most of its sprites.
    for entry in objects:
        members = ([entry["w"]] if entry["w"] is not None else []) + entry["p"]
        sheets = defaultdict(int)
        for index in members:
            page = records[index].get("ax")
            if page is not None:
                sheets[page] += 1
        entry["a"] = max(sheets, key=sheets.get) if sheets else None

    objects.sort(key=lambda e: (-(len(e["p"]) + (1 if e["w"] is not None else 0)),
                                e["n"].lower()))
    return objects
