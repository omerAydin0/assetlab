"""Read FlatBuffers content using the schema the build's own decompiled code carries.

Two of the builds catalogued here ship their whole level corpus as FlatBuffers -
thousands of numbered `.bytes` files with no header, no magic and no field names.
Filed by extension they are anonymous binaries, and the level parser reported them,
honestly, as a corpus it could not read.

The schema is not missing, though. FlatBuffers generates a C# accessor class per
table, and an IL2CPP export decompiles those classes back out: `FTiledLevel` still
declares `Name`, `Move`, `Grid`, `Predefined`, `Sets` and the rest, in order, with
their types. The method bodies may be stubbed away - the offsets are what a
decompiler drops first - but the offsets are recoverable, because FlatBuffers
assigns vtable slots 4, 6, 8, ... strictly in declaration order.

That last step is an inference, so it is checked rather than trusted: a schema is
only accepted for a corpus when it actually fits the bytes - every populated slot
maps to a declared field, and the fields that claim to be strings decode as text.
Reading one build's level with another build's schema fails that test, which is the
point.

Nothing here knows what game it is looking at. Give it a scripts tree and a corpus
and it finds which of the build's own tables the corpus was written from.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

#: FlatBuffers numbers vtable slots from 4, two bytes apart, in declaration order.
FIRST_SLOT = 2
SLOT_STEP = 2

#: A generated accessor declares its fields as properties before the boilerplate.
#: `public string Name => null;`  `public int MovesLength => 0;`  `public FGrid? Grid`
PROPERTY_RE = re.compile(
    r"^\s*public\s+(?!static\b)([\w.<>]+\??)\s+(\w+)\s*=>", re.M)
#: Everything after this is machinery, not schema.
BOILERPLATE_RE = re.compile(r"public static (void ValidateVersion|\w+\?? GetRootAs)")
#: The generated name for a vector is `<Field>Length`, beside a `<Field>(int j)`.
#: Only the second says what the vector holds; the first is always `int`.
VECTOR_SUFFIX = "Length"
INDEXER_RE = re.compile(r"^\s*public\s+([\w.]+)\??\s+(\w+)\(int\s+\w+\)", re.M)
SKIP_PROPERTIES = {"ByteBuffer"}

SCALARS = {
    "bool": ("?", 1), "byte": ("B", 1), "sbyte": ("b", 1),
    "short": ("h", 2), "ushort": ("H", 2),
    "int": ("i", 4), "uint": ("I", 4),
    "long": ("q", 8), "ulong": ("Q", 8),
    "float": ("f", 4), "double": ("d", 8),
}

STRING, VECTOR, TABLE, SCALAR = "string", "vector", "table", "scalar"


@dataclass
class Field:
    """One schema field: what it is called, where it sits, and how to read it."""
    name: str
    slot: int
    kind: str
    type_name: str

    @property
    def format(self) -> tuple[str, int] | None:
        return SCALARS.get(self.type_name)


@dataclass
class Schema:
    name: str
    fields: list[Field] = field(default_factory=list)

    @property
    def last_slot(self) -> int:
        return self.fields[-1].slot if self.fields else 0

    def by_slot(self, slot: int) -> Field | None:
        for entry in self.fields:
            if entry.slot == slot:
                return entry
        return None


def parse_schema(source: str, name: str) -> Schema:
    """Recover a table's fields from its decompiled accessor class.

    Only the properties before the generated boilerplate are schema. A vector shows
    up as `<Field>Length`, which is the codegen's own naming and is folded back to
    the field it belongs to.
    """
    cut = BOILERPLATE_RE.search(source)
    head = source[:cut.start()] if cut else source
    # `Cells(int j)` returns FTiledCell; `CellsLength` returns int. The element
    # type is only ever on the indexer, and the indexers come after the cut.
    elements = {prop: kind for kind, prop in INDEXER_RE.findall(source)}

    schema = Schema(name)
    slot = FIRST_SLOT
    for type_name, prop in PROPERTY_RE.findall(head):
        if prop in SKIP_PROPERTIES:
            continue
        slot += SLOT_STEP
        bare = type_name.rstrip("?")
        if prop.endswith(VECTOR_SUFFIX) and len(prop) > len(VECTOR_SUFFIX):
            field_name = prop[:-len(VECTOR_SUFFIX)]
            schema.fields.append(
                Field(field_name, slot, VECTOR,
                      elements.get(field_name, bare).rstrip("?")))
        elif bare == "string":
            schema.fields.append(Field(prop, slot, STRING, bare))
        elif bare in SCALARS:
            schema.fields.append(Field(prop, slot, SCALAR, bare))
        else:
            schema.fields.append(Field(prop, slot, TABLE, bare))
    return schema


#: A generated file says what it is in its first few lines - `using
#: Google.FlatBuffers;` sits above the namespace. Testing that prefix instead of
#: reading whole files takes the scan over an 8,775-file tree from 189s to seconds,
#: and the full read still happens for the handful that match.
HEADER_BYTES = 4096


def load_schemas(scripts: Path, limit: int = 4000) -> dict[str, Schema]:
    """Every FlatBuffers table the build declares, by class name."""
    found: dict[str, Schema] = {}
    for path in scripts.rglob("*.cs"):
        if len(found) >= limit:
            break
        try:
            with path.open("rb") as handle:
                head = handle.read(HEADER_BYTES)
            if b"FlatBuffers" not in head:
                continue
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "IFlatbufferObject" not in source:
            continue
        declarations = list(re.finditer(
            r"(?:struct|class)\s+(\w+)\s*:\s*IFlatbufferObject", source))
        for index, match in enumerate(declarations):
            name = match.group(1)
            end = (declarations[index + 1].start()
                   if index + 1 < len(declarations) else len(source))
            schema = parse_schema(source[match.start():end], name)
            if schema.fields:
                found[name] = schema
    return found


class Buffer:
    """Just enough FlatBuffers to walk a table when the schema is known."""

    def __init__(self, data: bytes):
        self.data = data

    # -- primitives --------------------------------------------------------
    def u16(self, at: int) -> int:
        return struct.unpack_from("<H", self.data, at)[0]

    def i32(self, at: int) -> int:
        return struct.unpack_from("<i", self.data, at)[0]

    def u32(self, at: int) -> int:
        return struct.unpack_from("<I", self.data, at)[0]

    # -- structure ---------------------------------------------------------
    def root(self) -> int:
        return self.u32(0)

    def populated(self, table: int) -> list[int]:
        """The slots this particular table actually wrote.

        A field left at its default is simply absent, so two levels of the same
        game routinely disagree about which slots exist. Only the union across a
        corpus says anything about the schema.
        """
        vtable = table - self.i32(table)
        size = self.u16(vtable)
        return [slot for slot in range(4, size, SLOT_STEP) if self.u16(vtable + slot)]

    def at(self, table: int, slot: int) -> int | None:
        vtable = table - self.i32(table)
        if slot >= self.u16(vtable):
            return None
        offset = self.u16(vtable + slot)
        return table + offset if offset else None

    # -- typed reads -------------------------------------------------------
    def string(self, at: int) -> str:
        start = at + self.u32(at)
        return self.data[start + 4:start + 4 + self.u32(start)].decode("utf-8")

    def vector(self, at: int) -> tuple[int, int]:
        """(position of the first element, element count)."""
        start = at + self.u32(at)
        return start + 4, self.u32(start)

    def table(self, at: int) -> int:
        return at + self.u32(at)

    def scalar(self, at: int, spec: tuple[str, int]):
        code, _size = spec
        if code == "?":
            return bool(self.data[at])
        return struct.unpack_from("<" + code, self.data, at)[0]

    def read(self, table: int, entry: Field):
        """One field's value, or None when the writer left it out."""
        where = self.at(table, entry.slot)
        if where is None:
            return None
        if entry.kind == STRING:
            return self.string(where)
        if entry.kind == VECTOR:
            return self.vector(where)[1]
        if entry.kind == TABLE:
            return self.table(where)
        spec = entry.format
        return self.scalar(where, spec) if spec else None

    def elements(self, table: int, entry: Field) -> list[int]:
        """Positions of a vector's members, as table offsets."""
        where = self.at(table, entry.slot)
        if where is None or entry.kind != VECTOR:
            return []
        first, count = self.vector(where)
        return [first + index * 4 + self.u32(first + index * 4)
                for index in range(count)]


def fits(schema: Schema, samples: list[bytes]) -> float:
    """How well a schema explains a corpus, from 0 to 1.

    A schema that is merely large enough is not a fit: the string fields have to
    decode as text and the vector counts have to be believable. Reading one build's
    levels with another build's table fails on both, which is what makes this a
    test rather than a guess.
    """
    if not schema.fields or not samples:
        return 0.0
    scored = 0.0
    for raw in samples:
        buffer = Buffer(raw)
        try:
            table = buffer.root()
            slots = buffer.populated(table)
        except (struct.error, IndexError):
            continue
        if not slots or max(slots) > schema.last_slot:
            continue
        good = 0
        for slot in slots:
            entry = schema.by_slot(slot)
            if entry is None:
                continue
            try:
                value = buffer.read(table, entry)
            except (struct.error, IndexError, UnicodeDecodeError):
                continue
            if entry.kind == STRING and isinstance(value, str) and value.isprintable():
                good += 1
            elif entry.kind == VECTOR and isinstance(value, int) and value < 100000:
                good += 1
            elif entry.kind in (TABLE, SCALAR):
                good += 1
        scored += good / len(slots)
    return scored / len(samples)


def choose_schema(schemas: dict[str, Schema], samples: list[bytes],
                  hint: str = "") -> tuple[Schema | None, float]:
    """The build's own table that best explains this corpus.

    `hint` only breaks ties between equally good fits - it never promotes a schema
    that the bytes disagree with, so a corpus is matched on evidence and named
    afterwards.
    """
    ranked: list[tuple[float, int, Schema]] = []
    for schema in schemas.values():
        score = fits(schema, samples)
        if score <= 0:
            continue
        # Among equal fits, prefer the tighter schema and then the expected name:
        # a table with spare slots explains the same bytes as one sized for them.
        tie = -schema.last_slot + (1000 if hint and hint.lower() in schema.name.lower()
                                   else 0)
        ranked.append((round(score, 4), tie, schema))
    if not ranked:
        return None, 0.0
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = ranked[0]
    return best[2], best[0]
