#!/usr/bin/env python
"""Verify the local ffmpeg actually has everything the render pipeline needs.

    python scripts/check_ffmpeg.py

Written for the Oracle Cloud (or any Linux) migration. `ffmpeg -version`
succeeding proves almost nothing: distro builds routinely ship without libass,
in which case `subtitles=` fails and every video renders with NO CAPTIONS AT
ALL - and it fails at the last step, after the voice, images and per-scene
clips have already been produced. Better to find out in two seconds.

Exits non-zero if anything required is missing, so it can gate a deploy.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.core.util import ffmpeg_bin, run  # noqa: E402

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

# Every filter the engine actually invokes, with what breaks without it.
FILTERS: list[tuple[str, str]] = [
    ("zoompan", "Ken Burns motion - every scene would be a static frame"),
    ("xfade", "cross-fades between scenes; also the batched long-form render"),
    ("subtitles", "burnt-in captions (needs libass)"),
    ("sidechaincompress", "ducking music under the voice"),
    ("loudnorm", "the -14 LUFS master; YouTube would re-level it badly"),
    ("silencedetect", "quality gate: long-silence check"),
    ("volumedetect", "quality gate: audio level check"),
    ("alimiter", "peak limiting; without it the master can clip"),
    ("dynaudnorm", "voice levelling"),
    ("acompressor", "voice compression"),
    ("equalizer", "voice presence EQ"),
    ("highpass", "rumble removal"),
    ("lowpass", "sibilance control"),
    ("amix", "mixing voice + music + SFX"),
    ("asplit", "side-chain key routing"),
    ("areverse", "trailing-silence trim"),
    ("silenceremove", "trailing-silence trim"),
    ("anoisesrc", "synthesised music bed"),
    ("sine", "synthesised music bed"),
    ("tremolo", "music bed movement"),
    ("aecho", "music bed depth"),
    ("aformat", "channel/rate conforming"),
    ("scale", "geometry and the limited-range colour conversion"),
    ("crop", "geometry"),
    ("setsar", "pixel aspect ratio"),
    ("eq", "contrast/saturation grade"),
]

ENCODERS: list[tuple[str, str]] = [
    ("libx264", "video encoding - there is no fallback"),
    ("aac", "audio encoding for the MP4"),
    ("pcm_s16le", "intermediate WAV writing"),
]

# Build flags whose absence is not fatal but changes behaviour.
CONFIG_FLAGS: list[tuple[str, str]] = [
    ("--enable-libass", "required for burnt-in captions"),
    ("--enable-libfreetype", "font rasterisation"),
]


def _capture(args: list[str]) -> str:
    try:
        proc = subprocess.run([ffmpeg_bin(), *args], capture_output=True,
                              text=True, timeout=60)
    except Exception as exc:
        print(f"{RED}cannot run ffmpeg: {exc}{RESET}")
        sys.exit(2)
    return (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    banner = _capture(["-hide_banner", "-version"])
    first = banner.splitlines()[0] if banner else "unknown"
    print(f"ffmpeg: {ffmpeg_bin()}")
    print(f"        {first}\n")

    failures: list[str] = []
    warnings: list[str] = []

    filters = _capture(["-hide_banner", "-filters"])
    print("Filters")
    for name, why in FILTERS:
        # Match the filter NAME column, not a substring of some description.
        present = any(
            name in line.split()[1:2] for line in filters.splitlines()
            if len(line.split()) > 1)
        if present:
            print(f"  {GREEN}OK     {RESET} {name}")
        else:
            failures.append(f"filter `{name}` missing - {why}")
            print(f"  {RED}MISSING{RESET} {name:20} {why}")

    encoders = _capture(["-hide_banner", "-encoders"])
    print("\nEncoders")
    for name, why in ENCODERS:
        if name in encoders:
            print(f"  {GREEN}OK     {RESET} {name}")
        else:
            failures.append(f"encoder `{name}` missing - {why}")
            print(f"  {RED}MISSING{RESET} {name:20} {why}")

    print("\nBuild configuration")
    for flag, why in CONFIG_FLAGS:
        if flag in banner:
            print(f"  {GREEN}OK     {RESET} {flag}")
        else:
            warnings.append(f"{flag} not in the build - {why}")
            print(f"  {YELLOW}absent {RESET} {flag:24} {why}")

    # A real end-to-end smoke test. Distro builds have shipped `subtitles`
    # listed but non-functional, so listing it is not proof.
    print("\nSmoke test: burn a caption onto a generated frame")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ass = Path(tmp) / "t.ass"
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 240\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour\n"
            "Style: D,Sans,28,&H00FFFFFF\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,D,HELLO\n",
            encoding="utf-8")
        out = Path(tmp) / "t.mp4"
        arg = str(ass).replace("\\", "/").replace(":", r"\:")
        try:
            run([ffmpeg_bin(), "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=navy:s=320x240:d=1",
                 "-vf", f"subtitles=filename='{arg}',format=yuv420p",
                 "-frames:v", "10", "-c:v", "libx264", str(out)], timeout=120)
            ok = out.exists() and out.stat().st_size > 500
        except Exception as exc:
            ok = False
            failures.append(f"caption burn-in smoke test failed: {str(exc)[:200]}")
        print(f"  {GREEN}OK{RESET}" if ok else f"  {RED}FAILED{RESET}")

    print()
    if failures:
        print(f"{RED}FAILED{RESET} - {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        print("\nOn Debian/Ubuntu, the full build is:  sudo apt install -y ffmpeg")
        print("If captions are the problem, the distro build lacks libass.")
        return 1
    if warnings:
        print(f"{GREEN}PASSED{RESET} with {len(warnings)} note(s):")
        for w in warnings:
            print(f"  - {w}")
    else:
        print(f"{GREEN}PASSED{RESET} - ffmpeg has everything the pipeline needs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
