import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, NamedTuple

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from taskmanager.core.enums import EmbeddingProviderType

DEFAULT_KEY_ENV: Final = "TASKMANAGER_OPENAI_API_KEY"

KEYS: Final = (
    "embeddings.provider",
    "embeddings.model",
    "embeddings.base_url",
    "embeddings.api_key_env",
    "embeddings.dimensions",
    "worktree_dir",
    "lease_ttl",
)

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


class ProjectConfig(BaseModel):
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    worktree_dir: str = Field(default=".worktrees", min_length=1)
    lease_ttl: int = Field(default=300, gt=0)


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
        if isinstance(value, dict):
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
    try:
        model = ProjectConfig.model_validate(_nest({key: raw}))
    except ValidationError as exc:
        msg = str(exc.errors()[0]["msg"]).removeprefix("Value error, ")
        raise ConfigError(f"{key}: {msg} (valid keys: {', '.join(KEYS)})") from exc
    return _lookup(model.model_dump(mode="json"), key)


def _default(key: str) -> Any:
    return _lookup(ProjectConfig().model_dump(mode="json"), key)


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
        self._write({**self.read(), key: _typed(key, raw)})

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

    def embeddings(self) -> EmbeddingsConfig:
        values = {key: r.value for key, r in self.effective().items()}
        return ProjectConfig.model_validate(_nest(values)).embeddings

    def document(self) -> dict[str, Any] | None:
        """The stored keys as a nested mapping, or None when nothing was ever set."""
        return _nest(self.read()) if self.path.exists() else None

    def replace(self, document: dict[str, Any]) -> None:
        self._write({key: _typed(key, value) for key, value in _flatten(document)})
