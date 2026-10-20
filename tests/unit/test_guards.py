import pytest

from taskmanager.core.models import Check
from taskmanager.core.status import CheckResult
from taskmanager.engine.guards import GuardSite, added_lines, is_test_file, sites_in, uncovered


def rows_at(path: str, source: str, line: int) -> int:
    sites = sites_in(path, source, [line])
    return sites[0].rows if sites else 0


@pytest.mark.parametrize(
    ("path", "source", "line", "rows"),
    [
        ("A.java", "if (a == null) {\n", 1, 1),
        ("A.java", "} else if (x) {\n", 1, 1),
        ("A.java", "while (running) {\n", 1, 1),
        ("A.java", "int n = ok ? 1 : 2;\n", 1, 1),
        ("A.java", "Map<K, ? extends T> m = f();\n", 1, 0),
        ("A.java", "if (a && b || c) {\n", 1, 3),
        ("A.java", "} catch (IOException e) {\n", 1, 1),
        ("a.ts", "promise.catch((e) => log(e));\n", 1, 1),
        ("A.java", "if (x) {\n    return;\n}\n", 2, 1),
        ("A.java", "} else {\n    return 0;\n}\n", 2, 1),
        ("A.java", "int f() {\n    return 1;\n}\n", 2, 0),
        ("a.ts", "if (x) return;\n", 1, 2),
        ("a.ts", "dead = true;\n", 1, 1),
        ("a.ts", "this.closing = false;\n", 1, 1),
        ("a.ts", "count += 1;\n", 1, 1),
        ("A.java", "private final Clock clock = Clock.systemUTC();\n", 1, 1),
        ("a.ts", "const x = 1;\n", 1, 0),
        ("a.ts", "if (a === b) {\n", 1, 1),
        ("A.java", 'List<String> xs = List.of(\n    "a",\n);\n', 2, 1),
        ("a.ts", "const xs = [\n  'a',\n];\n", 2, 1),
        ("a.ts", "const xs = [\n  'a',\n];\n", 3, 0),
        ("A.java", 'String[] xs = new String[] {\n    "a",\n};\n', 2, 1),
        ("A.java", 'String[] xs = new String[] {\n    "a",\n};\n', 3, 0),
        ("A.java", 'List<String> xs = List.of(\n    "a",\n);\n', 3, 0),
        ("a.ts", "const m = {\n  a: 1,\n};\n", 2, 0),
        ("a.ts", "const xs = ['a', 'b'];\n", 1, 1),
        ("A.java", "var xs = List.of(a, b);\n", 1, 1),
        ("a.ts", "const [a, b] = useState();\n", 1, 0),
        ("a.ts", 'log("a && b || c ? d : e");\n', 1, 0),
        ("a.ts", "// if (x) {\n", 1, 0),
        ("a.ts", "f(); // a && b\n", 1, 0),
        ("a.py", "if x:\n", 1, 1),
        ("a.py", "elif y:\n", 1, 1),
        ("a.py", "while z:\n", 1, 1),
        ("a.py", "v = a if c else b\n", 1, 1),
        ("a.py", "ys = [x for x in xs if x]\n", 1, 1),
        ("a.py", "if a and b or c:\n", 1, 3),
        ("a.py", "except ValueError:\n", 1, 1),
        ("a.py", "if x:\n    return None\n", 2, 1),
        ("a.py", "def f():\n    return 1\n", 2, 0),
        ("a.py", "self.x = 1\n", 1, 1),
        ("a.py", "self.x: int = 1\n", 1, 1),
        ("a.py", 'd["k"] = v\n', 1, 1),
        ("a.py", "x = 1\n", 1, 0),
        ("a.py", 'XS = [\n    "a",\n]\n', 2, 1),
        ("a.py", "# if x:\n", 1, 0),
        ("a.py", 'log("x if y else z")\n', 1, 0),
        ("README.md", "if (x) {\n", 1, 0),
        ("A.java", "Map<K, ? super T> m = f();\n", 1, 0),
        ("a.kt", "val s: String? = null\n", 1, 0),
        ("A.java", "} else if (x) return 1;\n", 1, 2),
        ("a.py", "f()  # a and b\n", 1, 0),
        ("A.java", "if (x) {\n\n    return;\n}\n", 3, 1),
        ("a.py", "elif y:\n    return None\n", 2, 1),
        ("A.java", "case 1:\n    return 2;\n", 2, 1),
        ("A.java", "default:\n    return 0;\n", 2, 1),
        ("a.ts", "a ??= b;\n", 1, 1),
        ("a.ts", "a ||= b;\n", 1, 1),
        ("a.ts", "a &&= b;\n", 1, 1),
        ("a.ts", "n <<= 1;\n", 1, 1),
        ("a.ts", "n >>= 1;\n", 1, 1),
        ("a.py", "self.x == y\n", 1, 0),
        ("a.ts", "state.dead = true;\n", 1, 1),
        ("a.ts", "cache[key] = v;\n", 1, 1),
        ("A.java", "protected int n = 0;\n", 1, 1),
        ("A.java", "public int n = 0;\n", 1, 1),
        ("a.ts", "static n = 0;\n", 1, 1),
        ("a.ts", "readonly limit = 5;\n", 1, 1),
        ("A.java", "final int n = 0;\n", 1, 0),
        ("A.java", "var xs = Arrays.asList(a, b);\n", 1, 1),
        ("a.kt", "val xs = listOf(a, b)\n", 1, 1),
        ("a.kt", "val xs = setOf(a, b)\n", 1, 1),
        ("a.kt", "val xs = arrayOf(a, b)\n", 1, 1),
        ("a.kt", "val xs = mutableListOf(a, b)\n", 1, 1),
        ("a.kt", "val xs = listOf(\n    a,\n)\n", 2, 1),
        ("a.ts", "let [a, b] = f();\n", 1, 0),
        ("a.ts", "var [a, b] = f();\n", 1, 0),
        ("a.ts", "log('a && b');\n", 1, 0),
        ("a.ts", "log(`a && b`);\n", 1, 0),
        ("a.py", "if x: return None\n", 1, 2),
        ("a.py", "elif x: return None\n", 1, 2),
        ("a.ts", "if (x) { return; }\n", 1, 2),
        ("a.ts", "const xs = [\n\n  'a',\n];\n", 2, 0),
        ("A.java", " * if (a && b) return x;\n", 1, 0),
        ("A.java", "/* a && b */\n", 1, 0),
        ("a.ts", "#count = 0;\n", 1, 1),
        ("a.ts", "this.#count = 0;\n", 1, 1),
        ("a.ts", "const re = /)/;\ndead = true;\n", 2, 1),
    ],
)
def test_each_guard_kind_is_read_as_its_deletions(
    path: str, source: str, line: int, rows: int
) -> None:
    assert rows_at(path, source, line) == rows


@pytest.mark.parametrize(
    "suffix", [".java", ".kt", ".kts", ".scala", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]
)
def test_each_brace_language_is_read(suffix: str) -> None:
    assert rows_at(f"a{suffix}", "if (x) {\n", 1) == 1


def test_a_line_past_the_source_read_is_none() -> None:
    assert sites_in("a.ts", "", [1]) == []


def test_a_line_with_no_guard_is_no_site() -> None:
    assert sites_in("a.ts", "const x = 1;\n", [1]) == []


@pytest.mark.parametrize(
    "path",
    [
        "src/test/java/a/FooTest.java",
        "src/main/java/a/FooTests.java",
        "svc/FooIT.java",
        "web/a.test.ts",
        "web/a.spec.tsx",
        "web/__tests__/a.ts",
        "web/__mocks__/a.ts",
        "tests/a.py",
        "pkg/test_a.py",
        "pkg/a_test.py",
        "pkg/conftest.py",
        "src/test/java/a/Fixtures.java",
        "src/a/FooTest.kt",
        "src/a/FooTest.scala",
        "web/a.test.mjs",
        "web/a.spec.js",
    ],
)
def test_a_guard_in_a_test_file_is_none(path: str) -> None:
    assert is_test_file(path)
    assert sites_in(path, "if (x) {\nif x:\n", [1, 2]) == []


@pytest.mark.parametrize("path", ["src/main/java/a/Contest.java", "pkg/a.py", "web/latest.ts"])
def test_a_source_file_is_no_test_file(path: str) -> None:
    assert not is_test_file(path)


def test_added_lines_reads_each_hunk_s_new_lines_and_skips_deleted_files() -> None:
    diff = (
        "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n"
        "@@ -1,0 +2,2 @@\n+x\n+y\n@@ -9 +11 @@\n-z\n+w\n@@ -20,3 +23,0 @@\n-a\n-b\n-c\n"
        "diff --git a/gone.ts b/gone.ts\n--- a/gone.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    assert added_lines(diff) == {"a.ts": [2, 3, 11]}


SITE = GuardSite("src/a.ts", 12, "if (a && b) {", 2)


def check(deleted: str, result: CheckResult = CheckResult.RED) -> Check:
    test = None if result == CheckResult.GREEN else "why"
    return Check(node_id="T", deleted=deleted, result=result, test=test)


@pytest.mark.parametrize(
    "checks",
    [
        [check("src/a.ts:12 left"), check("src/a.ts:12 right")],
        [check("a.ts:12 left"), check("api/src/a.ts:12 right")],
        [check("src/a.ts:if (a && b) { left"), check("src/a.ts:12 right")],
        [check("src/a.ts:12", CheckResult.WAIVED)],
    ],
    ids=["path-and-line", "path-suffixes", "line-text", "waived"],
)
def test_a_site_is_covered_by_one_deletion_per_operand_or_a_waiver(checks: list[Check]) -> None:
    assert uncovered([SITE], checks) == []


@pytest.mark.parametrize(
    "checks",
    [
        [],
        [check("src/a.ts:12")],
        [check("src/a.ts:12"), check("src/a.ts:12")],
        [check("src/a.ts:13 left"), check("src/a.ts:13 right")],
        [check("src/b.ts:12 left"), check("src/b.ts:12 right")],
        [check("src/a.ts:120 left"), check("src/a.ts:120 right")],
    ],
    ids=["none", "one-operand", "same-deletion-twice", "other-line", "other-file", "longer-line"],
)
def test_a_site_short_of_its_deletions_is_uncovered(checks: list[Check]) -> None:
    assert uncovered([SITE], checks) == [SITE]
