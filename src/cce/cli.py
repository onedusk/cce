"""CLI management commands.

cce api start              — run the FastAPI server
cce api key generate       — generate and print a new API key
cce api key list           — list active keys
cce api key revoke <hash>  — delete a key
"""

from __future__ import annotations

import asyncio
from typing import Optional

import typer

app = typer.Typer(name="cce", help="Content Curation Engine CLI")
api_app = typer.Typer(name="api", help="API server management")
key_app = typer.Typer(name="key", help="API key management")

app.add_typer(api_app)
api_app.add_typer(key_app)


@api_app.command("start")
def start_server(
    host: str = typer.Option("0.0.0.0", help="Bind address"),
    port: int = typer.Option(8000, help="Bind port"),
    config: Optional[str] = typer.Option(None, help="Path to config YAML"),
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
    config: Optional[str] = typer.Option(None, help="Path to config YAML"),
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
    config: Optional[str] = typer.Option(None, help="Path to config YAML"),
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
    config: Optional[str] = typer.Option(None, help="Path to config YAML"),
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
