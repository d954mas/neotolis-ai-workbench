"""Controller configuration: defaults + NAIW_DATA env override + config.yaml loader."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

SCHEMA_VERSION: int = 1
# Default proxy URL. Internal DNS name from `naiw-internal`; operator override
# via NAIW_DOCKER_PROXY_URL env or `docker_proxy_url` in ~/naiw-data/config.yaml.
DEFAULT_DOCKER_PROXY_URL: str = "tcp://naiw-docker-proxy:2375"
# Mutable :latest tag. Operator may pin to @sha256:<digest> via config.yaml.
DEFAULT_TASK_IMAGE: str = "ghcr.io/d954mas/naiw-task-image:latest"

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


def _resolve_docker_proxy_url(raw: dict) -> str:
    # Precedence: NAIW_DOCKER_PROXY_URL env > config.yaml docker_proxy_url > default.
    # Env-first lets the wrapper script (and ad-hoc debug shells) override without
    # editing config.yaml; matches the NAIW_DATA contract.
    env = os.environ.get("NAIW_DOCKER_PROXY_URL")
    if env:
        return env
    return raw.get("docker_proxy_url", DEFAULT_DOCKER_PROXY_URL)


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
        return Config(
            data_root=data_root,
            docker_proxy_url=_resolve_docker_proxy_url({}),
        )

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

    # Type-check string fields so bad YAML surfaces here, not later at
    # docker.DockerClient or client.containers.run. The yaml value is checked
    # even when env-override wins, so a broken config.yaml is caught up front.
    if "docker_proxy_url" in raw and (
        not isinstance(raw["docker_proxy_url"], str) or not raw["docker_proxy_url"]
    ):
        raise ValueError(
            f"naiw-tasks: config.yaml docker_proxy_url must be a non-empty "
            f"string, got {type(raw['docker_proxy_url']).__name__}: "
            f"{raw['docker_proxy_url']!r}"
        )
    docker_proxy_url = _resolve_docker_proxy_url(raw)
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
