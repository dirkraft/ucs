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
from .dispatcher import _get_container, find_cc_binary, setup_container

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

    # Check CC binary is available on host
    if find_cc_binary() is None:
        raise click.ClickException(
            "Claude Code binary not found on host.\n"
            "Run: claude install"
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


# ---------------------------------------------------------------------------
# ucs shell
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("container")
def shell(container):
    """Shell into a UCS container by its full Docker container name."""
    docker_host = os.environ.get("DOCKER_HOST", f"unix:///run/user/{os.getuid()}/docker.sock")
    os.environ["DOCKER_HOST"] = docker_host  # ensure SDK picks it up

    import docker as docker_sdk
    try:
        c = _get_container(container)
    except docker_sdk.errors.DockerException as e:
        raise click.ClickException(f"Docker error: {e}")

    if c is None:
        raise click.ClickException(f"No container found: '{container}'.")

    if c.status != "running":
        click.echo(f"Starting container {container}…")
        c.start()

    os.execvpe(
        "docker",
        ["docker", "exec", "-it", container, "/bin/bash"],
        {**os.environ, "DOCKER_HOST": docker_host},
    )


# ---------------------------------------------------------------------------
# ucs config
# ---------------------------------------------------------------------------

@cli.group("config")
def config_group():
    """Manage UCS configuration."""


@config_group.command("test")
def config_test():
    """Test config and agent installation on the configured Docker image."""
    docker_host = os.environ.get("DOCKER_HOST", f"unix:///run/user/{os.getuid()}/docker.sock")
    os.environ["DOCKER_HOST"] = docker_host

    try:
        config = load_config()
    except ConfigError as e:
        raise click.ClickException(str(e))

    click.echo(f"Config:  {CONFIG_PATH}")
    click.echo(f"Image:   {config.docker.image}")

    cc_binary = find_cc_binary()
    if cc_binary is None:
        raise click.ClickException("Claude Code binary not found on host. Run: claude install")
    click.echo(f"CC bin:  {cc_binary}")

    import docker as docker_sdk
    import uuid

    test_name = f"ucs_test_{uuid.uuid4().hex[:8]}"
    click.echo(f"\nSpinning up test container '{test_name}'…")

    failed = False
    try:
        def emit(msg):
            click.echo(f"  {msg}")

        setup_container(test_name, config.docker.image, log_fn=emit)

        # Verify claude runs
        click.echo("  Verifying claude --version inside container…")
        import docker as docker_sdk
        container = _get_container(test_name)
        exec_user = __import__("ucs.dispatcher", fromlist=["agent_users"]).agent_users.get(test_name, "")
        result = container.exec_run(
            "claude --version",
            user=exec_user or None,
        )
        output = result.output.decode().strip()
        if result.exit_code != 0:
            click.echo(f"  ✗ claude --version failed (exit {result.exit_code}): {output}", err=True)
            failed = True
        else:
            click.echo(f"  ✓ {output}")

    except Exception as e:
        click.echo(f"  ✗ {e}", err=True)
        failed = True
    finally:
        click.echo(f"\nCleaning up test container…")
        try:
            c = _get_container(test_name)
            if c:
                c.remove(force=True)
                click.echo("  Removed.")
        except Exception as e:
            click.echo(f"  Warning: could not remove test container: {e}")

    if failed:
        raise click.ClickException("Config test failed.")
    click.echo("\n✓ Config test passed.")
