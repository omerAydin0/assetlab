"""Stage 5 - group identical and near-identical assets.

Exact duplicates come from sha256. Near-duplicates use a difference hash, but
only images of the same dimensions can match, so bucketing by (width, height)
first turns the original O(n^2) sweep into a handful of small comparisons.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

from .core import connect

HAMMING_LIMIT = 4


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: int, second: int) -> None:
        left, right = self.find(first), self.find(second)
        if left != right:
            self.parent[right] = left


def deduplicate(conn: sqlite3.Connection) -> dict[str, int]:
    union = UnionFind()

    exact = defaultdict(list)
    for row in conn.execute("SELECT id, sha256 FROM assets WHERE sha256 IS NOT NULL"):
        exact[row["sha256"]].append(row["id"])
    exact_pairs = 0
    for members in exact.values():
        for member in members[1:]:
            union.union(members[0], member)
            exact_pairs += 1

    buckets: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for row in conn.execute(
        """SELECT id, width, height, dhash FROM assets
            WHERE dhash IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL"""
    ):
        buckets[(row["width"], row["height"])].append((row["id"], int(row["dhash"], 16)))

    comparisons, near_pairs = 0, 0
    for members in buckets.values():
        for index, (left_id, left_hash) in enumerate(members):
            for right_id, right_hash in members[index + 1:]:
                comparisons += 1
                if (left_hash ^ right_hash).bit_count() <= HAMMING_LIMIT:
                    union.union(left_id, right_id)
                    near_pairs += 1

    groups: dict[int, list[int]] = defaultdict(list)
    for item in union.parent:
        groups[union.find(item)].append(item)

    updates, group_number = [], 0
    for root in sorted(groups):
        members = groups[root]
        if len(members) < 2:
            continue
        group_number += 1
        label = f"dup_{group_number:04d}"
        updates.extend({"id": member, "duplicate_group": label} for member in members)

    conn.execute("UPDATE assets SET duplicate_group = NULL")
    conn.executemany("UPDATE assets SET duplicate_group=:duplicate_group WHERE id=:id", updates)
    conn.commit()
    return {"groups": group_number, "members": len(updates), "exact_pairs": exact_pairs,
            "near_pairs": near_pairs, "comparisons": comparisons,
            "buckets": len(buckets)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Group duplicate assets.")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    conn = connect(args.out / "assetlab.db")
    stats = deduplicate(conn)
    print(f"duplicate groups: {stats['groups']} covering {stats['members']} assets")
    print(f"  exact pairs={stats['exact_pairs']}  near pairs={stats['near_pairs']}")
    print(f"  {stats['buckets']} size buckets, {stats['comparisons']} comparisons "
          f"(a full pairwise sweep would be far larger)")
    for row in conn.execute(
        """SELECT duplicate_group, COUNT(*) n, MIN(name) example FROM assets
            WHERE duplicate_group IS NOT NULL GROUP BY duplicate_group
            ORDER BY n DESC LIMIT 5"""):
        print(f"  {row['duplicate_group']}: {row['n']} x {row['example'][:40]}")
    conn.close()


if __name__ == "__main__":
    main()
