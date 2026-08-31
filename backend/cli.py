"""AutoTube AI command line interface (spec section 34).

    autotube doctor
    autotube auth login
    autotube research --niche "science"
    autotube run --niche "science" --length 45 --language en --at "20:00"
    autotube jobs list
    autotube jobs show <job_id>
    autotube approve <job_id>
    autotube upload --job <job_id>
    autotube analytics
    autotube serve
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# Allow `python backend/cli.py` as well as `python -m backend.cli`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.core.config import load_config                       # noqa: E402
from engine.core.db import Database                              # noqa: E402
from engine.core.models import AutomationRequest, JobStatus  # noqa: E402
from engine.core.niche import build_profile                      # noqa: E402
from engine.core.util import have_ffmpeg, which                  # noqa: E402

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="AutoTube AI - automated YouTube research, "
                       "production and publishing.")
auth_app = typer.Typer(no_args_is_help=True, help="YouTube account (OAuth 2.0).")
jobs_app = typer.Typer(no_args_is_help=True, help="Inspect and act on jobs.")
app.add_typer(auth_app, name="auth")
app.add_typer(jobs_app, name="jobs")

console = Console()


def _pipeline():
    from engine.pipeline import Pipeline
    return Pipeline(load_config())


def _status_colour(status: str) -> str:
    return {
        JobStatus.PUBLISHED.value: "green",
        JobStatus.SCHEDULED.value: "cyan",
        JobStatus.READY.value: "green",
        JobStatus.AWAITING_APPROVAL.value: "yellow",
        JobStatus.FAILED.value: "red",
        JobStatus.REJECTED.value: "red",
    }.get(status, "white")


# ==========================================================================
@app.command()
def doctor() -> None:
    """Check the environment and report exactly what is missing."""
    cfg = load_config()
    table = Table(title="AutoTube AI environment", show_lines=False)
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Detail")

    def row(name: str, ok: bool | None, detail: str) -> None:
        mark = "[green]OK[/green]" if ok else (
            "[yellow]OPTIONAL[/yellow]" if ok is None else "[red]MISSING[/red]")
        table.add_row(name, mark, detail)

    row("ffmpeg", have_ffmpeg(), which("ffmpeg") or "not on PATH - required")
    row("ffprobe", bool(which("ffprobe")), which("ffprobe") or "not on PATH")

    row("YouTube API key", cfg.has_secret("YOUTUBE_API_KEY"),
        "research needs this (free, no card)")
    row("YouTube OAuth client",
        cfg.has_secret("YOUTUBE_CLIENT_ID") and cfg.has_secret("YOUTUBE_CLIENT_SECRET"),
        "upload + analytics need this")

    from engine.content.llm import LLMError, LLMRouter
    router = LLMRouter(list(cfg.get("content.llm_provider_order", [])), cfg)
    usable = [p.name for p in router.usable]
    row("LLM provider", bool(usable),
        f"usable: {', '.join(usable) or 'none - scripts fall back to template'}")

    # Which model actually answers, not which one is configured. Model access
    # varies by account: a real Groq key here could not use
    # llama-3.3-70b-versatile at all and fell forward to gpt-oss-120b, which is
    # correct behaviour but worth surfacing - you should know what is writing
    # your scripts, and pinning the winner in .env saves one 404 per run.
    for provider in router.usable:
        if provider.name not in ("groq", "gemini"):
            continue
        try:
            # Plain text, and generous headroom. A 32-token JSON-mode probe
            # failed against both providers for uninteresting reasons: Groq
            # truncated mid-object and rejected its own output, and Gemini 3.x
            # spends tokens reasoning before it emits any, so it returned a
            # candidate with no parts at all. Neither meant the key was bad.
            result = provider.complete(
                "Reply with the single word: ready", max_tokens=512)
        except LLMError as exc:
            row(f"  {provider.name} model", False, str(exc)[:64])
            continue
        pinned = str(cfg.secret(f"{provider.name.upper()}_MODEL") or "")
        note = f"{result.model} answered"
        if pinned and pinned != result.model:
            note += f"  (you pinned {pinned}, which failed)"
        elif not pinned:
            note += f"  (pin it: {provider.name.upper()}_MODEL={result.model})"
        row(f"  {provider.name} model", True, note)

    from engine.tts.providers import build_providers
    tts = [p.name for p in build_providers(list(cfg.get("tts.provider_order", [])))
           if p.available()]
    row("TTS provider", bool(tts), f"usable: {', '.join(tts) or 'none'}")

    row("Pexels key", None if not cfg.has_secret("PEXELS_API_KEY") else True,
        "sharper stock photos (free key)")
    row("Pixabay key", None if not cfg.has_secret("PIXABAY_API_KEY") else True,
        "sharper stock photos (free key)")

    from engine.video.fonts import display_font
    try:
        font, family = display_font()
        row("Caption font", True, f"{family} ({font.name})")
    except Exception as exc:
        row("Caption font", False, str(exc)[:70])

    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(str(cfg.get("timezone.default", "Asia/Kolkata")))
        row("Timezone data", True, str(cfg.get("timezone.default")))
    except Exception:
        row("Timezone data", False, "pip install tzdata")

    auth_ok = False
    try:
        from engine.youtube.auth import YouTubeAuth
        auth_ok = YouTubeAuth(cfg).authorized
    except Exception:
        pass
    row("YouTube authorised", auth_ok, "run: autotube auth login")

    console.print(table)
    console.print(Panel(
        f"workspace: {cfg.workspace}\n"
        f"DRY_RUN: {cfg.dry_run}   upload_enabled: "
        f"{cfg.get('youtube.upload_enabled')}\n"
        f"approval_required: {cfg.get('automation.approval_required')}   "
        f"min quality: {cfg.get('quality.minimum_score')}",
        title="config", expand=False))

    # Long-form has its own preconditions, and the failure mode without them is
    # slow and confusing: you discover it after the render, not before it.
    max_scenes = int(cfg.get("content.max_scenes", 400))
    hosted = [n for n in usable if n in ("groq", "gemini")]
    if hosted:
        longform = [f"Long-form   [green]ready[/green] via {', '.join(hosted)}, "
                    f"up to {max_scenes} scenes/video"]
    elif "ollama" in usable:
        # Sectioned generation makes one call per section, so a slow local
        # model multiplies. Measured here: >10 min per call on CPU. The run
        # bails out of sectioned mode and template-fills instead, which is
        # honest but not what you want from a 20-minute video.
        longform = [
            "Long-form   [yellow]degraded[/yellow] - only ollama is usable",
            "            A long script needs ~14 calls, and CPU-only ollama",
            "            measured over 10 min each, so most sections would",
            "            fall back to the template builder.",
            "            Set GROQ_API_KEY or GEMINI_API_KEY (free) to fix.",
        ]
    else:
        longform = [
            "Long-form   [red]blocked[/red] - no LLM configured",
            "            The template builder cannot honestly fill more than",
            "            ~2 minutes of narration. Set GROQ_API_KEY or",
            "            GEMINI_API_KEY (free, no credit card).",
        ]
    lines = [
        "Shorts      [green]ready[/green], up to 3 minutes" if tts else
        "Shorts      [red]blocked[/red] - no TTS provider",
        *longform,
        "",
        "YouTube caps uploads at 15 minutes until the channel is",
        "verified (free: Studio > Settings > Channel > Feature",
        "eligibility). Nothing here generates video, so length is",
        "bounded by render time, not by a service quota.",
        "See docs/SERVICE_COSTS.md section 6a.",
    ]
    console.print(Panel("\n".join(lines), title="video length", expand=False))


# ==========================================================================
@auth_app.command("login")
def auth_login(port: int = typer.Option(8765, help="local redirect port")) -> None:
    """Authorise a YouTube account (opens a browser)."""
    from engine.youtube.auth import YouTubeAuth
    auth = YouTubeAuth(load_config())
    auth.login_local_server(port=port)
    console.print("[green]Authorised.[/green]")
    for ch in auth.channels():
        console.print(f"  {ch['title']} ({ch['channel_id']}) - "
                      f"{ch['subscribers']:,} subs, {ch['videos']} videos")


@auth_app.command("import-token")
def auth_import(refresh_token: str = typer.Argument(...)) -> None:
    """Import a refresh token obtained by the Android app."""
    from engine.youtube.auth import YouTubeAuth
    YouTubeAuth(load_config()).import_refresh_token(refresh_token)
    console.print("[green]Token stored.[/green]")


@auth_app.command("channels")
def auth_channels() -> None:
    """List the authorised account's channels."""
    from engine.youtube.auth import YouTubeAuth
    for ch in YouTubeAuth(load_config()).channels():
        console.print(f"{ch['channel_id']}  {ch['title']}  "
                      f"{ch['subscribers']:,} subs")


@auth_app.command("logout")
def auth_logout() -> None:
    """Delete stored credentials."""
    from engine.youtube.auth import YouTubeAuth
    YouTubeAuth(load_config()).logout()
    console.print("Credentials cleared.")


# ==========================================================================
@app.command()
def research(
    niche: str = typer.Option(..., "--niche", "-n"),
    fmt: str = typer.Option("SHORT", "--format", help="SHORT | LONGFORM"),
    limit: int = typer.Option(12, help="rows to display"),
    out: Optional[Path] = typer.Option(None, help="write full JSON here"),
) -> None:
    """Research a niche and print the scored opportunities."""
    from engine.research.gaps import cluster_videos, find_gaps
    pipe = _pipeline()
    profile = build_profile(niche)
    videos = pipe.research_engine.research(niche, profile, video_format=fmt)

    table = Table(title=f"Research: {niche}")
    for col in ("Viral", "CTRpot", "Views", "Views/day", "Ch.rel", "Age",
                "Break", "Title"):
        table.add_column(col)
    for v in videos[:limit]:
        table.add_row(f"{v.viral_score:.1f}", f"{v.ctr_potential_score:.0f}",
                      f"{v.views:,}", f"{v.view_velocity:,.0f}",
                      f"{v.performance_ratio:.1f}x", f"{v.age_days:.0f}d",
                      "YES" if v.is_breakout else "", v.title[:52])
    console.print(table)

    clusters = cluster_videos(videos)
    gaps = find_gaps(clusters, videos)
    gap_table = Table(title="Content gaps")
    gap_table.add_column("Score")
    gap_table.add_column("Topic")
    gap_table.add_column("Missing angle")
    for g in gaps[:6]:
        gap_table.add_row(f"{g.gap_score:.2f}", g.topic,
                          (g.missing_angles[0] if g.missing_angles else "-")[:56])
    console.print(gap_table)
    console.print(f"[dim]YouTube quota used today: {pipe.quota.used()}/"
                  f"{pipe.quota.limit} units[/dim]")

    if out:
        out.write_text(json.dumps(
            {"videos": [v.to_dict() for v in videos],
             "clusters": [c.to_dict() for c in clusters],
             "gaps": [g.to_dict() for g in gaps]}, indent=2), encoding="utf-8")
        console.print(f"wrote {out}")
    pipe.close()


# ==========================================================================
@app.command()
def run(
    niche: str = typer.Option(..., "--niche", "-n"),
    length: int = typer.Option(45, "--length", "-l", help="target seconds"),
    language: str = typer.Option("en", "--language"),
    audience: str = typer.Option("18-35", "--audience"),
    style: str = typer.Option("fast-paced, curiosity-driven", "--style"),
    fmt: str = typer.Option("SHORT", "--format", help="SHORT | LONGFORM"),
    at: str = typer.Option("", "--at", help="local publish time, e.g. 20:00"),
    timezone: str = typer.Option("", "--timezone"),
    frequency: str = typer.Option("once", "--frequency",
                                  help="once | daily | weekly | days"),
    count: int = typer.Option(1, "--count", help="number of videos"),
    mode: str = typer.Option("", "--mode", help="AUTO | APPROVAL"),
    kids: bool = typer.Option(False, "--kids", help="child-directed content"),
    dry_run: Optional[bool] = typer.Option(None, "--dry-run/--no-dry-run"),
) -> None:
    """Run the full pipeline: research -> script -> voice -> video -> checks."""
    cfg = load_config()
    if dry_run is not None:
        cfg.set("dry_run", dry_run)
    from engine.pipeline import Pipeline, PipelineError
    pipe = Pipeline(cfg)

    request = AutomationRequest(
        niche=niche, audience=audience, language=language, video_format=fmt,
        duration_seconds=length, style=style, count=count,
        mode=(mode or ("APPROVAL" if cfg.get("automation.approval_required")
                       else "AUTO")).upper(),
        frequency=frequency, upload_time=at,
        timezone=timezone or str(cfg.get("timezone.default", "Asia/Kolkata")),
        made_for_kids=kids)

    failures = 0
    for i in range(max(1, count)):
        if count > 1:
            console.rule(f"video {i + 1}/{count}")
        try:
            result = pipe.run(request)
        except PipelineError as exc:
            failures += 1
            console.print(f"[red]FAILED:[/red] {exc}")
            continue
        job = result.job
        q = result.quality
        console.print(Panel(
            f"job:      {job.job_id}\n"
            f"status:   [{_status_colour(job.status)}]{job.status}[/]\n"
            f"title:    {(job.metadata or {}).get('title', '-')}\n"
            f"quality:  {q.score if q else 0:.0f}/100 "
            f"(min {cfg.get('quality.minimum_score')})\n"
            f"video:    {job.video_path}\n"
            f"dir:      {job.dir}",
            title="result", expand=False))
        if q and q.blockers:
            console.print("[red]blockers:[/red] " + "; ".join(q.blockers))
        if q and q.warnings:
            console.print("[yellow]warnings:[/yellow] " + "; ".join(q.warnings[:4]))
    pipe.close()
    if failures:
        raise typer.Exit(code=1)


# ==========================================================================
@jobs_app.command("list")
def jobs_list(status: str = typer.Option("", "--status"),
              limit: int = typer.Option(20, "--limit")) -> None:
    """List jobs, newest first."""
    cfg = load_config()
    db = Database(cfg.workspace / "autotube.db")
    jobs = db.list_jobs(status.upper() or None, limit=limit)
    table = Table(title="Jobs")
    for col in ("Job", "Status", "Quality", "Niche", "Title", "Retries"):
        table.add_column(col)
    for j in jobs:
        table.add_row(
            j.job_id, f"[{_status_colour(j.status)}]{j.status}[/]",
            f"{(j.quality or {}).get('score', 0):.0f}",
            (j.request or {}).get("niche", "-"),
            ((j.metadata or {}).get("title", "-") or "-")[:44],
            str(j.retry_count))
    console.print(table)
    db.close()


@jobs_app.command("show")
def jobs_show(job_id: str = typer.Argument(...)) -> None:
    """Show one job in full, including quality checks."""
    cfg = load_config()
    db = Database(cfg.workspace / "autotube.db")
    job = db.get_job(job_id)
    if job is None:
        console.print(f"[red]unknown job {job_id}[/red]")
        raise typer.Exit(1)
    meta = job.metadata or {}
    quality = job.quality or {}
    console.print(Panel(
        f"status:   [{_status_colour(job.status)}]{job.status}[/]\n"
        f"niche:    {(job.request or {}).get('niche')}\n"
        f"title:    {meta.get('title', '-')}\n"
        f"quality:  {quality.get('score', 0):.0f}/100 "
        f"passed={quality.get('passed')}\n"
        f"video:    {job.video_path}\n"
        f"error:    {job.error or '-'}",
        title=job.job_id, expand=False))
    if quality.get("checks"):
        table = Table(title="quality checks")
        table.add_column("Check")
        table.add_column("")
        table.add_column("Detail")
        for c in quality["checks"]:
            mark = "[green]OK[/green]" if c["passed"] else (
                "[red]BLOCK[/red]" if c["blocking"] else "[yellow]warn[/yellow]")
            table.add_row(c["name"], mark, str(c["detail"])[:64])
        console.print(table)
    for line in job.logs[-12:]:
        console.print(f"[dim]{line}[/dim]")
    db.close()


@app.command()
def approve(job_id: str = typer.Argument(...)) -> None:
    """Approve a job waiting for review, then upload/schedule it."""
    pipe = _pipeline()
    result = pipe.approve(job_id)
    console.print(json.dumps(result, indent=2))
    pipe.close()


@app.command()
def reject(job_id: str = typer.Argument(...),
           reason: str = typer.Option("", "--reason")) -> None:
    """Reject a job."""
    pipe = _pipeline()
    job = pipe.reject(job_id, reason)
    console.print(f"{job.job_id} -> {job.status}")
    pipe.close()


@app.command()
def upload(job: str = typer.Option(..., "--job"),
           publish_now: bool = typer.Option(False, "--now",
                                            help="ignore the schedule")) -> None:
    """Upload an existing READY job."""
    from engine.core.models import VideoMetadata
    pipe = _pipeline()
    video_job = pipe.db.get_job(job)
    if video_job is None:
        console.print(f"[red]unknown job {job}[/red]")
        raise typer.Exit(1)
    request = AutomationRequest.from_dict(video_job.request or {})
    if publish_now:
        request.upload_time = ""
        request.frequency = "once"
    meta = VideoMetadata.from_dict(video_job.metadata or {})
    result = pipe.publish_now(video_job, request, meta)
    console.print(json.dumps(result, indent=2))
    pipe.close()


# ==========================================================================
@app.command()
def analytics(days: int = typer.Option(28, "--days")) -> None:
    """Collect own-channel analytics and update the learned strategy."""
    pipe = _pipeline()
    report = pipe.collect_analytics(days=days)
    if report["collected"]:
        table = Table(title="Collected analytics (own channel only)")
        for col in ("Video", "Views", "Retention", "AvgDur", "Subs", "CTR"):
            table.add_column(col)
        for s in report["collected"]:
            table.add_row(s["video_id"], f"{s['views']:,}",
                          f"{s['avg_view_percentage']:.1f}%",
                          f"{s['avg_view_duration']:.0f}s",
                          str(s["subscribers_gained"]),
                          f"{s['ctr']:.2f}%" if s["ctr_available"] else "n/a")
        console.print(table)
    else:
        console.print("[yellow]no analytics yet - publish a video first[/yellow]")
    if report["insights"]:
        table = Table(title="Learned strategy (weighted statistics)")
        for col in ("Dimension", "Value", "n", "Weight", "AvgRetention"):
            table.add_column(col)
        for i in report["insights"][:16]:
            table.add_row(i["dimension"], i["value"], str(i["samples"]),
                          f"{i['weight']:.2f}x", f"{i['avg_retention']:.1f}%")
        console.print(table)
    if report["hints"]:
        console.print(Panel(report["hints"], title="applied to future videos"))
    pipe.close()


@app.command()
def resume() -> None:
    """Recover jobs interrupted by a crash or reboot."""
    pipe = _pipeline()
    ids = pipe.resume_pending()
    console.print(f"recoverable jobs: {ids or 'none'}")
    pipe.close()


@app.command()
def quota() -> None:
    """Show YouTube API quota spend for today."""
    pipe = _pipeline()
    used, limit = pipe.quota.used(), pipe.quota.limit
    console.print(Panel(
        f"used today:      {used}/{limit} units\n"
        f"reserved for uploads: {pipe.quota.reserve}\n"
        f"available for research: {pipe.quota.remaining()}\n\n"
        f"search.list = {pipe.quota.cost('search_list')} units\n"
        f"videos.insert = {pipe.quota.cost('video_insert')} units\n"
        f"=> at most {(limit // pipe.quota.cost('video_insert'))} uploads/day "
        f"on the default quota",
        title="YouTube API quota (resets midnight US Pacific)", expand=False))
    pipe.close()


@app.command()
def prune(keep_days: int = typer.Option(14, "--keep-days",
                                        help="delete job folders older than this"),
          keep_last: int = typer.Option(10, "--keep-last",
                                        help="always keep this many newest jobs"),
          videos_only: bool = typer.Option(
              False, "--videos-only",
              help="delete only the large media, keep the JSON reports"),
          yes: bool = typer.Option(False, "--yes", help="do not ask")) -> None:
    """Delete old job output to reclaim disk.

    A finished job folder is 30-150 MB, almost all of it video and WAV. On a
    laptop that is a rounding error; on an Oracle Always Free instance it fills
    the boot volume in a few weeks and the next render dies mid-encode with a
    confusing ffmpeg error.

    Published videos live on YouTube, so the local copy is only useful for
    review. `--videos-only` keeps every report and originality/quality record
    while dropping the media, which is usually the right trade.
    """
    import shutil
    import time as _time

    cfg = load_config()
    jobs_dir = cfg.workspace / "jobs"
    if not jobs_dir.exists():
        console.print("no jobs directory yet")
        return

    folders = sorted((p for p in jobs_dir.iterdir() if p.is_dir()),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    protected = set(folders[:max(0, keep_last)])
    cutoff = _time.time() - keep_days * 86400

    def folder_size(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    MEDIA = {".mp4", ".wav", ".mp3", ".jpg", ".jpeg", ".png", ".webp"}
    targets: list[tuple[Path, int]] = []
    for folder in folders:
        if folder in protected or folder.stat().st_mtime >= cutoff:
            continue
        if videos_only:
            size = sum(f.stat().st_size for f in folder.rglob("*")
                       if f.is_file() and f.suffix.lower() in MEDIA)
            if size == 0:
                continue          # already pruned
        else:
            size = folder_size(folder)
        targets.append((folder, size))

    if not targets:
        console.print(f"nothing to prune (keeping the newest {keep_last} and "
                      f"anything under {keep_days} days old)")
        return

    total = sum(size for _, size in targets)
    what = "media files in" if videos_only else "entire folders"
    console.print(Panel(
        "\n".join(f"{p.name:44} {s / 1e6:8.1f} MB" for p, s in targets[:15])
        + (f"\n... and {len(targets) - 15} more" if len(targets) > 15 else "")
        + f"\n\nwould free {total / 1e6:.0f} MB from {len(targets)} {what}",
        title="prune candidates", expand=False))

    if not yes and not typer.confirm("delete these?"):
        console.print("cancelled")
        return

    freed = 0
    for folder, size in targets:
        try:
            if videos_only:
                for f in folder.rglob("*"):
                    if f.is_file() and f.suffix.lower() in MEDIA:
                        f.unlink(missing_ok=True)
            else:
                shutil.rmtree(folder)
            freed += size
        except OSError as exc:
            console.print(f"[yellow]skipped {folder.name}: {exc}[/yellow]")
    console.print(f"[green]freed {freed / 1e6:.0f} MB[/green]")


@app.command()
def serve(host: str = typer.Option("127.0.0.1", "--host"),
          port: int = typer.Option(8099, "--port"),
          reload: bool = typer.Option(False, "--reload")) -> None:
    """Start the backend HTTP API the Android app talks to."""
    import uvicorn
    console.print(f"Backend on http://{host}:{port}  (docs at /docs)")
    uvicorn.run("backend.api.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
