from pathlib import Path

import pytest
from lifecycle_estate import add, commit, make_estate, on_branch, stored

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import Action, Outcome, Status
from taskmanager.engine import selection
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import (
    MIGRATIONS,
    SENSITIVE_AREAS,
    UNLOCKED_FILES,
    ConfigStore,
    Gate,
    ProjectConfig,
    RepoConfig,
)
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.operations import OperationError, child_defaults
from taskmanager.engine.simulate import simulate
from taskmanager.engine.snapshot import SnapshotBuilder, cycle_in, writes_migration
from taskmanager.engine.stepgraph import migration_holders
from taskmanager.renderers.importers import BulkImporter

PRISMA = "prisma/migrations/20240101000000_init/migration.sql"
RAILS = "db/migrate/20240101000000_create_users.rb"


class FakeLanding:
    def start_land(self, node_id: str) -> str:
        return f"job-{node_id}"

    def start_sync(self, node_id: str, pairs: list[tuple[str, str, str]]) -> str:
        raise AssertionError("no sync is expected here")


def estate(tmp_path: Path, repo: RepoConfig | None = None, **project: object) -> Claims:
    gate = {"main": Gate(command="true")}
    api = (repo or RepoConfig()).model_copy(update={"gates": gate})
    return make_estate(
        tmp_path, config=ProjectConfig.model_validate({"repos": {"api": api}, **project})
    )


def locks(claims: Claims, node_id: str) -> list[str]:
    return SnapshotBuilder.lock_set(node_id, claims.snapshots.build())


def test_lock_set_of_a_package_json_task_leaves_package_json_unlocked(tmp_path: Path) -> None:
    claims = estate(tmp_path)
    add(claims, "T1", files=["package.json", "src/app.ts"])

    assert locks(claims, "T1") == ["api:src/app.ts"]


def test_lock_set_with_configured_unlocked_files_locks_package_json(tmp_path: Path) -> None:
    claims = estate(tmp_path, RepoConfig(unlocked_files=["deps.lock"]))
    add(claims, "T1", files=["package.json", "deps.lock"])

    assert locks(claims, "T1") == ["api:package.json"]


@pytest.mark.parametrize(
    ("repo", "batch"),
    [(None, ["T1", "T2"]), (RepoConfig(unlocked_files=[]), ["T1"])],
    ids=["default", "nothing-unlocked"],
)
def test_next_tasks_batch_shares_an_unlocked_package_json(
    tmp_path: Path, repo: RepoConfig | None, batch: list[str]
) -> None:
    claims = estate(tmp_path, repo)
    add(claims, "T1", files=["package.json", "a.ts"], priority=2)
    add(claims, "T2", files=["package.json", "b.ts"], priority=1)
    engine = RecommendationEngine(claims.nodes, claims.runtime, claims.snapshots)

    assert [t.task_id for t in engine.get_next_tasks()] == batch


@pytest.mark.parametrize("path", [PRISMA, RAILS], ids=["prisma", "rails"])
def test_migration_path_marks_the_task_sensitive_and_joins_the_chain(
    tmp_path: Path, path: str
) -> None:
    claims = estate(tmp_path)
    add(claims, "T1", files=[path])
    add(claims, "T2", files=[path.replace("2024", "2025")])
    add(claims, "PLAIN", files=["src/app.ts"])
    snap = claims.snapshots.build()

    assert cycle_in(snap, stored(claims, "T1")).sensitive
    assert claims.snapshots.cycle(stored(claims, "T1")).sensitive
    assert not cycle_in(snap, stored(claims, "PLAIN")).sensitive
    assert migration_holders(snap, "api") == {"T2": "T1"}


def test_configured_migrations_replace_the_default_globs(tmp_path: Path) -> None:
    claims = estate(tmp_path, RepoConfig(migrations=["schema/*.sql"]))
    add(claims, "RAILS", files=[RAILS])
    add(claims, "SQL", files=["schema/0002.sql"])
    snap = claims.snapshots.build()

    assert not snap.nodes["RAILS"].writes_migration
    assert snap.nodes["SQL"].writes_migration
    assert claims.snapshots.cycle(stored(claims, "SQL")).sensitive
    assert not claims.snapshots.cycle(stored(claims, "RAILS")).sensitive


@pytest.mark.parametrize(("path", "own"), [(RAILS, True), ("src/app.ts", False)])
def test_child_defaults_under_a_reviewed_plan_reviews_a_migration_writer(
    path: str, own: bool
) -> None:
    plan = Node(id="P", kind=NodeKind.PLAN, title="P", review=True, fix=True)
    task = Node(
        id="T", kind=NodeKind.TASK, title="T", target_repo="api",
        frontmatter={"declared_files": [path]},
    )  # fmt: skip

    assert child_defaults(task, plan, config=ProjectConfig()).review is own


def test_sensitive_with_a_configured_extra_area_is_accepted(tmp_path: Path) -> None:
    claims = estate(tmp_path, sensitive_areas=[*SENSITIVE_AREAS, "payments"])
    add(claims, "T1")

    claims.ops.update_node("T1", frontmatter_set={"sensitive": ["payments", "rls"]})

    assert stored(claims, "T1").frontmatter["sensitive"] == ["payments", "rls"]


def test_sensitive_with_a_typo_area_is_refused_naming_the_configured_areas(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path, sensitive_areas=[*SENSITIVE_AREAS, "payments"])
    add(claims, "T1")

    with pytest.raises(OperationError, match="'paymnets'; it takes .*payments"):
        claims.ops.update_node("T1", frontmatter_set={"sensitive": "paymnets"})


def test_sensitive_with_an_extra_area_is_refused_by_default(tmp_path: Path) -> None:
    claims = estate(tmp_path)
    add(claims, "T1")

    with pytest.raises(OperationError, match="'payments'"):
        claims.ops.update_node("T1", frontmatter_set={"sensitive": "payments"})


def test_config_set_stores_each_key_as_a_list(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.set("repos.api.migrations", "[schema/*.sql]")
    store.set("repos.api.unlocked_files", "[deps.lock]")
    store.set("sensitive_areas", "[tenant, payments]")

    rules = store.rules()
    assert rules.repo("api").migrations == ["schema/*.sql"]
    assert rules.repo("api").unlocked_files == ["deps.lock"]
    assert rules.sensitive_areas == ["tenant", "payments"]
    assert rules.repo("web").migrations == list(MIGRATIONS)


def test_defaults_cover_each_ecosystem() -> None:
    config = ProjectConfig()
    repo = config.repo("any")

    assert {
        "pyproject.toml", "uv.lock", "package.json", "package-lock.json", "pnpm-lock.yaml",
        "yarn.lock", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile.lock",
        "build.gradle",
    } <= set(repo.unlocked_files)  # fmt: skip
    assert repo.unlocked_files == list(UNLOCKED_FILES)
    assert repo.migrations == list(MIGRATIONS)
    assert config.sensitive_areas == ["tenant", "rls", "crypto", "migration"]
    layouts = {
        "alembic": "api/alembic/versions/0001_init.py",
        "alembic-in-migrations": "migrations/versions/0001_init.py",
        "django": "shop/orders/migrations/0001_initial.py",
        "rails": RAILS,
        "flyway": "src/main/resources/db/migration/V1__init.sql",
        "liquibase": "src/main/resources/db/changelog/db.changelog-master.yaml",
        "prisma": PRISMA,
        "ef-core": "src/Shop/Migrations/20240101_Init.cs",
    }
    assert [k for k, p in layouts.items() if not writes_migration([p], repo.migrations)] == []
    assert not writes_migration(["src/migration_helpers.py", "app.ts"], repo.migrations)


@pytest.mark.parametrize(
    ("path", "action"),
    [(RAILS, Action.REVIEW), ("src/app.rb", Action.MERGE)],
    ids=["migration", "plain"],
)
def test_complete_of_a_fix_that_adds_a_migration_owes_a_re_review(
    tmp_path: Path, path: str, action: Action
) -> None:
    claims = estate(tmp_path)
    claims.landing = FakeLanding()
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.REJECT, review_cycles=1)
    on_branch(claims.root / "api", "tm/T1", "a.rb", "a = 1\n")
    worktree = claims.start("T1", "fixer", "s1").worktree
    assert worktree is not None
    commit(Path(worktree), path, "x = 1\n", "add a file")

    assert claims.complete("T1", agent="fixer") == Status.FIXED
    assert (path in stored(claims, "T1").frontmatter.get("declared_files", [])) is (
        action == Action.REVIEW
    )
    assert claims.start("T1", "next", "s2").action == action


def test_complete_on_a_spec_target_not_on_origin_yet_reads_the_default_branch(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path)
    claims.nodes.save_node(
        Node(id="S", kind=NodeKind.SPEC, title="S", frontmatter={"land_on": "release"})
    )
    add(claims, "T1", parent="S", status=Status.REVIEWED, outcome=Outcome.REJECT, review_cycles=1)
    on_branch(claims.root / "api", "tm/T1", "a.rb", "a = 1\n")
    worktree = claims.start("T1", "fixer", "s1").worktree
    assert worktree is not None
    commit(Path(worktree), RAILS, "x = 1\n", "add a migration")

    claims.complete("T1", agent="fixer")

    assert stored(claims, "T1").frontmatter["declared_files"] == [RAILS]


@pytest.mark.parametrize(
    ("repo", "chosen"),
    [(None, ["T1", "T2"]), (RepoConfig(unlocked_files=[]), ["T1"])],
    ids=["default", "nothing-unlocked"],
)
def test_select_wave_shares_an_unlocked_package_json(
    tmp_path: Path, repo: RepoConfig | None, chosen: list[str]
) -> None:
    claims = estate(tmp_path, repo)
    add(claims, "T1", files=["package.json", "a.ts"], priority=2)
    add(claims, "T2", files=["package.json", "b.ts"], priority=1)
    snap = claims.snapshots.build()
    found, _ = selection.candidates(snap, None)

    assert [e["id"] for e in selection.select(found, snap, 10, 10).chosen] == chosen


def test_simulate_later_wave_reads_the_configured_unlocked_files(tmp_path: Path) -> None:
    claims = estate(tmp_path, RepoConfig(unlocked_files=[]))
    for n, task in enumerate(("T1", "T2", "T3")):
        add(claims, task, files=["package.json", f"{task}.ts"], priority=3 - n)

    waves = simulate(claims.snapshots.build(), 2, 3, 10, None)

    assert [[e.id for e in w.entries if not e.in_flight] for w in waves] == [["T1"], ["T1", "T2"]]
    assert waves[1].held == ["T3: declared_files overlap a node chosen this wave"]


def configured(tmp_path: Path) -> Claims:
    claims = estate(tmp_path, RepoConfig(migrations=["schema/*.sql"]))
    (claims.root / ".taskmanager" / "config.yaml").write_text(
        "repos:\n  api:\n    migrations: ['schema/*.sql']\n"
    )
    return claims


def test_add_task_under_a_reviewed_plan_reviews_a_configured_migration_writer(
    tmp_path: Path,
) -> None:
    ops = configured(tmp_path).ops
    spec = ops.add_spec("S", slug="S")
    plan = ops.add_plan("P", spec, slug="P", review=True, fix=True)

    sql = ops.add_task("T", plan, frontmatter={"declared_files": ["schema/1.sql"]}, repo="api")
    rails = ops.add_task("R", plan, frontmatter={"declared_files": [RAILS]}, repo="api")

    assert [ops.node_repo.get_node(t).review for t in (sql, rails)] == [True, False]


def test_import_under_a_reviewed_plan_reviews_a_configured_migration_writer(
    tmp_path: Path,
) -> None:
    claims = configured(tmp_path)
    tasks = [
        {
            "id": "T",
            "title": "T",
            "target_repo": "api",
            "frontmatter": {"declared_files": ["schema/1.sql"]},
        },
        {"id": "R", "title": "R", "target_repo": "api", "frontmatter": {"declared_files": [RAILS]}},
    ]
    document = {
        "spec": {"id": "S", "title": "S"},
        "plans": [{"id": "P", "title": "P", "review": True, "fix": True, "tasks": tasks}],
    }

    BulkImporter(claims.nodes, claims.ops).import_dict(document)

    assert [stored(claims, t).review for t in ("T", "R")] == [True, False]
