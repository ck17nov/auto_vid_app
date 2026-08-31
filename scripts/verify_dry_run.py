#!/usr/bin/env python
"""Verify a dry-run job produced every artifact the spec requires (section 36).

    python scripts/verify_dry_run.py                 # newest job in the workspace
    python scripts/verify_dry_run.py <job_dir>

Exits non-zero if anything required is missing or malformed, so it can be used
as a CI gate.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.core.config import load_config          # noqa: E402
from engine.core.util import probe_json               # noqa: E402

# (path, required, description)
REQUIRED = [
    ("research.json", True, "research corpus"),
    ("idea.json", True, "content ideas + gaps"),
    ("script.json", True, "script + retention report"),
    ("assets", True, "generated/licensed images"),
    ("asset_manifest.json", True, "asset source + licence record"),
    ("voice.wav", True, "narration"),
    ("captions.ass", True, "animated captions"),
    ("captions.srt", True, "YouTube caption track"),
    ("master.wav", True, "final audio mix"),
    ("video.mp4", True, "rendered video"),
    ("metadata.json", True, "title/description/tags"),
    ("quality_report.json", True, "quality gate result"),
    ("originality_report.json", True, "originality report"),
    ("factcheck_report.json", True, "fact-check report"),
    ("niche_profile.json", True, "resolved niche profile"),
    ("render_report.json", True, "render diagnostics"),
    ("music.wav", False, "background music bed"),
    ("sfx.wav", False, "transition sound effects"),
    ("thumbnails", False, "thumbnail variants (optional for Shorts)"),
]

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def newest_job(workspace: Path) -> Path | None:
    jobs = workspace / "jobs"
    if not jobs.exists():
        return None
    candidates = [p for p in jobs.iterdir() if p.is_dir()]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def check_json(path: Path) -> tuple[bool, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return False, f"invalid JSON: {exc}"
    if isinstance(data, dict) and not data:
        return False, "empty object"
    return True, f"{len(json.dumps(data))} bytes of JSON"


def check_video(path: Path) -> tuple[bool, str]:
    probe = probe_json(path)
    streams = probe.get("streams", []) or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        return False, "no video stream"
    if audio is None:
        return False, "no audio stream"
    duration = float((probe.get("format") or {}).get("duration") or 0)
    if duration <= 1.0:
        return False, f"duration {duration:.2f}s is implausible"
    return True, (f"{video.get('width')}x{video.get('height')} "
                  f"{video.get('codec_name')}/{video.get('pix_fmt')} "
                  f"{duration:.2f}s, audio {audio.get('codec_name')} "
                  f"{audio.get('channels')}ch")


def main() -> int:
    if len(sys.argv) > 1:
        job_dir = Path(sys.argv[1])
    else:
        cfg = load_config()
        found = newest_job(cfg.workspace)
        if found is None:
            print(f"{RED}no jobs found in {cfg.workspace / 'jobs'}{RESET}")
            print("run:  python -m backend.cli run --niche science --dry-run")
            return 2
        job_dir = found

    if not job_dir.exists():
        print(f"{RED}job directory not found: {job_dir}{RESET}")
        return 2

    print(f"Verifying dry-run artifacts in:\n  {job_dir}\n")
    failures: list[str] = []
    warnings: list[str] = []

    for name, required, description in REQUIRED:
        path = job_dir / name
        if not path.exists():
            if required:
                failures.append(f"{name} missing ({description})")
                print(f"  {RED}MISSING{RESET} {name:26} {description}")
            else:
                warnings.append(f"{name} absent ({description})")
                print(f"  {YELLOW}absent {RESET} {name:26} {description}")
            continue

        if path.is_dir():
            count = len(list(path.iterdir()))
            if count == 0 and required:
                failures.append(f"{name}/ is empty")
                print(f"  {RED}EMPTY  {RESET} {name:26} {description}")
            else:
                print(f"  {GREEN}OK     {RESET} {name:26} {count} files")
            continue

        if name.endswith(".json"):
            ok, detail = check_json(path)
        elif name == "video.mp4":
            ok, detail = check_video(path)
        else:
            size = path.stat().st_size
            ok = size > 500
            detail = f"{size / 1024:.0f} KB"

        if ok:
            print(f"  {GREEN}OK     {RESET} {name:26} {detail}")
        else:
            failures.append(f"{name}: {detail}")
            print(f"  {RED}BAD    {RESET} {name:26} {detail}")

    # Cross-check the quality report against the actual file.
    quality_path = job_dir / "quality_report.json"
    if quality_path.exists():
        report = json.loads(quality_path.read_text(encoding="utf-8"))
        print(f"\nQuality score: {report.get('score')}/100  "
              f"passed={report.get('passed')}")
        for blocker in report.get("blockers", []):
            print(f"  {RED}BLOCKER{RESET} {blocker}")
        for warning in report.get("warnings", [])[:6]:
            print(f"  {YELLOW}warn   {RESET} {warning}")

    orig_path = job_dir / "originality_report.json"
    if orig_path.exists():
        orig = json.loads(orig_path.read_text(encoding="utf-8"))
        print(f"\nOriginality: passed={orig.get('passed')}  "
              f"vs research={orig.get('max_similarity_to_research')}  "
              f"vs own scripts={orig.get('self_similarity')}")

    print()
    if failures:
        print(f"{RED}FAILED{RESET} - {len(failures)} required artifact problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    if warnings:
        print(f"{GREEN}PASSED{RESET} with {len(warnings)} optional artifact(s) absent.")
    else:
        print(f"{GREEN}PASSED{RESET} - every artifact present and valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
