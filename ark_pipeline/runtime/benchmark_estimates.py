"""Explicit workload models for the post-acquisition pipeline benchmark."""

from __future__ import annotations

import math
from collections import defaultdict

import h3

SIZE_BREAKS = (1e-10, 1e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 3e-4,
               1e-3, 3e-3, 1e-2, 3e-2, 1e-1, 3e-1, 1., 3., 10., 30.,
               100., 300., 1000., 3000., 10000.)


def estimate(stages: list[dict], population: dict, pairs: dict, lists: dict) -> dict:
    """Project measured work; scenario ranges are not statistical confidence intervals.

    Forced minimum/maximum polygons exercise the kernel and downstream stages,
    but are not random observations. Use them only in a completely sampled band.
    """
    groups = defaultdict(list)
    for row in pairs["observations"]:
        if not 0 <= row["size_bin"] < len(population["size_bin_counts"]) or population["size_bin_counts"][row["size_bin"]] == 0:
            raise ValueError("Sample contains an observation outside the current population")
        groups[row["size_bin"]].append(row)
    bands = []
    global_kernel = global_pairs = 0.0
    missing = []
    for band, count in enumerate(population["size_bin_counts"]):
        if not count:
            continue
        all_rows = groups[band]
        if len(all_rows) > count:
            raise ValueError(f"Sample exceeds the current population in band {band}")
        rows = all_rows if len(all_rows) == count else [r for r in all_rows if not r["forced_extreme"]]
        if not rows:
            missing.append(band)
            bands.append({"size_bin": band, "population": count, "observations": 0})
            continue
        seconds = sum(r["kernel_seconds"] for r in rows) / len(rows)
        cells = sum(r["output_pairs"] for r in rows) / len(rows)
        global_kernel += seconds * count
        global_pairs += cells * count
        bands.append({"size_bin": band, "population": count, "observations": len(rows),
                      "mean_kernel_seconds": seconds, "mean_output_pairs": cells})
    result = {
        "status": "incomplete" if missing else "planning-estimate",
        "missing_size_bins": missing,
        "bands": bands,
        "assumptions": [
            "Size-band means represent other eligible polygons in that band; forced extremes are excluded unless the entire band was sampled.",
            "Pair generation retains measured worker utilization and write throughput. The full source scan is added separately, including checksum verification and WKB decoding.",
            "The sample duplicate rate and species-per-cell density are extrapolated; distinct cell counts are capped by the number of H3 cells on Earth. Global overlap may differ substantially.",
            "Crosswalk, boundary preparation and metadata use full reference data: their measured costs are added once.",
            "Sorting and tile compilation scenarios span linear and N log N work; metrics span cell-count and relationship-count scaling. These are sensitivity scenarios, not confidence intervals or guaranteed bounds.",
            "Disk spilling, OS caches, memory pressure, spatial overlap and changes in worker utilization can invalidate these projections. Acquisition and database loading are outside this command's scope.",
        ],
        "stages": [],
        "total_seconds": None,
        "scenario_seconds": None,
    }
    fixed = {"source_scan", "crosswalk", "boundaries", "metadata"}
    if missing:
        result["stages"] = [{"name": s["name"], "estimated_seconds": s["wall_seconds"] if s["name"] in fixed else None}
                            for s in stages if s["name"] != "sample_setup"]
        return result
    raw = pairs["pair_rows"]
    if not raw or not global_pairs:
        raise ValueError("No spatial relationships available to estimate downstream work")
    raw_scale = global_pairs / raw
    projected = {"raw_pairs": global_pairs}
    for resolution in (3, 7):
        for unit in ("cells", "relationships"):
            key = f"res{resolution}_{unit}"
            projected[key] = lists[key] * raw_scale
            if unit == "cells":
                projected[key] = min(h3.get_num_cells(resolution), projected[key])
    result["projected_workload"] = projected
    kernel_sum = sum(r["kernel_seconds"] for r in pairs["observations"])
    if kernel_sum <= 0:
        raise ValueError("No positive kernel timing measurements")
    for stage in stages:
        name, elapsed = stage["name"], stage["wall_seconds"]
        if name == "sample_setup":
            continue
        if name in fixed:
            low = point = high = elapsed
            model = "measured full reference/source workload; no scaling"
        elif name == "pairs":
            write = min(elapsed, pairs["write_seconds"])
            low = point = high = (elapsed - write) * global_kernel / kernel_sum + write * raw_scale
            model = "stratified kernel time at observed utilization + raw-pair write throughput"
        elif name in {"coarse_db", "coarse_cache", "fine_metrics"}:
            resolution = 7 if name == "fine_metrics" else 3
            scales = [projected[f"res{resolution}_{unit}"] / lists[f"res{resolution}_{unit}"]
                      for unit in ("cells", "relationships")]
            low, high = elapsed * min(scales), elapsed * max(scales)
            point = (low + high) / 2
            model = f"midpoint of res{resolution} cell and relationship throughput scenarios"
        else:
            if name == "tiles":
                sample_work = lists["res3_cells"] + lists["res7_cells"]
                full_work = projected["res3_cells"] + projected["res7_cells"]
                unit = "tile features"
            else:
                sample_work, full_work, unit = raw, global_pairs, "raw pairs"
            scale = full_work / sample_work
            scenarios = [elapsed * scale]
            if name in {"lists", "tiles"}:
                scenarios.append(elapsed * scale * math.log2(max(2, full_work)) / math.log2(max(2, sample_work)))
            low, high = min(scenarios), max(scenarios)
            point = scenarios[0]
            model = f"{unit} throughput" + ("; N log N sensitivity" if len(scenarios) > 1 else "")
        result["stages"].append({"name": name, "estimated_seconds": point,
                                 "scenario_seconds": [low, high], "model": model})
    result["total_seconds"] = sum(s["estimated_seconds"] for s in result["stages"])
    result["scenario_seconds"] = [sum(s["scenario_seconds"][i] for s in result["stages"]) for i in (0, 1)]
    return result


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "unavailable"
    return f"{seconds / 3600:.2f} h" if seconds >= 3600 else f"{seconds / 60:.2f} min" if seconds >= 60 else f"{seconds:.2f} s"


def markdown_report(report: dict) -> str:
    projection = report.get("estimate", {})
    by_name = {s["name"]: s for s in projection.get("stages", [])}
    lines = ["# Pipeline benchmark", "", f"Status: **{report['status']}**", "",
             f"Output: `{report['output']}`", "",
             f"Workers: `{report['resources']}`", ""]
    if report.get("selection"):
        selection = report["selection"]
        lines += [f"Sample: **{selection['selected_rows']:,} selected polygons** from the {selection['original_rows']:,}-row fixture. "
                  f"Current-policy exclusions: `{selection['excluded_by_current_policy']}`.", ""]
    if report.get("workload"):
        work = report["workload"]
        lines += [f"Observed output: **{work['raw_pairs']:,} raw pairs**, **{work['res7_relationships']:,} distinct fine relationships**, "
                  f"**{work['res7_cells']:,} fine cells** and **{work['res3_cells']:,} coarse cells**. "
                  f"Current full population: **{work['eligible_polygons']:,} eligible polygons**.", ""]
    lines += ["| Stage | Measured wall time | Estimated full build | Model |",
             "| --- | ---: | ---: | --- |"]
    for stage in report["stages"]:
        projected = by_name.get(stage["name"], {})
        lines.append(f"| {stage['name']} ({stage['status']}) | {duration(stage.get('wall_seconds'))} | "
                     f"{duration(projected.get('estimated_seconds'))} | {projected.get('model', 'not projected')} |")
    lines += ["", f"Measured benchmark total (including setup): **{duration(report.get('wall_seconds'))}**.",
              f"Estimated full post-acquisition build: **{duration(projection.get('total_seconds'))}**."]
    if projection.get("scenario_seconds"):
        lines += [f"Model sensitivity: **{duration(projection['scenario_seconds'][0])}–{duration(projection['scenario_seconds'][1])}** (not a confidence interval)."]
    if projection.get("missing_size_bins"):
        lines += [f"No full estimate: missing representative observations in size bands {projection['missing_size_bins']}."]
    lines += ["", *[f"- {message}" for message in report.get("warnings", []) + projection.get("assumptions", [])]]
    if report.get("error"):
        lines += ["", f"Failure: {report['error']}"]
    return "\n".join(lines) + "\n"
