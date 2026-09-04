"""Readable, one-shot source and spatial readiness reports."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

CYAN, MUTED, GREEN, AMBER = "#69d2e7", "#8796aa", "#91d7a3", "#e5c07b"
PRESENT = {"present", "present-unverified"}
LABELS = {
    "present": ("✓ Present", GREEN),
    "present-unverified": ("✓ Present*", GREEN),
    "needs-attention": ("! Attention", AMBER),
    "missing": ("· Not built", MUTED),
    "stale": ("↻ Stale", AMBER),
    "partial": ("◐ Partial", AMBER),
    "blocked-by-archives": ("· Needs pairs", MUTED),
    "blocked-by-relations": ("· Needs relations", MUTED),
    "not-required-for-direct-aggregation": ("— Not required", MUTED),
}


def badge(status: str) -> Text:
    label, color = LABELS.get(status, (status.replace("-", " ").capitalize(), AMBER))
    return Text(label, style=color, no_wrap=True)


def count_card(label: str, complete: int, total: int):
    text = Text(f"{complete:,} / {total:,}", style=f"bold {GREEN if total and complete == total else CYAN}")
    text.append(" present", style=MUTED)
    return Panel(Group(text, ProgressBar(total=max(1, total), completed=complete, complete_style=CYAN, finished_style=GREEN)),
                 title=label, border_style=MUTED, height=4)


def render_status(report: dict, *, width: int):
    sources, archives = report["sources"], report["archives"]
    source_count = sum(item["status"] in PRESENT for item in sources)
    archive_count = sum(item["status"] in PRESENT for item in archives)
    complete = report["status"] == "present-unverified"
    heading = Text("A R K  /  DATA STATUS", style=f"bold {CYAN}")
    heading.append("    OUTPUTS PRESENT*" if complete else "    NEEDS WORK", style=GREEN if complete else AMBER)
    header = Panel(Group(heading, Text("Source & spatial readiness · " + datetime.now().strftime("%d %b %Y · %H:%M:%S"), style=MUTED)),
                   border_style=CYAN)

    totals = Table.grid(expand=True, padding=(0, 1))
    totals.add_column(ratio=1)
    totals.add_column(ratio=1)
    totals.add_row(count_card("SOURCE DOWNLOADS", source_count, len(sources)), count_card("GENERATED PAIR OUTPUTS", archive_count, len(archives)))

    source_table = Table(expand=True, box=box.SIMPLE, show_edge=False, header_style=MUTED)
    source_table.add_column("Source", ratio=2)
    source_table.add_column("Status")
    source_table.add_column("Details", ratio=3)
    for item in sources:
        errors = item.get("errors", [])
        detail = "; ".join(errors[:2]) if errors else "Metadata & size checks passed"
        if len(errors) > 2:
            detail += f" · +{len(errors) - 2} more (see --json)"
        source_table.add_row(Text(item["source"]), badge(item["status"]), Text(detail, style=MUTED))

    stage_table = Table.grid(expand=True, padding=(0, 2))
    stage_table.add_column(ratio=1)
    stage_table.add_column()
    pairs_status = "present-unverified" if archives and archive_count == len(archives) else "stale" if any(a["status"] == "stale" for a in archives) else "partial" if archive_count else "missing"
    stage_table.add_row("Polygons, points & basins → H3", badge(pairs_status))
    stage_table.add_row("Deduplicate & group", badge(report.get("aggregation", report["serving_lists"])))
    stage_table.add_row("Serving species lists", badge(report["serving_lists"]))
    if report["relations"] != "not-required-for-direct-aggregation":
        stage_table.add_row("Intermediate relations", badge(report["relations"]))

    archive_table = Table(expand=True, box=box.SIMPLE, show_edge=False, header_style=MUTED)
    columns = 2 if width >= 110 else 1
    for _ in range(columns):
        archive_table.add_column("Source ZIP", ratio=1)
        archive_table.add_column("Generated pairs")
    rows = (len(archives) + columns - 1) // columns
    for index in range(rows):
        cells = []
        for column in range(columns):
            offset = index + column * rows
            if offset < len(archives):
                item = archives[offset]
                cells.extend([Text(item["archive"]), badge(item["status"])])
            else:
                cells.extend(["", ""])
        archive_table.add_row(*cells)
    counts = Counter(item["status"] for item in archives)
    archive_summary = " · ".join(f"{count:,} {'not built' if status == 'missing' else status.replace('-', ' ')}" for status, count in sorted(counts.items()))
    archive_content = Group(Text("These are generated H3 pair outputs. ZIP download status is shown above.", style=MUTED), archive_table)
    archive_panel = Panel(archive_content if archives else Text("No spatial archives registered yet. Acquire the source data first.", style=MUTED),
                          title="PAIR GENERATION BY SOURCE ZIP", subtitle=Text(archive_summary, style=MUTED) if archives else None, border_style=MUTED)

    action = Text(report["next_command"], style=f"bold {CYAN}")
    action.append("\n" + report["verification"], style=MUTED)
    action.append("\n* Present does not mean checksum-verified. Metrics and tiles are outside this status report.", style=MUTED)
    paths = Text("Data  ", style=MUTED)
    paths.append(report["data_root"] + "\n", style="default")
    paths.append("Pairs ", style=MUTED)
    paths.append(report["output_root"], style="default")
    return Group(header, totals, Panel(source_table, title="SOURCE INVENTORY", border_style=MUTED),
                 Panel(stage_table, title="SPATIAL STAGES", border_style=MUTED), archive_panel,
                 Panel(action, title="NEXT STEP", border_style=CYAN), paths)


def print_status(report: dict, console: Console | None = None):
    console = console or Console()
    console.print(render_status(report, width=console.width))
