"""Small, incremental coverage time series; no UI traces or repeated target lists."""

import csv
import os


class ActivityCoverageTimeline:
    def __init__(self, log_dir="logs", sample_interval_seconds=60):
        self.path = os.path.join(log_dir, "coverage_timeline.csv")
        self.sample_interval_seconds = sample_interval_seconds
        self._last_sample_elapsed = None

    def should_sample(self, elapsed_seconds):
        return (self._last_sample_elapsed is None
                or elapsed_seconds - self._last_sample_elapsed >= self.sample_interval_seconds)

    def sample(self, coverage_result, elapsed_seconds, reason="periodic"):
        if reason == "periodic" and not self.should_sample(elapsed_seconds):
            return False
        rows = [{"elapsed_seconds": round(elapsed_seconds, 3), "reason": reason, "scope": scope,
                 **{key: metrics.get(key) for key in (
                     "activity_cov", "method_cov", "class_cov", "covered_activities", "known_activities",
                     "covered_methods", "known_code_units", "covered_classes", "known_classes")}}
                for scope, metrics in coverage_result.items()]
        if not rows:
            return False
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        exists = os.path.exists(self.path)
        with open(self.path, "a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            if not exists:
                writer.writeheader()
            writer.writerows(rows)
        self._last_sample_elapsed = elapsed_seconds
        return True
