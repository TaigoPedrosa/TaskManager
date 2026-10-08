"""Every command a built-in guide prints is a command the CLI has.

An agent runs what the guide shows verbatim, so a renamed command or flag has to fail here rather
than in the agent's terminal.
"""

import re
import shlex
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import typer.main
from typer.testing import CliRunner

from taskmanager.cli.main import _guide_topics, app
from taskmanager.engine.snapshot import writes_migration
from taskmanager.engine.validation import SENSITIVE_AREAS

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


def test_dispatch_guide_says_the_fix_round_caps_bound_only_the_sensitive_path() -> None:
    """A rejection buys one fix, so only a sensitive node's re-review can reach a cap."""
    text = _guide_text("dispatch")
    assert (
        "`max_fix_rounds.task` and `max_fix_rounds.container` bound only the sensitive path" in text
    )
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


def test_plan_guide_names_where_an_invariant_or_refusal_holds(
    rendered: Callable[[str], str],
) -> None:
    """A rule enforced on one path of several is a rule not enforced; acceptance must name each path,
    and for an invariant tying two fields, the paths that write each field."""
    text = rendered("plan")
    assert "names where it holds" in text
    assert (
        "An invariant over two fields (a status and a flag, a default and every path that creates "
        "the node) names the writes of both fields, not only the one the task touches. A changed "
        "shape names every caller."
    ) in text
    assert "a changed shape names every caller" not in text


def test_plan_guide_writes_a_config_backed_limit_as_its_key() -> None:
    """A spec that hardcodes a bound a tm config key already covers drifts from that key silently."""
    assert "written as the project's key" in _guide_text("plan")


def test_plan_guide_asks_a_reviewed_plan_for_its_own_verification() -> None:
    """A plan without a verification of its own reached review twice with nothing beyond its children's suites."""
    assert "carries a verification of its own" in _guide_text("plan")


def test_plan_guide_lists_every_input_a_matches_acceptance_reads() -> None:
    """A dropped condition, lock or config key in a 'matches' acceptance is a mismatch a test never catches."""
    assert "lists every input the reference reads" in _guide_text("plan")


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


def test_plan_guide_names_each_thing_a_replacement_or_removal_deletes(
    rendered: Callable[[str], str],
) -> None:
    """A replaced implementation or a removed feature's markup left behind is found by a caller by accident."""
    text = rendered("plan")
    assert (
        "- A task that replaces or removes something names each thing that goes (the "
        "implementations one now replaces; a feature's markup, handlers, styles and the state it "
        "reset) and deletes each one in the same task, or moves it to where it is still used; its "
        "acceptance lists them, each with a check that fails when it comes back."
    ) in text
    assert "names the function that stays" not in text
    assert "A task that removes a feature names" not in text


def test_plan_guide_asks_a_ui_acceptance_for_each_interaction_s_behaviour(
    rendered: Callable[[str], str],
) -> None:
    """A UI brief that names only the look gets a review that compares screenshots and never clicks."""
    assert (
        "- A UI task's acceptance names each interaction's behaviour, not only its look: the "
        "feedback for every write, where focus lands after it, the keyboard route to every pointer "
        "action, what a live update does to a field mid-edit, what survives a reload, and how a "
        "reviewer reaches each state (a route, a fixture). Each line has a check that drives it."
    ) in rendered("plan")


def test_review_guide_drives_a_ui_node_s_behaviour_in_the_running_app(
    rendered: Callable[[str], str],
) -> None:
    """A screenshot beside the frame passes a page whose writes, focus and keyboard route are broken."""
    assert (
        "A UI node's behaviour lines are checked by driving them in the running app: the write and "
        "its feedback, the focus after it, the keyboard route, a live update mid-edit, a reload. A "
        "screenshot beside the frame shows the look and proves none of them."
    ) in rendered("review")


def test_review_guide_runs_every_check_the_acceptance_lists() -> None:
    """A check skipped rather than run and reported leaves a node approved over untested acceptance."""
    assert (
        "Run every check the acceptance lists. A check you could not run is named in the findings "
        "as not run, and the node is not approved over it."
    ) in _guide_text("review")


def test_fix_guide_says_a_source_grep_is_not_a_test_of_behaviour() -> None:
    """A test that greps for a call site proves nothing about what the code does when it runs."""
    assert (
        "A test of behaviour runs the code it tests: it calls the function, drives the page's "
        "scripts, or runs the command, and asserts what comes out. Searching the source for a call "
        "or a string is not a test of behaviour, even when it fails once the line is deleted."
    ) in _guide_text("fix")


def test_plan_guide_says_a_source_grep_is_not_a_test_of_behaviour() -> None:
    """An acceptance line reviewed as behaviour must be closed by a test that runs the code, not greps for it."""
    assert (
        "A test of behaviour runs the code it tests: it calls the function, drives the page's "
        "scripts, or runs the command, and asserts what comes out. Searching the source for a call "
        "or a string is not a test of behaviour, even when it fails once the line is deleted."
    ) in _guide_text("plan")


def test_review_guide_says_a_source_grep_is_no_evidence_for_behaviour() -> None:
    """A reviewer who accepts a source grep as proof of behaviour approves a page that crashes at runtime."""
    assert (
        "A test that only searches source text is not evidence for an acceptance line about "
        "behaviour; name it in the findings."
    ) in _guide_text("review")


def test_dispatch_guide_closes_a_wording_only_rejection_without_another_fix_round() -> None:
    """A rejection that only disputes the record's wording, on a ruling already made, is not a defect to fix again."""
    assert (
        "A rejection whose findings are only about the record's wording or accuracy, on work whose "
        "ruling is already made, does not buy another fix round: the findings go in the node's "
        "`report` section and the node closes as the ruling stands."
    ) in _guide_text("dispatch")


GENERATOR_MISSING_FAILS = (
    "- A test that checks a generated artifact against its generator fails, never skips, when the "
    "generator is missing: a skipped check reads as a pass in every gate that runs it."
)
GENERATED_FILE_IS_REBUILT = (
    "- A generated file (a built stylesheet, a lockfile, a schema dump) is regenerated, never "
    "edited or hand-merged: a branch that changes any of its inputs rebuilds it before closing, "
    "and a conflict on it is resolved by rebuilding it on the merged tree."
)
GENERATED_ARTIFACT_RULES = [
    ("plan", GENERATOR_MISSING_FAILS),
    ("fix", GENERATOR_MISSING_FAILS),
    ("implement", GENERATED_FILE_IS_REBUILT),
    ("merge", GENERATED_FILE_IS_REBUILT),
]


@pytest.mark.parametrize(
    "topic,rule", GENERATED_ARTIFACT_RULES, ids=[t for t, _ in GENERATED_ARTIFACT_RULES]
)
def test_tm_guide_prints_the_generated_artifact_rule(topic: str, rule: str, tmp_path: Path) -> None:
    """A freshness check that skips without its generator reads as a pass in every gate that runs it."""
    result = CliRunner().invoke(app, ["guide", topic, "--builtin", "-C", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert rule in result.stdout


@pytest.fixture
def rendered(tmp_path: Path) -> Callable[[str], str]:
    """`tm guide <topic>` as an agent reads it, on an estate with no addendum."""
    runner = CliRunner()
    assert runner.invoke(app, ["init", "-C", str(tmp_path)]).exit_code == 0

    def render(topic: str) -> str:
        result = runner.invoke(app, ["guide", topic, "-C", str(tmp_path)])
        assert result.exit_code == 0, result.output
        return result.stdout

    return render


def test_review_guide_reviews_a_container_once_on_its_landed_target(
    rendered: Callable[[str], str],
) -> None:
    text = rendered("review")
    assert "- `LANDED`: a plan's or spec's one review." in text
    assert "**A plan's or spec's review** runs once, on its landed target." in text


def test_review_guide_reads_what_every_node_under_a_landed_container_landed_on_its_target(
    rendered: Callable[[str], str],
) -> None:
    """A plan whose children landed on main by themselves has no landing merge of its own to read."""
    text = rendered("review")
    assert (
        "the landing merge of every node under it that landed on that target itself rather than "
        "on the node's branch; a node whose children all landed that way has no landing merge of "
        "its own."
    ) in text
    assert (
        "git -C <repo> log -p --diff-merges=first-parent -E --grep "
        "'^merge[(](<node-id>|<id under it>|...)[)]: land [^ ]+ on <base>$' <branch> --"
    ) in text
    assert "A repository where that prints nothing had nothing land." in text
    assert '--grep "^merge(<node-id>): land " <branch>' not in text


def test_merge_guide_ends_a_reviewed_container_s_landing_at_landed(
    rendered: Callable[[str], str],
) -> None:
    assert (
        "7. **Complete.** The merge worktree is removed and the node is `COMPLETED`, or `LANDED` "
        "when it is a plan or spec with review on: its one review reads what landed."
    ) in rendered("merge")


def test_merge_guide_scopes_push_errors_to_a_push_to_main(
    rendered: Callable[[str], str],
) -> None:
    """A container branch's compare-and-swap stops at push_failed without any push_errors."""
    text = rendered("merge")
    assert (
        "A push to `main` records each in `result.push_errors` with its command, exit code and "
        "stderr: an `ls-remote` with no answer is the network or the remote, a refused `push` a "
        "permission, a protection rule or a hook."
    ) in text
    assert (
        "A container branch, moved by a landing or a sync, records none: it moved under each of "
        "three compare-and-swaps, so other landings or syncs onto it kept moving it."
    ) in text


def test_review_guide_scopes_a_re_review_to_the_open_findings_of_a_sensitive_fix(
    rendered: Callable[[str], str],
) -> None:
    text = rendered("review")
    steps, never = text.split("## Never", 1)
    assert "**A re-review** is scoped to the open findings of a sensitive fix" in steps
    assert "It never widens" in steps
    assert "Never widen a re-review past the findings still open." in never


def test_fix_guide_lands_a_fix_without_a_re_review_unless_the_node_is_sensitive(
    rendered: Callable[[str], str],
) -> None:
    text = rendered("fix")
    assert text.count("A fix lands without a re-review unless the node is sensitive") == 1
    assert "is reviewed again" not in text
    assert "The node moves to `FIXED`. Leave the worktree in place." in text
    assert "runs its one re-review first" not in text


def test_review_guide_says_once_that_a_fix_lands_without_a_re_review_unless_sensitive(
    rendered: Callable[[str], str],
) -> None:
    text = rendered("review")
    assert text.count("unless the node is sensitive") == 1
    assert "lands without another review unless the node is sensitive" in text
    assert "It is the only review a fix gets; every other fix lands without one." not in text
    assert "- `FIXED`: the re-review of a sensitive node's fix (`tm guide plan`, §2).\n" in text
    assert "two halves that do not join.\n" in text
    assert "the fix lands without coming back to review" not in text


def test_plan_guide_gives_children_of_a_reviewed_plan_no_review_of_their_own(
    rendered: Callable[[str], str],
) -> None:
    assert (
        "Children under a reviewed plan or spec take `review: false` and `fix: false` by default"
        in rendered("plan")
    )


def test_plan_guide_shows_the_sensitive_key_with_every_area_tm_accepts_and_the_migration_rule(
    rendered: Callable[[str], str],
) -> None:
    """The areas and the migration path are read off the guide, so it cannot drift from what the
    validator accepts or what `writes_migration` matches."""
    line = next(ln for ln in rendered("plan").splitlines() if ln.startswith("- `sensitive`:"))
    assert [area for area in SENSITIVE_AREAS if f"`{area}`" not in line] == []
    assert "`sensitive: [tenant, rls]`" in line
    marker = re.search(r"a path under `([^`]+)` in its `declared_files`, is sensitive", line)
    assert marker is not None, line
    assert writes_migration([f"api/{marker.group(1)}0001_tenants.py"])


def test_dispatch_guide_offers_a_landed_node_s_review(rendered: Callable[[str], str]) -> None:
    assert "it reads `LANDED`, and its one review is claimable" in rendered("dispatch")


def test_dispatch_guide_never_re_dispatches_a_review_of_a_fix_that_is_not_sensitive(
    rendered: Callable[[str], str],
) -> None:
    steps, never = rendered("dispatch").split("## Never", 1)
    assert "A dispatcher never re-dispatches a review of a fix that is not sensitive" in steps
    assert "Never re-dispatch a review of a fix that is not sensitive." in never


@pytest.mark.parametrize(
    "doc", ["skills/dispatcher/SKILL.md", "src/taskmanager/skills/dispatcher/SKILL.md"]
)
def test_dispatcher_skill_lands_a_reviewed_container_first_and_never_re_reviews_a_plain_fix(
    doc: str,
) -> None:
    text = _doc_text(doc)
    assert "reads `LANDED` until its one review, on its landed target, runs" in text
    assert "never re-dispatch a review of a fix that is not sensitive" in text


def test_overview_lands_a_reviewed_container_before_its_one_review(
    rendered: Callable[[str], str],
) -> None:
    text = rendered("overview")
    assert (
        "IMPLEMENTED ──claim, a plan or spec with review on──▶ MERGING ──landed and verified──▶ "
        "LANDED ──claim──▶ REVIEWING"
    ) in text
    assert "| `review` | `IMPLEMENTED` for a task, `LANDED` for a plan or spec," in text


def test_overview_lands_a_fix_without_a_re_review_unless_the_node_is_sensitive(
    rendered: Callable[[str], str],
) -> None:
    text = rendered("overview")
    assert "FIXED ──claim, not sensitive──▶ MERGING" in text
    assert "FIXED ──claim, sensitive──▶ REVIEWING" in text
    assert "FIXED ──claim──▶ REVIEWING" not in text
    assert "`FIXED` for a sensitive node" in text


PLAN_REVIEW = "## 8. Write the review into the node"
BRIEF_RULES = [
    pytest.param(
        "plan",
        PLAN_REVIEW,
        "- Tasks that write into one directory each declare their own paths, and none deletes a "
        "path it did not declare.",
        id="plan:shared-directory",
    ),
    pytest.param(
        "plan",
        PLAN_REVIEW,
        "- A task whose own code runs work concurrently names every file or row two workers write, "
        "and how those writes serialize. Concurrency across tasks is what `declared_files` and "
        "discovery already keep disjoint.",
        id="plan:concurrent-writes",
    ),
    pytest.param(
        "plan",
        PLAN_REVIEW,
        "- A task that consumes another task's derived structure (ids, an ordering, a mapping) "
        "names that task and reads its output. It never re-derives the structure.",
        id="plan:derived-structure",
    ),
    pytest.param(
        "plan",
        PLAN_REVIEW,
        "- A brief that has the implementer step or search over a value names that value's "
        "allowed range.",
        id="plan:value-range",
    ),
    pytest.param(
        "plan",
        PLAN_REVIEW,
        "Each line has a check that drives it. A panel over lazily loaded data names each of its "
        "states (loading, partial, empty, error, ready) and the reads each state depends on.",
        id="plan:lazy-panel-states",
    ),
    pytest.param(
        "implement",
        "## 3. Work in that worktree and nowhere else",
        "Create, modify or delete nothing outside `declared_files`. Before touching another file, "
        "read the locks in `tm run list --yaml`. When another live node holds the file, release "
        "blocked and name that node (below). Otherwise add the file with `tm task update "
        "<task-id> --set declared_files='[...]'` before the edit, and name it in the report.",
        id="implement:declared-files",
    ),
    pytest.param(
        "implement",
        "## 6. Report",
        "A report that fixes a contract its dependents build on (a shape, a name, an id scheme) "
        "writes the contract into the parent plan's `overview` with `tm section set "
        "<plan-id>:overview --file <path>` before the step closes. Only the parent's `context` "
        "and `overview` reach a dependent's brief, and no step runs between tasks to copy it "
        "there.",
        id="implement:contract-to-overview",
    ),
    pytest.param(
        "implement",
        "## 5. Verify",
        "- A test selects only markup that its own task's declared files render. It reaches "
        "another file's control by what that control shows the user (role, accessible name), "
        "never by its classes or inner elements.",
        id="implement:test-markup",
    ),
    pytest.param(
        "implement",
        "## 5. Verify",
        "- When a task stops reading a payload field, it removes the producer in the same task, "
        "or names the task that does.",
        id="implement:payload-producer",
    ),
    pytest.param(
        "review",
        "## 3. Read the branch",
        "A diff that edits a file missing from `declared_files`, a test that selects another "
        "file's markup by class, and a payload field with no remaining reader are each a finding.",
        id="review:scope-findings",
    ),
    pytest.param(
        "overview",
        "# How TaskManager works",
        "After `tm init`, set `repos.<repo>.gates.main` for every target repo before the first "
        "dispatch. Without it, every landing on `main` is refused with `no gate`.",
        id="overview:main-gate",
    ),
]

_HEADING = re.compile(r"^(?=#{1,2} )", re.MULTILINE)


@pytest.mark.parametrize(("topic", "heading", "rule"), BRIEF_RULES)
def test_tm_guide_prints_each_brief_rule_once_in_its_role_s_section(
    topic: str, heading: str, rule: str, rendered: Callable[[str], str]
) -> None:
    """The skills defer these rules to `tm guide`, so a guide that drops one leaves no copy an agent reads."""
    holding = [
        (t, part.splitlines()[0])
        for t in _topics()
        for part in _HEADING.split(rendered(t))
        if rule in part
    ]
    assert holding == [(topic, heading)]
