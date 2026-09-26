"""Every command a built-in guide prints is a command the CLI has.

An agent runs what the guide shows verbatim, so a renamed command or flag has to fail here rather
than in the agent's terminal.
"""

import re
import shlex
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import typer.main

from taskmanager.cli.main import _guide_topics, app

REQUIRED_TOPICS = {"implement", "review", "fix", "merge", "overview"}
COMMAND_FLOOR = 40

_SPAN = re.compile(r"`([^`\n]+)`")
_STOP = re.compile(r"[|;#]|&&")


def _topics() -> list[str]:
    return sorted(_guide_topics())


def _guide_text(topic: str) -> str:
    return str(files("taskmanager").joinpath(f"guides/{topic}.md").read_text(encoding="utf-8"))


def _snippets(text: str) -> list[str]:
    """Every fenced line and every inline code span, fences and backticks dropped."""
    found: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            found.append(line.strip())
        else:
            found.extend(m.group(1) for m in _SPAN.finditer(line))
    return [s for s in found if s]


def _tm_commands(text: str) -> list[str]:
    commands: list[str] = []
    for snippet in _snippets(text):
        head = _STOP.split(snippet)[0].strip()
        if head.startswith("tm ") and head not in commands:
            commands.append(head)
    return commands


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)[1:]
    except ValueError:
        return command.split()[1:]


def _resolve(tokens: list[str]) -> tuple[str, Any, list[str]]:
    """Walk the typer app down to the command the tokens name, returning it and the rest.

    Typer builds its own click-compatible classes rather than click's own, so a group is what
    carries subcommands, not what passes an isinstance check.
    """
    cmd: Any = typer.main.get_command(app)
    named = ["tm"]
    rest = list(tokens)
    while getattr(cmd, "commands", None) and rest and not rest[0].startswith("-"):
        sub = cmd.commands.get(rest[0])
        assert sub is not None, f"`{' '.join(named)}` has no subcommand {rest[0]!r}"
        named.append(rest.pop(0))
        cmd = sub
    return " ".join(named), cmd, rest


def _accepted_flags(cmd: Any) -> set[str]:
    flags = {"--help"}
    for param in cmd.params:
        flags.update(param.opts)
        flags.update(param.secondary_opts)
    return flags


_CASES = [(topic, cmd) for topic in _topics() for cmd in _tm_commands(_guide_text(topic))]


def test_every_topic_has_a_guide_file() -> None:
    topics = _topics()
    assert REQUIRED_TOPICS <= set(topics), f"missing role guides: {REQUIRED_TOPICS - set(topics)}"
    for topic in topics:
        assert _guide_text(topic).strip(), f"guide '{topic}' is empty"


@pytest.mark.parametrize("topic", _topics())
def test_guide_opens_with_a_title_and_a_one_sentence_blurb(topic: str) -> None:
    """`tm guide` lists a topic by the paragraph under its title, so that paragraph is one sentence."""
    text = _guide_text(topic)
    first, *_ = text.splitlines()
    assert first.startswith("# "), f"guide '{topic}' does not open with a `# ` title"

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    assert len(paragraphs) >= 2, f"guide '{topic}' has no paragraph under its title"
    blurb = paragraphs[1]
    assert "\n" not in blurb, f"guide '{topic}' blurb is more than one line: {blurb!r}"
    assert blurb.endswith("."), f"guide '{topic}' blurb is not a sentence: {blurb!r}"
    assert ". " not in blurb, f"guide '{topic}' blurb is more than one sentence: {blurb!r}"


def test_the_guides_show_enough_commands_to_be_worth_checking() -> None:
    """A collection that silently shrinks to nothing would leave every case below green."""
    assert len(_CASES) >= COMMAND_FLOOR, f"only {len(_CASES)} `tm` commands found in the guides"
    for topic in _topics():
        assert _tm_commands(_guide_text(topic)), f"guide '{topic}' shows no `tm` command"


def _assert_command_exists(where: str, command: str) -> None:
    tokens = _tokens(command)
    if not tokens:
        return
    name, cmd, rest = _resolve(tokens)
    accepted = _accepted_flags(cmd)
    for token in rest:
        if not token.startswith("-"):
            continue
        flag = token.split("=", 1)[0]
        assert flag in accepted, f"`{name}` has no flag {flag} ({where} shows `{command}`)"


@pytest.mark.parametrize("topic,command", _CASES, ids=[f"{t}:{c}" for t, c in _CASES])
def test_guide_command_exists(topic: str, command: str) -> None:
    _assert_command_exists(f"{topic}.md", command)


RETIRED = (
    "tm run start",
    "tm run stop",
    "tm run heartbeat",
    "NOT_STARTED",
    "WAITING_FIXES",
    "IN_FLIGHT",
    ":hold",
    "external_blockers",
    "--release",
    "maxFixRounds",
)

CLOSING_VERBS = {
    "implement": "tm task complete <task-id> --agent <name> --token <token>",
    "fix": "tm task complete <node-id> --agent <name> --token <token>",
    "review": "tm task review <node-id> --agent <name> --token <token> --approve",
    "merge": "tm job resume <job> --agent <name> --token <token>",
}

WORKFLOW = Path(__file__).resolve().parents[2] / "workflows" / "tm-wave.js"


@pytest.mark.parametrize("topic", _topics())
def test_guide_carries_no_retired_lifecycle_vocabulary(topic: str) -> None:
    text = _guide_text(topic)
    assert [word for word in RETIRED if word in text] == []


@pytest.mark.parametrize("topic,verb", sorted(CLOSING_VERBS.items()))
def test_role_guide_shows_the_verb_that_closes_its_step(topic: str, verb: str) -> None:
    assert verb in _guide_text(topic)


def test_overview_carries_the_cycle_and_the_cutover_runbook() -> None:
    text = _guide_text("overview")
    for needle in (
        "READY ──claim──▶ IMPLEMENTING",
        "MERGING ──landed and verified──▶ COMPLETED",
        "## Moving an estate to this version",
        "`tm init --archive`",
        "`tm export <export dir>`",
    ):
        assert needle in text, needle


def test_fix_guide_requires_a_test_per_branch_that_fails_when_reverted() -> None:
    """A finding closes only with a test per branch, each proven to fail when that branch alone is undone."""
    text = _guide_text("fix")
    steps, never = text.split("## Never", 1)
    assert "each test fails when its branch alone is" in steps
    assert "Never close a finding with a branch that nothing fails on" in never
    assert "Never close a finding with nothing that fails when the fix is reverted" not in text


def test_dispatch_guide_states_the_task_fix_round_cap() -> None:
    """The model table's round-3 row is containers only because a task fails before it gets there."""
    text = _guide_text("dispatch")
    assert "max_fix_rounds.task" in text
    assert "widening `acceptable_models`" in text


def test_dispatch_guide_routes_questions_through_decisions() -> None:
    """A question in chat is lost when the session ends; every question is a decision instead."""
    text = _guide_text("dispatch")
    assert "never ask" in text.lower()
    assert "tm decision add" in text
    assert "--blocks" in text
    assert "tm decision list --status open" in text


def test_dispatch_guide_answers_a_ruling_rejection_with_a_decision() -> None:
    text = _guide_text("dispatch")
    assert "not another fix round" in text


RULING_RULE_OF_THUMB_GUIDES = ("implement", "fix", "review")


@pytest.mark.parametrize("topic", RULING_RULE_OF_THUMB_GUIDES)
def test_guide_tells_a_ruling_from_a_defect(topic: str) -> None:
    """A blocker that is a judgement call, not a defect, is a decision raised at once."""
    text = _guide_text(topic)
    assert "the brief doesn't say" in text
    assert "which of these is correct" in text


def test_review_guide_says_a_reviewer_raises_a_ruling_instead_of_rejecting() -> None:
    assert "raises it instead of rejecting" in _guide_text("review")


def test_dispatch_guide_names_every_argument_tm_wave_reads() -> None:
    read = set(re.findall(r"\bA\.([A-Za-z]+)", WORKFLOW.read_text(encoding="utf-8")))
    assert read, "no argument found in the workflow script"
    text = _guide_text("dispatch")
    assert sorted(arg for arg in read if f"`{arg}`" not in text) == []


REPO = Path(__file__).resolve().parents[2]
DOCS = (
    "README.md",
    "agents/tm-op.md",
    "commands/board.md",
    "commands/task.md",
    "commands/tm.md",
    "skills/dispatcher/SKILL.md",
    "skills/taskmanager/SKILL.md",
    "src/taskmanager/skills/dispatcher/SKILL.md",
    "src/taskmanager/skills/taskmanager/SKILL.md",
)


def _doc_text(doc: str) -> str:
    return (REPO / doc).read_text(encoding="utf-8")


_DOC_CASES = [
    (doc, cmd)
    for doc in DOCS
    for cmd in _tm_commands(_doc_text(doc))
    if not _tokens(cmd)[:1] or not _tokens(cmd)[0].startswith("$")
]


def test_the_docs_show_enough_commands_to_be_worth_checking() -> None:
    assert len(_DOC_CASES) >= 15, f"only {len(_DOC_CASES)} `tm` commands found in the shipped docs"


@pytest.mark.parametrize("doc,command", _DOC_CASES, ids=[f"{d}:{c}" for d, c in _DOC_CASES])
def test_doc_command_exists(doc: str, command: str) -> None:
    _assert_command_exists(doc, command)


@pytest.mark.parametrize("doc", DOCS)
def test_doc_carries_no_retired_lifecycle_vocabulary(doc: str) -> None:
    text = _doc_text(doc)
    assert [word for word in RETIRED if word in text] == []


_OWNED_VERBS = ("tm task complete", "tm task review", "tm task release", "tm job resume")
_OWNED_CASES = [
    (where, cmd)
    for where, text in [
        *((f"{t}.md", _guide_text(t)) for t in _topics()),
        *((d, _doc_text(d)) for d in DOCS),
    ]
    for cmd in _tm_commands(text)
    if cmd.startswith(_OWNED_VERBS) and "--agent" in cmd
]


def test_the_docs_show_enough_owned_closes_to_be_worth_checking() -> None:
    assert len(_OWNED_CASES) >= 15, f"only {len(_OWNED_CASES)} closes naming --agent found"


@pytest.mark.parametrize("where,command", _OWNED_CASES, ids=[f"{w}:{c}" for w, c in _OWNED_CASES])
def test_a_close_that_names_its_agent_names_its_claim_s_token(where: str, command: str) -> None:
    """An agent name repeats across claims of one node; only the token tells this claim apart."""
    assert "--token" in command, f"{where} shows `{command}`"


def test_plan_guide_names_the_measurement_for_an_unchanged_acceptance() -> None:
    """A file left unchanged is proven by its bytes or hash, never by re-reading a field or two."""
    assert "names its measurement" in _guide_text("plan")


def test_plan_guide_names_where_an_invariant_or_refusal_holds() -> None:
    """A rule enforced on one path of several is a rule not enforced; acceptance must name each path."""
    assert "names where it holds" in _guide_text("plan")


def test_plan_guide_writes_a_config_backed_limit_as_its_key() -> None:
    """A spec that hardcodes a bound a tm config key already covers drifts from that key silently."""
    assert "written as the project's key" in _guide_text("plan")


def test_plan_guide_asks_a_reviewed_plan_for_its_own_verification() -> None:
    """A plan without a verification of its own reached review twice with nothing beyond its children's suites."""
    assert "carries a verification of its own" in _guide_text("plan")


def test_plan_guide_lists_every_input_a_matches_acceptance_reads() -> None:
    """A dropped condition, lock or config key in a 'matches' acceptance is a mismatch a test never catches."""
    assert "lists every input the reference reads" in _guide_text("plan")


def test_plan_guide_names_the_function_that_stays_and_the_ones_it_replaces() -> None:
    """A copied-not-moved implementation leaves the replaced ones alive for a caller to find by accident."""
    assert "names the function that stays" in _guide_text("plan")


def test_review_guide_rejects_on_a_red_test_whoever_declared_its_file() -> None:
    """A red test on the branch is this diff's failure regardless of which task's declared_files named the file."""
    assert "rejects the node" in _guide_text("review")


def test_plan_guide_tells_a_deliverable_from_a_ruling_and_a_measurement() -> None:
    """A spurious task files a ruling or a measurement as if it were code that must land."""
    assert (
        "A task's deliverable is code or an artifact that must land. A question whose answer is a "
        "ruling is a `tm decision add`. A measurement is one read-only agent whose result goes "
        "into that decision's context or a section, with no implement, review or fix cycle. Before "
        "filing either, look for the answer where it may already be: an agent's report, a section, "
        "an earlier decision. When it exists, raise the decision with that data in its context."
    ) in _guide_text("plan")


def test_plan_guide_scales_review_and_fix_to_deliverable_risk() -> None:
    """A document or research deliverable reviewed like shippable code buys a fix cycle nothing needs."""
    assert (
        "Scale review and fix to what the deliverable risks: a document or research deliverable is "
        "`review: false`, or not a task at all."
    ) in _guide_text("plan")


def test_plan_guide_keeps_two_small_changes_to_one_file_as_one_task() -> None:
    """Serializing two small changes to one file across two tasks buys a review cycle each for nothing."""
    assert (
        "Two small changes to one file from one finding are one task, not two tasks serialized on "
        "that file with a review cycle each."
    ) in _guide_text("plan")


def test_plan_guide_names_the_source_instead_of_copying_a_moving_figure() -> None:
    """A figure copied from a source still under review or still being measured goes stale before it lands."""
    assert (
        "A brief never copies a figure from a source still under review or still being measured; "
        "it names the source, and the implementer reads the current value."
    ) in _guide_text("plan")


def test_plan_guide_names_what_a_removed_feature_reached() -> None:
    """A removal task with no list of what only that feature reached leaves its markup and handlers behind."""
    assert (
        "A task that removes a feature names what only that feature reached (its markup, handlers, "
        "styles, the state it reset) and deletes each one, or moves it to where it is still used; "
        "its acceptance lists them, each with a check that fails when it comes back."
    ) in _guide_text("plan")


def test_review_guide_runs_every_check_the_acceptance_lists() -> None:
    """A check skipped rather than run and reported leaves a node approved over untested acceptance."""
    assert (
        "Run every check the acceptance lists. A check you could not run is named in the findings "
        "as not run, and the node is not approved over it."
    ) in _guide_text("review")


def test_dispatch_guide_closes_a_wording_only_rejection_without_another_fix_round() -> None:
    """A rejection that only disputes the record's wording, on a ruling already made, is not a defect to fix again."""
    assert (
        "A rejection whose findings are only about the record's wording or accuracy, on work whose "
        "ruling is already made, does not buy another fix round: the findings go in the node's "
        "`report` section and the node closes as the ruling stands."
    ) in _guide_text("dispatch")
