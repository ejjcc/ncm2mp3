#!/usr/bin/env python3
"""De-duplicate a local music folder using two independent signals.

Signal A: md5 of file bytes (catches identical files under unrelated names).
Signal B: normalized title token set (catches re-encodes and reordered
          artist/title, e.g. different bitrates of the same track).

The two signals are combined with union-find. Either signal alone misses about
half of the duplicates in a real library, so both are always computed.

Audio duration is deliberately NOT a grouping signal: integer-second collisions
between unrelated tracks are overwhelmingly false positives. Duration is only
displayed, and used as a confirmation column on groups already formed by A or B.
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import unicodedata

AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".wma",
    ".aiff",
    ".aif",
    ".alac",
    ".ape",
}

# Tokens that carry no identity information and would otherwise prevent two
# spellings of the same track from matching.
STOPWORDS = {
    "feat",
    "ft",
    "featuring",
    "original",
    "mix",
    "edit",
    "from",
    "by",
    "the",
}

# Trailing copy markers produced by download managers and file browsers.
COPY_SUFFIX_RE = re.compile(r"\s*[\(\[]\d+[\)\]]\s*$")

HASH_CHUNK_SIZE = 1024 * 1024

DEFAULT_QUARANTINE_NAME = ".dedup-quarantine"

EXIT_OK = 0
EXIT_ERROR = 2


def normalize_tokens(filename):
    """Return the sorted token set used as signal B for one filename."""
    stem = os.path.splitext(filename)[0]
    stem = COPY_SUFFIX_RE.sub("", stem)
    # NFKD then dropping combining marks folds accents: "Tiesto" == "Tiesto".
    decomposed = unicodedata.normalize("NFKD", stem)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.lower()

    tokens = set()
    current = []
    for char in lowered:
        if char.isascii() and (char.isalpha() or char.isdigit()):
            current.append(char)
            continue
        if current:
            tokens.add("".join(current))
            current = []
        # Keep CJK ideographs and kana as single-character tokens.
        if unicodedata.category(char).startswith("L"):
            tokens.add(char)
    if current:
        tokens.add("".join(current))

    tokens -= STOPWORDS
    return tuple(sorted(tokens))


def file_md5(path):
    """Hash a file in chunks; never load the whole file into memory."""
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def probe_duration(path):
    """Return duration in seconds via ffprobe, or None when unavailable."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def collect_files(root, recursive, excluded_dirs):
    """Yield audio file paths, skipping AppleDouble stubs and hidden dirs."""
    files = []
    excluded = {os.path.realpath(d) for d in excluded_dirs}
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.realpath(dirpath) in excluded:
            dirnames[:] = []
            continue
        dirnames[:] = [
            d
            for d in sorted(dirnames)
            if not d.startswith(".")
            and os.path.realpath(os.path.join(dirpath, d)) not in excluded
        ]
        for name in sorted(filenames):
            # macOS writes ._<name> AppleDouble sidecars on FAT/exFAT volumes.
            # They are 4KB metadata stubs and would double every count.
            if name.startswith("._") or name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in AUDIO_EXTENSIONS:
                continue
            path = os.path.join(dirpath, name)
            if os.path.isfile(path) and not os.path.islink(path):
                files.append(path)
        if not recursive:
            dirnames[:] = []
    return files


class UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a, b):
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a


def build_groups(paths, hashes, token_sets):
    """Union signal A and signal B, returning groups and their origin labels."""
    uf = UnionFind(len(paths))

    hash_buckets = {}
    for index, digest in enumerate(hashes):
        if digest is not None:
            hash_buckets.setdefault(digest, []).append(index)

    token_buckets = {}
    for index, tokens in enumerate(token_sets):
        if tokens:
            token_buckets.setdefault(tokens, []).append(index)

    hash_pairs = set()
    for members in hash_buckets.values():
        for other in members[1:]:
            uf.union(members[0], other)
        for member in members:
            hash_pairs.add(member)

    token_pairs = set()
    for members in token_buckets.values():
        if len(members) < 2:
            continue
        for other in members[1:]:
            uf.union(members[0], other)
        for member in members:
            token_pairs.add(member)

    duplicate_hash_members = set()
    for members in hash_buckets.values():
        if len(members) > 1:
            duplicate_hash_members.update(members)

    grouped = {}
    for index in range(len(paths)):
        grouped.setdefault(uf.find(index), []).append(index)

    groups = []
    for members in grouped.values():
        if len(members) < 2:
            continue
        members.sort()
        has_hash_match = bool(set(members) & duplicate_hash_members)
        has_token_match = bool(set(members) & token_pairs)
        if has_hash_match and has_token_match:
            label = "identical content + title match"
        elif has_hash_match:
            label = "identical content"
        else:
            label = "title match"
        groups.append((label, members))

    groups.sort(key=lambda item: paths[item[1][0]])
    return groups


ARTIST_TITLE_RE = re.compile(r"^.+\s+-\s+.+$")


def choose_keeper(members, paths, sizes):
    """Pick the keeper: largest size, then Artist - Title naming, then shortest name."""

    def rank(index):
        name = os.path.basename(paths[index])
        stem = os.path.splitext(name)[0]
        return (
            -sizes[index],
            0 if ARTIST_TITLE_RE.match(stem) else 1,
            len(name),
            name,
        )

    return min(members, key=rank)


def format_size(size):
    return "%8.2f MB" % (size / (1024.0 * 1024.0))


def format_duration(seconds):
    if seconds is None:
        return "  --:--"
    total = int(round(seconds))
    return "%4d:%02d" % (total // 60, total % 60)


def render_report(groups, paths, sizes, durations, keepers):
    lines = []
    for number, (label, members) in enumerate(groups, start=1):
        keeper = keepers[number - 1]
        lines.append("Group %d [%s] - %d files" % (number, label, len(members)))
        lines.append(
            "  KEEP   %s %s  %s"
            % (format_size(sizes[keeper]), format_duration(durations[keeper]), paths[keeper])
        )
        for index in members:
            if index == keeper:
                continue
            lines.append(
                "  REMOVE %s %s  %s"
                % (format_size(sizes[index]), format_duration(durations[index]), paths[index])
            )
        lines.append("")
    return "\n".join(lines)


def shell_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="audio_dedup.py",
        description=(
            "Find duplicate audio files by content hash and normalized title, "
            "and optionally quarantine the non-keepers."
        ),
    )
    parser.add_argument("directory", metavar="DIR", help="directory to scan")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="move non-keeper files into the quarantine dir (default is dry-run)",
    )
    parser.add_argument(
        "--quarantine",
        metavar="DIR",
        help="quarantine directory (default: <DIR>/%s)" % DEFAULT_QUARANTINE_NAME,
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="descend into subdirectories",
    )
    parser.add_argument(
        "--report",
        metavar="FILE",
        help="also write the report to this file",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    scan_root = os.path.abspath(args.directory)
    if not os.path.isdir(scan_root):
        print("error: not a directory: %s" % scan_root, file=sys.stderr)
        return EXIT_ERROR

    if args.quarantine:
        quarantine_dir = os.path.abspath(args.quarantine)
    else:
        quarantine_dir = os.path.join(scan_root, DEFAULT_QUARANTINE_NAME)

    # The quarantine dir must never be scanned, otherwise previously moved
    # duplicates re-enter the grouping and can be moved onto themselves.
    inside_scan_tree = (
        quarantine_dir == scan_root
        or quarantine_dir.startswith(scan_root + os.sep)
    )
    if quarantine_dir == scan_root:
        print(
            "error: quarantine directory must not be the scan directory itself: %s"
            % quarantine_dir,
            file=sys.stderr,
        )
        return EXIT_ERROR
    if inside_scan_tree and not args.recursive:
        # Non-recursive scans only read the top level, so a nested quarantine is
        # already out of reach unless it sits directly in the scan root.
        pass

    excluded_dirs = [quarantine_dir] if inside_scan_tree else []

    paths = collect_files(scan_root, args.recursive, excluded_dirs)
    if not paths:
        print("Scanned %s" % scan_root)
        print("No audio files found.")
        return EXIT_OK

    sizes = []
    hashes = []
    token_sets = []
    unreadable = []
    for path in paths:
        try:
            sizes.append(os.path.getsize(path))
        except OSError:
            sizes.append(0)
        try:
            hashes.append(file_md5(path))
        except OSError as exc:
            hashes.append(None)
            unreadable.append((path, str(exc)))
        token_sets.append(normalize_tokens(os.path.basename(path)))

    groups = build_groups(paths, hashes, token_sets)

    # Duration is only probed for files already grouped, as a display and
    # confirmation column. It is never used to form groups.
    durations = {}
    for _, members in groups:
        for index in members:
            if index not in durations:
                durations[index] = probe_duration(paths[index])

    keepers = [choose_keeper(members, paths, sizes) for _, members in groups]

    header = [
        "Scanned %s%s" % (scan_root, " (recursive)" if args.recursive else ""),
        "Audio files: %d" % len(paths),
        "Duplicate groups: %d" % len(groups),
    ]
    removable = sum(len(members) - 1 for _, members in groups)
    reclaimable = 0
    for group_index, (_, members) in enumerate(groups):
        keeper = keepers[group_index]
        reclaimable += sum(sizes[i] for i in members if i != keeper)
    header.append("Redundant files: %d" % removable)
    header.append("Reclaimable space: %.2f MB" % (reclaimable / (1024.0 * 1024.0)))
    header.append("")

    report = "\n".join(header) + "\n" + render_report(groups, paths, sizes, durations, keepers)
    print(report, end="")

    if unreadable:
        print("Unreadable files (skipped for content hashing):")
        for path, reason in unreadable:
            print("  %s: %s" % (path, reason))
        print("")

    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as handle:
                handle.write(report)
        except OSError as exc:
            print("error: cannot write report: %s" % exc, file=sys.stderr)
            return EXIT_ERROR
        print("Report written to %s" % os.path.abspath(args.report))

    if not groups:
        print("No duplicates found.")
        return EXIT_OK

    if not args.apply:
        print("Dry run. Nothing was moved. Re-run with --apply to quarantine the non-keepers.")
        return EXIT_OK

    try:
        os.makedirs(quarantine_dir, exist_ok=True)
    except OSError as exc:
        print("error: cannot create quarantine directory: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    moved = 0
    failed = 0
    for group_index, (_, members) in enumerate(groups):
        keeper = keepers[group_index]
        for index in members:
            if index == keeper:
                continue
            source = paths[index]
            # Mirror the source path relative to the scan root instead of
            # flattening to the basename: same-named files in different
            # subdirectories cannot collide, original names are preserved, and
            # a restore puts every file back in its own directory.
            destination = os.path.join(quarantine_dir, os.path.relpath(source, scan_root))
            try:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.move(source, destination)
            except (OSError, shutil.Error) as exc:
                print("FAIL  %s: %s" % (source, exc), file=sys.stderr)
                failed += 1
                continue
            moved += 1
            print("MOVED %s -> %s" % (source, destination))

    print("")
    print("Moved %d file(s) to %s" % (moved, quarantine_dir))
    if failed:
        print("Failed to move %d file(s)." % failed)
    # --ignore-existing never overwrites a keeper that is still in place.
    print(
        "Restore with: rsync -a --ignore-existing %s/ %s/"
        % (shell_quote(quarantine_dir), shell_quote(scan_root))
    )
    return EXIT_ERROR if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
