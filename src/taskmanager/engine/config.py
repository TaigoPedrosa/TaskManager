import copy
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from taskmanager.core.enums import EmbeddingProviderType
from taskmanager.core.status import Action
from taskmanager.engine.git import valid_branch

DEFAULT_KEY_ENV: Final = "TASKMANAGER_OPENAI_API_KEY"
# Where a chain lands at the top when neither its spec's `land_on` nor its repository's
# `default_branch` names a branch.
DEFAULT_BRANCH: Final = "main"

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
    "codegraph.cache_commits",
    "web.archive_after_days",
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

_GATE_NAMES: Final = ("main", "parent")
# What a dotted key may name below `repos.<repo>`; a write through one merges into the stored
# `repos` mapping. The mapping-valued ones, the repository's whole entry included, take YAML.
_REPO_LEAVES: Final = (
    "default_branch",
    *(f"gates.{gate}.{field}" for gate in _GATE_NAMES for field in ("command", "junit", "timeout")),
)
_REPO_MAPPINGS: Final = ("gates", *(f"gates.{gate}" for gate in _GATE_NAMES))
_VALID: Final = ", ".join(
    (*KEYS, "repos.<repo>[.default_branch|.gates[.<main|parent>[.command|.junit|.timeout]]]")
)

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

    default_branch: str = DEFAULT_BRANCH
    gates: dict[Literal["main", "parent"], Gate] = Field(default_factory=dict)

    @field_validator("default_branch")
    @classmethod
    def _is_a_branch_name(cls, value: str) -> str:
        if not valid_branch(value):
            raise ValueError(f"'{value}' is not a branch name git accepts")
        return value


class DispatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tick_min: int = Field(default=DISPATCH_DEFAULTS["tick_min"], gt=0)
    tick_max: int = Field(default=DISPATCH_DEFAULTS["tick_max"], gt=0)
    wave_size: int = Field(default=DISPATCH_DEFAULTS["wave_size"], gt=0)
    tick_budget: int = Field(default=DISPATCH_DEFAULTS["tick_budget"], gt=0)


class CodegraphConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each commit a codegraph_query verified keeps an exported tree and its index on disk.
    cache_commits: int = Field(default=3, ge=1)


class WebConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 0 keeps every completed spec out of the archive.
    archive_after_days: int = Field(default=3, ge=0)


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
    codegraph: CodegraphConfig = Field(default_factory=CodegraphConfig)
    web: WebConfig = Field(default_factory=WebConfig)

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

    def default_branch(self, repo: str | None) -> str:
        found = self.repos.get(repo) if repo is not None else None
        return found.default_branch if found is not None else DEFAULT_BRANCH


def moved_defaults(before: ProjectConfig, after: ProjectConfig) -> list[str]:
    """Each repository whose default branch differs from `before` to `after`, by name."""
    repos = {*before.repos, *after.repos}
    return sorted(r for r in repos if before.default_branch(r) != after.default_branch(r))


class Resolved(NamedTuple):
    value: Any
    source: str


class RepoKey(NamedTuple):
    repo: str
    path: tuple[str, ...]

    @property
    def is_mapping(self) -> bool:
        return ".".join(self.path) not in _REPO_LEAVES


def _repo_key(key: str) -> RepoKey | None:
    """`repos.<repo>[.<key below it>]` as its repository and path; None for any other key. Read
    from the right, so a repository named `.` or with a dot in its name reads as written."""
    if not key.startswith("repos."):
        return None
    rest = key.removeprefix("repos.")
    below = next((k for k in (*_REPO_LEAVES, *_REPO_MAPPINGS) if rest.endswith(f".{k}")), None)
    repo = rest.removesuffix(f".{below}") if below else rest
    if not repo or {"gates", "default_branch"} & set(repo.split(".")):
        return None
    return RepoKey(repo, tuple(below.split(".")) if below else ())


def _with(repos: dict[str, Any], key: RepoKey, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(repos)
    *parents, leaf = (key.repo, *key.path)
    node = out
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value
    return out


def _without(repos: dict[str, Any], key: RepoKey) -> dict[str, Any]:
    out = copy.deepcopy(repos)
    *parents, leaf = (key.repo, *key.path)
    node: Any = out
    for part in parents:
        node = node.get(part)
        if not isinstance(node, dict):
            return out
    node.pop(leaf, None)
    return out


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
        raise ConfigError(f"unknown key '{key}' (valid keys: {_VALID})")


def _yaml(key: str, raw: str) -> Any:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{key}: not valid YAML (valid keys: {_VALID})") from exc


def _typed(key: str, raw: Any) -> Any:
    """`raw` validated as `key`'s type and dumped JSON-ready; every message names the valid keys."""
    _require_key(key)
    if key in _WHOLE and isinstance(raw, str):
        raw = _yaml(key, raw)
    try:
        model = ProjectConfig.model_validate(_nest({key: raw}))
    except ValidationError as exc:
        error = exc.errors()[0]
        msg = str(error["msg"]).removeprefix("Value error, ")
        # Inside `repos`, the message names the key that failed as a dotted write spells it.
        where = ".".join(str(p) for p in error["loc"] if p != "[key]") if key == "repos" else key
        raise ConfigError(f"{where}: {msg} (valid keys: {_VALID})") from exc
    # Only what was set: a stored default would read as a choice someone made.
    return _lookup(model.model_dump(mode="json", exclude_unset=True), key)


def _refuse_dropping(stored: dict[str, Any], repos: dict[str, Any]) -> None:
    dropped = sorted(set(stored) - set(repos))
    if dropped:
        raise ConfigError(
            f"repos: this value would drop the settings stored for {', '.join(dropped)}: "
            "include every repository to keep, set one through `repos.<repo>.<key>`, or remove "
            "one first with `tm config unset repos.<repo>`"
        )


def _default(key: str) -> Any:
    return _lookup(ProjectConfig().model_dump(mode="json"), key)


def _check_bound_pair(low_key: str, high_key: str, flat: dict[str, Any]) -> None:
    low, high = flat.get(low_key, _default(low_key)), flat.get(high_key, _default(high_key))
    if low > high:
        low_name, high_name = low_key.rsplit(".", 1)[-1], high_key.rsplit(".", 1)[-1]
        raise ConfigError(f"{low_key}: {low_name} must not exceed {high_name} ({low} > {high})")


def _check_bounds(key: str, flat: dict[str, Any]) -> None:
    """`flat` is the file about to be written; a bound only applies once both sides are known."""
    for low_key, high_key in _DISPATCH_BOUNDS:
        if key in (low_key, high_key):
            _check_bound_pair(low_key, high_key, flat)


def _check_all_bounds(flat: dict[str, Any]) -> None:
    """Every bound, for a write that replaces the whole document in one shot."""
    for low_key, high_key in _DISPATCH_BOUNDS:
        _check_bound_pair(low_key, high_key, flat)


class ConfigStore:
    """`<root>/.taskmanager/config.yaml`, holding only the keys that were set."""

    def __init__(
        self, root: Path, guard: Callable[[ProjectConfig, ProjectConfig], None] | None = None
    ) -> None:
        self.path = root / ".taskmanager" / "config.yaml"
        # Shown the stored and the written `repos` before a write that moves a default branch;
        # raising refuses the write.
        self.guard = guard

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

    def _check_branches(self, flat: dict[str, Any]) -> None:
        if self.guard is None:
            return
        before, after = self.branches(), ProjectConfig(repos=flat.get("repos", {}))
        if moved_defaults(before, after):
            self.guard(before, after)

    def _write(self, flat: dict[str, Any]) -> None:
        self._check_branches(flat)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(_nest(flat), sort_keys=True), encoding="utf-8")

    def set(self, key: str, raw: str) -> None:
        flat = self.read()
        stored = flat.get("repos", {})
        found = _repo_key(key)
        if found is not None:
            value = _yaml(key, raw) if found.is_mapping else raw
            flat["repos"] = _typed("repos", _with(stored, found, value))
        else:
            flat[key] = _typed(key, raw)
            if key == "repos":
                _refuse_dropping(stored, flat[key])
        _check_bounds(key, flat)
        self._write(flat)

    def unset(self, key: str) -> None:
        found = _repo_key(key)
        if found is None:
            _require_key(key)
            flat = self.read()
            flat.pop(key, None)
        else:
            flat = self.read()
            if repos := _without(flat.pop("repos", {}), found):
                flat["repos"] = _typed("repos", repos)
        _check_bounds(key, flat)
        if flat:
            self._write(flat)
        else:
            self._check_branches(flat)
            self.path.unlink(missing_ok=True)

    def resolve(self, key: str, flag: Any = None) -> Resolved:
        """Flag, then the key's environment variable, then the file, then the default."""
        found = _repo_key(key)
        if found is not None:
            return self._resolve_in_repos(found)
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

    def _resolve_in_repos(self, key: RepoKey) -> Resolved:
        """A repository with no entry reads as the defaults every repository has; a key under
        it that is not set, such as a gate never configured, reads as None."""
        repos = self.resolve("repos")
        entry = repos.value.get(key.repo)
        node: Any = RepoConfig.model_validate(entry or {}).model_dump(mode="json")
        for part in key.path:
            node = node.get(part) if isinstance(node, dict) else None
        return Resolved(node, "default" if entry is None else repos.source)

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

    def branches(self) -> ProjectConfig:
        """Only `repos` resolved: a reader of default branches needs no other key's read."""
        return ProjectConfig(repos=self.resolve("repos").value)

    def document(self) -> dict[str, Any] | None:
        """The stored keys as a nested mapping, or None when nothing was ever set."""
        return _nest(self.read()) if self.path.exists() else None

    def replace(self, document: dict[str, Any]) -> None:
        flat = {key: _typed(key, value) for key, value in _flatten(document)}
        _check_all_bounds(flat)
        self._write(flat)
