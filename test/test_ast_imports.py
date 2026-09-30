"""
Tests for the AST-based import traversal and two-column aggregation in
import_tracker.import_tracker.
"""

# Standard
from types import ModuleType
import os
import subprocess
import sys

# Third Party
import pytest

# Local
from import_tracker import constants
from import_tracker.__main__ import main
from import_tracker.html_report import render_html_report
from import_tracker.import_tracker import (
    _scan_package_anchor,
    analyze_module,
    scan_module,
    scan_source,
    track_module,
)

## Four conditional shapes ####################################################


def test_except_import_error_is_conditional():
    """except ImportError fallback imports land in the conditional column and
    never in the direct dependency column, while try/finally stays direct."""
    analysis = analyze_module("ast_conditional_deps")
    optional_mod = analysis["modules"]["ast_conditional_deps.optional_mod"]
    assert optional_mod["direct"] == ["yaml"]
    assert optional_mod["conditional"] == {
        "alog": constants.IMPORT_OPTIONAL,
    }


def test_type_checking_guard_is_conditional():
    """Imports inside an if TYPE_CHECKING guard are conditional only"""
    analysis = analyze_module("ast_conditional_deps")
    type_mod = analysis["modules"]["ast_conditional_deps.type_checking_mod"]
    assert type_mod["direct"] == []
    assert type_mod["conditional"] == {
        "alog": constants.IMPORT_TYPE_CHECKING,
    }


def test_deferred_function_body_import_is_conditional():
    """Imports nested in function (and async function) bodies are deferred"""
    analysis = analyze_module("ast_conditional_deps")
    deferred = analysis["modules"]["ast_conditional_deps.deferred_mod"]
    assert deferred["direct"] == []
    assert deferred["conditional"] == {
        "yaml": constants.IMPORT_DEFERRED,
        "alog": constants.IMPORT_DEFERRED,
    }


def test_relative_from_imports_resolved():
    """from . / from .. relative imports resolve to absolute module names"""
    analysis = analyze_module("ast_conditional_deps")
    # All relative edges are internal, so they do not show in either external
    # column, but the analysis must not have failed to include the module set
    assert set(analysis["modules"]) >= {
        "ast_conditional_deps.optional_mod",
        "ast_conditional_deps.type_checking_mod",
        "ast_conditional_deps.deferred_mod",
        "ast_conditional_deps.subpkg.deep",
    }
    deep = analysis["modules"]["ast_conditional_deps.subpkg.deep"]
    assert deep["direct"] == []
    assert deep["conditional"] == {}


def test_conditional_imports_excluded_from_track_module_direct():
    """Conditional imports must not leak into the track_module dependency
    output as required deps, otherwise a missing optional package causes a
    false cycle"""
    # The conditional alog dep is shown as optional rather than a required
    # direct dependency, while the deferred/type-checking imports never appear
    # at all.
    mapping = track_module(
        "ast_conditional_deps", show_optional=True, submodules=True
    )
    assert mapping["ast_conditional_deps"]["alog"]["optional"] is True
    assert mapping["ast_conditional_deps"]["yaml"]["optional"] is False
    type_mod = mapping["ast_conditional_deps.type_checking_mod"]
    assert "alog" not in type_mod
    deferred = track_module("ast_conditional_deps.deferred_mod")
    assert deferred == {"ast_conditional_deps.deferred_mod": []}


## Missing optional dependencies ##############################################


def test_missing_optional_dep_does_not_falsely_break():
    """Optional deps that are not installed still show up in the conditional
    column and dependency graph listing, never in the direct column, and they
    must not trigger false cycles"""
    analysis = analyze_module("missing_optional_pkg")
    core = analysis["modules"]["missing_optional_pkg.core"]
    assert core["direct"] == []
    assert core["conditional"] == {
        "definitely_not_installed_xyz": constants.IMPORT_OPTIONAL,
        "also_not_installed_abc": constants.IMPORT_TYPE_CHECKING,
    }
    assert analysis["cycles"] == []

    # track_module must not require the missing package to be installed
    assert track_module("missing_optional_pkg") == {
        "missing_optional_pkg": []
    }


## Dedup and ordering #########################################################


def test_dedup_by_name_and_kind_with_stable_order(tmp_path):
    """Repeated imports of the same module collapse to one (name, category)
    entry and the result never depends on set iteration order"""
    source = "\n".join(
        [
            "import yaml",
            "import yaml",
            "try:",
            "    import alog",
            "except ImportError:",
            "    alog = None",
            "import os",
            "try:",
            "    import alog",
            "except ImportError:",
            "    pass",
            "",
        ]
    )
    refs_a = scan_source(source, "stable_mod")
    refs_b = scan_source(source, "stable_mod")
    names_kinds = [(ref.full_name, ref.kind) for ref in refs_a]
    assert names_kinds == [
        ("yaml", constants.TYPE_DIRECT),
        ("alog", constants.IMPORT_OPTIONAL),
        ("os", constants.TYPE_DIRECT),
    ]
    # Deterministic across runs and ordered by source position
    assert [(r.full_name, r.kind) for r in refs_b] == names_kinds


def test_analysis_columns_are_sorted():
    """The public analysis output must be stable and sorted"""
    analysis = analyze_module("ast_conditional_deps")
    for module_name, info in analysis["modules"].items():
        assert info["direct"] == sorted(info["direct"])
        assert list(info["conditional"]) == sorted(info["conditional"])
    assert list(analysis["modules"]) == sorted(analysis["modules"])


## Relative import resolution consistency #####################################


def test_memory_anchor_and_path_scan_agree_on_same_file():
    """The __package__/__name__ derivation and the path-scan fallback must
    resolve a relative import of the same file to the same absolute name"""
    sample_root = os.path.realpath("test/sample_libs")
    rel_file = os.path.join(
        sample_root, "ast_conditional_deps", "subpkg", "deep.py"
    )
    with open(rel_file, "r", encoding="utf-8") as handle:
        source = handle.read()

    # Primary path: in-memory __package__/__name__ information
    memory_refs = scan_source(
        source,
        module_name="ast_conditional_deps.subpkg.deep",
        file_path=rel_file,
        package="ast_conditional_deps.subpkg",
    )
    # Fallback path: no in-memory info at all (e.g. script executed directly)
    fallback_refs = scan_source(
        source,
        module_name="__main__",
        file_path=rel_file,
        package=None,
    )
    assert [
        (ref.full_name, ref.kind) for ref in memory_refs
    ] == [
        (ref.full_name, ref.kind) for ref in fallback_refs
    ]
    # And specifically the two-dot import must resolve to the parent package
    resolved = {ref.full_name for ref in fallback_refs}
    assert "ast_conditional_deps.optional_mod" in resolved
    assert "ast_conditional_deps.deferred_mod" in resolved


def test_scan_package_anchor_for_init_and_leaf():
    """Path scanning derives the package anchor for both __init__ and leaves"""
    sample_root = os.path.realpath("test/sample_libs")
    assert (
        _scan_package_anchor(
            os.path.join(sample_root, "ast_conditional_deps", "__init__.py")
        )
        == "ast_conditional_deps"
    )
    assert (
        _scan_package_anchor(
            os.path.join(
                sample_root, "ast_conditional_deps", "subpkg", "deep.py"
            )
        )
        == "ast_conditional_deps.subpkg"
    )


def test_direct_script_with_none_package(tmp_path):
    """A script executed directly (python path/to/script.py) has
    __package__ == None; relative imports must still resolve by path scanning
    """
    pkg_dir = tmp_path / "script_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "sibling.py").write_text("VALUE = 1\n", encoding="utf-8")
    script = pkg_dir / "tool.py"
    script.write_text(
        "from .sibling import VALUE\n"
        "def main():\n"
        "    from . import sibling\n"
        "    return sibling.VALUE\n",
        encoding="utf-8",
    )

    # Simulate the module object created for a directly executed script
    script_mod = ModuleType("__main__")
    script_mod.__file__ = str(script)
    script_mod.__package__ = None
    refs = scan_module(script_mod)
    resolved = [
        (ref.full_name, ref.kind)
        for ref in refs
        if ref.full_name.startswith("script_pkg")
    ]
    assert resolved == [
        ("script_pkg.sibling", constants.TYPE_DIRECT),
        ("script_pkg.sibling", constants.IMPORT_DEFERRED),
    ]

    # Execute the same script as a subprocess to verify the real direct-run
    # environment behaves the same
    runner = (
        "import runpy, sys; "
        f"sys.path.insert(0, r'{tmp_path}'); "
        "import import_tracker.import_tracker as it; "
        "from types import ModuleType; "
        "mod = ModuleType('__main__'); "
        f"mod.__file__ = r'{script}'; mod.__package__ = None; "
        "refs = it.scan_module(mod); "
        "print(sorted(set(r.full_name for r in refs)))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    out = subprocess.run(
        [sys.executable, "-c", runner],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "script_pkg.sibling" in out.stdout


def test_direct_script_subprocess_real_execution(tmp_path):
    """Executing a script with `python tool.py` leaves __package__ as None;
    path scanning must still resolve its relative imports"""
    pkg_dir = tmp_path / "ran_pkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "sibling.py").write_text("VALUE = 7\n", encoding="utf-8")
    # The script lives inside the package directory; this is the only shape
    # where a direct-run script can still resolve relative imports
    script = pkg_dir / "run_tool.py"
    script.write_text(
        "from .sibling import VALUE\n"
        "def main():\n"
        "    from . import sibling\n"
        "    return VALUE + sibling.VALUE\n",
        encoding="utf-8",
    )
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runner = (
        "import sys; "
        f"sys.path.insert(0, r'{repo_root}'); "
        f"sys.path.insert(0, r'{tmp_path}'); "
        "from types import ModuleType; "
        "from import_tracker.import_tracker import scan_module; "
        f"self_mod = ModuleType('__main__'); "
        f"self_mod.__file__ = r'{script}'; "
        "self_mod.__package__ = None; "
        "refs = [(r.full_name, r.kind) for r in scan_module(self_mod)]; "
        "assert ('ran_pkg.sibling', 'direct') in refs, refs; "
        "assert ('ran_pkg.sibling', 'deferred') in refs, refs; "
        "print('DIRECT_RUN_OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", runner],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "DIRECT_RUN_OK" in proc.stdout


## Cycles and HTML ############################################################


def test_cycle_detected_with_conditional_edge():
    """The dependency graph includes conditional edges, exposing the suspected
    cycle even though the conditional import never executes at runtime"""
    analysis = analyze_module("cycle_sample")
    cycle_modules = [cycle["modules"] for cycle in analysis["cycles"]]
    assert ["cycle_sample.a", "cycle_sample.b"] in cycle_modules
    edge_map = {
        (edge["source"], edge["target"]): edge
        for cycle in analysis["cycles"]
        for edge in cycle["edges"]
    }
    assert (
        edge_map[("cycle_sample.b", "cycle_sample.a")]["kind"]
        == constants.IMPORT_TYPE_CHECKING
    )


def test_html_report_renders_two_columns_and_cycles(tmp_path):
    """The --html report opens in a browser and annotates cycles"""
    analysis = analyze_module("cycle_sample")
    page = render_html_report(analysis)
    assert "<!DOCTYPE html>" in page
    assert "模块级直接依赖" in page
    assert "条件导入" in page
    assert "疑似导入环路" in page
    assert "cycle_sample.a" in page
    assert "TYPE_CHECKING" in page

    out_path = tmp_path / "report.html"
    import sys as _sys

    old_argv = _sys.argv
    _sys.argv = [
        "dummy_script",
        "--name",
        "cycle_sample",
        "--submodules",
        "--html",
        str(out_path),
    ]
    try:
        main()
    finally:
        _sys.argv = old_argv
    assert out_path.exists()
    written = out_path.read_text(encoding="utf-8")
    assert written == page


## HTML render corner cases ###################################################


def test_html_report_no_cycles_and_conditional_tags():
    """The report renders the no-cycle banner and every conditional tag kind"""
    analysis = analyze_module("ast_conditional_deps")
    assert analysis["cycles"] == []
    page = render_html_report(analysis)
    assert "未检测到疑似导入环路" in page
    assert "except ImportError 兜底" in page
    assert "TYPE_CHECKING 守卫" in page
    assert "函数体内延迟导入" in page


def test_scan_source_extra_ast_shapes():
    """Cover additional AST shapes: class bodies, tuple handlers, bare except,
    nested expression statements, attribute dotted handlers"""
    source = "\n".join(
        [
            "class Holder:",
            "    import os",
            "    try:",
            "        import alog",
            "    except (ImportError, ValueError):",
            "        alog = None",
            "",
            "try:",
            "    import foobar",
            "except:",
            "    foobar = None",
            "",
            "try:",
            "    import yaml",
            "except Exception:",
            "    yaml = None",
            "",
            "[x for x in ()]",
            "",
        ]
    )
    refs = {(r.full_name, r.kind) for r in scan_source(source, "shapes_mod")}
    assert ("os", constants.TYPE_DIRECT) in refs
    assert ("alog", constants.IMPORT_OPTIONAL) in refs
    assert ("foobar", constants.IMPORT_OPTIONAL) in refs
    assert ("yaml", constants.IMPORT_OPTIONAL) in refs


def test_analyze_module_single_file_and_limited_submodules():
    """submodules=False analyzes only the named module; a list limits members"""
    root_only = analyze_module("cycle_sample", submodules=False)
    assert list(root_only["modules"]) == ["cycle_sample"]

    limited = analyze_module(
        "cycle_sample", submodules=["cycle_sample.a"]
    )
    assert set(limited["modules"]) == {"cycle_sample", "cycle_sample.a"}


def test_analyze_missing_module_raises():
    """An unknown root module raises ModuleNotFoundError"""
    with pytest.raises(ModuleNotFoundError):
        analyze_module("definitely_not_a_real_module_zzz")


## Scanner internals ##########################################################


def test_scan_handler_name_variations_and_inverted_guard():
    """Bare except, dotted handlers and `if not TYPE_CHECKING` are covered"""
    source = "\n".join(
        [
            "import typing",
            "if not typing.TYPE_CHECKING:",
            "    import runtime_here",
            "else:",
            "    import hint_here",
            "try:",
            "    import builtins",
            "except builtins.ImportError:",
            "    builtins = None",
            "try:",
            "    import json",
            "except ImportError:",
            "    json = None",
            "else:",
            "    JSON_OK = True",
            "",
        ]
    )
    refs = {(r.full_name, r.kind) for r in scan_source(source, "handler_mod")}
    assert ("runtime_here", constants.TYPE_DIRECT) in refs
    assert ("hint_here", constants.IMPORT_TYPE_CHECKING) in refs
    assert ("builtins", constants.IMPORT_OPTIONAL) in refs
    assert ("json", constants.IMPORT_OPTIONAL) in refs


def test_scan_class_decorator_and_nested_class_with_import():
    """Class decorator expressions and imports nested in class bodies are
    scanned"""
    source = "\n".join(
        [
            "import decomodule",
            "@decomodule.decorator",
            "class A:",
            "    import classbody_mod",
            "",
        ]
    )
    refs = {(r.full_name, r.kind) for r in scan_source(source, "cls_mod")}
    assert ("decomodule", constants.TYPE_DIRECT) in refs
    assert ("classbody_mod", constants.TYPE_DIRECT) in refs


def test_scan_package_anchor_top_level_script(tmp_path):
    """A loose script outside any package scans to no anchor and its invalid
    relative imports are skipped with a warning rather than crashing"""
    script = tmp_path / "loose.py"
    script.write_text("from . import something\n", encoding="utf-8")
    refs = scan_source(
        script.read_text(encoding="utf-8"),
        module_name="__main__",
        file_path=str(script),
        package=None,
    )
    assert refs == []


def test_scan_init_file_detection(tmp_path):
    """__init__ files are recognized via path even without a dotted name"""
    pkg = tmp_path / "init_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sibling.py").write_text("X=1\n", encoding="utf-8")
    init_file = pkg / "__init__.py"
    refs = scan_source(
        "from .sibling import X\n",
        module_name="init_pkg",
        file_path=str(init_file),
        package=None,
    )
    assert [(r.full_name, r.kind) for r in refs] == [
        ("init_pkg.sibling", constants.TYPE_DIRECT)
    ]


## Scanner resolution edge cases ##############################################


def test_bare_type_checking_name():
    """The unqualified ``if TYPE_CHECKING:`` form is matched directly"""
    source = "\n".join(
        [
            "TYPE_CHECKING = False",
            "if TYPE_CHECKING:",
            "    import bare_hint",
            "",
        ]
    )
    refs = {(r.full_name, r.kind) for r in scan_source(source, "bare_tc")}
    assert ("bare_hint", constants.IMPORT_TYPE_CHECKING) in refs


def test_relative_base_too_many_dots(tmp_path):
    """Asking for more parent packages than exist resolves to no imports"""
    pkg = tmp_path / "toomany"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "leaf.py").write_text(
        "from .... import nope\n", encoding="utf-8"
    )
    refs = scan_source(
        (pkg / "leaf.py").read_text(encoding="utf-8"),
        module_name="toomany.leaf",
        file_path=str(pkg / "leaf.py"),
        package="toomany",
    )
    assert refs == []


def test_name_is_module_attribute_fallback():
    """A from-name that is an attribute (not a module) keeps the base name"""
    source = "from alog import configure\n"
    refs = scan_source(source, "attr_mod")
    assert [r.full_name for r in refs] == ["alog"]


def test_load_module_source_none_for_synthetic_module():
    """A synthetic module with no file and no loader returns no source"""
    from import_tracker.import_tracker import _load_module_source

    assert _load_module_source(ModuleType("synthetic")) is None


def test_deep_relative_into_subpackage(tmp_path):
    """A two-dot import into a sibling sub-package resolves via path scan with
    no in-memory package information"""
    root = tmp_path / "deeppkg"
    sub_a = root / "a"
    sub_b = root / "b"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (sub_a / "__init__.py").write_text("", encoding="utf-8")
    (sub_b / "__init__.py").write_text("", encoding="utf-8")
    (sub_b / "target.py").write_text("X = 1\n", encoding="utf-8")
    leaf = sub_a / "leaf.py"
    leaf.write_text("from ..b.target import X\n", encoding="utf-8")
    refs = scan_source(
        leaf.read_text(encoding="utf-8"),
        module_name="__main__",
        file_path=str(leaf),
        package=None,
    )
    assert [r.full_name for r in refs] == ["deeppkg.b.target"]


def test_scan_package_anchor_none_for_namespace_free_script(tmp_path):
    """A file inside a directory with no __init__.py chain scans to None"""
    loose = tmp_path / "loose_script.py"
    loose.write_text("import os\n", encoding="utf-8")
    assert _scan_package_anchor(str(loose)) is None


def test_bare_inverted_type_checking_and_parent_paths():
    """Cover the bare `if not TYPE_CHECKING` branch and relative dir merge"""
    source = "\n".join(
        [
            "if not TYPE_CHECKING:",
            "    import runtime_bare",
            "else:",
            "    import hint_bare",
            "",
        ]
    )
    refs = {(r.full_name, r.kind) for r in scan_source(source, "bare_inv")}
    assert ("runtime_bare", constants.TYPE_DIRECT) in refs
    assert ("hint_bare", constants.IMPORT_TYPE_CHECKING) in refs


def test_path_scan_tail_below_anchor(tmp_path):
    """A path-scan fallback import naming a sub-package below the anchor walks
    into the right directory to disambiguate a sibling submodule"""
    root = tmp_path / "tailpkg"
    inner = root / "inner"
    sibling = inner / "inner2"
    sibling.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (inner / "__init__.py").write_text("", encoding="utf-8")
    (sibling / "__init__.py").write_text("", encoding="utf-8")
    leaf = inner / "leaf.py"
    leaf.write_text("from .inner2 import X\n", encoding="utf-8")
    refs = scan_source(
        leaf.read_text(encoding="utf-8"),
        module_name="__main__",
        file_path=str(leaf),
        package=None,
    )
    # inner2 exists on disk as a sub-package, even though nothing is imported
    assert [r.full_name for r in refs] == ["tailpkg.inner.inner2"]
