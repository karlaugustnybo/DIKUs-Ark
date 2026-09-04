"""Benchmark priors updated by observed work, without extrapolating tiny polygons."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from ark_pipeline.runtime.provenance import sha256


def load_prior(root: Path, profile_sha: str, explicit: Path | None = None) -> tuple[dict | None, str]:
    paths = [explicit] if explicit else sorted((root / "benchmarks/pipeline").glob("*/benchmark-report.json"), reverse=True)
    manifest = root / "acquisition/current.json"
    current = sha256(manifest) if manifest.is_file() else None
    for path in paths:
        try:
            report = json.loads(path.read_text())
            if report.get("status") != "passed" or report.get("profile_sha256") != profile_sha or report.get("acquisition_manifest_sha256") != current:
                if explicit:
                    raise ValueError("Benchmark report is incomplete or its source/profile fingerprints differ from this run")
                continue
            report["path"] = str(path)
            report["observations"] = json.loads(path.with_name("pairs-report.json").read_text())["observations"]
            report["population"] = json.loads(path.with_name("population.json").read_text())
            return report, f"Benchmark: {path.parent.name}"
        except (OSError, KeyError, ValueError):
            if explicit:
                raise ValueError(f"Cannot use benchmark report: {path}; a passed report with matching sources/profile and polygon timings is required") from None
    return None, "No compatible benchmark yet · calibrating from this run"


class Forecast:
    """Maintain actual observations independently of display refresh frequency."""
    def __init__(self, prior: dict | None, resources: dict, mode="benchmark"):
        self.prior, self.resources, self.mode = prior, resources, mode
        self.base = {}
        self.band_prior = {}
        self.counts = Counter()
        self.initial_counts = Counter()
        self.done = Counter()
        self.observed = defaultdict(list)
        self.active = {}
        self.active_partitions = {}
        self.work_history = {}
        self.completed_ids = set()
        self.special_costs = {}
        self.jobs = {}
        self.kernel_done = self.write_seconds = self.pair_rows = 0.0
        self.live_workload = {}
        self.progress = {}
        self.started = {}
        self.finished = {}
        if prior:
            for band in prior.get("estimate", {}).get("bands", []):
                if band.get("mean_kernel_seconds") is not None:
                    self.band_prior[band["size_bin"]] = (band["mean_kernel_seconds"], band["mean_output_pairs"])
                    if mode == "full":
                        self.counts[band["size_bin"]] = band["population"]
            self.base = {row["name"]: row.get("estimated_seconds") for row in prior.get("estimate", {}).get("stages", [])} if mode == "full" else {
                row["name"]: row["wall_seconds"] for row in prior["stages"]}
            # This is an initial parallelism assumption, not measured speedup.
            for name, key in {"pairs": "spatial_workers", "fine_metrics": "metric_workers", "tiles": "tile_threads",
                              "lists": "duckdb_threads", "coarse_db": "duckdb_threads", "coarse_cache": "duckdb_threads"}.items():
                if self.base.get(name) is not None:
                    self.base[name] *= prior["resources"][key] / resources[key]
        self.reference_base = self.base.copy()
        self.initial_counts = self.counts.copy()
        self.reference_work = (prior.get("estimate", {}).get("projected_workload", {}) if mode == "full" else prior.get("workload", {})) if prior else {}

    def set_selection(self, selection: dict):
        self.counts = Counter(row["size_bin"] for row in selection["rows"])
        self.initial_counts = self.counts.copy()
        self.jobs = {str(row["sample_id"]): row for row in selection["rows"]}
        if not self.prior:
            return
        previous = {str(row["sample_id"]): row for row in self.prior["observations"]}
        same_fixture = selection["sha256"] == self.prior.get("selection", {}).get("sha256")
        expected_kernel = expected_pairs = 0
        for key, row in self.jobs.items():
            observation = previous.get(key) if same_fixture else None
            if observation:
                cost = observation["kernel_seconds"], observation["output_pairs"]
            else:
                cost = self.band_prior.get(row["size_bin"])
            if cost is None:
                self.base = {k: v for k, v in self.base.items() if k in {"source_scan", "crosswalk", "metadata", "boundaries"}}
                return
            if row.get("forced_extreme") and observation:
                self.special_costs[key] = cost
            expected_kernel += cost[0]
            expected_pairs += cost[1]
        old_kernel = sum(row["kernel_seconds"] for row in previous.values())
        old_pairs = sum(row["output_pairs"] for row in previous.values())
        for name in ("pairs", "lists", "coarse_db", "coarse_cache", "fine_metrics", "prepared_inputs", "tiles"):
            if self.base.get(name) is not None:
                denominator = old_kernel if name == "pairs" else old_pairs
                if denominator > 0:
                    self.base[name] *= (expected_kernel if name == "pairs" else expected_pairs) / denominator
                else:
                    self.base.pop(name, None)

    def accept(self, event: dict):
        name, kind = event["stage"], event["kind"]
        if kind == "stage_start":
            self.started.setdefault(name, event["time"])
        elif kind == "stage_end":
            if event.get("status") == "passed":
                self.finished[name] = event.get("elapsed", event["time"] - self.started.get(name, event["time"]))
        elif kind == "geometry_start" and event.get("source_kind", "polygon") == "polygon":
            self.active[event["task"]] = event
        elif kind == "detail" and event.get("task") in self.active:
            self.active[event["task"]].update(fraction=event.get("fraction"),
                                              tile_workers=event.get("tile_workers", 1))
        elif kind == "detail" and name == "fine_metrics" and event.get("unit") == "partition":
            task = event["task"]
            row = self.active_partitions.setdefault(task, {"time": event["time"]})
            row["fraction"] = event.get("fraction")
        elif kind == "task_end" and name == "fine_metrics":
            self.active_partitions.pop(event.get("task"), None)
        elif kind == "geometry_done" and event.get("source_kind", "polygon") == "polygon":
            self.active.pop(event["task"], None)
            key = event["id"]
            if key in self.completed_ids:
                return
            self.completed_ids.add(key)
            band = event["size_bin"]
            self.done[band] += 1
            self.kernel_done += event["kernel_seconds"]
            self.pair_rows += event["output_pairs"]
            if not event.get("forced_extreme"):
                self.observed[band].append((event["kernel_seconds"], event["output_pairs"]))
        elif kind == "pair_write":
            self.write_seconds += event["seconds"]
        elif kind == "archive_reused":
            archived = next((a for a in (self.prior or {}).get("population", {}).get("archives", [])
                             if a["logical_name"] == event["logical_name"]), None)
            if archived and "size_bin_counts" in archived:
                self.counts.subtract(dict(enumerate(archived["size_bin_counts"])))
            else:
                self.counts.clear()
                self.base.pop("pairs", None)
        elif kind == "workload":
            self.live_workload.update(event["counts"])
            for stage, resolution in (("coarse_db", 3), ("coarse_cache", 3), ("fine_metrics", 7)):
                keys = [f"res{resolution}_{unit}" for unit in ("cells", "relationships")]
                if stage not in self.started and self.reference_base.get(stage) and all(self.reference_work.get(k) and self.live_workload.get(k) for k in keys):
                    self.base[stage] = self.reference_base[stage] * sum(self.live_workload[k] / self.reference_work[k] for k in keys) / 2
            keys = ["res3_cells", "res7_cells"]
            if "tiles" not in self.started and self.reference_base.get("tiles") and all(self.reference_work.get(k) and self.live_workload.get(k) for k in keys):
                self.base["tiles"] = self.reference_base["tiles"] * sum(self.live_workload[k] for k in keys) / sum(self.reference_work[k] for k in keys)
        elif kind == "work" and event.get("overall") and event.get("total", 0) > 0:
            previous = self.progress.get(name)
            history = self.work_history.setdefault(name, [])
            if previous and (previous.get("task") != event.get("task")
                             or previous["total"] != event["total"]
                             or previous["completed"] > event["completed"]):
                history.clear()
            if "time" in event:
                history.append((event["time"], event["completed"]))
                # Retain a recent throughput window rather than assuming that
                # the first (often tiny) partitions represent the final ones.
                while len(history) > 2 and history[1][0] < event["time"] - 30:
                    history.pop(0)
            self.progress[name] = event
        elif kind == "phase":
            self.progress.pop(name, None)
            self.work_history.pop(name, None)

    def snapshot(self):
        # The event offset makes completed events durable. Attempt-local IDs and
        # raw observations need not be rewritten for every polygon every second;
        # retain their per-band cost summary and all completed-work counters.
        bands = set(self.band_prior) | set(self.observed)
        return {**self.__dict__, "completed_ids": [], "observed": {},
                "band_prior": {band: self.band_cost(band) for band in bands}}

    @classmethod
    def restore(cls, data):
        model = cls(data["prior"], data["resources"], data["mode"])
        model.__dict__.update(data)
        for name in ("counts", "initial_counts", "done"):
            setattr(model, name, Counter({int(k): v for k, v in data[name].items()}))
        model.band_prior = {int(k): v for k, v in data["band_prior"].items()}
        model.observed = defaultdict(list, {int(k): v for k, v in data["observed"].items()})
        model.completed_ids = set(data["completed_ids"])
        return model

    def restart_stage(self, name, now):
        self.started[name] = now
        self.finished.pop(name, None)
        self.progress.pop(name, None)
        self.work_history.pop(name, None)
        if name == "fine_metrics":
            self.active_partitions.clear()
        if name == "pairs":
            # Retain learned cost per size band, but never count unpublished
            # polygons as durable output when an interrupted archive restarts.
            for band in self.observed:
                self.band_prior[band] = self.band_cost(band)
            self.observed.clear()
            self.counts = self.initial_counts.copy()
            self.done.clear()
            self.completed_ids.clear()
            self.active.clear()
            self.kernel_done = self.write_seconds = self.pair_rows = 0.0

    def band_cost(self, band):
        rows = self.observed[band]
        prior = self.band_prior.get(band)
        if not rows:
            return prior
        # Five equivalent observations stabilize startup, independently per band.
        weight = 5 if prior else 0
        return tuple((sum(row[i] for row in rows) + weight * (prior[i] if prior else 0)) / (len(rows) + weight) for i in (0, 1))

    def pair_remaining(self, elapsed):
        if not self.counts:
            return None
        if all(self.done[band] >= count for band, count in self.counts.items()):
            return None  # all polygons done; writing/checksums may still be running
        remaining_kernel = remaining_pairs = 0.0
        for band, count in self.counts.items():
            remaining = max(0, count - self.done[band])
            if not remaining:
                continue
            cost = self.band_cost(band)
            if cost is None:
                return None
            remaining_kernel += cost[0] * remaining
            remaining_pairs += cost[1] * remaining
        # Forced extreme fixtures get their individual measured prior, while
        # never influencing the population's ordinary per-band mean.
        for key, cost in self.special_costs.items():
            if key not in self.completed_ids:
                mean = self.band_cost(self.jobs[key]["size_bin"])
                if mean:
                    remaining_kernel += cost[0] - mean[0]
                    remaining_pairs += cost[1] - mean[1]
        active_time = 0.0
        longest_remaining = 0.0
        now = self.started.get("pairs", 0) + elapsed
        for row in self.active.values():
            cost = self.special_costs.get(row["id"]) or self.band_cost(row["size_bin"])
            if cost:
                age = max(0, now - row.get("time", now))
                fraction = row.get("fraction")
                if fraction is not None and 0 < fraction < 1:
                    # Grid progress measures this particular polygon. A long
                    # outlier must not inherit the mean of its faster peers.
                    projected = age * (1 - fraction) / fraction
                    left = max(projected, cost[0] - age, 0)
                elif cost[0] > age and not (row.get("forced_extreme") and row["id"] not in self.special_costs):
                    left = cost[0] - age
                else:
                    # An overdue task with no measurable phase progress has no
                    # defensible countdown (e.g. native sort/deduplication).
                    return None
                remaining_kernel += left - cost[0]
                active_time += age
                longest_remaining = max(longest_remaining, left)
        remaining_kernel = max(0, remaining_kernel)
        workers = self.resources["spatial_workers"]
        utilization = min(workers, (self.kernel_done + active_time) / max(0.01, elapsed - self.write_seconds)) if self.kernel_done else workers
        # Include observed serialization/dispatch overhead, but avoid a zero
        # utilization estimate during process startup.
        utilization = max(0.05, utilization)
        write_rate = self.write_seconds / self.pair_rows if self.pair_rows else 0
        jobs_left = sum(max(0, count - self.done[band]) for band, count in self.counts.items())
        utilization = min(utilization, jobs_left)
        return max(longest_remaining, remaining_kernel / utilization) + max(0, remaining_pairs) * write_rate

    def work_remaining(self, name, now):
        work = self.progress.get(name)
        if not work or not 0 < work["completed"] < work["total"]:
            return None
        elapsed = max(0, now - self.started.get(name, now))
        observed = elapsed * (work["total"] - work["completed"]) / work["completed"]
        history = self.work_history.get(name, [])
        if len(history) >= 2:
            start, completed = history[0]
            end, latest = history[-1]
            if end - start >= 5 and latest > completed:
                recent = (work["total"] - latest) * (now - start) / (latest - completed)
                observed = max(observed, recent)
        if name == "fine_metrics":
            for row in self.active_partitions.values():
                fraction = row.get("fraction")
                if fraction is not None and 0 < fraction < 1:
                    observed = max(observed, (now - row["time"]) * (1 - fraction) / fraction)
        return observed

    def remaining(self, name, now):
        if name in self.finished:
            return 0.0
        elapsed = max(0, now - self.started.get(name, now))
        work = self.progress.get(name)
        if name == "pairs" and name in self.started:
            # The benchmark models range polygons only. Point archives and
            # HydroBASINS phases expose exact progress, but a polygon-derived
            # countdown would be false precision after range work completes.
            if work and work.get("unit") != "polygons":
                return None
            calculated = self.pair_remaining(elapsed)
            # Once a pair plan exists, a missing band cannot be filled with a
            # whole-stage naive average or an unrelated historical sample.
            if calculated is not None or self.counts:
                return calculated
        baseline = self.base.get(name)
        observed = self.work_remaining(name, now)
        if work and work.get("scope") == "phase":
            # Streaming features excludes the compiler's subsequent zoom/index
            # passes. A phase estimate alone cannot predict stage completion.
            if baseline is None or baseline <= elapsed:
                return None
            return max(baseline - elapsed, observed or 0)
        if observed is not None:
            fraction = work["completed"] / work["total"]
            weight = min(1, fraction * 4)
            return observed if baseline is None else weight * observed + (1 - weight) * max(0, baseline - elapsed)
        # Do not display zero remaining merely because a prior deadline passed.
        if baseline is not None and baseline > elapsed:
            return baseline - elapsed
        return None

    def total(self, names, now):
        values = [self.remaining(name, now) for name in names]
        known = sum(value for value in values if value is not None)
        return (known if all(value is not None for value in values) else None), known
