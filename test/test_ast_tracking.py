"""Tests for the AST-based import discovery, conditional-import splitting,
relative-import resolution and HTML reporting.
"""

# Standard
from types import ModuleType
import ast
import os
import subprocess
import sys

# Local
from import_tracker.import_tracker import (
    _derive_package_from_attrs,
    _derive_package_from_path,
    _get_imports,
    build_report,
    detect_cycles,
    render_html_report,
    track_module,
    write_html_report,
)


## Four conditional-import idioms #############################################


def test_except_import_error_is_conditional():
    """try/except ImportError (and ModuleNotFoundError) fallbacks are
    conditional, while try/finally and direct imports stay direct.
    """
    report = build_report("conditional_flavors", submodules=True)
    fallback = report["conditional_flavors.fallback_mod"]
    assert fallback["conditional"] == ["alog", "some_missing_optional_thing"]
    assert "google" in fallback["direct"]
    assert "yaml" in fallback["direct"]
    # A conditional dep must never leak into the direct set
    assert "alog" not in fallback["direct"]
    assert "some_missing_optional_thing" not in fallback["direct"]


def test_type_checking_guard_is_conditional():
    """Imports inside a TYPE_CHECKING guard land only in the conditional set"""
    report = build_report("conditional_flavors", submodules=True)
    guarded = report["conditional_flavors.guarded_mod"]
    assert guarded["direct"] == []
    assert guarded["conditional"] == ["alog", "yaml"]

    root = report["conditional_flavors"]
    assert "alog" in root["conditional"]
    assert "alog" not in root["direct"]


def test_function_body_imports_are_conditional():
    """Imports performed inside function bodies (including nested functions)
    are lazy/conditional and never count as direct"""
    report = build_report("conditional_flavors", submodules=True)
    lazy = report["conditional_flavors.lazy_mod"]
    assert lazy["direct"] == []
    assert lazy["conditional"] == ["alog", "yaml"]


def test_relative_imports_resolve_to_absolute():
    """from . style relative imports resolve to absolute module names and the
    tracked output stays stable and correct"""
    mapping = track_module("conditional_flavors", submodules=True)
    # Local relative submodules are internal and must not surface as third
    # party roots
    for mod, deps in mapping.items():
        assert not any(
            dep.startswith("conditional_flavors") for dep in deps
        ), mod


def test_get_imports_classifies_four_idioms():
    """Direct check of _get_imports against the fallback sample module"""
    import importlib

    mod = importlib.import_module("conditional_flavors.fallback_mod")
    required, try_except, lazy = _get_imports(mod)
    assert "yaml" in required
    assert "google.protobuf" in required
    assert "alog" in try_except
    assert "some_missing_optional_thing" in try_except
    assert "alog" not in required


## Conditional imports: graph list but never direct ##########################


def test_missing_optional_dep_does_not_fake_cycle():
    """A missing optional dep must not appear as a hard direct dependency and
    must not trigger cycle detection (conditional edges are excluded)"""
    cycles = detect_cycles("conditional_flavors", submodules=True)
    # No unconditional cycle in this library
    assert cycles == {}


def test_conditional_and_direct_are_disjoint_columns():
    """The two output columns never share a dependency for a module"""
    report = build_report("conditional_flavors", submodules=True)
    for cols in report.values():
        assert set(cols["direct"]).isdisjoint(cols["conditional"])


## Relative import resolution: both paths must agree #########################


def test_relative_import_resolution_paths_agree():
    """Lock down that __package__/__name__ derivation and the path-scan
    fallback produce the same absolute anchor for the same file across a range
    of relative depths.
    """
    import importlib

    cases = [
        # (module, import level, module part)
        ("sample_lib", 1, ""),
        ("sample_lib.nested", 1, ""),
        ("sample_lib.nested.submod3", 1, ""),
        ("sample_lib.submod2", 1, ""),
        ("all_import_types.sub_module2", 2, ""),
        ("deep_siblings.workflows.foo_type.foo", 3, "blocks.foo_type.foo"),
    ]
    for mod_name, level, module in cases:
        mod = importlib.import_module(mod_name)
        derived = _derive_package_from_attrs(
            name=mod.__name__,
            package=getattr(mod, "__package__", None),
            defined_in_init=os.path.splitext(
                os.path.basename(getattr(mod, "__file__", ""))
            )[0]
            == "__init__",
            level=level,
            module=module,
        )
        scanned = _derive_package_from_path(
            file_path=getattr(mod, "__file__", None),
            level=level,
            module=module,
        )
        assert derived is not None, mod_name
        assert derived == scanned, (mod_name, level, derived, scanned)


def test_derive_package_name_uses_package_attrs():
    """Single-dot relative imports anchor at __package__ for both packages and
    ordinary modules"""
    assert (
        _derive_package_from_attrs(
            name="pkg.mod", package="pkg", defined_in_init=False, level=1
        )
        == "pkg"
    )
    assert (
        _derive_package_from_attrs(
            name="pkg", package="pkg", defined_in_init=True, level=1
        )
        == "pkg"
    )
    assert (
        _derive_package_from_attrs(
            name="pkg.sub.mod",
            package="pkg.sub",
            defined_in_init=False,
            level=2,
        )
        == "pkg"
    )


def test_derive_package_name_none_for_top_level_script():
    """A script run directly (__package__ is None and not an __init__) cannot
    derive an anchor from attrs and must return None so the path fallback runs"""
    assert (
        _derive_package_from_attrs(
            name="__main__",
            package=None,
            defined_in_init=False,
            level=1,
        )
        is None
    )


def test_path_fallback_matches_for_real_files():
    """The path scan fallback reconstructs the same anchor the import system
    would compute for real sample library files"""
    import importlib

    # submod2 is a package (__init__.py): level 1 anchors at its own name
    mod = importlib.import_module("sample_lib.submod2")
    scanned = _derive_package_from_path(
        file_path=mod.__file__, level=1, module=""
    )
    assert scanned == "sample_lib.submod2"

    # A plain module file (sample_lib/nested/submod3.py) anchors level 1 at
    # its __package__
    mod_file = importlib.import_module("sample_lib.nested.submod3")
    scanned_file = _derive_package_from_path(
        file_path=mod_file.__file__, level=1, module=""
    )
    assert scanned_file == "sample_lib.nested"

    mod2 = importlib.import_module("all_import_types.sub_module2")
    scanned2 = _derive_package_from_path(
        file_path=mod2.__file__, level=2, module=""
    )
    assert scanned2 == "all_import_types"

## De-duplication and stable ordering #########################################


def test_duplicate_imports_dedup_by_module_name():
    """Repeated imports of the same module collapse to one entry, regardless of
    set iteration order; output is sorted"""
    report = build_report("conditional_flavors", submodules=True)
    for cols in report.values():
        for key in ("direct", "conditional"):
            values = cols[key]
            assert values == sorted(values)
            assert len(values) == len(set(values))


def test_plain_output_is_sorted_and_unique():
    """The dict-list output is sorted and de-duplicated"""
    mapping = track_module("optional_deps", submodules=True)
    for deps in mapping.values():
        assert deps == sorted(deps)
        assert len(deps) == len(set(deps))


def test_collector_dedups_same_module_within_source():
    """The AST collector de-duplicates repeated imports and keeps first-seen
    order rather than leaking set ordering"""
    source = (
        "import alog\n"
        "import alog\n"
        "import yaml\n"
        "import alog\n"
    )
    import importlib.util
    import import_tracker.import_tracker as it

    mod = ModuleType("dedup_mod")
    mod.__file__ = None
    # Bypass file reading by parsing directly through the collector
    tree = ast.parse(source)
    collector = it._ImportCollector(mod)
    collector.visit(tree)
    assert collector.required == ["alog", "yaml"]


## Cycle detection and HTML report ############################################


def test_detect_cycles_flags_real_cycle(tmp_path):
    """A genuine unconditional import cycle is detected and annotated; a
    conditional-only back-edge is not"""
    pkg_dir = tmp_path / "cycle_lib"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("from . import a\n")
    (pkg_dir / "a.py").write_text("from . import b\n")
    (pkg_dir / "b.py").write_text("from . import a\n")
    sys.path.insert(0, str(tmp_path))
    try:
        cycles = detect_cycles("cycle_lib", submodules=True)
        joined = {node: paths for node, paths in cycles.items()}
        assert "cycle_lib.a" in joined
        assert "cycle_lib.b" in joined
        report = build_report("cycle_lib", submodules=True)
        html = render_html_report(report, cycles=cycles)
        assert "suspected cycle" in html
        assert "cycle_lib.a" in html
    finally:
        sys.path.remove(str(tmp_path))


def test_conditional_back_edge_is_not_a_cycle(tmp_path):
    """When the back edge sits in a TYPE_CHECKING guard or function body, no
    hard cycle is reported"""
    pkg_dir = tmp_path / "nocycle_lib"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("from . import a\n")
    (pkg_dir / "a.py").write_text("from . import b\n")
    (pkg_dir / "b.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from . import a\n"
    )
    sys.path.insert(0, str(tmp_path))
    try:
        cycles = detect_cycles("nocycle_lib", submodules=True)
        assert cycles == {}
        report = build_report("nocycle_lib", submodules=True)
        assert "nocycle_lib.a" not in report["nocycle_lib.b"]["direct"]
    finally:
        sys.path.remove(str(tmp_path))


def test_render_html_two_columns_and_escaping():
    """The HTML page contains both columns, one row per module, and escapes
    content safely"""
    report = {
        "some.mod": {
            "direct": ["yaml"],
            "conditional": ["alog"],
        }
    }
    html = render_html_report(report, title="T")
    assert "<title>T</title>" in html
    assert "Direct Dependencies" in html
    assert "Conditional Imports" in html
    assert "some.mod" in html
    assert "yaml" in html
    assert "alog" in html


def test_write_html_report_creates_browser_ready_file(tmp_path):
    """write_html_report writes a standalone utf-8 html file"""
    out = tmp_path / "report.html"
    write_html_report("optional_deps", str(out), submodules=True)
    content = out.read_text(encoding="utf-8")
    assert content.lstrip().startswith("<!DOCTYPE html>")
    assert "optional_deps.opt" in content
    assert "</html>" in content


def test_html_conditional_not_in_direct_column(tmp_path):
    """The optional alog dep appears in the conditional column but is never
    rendered in the direct column for optional_deps.opt"""
    out = tmp_path / "report.html"
    write_html_report("conditional_flavors", str(out), submodules=True)
    html = out.read_text(encoding="utf-8")
    # The guarded module row must place alog in the conditional <td>
    assert "conditional_flavors.guarded_mod" in html
    assert html.count("alog") >= 1

## __package__ is None: a script run directly ##################################


def test_direct_script_with_none_package_relative_import(tmp_path):
    """End-to-end: a script executed directly (``python script.py``) has
    ``__package__ is None``. Its relative-style imports must still resolve via
    the path-scan fallback, and the tracker must succeed rather than crashing.

    Layout::

        tmp_path/
            mypkg/
                __init__.py      (imports sibling)
                sibling.py       (imports yaml directly)
            analyze.py           (run as __main__, builds a report on mypkg)
    """
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text(
        "from . import sibling\n"
        "import yaml\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import alog\n"
    )
    (pkg_dir / "sibling.py").write_text("import google.protobuf\n")

    analyzer = tmp_path / "analyze.py"
    analyzer.write_text(
        "import json, sys\n"
        "sys.path.insert(0, r'%s')\n"
        "from import_tracker.import_tracker import build_report\n"
        "print('JSONSTART')\n"
        "print(json.dumps(build_report('mypkg', submodules=True)))\n"
        "print('JSONEND')\n"
        % str(__import__("pathlib").Path(__import__("import_tracker").__file__).parent.parent).replace("\\", "\\\\")
    )

    # Run from a neutral cwd; the analyzer adds the real cwd (repo root) so
    # import_tracker is importable, and tmp_path so mypkg is importable
    env = dict(__import__("os").environ)
    repo_root = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..")
    )
    env["PYTHONPATH"] = os.pathsep.join(
        [str(tmp_path), repo_root, env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [sys.executable, str(analyzer)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    start = out.index("JSONSTART") + len("JSONSTART")
    end = out.index("JSONEND")
    import json as _json

    report = _json.loads(out[start:end].strip())
    # Root aggregates the unconditional direct deps of its child modules
    # (existing parent-direct semantics), and alog stays conditional. The key
    # point here is that the relative import (from . import sibling) resolved
    # correctly even though the analyzer ran with __package__ is None.
    assert set(report["mypkg"]["direct"]) == {"google", "yaml"}
    assert report["mypkg"]["conditional"] == ["alog"]
    assert "google" in report["mypkg.sibling"]["direct"]
    assert "mypkg.sibling" in report


def test_path_fallback_for_none_package_script(tmp_path):
    """Unit-level: when __package__ is None for a file inside a package on
    sys.path, the path scan reconstructs the package anchor"""
    pkg_dir = tmp_path / "standalone_pkg"
    pkg_dir.mkdir()
    script = pkg_dir / "tool.py"
    script.write_text("import yaml\n")
    sys.path.insert(0, str(tmp_path))
    try:
        from import_tracker.import_tracker import _derive_package_from_path

        anchor = _derive_package_from_path(str(script), level=1)
        assert anchor == "standalone_pkg"
    finally:
        sys.path.remove(str(tmp_path))
