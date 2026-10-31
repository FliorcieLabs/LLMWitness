"""Architecture regressions for SOLID dependency direction."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "llmwitness"
CORE_MODULES = {
    "artifacts",
    "authority",
    "contracts",
    "effects",
    "envelope",
    "journal",
    "passport",
    "planning",
    "receipts",
    "recovery",
    "replay",
    "state",
    "transactions",
}
EDGE_MODULES = {
    "adapters",
    "cli",
    "community",
    "evidence_bundle",
    "effect_conformance",
    "gateway",
    "ingest",
    "mcp_server",
    "projects",
    "reference_effects",
    "reliability",
    "runtime",
    "sdk",
}
PACKAGE_MODULES = {path.stem for path in PACKAGE_ROOT.glob("*.py")}


def _dependencies_from_source(source: str) -> set[str]:
    tree = ast.parse(source)
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                dependencies.add(node.module.split(".", 1)[0])
            elif node.level:
                dependencies.update(
                    alias.name.split(".", 1)[0]
                    for alias in node.names
                    if alias.name.split(".", 1)[0] in PACKAGE_MODULES
                )
            elif node.module == "llmwitness":
                dependencies.update(
                    alias.name.split(".", 1)[0]
                    for alias in node.names
                    if alias.name.split(".", 1)[0] in PACKAGE_MODULES
                )
            elif node.module and node.module.startswith("llmwitness."):
                dependencies.add(node.module.split(".", 1)[1].split(".", 1)[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("llmwitness."):
                    dependencies.add(alias.name.split(".", 1)[1].split(".", 1)[0])
    return dependencies


def _llmwitness_dependencies(module: str) -> set[str]:
    source = (PACKAGE_ROOT / f"{module}.py").read_text(encoding="utf-8")
    return _dependencies_from_source(source)


def test_dependency_parser_covers_absolute_and_relative_import_forms():
    cases = {
        "import llmwitness.projects": {"projects"},
        "from llmwitness.projects import ProjectRunner": {"projects"},
        "from llmwitness import projects": {"projects"},
        "from .projects import ProjectRunner": {"projects"},
        "from . import projects": {"projects"},
    }

    for source, expected in cases.items():
        assert _dependencies_from_source(source) == expected


def test_core_phase_modules_do_not_depend_on_edge_or_compatibility_modules():
    violations = {
        module: sorted(_llmwitness_dependencies(module) & EDGE_MODULES)
        for module in sorted(CORE_MODULES)
    }
    assert {module: imports for module, imports in violations.items() if imports} == {}


def test_core_phase_dependency_graph_is_acyclic():
    graph = {
        module: _llmwitness_dependencies(module) & CORE_MODULES
        for module in CORE_MODULES
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, path: tuple[str, ...]) -> None:
        if module in visiting:
            cycle_start = path.index(module)
            cycle = " -> ".join((*path[cycle_start:], module))
            raise AssertionError(f"core dependency cycle: {cycle}")
        if module in visited:
            return
        visiting.add(module)
        for dependency in sorted(graph[module]):
            visit(dependency, (*path, module))
        visiting.remove(module)
        visited.add(module)

    for module in sorted(CORE_MODULES):
        visit(module, ())
