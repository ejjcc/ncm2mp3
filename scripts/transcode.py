#!/usr/bin/env python3
"""Transcode audio files with ffmpeg, preserving tags and embedded cover art.

Part of the music-converter skill. Local library work only.
"""

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".wav",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
    ".aiff",
    ".alac",
    ".ape",
}

TARGET_EXTENSION = {
    "mp3": ".mp3",
    "flac": ".flac",
    "m4a": ".m4a",
    "wav": ".wav",
}

DEFAULT_BITRATE = "320k"

# Duration drift tolerated between source and result before --replace is refused.
DURATION_TOLERANCE_SECONDS = 1.0

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_NO_FFMPEG = 3


def is_appledouble(path):
    """macOS writes ._<name> sidecars on FAT/exFAT volumes. They are not audio."""
    return path.name.startswith("._")


def collect_inputs(raw_inputs):
    """Expand files and directories into a sorted list of audio files."""
    files = []
    seen = set()
    missing = []
    for raw in raw_inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            candidates = sorted(path.rglob("*"))
        elif path.exists():
            candidates = [path]
        else:
            missing.append(path)
            continue
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if is_appledouble(candidate):
                continue
            if candidate.name.startswith("."):
                continue
            # pathlib's glob descends into dot-directories, so hidden directories
            # must be filtered explicitly. This keeps the audio_dedup.py quarantine
            # at <DIR>/.dedup-quarantine out of the input set.
            if any(part.startswith(".") for part in candidate.relative_to(path).parts):
                continue
            if candidate.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            # Identify duplicates by inode, not by path text: resolve() does not
            # fold case, so on a case-insensitive volume "X.flac" and "x.flac"
            # name one file but two distinct resolved paths.
            try:
                info = candidate.stat()
                identity = (info.st_dev, info.st_ino)
            except OSError:
                identity = candidate.resolve()
            if identity in seen:
                continue
            seen.add(identity)
            files.append(candidate)
    return files, missing


def probe_duration(path):
    """Return the duration in seconds, or None if ffprobe cannot decode the file."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-print_format",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return None
    if duration <= 0:
        return None
    return duration


def build_ffmpeg_command(source, destination, target, bitrate):
    """Assemble the ffmpeg invocation for one file."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]

    if target == "mp3":
        # -map 0:v? keeps embedded cover art when present without failing when absent.
        cmd += ["-map", "0:a", "-map", "0:v?", "-c:v", "copy"]
        cmd += ["-map_metadata", "0", "-id3v2_version", "3"]
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
    elif target == "flac":
        cmd += ["-map", "0:a", "-map", "0:v?", "-c:v", "copy"]
        cmd += ["-map_metadata", "0"]
        cmd += ["-c:a", "flac"]
    elif target == "m4a":
        cmd += ["-map", "0:a", "-map", "0:v?", "-c:v", "copy", "-disposition:v", "attached_pic"]
        cmd += ["-map_metadata", "0"]
        cmd += ["-c:a", "aac", "-b:a", bitrate]
    elif target == "wav":
        # WAV carries no cover art and only minimal tags.
        cmd += ["-map", "0:a", "-map_metadata", "0", "-c:a", "pcm_s16le"]
    else:
        raise ValueError("unsupported target format: %s" % target)

    cmd.append(str(destination))
    return cmd


def run_ffmpeg(cmd):
    """Run ffmpeg. Return (ok, stderr_text)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, result.stderr.strip() or "ffmpeg exited with %d" % result.returncode
    return True, ""


def verify_output(path, source_duration):
    """Confirm the new file is non-empty and decodes to a plausible duration."""
    try:
        if path.stat().st_size == 0:
            return False, "output file is empty"
    except OSError as exc:
        return False, "output file missing: %s" % exc

    duration = probe_duration(path)
    if duration is None:
        return False, "output does not decode or has zero duration"
    if source_duration is not None:
        drift = abs(duration - source_duration)
        if drift > DURATION_TOLERANCE_SECONDS:
            return False, "duration drift %.2fs (source %.2fs, output %.2fs)" % (
                drift,
                source_duration,
                duration,
            )
    return True, ""


def destination_for(source, target, output_dir, input_roots):
    """Compute the output path, mirroring directory structure under -o when given."""
    extension = TARGET_EXTENSION[target]
    if output_dir is None:
        return source.with_suffix(extension)

    relative = Path(source.name)
    for root in input_roots:
        try:
            relative = source.resolve().relative_to(root)
            break
        except ValueError:
            continue
    return (output_dir / relative).with_suffix(extension)


def planned_destination(source, target, output_dir, input_roots, replace):
    """Compute the output path for one source. Single source of truth for callers."""
    if replace:
        return source.with_suffix(TARGET_EXTENSION[target])
    return destination_for(source, target, output_dir, input_roots)


_FOLDS_CASE_CACHE = {}


def volume_folds_case(directory):
    """True when `directory` lives on a case-insensitive volume (APFS, HFS+, exFAT).

    os.path.normcase cannot answer this: on posix it is the identity function.
    Case sensitivity is a property of the mount point, so probe it once per
    directory with a real file. Non-existent directories are probed through
    their nearest existing ancestor, which is on the same volume.
    """
    probe_dir = Path(directory)
    while not probe_dir.is_dir() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    key = str(probe_dir)
    if key not in _FOLDS_CASE_CACHE:
        # Conservative fallback for unwritable directories: assume folding, which
        # can only turn a silent overwrite into a reported conflict.
        folds = sys.platform in ("darwin", "win32")
        upper = probe_dir / ".transcode-CASEPROBE"
        try:
            upper.touch()
            try:
                folds = (probe_dir / ".transcode-caseprobe").exists()
            finally:
                upper.unlink()
        except OSError:
            pass
        _FOLDS_CASE_CACHE[key] = folds
    return _FOLDS_CASE_CACHE[key]


def destination_key(destination):
    """Key a planned destination by the directory entry it will actually claim.

    Two sources whose destinations differ only in letter case resolve to one
    entry on a case-insensitive volume and must collide here, otherwise the
    conflict pre-check misses them and parallel workers overwrite each other.
    """
    key = os.path.realpath(str(destination))
    if volume_folds_case(destination.parent):
        key = key.casefold()
    return key


def is_same_file(left, right):
    """True when both paths name one existing file.

    Compares inode identity rather than path text: on case-insensitive volumes
    (APFS, HFS+, exFAT) "SONG.MP3" and "SONG.mp3" are the same directory entry,
    and resolve() does not normalize case. When either path does not exist they
    cannot be the same file.
    """
    try:
        return os.path.samefile(str(left), str(right))
    except OSError:
        return False


def transcode_one(source, target, bitrate, output_dir, input_roots, replace, dry_run):
    """Transcode a single file. Return a (source, status, detail) tuple.

    status is one of: "ok", "skip", "fail".
    """
    already_target = source.suffix.lower() == TARGET_EXTENSION[target]
    if already_target and not replace:
        return source, "skip", "already %s" % target

    destination = planned_destination(source, target, output_dir, input_roots, replace)

    # Resolve identity before anything is written: this flag decides both whether
    # an existing destination is an unrelated file and whether the source may be
    # unlinked afterwards.
    destination_is_source = is_same_file(source, destination)
    if destination_is_source and not replace:
        return source, "skip", "output would overwrite source (use --replace)"
    if not destination_is_source and destination.exists():
        return source, "fail", "destination already exists, not overwritten: %s" % destination

    if dry_run:
        return source, "ok", "would write %s" % destination

    source_duration = probe_duration(source)
    if source_duration is None:
        return source, "fail", "source does not decode"

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return source, "fail", "cannot create output directory: %s" % exc

    # Always build into a temp file in the destination directory so a crash or a
    # failed verification never leaves a truncated file at the real path.
    try:
        handle, temp_name = tempfile.mkstemp(
            prefix=".transcode-",
            suffix=TARGET_EXTENSION[target],
            dir=str(destination.parent),
        )
    except OSError as exc:
        return source, "fail", "cannot create temp file: %s" % exc
    os.close(handle)
    temp_path = Path(temp_name)
    # mkstemp creates the file 0600. A music library file should carry the same
    # permissions as the file it came from, falling back to the process umask.
    try:
        os.chmod(temp_name, source.stat().st_mode & 0o7777)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        try:
            os.chmod(temp_name, 0o666 & ~umask)
        except OSError:
            pass

    try:
        cmd = build_ffmpeg_command(source, temp_path, target, bitrate)
        ok, stderr = run_ffmpeg(cmd)
        if not ok:
            return source, "fail", stderr

        ok, reason = verify_output(temp_path, source_duration)
        if not ok:
            return source, "fail", reason

        if replace:
            # os.replace is atomic within one filesystem. When the format changed
            # the original path has a different extension and must be removed after
            # the new file is safely in place. The identity check must use the flag
            # computed before the replace: afterwards the source path may resolve to
            # the newly written file, and unlinking it would destroy the result.
            os.replace(str(temp_path), str(destination))
            temp_path = None
            if not destination_is_source:
                try:
                    source.unlink()
                except OSError as exc:
                    return source, "fail", "new file written but original not removed: %s" % exc
            return source, "ok", "replaced %s" % destination
        else:
            os.replace(str(temp_path), str(destination))
            temp_path = None
            return source, "ok", str(destination)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="transcode.py",
        description="Transcode audio files with ffmpeg, preserving tags and cover art.",
    )
    parser.add_argument(
        "--to",
        required=True,
        choices=sorted(TARGET_EXTENSION),
        help="target audio format",
    )
    parser.add_argument(
        "-b",
        "--bitrate",
        default=DEFAULT_BITRATE,
        help="audio bitrate for lossy targets (default: %s)" % DEFAULT_BITRATE,
    )
    parser.add_argument(
        "-o",
        "--output",
        help="output directory (default: alongside each source file)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="number of parallel ffmpeg jobs (default: CPU count)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="overwrite the source file after the result is verified",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned work without running ffmpeg",
    )
    parser.add_argument("inputs", nargs="+", metavar="INPUT", help="audio files or directories")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.jobs < 1:
        print("error: --jobs must be at least 1", file=sys.stderr)
        return EXIT_USAGE

    if shutil.which("ffmpeg") is None:
        print("error: ffmpeg not found on PATH. Install it with: brew install ffmpeg", file=sys.stderr)
        return EXIT_NO_FFMPEG
    if shutil.which("ffprobe") is None:
        print("error: ffprobe not found on PATH. Install it with: brew install ffmpeg", file=sys.stderr)
        return EXIT_NO_FFMPEG

    output_dir = Path(args.output).expanduser() if args.output else None
    if output_dir is not None and args.replace:
        print("error: --output and --replace are mutually exclusive", file=sys.stderr)
        return EXIT_USAGE

    files, missing = collect_inputs(args.inputs)
    for path in missing:
        print("MISSING  %s" % path, file=sys.stderr)

    if not files:
        print("no audio files found")
        return EXIT_PARTIAL if missing else EXIT_OK

    input_roots = [Path(raw).expanduser().resolve() for raw in args.inputs if Path(raw).expanduser().is_dir()]

    if args.dry_run:
        print("dry run: no files will be written")

    counts = {"ok": 0, "skip": 0, "fail": 0}
    failures = []

    # Several sources can map to one destination (track.wav and track.flac both
    # become track.mp3). Detect that before submitting, otherwise the workers race
    # on the same path and one of the tracks is lost.
    by_destination = {}
    for source in files:
        destination = planned_destination(source, args.to, output_dir, input_roots, args.replace)
        by_destination.setdefault(destination_key(destination), []).append((source, destination))

    conflicting = set()
    for _, entries in sorted(by_destination.items()):
        if len(entries) < 2:
            continue
        sources = [source for source, _ in entries]
        detail = "destination conflict: %d sources map to %s" % (len(entries), entries[0][1])
        for source in sources:
            conflicting.add(source)
            counts["fail"] += 1
            print("FAIL %s -> %s" % (source.name, detail))
            failures.append((source, detail))

    pending = [source for source in files if source not in conflicting]

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(
                transcode_one,
                source,
                args.to,
                args.bitrate,
                output_dir,
                input_roots,
                args.replace,
                args.dry_run,
            ): source
            for source in pending
        }
        for future in concurrent.futures.as_completed(futures):
            source = futures[future]
            try:
                _, status, detail = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
                status, detail = "fail", "unexpected error: %s" % exc
            counts[status] += 1
            label = {"ok": "OK  ", "skip": "SKIP", "fail": "FAIL"}[status]
            print("%s %s -> %s" % (label, source.name, detail))
            if status == "fail":
                failures.append((source, detail))

    print()
    print(
        "summary: %d converted, %d skipped, %d failed (target: %s)"
        % (counts["ok"], counts["skip"], counts["fail"], args.to)
    )
    if failures:
        print("failed files:")
        for source, detail in failures:
            print("  %s: %s" % (source, detail))

    if counts["fail"] or missing:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
