from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class QualifiedPath:
    node_id: str
    section_key: str | None = None

    @classmethod
    def parse(cls, raw: str) -> QualifiedPath:
        cleaned = raw.strip()
        if ":" in cleaned:
            parts = cleaned.split(":", 1)
            section = parts[1].strip() or None
            return cls(node_id=parts[0].strip(), section_key=section)
        return cls(node_id=cleaned, section_key=None)


class LevelNamingRule(BaseModel):
    prefix: str = "T"
    require_slug: bool = False
    pad: int = 1


class NamingConfig(BaseModel):
    separator: str = "-"
    spec: LevelNamingRule = Field(default_factory=lambda: LevelNamingRule(prefix="S"))
    plan: LevelNamingRule = Field(default_factory=lambda: LevelNamingRule(prefix="P"))
    task: LevelNamingRule = Field(default_factory=lambda: LevelNamingRule(prefix="T"))

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)


class SlugGenerator:
    def __init__(self, config: NamingConfig | None = None) -> None:
        self.config = config or NamingConfig()

    def generate_spec_id(self, slug: str | None = None, counter: int = 1) -> str:
        clean_slug = slug.strip() if slug is not None else None
        if clean_slug:
            return clean_slug
        if self.config.spec.require_slug:
            raise ValueError("Slug is required for spec")
        idx_str = str(counter).zfill(self.config.spec.pad)
        return f"{self.config.spec.prefix}{idx_str}"

    def generate_plan_id(
        self, parent_spec_id: str, slug: str | None = None, counter: int = 1
    ) -> str:
        sep = self.config.separator
        clean_slug = slug.strip() if slug is not None else None
        if clean_slug:
            return f"{parent_spec_id}{sep}{clean_slug}"
        if self.config.plan.require_slug:
            raise ValueError("Slug is required for plan")
        idx_str = str(counter).zfill(self.config.plan.pad)
        return f"{parent_spec_id}{sep}{self.config.plan.prefix}{idx_str}"

    def generate_task_id(
        self, parent_plan_id: str, slug: str | None = None, counter: int = 1
    ) -> str:
        sep = self.config.separator
        clean_slug = slug.strip() if slug is not None else None
        if clean_slug:
            return f"{parent_plan_id}{sep}{clean_slug}"
        if self.config.task.require_slug:
            raise ValueError("Slug is required for task")
        idx_str = str(counter).zfill(self.config.task.pad)
        return f"{parent_plan_id}{sep}{self.config.task.prefix}{idx_str}"
