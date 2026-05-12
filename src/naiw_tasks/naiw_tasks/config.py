"""Controller configuration: defaults + NAIW_DATA env override + config.yaml loader."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

SCHEMA_VERSION: int = 1
DEFAULT_DOCKER_PROXY_URL: str = "tcp://127.0.0.1:2375"
DEFAULT_TASK_IMAGE: str = "naiw-task-image:latest"

# Whitelist drives the typo-rejection error message — keep keys in sync with Config fields.
ALLOWED_CONFIG_KEYS: frozenset[str] = frozenset(
    {"schema_version", "docker_proxy_url", "task_image"}
)


@dataclass(frozen=True)
class Config:
    data_root: Path
    docker_proxy_url: str = DEFAULT_DOCKER_PROXY_URL
    task_image: str = DEFAULT_TASK_IMAGE


def _resolve_data_root() -> Path:
    # NAIW_DATA env override mirrors the bootstrap-script contract — single env knob
    # configures both bash scripts and the controller. Expand `~` so
    # `export NAIW_DATA=~/somewhere` works the same as it would in shell —
    # `Path("~/...")` does NOT auto-expand the tilde the way `Path.home()` does.
    env = os.environ.get("NAIW_DATA")
    if env:
        return Path(env).expanduser()
    return Path.home() / "naiw-data"


def load() -> Config:
    """Load controller config. Returns defaults when config.yaml is absent.

    Raises:
        ValueError: yaml unparseable, schema invalid, unsupported version, or
            unknown keys. yaml.YAMLError is normalised into ValueError so
            callers (cli.py) do not need to depend on yaml internals.
    """
    data_root = _resolve_data_root()
    cfg_path = data_root / "config.yaml"
    if not cfg_path.exists():
        return Config(data_root=data_root)

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(
            f"naiw-tasks: config.yaml cannot be parsed at {cfg_path} ({exc})"
        ) from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"naiw-tasks: config.yaml must be a mapping, got {type(raw).__name__}"
        )

    unknown = set(raw.keys()) - ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"naiw-tasks: config.yaml has unknown key {sorted(unknown)!r} "
            f"(schema_version={SCHEMA_VERSION} supports: "
            f"{sorted(ALLOWED_CONFIG_KEYS)}); reject typos early"
        )

    sv = raw.get("schema_version", SCHEMA_VERSION)
    if sv != SCHEMA_VERSION:
        raise ValueError(
            f"naiw-tasks: unsupported config schema_version={sv} "
            f"(controller supports schema_version={SCHEMA_VERSION})"
        )

    # Type-check string fields here so a bad value (`docker_proxy_url: null`,
    # `task_image: 123`) surfaces as a schema error from this loader, not as
    # a raw TypeError later when the value reaches docker.DockerClient or
    # client.containers.run. Default values are not subject to the check —
    # an absent key falls back to the typed DEFAULT_* constants.
    docker_proxy_url = raw.get("docker_proxy_url", DEFAULT_DOCKER_PROXY_URL)
    if not isinstance(docker_proxy_url, str) or not docker_proxy_url:
        raise ValueError(
            f"naiw-tasks: config.yaml docker_proxy_url must be a non-empty "
            f"string, got {type(docker_proxy_url).__name__}: "
            f"{docker_proxy_url!r}"
        )
    task_image = raw.get("task_image", DEFAULT_TASK_IMAGE)
    if not isinstance(task_image, str) or not task_image:
        raise ValueError(
            f"naiw-tasks: config.yaml task_image must be a non-empty string, "
            f"got {type(task_image).__name__}: {task_image!r}"
        )

    return Config(
        data_root=data_root,
        docker_proxy_url=docker_proxy_url,
        task_image=task_image,
    )
