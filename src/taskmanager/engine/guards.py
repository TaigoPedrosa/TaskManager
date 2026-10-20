"""The guards a step's diff adds or changes, read line by line from the unified diff, and which of
them its red-check records cover.

ponytail: a regex heuristic over single lines, not a parser; it misses a condition split across
lines and reads a guard inside a multi-line string. A tree-sitter pass per language is the upgrade.
"""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from taskmanager.core.models import Check
from taskmanager.core.status import CheckResult
from taskmanager.engine import git as gitops

_BRACE = frozenset({".java", ".kt", ".kts", ".scala", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_PYTHON = frozenset({".py"})
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "__mocks__"})
_TEST_NAME = re.compile(
    r"^test_.*\.py$|_test\.py$|^conftest\.py$|(Test|Tests|IT)\.(java|kt|scala)$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$"
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')

# A ternary's ` ? `, never Java's `? extends` wildcard.
_BRACE_CONDITION = re.compile(r"^(\}\s*)?(else\s+)?(if|while)\s*\(|\s\?\s(?!extends\b|super\b)")
_PY_CONDITION = re.compile(r"^(if|elif|while)\b|\bif\b.+\belse\b|\bfor\b.+\bif\b")
_BRACE_OPERATOR = re.compile(r"(&&|\|\|)(?!=)")
_PY_OPERATOR = re.compile(r"\b(and|or)\b")
_BRACE_CATCH = re.compile(r"^(\}\s*)?catch\b|\.catch\(")
_PY_CATCH = re.compile(r"^except\b")
_RETURN = re.compile(r"^return\b")
_INLINE_RETURN = re.compile(r"^(\}\s*)?(else\s+|el)?if\b.*[):{]\s*return\b")
_HEADER = re.compile(r"^(\}\s*)?(else\b|if\b|elif\b|case\b|default\b)")
_ASSIGN_OP = r"\s*(?:[-+*/%&|^]|\?\?|\|\||&&|<<|>>)?=(?!=)"
_BRACE_ASSIGN = re.compile(
    r"^#?[A-Za-z_$][\w$]*(\.#?[A-Za-z_$][\w$]*|\[[^\]]*\])*"
    + _ASSIGN_OP
    + r"|^((private|protected|public|static|readonly)\s+)+[^=(]*\b[\w$]+"
    + _ASSIGN_OP
)
# An attribute or an item: a bare name is a local in Python.
_PY_ASSIGN = re.compile(r"^[A-Za-z_]\w*(\.\w+|\[[^\]]*\])+(\s*:[^=]+)?" + _ASSIGN_OP)
_LIST_FACTORY = r"(\.of|\basList|\blistOf|\bsetOf|\barrayOf|\bmutableListOf)"
_ONE_LINE_LIST = re.compile(r"\[[^\[\]]*,[^\[\]]*\]|" + _LIST_FACTORY + r"\([^()]*,")
_DESTRUCTURE = re.compile(r"\b(const|let|var)\s*\[")
_LIST_CALL = re.compile(_LIST_FACTORY + r"\s*$")


@dataclass(frozen=True)
class GuardSite:
    path: str
    line: int
    text: str
    # Deletions the site needs: one per `&&`/`||` operand, one otherwise.
    rows: int

    @property
    def label(self) -> str:
        return f"{self.path}:{self.line}"


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(_TEST_DIRS.intersection(p.parts[:-1])) or _TEST_NAME.search(p.name) is not None


def added_lines(diff: str) -> dict[str, list[int]]:
    """Each file a `git diff -U0` adds or changes lines in, with those lines' new numbers."""
    found: dict[str, list[int]] = {}
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else None
        elif path is not None and (hunk := _HUNK.match(line)):
            start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
            found.setdefault(path, []).extend(range(start, start + count))
    return found


def _code(line: str, python: bool) -> str:
    """`line` with its string literals emptied and its trailing comment dropped."""
    bare = _STRING.sub('""', line).strip()
    comment = "#" if python else "//"
    return bare.split(comment, 1)[0].strip()


def _list_openers(lines: list[str], python: bool) -> list[bool]:
    """For each line, whether it starts inside a list literal: the innermost bracket open at its
    start is a `[`, a `List.of(`-style call, or a Java array's `]{`."""
    inside: list[bool] = []
    stack: list[bool] = []
    for line in lines:
        inside.append(bool(stack) and stack[-1])
        code = _code(line, python)
        for i, char in enumerate(code):
            if char in "[({":
                before = code[:i].rstrip()
                stack.append(
                    char == "["
                    or (char == "(" and _LIST_CALL.search(before) is not None)
                    or (char == "{" and before.endswith("]"))
                )
            elif char in "])}" and stack:
                stack.pop()
    return inside


def _rows(code: str, prev: str, in_list: bool, python: bool) -> int:
    condition = (_PY_CONDITION if python else _BRACE_CONDITION).search(code)
    operators = len((_PY_OPERATOR if python else _BRACE_OPERATOR).findall(code))
    rows = operators + 1 if operators else int(condition is not None)
    if (_PY_CATCH if python else _BRACE_CATCH).search(code):
        rows += 1
    header_return = _RETURN.match(code) and _HEADER.match(prev)
    if header_return or _INLINE_RETURN.match(code):
        rows += 1
    if (_PY_ASSIGN if python else _BRACE_ASSIGN).match(code):
        rows += 1
    element = in_list and not code.startswith(("]", ")", "}"))
    one_line = _ONE_LINE_LIST.search(code) and not _DESTRUCTURE.search(code)
    if element or one_line:
        rows += 1
    return rows


def sites_in(path: str, source: str, changed: list[int]) -> list[GuardSite]:
    """The guards on `changed` lines of `path`, whose content is `source`."""
    suffix = PurePosixPath(path).suffix
    if is_test_file(path) or suffix not in _BRACE | _PYTHON:
        return []
    python = suffix in _PYTHON
    lines = source.splitlines()
    in_list = _list_openers(lines, python)
    sites = []
    for number in sorted(set(changed)):
        # A path `git show` cannot read (a submodule) has no lines to hold its hunk's.
        if number > len(lines):
            continue
        code = _code(lines[number - 1], python)
        if not code or code.startswith(("*", "/*")):
            continue
        prev = next((c for raw in reversed(lines[: number - 1]) if (c := _code(raw, python))), "")
        rows = _rows(code, prev, in_list[number - 1], python)
        if rows:
            sites.append(GuardSite(path, number, lines[number - 1].strip(), rows))
    return sites


def guard_sites(repo_dir: Path, base: str, branch: str) -> list[GuardSite]:
    """Every guard `branch` adds or changes against its merge base with `base`."""
    diff = gitops.unified_diff(repo_dir, base, branch)
    sites = []
    for path, changed in added_lines(diff).items():
        sites.extend(sites_in(path, gitops.show(repo_dir, branch, path), changed))
    return sites


def _same_file(named: str, path: str) -> bool:
    return named == path or path.endswith(f"/{named}") or named.endswith(f"/{path}")


def _names(check: Check, site: GuardSite) -> bool:
    """A check names a site by `<path>:<line>`, or by its path and the site's line text."""
    path, _, rest = check.deleted.partition(":")
    if not _same_file(path.strip(), site.path):
        return False
    line = re.match(r"\d+", rest)
    return (line is not None and int(line.group()) == site.line) or site.text in rest


def uncovered(sites: list[GuardSite], checks: list[Check]) -> list[GuardSite]:
    """Each site with fewer distinct deletions recorded than it needs; a waived check covers its
    whole site."""
    left = []
    for site in sites:
        naming = [c for c in checks if _names(c, site)]
        if any(c.result == CheckResult.WAIVED for c in naming):
            continue
        if len({c.deleted for c in naming}) < site.rows:
            left.append(site)
    return left
