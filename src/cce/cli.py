"""CLI management commands.

cce api start              — run the FastAPI server
cce api key generate       — generate and print a new API key
cce api key list           — list active keys
cce api key revoke <hash>  — delete a key
cce emit-mdx               — emit MDX files from completed jobs
"""

from __future__ import annotations

import asyncio

import typer

app = typer.Typer(name="cce", help="Content Curation Engine CLI")
api_app = typer.Typer(name="api", help="API server management")
key_app = typer.Typer(name="key", help="API key management")
emit_app = typer.Typer(name="emit-mdx", help="Emit MDX files from completed jobs")

app.add_typer(api_app)
api_app.add_typer(key_app)
app.add_typer(emit_app, name="emit-mdx")


@api_app.command("start")
def start_server(
    host: str = typer.Option("0.0.0.0", help="Bind address"),
    port: int = typer.Option(8000, help="Bind port"),
    config: str | None = typer.Option(None, help="Path to config YAML"),
) -> None:
    """Start the CCE API server."""
    import uvicorn

    from cce.api.app import create_app
    from cce.config.loader import load_config

    engine_config = load_config(config)
    # CLI flags override config values
    engine_config.api.host = host
    engine_config.api.port = port

    application = create_app(engine_config)
    uvicorn.run(application, host=host, port=port)


@key_app.command("generate")
def generate_key(
    label: str = typer.Option("", help="Human-readable label for the key"),
    config: str | None = typer.Option(None, help="Path to config YAML"),
) -> None:
    """Generate a new API key and print it (shown once only)."""
    from cce.api.auth import generate_api_key, hash_api_key

    async def _run() -> None:
        store = await _get_job_store(config)
        try:
            key = generate_api_key()
            key_hash = hash_api_key(key)
            await store.store_api_key(key_hash, label=label or None)

            typer.echo(f"API Key:  {key}")
            typer.echo(f"Hash:     {key_hash[:16]}...")
            if label:
                typer.echo(f"Label:    {label}")
            typer.echo("\nStore this key securely — it cannot be recovered.")
        finally:
            await store.close()

    asyncio.run(_run())


@key_app.command("list")
def list_keys(
    config: str | None = typer.Option(None, help="Path to config YAML"),
) -> None:
    """List active API keys."""

    async def _run() -> None:
        store = await _get_job_store(config)
        try:
            keys = await store.list_api_keys()
            if not keys:
                typer.echo("No API keys found.")
                return

            typer.echo(f"{'HASH (prefix)':<20} {'LABEL':<20} {'CREATED'}")
            typer.echo("-" * 60)
            for k in keys:
                hash_prefix = k["key_hash"][:16] + "..."
                label = k["label"] or ""
                created = k["created_at"][:19]
                typer.echo(f"{hash_prefix:<20} {label:<20} {created}")
        finally:
            await store.close()

    asyncio.run(_run())


@key_app.command("revoke")
def revoke_key(
    key_hash_prefix: str = typer.Argument(help="Hash prefix of the key to revoke"),
    config: str | None = typer.Option(None, help="Path to config YAML"),
) -> None:
    """Revoke an API key by hash prefix."""

    async def _run() -> None:
        store = await _get_job_store(config)
        try:
            keys = await store.list_api_keys()
            matches = [k for k in keys if k["key_hash"].startswith(key_hash_prefix)]

            if not matches:
                typer.echo(f"No key found matching prefix: {key_hash_prefix}")
                raise typer.Exit(1)

            if len(matches) > 1:
                typer.echo(
                    f"Ambiguous prefix — {len(matches)} keys match. "
                    "Provide a longer prefix."
                )
                raise typer.Exit(1)

            key_hash = matches[0]["key_hash"]
            await store.delete_api_key(key_hash)
            typer.echo(f"Revoked key: {key_hash[:16]}...")
        finally:
            await store.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# emit-mdx
# ---------------------------------------------------------------------------


@emit_app.callback(invoke_without_command=True)
def emit_mdx_command(
    job: str | None = typer.Option(None, help="Job ID to emit"),
    topic: str | None = typer.Option(
        None, help="Topic name (emits latest completed job)"
    ),
    all_jobs: bool = typer.Option(False, "--all", help="Emit all completed jobs"),
    target: str = typer.Option(..., help="Target content directory"),
    config: str | None = typer.Option(None, help="Path to config YAML"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview files without writing"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed stats"),
) -> None:
    """Emit MDX files from a completed curation job."""
    if sum([bool(job), bool(topic), all_jobs]) > 1:
        typer.echo("Error: --job, --topic, and --all are mutually exclusive", err=True)
        raise typer.Exit(1)
    if not job and not topic and not all_jobs:
        typer.echo("Error: provide --job, --topic, or --all", err=True)
        raise typer.Exit(1)

    from pathlib import Path

    from cce.models.job import JobStatus
    from cce.models.package import PublishPackage
    from cce.output.mdx import EmitResult, emit_mdx, slugify

    target_path = Path(target)
    if not target_path.is_dir():
        typer.echo(f"Error: target directory does not exist: {target}", err=True)
        raise typer.Exit(1)

    async def _fetch() -> list[tuple[PublishPackage, str | None, str | None]]:
        """Fetch packages from store. Returns list of (package, slug_override, topic_name)."""
        store = await _get_job_store(config)
        try:
            if all_jobs:
                jobs = await store.list_jobs(status=JobStatus.COMPLETED, limit=1000)
                if not jobs:
                    typer.echo("Error: no completed jobs found", err=True)
                    raise typer.Exit(1)
                packages: list[tuple[PublishPackage, str | None, str | None]] = []
                for j in jobs:
                    package = await store.get_package(j.id)
                    if package is not None:
                        packages.append((package, None, j.request.topic))
                if not packages:
                    typer.echo(
                        "Error: completed jobs found but none have packages", err=True
                    )
                    raise typer.Exit(1)
                return packages

            if job:
                package = await store.get_package(job)
                if package is None:
                    typer.echo(f"Error: no package found for job {job}", err=True)
                    raise typer.Exit(1)
                job_obj = await store.get_job(job)
                if job_obj is None:
                    typer.echo(f"Error: job record not found for {job}", err=True)
                    raise typer.Exit(1)
                return [(package, None, job_obj.request.topic)]
            else:
                # Find latest completed job for topic
                jobs = await store.list_jobs(
                    status=JobStatus.COMPLETED, topic=topic, limit=1
                )
                if not jobs:
                    typer.echo(
                        f"Error: no completed jobs for topic '{topic}'", err=True
                    )
                    raise typer.Exit(1)
                package = await store.get_package(jobs[0].id)
                if package is None:
                    typer.echo(f"Error: no package for job {jobs[0].id}", err=True)
                    raise typer.Exit(1)
                return [(package, topic, jobs[0].request.topic)]
        finally:
            await store.close()

    packages = asyncio.run(_fetch())

    if dry_run:
        for pkg, slug_override, topic_name in packages:
            slug = slug_override or slugify(topic_name or "")
            typer.echo(f"Would emit: {slug}/")
            for unit in pkg.units:
                typer.echo(f"  {unit.path}/page.mdx")
            typer.echo("  _evidence.json")
            typer.echo("  meta.json")
            file_count = len(pkg.units) + 2  # N page files + _evidence.json + meta.json
            typer.echo(f"  ({file_count} files, {len(pkg.evidence)} evidence)")
        typer.echo(f"Dry run — {len(packages)} topic(s), no files written")
        return

    results: list[EmitResult] = []
    for pkg, slug_override, topic_name in packages:
        results.append(
            emit_mdx(
                package=pkg,
                target_dir=target_path,
                topic_slug=slug_override,
                topic_name=topic_name,
            )
        )

    for result in results:
        typer.echo(f"Emitted: {result.topic_slug}/")
        slug_dir = result.target_dir
        for p in result.paths_written:
            if verbose:
                mdx_path = slug_dir / p / "page.mdx"
                if mdx_path.exists():
                    cite_count = mdx_path.read_text().count("[^")
                    size_kb = mdx_path.stat().st_size / 1024
                    typer.echo(
                        f"  {p}/page.mdx  ({cite_count} citations, {size_kb:.1f} KB)"
                    )
                else:
                    typer.echo(f"  {p}/page.mdx")
            else:
                typer.echo(f"  {p}/page.mdx")
        if verbose:
            ev_path = slug_dir / "_evidence.json"
            ev_size = (
                f"  ({ev_path.stat().st_size / 1024:.1f} KB)"
                if ev_path.exists()
                else ""
            )
            typer.echo(f"  _evidence.json{ev_size}")
        else:
            typer.echo("  _evidence.json")
        typer.echo("  meta.json")

    total_files = sum(r.files_written for r in results)
    typer.echo(f"Total: {total_files} files, {len(results)} topic(s) → {target_path}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_job_store(config_path: str | None = None):
    """Open a JobStore connection using config defaults."""
    from cce.config.loader import load_config
    from cce.jobs.store import JobStore

    config = load_config(config_path)
    store = JobStore(db_path=config.evidence_store.sqlite_path)
    await store.connect()
    return store
