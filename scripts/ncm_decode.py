#!/usr/bin/env python3
"""Decrypt NetEase Cloud Music .ncm files by wrapping the ncmdump binary.

The .ncm container may hold either an MP3 or a FLAC payload; ncmdump writes
whichever it finds, so the output extension is never assumed here -- the output
directories are scanned before and after the run, and the files that newly
appeared are reported as the format breakdown.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".aac", ".wma"}

TAGLIB_HINT = """ncmdump failed to start because the taglib shared library is missing.
The macOS release binary links against /opt/homebrew/opt/taglib/lib/libtag.2.dylib.
Fix it with:

    brew install taglib

If the binary was downloaded with a browser, also clear the quarantine flag:

    xattr -d com.apple.quarantine <path-to-ncmdump>
"""


def is_appledouble(path: Path) -> bool:
    """macOS writes ._<name> metadata stubs on FAT/exFAT volumes; they are not audio."""
    return path.name.startswith("._")


def resolve_ncmdump() -> Path | None:
    """Resolve the ncmdump binary: env var, then PATH, then ./bin next to the repo."""
    env_value = os.environ.get("MUSIC_CONVERTER_NCMDUMP")
    if env_value:
        candidate = Path(env_value).expanduser()
        return candidate if candidate.is_file() else None

    found = shutil.which("ncmdump")
    if found:
        return Path(found)

    bundled = Path(__file__).resolve().parent.parent / "bin" / "ncmdump"
    return bundled if bundled.is_file() else None


def preflight(binary: Path) -> None:
    """Run `ncmdump --version`; exit 2 with remediation if the binary cannot start."""
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as exc:
        print(f"error: cannot execute {binary}: {exc}", file=sys.stderr)
        sys.exit(2)
    except subprocess.TimeoutExpired:
        print(f"error: {binary} --version timed out", file=sys.stderr)
        sys.exit(2)

    if proc.returncode == 0:
        version = (proc.stdout or proc.stderr).strip().splitlines()
        print(f"ncmdump: {binary} ({version[0] if version else 'version unknown'})")
        return

    combined = (proc.stdout or "") + (proc.stderr or "")
    if "Library not loaded" in combined and "libtag" in combined:
        print(combined.strip(), file=sys.stderr)
        print("", file=sys.stderr)
        print(TAGLIB_HINT, file=sys.stderr)
        sys.exit(2)

    print(f"error: {binary} --version exited {proc.returncode}", file=sys.stderr)
    if combined.strip():
        print(combined.strip(), file=sys.stderr)
    sys.exit(2)


def expand_directory(directory: Path, recursive: bool) -> list[Path]:
    """List the .ncm files in a directory, skipping AppleDouble stubs and hidden dirs."""
    walker = directory.rglob("*") if recursive else directory.glob("*")
    found: list[Path] = []
    for path in walker:
        if not path.is_file() or is_appledouble(path):
            continue
        if path.suffix.lower() != ".ncm":
            continue
        if any(part.startswith(".") for part in path.relative_to(directory).parts[:-1]):
            continue
        found.append(path)
    return sorted(found)


def collect_inputs(raw_inputs: list[str], recursive: bool) -> tuple[list[Path], list[str]]:
    """Expand CLI inputs into a flat list of .ncm files, plus unusable entries.

    Directories are walked here instead of being handed to ncmdump's -d option:
    ncmdump has no filter for macOS AppleDouble sidecars (._<name>), so feeding
    it a directory on an exFAT/FAT volume would make it read the 4KB metadata
    stubs as if they were .ncm containers.
    """
    files: list[Path] = []
    bad: list[str] = []

    for raw in raw_inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            found = expand_directory(path, recursive)
            if not found:
                bad.append(f"{raw} (no .ncm files found)")
            files.extend(found)
        elif path.is_file():
            if is_appledouble(path):
                bad.append(f"{raw} (AppleDouble sidecar, skipped)")
            elif path.suffix.lower() != ".ncm":
                bad.append(f"{raw} (not a .ncm file)")
            else:
                files.append(path)
        else:
            bad.append(f"{raw} (no such file or directory)")

    # Preserve order, drop duplicates.
    def dedupe(items: list[Path]) -> list[Path]:
        seen: set[Path] = set()
        out: list[Path] = []
        for item in items:
            resolved = item.resolve()
            if resolved not in seen:
                seen.add(resolved)
                out.append(item)
        return out

    return dedupe(files), bad


def run_ncmdump(binary: Path, args: list[str]) -> tuple[int, str]:
    """Invoke ncmdump. -m (remove original) is never passed: inputs are never deleted."""
    try:
        proc = subprocess.run(
            [str(binary), *args],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return 1, f"cannot execute {binary}: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def audio_snapshot(directories: list[Path]) -> dict[Path, tuple[int, int]]:
    """Map each audio file in the directories to (size, mtime_ns), non-recursively.

    Each unit writes into exactly one directory, so a flat listing is the whole
    result set; recursing would pull in unrelated audio from subdirectories.
    The stat signature, rather than the path alone, is what makes a re-decode
    into an existing output folder visible: ncmdump overwrites in place, so the
    path set would be unchanged while the file was in fact rewritten.
    """
    seen: dict[Path, tuple[int, int]] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.glob("*"):
            if not path.is_file() or is_appledouble(path):
                continue
            if path.suffix.lower() in AUDIO_SUFFIXES:
                stat = path.stat()
                seen[path.resolve()] = (stat.st_size, stat.st_mtime_ns)
    return seen


def format_breakdown(paths: set[Path]) -> dict[str, int]:
    """Count paths by lowercased extension."""
    breakdown: dict[str, int] = {}
    for path in paths:
        suffix = path.suffix.lower()
        breakdown[suffix] = breakdown.get(suffix, 0) + 1
    return breakdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ncm_decode.py",
        description="Decrypt NetEase Cloud Music .ncm files into playable audio.",
    )
    parser.add_argument(
        "inputs",
        metavar="INPUT",
        nargs="+",
        help="one or more .ncm files or directories containing them",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="OUTPUT",
        help="output folder (default: alongside each source file)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="descend into subdirectories of directory inputs",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="continue after a failure instead of stopping at the first one",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the ncmdump commands that would run and exit",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    binary = resolve_ncmdump()
    if binary is None:
        print(
            "error: ncmdump not found. Set MUSIC_CONVERTER_NCMDUMP, put ncmdump on PATH, "
            "or place it at bin/ncmdump in the repository.\n"
            "Releases: https://github.com/taurusxin/ncmdump/releases",
            file=sys.stderr,
        )
        return 2

    files, bad = collect_inputs(args.inputs, args.recursive)
    for entry in bad:
        print(f"skip: {entry}", file=sys.stderr)

    if not files:
        print("error: no .ncm files to process", file=sys.stderr)
        return 2

    output_dir: Path | None = None
    if args.output:
        output_dir = Path(args.output).expanduser()

    # Build the unit list first so --dry-run can print it without touching anything.
    # A unit is (label, ncmdump argv, directories to scan for results).
    units: list[tuple[str, list[str], list[Path]]] = []
    for path in files:
        argv = [str(path)]
        if output_dir is not None:
            argv += ["-o", str(output_dir)]
        units.append((str(path), argv, [output_dir or path.parent]))

    if args.dry_run:
        print("dry-run: no files will be written")
        for label, argv, _ in units:
            printable = " ".join(shlex_quote(part) for part in [str(binary), *argv])
            print(f"  {label}")
            print(f"    {printable}")
        return 0

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    preflight(binary)

    ok = 0
    failed = 0
    produced: set[Path] = set()

    for label, argv, result_dirs in units:
        # Snapshot per unit so the summary reports only the files this run
        # produced, not audio that was already there.
        before = audio_snapshot(result_dirs)
        code, output = run_ncmdump(binary, argv)
        after = audio_snapshot(result_dirs)
        written = {path for path, sig in after.items() if before.get(path) != sig}

        if code == 0 and written:
            ok += 1
            produced |= written
            print(f"OK   {label}")
        else:
            # ncmdump 1.5.1 exits 0 even when it rejects a file ("Not netease
            # protected file"), so the exit code alone cannot decide the
            # outcome; an empty write set is the reliable failure signal.
            failed += 1
            reason = f"exit {code}" if code != 0 else "no audio file was written"
            print(f"FAIL {label} ({reason})", file=sys.stderr)
            if output:
                print(f"     {output}", file=sys.stderr)
            if not args.keep_going:
                break

    print("")
    print(f"summary: {ok} OK, {failed} failed, {len(units)} input(s) total")

    breakdown = format_breakdown(produced)
    if breakdown:
        parts = ", ".join(
            f"{suffix.lstrip('.')}: {count}"
            for suffix, count in sorted(breakdown.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        print(f"output formats: {parts}")
    else:
        print("output formats: no new audio files in the output location")

    if failed and not args.keep_going:
        return 1
    return 1 if failed else 0


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


if __name__ == "__main__":
    sys.exit(main())
