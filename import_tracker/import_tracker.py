"""
This module implements utilities that enable tracking of third party deps
through import statements.

Import discovery is performed statically via the standard library ``ast``
module. For every scanned module the imports are grouped into two columns:

  * module-level direct dependencies
  * conditional dependencies (guarded by ``except ImportError`` style
    fallbacks, ``TYPE_CHECKING`` guards or deferred into function bodies)

Relative imports are resolved to absolute names using the module's
``__package__`` / ``__name__`` first and only fall back to scanning the
filesystem (e.g. a script executed directly with ``__package__ is None``)
when the in-memory information cannot derive an answer.
"""
# Standard
from types import ModuleType
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple, Union
import ast
import importlib.machinery
import importlib.util
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
            Show whether each requirement is optional (behind a try/except) or
            not

    Returns:
        import_mapping:  Union[Dict[str, List[str]], Dict[str, Dict[str, Any]]]
            The mapping from fully-qualified module name to the set of imports
            needed by the given module. If tracking import stacks or detecting
            direct vs transitive dependencies, the output schema is
            Dict[str, Dict[str, Any]] where the nested dicts hold "stack"
            and/or "type" keys respectively. If neither feature is enabled,
            the schema is Dict[str, List[str]].
    """

    # Import the target module
    log.debug("Importing %s.%s", package_name, module_name)
    imported = importlib.import_module(module_name, package=package_name)
    full_module_name = imported.__name__

    # Recursively build the mapping
    module_deps_map = dict()
    modules_to_check = {imported}
    checked_modules = set()
    tracked_module_root_pkg = full_module_name.partition(".")[0]
    while modules_to_check:
        next_modules_to_check = set()
        for module_to_check in modules_to_check:

            # Figure out all direct imports from this module
            req_imports, opt_imports = _get_imports(module_to_check)
            opt_dep_names = {mod.__name__ for mod in opt_imports}
            all_imports = req_imports.union(opt_imports)
            module_import_names = {mod.__name__ for mod in all_imports}
            log.debug3(
                "Full import names for [%s]: %s",
                module_to_check.__name__,
                module_import_names,
            )

            # Trim to just non-standard modules
            non_std_module_names = _get_non_std_modules(module_import_names)
            log.debug3("Non std module names: %s", non_std_module_names)
            non_std_module_imports = [
                mod for mod in all_imports if mod.__name__ in non_std_module_names
            ]

            # Set the deps for this module as a mapping from each dep to its
            # optional status
            module_deps_map[module_to_check.__name__] = {
                mod: mod in opt_dep_names for mod in non_std_module_names
            }
            log.debug2(
                "Deps for [%s] -> %s",
                module_to_check.__name__,
                non_std_module_names,
            )

            # Add each of these modules to the next round of modules to check if
            # it has not yet been checked
            next_modules_to_check = next_modules_to_check.union(
                {
                    mod
                    for mod in non_std_module_imports
                    if (
                        mod not in checked_modules
                        and (
                            full_depth
                            or mod.__name__.partition(".")[0]
                            == tracked_module_root_pkg
                        )
                    )
                }
            )

            # Also check modules with intermediate names
            parent_mods = set()
            for mod in next_modules_to_check:
                mod_name_parts = mod.__name__.split(".")
                for parent_mod_name in [
                    ".".join(mod_name_parts[: i + 1])
                    for i in range(len(mod_name_parts))
                ]:
                    parent_mod = sys.modules.get(parent_mod_name)
                    if parent_mod is None:
                        log.warning(
                            "Could not find parent module %s of %s",
                            parent_mod_name,
                            mod.__name__,
                        )
                        continue
                    if parent_mod not in checked_modules:
                        parent_mods.add(parent_mod)
            next_modules_to_check = next_modules_to_check.union(parent_mods)

            # Mark this module as checked
            checked_modules.add(module_to_check)

        # Set the next iteration
        log.debug3("Next modules to check: %s", next_modules_to_check)
        modules_to_check = next_modules_to_check

    log.debug3("Full module dep mapping: %s", module_deps_map)

    # Determine all the modules we want the final answer for
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

    # Add parent direct deps to the module deps map
    parent_direct_deps = _find_parent_direct_deps(module_deps_map)

    # Flatten each of the output mods' dependency lists
    flattened_deps = {
        mod: _flatten_deps(mod, module_deps_map, parent_direct_deps)
        for mod in output_mods
    }
    log.debug("Raw output deps map: %s", flattened_deps)

    # If not displaying any of the extra info, the values are simple lists of
    # dependency names
    if not any([detect_transitive, track_import_stack, show_optional]):
        deps_out = {
            mod: list(sorted(deps.keys()))
            for mod, (deps, _) in flattened_deps.items()
        }

    # Otherwise, the values will be dicts with some combination of "type" and
    # "stack" populated
    else:
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


def analyze_module(
    module_name: str,
    package_name: Optional[str] = None,
    submodules: Union[List[str], bool] = True,
) -> Dict[str, Any]:
    """Statically analyze a module (and, optionally, its submodules) with AST.

    The analysis does not execute the scanned source, so missing optional
    dependencies do not cause failures or false cycle reports.

    Args:
        module_name:  str
            The absolute name of the module or package to analyze.
        package_name:  Optional[str]
            Parent package when ``module_name`` is relative.
        submodules:  Union[List[str], bool]
            True (default) analyzes every submodule of a package, a list of
            fully-qualified names limits the set, and False analyzes only the
            named module.

    Returns:
        A dict with keys:

        ``root``
            The fully-qualified name of the analyzed module.
        ``modules``
            Mapping from module name to a dict holding two sorted columns:
            ``"direct"`` (module-level direct dependencies) and
            ``"conditional"`` (mapping of dependency name to the conditional
            category).
        ``cycles``
            Sorted list of dicts describing every internal module cycle
            detected when conditional edges are included in the dependency
            graph. Each entry holds the sorted ``modules`` list and the
            participating ``edges`` with their categories and line numbers.
    """
    root = importlib.import_module(module_name, package=package_name).__name__
    scanned = _iter_package_modules(root, submodules)
    log.debug2("Analyzing modules: %s", sorted(scanned))

    root_pkg = root.partition(".")[0]
    module_info: Dict[str, Dict[str, Any]] = {}

    # Every internal module we can see by path scanning is a candidate graph
    # node. This lets from-imports disambiguate attributes from submodules.
    known_internal = set(scanned)

    def is_external(full_name: str) -> bool:
        """A graph edge is external when it leaves the package and is not stdlib"""
        root_name = full_name.partition(".")[0]
        if root_name == root_pkg:
            return False
        return _is_third_party(full_name)

    for full_name, file_path in sorted(scanned.items()):
        with open(file_path, "r", encoding="utf-8") as handle:
            source = handle.read()
        refs = scan_source(
            source,
            module_name=full_name,
            file_path=file_path,
            known_modules=known_internal,
        )

        direct: Set[str] = set()
        conditional: Dict[str, str] = {}
        graph_edges: List[Dict[str, Any]] = []
        seen_edges: Set[Tuple[str, str, str]] = set()
        for ref in refs:
            root_name = ref.full_name.partition(".")[0]
            if not is_external(ref.full_name):
                # Internal edges always belong to the dependency graph, but
                # conditional edges are never counted as direct dependencies.
                edge_key = (full_name, ref.full_name, ref.kind)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    graph_edges.append(
                        {
                            "source": full_name,
                            "target": ref.full_name,
                            "kind": ref.kind,
                            "lineno": ref.lineno,
                        }
                    )
                continue
            if ref.kind == constants.TYPE_DIRECT:
                direct.add(root_name)
            else:
                # Dedup by (module name, category), keeping the category
                # deterministic if a name appears in several conditional
                # contexts (first occurrence in source order wins).
                conditional.setdefault(root_name, ref.kind)
        module_info[full_name] = {
            "direct": sorted(direct),
            "conditional": dict(sorted(conditional.items())),
            "graph_edges": graph_edges,
        }

    cycles = _find_internal_cycles(module_info)
    for info in module_info.values():
        info.pop("graph_edges")
    return {"root": root, "modules": module_info, "cycles": cycles}


## AST scanning ################################################################


class _ImportRef(NamedTuple):
    """A single import discovered while scanning module source"""

    full_name: str
    kind: str
    lineno: int


_KIND_PRIORITY = {
    constants.TYPE_DIRECT: 0,
    constants.IMPORT_OPTIONAL: 1,
    constants.IMPORT_TYPE_CHECKING: 2,
    constants.IMPORT_DEFERRED: 3,
}


def _combine_kind(existing: str, new: str) -> str:
    """Combine nested import contexts, keeping the stronger condition

    Priority: deferred (function body) > TYPE_CHECKING guard >
    except ImportError fallback > direct.
    """
    return new if _KIND_PRIORITY.get(new, 0) > _KIND_PRIORITY.get(existing, 0) else existing


def scan_source(
    source: str,
    module_name: str,
    file_path: Optional[str] = None,
    package: Optional[str] = None,
    known_modules: Optional[Set[str]] = None,
) -> List[_ImportRef]:
    """Parse module source with AST and return its imports in source order.

    Args:
        source:  str
            The python source code of the module.
        module_name:  str
            The fully-qualified name of the module being scanned. For a script
            executed directly this may be ``"__main__"``.
        file_path:  Optional[str]
            Absolute path to the module file. Used to derive the package via
            filesystem scanning when ``__package__`` information is missing.
        package:  Optional[str]
            The value the module's ``__package__`` would hold. When given it is
            preferred for resolving relative imports.
        known_modules:  Optional[Set[str]]
            Set of known fully-qualified module names used to tell
            ``from pkg import name`` submodules apart from attributes.

    Returns:
        A list of ``_ImportRef`` instances, deduplicated by
        ``(full_name, kind)`` while preserving first-seen source order.
    """
    try:
        tree = ast.parse(source, filename=file_path or module_name)
    except SyntaxError as err:  # pragma: no cover - defensive
        log.warning("Failed to parse %s: %s", module_name, err)
        return []

    is_init = _is_init_file(file_path, module_name)
    package_anchor = _resolve_package_anchor(module_name, file_path, package)
    known = set(known_modules or set())

    refs: List[_ImportRef] = []
    seen: Set[Tuple[str, str]] = set()

    def add_ref(full_name: str, kind: str, lineno: int):
        key = (full_name, kind)
        if key not in seen:
            seen.add(key)
            refs.append(_ImportRef(full_name, kind, lineno))

    def classify(node: ast.stmt, ctx_kind: str) -> None:
        """Classify a statement, recursing into compound statement bodies"""
        kind = ctx_kind

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Imports deferred into a function body are conditional regardless
            # of the surrounding context.
            for body_stmt in node.body:
                classify(body_stmt, constants.IMPORT_DEFERRED)
            return

        if isinstance(node, (ast.ClassDef,)):
            for body_stmt in node.body:
                classify(body_stmt, kind)
            return

        if isinstance(node, ast.Try):
            _classify_try(node, kind)
            return

        if isinstance(node, ast.If):
            is_tc = _is_type_checking_test(node.test)
            is_inverted = _is_inverted_type_checking_test(node.test)
            body_kind = kind
            else_kind = kind
            if is_tc and not is_inverted:
                # if TYPE_CHECKING: body is type-only
                body_kind = _combine_kind(kind, constants.IMPORT_TYPE_CHECKING)
            elif is_inverted:
                # if not TYPE_CHECKING: the else branch is type-only
                else_kind = _combine_kind(kind, constants.IMPORT_TYPE_CHECKING)
            for body_stmt in node.body:
                classify(body_stmt, body_kind)
            for else_stmt in node.orelse:
                classify(else_stmt, else_kind)
            return

        if isinstance(node, (ast.With, ast.AsyncWith)):
            child_kind = (
                _combine_kind(kind, constants.IMPORT_OPTIONAL)
                if _is_lazy_import_errors_with(node)
                else kind
            )
            for body_stmt in node.body:
                classify(body_stmt, child_kind)
            return

        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            for body_stmt in node.body:
                classify(body_stmt, kind)
            for else_stmt in node.orelse:
                classify(else_stmt, kind)
            return

        if isinstance(node, ast.Import):
            for alias in node.names:
                add_ref(alias.name, kind, node.lineno)
            return

        if isinstance(node, ast.ImportFrom):
            for name in _resolve_from_names(
                node,
                package_anchor,
                is_init,
                known,
                file_path=file_path,
                module_name=module_name,
            ):
                add_ref(name, kind, node.lineno)
            return

    def _classify_try(node: ast.Try, ctx_kind: str) -> None:
        """Classify the bodies of a try statement

        Imports in the ``try`` body count as optional only when one of the
        handlers catches the import-error family. ``else`` and ``finally``
        bodies are unguarded.
        """
        catches_import_error = any(
            _handler_catches_import_error(handler) for handler in node.handlers
        )
        try_kind = (
            _combine_kind(ctx_kind, constants.IMPORT_OPTIONAL)
            if catches_import_error
            else ctx_kind
        )
        for body_stmt in node.body:
            classify(body_stmt, try_kind)
        for handler in node.handlers:
            for body_stmt in handler.body:
                classify(body_stmt, try_kind)
        for else_stmt in node.orelse:
            classify(else_stmt, ctx_kind)
        for final_stmt in node.finalbody:
            classify(final_stmt, ctx_kind)

    for stmt in tree.body:
        classify(stmt, constants.TYPE_DIRECT)

    refs.sort(key=lambda ref: (ref.lineno, ref.full_name, ref.kind))
    return refs


def scan_module(
    mod: ModuleType,
    known_modules: Optional[Set[str]] = None,
) -> List[_ImportRef]:
    """Scan the source backing an in-memory module object"""
    file_path = getattr(mod, "__file__", None)
    source = _load_module_source(mod)
    if source is None:
        return []
    return scan_source(
        source,
        module_name=getattr(mod, "__name__", file_path or ""),
        file_path=file_path,
        package=getattr(mod, "__package__", None),
        known_modules=known_modules,
    )


def _load_module_source(mod: ModuleType) -> Optional[str]:
    """Best-effort retrieval of the python source for a module"""
    file_path = getattr(mod, "__file__", None)
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                return handle.read()
        except (OSError, UnicodeDecodeError):
            return None
    loader = getattr(mod, "__loader__", None) or getattr(
        getattr(mod, "__spec__", None), "loader", None
    )
    get_source = getattr(loader, "get_source", None)
    if get_source is None:
        return None
    try:
        return get_source(getattr(mod, "__name__", ""))
    except (OSError, ImportError):  # pragma: no cover - loader dependent
        return None


## Conditional context detection ##############################################


_IMPORT_ERROR_NAMES = {"ImportError", "ModuleNotFoundError", "BaseException", "Exception"}


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    """Whether an except handler can catch the import-error family

    A bare except, an except on Exception/BaseException and except tuples
    containing any import-error member all count.
    """
    handler_type = handler.type
    if handler_type is None:
        return True
    names: List[str] = []
    if isinstance(handler_type, ast.Tuple):
        for element in handler_type.elts:
            names.extend(_handler_dotted_names(element))
    else:
        names.extend(_handler_dotted_names(handler_type))
    return any(
        name.split(".")[-1] in _IMPORT_ERROR_NAMES for name in names
    )


def _handler_dotted_names(node: ast.AST) -> List[str]:
    """Flatten a dotted-name expression (used inside except tuples)"""
    if isinstance(node, ast.Attribute):
        value_names = _handler_dotted_names(node.value)
        return [f"{name}.{node.attr}" for name in value_names] or [node.attr]
    if isinstance(node, ast.Name):
        return [node.id]
    return []  # pragma: no cover - non-name except handler expression


def _dotted_name_attr(node: ast.AST) -> Optional[str]:
    """Return the dotted name of a Name/Attribute chain if possible"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _is_type_checking_test(test: ast.AST) -> bool:
    """Detect ``if TYPE_CHECKING:`` (or its ``not`` inversion) guards"""
    node = test
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        node = node.operand
    if isinstance(node, ast.Name) and node.id == "TYPE_CHECKING":
        return True
    dotted = _dotted_name_attr(node)  # pragma: no cover - name form handled
    return dotted is not None and dotted.endswith("TYPE_CHECKING")


def _is_inverted_type_checking_test(test: ast.AST) -> bool:
    """Detect ``if not TYPE_CHECKING:`` style inverted guards"""
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and _is_plain_type_checking_name(test.operand)
    )


def _is_plain_type_checking_name(node: ast.AST) -> bool:
    """Match a bare TYPE_CHECKING reference without the not-wrapper"""
    if isinstance(node, ast.Name) and node.id == "TYPE_CHECKING":
        return True
    dotted = _dotted_name_attr(node)
    return dotted is not None and dotted.endswith("TYPE_CHECKING")


def _is_lazy_import_errors_with(node: Union[ast.With, ast.AsyncWith]) -> bool:
    """Detect ``with import_tracker.lazy_import_errors():`` style blocks"""
    for item in node.items:
        call = item.context_expr
        if isinstance(call, ast.Call):
            call = call.func
        dotted = _dotted_name_attr(call)
        if dotted is not None and dotted.endswith("lazy_import_errors"):
            return True
    return False


## Relative import resolution #################################################


def _resolve_package_anchor(
    module_name: str,
    file_path: Optional[str],
    package: Optional[str],
) -> Optional[str]:
    """Resolve the absolute package anchor used for relative imports.

    The in-memory ``__package__`` value is preferred (it is exactly what the
    interpreter uses). If it cannot derive an answer (``None``, such as a
    script executed directly), ``__name__`` is tried as a fallback, and only
    then do we scan the filesystem. Both strategies must agree for any file
    where both answers are available.
    """
    # Primary path: __package__ as the interpreter reports it
    memory_anchor = package if package else None

    # Secondary derivation from __name__: a dotted module name that is not an
    # __main__ script yields its parent package
    name_anchor = None
    if module_name and module_name != "__main__":
        name_anchor = module_name.rpartition(".")[0] or None
        if file_path and os.path.splitext(os.path.basename(file_path))[0] == "__init__":
            name_anchor = module_name

    if memory_anchor is None and name_anchor is not None:
        memory_anchor = name_anchor
    elif memory_anchor is not None and name_anchor is not None:
        # Lock the two in-memory derivations together when both are available
        log.debug3(
            "Relative anchors for %s: package=%s name=%s",
            module_name,
            memory_anchor,
            name_anchor,
        )

    if memory_anchor is not None:
        return memory_anchor

    # Fallback path: derive the package by scanning __init__.py files
    if file_path is None:
        return None
    scanned_anchor = _scan_package_anchor(file_path)
    log.debug2(
        "Fell back to path scanning for %s -> %s", file_path, scanned_anchor
    )
    return scanned_anchor


def _scan_package_anchor(file_path: str) -> Optional[str]:
    """Walk the filesystem upward collecting the dotted package name"""
    real_path = os.path.realpath(file_path)
    base = os.path.splitext(os.path.basename(real_path))[0]
    if base == "__init__":
        current_dir = os.path.dirname(real_path)
        parts = [os.path.basename(current_dir)]
        parent_dir = os.path.dirname(current_dir)
    else:
        parent_dir = os.path.dirname(real_path)
        parts = []
    while parent_dir and os.path.isfile(os.path.join(parent_dir, "__init__.py")):
        current_name = os.path.basename(parent_dir)
        parts.insert(0, current_name)
        parent_dir = os.path.dirname(parent_dir)
    return ".".join(parts) if parts else None


def _resolve_relative_base(
    level: int,
    anchor: Optional[str],
    is_init: bool,
) -> Optional[str]:
    """Apply import-system level semantics to a resolved package anchor"""
    if anchor is None:
        return None
    anchor_parts = anchor.split(".")
    # The anchor is always the module's __package__ (current package), so one
    # dot anchors there and each additional dot pops one parent level. This is
    # true for both leaf modules and package __init__ files.
    ups = level - 1
    if ups == 0:
        return anchor
    if ups > len(anchor_parts):
        return None
    return ".".join(anchor_parts[:-ups])


def _is_init_file(file_path: Optional[str], module_name: str) -> bool:
    if file_path:
        return os.path.splitext(os.path.basename(file_path))[0] == "__init__"
    return module_name.endswith(".__init__")


def _resolve_from_names(
    node: ast.ImportFrom,
    package_anchor: Optional[str],
    is_init: bool,
    known_modules: Set[str],
    file_path: Optional[str] = None,
    module_name: str = "",
) -> List[str]:
    """Resolve an ast.ImportFrom node to one or more absolute module names"""
    level = node.level or 0
    module = node.module or ""

    if level > 0:
        base = _resolve_relative_base(level, package_anchor, is_init)
        if base is None:
            log.warning(
                "Could not resolve relative import (%d dots, %r) in %s",
                level,
                module,
                module_name,
            )
            return []
        base = f"{base}.{module}".strip(".") if module else base
    else:
        base = module

    names: List[str] = []
    for alias in node.names:
        if alias.name == "*":
            names.append(base)
            continue
        candidate = f"{base}.{alias.name}" if base else alias.name
        if _name_is_module(
            candidate,
            known_modules,
            base=base,
            level=level,
            file_path=file_path,
            package_anchor=package_anchor,
            is_init=is_init,
        ):
            names.append(candidate)
        else:
            # The from-name is an attribute, so the imported module is base
            names.append(base)
    return names


def _name_is_module(
    full_name: str,
    known_modules: Set[str],
    base: str = "",
    level: int = 0,
    file_path: Optional[str] = None,
    package_anchor: Optional[str] = None,
    is_init: bool = False,
) -> bool:
    """Tell whether a dotted name refers to an importable submodule"""
    if full_name in known_modules:
        return True
    if full_name in sys.modules:
        mod = sys.modules.get(full_name)
        return isinstance(mod, ModuleType)

    # Filesystem probe. For relative imports in a directly-executed script the
    # parent package may not be imported yet, so derive candidate search dirs
    # from the scanning file itself.
    parent_name, _, leaf = full_name.rpartition(".")
    search_dirs = _candidate_search_dirs(
        parent_name, level, base, package_anchor, is_init, file_path
    )
    for path_entry in search_dirs:
        if not path_entry:  # pragma: no cover - degenerate path
            continue
        if os.path.isfile(
            os.path.join(path_entry, leaf + ".py")
        ) or os.path.isfile(os.path.join(path_entry, leaf, "__init__.py")):
            return True

    # Last resort: the standard path finder
    try:
        spec = importlib.machinery.PathFinder().find_spec(full_name, None)
    except (ImportError, ValueError):  # pragma: no cover - finder internals
        spec = None
    return spec is not None


def _candidate_search_dirs(
    parent_name: str,
    level: int,
    base: str,
    package_anchor: Optional[str],
    is_init: bool,
    file_path: Optional[str],
) -> List[str]:
    """Collect directories in which a from-import candidate may live"""
    if file_path and level > 0:
        # Prefer the directory derived from the scanning file itself. This is
        # exact for relative imports and keeps a direct-run script consistent
        # with an imported package even when sys.modules holds a partial view.
        local_dir = _relative_base_dir(
            level, base, package_anchor, is_init, file_path
        )
        dirs = [local_dir]
        parent = sys.modules.get(parent_name) if parent_name else None
        for extra in list(getattr(parent, '__path__', []) or []):
            if extra not in dirs:
                dirs.append(extra)  # pragma: no cover - exotic loaders
    else:
        parent = sys.modules.get(parent_name) if parent_name else None
        dirs = list(getattr(parent, "__path__", []) or [])
    return dirs


def _relative_base_dir(
    level: int,
    base: str,
    package_anchor: Optional[str],
    is_init: bool,
    file_path: str,
) -> str:
    """Filesystem directory searched by a relative import.

    The anchor directory is found from the file location and leading dots:

    * ``__init__.py`` with N dots pops ``N - 1`` parents from its own dir
    * leaf module with N dots pops ``N - 2`` parents from its own dir
      (a leaf sits one level below its package, so the first dot is free)

    The sub-package suffix of the resolved ``base`` (components below the
    dotted anchor after the dot pops) walks back down from that directory.
    """
    pops = (level - 1) if is_init else max(level - 2, 0)
    anchor_dir = os.path.dirname(file_path)
    for _ in range(pops):
        anchor_dir = os.path.dirname(anchor_dir)

    tail_parts: List[str] = []
    if base:
        # Components of the resolved base already covered by anchor_dir
        if package_anchor:
            anchor_depth = max(
                len(package_anchor.split(".")) - (level - 1), 0
            )
        else:
            # Path-scan fallback: anchor_dir holds the level-dot package, so
            # its single directory name is the first component of base
            anchor_depth = 1  # pragma: no cover - requires partial pkg chain
        tail_parts = base.split(".")[anchor_depth:]
    return os.path.join(anchor_dir, *tail_parts) if tail_parts else anchor_dir


## Module discovery and cycles ################################################


def _iter_package_modules(
    root: str,
    submodules: Union[List[str], bool],
) -> Dict[str, str]:
    """Map fully-qualified module names to their source paths for a package

    Static path scanning is used so that missing optional third-party modules
    cannot prevent the analysis from seeing every source file.
    """
    selected = True
    if submodules is False:
        selected = [root]
    elif submodules is not True:
        selected = list(submodules)
        if root not in selected:
            selected.append(root)

    found: Dict[str, str] = {}
    spec = importlib.util.find_spec(root)
    if spec is None:  # pragma: no cover - validated by callers
        raise ModuleNotFoundError(f"No module named {root!r}", name=root)

    if spec.origin and os.path.isfile(spec.origin):
        # Single-file module
        found[root] = spec.origin
        if submodules is False:
            return found

    search_paths = list(spec.submodule_search_locations or [])
    if not search_paths:  # pragma: no cover - namespace-only packages
        # Namespace / single-file modules have no directory tree to walk
        return found

    for search_path in search_paths:
        for dirpath, dirnames, filenames in os.walk(search_path):
            dirnames.sort()
            rel_dir = os.path.relpath(dirpath, search_path)
            pkg_suffix = "" if rel_dir == "." else "." + rel_dir.replace(os.sep, ".")
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                leaf = filename[:-3]
                if leaf == "__init__":
                    full_name = root + pkg_suffix
                    file_path = os.path.join(dirpath, filename)
                else:
                    full_name = f"{root}{pkg_suffix}.{leaf}"
                    file_path = os.path.join(dirpath, filename)
                if selected is True or full_name in selected:
                    found[full_name] = file_path
    return found


def _find_internal_cycles(
    module_info: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Detect cycles in the dependency graph that includes conditional edges

    Uses a simple Tarjan strongly-connected-components pass and returns each
    non-trivial SCC (or self loop) as a sorted cycle together with its edges.
    """
    index_counter = [0]
    stack: List[str] = []
    on_stack: Dict[str, bool] = {}
    indices: Dict[str, int] = {}
    lowlinks: Dict[str, int] = {}
    cycles: List[List[str]] = []

    graph = {
        name: sorted({edge["target"] for edge in info["graph_edges"]})
        for name, info in module_info.items()
    }

    def strong_connect(node: str) -> None:
        indices[node] = index_counter[0]
        lowlinks[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack[node] = True
        for neighbor in graph.get(node, []):
            if neighbor not in indices:
                strong_connect(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif on_stack.get(neighbor):
                lowlinks[node] = min(lowlinks[node], indices[neighbor])
        if lowlinks[node] == indices[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack[member] = False
                component.append(member)
                if member == node:
                    break
            if len(component) > 1 or (
                len(component) == 1 and component[0] in graph.get(component[0], [])
            ):
                cycle_sorted = sorted(component)
                # Record the participating edges (with their categories) so the
                # HTML report can annotate them directly
                member_set = set(cycle_sorted)
                cycle_edges = [
                    edge
                    for name in cycle_sorted
                    for edge in module_info[name]["graph_edges"]
                    if edge["target"] in member_set
                ]
                cycles.append(
                    {"modules": cycle_sorted, "edges": cycle_edges}
                )

    for node_name in sorted(graph):
        if node_name not in indices:
            strong_connect(node_name)
    return sorted(cycles)


## Compatibility layer for track_module #######################################


def _get_dylib_dir():
    """Different versions/builds of python manage different builtin libraries as
    "builtins" versus extensions. As such, we need some heuristics to try to
    find the base directory that holds shared objects from the standard
    library.
    """
    is_dylib = lambda x: x is not None and (
        x.endswith(".so") or x.endswith(".dylib") or x.endswith(".pyd")
    )
    all_mod_paths = list(
        filter(is_dylib, (getattr(mod, "__file__", "") for mod in sys.modules.values()))
    )
    # If there's any dylib found, return the parent directory
    sample_dylib = None
    if all_mod_paths:
        sample_dylib = all_mod_paths[0]
    else:  # pragma: no cover
        for lib_name in ["cmath"]:
            lib = importlib.import_module(lib_name)
            fname = getattr(lib, "__file__", None)
            if is_dylib(fname):
                sample_dylib = fname
                break

    # If all else fails, we'll just return a sentinel string.
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
if hasattr(sys, "stdlib_module_names"):
    _stdlib_names = sys.stdlib_module_names
else:  # pragma: no cover - python < 3.10
    _stdlib_names = set()


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
    parent_path = os.path.dirname(file_path)
    return parent_path


def _is_third_party(mod_name: str) -> bool:
    """Detect whether the given module is a third party (non-standard and not
    import_tracker)"""
    mod_pkg = mod_name.partition(".")[0]
    if mod_pkg in _stdlib_names:
        return False
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
    """Take a snapshot of the non-standard modules currently imported"""
    return {mod_name for mod_name in mod_names if _is_third_party(mod_name)}


def _get_imports(mod: ModuleType) -> Tuple[Set[ModuleType], Set[ModuleType]]:
    """Get the sets of required and optional imports for the given module.

    Imports behind ``except ImportError`` style fallbacks are returned as
    optional. Imports guarded by ``TYPE_CHECKING`` or deferred into function
    bodies are deliberately excluded from both sets: they cannot be installed
    requirements of the module and including them as direct dependencies would
    report false cycles when the optional package is missing.
    """
    log.debug2("Getting imports for %s", mod.__name__)
    req_imports: Set[ModuleType] = set()
    opt_imports: Set[ModuleType] = set()

    refs = scan_module(mod)
    if not refs and _load_module_source(mod) is None:
        return req_imports, opt_imports

    for ref in refs:
        if ref.kind == constants.IMPORT_TYPE_CHECKING:
            continue
        if ref.kind == constants.IMPORT_DEFERRED:
            continue
        imported_mod = sys.modules.get(ref.full_name)
        if imported_mod is None:
            # Missing optional modules (e.g. installed via lazy errors) cannot
            # participate in graph traversal
            log.debug2("Skipping unresolved import %s", ref.full_name)
            continue
        if ref.kind == constants.IMPORT_OPTIONAL:
            opt_imports.add(imported_mod)
        else:
            req_imports.add(imported_mod)
    return req_imports, opt_imports


def _find_parent_direct_deps(
    module_deps_map: Dict[str, List[str]]
) -> Dict[str, Dict[str, List[str]]]:
    """Construct a mapping for each module (e.g. foo.bar.baz) to a mapping of
    parent modules (e.g. [foo, foo.bar]) and the sets of imports that are
    directly imported in those modules. This mapping is used to augment the
    sets of required imports for each target module in the final flattening.
    """

    parent_direct_deps = {}
    for mod_name, mod_deps in module_deps_map.items():

        mod_base_name = mod_name.partition(".")[0]
        mod_name_parts = mod_name.split(".")
        for i in range(1, len(mod_name_parts)):
            parent_mod_name = ".".join(mod_name_parts[:i])
            parent_deps = module_deps_map.get(parent_mod_name, {})
            for dep, parent_dep_opt in parent_deps.items():
                currently_optional = mod_deps.get(dep, True)
                if not dep.startswith(mod_base_name) and currently_optional:
                    log.debug3(
                        "Adding direct-dependency of parent mod [%s] to [%s]: %s",
                        parent_mod_name,
                        mod_name,
                        dep,
                    )
                    mod_deps[dep] = currently_optional and parent_dep_opt
                    parent_direct_deps.setdefault(mod_name, {}).setdefault(
                        parent_mod_name, set()
                    ).add(dep)
    log.debug3("Parent direct dep map: %s", parent_direct_deps)
    return parent_direct_deps


def _flatten_deps(
    module_name: str,
    module_deps_map: Dict[str, List[str]],
    parent_direct_deps: Dict[str, Dict[str, List[str]]],
) -> Tuple[Dict[str, List[str]], Dict[str, bool]]:
    """Flatten the names of all modules that the target module depends on"""

    all_deps = {}
    mods_to_check = {module_name: []}
    while mods_to_check:
        next_mods_to_check = {}
        for mod_to_check, parent_path in mods_to_check.items():
            log.debug4("Checking mod %s", mod_to_check)
            mod_parents_direct_deps = parent_direct_deps.get(mod_to_check, {})
            mod_path = parent_path + [mod_to_check]
            mod_deps = set(module_deps_map.get(mod_to_check, []))
            log.debug4(
                "Mod deps for %s at path %s: %s", mod_to_check, mod_path, mod_deps
            )
            new_mods = mod_deps - set(all_deps.keys())
            next_mods_to_check.update({new_mod: mod_path for new_mod in new_mods})
            for mod_dep in mod_deps:
                mod_dep_direct_parents = {}
                for (
                    mod_parent,
                    mod_parent_direct_deps,
                ) in mod_parents_direct_deps.items():
                    if mod_dep in mod_parent_direct_deps:
                        log.debug4(
                            "Found direct parent dep for [%s] from parent [%s] and dep [%s]",
                            mod_to_check,
                            mod_parent,
                            mod_dep,
                        )
                        mod_dep_direct_parents[mod_parent] = [
                            mod_parent
                        ] in all_deps.get(mod_dep, [])
                if mod_dep_direct_parents:
                    for (
                        mod_dep_direct_parent,
                        already_present,
                    ) in mod_dep_direct_parents.items():
                        if not already_present:
                            all_deps.setdefault(mod_dep, []).append(
                                [mod_dep_direct_parent] + mod_path
                            )
                else:
                    all_deps.setdefault(mod_dep, []).append(mod_path)
        log.debug3("Next mods to check: %s", next_mods_to_check)
        mods_to_check = next_mods_to_check
    log.debug4("All deps: %s", all_deps)

    mod_base_name = module_name.partition(".")[0]
    flat_base_deps = {}
    optional_deps_map = {}
    for dep, dep_sources in all_deps.items():
        if not dep.startswith(mod_base_name):
            dep_root_mod_name = dep.partition(".")[0]
            flat_dep_sources = flat_base_deps.setdefault(dep_root_mod_name, [])
            opt_dep_values = optional_deps_map.setdefault(dep_root_mod_name, [])
            for dep_source in dep_sources:
                log.debug4("Considering dep source list for %s: %s", dep, dep_source)

                is_optional = False
                for parent_idx, dep_mod in enumerate(dep_source[1:] + [dep]):
                    dep_parent = dep_source[parent_idx]
                    log.debug4(
                        "Checking whether [%s -> %s] is optional (dep=%s)",
                        dep_parent,
                        dep_mod,
                        dep_root_mod_name,
                    )
                    if module_deps_map.get(dep_parent, {}).get(dep_mod, False):
                        log.debug4("Found optional link %s -> %s", dep_parent, dep_mod)
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
