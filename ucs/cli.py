"""
UCS CLI — stack management and other tooling.

Entry point: ucs (defined in pyproject.toml)
"""

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
    inner_cmd = (
        f"PYTHONUNBUFFERED=1 {python} -m ucs.dispatcher 2>&1 | tee {log_path}"
    )

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", TMUX_SESSION, inner_cmd],
        check=True,
    )

    click.echo(f"Stack started in tmux session '{TMUX_SESSION}'.")
    click.echo(f"Log: {log_path}")
    click.echo(f"Attach: tmux attach -t {TMUX_SESSION}")
