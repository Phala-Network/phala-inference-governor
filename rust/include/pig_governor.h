#ifndef PIG_GOVERNOR_H
#define PIG_GOVERNOR_H
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ABI v1: caller provides valid pointers; free must not race another call.
 * Status: 0 success, 1 invalid input, 2 revision conflict, 3 internal failure.
 * Outputs and state remain unchanged on invalid input or revision conflict.
 * new starts revision at 1. All times use one monotonic seconds clock.
 * Buckets quantize the oldest window edge by less than 0.5 seconds.
 * Python owns epoch checks, request lifecycle and exactly-once Decode deltas.
 * Snapshot/choose accrue actual time using the preceding active count.
 * choose output: 0 native, 1 bounded Decode preference (never admission).
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

uint32_t pig_governor_abi_version(void);
int32_t pig_governor_new(double reference, PigGovernor **out);
int32_t pig_governor_free(PigGovernor *handle);
int32_t pig_governor_observe(PigGovernor *handle, double now, uint64_t delta,
                           uint64_t active_after);
int32_t pig_governor_prefill(PigGovernor *handle, double now, double wall);
int32_t pig_governor_update_reference(PigGovernor *handle,
                                    uint64_t expected_revision, double reference);
int32_t pig_governor_snapshot(PigGovernor *handle, double now,
                            PigGovernorSnapshot *out);
int32_t pig_governor_choose(PigGovernor *handle, double now, uint32_t decode,
                          uint32_t prefill, double oldest_age, uint32_t *out);

#ifdef __cplusplus
}
#endif
#endif
