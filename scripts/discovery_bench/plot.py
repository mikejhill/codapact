"""Draw the report's figures from docs/discovery-efficiency/data/results.json.

    python scripts/discovery_bench/plot.py

Writes plain SVG (no plotting library) to docs/discovery-efficiency/figures/. Each SVG
carries its own light and dark colors and follows the reader's color scheme.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs/discovery-efficiency/data/results.json"
FIGS = ROOT / "docs/discovery-efficiency/figures"

LABEL = {
    "brief": "describe --brief, describe CMD (now)",
    "brief_v091": "Same flow in codapact 0.9.1",
    "group_brief": "--help, describe GROUP --brief, describe CMD",
    "full": "Full describe",
    "help": "--help walk",
    "mcp": "MCP, every tool loaded",
    "mcp_search": "MCP + tool search",
    "openapi": "Raw OpenAPI document",
    "openapi_slice": "OpenAPI, targeted reads",
    "llms": "llms.txt, then one page",
    "llms_full": "llms-full.txt",
}
SHORT = {
    "brief": "brief + describe CMD (now)",
    "brief_v091": "brief + describe CMD (0.9.1)",
    "group_brief": "group brief + describe CMD",
    "full": "Full describe",
    "help": "--help walk",
    "mcp": "MCP, every tool loaded",
    "mcp_search": "MCP + tool search",
}
FAMILY = {
    "brief": 1,
    "group_brief": 1,
    "full": 1,
    "help": 1,
    "brief_v091": 4,
    "mcp": 2,
    "mcp_search": 2,
    "openapi": 3,
    "openapi_slice": 3,
    "llms": 3,
    "llms_full": 3,
}
FAMILY_NAME = {
    1: "codapact CLI (now)",
    2: "MCP",
    3: "Docs and specs",
    4: "codapact CLI (0.9.1)",
}

STYLE = """
<style>
  .surface { fill: #fcfcfb; }
  .t1 { fill: #0b0b0b; } .t2 { fill: #52514e; } .t3 { fill: #898781; }
  .grid { stroke: #e1e0d9; stroke-width: 1; } .axis { stroke: #c3c2b7; stroke-width: 1; }
  .s1 { fill: #2a78d6; stroke: #2a78d6; } .s2 { fill: #eb6834; stroke: #eb6834; }
  .s3 { fill: #1baf7a; stroke: #1baf7a; } .s4 { fill: #eda100; stroke: #eda100; }
  .s5 { fill: #e87ba4; stroke: #e87ba4; } .s6 { fill: #008300; stroke: #008300; }
  .s7 { fill: #6250d6; stroke: #6250d6; }
  .ring { stroke: #fcfcfb; }
  text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 12px; }
  .title { font-size: 15px; font-weight: 600; } .num { font-variant-numeric: tabular-nums; }
  @media (prefers-color-scheme: dark) {
    .surface { fill: #1a1a19; }
    .t1 { fill: #f0efec; } .t2 { fill: #c3c2b7; } .t3 { fill: #898781; }
    .grid { stroke: #2c2c2a; } .axis { stroke: #383835; }
    .s1 { fill: #3987e5; stroke: #3987e5; } .s2 { fill: #d95926; stroke: #d95926; }
    .s3 { fill: #199e70; stroke: #199e70; } .s4 { fill: #c98500; stroke: #c98500; }
    .s5 { fill: #d55181; stroke: #d55181; } .s6 { fill: #0ca30c; stroke: #0ca30c; }
    .s7 { fill: #9085e9; stroke: #9085e9; }
    .ring { stroke: #1a1a19; }
  }
</style>
"""


def k(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return f"{n:.0f}"


class Svg:
    def __init__(self, width: int, height: int, title: str, subtitle: str) -> None:
        self.w, self.h = width, height
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" aria-label="{escape(title)}">',
            f"<title>{escape(title)}</title>",
            STYLE,
            f'<rect class="surface" x="0" y="0" width="{width}" height="{height}" rx="8"/>',
            f'<text class="title t1" x="20" y="28">{escape(title)}</text>',
            f'<text class="t2" x="20" y="47">{escape(subtitle)}</text>',
        ]

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(
        self, x: float, y: float, s: str, cls: str = "t2", anchor: str = "start", extra: str = ""
    ) -> None:
        self.add(
            f'<text class="{cls}" x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" {extra}>'
            f"{escape(s)}</text>"
        )

    def legend(self, items: list[tuple[int, str]], y: float, x: float = 20) -> None:
        for cls, name in items:
            self.add(f'<rect class="s{cls}" x="{x}" y="{y - 9}" width="10" height="10" rx="2"/>')
            self.text(x + 15, y, name, "t2")
            x += 30 + 6.5 * len(name)

    def save(self, name: str) -> None:
        FIGS.mkdir(parents=True, exist_ok=True)
        (FIGS / name).write_text("\n".join([*self.parts, "</svg>"]) + "\n", "utf-8")


def hbars(
    name: str, title: str, subtitle: str, rows: list[tuple[str, int, float, str]], note: str
) -> None:
    """rows: (label, family, value, annotation). Linear axis; bars sorted as given."""
    left, right, top, bar, gap = 300, 130, 92, 18, 10
    width = 900
    height = top + len(rows) * (bar + gap) + 56
    svg = Svg(width, height, title, subtitle)
    fams = sorted({f for _, f, _, _ in rows})
    svg.legend([(f, FAMILY_NAME[f]) for f in fams], 72)
    vmax = max(v for _, _, v, _ in rows)
    step = 10 ** math.floor(math.log10(vmax))
    step = step / 2 if vmax / step < 4 else step
    span = width - left - right
    ticks = [i * step for i in range(int(vmax // step) + 1)]
    bottom = top + len(rows) * (bar + gap)
    for t in ticks:
        x = left + span * t / vmax
        svg.add(f'<line class="grid" x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{bottom}"/>')
        svg.text(x, bottom + 16, k(t), "t3 num", "middle")
    for i, (label, fam, value, ann) in enumerate(rows):
        y = top + i * (bar + gap)
        w = max(span * value / vmax, 2)
        svg.text(left - 10, y + bar - 5, label, "t1", "end")
        svg.add(
            f'<path class="s{fam}" d="M{left},{y} h{w - 4:.1f} a4,4 0 0 1 4,4 v{bar - 8} '
            f'a4,4 0 0 1 -4,4 h{-(w - 4):.1f} z"><title>{escape(label)}: {value:,.0f} tokens; '
            f"{escape(ann)}</title></path>"
        )
        svg.text(left + w + 6, y + bar - 5, f"{k(value)}  ·  {ann}", "t2 num")
    svg.add(f'<line class="axis" x1="{left}" y1="{top - 4}" x2="{left}" y2="{bottom}"/>')
    svg.text(20, height - 14, note, "t3")
    svg.save(name)


def loglog(
    name: str,
    title: str,
    subtitle: str,
    series: Sequence[tuple[str, int, Sequence[tuple[float, float]]]],
    xlabel: str,
    ylabel: str,
    note: str,
    xticks: list[float],
    yticks: list[float],
    xlog: bool = True,
) -> None:
    width, height = 900, 470
    left, right, top, bottom = 80, 250, 80, 400
    svg = Svg(width, height, title, subtitle)
    xs = [x for _, _, pts in series for x, _ in pts]
    ys = [y for _, _, pts in series for _, y in pts]
    x0, x1 = min(xticks + xs), max(xticks + xs)
    y0, y1 = min(yticks + ys), max(yticks + ys)

    def fx(x: float) -> float:
        if xlog:
            return left + (width - left - right) * (math.log10(x) - math.log10(x0)) / (
                math.log10(x1) - math.log10(x0)
            )
        return left + (width - left - right) * (x - x0) / (x1 - x0)

    def fy(y: float) -> float:
        return bottom - (bottom - top) * (math.log10(y) - math.log10(y0)) / (
            math.log10(y1) - math.log10(y0)
        )

    for t in yticks:
        svg.add(
            f'<line class="grid" x1="{left}" y1="{fy(t):.1f}" '
            f'x2="{width - right}" y2="{fy(t):.1f}"/>'
        )
        svg.text(left - 8, fy(t) + 4, k(t), "t3 num", "end")
    for t in xticks:
        svg.text(fx(t), bottom + 18, k(t), "t3 num", "middle")
    svg.add(f'<line class="axis" x1="{left}" y1="{bottom}" x2="{width - right}" y2="{bottom}"/>')
    svg.text((left + width - right) / 2, bottom + 38, xlabel, "t2", "middle")
    svg.text(
        18,
        (top + bottom) / 2,
        ylabel,
        "t2",
        "middle",
        f'transform="rotate(-90 18 {(top + bottom) / 2:.0f})"',
    )
    ends: list[list[Any]] = []
    for label, cls, pts in series:
        d = " ".join(
            f"{'M' if i == 0 else 'L'}{fx(x):.1f},{fy(y):.1f}" for i, (x, y) in enumerate(pts)
        )
        svg.add(f'<path class="s{cls}" d="{d}" fill="none" stroke-width="2" style="fill:none"/>')
        for x, y in pts:
            svg.add(
                f'<circle class="s{cls} ring" cx="{fx(x):.1f}" cy="{fy(y):.1f}" r="4" '
                'style="stroke-width:2;paint-order:stroke">'
                f"<title>{escape(label)}: {y:,.0f}</title></circle>"
            )
        ends.append([fy(pts[-1][1]), label, cls, fx(pts[-1][0])])
    ends.sort()
    for i in range(1, len(ends)):  # keep direct labels 14px apart
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 14)
    for y, label, cls, _x in ends:
        svg.add(
            f'<rect class="s{cls}" x="{width - right + 14}" y="{y - 8}" '
            'width="10" height="3" rx="1"/>'
        )
        svg.text(width - right + 30, y - 2, label, "t1")
    svg.text(20, height - 14, note, "t3")
    svg.save(name)


def main() -> None:
    d = json.loads(DATA.read_text("utf-8"))
    ynab = d["apis"]["ynab"]
    tasks = ynab["tasks"]

    # 1. YNAB: tokens and calls to the first correct call, median over the tasks.
    rows = []
    for tech in LABEL:
        res = [t["results"][tech] for t in tasks if tech in t["results"]]
        if not res:
            continue
        tok = statistics.median(r["tokens"] for r in res)
        calls = statistics.median(r["calls"] for r in res)
        extra = []
        if tech.startswith("mcp"):
            extra.append(
                f"{k(statistics.median(r['resident_tokens'] for r in res))} resident every turn"
            )
        fb = sum(r["fallback"] for r in res)
        if fb:
            extra.append(f"{fb}/{len(res)} tasks needed describe CMD")
        miss = sum(bool(r["missing"]) for r in res)
        if miss:
            extra.append(f"{miss}/{len(res)} tasks lack a needed fact")
        calls_s = f"{calls:g} call" + ("" if calls == 1 else "s")
        rows.append((LABEL[tech], FAMILY[tech], tok, ", ".join([calls_s, *extra])))
    rows.sort(key=lambda r: r[2])
    hbars(
        "ynab-tokens-to-first-call.svg",
        "YNAB: tokens read before the first correct call",
        f"Median of {len(tasks)} tasks; {ynab['commands']} commands; "
        "every reference call passes --dry-run",
        rows,
        "Tokens: anthropic 0.34.2 Claude tokenizer (approximate). "
        "Calls: tool calls the agent makes; "
        "MCP schemas arrive with the prompt instead.",
    )

    # 2. Scaling: median tokens to first call vs API size.
    order = sorted(
        (a for a in d["apis"].values() if a["commands"] >= 3), key=lambda a: a["commands"]
    )
    series = []
    for tech, cls in [
        ("mcp", 2),
        ("full", 1),
        ("brief_v091", 4),
        ("brief", 1),
        ("help", 3),
        ("mcp_search", 5),
    ]:
        pts = []
        for a in order:
            vals = [c[tech]["tokens"] for c in a["per_command"].values() if tech in c]
            if vals:
                pts.append((a["commands"], statistics.median(vals)))
        series.append((SHORT[tech], cls, pts))
    series[1] = (series[1][0], 6, series[1][2])  # full describe: yellow is 0.9.1
    loglog(
        "scaling-by-api-size.svg",
        "Discovery cost as the API grows",
        "Median tokens to the first correct call: "
        + ", ".join(f"{a['title'].split(' (')[0]} {a['commands']}" for a in order)
        + " commands",
        series,
        "commands in the API (log)",
        "tokens (log)",
        "Same measurement as figure 1, over up to 60 evenly spaced commands per API.",
        [c for c in [5, 10, 100, 1000] if c <= max(a["commands"] for a in order) * 1.2],
        [100, 1000, 10_000, 100_000, 1_000_000],
    )

    # 3. A YNAB session: context added as the agent uses more distinct commands.
    per = ynab["per_command"].values()
    med = {
        t: statistics.median(c[t]["tokens"] for c in per) for t in LABEL if all(t in c for c in per)
    }
    s = ynab["sizes"]
    skill = d["skill_tokens"]
    desc_cmd = s["describe_command_now_median"]
    old_cmd = s["describe_command_v091_median"]
    help_cmd = med["help"] - s["root_help"]
    mcp_tool = med["mcp_search"] - s["mcp_names"]
    n = range(0, 11)
    session = [
        (SHORT["mcp"], 2, [(i, s["mcp_all"]) for i in n]),
        (SHORT["full"], 6, [(i, skill + s["full"]) for i in n]),
        (SHORT["brief_v091"], 4, [(i, skill + s["brief_v091"] + i * old_cmd) for i in n]),
        (SHORT["brief"], 1, [(i, skill + s["brief"] + i * desc_cmd) for i in n]),
        (SHORT["help"], 3, [(i, s["root_help"] + i * help_cmd) for i in n]),
        (SHORT["mcp_search"], 5, [(i, s["mcp_names"] + i * mcp_tool) for i in n]),
    ]
    loglog(
        "ynab-session-context.svg",
        "YNAB: context spent over a session",
        "Tokens in context after using N distinct commands (median per command); "
        "CLI rows include the coda skill",
        session,
        "distinct commands used (0 to 10)",
        "tokens in context (log)",
        "MCP schemas are re-sent on every turn (cacheable); CLI output is read once.",
        list(n),
        [100, 1000, 10_000, 100_000],
        xlog=False,
    )


if __name__ == "__main__":
    main()
