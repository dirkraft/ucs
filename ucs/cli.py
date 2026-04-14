"""
UCS CLI — stack management and other tooling.

Entry point: ucs (defined in pyproject.toml)
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import click

from .config import CONFIG_PATH, ConfigError, load_config

LOGS_DIR = Path.home() / ".local" / "share" / "ucs" / "logs"
TMUX_SESSION = "ucs-stack"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmux_session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def _kill_tmux_session(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
def cli():
    """UCS — Universal Container Session manager."""


# ---------------------------------------------------------------------------
# ucs stack
# ---------------------------------------------------------------------------

@cli.group()
def stack():
    """Manage the UCS stack."""


@stack.command()
@click.option("-r", "--restart", is_flag=True, help="Kill existing session before starting.")
def up(restart):
    """Start (or restart) the UCS stack in a tmux session."""

    if not shutil.which("tmux"):
        raise click.ClickException("tmux is not installed or not on PATH.")

    # Validate config — errors are user-facing
    try:
        load_config()
    except ConfigError as e:
        raise click.ClickException(str(e))

    # Check Docker image exists
    docker_host = os.environ.get("DOCKER_HOST", f"unix:///run/user/{os.getuid()}/docker.sock")
    result = subprocess.run(
        ["docker", "image", "inspect", "ucs_agent_claude"],
        capture_output=True,
        env={**os.environ, "DOCKER_HOST": docker_host},
    )
    if result.returncode != 0:
        raise click.ClickException(
            "Docker image 'ucs_agent_claude' not found.\n"
            f"Build it with: DOCKER_HOST={docker_host} docker build -t ucs_agent_claude docker/"
        )

    if _tmux_session_exists(TMUX_SESSION):
        if not restart:
            raise click.ClickException(
                f"Stack is already running in tmux session '{TMUX_SESSION}'.\n"
                "Use -r / --restart to kill it and start fresh."
            )
        click.echo(f"Stopping existing session '{TMUX_SESSION}'…")
        _kill_tmux_session(TMUX_SESSION)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOGS_DIR / f"dispatcher-{timestamp}.log"

    python = sys.executable
    docker_host = os.environ.get("DOCKER_HOST", f"unix:///run/user/{os.getuid()}/docker.sock")
    inner_cmd = (
        f"PYTHONUNBUFFERED=1 DOCKER_HOST={docker_host} {python} -m ucs.dispatcher 2>&1 | tee {log_path}"
    )

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", TMUX_SESSION, inner_cmd],
        check=True,
    )

    click.echo(f"Stack started in tmux session '{TMUX_SESSION}'.")
    click.echo(f"Log: {log_path}")
    click.echo(f"Attach: tmux attach -t {TMUX_SESSION}")


@stack.command()
def down():
    """Stop the UCS stack."""
    if not _tmux_session_exists(TMUX_SESSION):
        raise click.ClickException(f"Stack is not running (no tmux session '{TMUX_SESSION}').")
    _kill_tmux_session(TMUX_SESSION)
    click.echo(f"Stack stopped.")


@stack.command()
def status():
    """Show whether the UCS stack is running and the latest log."""
    running = _tmux_session_exists(TMUX_SESSION)
    click.echo(f"Stack:   {'running' if running else 'stopped'}")

    if LOGS_DIR.exists():
        logs = sorted(LOGS_DIR.glob("dispatcher-*.log"))
        if logs:
            latest = logs[-1]
            size = latest.stat().st_size
            click.echo(f"Log:     {latest} ({size:,} bytes)")
        else:
            click.echo("Log:     none")
