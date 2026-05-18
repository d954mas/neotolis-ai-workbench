"""Controller configuration: defaults + NAIW_DATA env override + config.yaml loader."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SCHEMA_VERSION: int = 1
DEFAULT_DOCKER_PROXY_URL: str = "tcp://naiw-docker-proxy:2375"
# Mutable :latest tag. Operator may pin to @sha256:<digest> via config.yaml.
DEFAULT_TASK_IMAGE: str = "ghcr.io/d954mas/naiw-task-image:latest"
# Default 50 GiB in bytes; matches user-facing 50GiB string in config.yaml.
# This is the per-NAIW-data namespace cap, NOT the host disk size.
DEFAULT_MAX_DATA_SIZE_BYTES: int = 50 * 1024 * 1024 * 1024  # 53_687_091_200

# Whitelist drives the typo-rejection error message — keep keys in sync with Config fields.
ALLOWED_CONFIG_KEYS: frozenset[str] = frozenset(
    {"schema_version", "docker_proxy_url", "task_image", "max_data_size"}
)


@dataclass(frozen=True)
class Config:
    data_root: Path
    docker_proxy_url: str = DEFAULT_DOCKER_PROXY_URL
    task_image: str = DEFAULT_TASK_IMAGE
    # Host-side data root, set by compose's NAIW_DATA_HOST env to the operator's
    # actual filesystem path (e.g. /home/op/naiw-data) when the controller runs
    # inside a container. None on direct host invocation (tests, ad-hoc CLI use)
    # — host_root then falls back to data_root.
    data_root_host: Path | None = None
    # Per-namespace cap on ~/naiw-data/ size in bytes. `disk` warns at >80%
    # and `start` refuses at >95%. Defaults to 50 GiB; operator overrides via
    # config.yaml `max_data_size: 100GiB` (IEC binary units only).
    max_data_size: int = DEFAULT_MAX_DATA_SIZE_BYTES

    @property
    def host_root(self) -> Path:
        """Host-side path corresponding to data_root, for `containers.run` bind sources."""
        return self.data_root_host if self.data_root_host is not None else self.data_root


def _resolve_data_root() -> Path:
    # NAIW_DATA env override mirrors the bootstrap-script contract — single env knob
    # configures both bash scripts and the controller. Expand `~` so
    # `export NAIW_DATA=~/somewhere` works the same as it would in shell —
    # `Path("~/...")` does NOT auto-expand the tilde the way `Path.home()` does.
    env = os.environ.get("NAIW_DATA")
    if env:
        return Path(env).expanduser()
    return Path.home() / "naiw-data"


def _resolve_data_root_host() -> Path | None:
    # NAIW_DATA_HOST is injected by compose at service-level so the controller
    # can hand the daemon bind sources resolvable on the HOST. None when the
    # controller is not running under the documented compose path.
    env = os.environ.get("NAIW_DATA_HOST")
    if env:
        return Path(env).expanduser()
    return None


def _resolve_docker_proxy_url(raw: dict) -> str:
    # Operator override path is config.yaml. NAIW_DOCKER_PROXY_URL only matters
    # for `docker compose run -e ...` debug — the wrapper does not forward
    # host-side env (test_wrapper_does_not_forward_host_proxy_url_into_container
    # locks this in).
    env = os.environ.get("NAIW_DOCKER_PROXY_URL")
    if env:
        return env
    return raw.get("docker_proxy_url", DEFAULT_DOCKER_PROXY_URL)


_IEC_UNIT_BYTES: dict[str, int] = {
    "KiB": 1024,
    "MiB": 1024 * 1024,
    "GiB": 1024 * 1024 * 1024,
    "TiB": 1024 * 1024 * 1024 * 1024,
}
_IEC_PATTERN = re.compile(r"^(\d+)(KiB|MiB|GiB|TiB)$")
_DECIMAL_TYPO_PATTERN = re.compile(r"^\d+(KB|MB|GB|TB)$")


def _parse_max_data_size(raw: str) -> int:
    """Parse <int><unit> where unit is one of KiB/MiB/GiB/TiB.

    Returns the size in bytes. IEC binary units only — the SI-style suffixes
    KB/MB/GB/TB are rejected with a clear hint pointing at the binary form,
    because mixing the two in a per-namespace cap silently shifts the
    threshold by ~7% per power-of-1024 step (1 GB = 1e9, 1 GiB = 2^30) and
    that drift is exactly the operator surprise we want to avoid.
    """
    if not isinstance(raw, str):
        raise ValueError(
            f"max_data_size must be a string like '50GiB', got "
            f"{type(raw).__name__}: {raw!r}"
        )
    value = raw.strip()
    m = _IEC_PATTERN.match(value)
    if m:
        n, unit = m.group(1), m.group(2)
        result = int(n) * _IEC_UNIT_BYTES[unit]
        # 0 disables both the >80% warning AND the >95% start-refusal gate
        # — silently letting a typo nuke the cap is exactly the operator
        # surprise we want to avoid. Operator who actually wants no cap
        # should set a deliberately huge value (e.g. 1024TiB).
        if result <= 0:
            raise ValueError(
                f"max_data_size={value!r}: must be > 0; setting 0 "
                f"silently disables the disk-threshold gate"
            )
        return result
    if _DECIMAL_TYPO_PATTERN.match(value):
        raise ValueError(
            f"max_data_size={value!r}: use IEC binary units "
            f"(KiB/MiB/GiB/TiB), not decimal units (KB/MB/GB/TB)"
        )
    raise ValueError(
        f"max_data_size={value!r}: expected <integer><unit> where "
        f"unit is one of KiB/MiB/GiB/TiB (e.g. '50GiB')"
    )


def load() -> Config:
    """Load controller config. Returns defaults when config.yaml is absent.

    Raises:
        ValueError: yaml unparseable, schema invalid, unsupported version, or
            unknown keys. yaml.YAMLError is normalised into ValueError so
            callers (cli.py) do not need to depend on yaml internals.
    """
    data_root = _resolve_data_root()
    data_root_host = _resolve_data_root_host()
    cfg_path = data_root / "config.yaml"
    if not cfg_path.exists():
        return Config(
            data_root=data_root,
            docker_proxy_url=_resolve_docker_proxy_url({}),
            data_root_host=data_root_host,
            max_data_size=DEFAULT_MAX_DATA_SIZE_BYTES,
        )

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(
            f"config.yaml cannot be parsed at {cfg_path} ({exc})"
        ) from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"config.yaml must be a mapping, got {type(raw).__name__}"
        )

    unknown = set(raw.keys()) - ALLOWED_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"config.yaml has unknown key {sorted(unknown)!r} "
            f"(schema_version={SCHEMA_VERSION} supports: "
            f"{sorted(ALLOWED_CONFIG_KEYS)}); reject typos early"
        )

    sv = raw.get("schema_version", SCHEMA_VERSION)
    if sv != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported config schema_version={sv} "
            f"(controller supports schema_version={SCHEMA_VERSION})"
        )

    # Type-check string fields so bad YAML surfaces here, not later at
    # docker.DockerClient or client.containers.run. The yaml value is checked
    # even when env-override wins, so a broken config.yaml is caught up front.
    if "docker_proxy_url" in raw and (
        not isinstance(raw["docker_proxy_url"], str) or not raw["docker_proxy_url"]
    ):
        raise ValueError(
            f"config.yaml docker_proxy_url must be a non-empty "
            f"string, got {type(raw['docker_proxy_url']).__name__}: "
            f"{raw['docker_proxy_url']!r}"
        )
    docker_proxy_url = _resolve_docker_proxy_url(raw)
    task_image = raw.get("task_image", DEFAULT_TASK_IMAGE)
    if not isinstance(task_image, str) or not task_image:
        raise ValueError(
            f"config.yaml task_image must be a non-empty string, "
            f"got {type(task_image).__name__}: {task_image!r}"
        )

    max_data_size = DEFAULT_MAX_DATA_SIZE_BYTES
    if "max_data_size" in raw:
        max_data_size = _parse_max_data_size(raw["max_data_size"])

    return Config(
        data_root=data_root,
        docker_proxy_url=docker_proxy_url,
        task_image=task_image,
        data_root_host=data_root_host,
        max_data_size=max_data_size,
    )
