from pathlib import Path

from dishka import Container, Provider, Scope, make_container, provide

from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.conditions import ConditionRunner
from taskmanager.engine.config import ConfigStore
from taskmanager.engine.git import GitManager
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.operations import Operations
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.search import EmbeddingProvider, SearchEngine, build_provider
from taskmanager.engine.snapshot import SnapshotBuilder
from taskmanager.engine.verification import VerificationEngine
from taskmanager.renderers.importers import BulkImporter
from taskmanager.renderers.markdown import MarkdownRenderer


class TaskManagerProvider(Provider):
    def __init__(self, root: Path, embedding_provider: EmbeddingProvider | None = None) -> None:
        super().__init__()
        self.root = Path(root).resolve()
        self.embedding_provider = embedding_provider
        self.provide(lambda: self, scope=Scope.APP, provides=TaskManagerProvider)

    @provide(scope=Scope.APP)
    def db_mgr(self) -> DatabaseManager:
        return DatabaseManager(self.root / ".taskmanager")

    @provide(scope=Scope.APP)
    def node_repo(self, db_mgr: DatabaseManager) -> NodeRepository:
        return NodeRepository(db_mgr)

    @provide(scope=Scope.APP)
    def runtime_repo(self, db_mgr: DatabaseManager) -> RuntimeRepository:
        return RuntimeRepository(db_mgr)

    @provide(scope=Scope.APP)
    def ledger_repo(self, db_mgr: DatabaseManager) -> LedgerRepository:
        return LedgerRepository(db_mgr)

    @provide(scope=Scope.APP)
    def job_repo(self, db_mgr: DatabaseManager) -> JobRepository:
        return JobRepository(db_mgr)

    @provide(scope=Scope.APP)
    def cache_repo(self, db_mgr: DatabaseManager) -> CacheRepository:
        return CacheRepository(db_mgr)

    @provide(scope=Scope.APP)
    def snapshots(
        self, node_repo: NodeRepository, runtime_repo: RuntimeRepository, job_repo: JobRepository
    ) -> SnapshotBuilder:
        return SnapshotBuilder(node_repo, runtime_repo, job_repo)

    @provide(scope=Scope.APP)
    def condition_runner(
        self, node_repo: NodeRepository, cache_repo: CacheRepository
    ) -> ConditionRunner:
        config = ConfigStore(self.root).project()
        return ConditionRunner(
            self.root, node_repo, cache_repo, config.condition_ttl, config.condition_timeout
        )

    @provide(scope=Scope.APP)
    def graph_engine(
        self, node_repo: NodeRepository, runtime_repo: RuntimeRepository
    ) -> GraphEngine:
        return GraphEngine(node_repo, runtime_repo)

    @provide(scope=Scope.APP)
    def git_mgr(self) -> GitManager:
        return GitManager(self.root)

    @provide(scope=Scope.APP)
    def coordinator(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
        git_mgr: GitManager,
    ) -> ExecutionCoordinator:
        return ExecutionCoordinator(node_repo, runtime_repo, graph_engine, git_mgr)

    @provide(scope=Scope.APP)
    def heuristics(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
    ) -> RecommendationEngine:
        return RecommendationEngine(node_repo, runtime_repo, graph_engine)

    @provide(scope=Scope.APP)
    def verification_engine(self) -> VerificationEngine:
        return VerificationEngine(self.root)

    @provide(scope=Scope.APP)
    def renderer(self, node_repo: NodeRepository) -> MarkdownRenderer:
        return MarkdownRenderer(node_repo)

    @provide(scope=Scope.APP)
    def importer(self, node_repo: NodeRepository) -> BulkImporter:
        return BulkImporter(node_repo)

    @provide(scope=Scope.APP)
    def search_engine(self, db_mgr: DatabaseManager) -> SearchEngine:
        settings = ConfigStore(self.root).embeddings()
        return SearchEngine(db_mgr, self.embedding_provider or build_provider(settings), settings)

    @provide(scope=Scope.APP)
    def operations(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
        coordinator: ExecutionCoordinator,
        ledger_repo: LedgerRepository,
        verification_engine: VerificationEngine,
        job_repo: JobRepository,
    ) -> Operations:
        return Operations(
            node_repo,
            runtime_repo,
            graph_engine,
            coordinator,
            ledger_repo,
            verification_engine,
            job_repo=job_repo,
        )

    get_db_mgr = db_mgr
    get_node_repo = node_repo
    get_runtime_repo = runtime_repo
    get_ledger_repo = ledger_repo
    get_job_repo = job_repo
    get_cache_repo = cache_repo
    get_snapshots = snapshots
    get_condition_runner = condition_runner
    get_graph_engine = graph_engine
    get_git_mgr = git_mgr
    get_coordinator = coordinator
    get_heuristics = heuristics
    get_verification_engine = verification_engine
    get_renderer = renderer
    get_importer = importer
    get_search_engine = search_engine
    get_operations = operations


def create_container(root: Path | None = None) -> Container:
    target_root = (root or Path.cwd()).resolve()
    return make_container(TaskManagerProvider(target_root))
