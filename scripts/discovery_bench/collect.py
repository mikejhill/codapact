"""Measure what an agent must read before its first correct call, per discovery technique.

Generates every example CLI with the codapact in this checkout, captures the text each
discovery technique hands an agent, counts its tokens, and writes
docs/discovery-efficiency/data/results.json. See docs/discovery-efficiency/README.md
("Methodology") for what each technique and number means.

Run from the repository root:

    uv run --with anthropic==0.34.2 --with tokenizers python scripts/discovery_bench/collect.py

Add --github to include the full GitHub REST description (1,279 operations, downloaded
from a pinned commit, about 14 MB).
"""

from __future__ import annotations

import argparse
import functools
import importlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/discovery-efficiency/data/results.json"
CORPUS = ROOT / "tests/fixtures/corpus"
GITHUB_URL = (
    "https://raw.githubusercontent.com/github/rest-api-description/"
    "5c36b6f685bae2acccf24a442f3240bf121cb488/descriptions/api.github.com/api.github.com.json"
)
SAMPLE = 60  # commands measured per API (evenly spaced); all of them when fewer
SKILL = ROOT / "extras/agent-plugin/skills/create-coda-skill/assets/SKILL.template.md"


# --------------------------------------------------------------------------- tokens


def _tokenizer() -> Any:
    """The Claude tokenizer shipped in anthropic 0.34.x (an approximation; see README)."""
    from importlib.resources import files

    from tokenizers import Tokenizer  # ty: ignore[unresolved-import]

    return Tokenizer.from_str(files("anthropic").joinpath("tokenizer.json").read_text("utf-8"))


TOK = _tokenizer()


@functools.cache
def tokens(text: str) -> int:
    return len(TOK.encode(text).ids) if text else 0


# --------------------------------------------------------------------------- APIs


@dataclass
class Api:
    key: str
    title: str
    source_dir: Path
    config: dict[str, Any]
    package: str
    openapi: str | None = None  # contract file name, for OpenAPI sources
    download: str | None = None


def _cfg(
    source: dict[str, Any], package: str, cli: dict[str, Any], shape: list[Any] | None = None
) -> dict[str, Any]:
    cfg: dict[str, Any] = {"version": 1, "source": source, "cli": cli}
    if shape:
        cfg["shape"] = shape
    if any(
        "auth" in s for s in shape or []
    ):  # the GitHub description: trim namespaces as its corpus config does
        cfg["surface"] = {"trim_namespace": True}
    cfg["targets"] = [
        {"name": "python", "output": "gen", "options": {"package": package}},
        {"name": "python-cli", "output": "gen"},
    ]
    return cfg


def apis(with_github: bool) -> list[Api]:
    def shape_of(path: Path) -> list[Any] | None:
        return yaml.safe_load(path.read_text("utf-8")).get("shape")

    out = [
        Api(
            "petstore",
            "Petstore",
            ROOT / "examples/petstore",
            _cfg(
                {"type": "openapi", "path": "openapi.yaml"},
                "petstore",
                {"program": "petstore"},
                shape_of(ROOT / "examples/petstore/codapact.yaml"),
            ),
            "petstore",
            "openapi.yaml",
        ),
        Api(
            "library",
            "Library (GraphQL)",
            ROOT / "examples/library",
            _cfg(
                yaml.safe_load((ROOT / "examples/library/codapact.yaml").read_text("utf-8"))[
                    "source"
                ],
                "library",
                {"program": "library"},
            ),
            "library",
        ),
        Api(
            "countries",
            "Countries (GraphQL)",
            CORPUS / "countries",
            _cfg(
                yaml.safe_load((CORPUS / "countries/codapact.yaml").read_text("utf-8"))["source"],
                "countries",
                {"program": "countries"},
            ),
            "countries",
        ),
        # The YNAB coda's own settings (mikejhill/codapact-ynab codapact.yaml).
        Api(
            "ynab",
            "YNAB",
            CORPUS / "ynab",
            _cfg(
                {"type": "openapi", "path": "openapi.yaml"},
                "codapact_ynab",
                {
                    "program": "codapact-ynab",
                    "env_prefix": "YNAB",
                    "auth": {
                        "bearer": {
                            "docs": (
                                "Create a personal access token in YNAB under Account Settings > "
                                "Developer Settings (https://app.ynab.com/settings/developer). "
                                "It grants full access to your budgets."
                            )
                        }
                    },
                },
                shape_of(CORPUS / "ynab/codapact.yaml"),
            ),
            "codapact_ynab",
            "openapi.yaml",
        ),
        Api(
            "pokeapi",
            "PokéAPI",
            CORPUS / "pokeapi",
            _cfg(
                {"type": "openapi", "path": "openapi.yml", "options": {"strict": True}},
                "pokeapi",
                {"program": "poke"},
                shape_of(CORPUS / "pokeapi/codapact.yaml"),
            ),
            "pokeapi",
            "openapi.yml",
        ),
    ]
    if with_github:
        out.append(
            Api(
                "github",
                "GitHub REST (full)",
                Path(),
                _cfg(
                    {
                        "type": "openapi",
                        "path": "api.github.com.json",
                        "options": {"strict": False},
                    },
                    "ghfull",
                    {"program": "gh-full"},
                    [
                        {"auth": {"schemes": {"token": "bearer"}, "optional": True}},
                        {"group": {"by": "tag"}},
                    ],
                ),
                "ghfull",
                "api.github.com.json",
                GITHUB_URL,
            )
        )
    return out


def generate(api: Api, work: Path) -> Path:
    """Copy the contract into work/<key>, generate the CLI there, return the src dir."""
    where = work / api.key
    if where.exists():
        shutil.rmtree(where)
    if api.download:
        where.mkdir(parents=True)
        cache = work / "_cache" / Path(api.download).name
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(api.download, cache)  # noqa: S310 - pinned https URL
        shutil.copy(cache, where / (api.openapi or cache.name))
    else:
        shutil.copytree(api.source_dir, where)
    (where / "codapact.yaml").write_text(yaml.safe_dump(api.config, sort_keys=False), "utf-8")
    subprocess.run(
        [sys.executable, "-m", "codapact", "generate", "-c", str(where / "codapact.yaml")],
        check=True,
        capture_output=True,
        text=True,
    )
    return where / "gen/src"


class Cli:
    """Run a generated CLI in-process (same output as the console script, much faster)."""

    def __init__(self, src: Path, package: str) -> None:
        sys.path.insert(0, str(src))
        for name in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
            del sys.modules[name]
        self.module = importlib.import_module(f"{package}.cli")
        sys.path.remove(str(src))

    def run(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = self.module.main(list(argv))
            except SystemExit as exit_:
                code = int(exit_.code or 0)
        return code, out.getvalue() + err.getvalue()

    def text(self, *argv: str) -> str:
        code, text = self.run(*argv)
        if code != 0:
            raise RuntimeError(f"{argv}: exit {code}: {text[:400]}")
        return text


# --------------------------------------------------------------------------- MCP


def mcp_tool(entry: dict[str, Any], with_output: bool = False) -> dict[str, Any]:
    """The MCP tools/list entry an OpenAPI-to-MCP bridge would publish for one command."""
    props: dict[str, Any] = {}
    required = []
    kinds = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}
    for arg in entry["arguments"]:
        if arg["type"] == "json":
            schema = dict(arg.get("schema") or {})
            schema.pop("$schema", None)
        else:
            schema = {"type": kinds.get(arg["type"], "string")}
            if arg.get("choices"):
                schema["enum"] = arg["choices"]
        if arg.get("repeated"):
            schema = {"type": "array", "items": schema}
        if arg.get("description"):
            schema["description"] = arg["description"]
        props[arg["name"]] = schema
        if arg.get("required"):
            required.append(arg["name"])
    description = entry.get("summary", "")
    if entry.get("description") and entry["description"] != description:
        description += "\n\n" + entry["description"]
    tool: dict[str, Any] = {
        "name": entry["command"].replace(" ", "_").replace("-", "_")[:64],
        "description": description,
        "inputSchema": {"type": "object", "properties": props, "required": required},
        "annotations": {
            "readOnlyHint": bool(entry.get("read_only")),
            "destructiveHint": bool(entry.get("destructive")),
        },
    }
    if with_output and entry.get("output_schema"):
        out = dict(entry["output_schema"])
        out.pop("$schema", None)
        tool["outputSchema"] = out
    return tool


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- OpenAPI


def load_spec(path: Path) -> dict[str, Any]:
    text = path.read_text("utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def _collect_refs(node: Any, known: dict[str, Any], out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/") and ref not in known:
            out.add(ref)
        for value in node.values():
            _collect_refs(value, known, out)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, known, out)


def resolve_slice(spec: dict[str, Any], method: str, path: str) -> tuple[dict[str, Any], int]:
    """The operation plus every component it references, and the $ref depth followed."""
    op = spec["paths"][path][method.lower()]
    pieces: dict[str, Any] = {}
    frontier, depth = [op, spec["paths"][path].get("parameters", [])], 0
    while frontier:
        refs: set[str] = set()
        _collect_refs(frontier, pieces, refs)
        frontier = []
        for ref in sorted(refs):
            node: Any = spec
            for part in ref[2:].split("/"):
                node = node[part.replace("~1", "/").replace("~0", "~")]
            pieces[ref] = node
            frontier.append(node)
        depth += 1 if refs else 0
    return {"paths": {path: {method.lower(): op}}, "components": pieces}, depth


def spec_preamble(spec: dict[str, Any]) -> str:
    """What a raw-HTTP agent also needs: servers and security schemes."""
    return yaml.safe_dump(
        {
            "servers": spec.get("servers"),
            "security": spec.get("security"),
            "securitySchemes": (spec.get("components") or {}).get("securitySchemes"),
        },
        sort_keys=False,
        allow_unicode=True,
    )


def llms_line(op: dict[str, Any], method: str, path: str) -> str:
    slug = (op.get("operationId") or f"{method}-{path}").strip("/").replace("/", "-")
    summary = (op.get("summary") or "").strip()
    return f"- [{summary or slug}](ops/{slug}.md): `{method.upper()} {path}`"


def llms_page(slice_: dict[str, Any], method: str, path: str) -> str:
    """An llms.txt-style Markdown page for one operation, with its schemas inline."""
    op = slice_["paths"][path][method.lower()]
    lines = [
        f"# {op.get('summary') or op.get('operationId')}",
        "",
        f"`{method.upper()} {path}`",
        "",
    ]
    if op.get("description"):
        lines += [op["description"].strip(), ""]
    params = op.get("parameters") or []
    if params:
        lines += ["## Parameters", ""]
        for p in params:
            if "$ref" in p:
                p = slice_["components"].get(p["$ref"], p)
            lines.append(
                f"- `{p.get('name')}` ({p.get('in')}, "
                f"{'required' if p.get('required') else 'optional'}): "
                f"{(p.get('description') or '').strip()}"
            )
        lines.append("")
    if op.get("requestBody") or slice_["components"]:
        lines += [
            "## Schemas",
            "",
            "```json",
            json.dumps(
                {
                    "requestBody": op.get("requestBody"),
                    "responses": op.get("responses"),
                    "components": slice_["components"],
                },
                indent=2,
                ensure_ascii=False,
            ),
            "```",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- measuring


@dataclass
class Step:
    call: str
    text: str
    resident: bool = False  # in context before the task starts (MCP tool lists)
    relevant: str | None = None  # the part about the target command; facts count only here


def _norm(text: str) -> str:
    """Compare facts loosely: case, JSON escaping, and - or _ separators do not matter."""
    return text.replace('\\"', '"').lower().replace("_", "").replace("-", "")


def _missing(facts: list[list[str]], steps: list[Step]) -> list[str]:
    seen = _norm("\n".join(s.text if s.relevant is None else s.relevant for s in steps))
    return [alts[0] for alts in facts if not any(_norm(a) in seen for a in alts)]


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    fallback: Step | None = None  # read only when the base steps lack the facts

    def measure(self, facts: list[list[str]]) -> dict[str, Any]:
        steps = list(self.steps)
        missing = _missing(facts, steps)
        used_fallback = False
        if missing and self.fallback:
            steps.append(self.fallback)
            used_fallback = True
            missing = _missing(facts, steps)
        return {
            "calls": sum(1 for s in steps if not s.resident),
            "tokens": sum(tokens(s.text) for s in steps),
            "resident_tokens": sum(tokens(s.text) for s in steps if s.resident),
            "bytes": sum(len(s.text.encode()) for s in steps),
            "steps": [s.call for s in steps],
            "fallback": used_fallback,
            "missing": missing,
        }


TECHNIQUES = {
    "brief": "codapact: describe --brief (then describe GROUP --brief if it lists groups), "
    "then describe COMMAND",
    "brief_v091": "codapact 0.9.1: indented describe --brief listing every command, then "
    "indented describe COMMAND with its output schema",
    "group_brief": "codapact: --help, describe GROUP --brief, describe COMMAND",
    "full": "codapact: full describe",
    "help": "--help tree walk (root, group, command)",
    "mcp": "MCP: every tool schema loaded up front",
    "mcp_search": "MCP + tool search: names resident, load one schema",
    "openapi": "Raw OpenAPI document",
    "openapi_slice": "OpenAPI, targeted reads (operation + $refs)",
    "llms": "llms.txt index, then one operation page",
    "llms_full": "llms-full.txt (every page)",
}


@dataclass
class Catalog:
    """Every text a technique can hand an agent for one API."""

    cli: Cli
    program: str
    entries: dict[str, dict[str, Any]]
    brief: str
    brief_v091: str
    full: str
    root_help: str
    mcp_all: str
    mcp_names: str
    spec_text: str | None
    spec: dict[str, Any] | None
    op_of: dict[str, tuple[str, str]]
    llms_index: str | None
    llms_full: str | None

    def plans(self, command: str) -> dict[str, Plan]:
        group = command.split()[0] if " " in command else None
        p = self.program
        entry = self.entries[command]
        catalog = json.loads(self.brief)
        listed = catalog["commands"] if group is None or "groups" not in catalog else []
        if group is not None and "groups" in catalog:
            listed = json.loads(self.cli.text("describe", group, "--brief"))
        brief_entry = dumps(next((c for c in listed if c["command"] == command), {}))
        full_entry = dumps(
            next(c for c in json.loads(self.full)["commands"] if c["command"] == command)
        )
        tool = dumps(mcp_tool(entry))
        describe_cmd = Step(f"{p} describe {command}", self.cli.text("describe", *command.split()))
        help_cmd = Step(f"{p} {command} --help", self.cli.text(*command.split(), "--help"))
        group_help = (
            Step(f"{p} {group} --help", self.cli.text(group, "--help"), relevant="")
            if group
            else None
        )
        group_brief = (
            Step(
                f"{p} describe {group} --brief",
                self.cli.text("describe", group, "--brief"),
                relevant=brief_entry,
            )
            if group
            else Step(f"{p} describe --brief", self.brief, relevant=brief_entry)
        )
        root = Step(f"{p} --help", self.root_help, relevant="")
        brief_steps = [Step(f"{p} describe --brief", self.brief, relevant=brief_entry)]
        if "groups" in catalog and group is not None:
            brief_steps = [
                Step(f"{p} describe --brief (groups)", self.brief, relevant=""),
                group_brief,
            ]
        plans = {
            "brief": Plan([*brief_steps, describe_cmd]),
            "brief_v091": Plan(
                [
                    Step(f"{p} describe --brief (0.9.1)", self.brief_v091, relevant=brief_entry),
                    Step(f"{p} describe {command} (0.9.1)", json.dumps(entry, indent=2)),
                ]
            ),
            "group_brief": Plan([root, group_brief, describe_cmd]),
            "full": Plan([Step(f"{p} describe", self.full, relevant=full_entry)], describe_cmd),
            "help": Plan([root, *([group_help] if group_help else []), help_cmd], describe_cmd),
            "mcp": Plan(
                [Step("tools/list (all tools)", self.mcp_all, resident=True, relevant=tool)]
            ),
            "mcp_search": Plan(
                [
                    Step("tool names (resident)", self.mcp_names, resident=True, relevant=""),
                    Step("tool search: load 1 schema", tool),
                ]
            ),
        }
        if self.spec is not None and command in self.op_of:
            method, path = self.op_of[command]
            slice_, depth = resolve_slice(self.spec, method, path)
            slice_text = yaml.safe_dump(slice_, sort_keys=False, allow_unicode=True)
            page = llms_page(slice_, method, path)
            plans["openapi"] = Plan(
                [Step("read the OpenAPI document", self.spec_text or "", relevant=slice_text)]
            )
            plans["openapi_slice"] = Plan(
                [
                    Step(
                        "search paths for the operation",
                        llms_line(slice_["paths"][path][method.lower()], method, path),
                        relevant="",
                    ),
                    Step("read servers + securitySchemes", spec_preamble(self.spec), relevant=""),
                    Step(f"read the operation and {depth} level(s) of $refs", slice_text),
                ]
                + [Step(f"follow $ref level {i + 2}", "") for i in range(max(depth - 1, 0))]
            )
            plans["llms"] = Plan(
                [
                    Step("read llms.txt", self.llms_index or "", relevant=""),
                    Step("read the operation page", page),
                ]
            )
            plans["llms_full"] = Plan(
                [Step("read llms-full.txt", self.llms_full or "", relevant=page)]
            )
        return plans


def catalog(api: Api, src: Path, work: Path) -> Catalog:
    cli = Cli(src, api.package)
    brief = cli.text("describe", "--brief")
    program = json.loads(brief)["program"]
    full = cli.text("describe")
    commands = [c["command"] for c in json.loads(full)["commands"]]
    # One call returns every entry with argument and output schemas, as describe COMMAND does.
    entries = {c["command"]: c for c in json.loads(cli.text("describe", "--schemas"))["commands"]}
    tools = [mcp_tool(entries[c]) for c in commands]
    # 0.9.1 printed indented JSON and listed every command in --brief.
    traits = ("read_only", "destructive", "paginated", "deprecated")
    old_brief = {k: v for k, v in json.loads(brief).items() if k != "groups"}
    old_brief["commands"] = [
        {
            "command": c,
            "summary": entries[c]["summary"],
            "usage": entries[c]["usage"],
            **({"traits": t} if (t := [n for n in traits if entries[c].get(n)]) else {}),
        }
        for c in commands
    ]
    brief_v091 = json.dumps(old_brief, indent=2, ensure_ascii=False)
    names = "\n".join(f"{t['name']}" for t in tools)
    spec = spec_text = llms_index = llms_full = None
    op_of: dict[str, tuple[str, str]] = {}
    if api.openapi:
        spec_path = work / api.key / api.openapi
        spec_text = spec_path.read_text("utf-8")
        spec = load_spec(spec_path)
        model = json.loads((src / api.package / "_model.json").read_text("utf-8"))
        by_summary: dict[str, list[tuple[str, str]]] = {}
        for op in model["operations"]:
            for b in op.get("bindings", []):
                if b.get("kind") == "http":
                    by_summary.setdefault(op.get("summary") or op["name"], []).append(
                        (b["method"], b["path"])
                    )
        for c in commands:
            hits = by_summary.get(entries[c].get("summary") or "", [])
            if len(hits) == 1:
                op_of[c] = hits[0]
        lines, pages = [f"# {spec.get('info', {}).get('title', api.title)}", ""], []
        for path, item in spec["paths"].items():
            for method, op in item.items():
                if method in ("get", "put", "post", "patch", "delete") and isinstance(op, dict):
                    lines.append(llms_line(op, method, path))
                    if len(pages) < 2000:
                        pages.append(llms_page(resolve_slice(spec, method, path)[0], method, path))
        llms_index = "\n".join(lines)
        llms_full = "\n\n".join(pages)
    return Catalog(
        cli,
        program,
        entries,
        brief,
        brief_v091,
        full,
        cli.text("--help"),
        dumps({"tools": tools}),
        names,
        spec_text,
        spec,
        op_of,
        llms_index,
        llms_full,
    )


# --------------------------------------------------------------------------- YNAB tasks

# Each task: the call a correct agent makes, and the facts it must have read to make it.
# A fact is satisfied when any of its spellings appears in what the technique returned.
YNAB_TASKS: list[dict[str, Any]] = [
    {
        "id": "list-transactions",
        "goal": "List transactions since 2026-11-01",
        "argv": ["transactions", "list", "--since-date", "2026-11-01"],
        "facts": [["--since-date", "since_date"]],
    },
    {"id": "list-payees", "goal": "List payees", "argv": ["payees", "list"], "facts": []},
    {
        "id": "current-month",
        "goal": "Show this month's budget summary",
        "argv": ["months", "get", "current"],
        "facts": [['"current"', "current month"]],
    },
    {
        "id": "budget-category",
        "goal": "Assign 250.00 to a category for November 2026",
        "argv": ["categories", "update-month", "2026-11-01", "CATEGORY_ID", "--budgeted", "250000"],
        "facts": [["--budgeted", "budgeted"], ["milliunits"]],
    },
    {
        "id": "create-transaction",
        "goal": "Record a 12.34 purchase at a cafe",
        "argv": [
            "transactions",
            "create",
            "--transaction",
            '{"account_id":"ACCOUNT_ID","date":"2026-11-15","amount":-12340,"payee_name":"Cafe"}',
        ],
        "facts": [["--transaction", "transaction"], ["account_id"], ["amount"], ["milliunits"]],
    },
    {
        "id": "approve-transaction",
        "goal": "Approve one transaction",
        "argv": ["transactions", "update", "TRANSACTION_ID", "--transaction", '{"approved":true}'],
        "facts": [["--transaction", "transaction"], ["approved"]],
    },
]


def command_of(cat: Catalog, argv: list[str]) -> str:
    best = ""
    for command in cat.entries:
        words = command.split()
        if argv[: len(words)] == words and len(command) > len(best):
            best = command
    return best


# Public GitHub REST responses (this repository): the shapes an agent typically reads back.
RESPONSES = {
    "repository (object)": "https://api.github.com/repos/mikejhill/codapact",
    "30 pull requests (list)": (
        "https://api.github.com/repos/mikejhill/codapact/pulls?state=all&per_page=30"
    ),
    "30 commits (list)": "https://api.github.com/repos/mikejhill/codapact/commits?per_page=30",
    "10 releases (list)": "https://api.github.com/repos/mikejhill/codapact/releases?per_page=10",
}


def response_sizes() -> dict[str, dict[str, int]] | None:
    """Tokens of real responses printed indented (0.9.1) and compact (now, to a pipe)."""
    out = {}
    for label, url in RESPONSES.items():
        request = urllib.request.Request(  # noqa: S310 - fixed https URLs
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "codapact-discovery-bench",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:  # noqa: S310 - fixed https
                value = json.loads(reply.read())
        except OSError as error:
            print(f"responses: {url}: {error}", file=sys.stderr)
            return None
        pretty = json.dumps(value, indent=2, ensure_ascii=False)
        compact = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        out[label] = {
            "pretty": tokens(pretty),
            "compact": tokens(compact),
            "pretty_bytes": len(pretty.encode()),
            "compact_bytes": len(compact.encode()),
        }
    return out


def uvx_latency(runs: int = 3) -> dict[str, float] | None:
    """Warm wall-clock seconds per call of the published coda through uvx (optional)."""
    if not shutil.which("uvx"):
        return None
    out = {}
    for label, argv in {
        "describe --brief": ["describe", "--brief"],
        "describe transactions create": ["describe", "transactions", "create"],
        "transactions create --help": ["transactions", "create", "--help"],
    }.items():
        times = []
        for _ in range(runs + 1):
            start = time.perf_counter()
            done = subprocess.run(
                ["uvx", "-q", "codapact-ynab", *argv], capture_output=True, text=True
            )
            times.append(time.perf_counter() - start)
            if done.returncode != 0:
                return None
        out[label] = round(statistics.median(times[1:]), 2)
    return out


# --------------------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--github", action="store_true", help="include the full GitHub REST description"
    )
    parser.add_argument(
        "--no-latency", action="store_true", help="skip timing the published coda via uvx"
    )
    parser.add_argument(
        "--no-responses", action="store_true", help="skip fetching public GitHub responses"
    )
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="discovery-bench-"))
    result: dict[str, Any] = {
        "codapact": subprocess.run(
            [sys.executable, "-m", "codapact", "--version"], capture_output=True, text=True
        ).stdout.strip(),
        "commit": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip(),
        "tokenizer": "anthropic==0.34.2 tokenizer.json (legacy Claude BPE)",
        "techniques": TECHNIQUES,
        "skill_tokens": tokens(SKILL.read_text("utf-8").split("---", 2)[2]),
        "apis": {},
    }
    for api in apis(args.github):
        print(f"{api.key}: generating", file=sys.stderr)
        src = generate(api, work)
        cat = catalog(api, src, work)
        commands = list(cat.entries)
        step = max(1, len(commands) // SAMPLE)
        sample = commands[::step][:SAMPLE]
        per_command = {}
        for command in sample:
            entry = cat.entries[command]
            facts = [
                [a["flag"] if a["flag"].startswith("--") else a["name"], a["name"]]
                for a in entry["arguments"]
                if a.get("required")
            ]
            for a in entry["arguments"]:  # a JSON body needs its schema's required fields
                schema = a.get("schema") or {}
                ref = (schema.get("$ref") or "").split("/")[-1]
                body = (schema.get("$defs") or {}).get(ref, schema)
                facts += [[name] for name in body.get("required", [])[:3]]
            per_command[command] = {
                k: {f: v for f, v in plan.measure(facts).items() if f != "steps"}
                for k, plan in cat.plans(command).items()
            }
        lean = {
            c: {k: v for k, v in e.items() if k not in ("output_schema", "output_note")}
            for c, e in cat.entries.items()
            if c in sample
        }
        entry_sizes = {
            c: {
                "v091": tokens(json.dumps(cat.entries[c], indent=2, ensure_ascii=False)),
                "v091_args": tokens(json.dumps(lean[c], indent=2, ensure_ascii=False)),
                "now": tokens(json.dumps(lean[c], separators=(",", ":"), ensure_ascii=False)),
            }
            for c in lean
        }
        record: dict[str, Any] = {
            "title": api.title,
            "program": cat.program,
            "commands": len(commands),
            "groups": len({c.split()[0] for c in commands if " " in c}),
            "sizes": {
                "brief": tokens(cat.brief),
                "brief_v091": tokens(cat.brief_v091),
                "full": tokens(cat.full),
                "root_help": tokens(cat.root_help),
                "mcp_all": tokens(cat.mcp_all),
                "mcp_names": tokens(cat.mcp_names),
                "openapi": tokens(cat.spec_text) if cat.spec_text else None,
                "openapi_bytes": len(cat.spec_text.encode()) if cat.spec_text else None,
                "llms_index": tokens(cat.llms_index) if cat.llms_index else None,
                "llms_full": tokens(cat.llms_full) if cat.llms_full else None,
                **{
                    f"describe_command_{key}_median": statistics.median(
                        v[key] for v in entry_sizes.values()
                    )
                    for key in ("now", "v091", "v091_args")
                },
            },
            "per_command": per_command,
        }
        if api.key == "ynab":
            tasks = []
            os.environ["YNAB_BEARER"] = "placeholder"
            for task in YNAB_TASKS:
                command = command_of(cat, task["argv"])
                code, text = cat.cli.run(*task["argv"], "--dry-run")
                tasks.append(
                    {
                        **{k: task[k] for k in ("id", "goal", "facts")},
                        "call": f"{cat.program} " + " ".join(task["argv"]),
                        "command": command,
                        "dry_run_exit": code,
                        "results": {
                            k: plan.measure(task["facts"]) for k, plan in cat.plans(command).items()
                        },
                    }
                )
                if code != 0:
                    raise SystemExit(
                        f"{task['id']}: the reference call fails --dry-run: {text[:300]}"
                    )
            record["tasks"] = tasks
            record["latency_uvx"] = None if args.no_latency else uvx_latency()
        result["apis"][api.key] = record
        print(f"{api.key}: {len(commands)} commands, {len(sample)} measured", file=sys.stderr)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    result["responses"] = None if args.no_responses else response_sizes()
    OUT.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", "utf-8")
    shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {OUT.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
