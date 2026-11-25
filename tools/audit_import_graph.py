import argparse
import ast
from collections import deque
from graphlib import TopologicalSorter
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def sources(ref):
    if ref is None:
        return {p.stem: p.read_bytes() for p in sorted(ROOT.glob("*.py"))
                if p.is_file() and p.name != "conftest.py"}
    paths = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-tree", "--name-only", "-z", ref],
    ).decode("utf-8").split("\0")
    return {Path(p).stem: subprocess.check_output(
                ["git", "-C", str(ROOT), "show", f"{ref}:{p}"],
            ) for p in paths if p.endswith(".py") and p != "conftest.py"}


def internal_imports(source, modules):
    deps = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            deps.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level <= 1:
            if node.module:
                deps.add(node.module.split(".", 1)[0])
            else:
                deps.update(alias.name for alias in node.names)
    return deps & modules


def reachable(start, graph):
    seen = set()
    pending = deque(graph[start])
    while pending:
        name = pending.popleft()
        if name not in seen:
            seen.add(name)
            pending.extend(graph[name] - seen)
    return seen


def main():
    parser = argparse.ArgumentParser(description="Check direct imports between root production modules.")
    parser.add_argument("--ref", help="inspect root Python files at a Git ref instead of the worktree")
    args = parser.parse_args()
    modules = sources(args.ref)
    if not modules:
        parser.error("no root production Python modules found")
    graph = {name: internal_imports(src, modules.keys()) for name, src in sorted(modules.items())}
    reach = {name: reachable(name, graph) for name in graph}
    cycles = {tuple(sorted(peer for peer in reachable_nodes if name in reach[peer]))
              for name, reachable_nodes in reach.items() if name in reachable_nodes}

    print(f"source={args.ref or 'worktree'} scope=root-production-python edge=importer->dependency")
    print(f"modules={len(graph)} edges={sum(map(len, graph.values()))} cyclic_components={len(cycles)}")
    for name, deps in graph.items():
        print(f"{name} -> {', '.join(sorted(deps)) or '-'}")
    if cycles:
        for component in sorted(cycles):
            print("cycle component: " + ", ".join(component))
        return 1

    sorter = TopologicalSorter({name: sorted(deps) for name, deps in graph.items()})
    print("dependency-first order: " + " -> ".join(sorter.static_order()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
