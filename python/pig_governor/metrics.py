"""Bounded native telemetry, published into SGLang's multiprocess registry."""
import logging
import math
import time

from .scheduler import ADMISSION_REASON_NAMES


logger = logging.getLogger(__name__)

GAUGES = {
    "available": "Native Governor telemetry implementation is available.",
    "enabled": "Native Governor admission is enabled on this scheduler.",
    "state_valid": "The last telemetry read succeeded; check sample age separately.",
    "sample_timestamp_seconds": "Unix timestamp of the last successful telemetry read.",
    "tps_reference": "Configured soft per-sequence decode TPS target.",
    "running_limit": "Configured hard running-request limit.",
    "waiting_limit": "Configured hard native waiting-owner limit.",
    "native_running_limit": "Resolved native scheduler running-request limit.",
    "outstanding_requests": "Live Governor request reservations, including queued work.",
    "active_decode_sequences": "Live sequences with committed decode progress.",
    "waiting_requests": "Current native waiting, grammar and chunked-prefill owners; NaN if unknown.",
    "waiting_valid": "A current waiting-owner count is known.",
    "decode_tokens_60s": "Committed decode tokens in the rolling 60-second window (gauge).",
    "decode_sequence_seconds_60s": "Sequence exposure in the rolling 60-second window (gauge).",
    "evidence_observed": "The current controller epoch has observed decode feedback.",
    "online_cells_qualified": "Fresh qualified online response-surface cells; excludes offline priors.",
    "profile_cells_valid": "Unexpired optional offline prior cells usable in the current identity.",
    "coverage_cells_total": "Possible response-surface cells: native running limit times four pressure classes.",
    "surface_ready": "At least one usable online or prior cell exists; not full workload coverage or SLO readiness.",
    "identity_transition": "New admission is paused while an old runtime identity drains.",
}
COUNTERS = {
    "admission_attempts": "Native Governor admission calls, including reservation reuse.",
    "admission_rejects": "Native Governor admission calls rejected before queue insertion.",
    "admission_reuses": "Native admission calls that already own a reservation.",
    "surface_epoch_rotations": "Runtime identity changes that rotated the response-surface epoch.",
}
VALID_FLAGS = {"waiting_valid", "surface_ready"}


class GovernorMetrics:
    def __init__(self, labels, *, registry=None):
        # SGLang sets PROMETHEUS_MULTIPROC_DIR before constructing its collectors.
        from prometheus_client import REGISTRY, Counter, Gauge

        if set(labels) & {"reason", "window"}:
            raise ValueError("Governor labels overlap reserved telemetry labels")
        registry = REGISTRY if registry is None else registry
        self.gauges = {
            name: Gauge(
                "pig_governor_" + name, help_text, tuple(labels),
                multiprocess_mode="mostrecent", registry=registry,
            ).labels(**labels)
            for name, help_text in GAUGES.items()
        }
        self.counters = {
            name: Counter(
                "pig_governor_" + name, help_text, tuple(labels), registry=registry,
            ).labels(**labels)
            for name, help_text in COUNTERS.items()
        }
        reasons = Counter(
            "pig_governor_admission_rejections",
            "Native admission rejections by a fixed, bounded reason vocabulary.",
            (*labels, "reason"), registry=registry,
        )
        self.rejections = {
            reason: reasons.labels(**labels, reason=reason)
            for reason in ADMISSION_REASON_NAMES.values()
        }
        self.average_tps = {}
        self.average_tps_valid = {}
        tps = Gauge(
            "pig_governor_average_tps", "Rolling observed per-sequence decode TPS; NaN if unavailable.",
            (*labels, "window"), multiprocess_mode="mostrecent", registry=registry,
        )
        valid = Gauge(
            "pig_governor_average_tps_valid", "Rolling TPS has exposure and a usable runtime identity.",
            (*labels, "window"), multiprocess_mode="mostrecent", registry=registry,
        )
        for window in ("60s", "2s"):
            self.average_tps[window] = tps.labels(**labels, window=window)
            self.average_tps_valid[window] = valid.labels(**labels, window=window)
        self._published = dict.fromkeys(COUNTERS, 0)
        self._published_rejections = dict.fromkeys(self.rejections, 0)
        self._failed = False
        self.gauges["available"].set(1)
        self.gauges["enabled"].set(0)
        self.gauges["sample_timestamp_seconds"].set(0)
        self._invalidate()

    def _invalidate(self):
        self.gauges["state_valid"].set(0)
        for name, gauge in self.gauges.items():
            if name not in {"available", "enabled", "state_valid", "sample_timestamp_seconds"}:
                gauge.set(0 if name in VALID_FLAGS else math.nan)
        for window in self.average_tps:
            self.average_tps[window].set(math.nan)
            self.average_tps_valid[window].set(0)

    def publish(self, governor, *, now, waiting_count=None, wall_time=None):
        self.gauges["enabled"].set(int(governor is not None))
        if governor is None:
            self._invalidate()
            self.gauges["state_valid"].set(1)
            self.gauges["sample_timestamp_seconds"].set(time.time() if wall_time is None else wall_time)
            return
        try:
            snapshot = governor.telemetry_snapshot(now, waiting_count=waiting_count)
            policy = snapshot["policy"]
            admission = snapshot["admission"]
            transitioning = snapshot["identity_transition"]
            prior_cells = 0 if transitioning else policy["profile"]["cell_count"]
            values = {
                "tps_reference": policy["mutable"]["tps_reference"],
                "running_limit": policy["mutable"]["max_running"],
                "waiting_limit": policy["mutable"]["max_waiting"],
                "native_running_limit": policy["native_max_running_requests"],
                "outstanding_requests": admission["outstanding"],
                "active_decode_sequences": admission["active"],
                "waiting_requests": admission["waiting_count"],
                "waiting_valid": int(admission["waiting_count"] is not None),
                "decode_tokens_60s": policy["decode_tokens"],
                "decode_sequence_seconds_60s": policy["decode_sequence_seconds"],
                "evidence_observed": int(policy["observed"]),
                "online_cells_qualified": snapshot["online_cells_qualified"],
                "profile_cells_valid": prior_cells,
                "coverage_cells_total": policy["native_max_running_requests"] * 4,
                "surface_ready": int(not transitioning and (
                    snapshot["online_cells_qualified"] > 0 or prior_cells > 0
                )),
                "identity_transition": int(transitioning),
            }
            totals = {
                "admission_attempts": admission["attempts"],
                "admission_rejects": admission["rejects"],
                "admission_reuses": admission["reuses"],
                "surface_epoch_rotations": admission["surface_epoch_rotations"],
            }
            reason_totals = {
                name: admission["reject_reasons"].get(name, 0) for name in self.rejections
            }
            if any(totals[name] < self._published[name] for name in totals) or any(
                reason_totals[name] < self._published_rejections[name] for name in reason_totals
            ):
                raise ValueError("Native telemetry counters moved backwards")
        except Exception:
            # A telemetry fault must not interrupt admission or reveal request state.
            self._invalidate()
            if not self._failed:
                logger.warning("Governor telemetry sample unavailable")
            self._failed = True
            return
        for name, value in values.items():
            self.gauges[name].set(math.nan if value is None else value)
        for window, field in (("60s", "average_tps"), ("2s", "average_tps_2s")):
            value = None if transitioning else policy[field]
            self.average_tps[window].set(math.nan if value is None else value)
            self.average_tps_valid[window].set(int(value is not None))
        for name, total in totals.items():
            self.counters[name].inc(total - self._published[name])
        for name, total in reason_totals.items():
            self.rejections[name].inc(total - self._published_rejections[name])
        self._published = totals
        self._published_rejections = reason_totals
        self._failed = False
        self.gauges["state_valid"].set(1)
        self.gauges["sample_timestamp_seconds"].set(time.time() if wall_time is None else wall_time)
