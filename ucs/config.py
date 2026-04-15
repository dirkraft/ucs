"""
Config loading from ~/.config/ucs/config.toml.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "ucs" / "config.toml"

DEFAULT_DOCKER_IMAGE = "debian:bookworm-slim"

TEMPLATE = """\
[slack]
# Bot token (xoxb-...) — from your Slack app's OAuth & Permissions page
bot_token = ""

# App-level token (xapp-...) — from your Slack app's Basic Information page (Socket Mode)
app_token = ""

[auth]
# Slack user IDs allowed to trigger agent activity.
# Find a user's ID: Slack profile → ⋮ → Copy member ID
authorized_user_ids = []

[docker]
# Docker image to use for agent containers.
# Any Linux x86-64 image works — the dispatcher installs Claude Code at container creation.
# Defaults to debian:bookworm-slim if not set.
# image = "your-org/your-image:tag"
"""


class ConfigError(Exception):
    pass


@dataclass
class SlackConfig:
    bot_token: str
    app_token: str


@dataclass
class AuthConfig:
    authorized_user_ids: list[str]


@dataclass
class DockerConfig:
    image: str = DEFAULT_DOCKER_IMAGE


@dataclass
class UCSConfig:
    slack: SlackConfig
    auth: AuthConfig
    docker: DockerConfig = field(default_factory=DockerConfig)


def load_config() -> UCSConfig:
    """
    Load and validate config. Raises ConfigError with a descriptive message on failure.
    If config file is missing, writes a template first.
    """
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(TEMPLATE)
        raise ConfigError(
            f"No config found. A template has been written to {CONFIG_PATH}\n"
            "Fill in your Slack tokens and authorized user IDs, then run again."
        )

    try:
        with open(CONFIG_PATH, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Config file is not valid TOML: {e}")

    errors = []

    slack_raw = raw.get("slack", {})
    bot_token = slack_raw.get("bot_token", "")
    app_token = slack_raw.get("app_token", "")

    if not bot_token:
        errors.append("slack.bot_token is missing")
    elif not bot_token.startswith("xoxb-"):
        errors.append("slack.bot_token should start with 'xoxb-'")

    if not app_token:
        errors.append("slack.app_token is missing")
    elif not app_token.startswith("xapp-"):
        errors.append("slack.app_token should start with 'xapp-'")

    auth_raw = raw.get("auth", {})
    authorized_user_ids = auth_raw.get("authorized_user_ids", [])

    if not isinstance(authorized_user_ids, list):
        errors.append("auth.authorized_user_ids must be a list")
    elif len(authorized_user_ids) == 0:
        errors.append("auth.authorized_user_ids is empty — no one can trigger the agent")

    if errors:
        bullet_list = "\n".join(f"  • {e}" for e in errors)
        raise ConfigError(f"Config validation failed ({CONFIG_PATH}):\n{bullet_list}")

    docker_raw = raw.get("docker", {})
    docker_image = docker_raw.get("image", DEFAULT_DOCKER_IMAGE)

    return UCSConfig(
        slack=SlackConfig(bot_token=bot_token, app_token=app_token),
        auth=AuthConfig(authorized_user_ids=authorized_user_ids),
        docker=DockerConfig(image=docker_image),
    )
