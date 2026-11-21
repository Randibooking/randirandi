# SPDX-License-Identifier: AGPL-3.0-only
"""The test files a commit's changes can reach, for the commit hook.

The pre-commit hook runs only these; GitHub runs every test on each push
. Changed paths come on standard input, one per line; the
test files go to standard output, one per line. Nothing is printed when no
test can be affected, for example by a change to documentation alone.

A test file is picked when it is changed itself, or when it imports a changed
module, directly or through other modules. A dotted name in a string, such
as a patch target or a setting, counts as an import. Any other changed file
counts as a change to each module whose code names its path or file name,
and a management command to each that names the command. A test that sends
requests through Django or reverses a URL reaches the settings, and through
them the URLs, the views and the middleware; a changed template reaches
every such test, since templates extend and include each other. A changed
migration also picks every test that runs migrations. A few files reach
every test.

With `--shares N` the files come in up to N shares of about as long a run,
one share per line, for the hook to run side by side. A file weighs the
seconds it took in the hook's last run of it, kept on this Mac by `--record
LOGS` from the shares' test reports; without any times (GitHub), a file
weighs its number of tests.
"""

import ast
import json
import math
import re
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

ROOTS = ("randirandi", "tools", "tests")
# Every test reads these: the settings, the URLs, the fixtures, the packages.
EVERY_TEST = {
    "pyproject.toml",
    "uv.lock",
    "images.env",
    "bin/check",
    "randirandi/settings.py",
    "randirandi/urls.py",
    "tests/__init__.py",
    "tests/conftest.py",
}
# What a test that runs migrations names.
MIGRATING = ("MigrationExecutor", '"migrate"')
# What a test that sends requests through Django, or reverses a URL, names.
CLIENT = re.compile(r"\bclient\b|\bClient\(|\breverse\(|RequestFactory")
SETTINGS = "randirandi.settings"
TEST = re.compile(r"^\s*(?:async )?def test_", re.MULTILINE)
# What each test file took when the hook last ran it, in seconds. Weighting by
# the number of tests left the share with the migration tests a minute behind
# the others, over the hook's budget .
SECONDS = Path(".local/commit-test-seconds.json")


def modules(root: Path) -> dict[str, Path]:
    """Every Python module in the repository, by dotted name."""
    found = {}
    for top in ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            parts = path.relative_to(root).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            found[".".join(parts)] = path
    return found


def strings(tree: ast.Module) -> list[str]:
    """The string values in the code, without its docstrings."""
    documents = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in documents
    ]


def _absolute(name: str, path: Path, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    base = package.split(".")
    base = base[: len(base) - node.level + 1]
    return ".".join([*base, node.module] if node.module else base)


def _within(dotted: str, known: dict[str, Path]) -> str | None:
    """The longest known module that a dotted name starts with."""
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in known:
            return candidate
    return None


def imports(
    known: dict[str, Path], trees: dict[str, ast.Module]
) -> dict[str, set[str]]:
    """The modules each module imports, by dotted name."""
    # The submodule each name a package passes on comes from.
    exports: dict[str, dict[str, str]] = {}
    for name, path in known.items():
        exports[name] = {}
        if path.name != "__init__.py":
            continue
        for node in ast.walk(trees[name]):
            if isinstance(node, ast.ImportFrom):
                source = _absolute(name, path, node)
                if source.startswith(name + ".") and source in known:
                    for alias in node.names:
                        exports[name][alias.asname or alias.name] = source

    graph = {}
    for name, path in known.items():
        edges = set()
        for node in ast.walk(trees[name]):
            if isinstance(node, ast.Import):
                edges |= {_within(alias.name, known) for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                source = _absolute(name, path, node)
                for alias in node.names:
                    whole = f"{source}.{alias.name}"
                    if whole in known:
                        edges.add(whole)
                    elif alias.name in exports.get(source, {}):
                        edges.add(exports[source][alias.name])
                    else:
                        edges.add(_within(source, known))
        for value in strings(trees[name]):
            if value.startswith(ROOTS) and all(
                part.isidentifier() for part in value.split(".")
            ):
                edges.add(_within(value, known))
        graph[name] = {edge for edge in edges if edge is not None} - {name}
    return graph


def select(root: Path, changed: list[str]) -> list[str]:
    known = modules(root)
    tests = sorted(
        name
        for name, path in known.items()
        if path.parent == root / "tests" and path.name.startswith("test_")
    )

    def file_of(name: str) -> str:
        return known[name].relative_to(root).as_posix()

    if EVERY_TEST & set(changed):
        return [file_of(name) for name in tests]
    trees = {
        name: ast.parse(path.read_text(), str(path)) for name, path in known.items()
    }
    values = {name: strings(tree) for name, tree in trees.items()}
    texts = {name: known[name].read_text() for name in tests}
    by_path = {file_of(name): name for name in known}
    touched: set[str] = set()
    picked: set[str] = set()
    for path in changed:
        if "/templates/" in path:
            touched.add(SETTINGS)
        if path in by_path and "/management/commands/" in path:
            # Run by name, not imported: also each module naming it.
            command = path.rpartition("/")[2].removesuffix(".py")
            touched.add(by_path[path])
            touched |= {
                name
                for name in known
                if any(command in value for value in values[name])
            }
        elif path in by_path:
            touched.add(by_path[path])
        else:
            # A file that is not a module, or a module that was removed. A
            # file name without a dot is too common a word to look for.
            names = [path]
            if path.endswith(".py"):
                names.append(path.removesuffix(".py").replace("/", "."))
            elif "." in path.rpartition("/")[2]:
                names.append(path.rpartition("/")[2])
            if "/management/commands/" in path:
                names.append(path.rpartition("/")[2].removesuffix(".py"))
            touched |= {
                name
                for name in known
                if any(item in value for value in values[name] for item in names)
            }
        if "/migrations/" in path:
            picked |= {
                name for name in tests if any(word in texts[name] for word in MIGRATING)
            }
    graph = imports(known, trees)
    for test in tests:
        if CLIENT.search(texts[test]):
            graph[test].add(SETTINGS)
        seen, todo = {test}, [test]
        while todo:
            for edge in graph[todo.pop()] - seen:
                seen.add(edge)
                todo.append(edge)
        if seen & touched:
            picked.add(test)
    return [file_of(name) for name in tests if name in picked]


def shares(
    root: Path, files: list[str], count: int, seconds: dict[str, float] | None = None
) -> list[list[str]]:
    """The files in up to `count` shares of about as long a run each. A file
    without a time of its own weighs its tests at the timed files' seconds per
    test; with no times at all, a share holds about as many tests."""
    tests = {
        path: max(1, len(TEST.findall((root / path).read_text()))) for path in files
    }
    timed = {path: took for path, took in (seconds or {}).items() if path in tests}
    per_test = sum(timed.values()) / sum(tests[path] for path in timed) if timed else 1
    weight = {path: timed.get(path, tests[path] * per_test) for path in files}
    found: list[tuple[float, list[str]]] = [
        (0, []) for _ in range(min(count, len(files)))
    ]
    for path in sorted(files, key=lambda path: (-weight[path], path)):
        index = min(range(len(found)), key=lambda index: found[index][0])
        total, paths = found[index]
        found[index] = (total + weight[path], [*paths, path])
    return [sorted(paths) for _, paths in found]


def test_file(root: Path, classname: str) -> str | None:
    """The test file of a report's `tests.test_x` or `tests.test_x.Group`."""
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        path = "/".join(parts[:end]) + ".py"
        if (root / path).is_file():
            return path
    return None


def kept_times(root: Path) -> dict[str, float]:
    """The kept times; none when the file is missing or unreadable, so that a
    damaged file splits by the number of tests instead of stopping commits."""
    try:
        found = json.loads((root / SECONDS).read_text())
    except OSError, ValueError:
        return {}
    if not isinstance(found, dict):
        return {}
    return {
        path: float(took)
        for path, took in found.items()
        if isinstance(took, (int, float))
        and not isinstance(took, bool)
        and math.isfinite(took)
        and took >= 0
    }


def record(root: Path, logs: Path) -> dict[str, float]:
    """Each file's seconds in the shares' reports, set into the kept times;
    a file that is gone leaves them."""
    took: dict[str, float] = {}
    for report in sorted(logs.glob("share-*.xml")):
        # The shares' own pytest reports, written a moment ago on this Mac. A
        # share killed while writing leaves a broken one: the others still count.
        try:
            cases = [
                (case.get("classname", ""), float(case.get("time", 0)))
                for case in ElementTree.parse(report).iter("testcase")  # noqa: S314
            ]
        except ElementTree.ParseError, ValueError:
            continue
        for classname, seconds in cases:
            path = test_file(root, classname)
            if path is not None:
                took[path] = took.get(path, 0) + seconds
    store = root / SECONDS
    kept = {**kept_times(root), **took}
    kept = {path: s for path, s in kept.items() if (root / path).is_file()}
    store.parent.mkdir(exist_ok=True)
    temporary = store.with_suffix(".tmp")
    temporary.write_text(json.dumps(kept, indent=1, sort_keys=True) + "\n")
    temporary.replace(store)
    return took


def main() -> None:
    root = Path.cwd()
    if sys.argv[1:2] == ["--record"]:
        record(root, Path(sys.argv[2]))
        return
    changed = [line.strip() for line in sys.stdin if line.strip()]
    picked = select(root, changed)
    if sys.argv[1:2] == ["--shares"]:
        found = shares(root, picked, int(sys.argv[2]), kept_times(root))
        lines = [" ".join(share) for share in found]
    else:
        lines = picked
    sys.stdout.write("".join(f"{line}\n" for line in lines))


if __name__ == "__main__":
    main()
