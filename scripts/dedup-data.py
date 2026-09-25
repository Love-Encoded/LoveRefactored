#!/usr/bin/env python3
"""Deduplicate data/ files via hardlinks (safe for gallery + chat_upload refs)."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import defaultdict

SKIP_DIRS = {"chroma_db"}
SKIP_NAMES = {".DS_Store", ".gitkeep"}
SKIP_SUFFIXES = {".db-shm", ".db-wal", ".db", ".bak"}


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def same_inode(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_ino == os.stat(b).st_ino
    except OSError:
        return False


def pick_canonical(paths: list[str]) -> str:
    def sort_key(p: str):
        try:
            st = os.stat(p)
            return (st.st_mtime, p)
        except OSError:
            return (float("inf"), p)

    return sorted(paths, key=sort_key)[0]


def iter_target_files(data_dir: str, only: set[str] | None):
    targets = only or {"galleries", "chat_uploads", "reference_images"}

    if "galleries" in targets:
        gal = os.path.join(data_dir, "galleries")
        if os.path.isdir(gal):
            for name in os.listdir(gal):
                if name.endswith("_meta.json") or name in SKIP_NAMES:
                    continue
                path = os.path.join(gal, name)
                if os.path.isfile(path) and not name.endswith(".json"):
                    yield path

            thumbs = os.path.join(gal, "_thumbs")
            if os.path.isdir(thumbs):
                for name in os.listdir(thumbs):
                    if name in SKIP_NAMES:
                        continue
                    path = os.path.join(thumbs, name)
                    if os.path.isfile(path):
                        yield path

    for folder in ("chat_uploads", "reference_images"):
        if folder not in targets:
            continue
        root = os.path.join(data_dir, folder)
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if name in SKIP_NAMES:
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path):
                yield path


def dedupe(data_dir: str, execute: bool, only: set[str] | None) -> dict:
    data_dir = os.path.abspath(data_dir)
    if not os.path.isdir(data_dir):
        raise SystemExit(f"Not a directory: {data_dir}")

    by_hash: dict[str, list[str]] = defaultdict(list)
    scanned = 0
    for path in iter_target_files(data_dir, only):
        if any(path.endswith(s) for s in SKIP_SUFFIXES):
            continue
        try:
            if os.path.getsize(path) == 0:
                continue
            by_hash[file_md5(path)].append(path)
            scanned += 1
        except OSError as err:
            print(f"  skip {path}: {err}", file=sys.stderr)

    groups = [paths for paths in by_hash.values() if len(paths) > 1]
    stats = {
        "scanned": scanned,
        "groups": len(groups),
        "linked": 0,
        "already_linked": 0,
        "bytes_saved": 0,
        "errors": 0,
    }

    print(f"\n{'=' * 60}")
    print(f"Data dir: {data_dir}")
    print(f"Mode: {'EXECUTE' if execute else 'DRY-RUN'}")
    print(f"Scanned: {scanned} files")
    print(f"Duplicate groups: {len(groups)}")

    for paths in sorted(groups, key=lambda ps: -os.path.getsize(ps[0]) * (len(ps) - 1)):
        canonical = pick_canonical(paths)
        size = os.path.getsize(canonical)
        extras = [p for p in paths if p != canonical]

        rel_canon = os.path.relpath(canonical, data_dir)
        print(f"\n  [{len(paths)}x {size / 1024 / 1024:.2f} MB] keep {rel_canon}")

        for extra in sorted(extras):
            rel_extra = os.path.relpath(extra, data_dir)
            if same_inode(canonical, extra):
                stats["already_linked"] += 1
                print(f"    = already linked: {rel_extra}")
                continue

            stats["bytes_saved"] += size
            print(f"    -> hardlink: {rel_extra}")

            if execute:
                try:
                    os.remove(extra)
                    os.link(canonical, extra)
                    stats["linked"] += 1
                except OSError as err:
                    stats["errors"] += 1
                    print(f"       ERROR: {err}", file=sys.stderr)

    print(f"\nSummary:")
    if execute:
        print(f"  Hardlinks created: {stats['linked']}")
        print(f"  Already linked: {stats['already_linked']}")
        print(f"  Errors: {stats['errors']}")
    else:
        would_link = sum(
            len([p for p in paths if p != pick_canonical(paths) and not same_inode(pick_canonical(paths), p)])
            for paths in groups
        )
        print(f"  Hardlinks to create: {would_link}")
    print(f"  Space recoverable: {stats['bytes_saved'] / 1024 / 1024 / 1024:.2f} GB")
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", help="Path to data/ folder")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply hardlinks (default is dry-run)",
    )
    parser.add_argument(
        "--only",
        choices=["galleries", "chat_uploads", "reference_images", "all"],
        default="all",
        help="Limit which folders to process",
    )
    args = parser.parse_args()

    only = None if args.only == "all" else {args.only}
    dedupe(args.data_dir, execute=args.execute, only=only)


if __name__ == "__main__":
    main()
