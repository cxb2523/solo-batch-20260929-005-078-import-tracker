"""
This module implements utilities that enable tracking of third party deps
through import statements.

Import discovery is performed statically via ``ast`` traversal rather than by
disassembling bytecode. This keeps the parser stable across Python versions and
lets the tracker distinguish four conditional-import idioms:

* ``try``/``except ImportError`` fallbacks (and ``ModuleNotFoundError`` / bare
  ``except`` handlers)
* ``TYPE_CHECKING`` guards
* imports performed lazily inside function bodies
* relative imports written with ``from . import ...``
"""

# Standard
from types import ModuleType
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union
import ast
import importlib
import os
import sys

# Local
from . import constants
from .log import log

## Public ######################################################################


def track_module(
    module_name: str,
    package_name: Optional[str] = None,
    submodules: Union[List[str], bool] = False,
    track_import_stack: bool = False,
    full_depth: bool = False,
    detect_transitive: bool = False,
    show_optional: bool = False,
) -> Union[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Track the dependencies of a single python module

    Args:
        module_name:  str
            The name of the module to track (may be relative if package_name
            provided)
        package_name:  Optional[str]
            The parent package name of the module if the module name is relative
        submodules:  Union[List[str], bool]
            If True, all submodules of the given module will also be tracked.
            If given as a list of strings, only those submodules will be
            tracked. If False, only the named module will be tracked.
        track_import_stack:  bool
            Store the stacks of modules causing each dependency of each tracked
            module for debugging purposes.
        full_depth:  bool
            Include transitive dependencies of the third party dependencies
            that are direct dependencies of modules within the target module's
            parent library.
        detect_transitive:  bool
            Detect whether each dependency is 'direct' or 'transitive'
        show_optional:  bool
            Show whether each requirement is optional (behind a conditional
            construct) or not

    Returns:
        import_mapping:  Union[Dict[str, List[str]], Dict[str, Dict[str, Any]]]
            The mapping from fully-qualified module name to the set of imports
            needed by the given module. If tracking import stacks or detecting
            direct vs transitive dependencies, the output schema is
            Dict[str, Dict[str, Any]] where the nested dicts hold "stack"
            and/or "type" keys respectively. If neither feature is enabled, the
            schema is Dict[str, List[str]].
    """
    module_deps_map, full_module_name = _collect_module_deps(
        module_name,
        package_name=package_name,
        submodules=submodules,
        full_depth=full_depth,
    )
    return _format_module_deps(
        full_module_name,
        module_deps_map,
        submodules=submodules,
        track_import_stack=track_import_stack,
        detect_transitive=detect_transitive,
        show_optional=show_optional,
    )


def build_report(
    module_name: str,
    package_name: Optional[str] = None,
    submodules: Union[List[str], bool] = False,
    full_depth: bool = False,
) -> Dict[str, Dict[str, List[str]]]:
    """Build the two-column report data for a module

    The returned mapping has one entry per reported module. Each value is a
    dict holding a sorted, de-duplicated list of ``direct`` dependency root
    package names and a sorted, de-duplicated list of ``conditional``
    dependency root package names (imports behind try/except ImportError
    fallbacks, TYPE_CHECKING guards or function bodies). Conditional imports
    participate in the dependency graph but never pollute the direct set, so
    a missing optional dependency cannot trigger a false cycle report.
    """
    imported = importlib.import_module(module_name, package=package_name)
    full_module_name = imported.__name__

    module_deps_map, _ = _collect_module_deps(
        module_name,
        package_name=package_name,
        submodules=submodules,
        full_depth=full_depth,
    )

    output_mods = _get_output_mods(full_module_name, module_deps_map, submodules)
    parent_direct_deps = _find_parent_direct_deps(module_deps_map)

    report = {}
    for mod in output_mods:
        deps, optional_mapping = _flatten_deps(
            mod, module_deps_map, parent_direct_deps
        )
        direct = sorted(
            dep for dep, optional in optional_mapping.items() if not optional
        )
        conditional = sorted(
            dep for dep, optional in optional_mapping.items() if optional
        )
        report[mod] = {"direct": direct, "conditional": conditional}
    log.debug("Report output: %s", report)
    return report


def detect_cycles(
    module_name: str,
    package_name: Optional[str] = None,
    submodules: Union[List[str], bool] = False,
    full_depth: bool = False,
) -> Dict[str, List[List[str]]]:
    """Detect suspected import cycles within the tracked library.

    Only unconditional (required) edges between modules of the tracked root
    package are considered. Conditional imports (try/except ImportError,
    TYPE_CHECKING guards, function bodies) are excluded because a missing
    optional dependency must never be reported as a cycle.

    Returns a mapping of module name -> a list of cycle paths (each a list of
    module names) in which that module participates.
    """
    module_deps_map, full_module_name = _collect_module_deps(
        module_name,
        package_name=package_name,
        submodules=submodules,
        full_depth=full_depth,
    )
    root_pkg = full_module_name.partition(".")[0]

    # Build an adjacency view of unconditional intra-package edges
    adjacency: Dict[str, List[str]] = {}
    for mod, deps in module_deps_map.items():
        if not mod.startswith(root_pkg):
            continue
        neighbors = sorted(
            dep
            for dep, kinds in deps.items()
            if not kinds and dep.startswith(root_pkg)
        )
        adjacency[mod] = neighbors

    cycles: Dict[str, List[List[str]]] = {}

    def _record(cycle):
        normalized = list(cycle)
        for node in normalized:
            existing = cycles.setdefault(node, [])
            if normalized not in existing:
                existing.append(normalized)

    # Depth-first search with an explicit stack state, recording back edges
    color = {}  # 0=white, 1=gray (on current path), 2=black
    stack_state: List[Tuple[str, int]] = []

    def _dfs(start):
        color[start] = 1
        stack_state.append((start, 0))
        path = [start]
        while stack_state:
            node, next_idx = stack_state[-1]
            neighbors = adjacency.get(node, [])
            if next_idx < len(neighbors):
                neighbor = neighbors[next_idx]
                stack_state[-1] = (node, next_idx + 1)
                state = color.get(neighbor, 0)
                if state == 1:
                    cycle_start = path.index(neighbor)
                    _record(path[cycle_start:] + [neighbor])
                elif state == 0:
                    color[neighbor] = 1
                    path.append(neighbor)
                    stack_state.append((neighbor, 0))
            else:
                color[node] = 2
                stack_state.pop()
                if path and path[-1] == node:
                    path.pop()

    for mod in sorted(adjacency):
        if color.get(mod, 0) == 0:
            _dfs(mod)
    return cycles


def render_html_report(
    report: Dict[str, Dict[str, List[str]]],
    title: str = "Import Tracker Report",
    cycles: Optional[Dict[str, List[List[str]]]] = None,
) -> str:
    """Render the two-column (direct / conditional) report as a standalone HTML
    page that can be opened directly in a browser. Modules participating in a
    suspected cycle are annotated.
    """
    cycles = cycles or {}

    def _esc(value):
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _dep_list(items, mod_name, css_class):
        if not items:
            return '<ul class="deps empty"><li class="empty">&mdash;</li></ul>'
        rendered = []
        for dep in items:
            cycle_hint = ""
            if mod_name in cycles:
                cycle_hint = (
                    ' <span class="cycle-badge" title="%s">&#9888; cycle</span>'
                    % _esc(
                        "; ".join(" -> ".join(c) for c in cycles[mod_name])
                    )
                )
            rendered.append(
                f'<li class="dep {css_class}">{_esc(dep)}{cycle_hint}</li>'
            )
        return '<ul class="deps">%s</ul>' % "".join(rendered)

    rows = []
    for mod_name in sorted(report):
        cols = report[mod_name]
        in_cycle = mod_name in cycles
        row_cls = "module cycle" if in_cycle else "module"
        cycle_marker = (
            ' <span class="cycle-badge">suspected cycle</span>'
            if in_cycle
            else ""
        )
        rows.append(
            f"""
        <tr class="{row_cls}">
          <th class="modname">{_esc(mod_name)}{cycle_marker}</th>
          <td class="col direct">{_dep_list(cols.get('direct', []), mod_name, 'direct')}</td>
          <td class="col conditional">{_dep_list(cols.get('conditional', []), mod_name, 'conditional')}</td>
        </tr>"""
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(title)}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 2rem; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 1.4rem; }}
  .legend {{ color: #555; font-size: 0.9rem; margin-bottom: 1rem; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff;
           box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  th, td {{ border: 1px solid #e3e3e3; padding: 0.55rem 0.8rem;
            text-align: left; vertical-align: top; }}
  thead th {{ background: #2d3748; color: #fff; position: sticky; top: 0; }}
  .modname {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
              white-space: nowrap; background: #f4f6f8; }}
  tr.cycle .modname {{ background: #fff3f3; }}
  ul.deps {{ list-style: none; margin: 0; padding: 0; }}
  li.dep {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 0.9rem; padding: 0.1rem 0; }}
  li.empty {{ color: #aaa; font-family: inherit; }}
  colgroup .c-direct {{ width: 38%; }}
  colgroup .c-cond {{ width: 38%; }}
  colgroup .c-name {{ width: 24%; }}
  .cycle-badge {{ display: inline-block; margin-left: 0.4rem; padding: 0 0.45rem;
                  border-radius: 999px; font-size: 0.72rem; font-weight: 600;
                  background: #fed7d7; color: #9b2c2c; cursor: help;
                  font-family: inherit; vertical-align: middle; }}
</style>
</head>
<body>
<h1>{_esc(title)}</h1>
<p class="legend">Direct dependencies import unconditionally at module import
time. Conditional imports sit behind an <code>except ImportError</code> fallback,
a <code>TYPE_CHECKING</code> guard, or a function body. Rows flagged
&#9888; participate in a suspected unconditional import cycle.</p>
<table>
<colgroup>
  <col class="c-name"><col class="c-direct"><col class="c-cond">
</colgroup>
<thead>
<tr><th>Module</th><th>Direct Dependencies</th>
<th>Conditional Imports</th></tr>
</thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
</body>
</html>
"""


def write_html_report(
    module_name: str,
    output_path: str,
    package_name: Optional[str] = None,
    submodules: Union[List[str], bool] = False,
    full_depth: bool = False,
    title: Optional[str] = None,
) -> Dict[str, Dict[str, List[str]]]:
    """Build the report, detect cycles and write a standalone HTML file"""
    report = build_report(
        module_name,
        package_name=package_name,
        submodules=submodules,
        full_depth=full_depth,
    )
    cycles = detect_cycles(
        module_name,
        package_name=package_name,
        submodules=submodules,
        full_depth=full_depth,
    )
    html = render_html_report(
        report,
        title=title or f"Import Tracker Report: {module_name}",
        cycles=cycles,
    )
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    return report



def _format_module_deps(
    module_name: str,
    module_deps_map: Dict[str, Dict[str, Set[str]]],
    submodules: Union[List[str], bool] = False,
    track_import_stack: bool = False,
    detect_transitive: bool = False,
    show_optional: bool = False,
) -> Union[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """Flatten the collected dependency graph into the public output schema"""
    full_module_name = module_name
    output_mods = _get_output_mods(full_module_name, module_deps_map, submodules)

    parent_direct_deps = _find_parent_direct_deps(module_deps_map)
    flattened_deps = {
        mod: _flatten_deps(mod, module_deps_map, parent_direct_deps)
        for mod in output_mods
    }
    log.debug("Raw output deps map: %s", flattened_deps)

    # If not displaying any of the extra info, the values are simple lists of
    # dependency names
    if not any([detect_transitive, track_import_stack, show_optional]):
        return {
            mod: list(sorted(deps.keys()))
            for mod, (deps, _) in flattened_deps.items()
        }

    # Otherwise, the values will be dicts with some combination of "type" and
    # "stack" populated
    deps_out = {mod: {} for mod in flattened_deps.keys()}

    # If detecting transitive deps, look through the stacks and mark each dep
    # as transitive or direct
    if detect_transitive:
        for mod, (deps, _) in flattened_deps.items():
            for dep_name, dep_stacks in deps.items():
                deps_out.setdefault(mod, {}).setdefault(dep_name, {})[
                    constants.INFO_TYPE
                ] = (
                    constants.TYPE_DIRECT
                    if any(len(dep_stack) == 1 for dep_stack in dep_stacks)
                    else constants.TYPE_TRANSITIVE
                )

    # If tracking import stacks, move them to the "stack" key in the output
    if track_import_stack:
        for mod, (deps, _) in flattened_deps.items():
            for dep_name, dep_stacks in deps.items():
                deps_out.setdefault(mod, {}).setdefault(dep_name, {})[
                    constants.INFO_STACK
                ] = dep_stacks

    # If showing optional, add the optional status of each dependency
    if show_optional:
        for mod, (deps, optional_mapping) in flattened_deps.items():
            for dep_name, dep_stacks in deps.items():
                deps_out.setdefault(mod, {}).setdefault(dep_name, {})[
                    constants.INFO_OPTIONAL
                ] = optional_mapping.get(dep_name, False)

    log.debug("Final output: %s", deps_out)
    return deps_out


def _get_output_mods(
    full_module_name: str,
    module_deps_map: Dict[str, Dict[str, Set[str]]],
    submodules: Union[List[str], bool],
) -> List[str]:
    """Determine the stable, sorted list of modules to include in the output"""
    output_mods = {full_module_name}
    if submodules:
        output_mods = output_mods.union(
            {
                mod
                for mod in module_deps_map
                if (
                    (submodules is True and mod.startswith(full_module_name))
                    or (submodules is not True and mod in submodules)
                )
            }
        )
    log.debug2("Output modules: %s", output_mods)
    return output_mods


def _collect_module_deps(
    module_name: str,
    package_name: Optional[str] = None,
    submodules: Union[List[str], bool] = False,
    full_depth: bool = False,
) -> Tuple[Dict[str, Dict[str, Set[str]]], str]:
    """Import the target module and walk the intra/inter-library dependency
    graph.

    Returns ``(module_deps_map, full_module_name)`` where the map is
    ``{module name: {dep name: set(edge kinds)}}``. Edge kinds are ``"optional"``
    (try/except ImportError fallback) and ``"lazy"`` (TYPE_CHECKING guard or
    function body); an empty set means an unconditional import.
    """
    log.debug("Importing %s.%s", package_name, module_name)
    imported = importlib.import_module(module_name, package=package_name)
    full_module_name = imported.__name__
    tracked_module_root_pkg = full_module_name.partition(".")[0]

    module_deps_map: Dict[str, Dict[str, Set[str]]] = {}
    modules_to_check = {imported}
    checked_modules: Set[ModuleType] = set()
    while modules_to_check:
        next_modules_to_check: Set[ModuleType] = set()
        for module_to_check in modules_to_check:

            # Figure out all imports from this module
            req_names, opt_names, lazy_names = _get_imports(module_to_check)
            log.debug3(
                "Import names for [%s] (req=%s, try_except=%s, lazy=%s)",
                module_to_check.__name__,
                req_names,
                opt_names,
                lazy_names,
            )

            # Trim to just non-standard modules
            non_std_required = _get_non_std_modules(req_names)
            non_std_optional = _get_non_std_modules(opt_names)
            non_std_lazy = _get_non_std_modules(lazy_names)
            # Optional (try/except) imports remain in the graph and are walked.
            # Lazy imports (TYPE_CHECKING/function body) are recorded as graph
            # edges so the dependency lists can show them, but are never walked
            # into third-party packages (they do not run at import time and
            # walking them pulls annotation-only packages into the result).
            walkable_names = non_std_required.union(non_std_optional)
            all_names = walkable_names.union(non_std_lazy)
            log.debug3("Non std module names: %s", all_names)

            # Conditional imports are part of the dependency graph but flagged
            # with their edge kind so they never get reported as hard, direct
            # requirements. The same dep may appear via multiple statements;
            # keep every kind that applies (module-name de-duplication happens
            # here, source order is irrelevant to the sets but output ordering
            # is always applied explicitly downstream).
            edge_kinds = {}
            for dep_name in all_names:
                kinds = set()
                if dep_name in non_std_optional:
                    kinds.add("optional")
                if dep_name in non_std_lazy:
                    kinds.add("lazy")
                edge_kinds[dep_name] = kinds
            module_deps_map[module_to_check.__name__] = edge_kinds
            log.debug2(
                "Deps for [%s] -> %s",
                module_to_check.__name__,
                all_names,
            )

            # Resolve the non-std modules that can be imported in this
            # environment so their own dependencies can be walked. A missing
            # optional dependency must never abort the walk.
            #
            # Required and try/except-optional edges are walked (the latter so
            # full_depth can discover the transitive deps behind an optional
            # package). Lazy edges (TYPE_CHECKING/function body) are walked
            # only into local modules: they never execute at import time and
            # traversing them into third-party packages would leak
            # annotation-only imports into the report.
            for dep_name in non_std_required.union(non_std_optional):
                dep_mod = _safe_import_module(dep_name)
                if dep_mod is None:
                    continue
                is_local = (
                    dep_mod.__name__.partition(".")[0] == tracked_module_root_pkg
                )
                if dep_mod in checked_modules:
                    continue
                if full_depth or is_local:
                    next_modules_to_check.add(dep_mod)
            for dep_name in non_std_lazy:
                dep_mod = _safe_import_module(dep_name)
                if dep_mod is None:
                    continue
                is_local = (
                    dep_mod.__name__.partition(".")[0] == tracked_module_root_pkg
                )
                if is_local and dep_mod not in checked_modules:
                    next_modules_to_check.add(dep_mod)

            # Also check modules with intermediate names on the edges that are
            # actually walkable from this module. Every intermediate prefix is
            # looked up in sys.modules and only enqueued when the edge leading
            # to the full dep name is not a lazy edge (lazy chains must not
            # pull annotation-only packages into the graph traversal).
            walkable_edges = non_std_required.union(non_std_optional)
            parent_mods = set()
            for dep_name in walkable_edges:
                mod_name_parts = dep_name.split(".")
                for parent_mod_name in [
                    ".".join(mod_name_parts[: i + 1])
                    for i in range(len(mod_name_parts))
                ]:
                    parent_mod = sys.modules.get(parent_mod_name)
                    if parent_mod is None:
                        log.warning(
                            "Could not find parent module %s of %s",
                            parent_mod_name,
                            dep_name,
                        )
                        continue
                    parent_is_local = (
                        parent_mod.__name__.partition(".")[0]
                        == tracked_module_root_pkg
                    )
                    if parent_mod not in checked_modules and (
                        full_depth or parent_is_local
                    ):
                        parent_mods.add(parent_mod)
            next_modules_to_check = next_modules_to_check.union(parent_mods)

            checked_modules.add(module_to_check)

        log.debug3("Next modules to check: %s", next_modules_to_check)
        modules_to_check = next_modules_to_check

    log.debug3("Full module dep mapping: %s", module_deps_map)
    return module_deps_map, full_module_name


def _safe_import_module(module_name: str) -> Optional[ModuleType]:
    """Import the given module, returning None if it cannot be imported"""
    try:
        return importlib.import_module(module_name)
    except Exception as err:  # pylint: disable=broad-except
        log.debug2("Could not import [%s]: %s", module_name, err)
        return None


## Private #####################################################################


def _get_dylib_dir():
    """Different versions/builds of python manage different builtin libraries as
    "builtins" versus extensions. As such, we need some heuristics to try to
    find the base directory that holds shared objects from the standard
    library.
    """
    is_dylib = lambda x: x is not None and (x.endswith(".so") or x.endswith(".dylib"))
    all_mod_paths = list(
        filter(is_dylib, (getattr(mod, "__file__", "") for mod in sys.modules.values()))
    )
    sample_dylib = all_mod_paths[0] if all_mod_paths else None
    if sample_dylib is None:  # pragma: no cover
        for lib_name in ["cmath"]:
            lib = importlib.import_module(lib_name)
            fname = getattr(lib, "__file__", None)
            if is_dylib(fname):
                sample_dylib = fname
                break

    return (
        os.path.realpath(os.path.dirname(sample_dylib))
        if sample_dylib is not None
        else "BADPATH"
    )


# The path where global modules are found
_std_lib_dir = os.path.realpath(os.path.dirname(os.__file__))
_std_dylib_dir = _get_dylib_dir()
_known_std_pkgs = [
    "collections",
]


def _mod_defined_in_init_file(mod: ModuleType) -> bool:
    """Determine if the given module is defined in an __init__.py[c]"""
    mod_file = getattr(mod, "__file__", None)
    if mod_file is None:
        return False
    return os.path.splitext(os.path.basename(mod_file))[0] == "__init__"


def _get_import_parent_path(mod_name: str) -> str:
    """Get the parent directory of the given module"""
    mod = sys.modules[mod_name]  # NOTE: Intentionally unsafe to raise if not there!

    # Some standard libs have no __file__ attribute
    file_path = getattr(mod, "__file__", None)
    if file_path is None:
        return _std_lib_dir

    # If the module comes from an __init__, we need to pop two levels off
    if _mod_defined_in_init_file(mod):
        file_path = os.path.dirname(file_path)
    return os.path.dirname(file_path)


def _is_third_party(mod_name: str) -> bool:
    """Detect whether the given module is a third party (non-standard and not
    import_tracker)"""
    mod_pkg = mod_name.partition(".")[0]
    return (
        not mod_name.startswith("_")
        and (
            mod_name not in sys.modules
            or _get_import_parent_path(mod_name) not in [_std_lib_dir, _std_dylib_dir]
        )
        and mod_pkg != constants.THIS_PACKAGE
        and mod_pkg not in _known_std_pkgs
    )


def _get_non_std_modules(mod_names: Iterable[str]) -> Set[str]:
    """Take a snapshot of the non-standard modules in the given names"""
    return {mod_name for mod_name in mod_names if _is_third_party(mod_name)}


def _get_module_source(mod: ModuleType) -> Optional[str]:
    """Read the python source for the given module, or return None if it
    cannot be read (e.g. compiled extensions or namespace packages).
    """
    mod_file = getattr(mod, "__file__", None)
    if mod_file is None:
        loader = getattr(mod, "__loader__", None) or getattr(
            getattr(mod, "__spec__", None), "loader", None
        )
        loader_name = getattr(loader, "name", "")
        if loader is None or "namespace" in loader_name.lower():
            return None
        try:
            return loader.get_source(mod.__name__)
        except (AttributeError, ImportError, OSError):
            return None
    try:
        with open(mod_file, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _get_imports(
    mod: ModuleType,
) -> Tuple[Set[str], Set[str], Set[str]]:
    """Get the sets of import names for the given module by parsing its AST.

    The three returned sets are:

    required:
        Imports that always run at module import time
    try_except:
        Imports guarded by a ``try``/``except ImportError`` fallback
    lazy:
        Imports that never run at module import time (TYPE_CHECKING guards and
        imports performed inside function bodies)

    Names are de-duplicated by module name and keep source order within each
    bucket. Sets are returned only to de-duplicate across the caller's
    operations; output ordering is always applied explicitly.
    """
    log.debug2("Getting imports for %s", mod.__name__)
    source = _get_module_source(mod)
    if source is None:
        log.warning("Couldn't find source for %s!", mod.__name__)
        return set(), set(), set()
    try:
        tree = ast.parse(source, filename=getattr(mod, "__file__", None) or mod.__name__)
    except SyntaxError as err:
        log.warning("Couldn't parse %s: %s", mod.__name__, err)
        return set(), set(), set()

    collector = _ImportCollector(mod)
    collector.visit(tree)
    log.debug3(
        "Collected imports for [%s]: required=%s try_except=%s lazy=%s",
        mod.__name__,
        collector.required,
        collector.try_except,
        collector.lazy,
    )
    return set(collector.required), set(collector.try_except), set(collector.lazy)


## AST Import Discovery #########################################################


class _ImportCollector(ast.NodeVisitor):
    """AST visitor collecting imports split by whether they unconditionally run
    at module import time or are guarded by a conditional construct.

    Conditional constructs are:

    * ``try`` blocks guarded by an ``except`` that can catch ``ImportError``
      (this includes ``ModuleNotFoundError`` and bare ``except:``)
    * ``if`` guards referencing ``TYPE_CHECKING``
    * any function or async-function body (lazy / deferred imports)

    Imports within ``finally`` blocks or ``else`` blocks are treated as
    required, matching import semantics.
    """

    def __init__(self, mod: ModuleType):
        self.mod = mod
        self.required: List[str] = []
        self.try_except: List[str] = []
        self.lazy: List[str] = []
        # Frame kinds: "optional" (try/except ImportError), "lazy"
        # (TYPE_CHECKING or function body) or None (required scope)
        self._frames: List[Optional[str]] = []

    @property
    def _frame_kind(self) -> Optional[str]:
        # "lazy" is the strongest guard: a try/except ImportError nested inside
        # a function body is still lazy, and a TYPE_CHECKING block nested in a
        # try is never a runtime fallback
        if "lazy" in self._frames:
            return "lazy"
        if "optional" in self._frames:
            return "optional"
        return None

    def _record(self, names: List[str]) -> None:
        kind = self._frame_kind
        bucket = (
            self.required
            if kind is None
            else self.try_except
            if kind == "optional"
            else self.lazy
        )
        for name in names:
            if name and name not in bucket:
                bucket.append(name)

    def visit_Import(self, node: ast.Import) -> None:
        # Duplicate imports of the same module de-duplicate by module name and
        # keep the order in which they first appear (never set iteration order)
        self._record([alias.name for alias in node.names])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._record(_resolve_import_from(self.mod, node))
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        guarded = "optional" if _try_catches_import_error(node) else None
        self._visit_with(node.body, guarded)
        # else runs only when no exception was raised, so imports there inherit
        # the surrounding frame only
        self._visit_with(node.orelse, None)
        # finally always runs, so imports there are never guarded by the
        # except path even if the try itself catches ImportError
        self._visit_with(node.finalbody, None)
        # The except handlers themselves may perform fallback imports; those
        # are optional relative to the happy path
        for handler in node.handlers:
            self._visit_with([handler], "optional")

    def visit_If(self, node: ast.If) -> None:
        # ``if TYPE_CHECKING:`` -> body is conditional, else runs at runtime.
        # ``if not TYPE_CHECKING:`` -> body runs at runtime, else is
        # conditional.
        positive = _strip_not(node.test)
        negated = positive is not node.test
        if _is_type_checking_test(positive):
            self._visit_with(node.body, None if negated else "lazy")
            self._visit_with(node.orelse, "lazy" if negated else None)
        else:
            # An ordinary runtime conditional (platform checks, feature flags)
            # makes both branches lazy/conditional
            self._visit_with(node.body, "lazy")
            self._visit_with(node.orelse, "lazy")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_like(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_like(node)

    def _visit_function_like(self, node: ast.AST) -> None:
        # Defaults/decorators/annotations are evaluated eagerly in the scope
        # the function is defined in, so visit them in the current frame
        for default in getattr(node, "args", ast.arguments()).defaults:
            self.visit(default)
        for default in getattr(node, "args", ast.arguments()).kw_defaults:
            if default is not None:
                self.visit(default)
        for decorator in getattr(node, "decorator_list", []):
            self.visit(decorator)
        returns = getattr(node, "returns", None)
        if returns is not None:
            self.visit(returns)
        # The body only executes when the function is called: lazy import
        self._visit_with(node.body, "lazy")

    def _visit_with(
        self, body: Iterable[ast.AST], kind: Optional[str]
    ) -> None:
        self._frames.append(kind)
        for child in body:
            self.visit(child)
        self._frames.pop()


def _try_catches_import_error(node: ast.Try) -> bool:
    """Determine if a try node has a handler that would catch ImportError"""
    for handler in node.handlers:
        if handler.type is None:
            return True
        for exc_name in _flatten_exception_names(handler.type):
            if exc_name in ("ImportError", "ModuleNotFoundError"):
                return True
    return False


def _flatten_exception_names(node: Optional[ast.AST]) -> List[str]:
    """Extract dotted exception names from an except type (handles tuples)"""
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        names = []
        for elt in node.elts:
            names.extend(_flatten_exception_names(elt))
        return names
    return [_dotted_name(node)]


def _dotted_name(node: ast.AST) -> str:
    """Build the dotted name out of an Attribute/Name chain"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _strip_not(test: ast.AST) -> ast.AST:
    """Strip a leading ``not`` from an if-test, returning the inner node"""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return test.operand
    return test


def _is_type_checking_test(test: ast.AST) -> bool:
    """Determine if an if-test is a TYPE_CHECKING guard (any spelling of
    ``typing.TYPE_CHECKING`` / ``typing_extensions.TYPE_CHECKING`` or a bare
    ``TYPE_CHECKING`` name, including ``if not TYPE_CHECKING:`` negation).
    """
    if isinstance(test, ast.BoolOp):
        return any(_is_type_checking_test(value) for value in test.values)
    name = _dotted_name(test)
    return name == "TYPE_CHECKING" or name.endswith(".TYPE_CHECKING")


## Relative / Absolute Import Resolution ######################################


def _resolve_import_from(mod: ModuleType, node: ast.ImportFrom) -> List[str]:
    """Resolve the absolute module names imported by an ImportFrom node"""
    module = node.module or ""
    level = node.level or 0
    package_name = _derive_package_name(mod, level=level, module=module)

    if level == 0:
        absolute = module
    elif package_name is None:
        log.warning(
            "Could not derive absolute name for relative import in %s (level=%d, "
            "module=%s)",
            mod.__name__,
            level,
            module,
        )
        absolute = module
    elif not module:
        absolute = package_name
    else:
        absolute = f"{package_name}.{module}"

    names = []
    if any(alias.name == "*" for alias in node.names):
        # We cannot statically expand star-imports, so record the package
        # module itself (importing ``from pkg import *`` requires importing
        # ``pkg`` either way)
        if absolute:
            names.append(absolute)
        return names

    for alias in node.names:
        candidate = f"{absolute}.{alias.name}" if absolute else alias.name
        if _importable_name(candidate):
            names.append(candidate)
        elif absolute:
            names.append(absolute)
        else:
            names.append(alias.name)
    return names


def _importable_name(name: str) -> bool:
    """Determine whether the given dotted name refers to an importable module,
    either already loaded or present on disk.
    """
    if name in sys.modules:
        return True
    return _module_exists_on_disk(name)


def _module_exists_on_disk(name: str) -> bool:
    """Check whether a dotted module name can be found on disk under any
    sys.path entry (including as a namespace package).
    """
    parts = name.split(".")
    for path_entry in sys.path:
        if not path_entry or not os.path.isdir(path_entry):
            continue
        candidate = os.path.join(path_entry, *parts)
        if os.path.isdir(candidate) and any(
            os.path.exists(os.path.join(candidate, init_file))
            for init_file in ["__init__.py", "__init__.pyc"]
        ) or os.path.isdir(candidate):
            return True
        if os.path.isfile(candidate + ".py") or os.path.isfile(candidate + ".pyc"):
            return True
    return False


def _derive_package_name(
    mod: ModuleType, level: int = 0, module: str = ""
) -> Optional[str]:
    """Derive the absolute package name that a relative import resolves to.

    This first attempts the standard ``__package__`` / ``__name__`` derivation
    (the same rule the import system uses). If that information is unavailable
    (e.g. a script run directly where ``__package__ is None``), it falls back to
    scanning the filesystem from the module's location. Both paths must agree
    for the same file.
    """
    package = getattr(mod, "__package__", None)
    name = getattr(mod, "__name__", None)
    derived = _derive_package_from_attrs(
        name=name,
        package=package,
        defined_in_init=_mod_defined_in_init_file(mod),
        level=level,
        module=module,
    )
    if derived is not None:
        return derived

    log.debug2(
        "Falling back to path scan for relative import in %s (%s)",
        name,
        getattr(mod, "__file__", None),
    )
    return _derive_package_from_path(
        file_path=getattr(mod, "__file__", None),
        level=level,
        module=module,
    )


def _derive_package_from_attrs(
    name: Optional[str],
    package: Optional[str],
    defined_in_init: bool,
    level: int,
    module: str = "",
) -> Optional[str]:
    """Implement the standard relative-import anchor rules from __name__ and
    __package__. Returns None if the anchor cannot be derived.

    The import system always anchors a relative import at the module's
    ``__package__`` (which equals ``__name__`` for a package's __init__). From
    that anchor, ``level == 1`` resolves to the anchor itself and every extra
    dot walks one more parent up.
    """
    if level == 0:
        return package

    if package is not None:
        anchor = package
    elif defined_in_init and name:
        # A package __init__ with a missing __package__ can still use __name__
        anchor = name
    else:
        # A top-level script (__package__ is None) cannot derive an anchor
        return None
    if level == 1:
        return anchor
    parts = anchor.split(".")
    steps = level - 1
    if steps > len(parts):
        return None
    return ".".join(parts[:-steps]) if steps < len(parts) else ""


def _derive_package_from_path(
    file_path: Optional[str], level: int, module: str = ""
) -> Optional[str]:
    """Fallback relative-import resolver: locate the file on disk under a
    sys.path entry and reconstruct its absolute package name.

    When several sys.path entries could contain the file (e.g. the current
    working directory and a nested package directory are both on the path),
    the deepest match is used: that is the longest sys.path prefix and hence
    the real top-level package the import system would use.
    """
    if not file_path or not os.path.isfile(file_path):
        return None
    real_file = os.path.realpath(file_path)
    directory = os.path.dirname(real_file)

    candidates = []
    # sys.path includes the script's own directory when a file is run
    # directly (the __package__ is None case), which anchors the scan
    for path_entry in sys.path:
        if not path_entry:
            continue
        real_base = os.path.realpath(path_entry)
        try:
            rel = os.path.relpath(directory, real_base)
        except ValueError:
            continue
        if rel == "." or rel.startswith("..") or os.path.isabs(rel):
            continue
        package_parts = [part for part in rel.split(os.sep) if part]
        if package_parts:
            # Prefer the deepest (longest base path) match
            candidates.append((len(real_base), package_parts))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, package_parts = candidates[0]

    # The directory of the file is the __package__ of the module (for an
    # __init__ it is also __name__), so one dot resolves to the anchor itself
    # and every extra dot walks one parent up, matching
    # _derive_package_from_attrs for the same file.
    anchor = ".".join(package_parts)
    if level == 0:
        return anchor
    steps = level - 1
    if steps <= 0:
        return anchor
    if steps > len(package_parts):
        return None
    return ".".join(package_parts[:-steps]) if steps < len(package_parts) else ""



## Dependency Flattening ######################################################


def _find_parent_direct_deps(
    module_deps_map: Dict[str, Dict[str, Set[str]]]
) -> Dict[str, Dict[str, Dict[str, Set[str]]]]:
    """Construct a mapping for each module (e.g. foo.bar.baz) to a mapping of
    parent modules (e.g. [foo, foo.bar]) and the third-party imports that are
    directly imported in those parent modules but not declared by the child
    itself. The original ``module_deps_map`` is left untouched; injected deps
    are applied only during flattening.

    Shape: {mod_name: {parent_mod_name: {dep_name: edge_kinds}}}
    """

    parent_direct_deps: Dict[str, Dict[str, Dict[str, Set[str]]]] = {}
    for mod_name, mod_deps in module_deps_map.items():

        mod_base_name = mod_name.partition(".")[0]
        mod_name_parts = mod_name.split(".")
        for i in range(1, len(mod_name_parts)):
            parent_mod_name = ".".join(mod_name_parts[:i])
            parent_deps = module_deps_map.get(parent_mod_name, {})
            for dep, parent_dep_kinds in parent_deps.items():
                # Only unconditional parent deps are aggregated down to child
                # modules. Conditional imports (try/except ImportError,
                # TYPE_CHECKING, function body) belong only to the module that
                # declares them: injecting them would make an optional parent
                # dep look like a child dep and can fabricate cycles.
                if parent_dep_kinds:
                    continue
                # Only inject a parent's direct dep into a child that does not
                # already declare it. Intra-library deps are not injected.
                if not dep.startswith(mod_base_name) and dep not in mod_deps:
                    log.debug3(
                        "Adding direct-dependency of parent mod [%s] to [%s]: %s",
                        parent_mod_name,
                        mod_name,
                        dep,
                    )
                    parent_direct_deps.setdefault(mod_name, {}).setdefault(
                        parent_mod_name, {}
                    )[dep] = set(parent_dep_kinds)
    log.debug3("Parent direct dep map: %s", parent_direct_deps)
    return parent_direct_deps



def _build_injected_stack(parent_name: str, mod_path: List[str]) -> List[str]:
    """Build a dependency stack for a dep injected from ``parent_name`` into the
    module at ``mod_path``. The stack begins at the injecting parent (a direct
    importer of the dep) and continues down to the module being flattened.
    """
    return [parent_name] + list(mod_path)


def _flatten_deps(
    module_name: str,
    module_deps_map: Dict[str, Dict[str, Set[str]]],
    parent_direct_deps: Dict[str, Dict[str, Dict[str, Set[str]]]],
) -> Tuple[Dict[str, List[List[str]]], Dict[str, bool]]:
    """Flatten the names of all modules that the target module depends on"""

    def _edge_kinds(mod, dep):
        return module_deps_map.get(mod, {}).get(dep, set())

    def _is_lazy(mod, dep):
        return "lazy" in _edge_kinds(mod, dep)

    all_deps: Dict[str, List[List[str]]] = {}
    # Work items carry (module, path, under_optional). under_optional means the
    # path to this module already crosses a try/except-optional edge; every dep
    # found downstream is then recorded as optional too, but optional- or
    # lazy-only leaves beyond an unconditional chain are pruned by the walk
    # rules below.
    mods_to_check = {module_name: ([], False)}
    while mods_to_check:
        next_mods_to_check = {}
        for mod_to_check, (parent_path, under_optional) in mods_to_check.items():
            log.debug4("Checking mod %s (optional_path=%s)", mod_to_check, under_optional)
            mod_parents_direct_deps = parent_direct_deps.get(mod_to_check, {})
            mod_path = parent_path + [mod_to_check]
            mod_deps = set(module_deps_map.get(mod_to_check, []))

            injected_by_parent = {}
            for parent_name, parent_deps in mod_parents_direct_deps.items():
                for dep_name, kinds in parent_deps.items():
                    mod_deps.add(dep_name)
                    injected_by_parent.setdefault(dep_name, {})[parent_name] = kinds
            log.debug4(
                "Mod deps for %s at path %s: %s", mod_to_check, mod_path, mod_deps
            )

            # Walk only unconditional edges, except that the first optional
            # edge reached from an unconditional chain is expanded so the
            # optional package itself and its own unconditional deps are
            # attributed. Lazy edges are never expanded.
            def _walk_decision(dep):
                kinds = _edge_kinds(mod_to_check, dep)
                injected_kinds = set()
                for kin in injected_by_parent.get(dep, {}).values():
                    injected_kinds.update(kin)
                is_lazy = "lazy" in kinds or "lazy" in injected_kinds
                is_optional = "optional" in kinds or "optional" in injected_kinds
                if is_lazy:
                    return False
                if is_optional:
                    return not under_optional
                return True

            for dep in mod_deps:
                if dep not in all_deps and _walk_decision(dep):
                    kinds = _edge_kinds(mod_to_check, dep)
                    injected_opt = any(
                        "optional" in kin
                        for kin in injected_by_parent.get(dep, {}).values()
                    )
                    next_optional = under_optional or bool(
                        "optional" in kinds or injected_opt
                    )
                    next_mods_to_check[dep] = (mod_path, next_optional)
            for mod_dep in mod_deps:
                mod_dep_direct_parents = {}
                for mod_parent, mod_parent_direct_deps in (
                    mod_parents_direct_deps.items()
                ):
                    if mod_dep in mod_parent_direct_deps:
                        log.debug4(
                            "Found direct parent dep for [%s] from parent [%s] "
                            "and dep [%s]",
                            mod_to_check,
                            mod_parent,
                            mod_dep,
                        )
                        mod_dep_direct_parents[mod_parent] = [
                            mod_parent
                        ] in all_deps.get(mod_dep, [])
                own_declared = mod_dep in module_deps_map.get(mod_to_check, {})
                if mod_dep_direct_parents:
                    for (
                        mod_dep_direct_parent,
                        already_present,
                    ) in mod_dep_direct_parents.items():
                        if not already_present:
                            injected_stack = _build_injected_stack(
                                mod_dep_direct_parent, mod_path
                            )
                            if injected_stack not in all_deps.setdefault(
                                mod_dep, []
                            ):
                                all_deps[mod_dep].append(injected_stack)
                    if own_declared:
                        all_deps.setdefault(mod_dep, []).append(mod_path)
                else:
                    kinds_here = _edge_kinds(mod_to_check, mod_dep)
                    # Lazy deps are recorded only on the flatten root
                    if "lazy" in kinds_here and mod_to_check != module_name:
                        continue
                    # A try/except-optional edge reached on an unconditional
                    # chain is recorded only on the module that declares it as
                    # its own direct dep (the flatten root). Nested optional
                    # leaves of packages reached unconditionally are pruned so
                    # a missing optional dep cannot leak (or fake a cycle). On
                    # an already-optional path, the edge is recorded (and
                    # stays optional) so an optional package keeps its own
                    # transitive deps.
                    if (
                        "optional" in kinds_here
                        and not under_optional
                        and mod_to_check != module_name
                    ):
                        continue
                    all_deps.setdefault(mod_dep, []).append(mod_path)
        log.debug3("Next mods to check: %s", next_mods_to_check)
        mods_to_check = next_mods_to_check



    # Create the flattened dependencies with the source lists for each
    mod_base_name = module_name.partition(".")[0]
    flat_base_deps: Dict[str, List[List[str]]] = {}
    optional_deps_map: Dict[str, List] = {}
    for dep, dep_sources in all_deps.items():
        if not dep.startswith(mod_base_name):
            dep_root_mod_name = dep.partition(".")[0]
            flat_dep_sources = flat_base_deps.setdefault(dep_root_mod_name, [])
            opt_dep_values = optional_deps_map.setdefault(dep_root_mod_name, [])
            for dep_source in dep_sources:
                log.debug4("Considering dep source list for %s: %s", dep, dep_source)

                # If any link in the dep_source is optional, the whole
                # dep_source should be considered optional
                is_optional = False
                for parent_idx, dep_mod in enumerate(dep_source[1:] + [dep]):
                    dep_parent = dep_source[parent_idx]
                    log.debug4(
                        "Checking whether [%s -> %s] is optional (dep=%s)",
                        dep_parent,
                        dep_mod,
                        dep_root_mod_name,
                    )
                    edge_kinds = module_deps_map.get(dep_parent, {}).get(
                        dep_mod, set()
                    )
                    if edge_kinds:
                        log.debug4(
                            "Found conditional link %s -> %s (%s)",
                            dep_parent,
                            dep_mod,
                            edge_kinds,
                        )
                        is_optional = True
                        break
                opt_dep_values.append(
                    [
                        is_optional,
                        dep_source,
                    ]
                )

                flat_dep_source = dep_source
                if dep_root_mod_name in dep_source:
                    flat_dep_source = dep_source[: dep_source.index(dep_root_mod_name)]
                if flat_dep_source not in flat_dep_sources:
                    flat_dep_sources.append(flat_dep_source)
    log.debug3("Optional deps map for [%s]: %s", module_name, optional_deps_map)
    optional_deps_map = {
        mod: all([opt_val[0] for opt_val in opt_vals])
        for mod, opt_vals in optional_deps_map.items()
    }
    return flat_base_deps, optional_deps_map
