import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from taskmanager.core.enums import EmbeddingProviderType
from taskmanager.core.status import Action

DEFAULT_KEY_ENV: Final = "TASKMANAGER_OPENAI_API_KEY"

KEYS: Final = (
    "embeddings.provider",
    "embeddings.model",
    "embeddings.base_url",
    "embeddings.api_key_env",
    "embeddings.dimensions",
    "worktree_dir",
    "lease_ttl",
    "max_fix_rounds.task",
    "max_fix_rounds.container",
    "max_merge_attempts",
    "max_step_failures",
    "condition_ttl",
    "condition_timeout",
    "red_target_decision_after",
    "repo_order",
    "repos",
    "dispatch.tick_min",
    "dispatch.tick_max",
    "dispatch.wave_size",
    "dispatch.tick_budget",
)

# The dispatch loop's typical target, printed by `tm guide dispatch`: a wakeup every tick_min-
# tick_max seconds, waves of at most wave_size nodes, at most tick_budget nodes per tick.
DISPATCH_DEFAULTS: Final = {
    "tick_min": 300,
    "tick_max": 900,
    "wave_size": 10,
    "tick_budget": 40,
}

# (low key, high key): the low value must never exceed the high one.
_DISPATCH_BOUNDS: Final = (
    ("dispatch.tick_min", "dispatch.tick_max"),
    ("dispatch.wave_size", "dispatch.tick_budget"),
)

# Keys whose value is a whole mapping or list: stored and set as one value, never split into
# dotted keys, and parsed from YAML when set from the command line.
_WHOLE: Final = frozenset({"lease_ttl", "repos", "repo_order"})

LEASE_TTL_DEFAULTS: Final = {
    "implement": 10800,
    "review": 3600,
    "fix": 7200,
    "merge": 3600,
    "sync": 3600,
}

ENV_VARS: Final = {
    "embeddings.model": "TASKMANAGER_OPENAI_MODEL",
    "embeddings.base_url": "TASKMANAGER_OPENAI_BASE_URL",
    "worktree_dir": "TM_WORKTREES",
    "lease_ttl": "TM_LEASE_TTL",
}


class ConfigError(ValueError):
    pass


class EmbeddingsConfig(BaseModel):
    provider: EmbeddingProviderType = EmbeddingProviderType.NONE
    model: str | None = Field(default=None, min_length=1)
    base_url: str | None = Field(default=None, min_length=1)
    api_key_env: str = DEFAULT_KEY_ENV
    dimensions: int = Field(default=384, gt=0)

    @field_validator("api_key_env")
    @classmethod
    def _is_a_variable_name(cls, value: str) -> str:
        # The file is committed and exported, so it must never be able to hold the key itself.
        if not value.isidentifier() or value != value.upper():
            raise ValueError("must be the NAME of an environment variable, never the key itself")
        return value


class FixRounds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: int = Field(default=2, ge=0)
    container: int = Field(default=3, ge=0)


class Gate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    junit: str | None = None
    timeout: int = Field(default=3600, gt=0)


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gates: dict[Literal["main", "parent"], Gate] = Field(default_factory=dict)


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tick_min: int = Field(default=DISPATCH_DEFAULTS["tick_min"], gt=0)
    tick_max: int = Field(default=DISPATCH_DEFAULTS["tick_max"], gt=0)
    wave_size: int = Field(default=DISPATCH_DEFAULTS["wave_size"], gt=0)
    tick_budget: int = Field(default=DISPATCH_DEFAULTS["tick_budget"], gt=0)


class ProjectConfig(BaseModel):
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    worktree_dir: str = Field(default=".worktrees", min_length=1)
    lease_ttl: dict[str, int] | int = Field(default_factory=lambda: dict(LEASE_TTL_DEFAULTS))
    max_fix_rounds: FixRounds = Field(default_factory=FixRounds)
    max_merge_attempts: int = Field(default=3, ge=1)
    max_step_failures: int = Field(default=3, ge=1)
    condition_ttl: int = Field(default=300, ge=0)
    condition_timeout: int = Field(default=60, gt=0)
    red_target_decision_after: int = Field(default=3600, gt=0)
    repo_order: list[str] = Field(default_factory=list)
    repos: dict[str, RepoConfig] = Field(default_factory=dict)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)

    @field_validator("lease_ttl")
    @classmethod
    def _positive_per_known_action(cls, value: dict[str, int] | int) -> dict[str, int] | int:
        seconds = [value] if isinstance(value, int) else list(value.values())
        if any(s <= 0 for s in seconds):
            raise ValueError("every TTL must be greater than 0")
        unknown = sorted(set(value) - set(LEASE_TTL_DEFAULTS)) if isinstance(value, dict) else []
        if unknown:
            raise ValueError(
                f"unknown action {', '.join(unknown)} (actions: {', '.join(LEASE_TTL_DEFAULTS)})"
            )
        return value

    def lease_ttl_for(self, action: Action) -> int:
        if isinstance(self.lease_ttl, int):
            # A single number is the implementer's lease; every other action keeps its default.
            return self.lease_ttl if action == Action.IMPLEMENT else LEASE_TTL_DEFAULTS[action]
        return self.lease_ttl.get(action, LEASE_TTL_DEFAULTS[action])


class Resolved(NamedTuple):
    value: Any
    source: str


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in flat.items():
        *parents, leaf = key.split(".")
        node = out
        for parent in parents:
            node = node.setdefault(parent, {})
        node[leaf] = value
    return out


def _flatten(nested: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for key, value in nested.items():
        if isinstance(value, dict) and f"{prefix}{key}" not in _WHOLE:
            yield from _flatten(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}", value


def _lookup(dumped: dict[str, Any], key: str) -> Any:
    node: Any = dumped
    for part in key.split("."):
        node = node[part]
    return node


def _require_key(key: str) -> None:
    if key not in KEYS:
        raise ConfigError(f"unknown key '{key}' (valid keys: {', '.join(KEYS)})")


def _typed(key: str, raw: Any) -> Any:
    """`raw` validated as `key`'s type and dumped JSON-ready; every message names the valid keys."""
    _require_key(key)
    if key in _WHOLE and isinstance(raw, str):
        try:
            raw = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{key}: not valid YAML (valid keys: {', '.join(KEYS)})") from exc
    try:
        model = ProjectConfig.model_validate(_nest({key: raw}))
    except ValidationError as exc:
        msg = str(exc.errors()[0]["msg"]).removeprefix("Value error, ")
        raise ConfigError(f"{key}: {msg} (valid keys: {', '.join(KEYS)})") from exc
    return _lookup(model.model_dump(mode="json"), key)


def _default(key: str) -> Any:
    return _lookup(ProjectConfig().model_dump(mode="json"), key)


def _check_bounds(key: str, flat: dict[str, Any]) -> None:
    """`flat` is the file about to be written; a bound only applies once both sides are known."""
    for low_key, high_key in _DISPATCH_BOUNDS:
        if key not in (low_key, high_key):
            continue
        low, high = flat.get(low_key, _default(low_key)), flat.get(high_key, _default(high_key))
        if low > high:
            low_name, high_name = low_key.rsplit(".", 1)[-1], high_key.rsplit(".", 1)[-1]
            raise ConfigError(f"{key}: {low_name} must not exceed {high_name} ({low} > {high})")


class ConfigStore:
    """`<root>/.taskmanager/config.yaml`, holding only the keys that were set."""

    def __init__(self, root: Path) -> None:
        self.path = root / ".taskmanager" / "config.yaml"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            loaded = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{self.path} is not valid YAML") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"{self.path} is not a mapping")
        return {key: _typed(key, value) for key, value in _flatten(loaded)}

    def _write(self, flat: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(_nest(flat), sort_keys=True), encoding="utf-8")

    def set(self, key: str, raw: str) -> None:
        flat = {**self.read(), key: _typed(key, raw)}
        _check_bounds(key, flat)
        self._write(flat)

    def unset(self, key: str) -> None:
        _require_key(key)
        flat = self.read()
        flat.pop(key, None)
        if flat:
            self._write(flat)
        else:
            self.path.unlink(missing_ok=True)

    def resolve(self, key: str, flag: Any = None) -> Resolved:
        """Flag, then the key's environment variable, then the file, then the default."""
        _require_key(key)
        if flag is not None:
            return Resolved(_typed(key, flag), "flag")
        env = ENV_VARS.get(key)
        if env and os.environ.get(env):
            try:
                return Resolved(_typed(key, os.environ[env]), "env")
            except ConfigError as exc:
                raise ConfigError(f"{env} (environment): {exc}") from exc
        file_values = self.read()
        if key in file_values:
            return Resolved(file_values[key], "config")
        return Resolved(_default(key), "default")

    def effective(self) -> dict[str, Resolved]:
        return {key: self.resolve(key) for key in KEYS}

    def project(self) -> ProjectConfig:
        values = {key: r.value for key, r in self.effective().items()}
        return ProjectConfig.model_validate(_nest(values))

    def embeddings(self) -> EmbeddingsConfig:
        return self.project().embeddings

    def lease_ttl(self, action: Action, flag: int | None = None) -> int:
        """`flag` is this claim's own TTL and wins outright; otherwise the action's stored TTL."""
        if flag is not None:
            return int(_typed("lease_ttl", flag))
        return ProjectConfig(lease_ttl=self.resolve("lease_ttl").value).lease_ttl_for(action)

    def document(self) -> dict[str, Any] | None:
        """The stored keys as a nested mapping, or None when nothing was ever set."""
        return _nest(self.read()) if self.path.exists() else None

    def replace(self, document: dict[str, Any]) -> None:
        self._write({key: _typed(key, value) for key, value in _flatten(document)})
