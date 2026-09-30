"""
HTML rendering for the static two-column (direct vs conditional) dependency
report produced by ``import_tracker.import_tracker.analyze_module``.

The generated page is fully self-contained (no external CSS or JavaScript) so
it can be opened directly from a file:// URL in any browser for screen
recording / manual review.
"""
# Standard
from typing import Any, Dict, List
import html as html_lib

# Local
from . import constants

# Human readable labels for the conditional import categories
KIND_LABELS = {
    constants.TYPE_DIRECT: "模块级直接依赖",
    constants.IMPORT_OPTIONAL: "except ImportError 兜底",
    constants.IMPORT_TYPE_CHECKING: "TYPE_CHECKING 守卫",
    constants.IMPORT_DEFERRED: "函数体内延迟导入",
}

_KIND_CSS_CLASS = {
    constants.IMPORT_OPTIONAL: "kind-optional",
    constants.IMPORT_TYPE_CHECKING: "kind-typechecking",
    constants.IMPORT_DEFERRED: "kind-deferred",
}

_CSS = """
body { font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
       margin: 24px; color: #1f2933; background: #fafbfc; }
h1 { font-size: 20px; }
h2 { font-size: 16px; margin-top: 28px; }
.summary { color: #52606d; margin-bottom: 16px; }
.cycles { border: 1px solid #e0b4b4; background: #fff6f6; border-radius: 6px;
          padding: 12px 16px; margin: 12px 0 20px; }
.cycles ul { margin: 6px 0; }
.cycle-edge { color: #9f3a38; }
.no-cycle { border: 1px solid #b7e1c1; background: #fcfff5; border-radius: 6px;
            padding: 10px 16px; margin: 12px 0 20px; color: #2c662d; }
table.module-grid { border-collapse: collapse; width: 100%;
                    table-layout: fixed; background: white;
                    box-shadow: 0 1px 2px rgba(0,0,0,.08); }
table.module-grid th, table.module-grid td {
    border: 1px solid #d9e2ec; padding: 8px 10px; vertical-align: top;
    text-align: left; word-break: break-all; }
table.module-grid th { background: #f0f4f8; }
.col-module { width: 26%; }
.col-direct { width: 30%; }
.col-conditional { width: 44%; }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px;
       border-radius: 10px; margin-left: 6px; white-space: nowrap; }
.kind-optional { background: #fff3cd; color: #8a6d3b; }
.kind-typechecking { background: #e7f1ff; color: #1c5fc4; }
.kind-deferred { background: #ede7f6; color: #5e35b1; }
.cycle-mod { background: #ffe8e6; border-radius: 3px; padding: 0 4px; }
.empty { color: #9aa5b1; }
code { font-family: Consolas, 'Courier New', monospace; font-size: 13px; }
"""


def render_html_report(analysis: Dict[str, Any]) -> str:
    """Render the analyze_module output as a self-contained HTML page"""
    root = analysis["root"]
    modules = analysis["modules"]
    cycles = analysis["cycles"]

    cycle_modules = {mod for cycle in cycles for mod in cycle["modules"]}
    parts: List[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>导入依赖报告 - {html_lib.escape(root)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>导入依赖报告：<code>{html_lib.escape(root)}</code></h1>",
        '<div class="summary">'
        f"共分析 <b>{len(modules)}</b> 个模块，"
        f"发现 <b>{len(cycles)}</b> 个疑似导入环路（条件边已计入依赖图）。"
        "</div>",
        _render_cycles(cycles),
        '<table class="module-grid"><thead><tr>'
        '<th class="col-module">模块</th>'
        '<th class="col-direct">模块级直接依赖</th>'
        '<th class="col-conditional">条件导入</th>'
        "</tr></thead><tbody>",
    ]

    for module_name in sorted(modules):
        info = modules[module_name]
        parts.append(
            "<tr>"
            f"<td>{_render_module_name(module_name, cycle_modules)}</td>"
            f"<td>{_render_direct(info['direct'])}</td>"
            f"<td>{_render_conditional(info['conditional'])}</td>"
            "</tr>"
        )
    parts.append("</tbody></table></body></html>")
    return "\n".join(parts)


def _render_cycles(cycles: List[Dict[str, Any]]) -> str:
    if not cycles:
        return '<div class="no-cycle">未检测到疑似导入环路。</div>'
    rows = ['<div class="cycles"><b>疑似导入环路标注</b><ul>']
    for cycle in cycles:
        members = " → ".join(
            f"<code>{html_lib.escape(name)}</code>" for name in cycle["modules"]
        )
        edge_lines = []
        for edge in cycle["edges"]:
            label = KIND_LABELS.get(edge["kind"], edge["kind"])
            edge_lines.append(
                '<li class="cycle-edge">'
                f"<code>{html_lib.escape(edge['source'])}</code> → "
                f"<code>{html_lib.escape(edge['target'])}</code> "
                f"（{html_lib.escape(label)}，第 {edge['lineno']} 行）</li>"
            )
        rows.append(f"<li>{members}（回到起点）<ul>{''.join(edge_lines)}</ul></li>")
    rows.append("</ul></div>")
    return "".join(rows)


def _render_module_name(module_name: str, cycle_modules: set) -> str:
    escaped = html_lib.escape(module_name)
    if module_name in cycle_modules:
        return f'<code><span class="cycle-mod">{escaped}</span></code>'
    return f"<code>{escaped}</code>"


def _render_direct(direct: List[str]) -> str:
    if not direct:
        return '<span class="empty">（无）</span>'
    return "<br>".join(
        f"<code>{html_lib.escape(name)}</code>" for name in direct
    )


def _render_conditional(conditional: Dict[str, str]) -> str:
    if not conditional:
        return '<span class="empty">（无）</span>'
    items = []
    for name in sorted(conditional):
        kind = conditional[name]
        css_class = _KIND_CSS_CLASS.get(kind, "")
        label = KIND_LABELS.get(kind, kind)
        tag = f'<span class="tag {css_class}">{html_lib.escape(label)}</span>'
        items.append(
            f"<div><code>{html_lib.escape(name)}</code>{tag}</div>"
        )
    return "".join(items)
