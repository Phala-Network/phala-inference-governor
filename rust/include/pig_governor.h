#ifndef PIG_GOVERNOR_H
#define PIG_GOVERNOR_H
#include <stdint.h>

#define PIG_GOVERNOR_ADMISSION_FIT 0u
#define PIG_GOVERNOR_ADMISSION_REFERENCE_DISABLED 1u
#define PIG_GOVERNOR_ADMISSION_TPS_RISK 2u
#define PIG_GOVERNOR_ADMISSION_COLD_PRIOR 3u
#define PIG_GOVERNOR_ADMISSION_UNKNOWN 4u
#define PIG_GOVERNOR_ADMISSION_WAITING_LIMIT 5u
#define PIG_GOVERNOR_ADMISSION_AGGREGATE_TPS_RISK 6u

#ifdef __cplusplus
extern "C" {
#endif

/* ABI v4: caller provides valid pointers; free must not race another call.
 * Status: 0 success, 1 invalid input, 2 revision conflict, 3 internal failure.
 * Outputs and state remain unchanged on invalid input or revision conflict.
 * new starts revision at 1 and receives the native max running request bound.
 * All times use one monotonic seconds clock.
 * Buckets quantize the oldest window edge by less than 0.5 seconds.
 * Python owns epoch checks, request lifecycle and exactly-once Decode deltas.
 * Snapshot/choose and observe_batch accrue aggregate time from the preceding
 * active count. observe_batch uses explicit sequence-seconds only for the
 * response-surface cell, so an intervening snapshot cannot double-count time.
 * choose output: 0 native, 1 bounded Decode preference (never admission).
 * observe_surface records one real Decode cell before state transition.
 * duration arguments are total Decode sequence-seconds, not elapsed wall-time.
 * A positive token delta with zero sequence-seconds is invalid evidence.
 * observe_batch commits surface and aggregate observations in one transaction.
 * Imported profile cells are deadline-bound priors and never become live
 * rolling-window evidence. Exact keys precede jointly-heavier fallback keys.
 * Admission observed is one only when selected live evidence constrains or is
 * qualified for the selected key; prior-only fits use COLD_PRIOR.
 * AGGREGATE_TPS_RISK is a negative-only current-active-run live bound;
 * evidence_concurrency is zero because it is not projected-cell evidence.
 * The public Snapshot aggregate retains cross-run history and is not the exact
 * arithmetic source for AGGREGATE_TPS_RISK.
 * A zero-cell profile uses a null cells pointer; nonzero count requires cells.
 * export_profile returns only currently qualified live surface cells.
 * start_surface_epoch requires active_after <= max_running_requests.
 */
typedef struct PigGovernor PigGovernor;
typedef struct {
    uint32_t abi_version;
    uint32_t observed;
    uint64_t revision;
    double reference;
    uint64_t tokens_60s;
    double sequence_seconds_60s;
    uint64_t tokens_2s;
    double sequence_seconds_2s;
    uint64_t active_sequences;
    double last_prefill_end; /* -1 when unknown */
    double last_prefill_wall;
    double preference_until; /* last evaluated deadline, 0 if unavailable */
} PigGovernorSnapshot;

typedef struct {
    uint32_t abi_version;
    uint32_t allowed;
    uint32_t reason;
    uint32_t observed;
    double reference;
    double conservative_tps;
    double projected_tps;
    uint32_t projected_concurrency;
    uint32_t pressure_class;
    uint32_t evidence_concurrency;
    uint32_t evidence_pressure_class;
    uint64_t active_sequences;
} PigGovernorAdmission;

typedef struct {
    uint32_t concurrency;
    uint32_t pressure_class;
    uint64_t long_tokens;
    double long_seconds;
    uint64_t short_tokens;
    double short_seconds;
    double approved_lower_tps;
} PigGovernorProfileCellV1;

uint32_t pig_governor_abi_version(void);
int32_t pig_governor_new(double reference, uint32_t max_running_requests,
                       PigGovernor **out);
int32_t pig_governor_new_with_profile(
    double reference, uint32_t max_running_requests, double now, double ttl,
    const PigGovernorProfileCellV1 *cells, uint32_t count, PigGovernor **out);
int32_t pig_governor_free(PigGovernor *handle);
int32_t pig_governor_observe(PigGovernor *handle, double now, uint64_t delta,
                           uint64_t active_after);
int32_t pig_governor_observe_surface(PigGovernor *handle, double now,
                                      uint64_t delta, double sequence_seconds,
                                      uint32_t concurrency,
                                      uint32_t pressure_class);
int32_t pig_governor_observe_batch(PigGovernor *handle, double now,
                                  uint64_t delta, double sequence_seconds,
                                  uint32_t concurrency,
                                  uint32_t pressure_class,
                                  uint64_t active_after);
/* Additive ABI-v4 extension required by the matching Python adapter.
 * Every prior active request must have retired (caller owns identities).
 * delta includes only retiring-set tokens with positive sequence_seconds;
 * zero sequence_seconds requires delta == 0. Entrant tokens are deferred.
 * Atomically records old evidence and resets only private active-run evidence.
 * concurrency must equal the nonzero prior active count.
 */
int32_t pig_governor_observe_replacement(PigGovernor *handle, double now,
                                        uint64_t delta, double sequence_seconds,
                                        uint32_t concurrency,
                                        uint32_t pressure_class,
                                        uint64_t active_after);
int32_t pig_governor_prefill(PigGovernor *handle, double now, double wall);
int32_t pig_governor_update_reference(PigGovernor *handle,
                                    uint64_t expected_revision, double reference);
int32_t pig_governor_start_surface_epoch(PigGovernor *handle, double now,
                                         uint64_t active_after);
int32_t pig_governor_export_profile(PigGovernor *handle, double now,
                                    PigGovernorProfileCellV1 *buffer,
                                    uint32_t capacity, uint32_t *out_count);
int32_t pig_governor_snapshot(PigGovernor *handle, double now,
                            PigGovernorSnapshot *out);
int32_t pig_governor_admit(PigGovernor *handle, double now,
                           uint32_t projected_concurrency,
                           uint32_t pressure_class,
                           PigGovernorAdmission *out);
int32_t pig_governor_choose(PigGovernor *handle, double now, uint32_t decode,
                          uint32_t prefill, double oldest_age, uint32_t *out);

#ifdef __cplusplus
}
#endif
#endif
