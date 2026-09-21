//! Version 2 C ABI. The caller owns pointer validity, output storage and handle
//! lifetime; never free a handle during another call. Calls on a live handle
//! serialize internally. All numeric validation precedes committing state.
//! Request identity, epoch and exactly-once committed deltas belong to Python.
//! This core never owns requests, allocates KV, or predicts output tokens. Its
//! admission result is a conservative TPS forecast over observed Decode work.

use std::collections::BTreeMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Mutex;

pub const ABI_VERSION: u32 = 2;
pub const OK: i32 = 0;
pub const INVALID: i32 = 1;
pub const CONFLICT: i32 = 2;
pub const INTERNAL: i32 = 3;
pub const ADMISSION_FIT: u32 = 0;
pub const ADMISSION_REFERENCE_DISABLED: u32 = 1;
pub const ADMISSION_TPS_RISK: u32 = 2;
pub const ADMISSION_COLD_PRIOR: u32 = 3;
pub const ADMISSION_UNKNOWN: u32 = 4;

const BUCKET_SECONDS: f64 = 0.5;
const WINDOW_SECONDS: f64 = 60.0;
const SHORT_WINDOW_SECONDS: f64 = 2.0;
const BUCKETS: usize = 121;
const MAX_PREFERENCE_SECONDS: f64 = 0.25;
const MAX_CLOCK: f64 = 1_125_899_906_842_624.0;
const PRESSURE_CLASSES: u32 = 4;
const MIN_EXPOSURE_SECONDS: f64 = 0.1;
const MAX_SURFACE_AGE_SECONDS: f64 = 60.0;

type Result<T> = std::result::Result<T, i32>;

#[derive(Clone, Copy)]
struct Bucket {
    tick: i64,
    tokens: u64,
    seconds: f64,
}

const EMPTY: Bucket = Bucket {
    tick: -1,
    tokens: 0,
    seconds: 0.0,
};

/// All fields are fixed-width; unknown Prefill end is -1, unknown TPS is
/// represented by zero sequence-seconds (the caller must not divide by zero).
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Snapshot {
    pub abi_version: u32,
    pub observed: u32,
    pub revision: u64,
    pub reference: f64,
    pub tokens_60s: u64,
    pub sequence_seconds_60s: f64,
    pub tokens_2s: u64,
    pub sequence_seconds_2s: f64,
    pub active_sequences: u64,
    pub last_prefill_end: f64,
    pub last_prefill_wall: f64,
    pub preference_until: f64,
}

/// A pre-enqueue TPS forecast. The caller owns request and scheduler lifecycle;
/// this value carries only the atomic decision and evidence.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Admission {
    pub abi_version: u32,
    pub allowed: u32,
    pub reason: u32,
    pub observed: u32,
    pub reference: f64,
    pub conservative_tps: f64,
    pub projected_tps: f64,
    pub projected_concurrency: u32,
    pub pressure_class: u32,
    pub evidence_concurrency: u32,
    pub evidence_pressure_class: u32,
    pub active_sequences: u64,
}

#[derive(Clone)]
struct SurfaceCell {
    buckets: [Bucket; BUCKETS],
    last_time: Option<f64>,
    exposure_seconds: f64,
    last_qualified: Option<f64>,
}

impl SurfaceCell {
    fn new() -> Self {
        Self {
            buckets: [EMPTY; BUCKETS],
            last_time: None,
            exposure_seconds: 0.0,
            last_qualified: None,
        }
    }

    fn bucket(&mut self, tick: i64) -> &mut Bucket {
        let bucket = &mut self.buckets[tick as usize % BUCKETS];
        if bucket.tick != tick {
            *bucket = Bucket { tick, ..EMPTY };
        }
        bucket
    }

    fn evidence(&self, now: f64, lookback: f64) -> Result<(u64, f64)> {
        let first = tick(now - lookback);
        let last = tick(now);
        let mut tokens: u64 = 0;
        let mut seconds = 0.0;
        for bucket in &self.buckets {
            if bucket.tick >= 0 && first <= bucket.tick && bucket.tick <= last {
                tokens = tokens.checked_add(bucket.tokens).ok_or(INVALID)?;
                seconds += bucket.seconds;
            }
        }
        if !seconds.is_finite() {
            return Err(INVALID);
        }
        Ok((tokens, seconds))
    }

    fn observe(&mut self, now: f64, delta: u64, duration: f64) -> Result<()> {
        let duration = duration.min(WINDOW_SECONDS);
        let start = (now - duration).max(0.0);
        let mut cursor = start;
        while cursor < now {
            let index = tick(cursor);
            let end = (((index + 1) as f64) * BUCKET_SECONDS).min(now);
            if end <= cursor {
                return Err(INVALID);
            }
            let segment = end - cursor;
            self.bucket(index).seconds += segment;
            cursor = end;
        }
        self.bucket(tick(now)).tokens = self
            .bucket(tick(now))
            .tokens
            .checked_add(delta)
            .ok_or(INVALID)?;
        self.exposure_seconds += duration;
        self.last_time = Some(now);
        if self.exposure_seconds >= MIN_EXPOSURE_SECONDS {
            self.last_qualified = Some(now);
        }
        Ok(())
    }

    fn qualified(&self, now: f64) -> bool {
        self.exposure_seconds >= MIN_EXPOSURE_SECONDS
            && self
                .last_qualified
                .is_some_and(|qualified| now - qualified <= MAX_SURFACE_AGE_SECONDS)
    }

    fn lower_bound(&self, now: f64) -> Result<Option<f64>> {
        let (long_tokens, long_seconds) = self.evidence(now, WINDOW_SECONDS)?;
        let (short_tokens, short_seconds) = self.evidence(now, SHORT_WINDOW_SECONDS)?;
        let long_tps = if long_seconds > 0.0 {
            Some(long_tokens as f64 / long_seconds)
        } else {
            None
        };
        let short_tps = if short_seconds > 0.0 {
            Some(short_tokens as f64 / short_seconds)
        } else {
            None
        };
        Ok(match (short_tps, long_tps) {
            (Some(short), Some(long)) => Some(short.min(long)),
            (Some(short), None) => Some(short),
            (None, Some(long)) => Some(long),
            (None, None) => None,
        })
    }
}

#[derive(Clone)]
struct State {
    buckets: [Bucket; BUCKETS],
    last_time: Option<f64>,
    observed: bool,
    active: u64,
    revision: u64,
    reference: f64,
    prefill_end: Option<f64>,
    prefill_wall: f64,
    preference_until: f64,
    surface: BTreeMap<(u32, u32), SurfaceCell>,
}

fn nonnegative(value: f64) -> bool {
    value.is_finite() && value >= 0.0
}

fn tick(now: f64) -> i64 {
    (now / BUCKET_SECONDS).floor() as i64
}

impl State {
    fn new(reference: f64) -> Result<Self> {
        if !nonnegative(reference) {
            return Err(INVALID);
        }
        Ok(Self {
            buckets: [EMPTY; BUCKETS],
            last_time: None,
            observed: false,
            active: 0,
            revision: 1,
            reference,
            prefill_end: None,
            prefill_wall: 0.0,
            preference_until: 0.0,
            surface: BTreeMap::new(),
        })
    }

    fn bucket(&mut self, tick: i64) -> &mut Bucket {
        let bucket = &mut self.buckets[tick as usize % BUCKETS];
        if bucket.tick != tick {
            *bucket = Bucket { tick, ..EMPTY };
        }
        bucket
    }

    fn advance(&mut self, now: f64) -> Result<()> {
        if !nonnegative(now) || now >= MAX_CLOCK || self.last_time.is_some_and(|last| now < last) {
            return Err(INVALID);
        }
        if let Some(last) = self.last_time {
            let oldest = (tick(now - WINDOW_SECONDS) as f64 * BUCKET_SECONDS).max(0.0);
            let mut start = last.max(oldest);
            while self.active > 0 && start < now {
                let index = tick(start);
                let end = (((index + 1) as f64) * BUCKET_SECONDS).min(now);
                if end <= start {
                    return Err(INVALID);
                }
                let exposure = (end - start) * self.active as f64;
                self.bucket(index).seconds += exposure;
                start = end;
            }
        }
        self.last_time = Some(now);
        Ok(())
    }

    fn evidence(&self, now: f64, lookback: f64) -> Result<(u64, f64)> {
        let first = tick(now - lookback);
        let last = tick(now);
        let mut tokens: u64 = 0;
        let mut seconds = 0.0;
        for bucket in &self.buckets {
            if bucket.tick >= 0 && first <= bucket.tick && bucket.tick <= last {
                tokens = tokens.checked_add(bucket.tokens).ok_or(INVALID)?;
                seconds += bucket.seconds;
            }
        }
        if !seconds.is_finite() {
            return Err(INVALID);
        }
        Ok((tokens, seconds))
    }

    fn observe(&mut self, now: f64, delta: u64, active_after: u64) -> Result<()> {
        self.advance(now)?;
        self.bucket(tick(now)).tokens = self
            .bucket(tick(now))
            .tokens
            .checked_add(delta)
            .ok_or(INVALID)?;
        self.evidence(now, WINDOW_SECONDS)?;
        self.active = active_after;
        self.observed = true;
        Ok(())
    }

    fn observe_surface(
        &mut self,
        now: f64,
        delta: u64,
        duration: f64,
        concurrency: u32,
        pressure_class: u32,
    ) -> Result<()> {
        if concurrency == 0 || pressure_class >= PRESSURE_CLASSES || !nonnegative(duration) {
            return Err(INVALID);
        }
        self.advance(now)?;
        let cell = self
            .surface
            .entry((concurrency, pressure_class))
            .or_insert_with(SurfaceCell::new);
        cell.observe(now, delta, duration)?;
        self.observed = true;
        Ok(())
    }

    fn prefill(&mut self, now: f64, wall: f64) -> Result<()> {
        if !wall.is_finite() || wall <= 0.0 || wall > now {
            return Err(INVALID);
        }
        self.advance(now)?;
        self.prefill_end = Some(now);
        self.prefill_wall = wall;
        self.preference_until = 0.0;
        Ok(())
    }

    fn update_reference(&mut self, revision: u64, reference: f64) -> Result<()> {
        if !nonnegative(reference) {
            return Err(INVALID);
        }
        if revision != self.revision {
            return Err(CONFLICT);
        }
        self.revision = self.revision.checked_add(1).ok_or(INVALID)?;
        self.reference = reference;
        self.preference_until = 0.0;
        Ok(())
    }

    fn snapshot(&mut self, now: f64) -> Result<Snapshot> {
        self.advance(now)?;
        let (tokens_60s, sequence_seconds_60s) = self.evidence(now, WINDOW_SECONDS)?;
        let (tokens_2s, sequence_seconds_2s) = self.evidence(now, SHORT_WINDOW_SECONDS)?;
        Ok(Snapshot {
            abi_version: ABI_VERSION,
            observed: u32::from(self.observed),
            revision: self.revision,
            reference: self.reference,
            tokens_60s,
            sequence_seconds_60s,
            tokens_2s,
            sequence_seconds_2s,
            active_sequences: self.active,
            last_prefill_end: self.prefill_end.unwrap_or(-1.0),
            last_prefill_wall: self.prefill_wall,
            preference_until: self.preference_until,
        })
    }

    fn admission(
        &mut self,
        now: f64,
        projected_concurrency: u32,
        pressure_class: u32,
    ) -> Result<Admission> {
        if projected_concurrency == 0 || pressure_class >= PRESSURE_CLASSES {
            return Err(INVALID);
        }
        self.advance(now)?;
        let reference = self.reference;
        let observed = u32::from(!self.surface.is_empty());
        if reference == 0.0 {
            return Ok(Admission {
                abi_version: ABI_VERSION,
                allowed: 1,
                reason: ADMISSION_REFERENCE_DISABLED,
                observed,
                reference,
                conservative_tps: 0.0,
                projected_tps: 0.0,
                projected_concurrency,
                pressure_class,
                evidence_concurrency: 0,
                evidence_pressure_class: 0,
                active_sequences: self.active,
            });
        }

        let exact_key = (projected_concurrency, pressure_class);
        let mut selected: Option<((u32, u32), f64)> = None;

        if let Some(cell) = self.surface.get(&exact_key) {
            if cell.qualified(now) {
                if let Some(bound) = cell.lower_bound(now)? {
                    selected = Some((exact_key, bound));
                }
            }
        }

        if selected.is_none() {
            for (&key, cell) in self.surface.iter() {
                if key.0 < projected_concurrency || key.1 < pressure_class {
                    continue;
                }
                if !cell.qualified(now) {
                    continue;
                }
                let Some(bound) = cell.lower_bound(now)? else {
                    continue;
                };
                let replace = match selected {
                    Some((_, best_bound)) => bound < best_bound,
                    None => true,
                };
                if replace {
                    selected = Some((key, bound));
                }
            }
        }

        match selected {
            Some(((evidence_concurrency, evidence_pressure), bound)) => {
                let allowed = bound >= reference;
                Ok(Admission {
                    abi_version: ABI_VERSION,
                    allowed: u32::from(allowed),
                    reason: if allowed {
                        ADMISSION_FIT
                    } else {
                        ADMISSION_TPS_RISK
                    },
                    observed,
                    reference,
                    conservative_tps: bound,
                    projected_tps: bound,
                    projected_concurrency,
                    pressure_class,
                    evidence_concurrency,
                    evidence_pressure_class: evidence_pressure,
                    active_sequences: self.active,
                })
            }
            None => Ok(Admission {
                abi_version: ABI_VERSION,
                allowed: 0,
                reason: ADMISSION_UNKNOWN,
                observed,
                reference,
                conservative_tps: 0.0,
                projected_tps: 0.0,
                projected_concurrency,
                pressure_class,
                evidence_concurrency: 0,
                evidence_pressure_class: 0,
                active_sequences: self.active,
            }),
        }
    }

    fn choose(&mut self, now: f64, decode: u32, prefill: u32, age: f64) -> Result<u32> {
        if decode > 1 || prefill > 1 || !nonnegative(age) {
            return Err(INVALID);
        }
        let view = self.snapshot(now)?;
        if decode == 0
            || prefill == 0
            || self.reference == 0.0
            || view.sequence_seconds_60s <= 0.0
            || view.tokens_60s == 0
        {
            return Ok(0);
        }
        let signal = view.tokens_60s as f64 / view.sequence_seconds_60s;
        if signal <= 0.0 || !signal.is_finite() {
            return Ok(0);
        }
        if self.preference_until > now {
            return Ok(1);
        }
        if self.prefill_end.is_none() || now < self.prefill_end.unwrap() {
            return Ok(0);
        }
        let Some(end) = self.prefill_end else {
            return Ok(0);
        };
        let duration =
            ((now - end) / MAX_PREFERENCE_SECONDS).clamp(0.0, 1.0) * MAX_PREFERENCE_SECONDS;
        if self.prefill_wall <= 0.0 {
            return Ok(0);
        }
        let preference = self.prefill_wall * (self.reference / signal - 1.0).max(0.0);
        let end = now + duration.max(preference.min(MAX_PREFERENCE_SECONDS));
        self.preference_until = end;
        Ok(1)
    }
}

pub struct Governor {
    state: Mutex<State>,
}

fn guarded(action: impl FnOnce() -> Result<()>) -> i32 {
    match catch_unwind(AssertUnwindSafe(action)) {
        Ok(Ok(())) => OK,
        Ok(Err(status)) => status,
        Err(_) => INTERNAL,
    }
}

fn transaction<T>(
    handle: *mut Governor,
    action: impl FnOnce(&mut State) -> Result<T>,
) -> Result<T> {
    if handle.is_null() {
        return Err(INVALID);
    }
    let governor = unsafe { &mut *handle };
    let mut state = governor.state.lock().map_err(|_| INTERNAL)?;
    action(&mut state)
}

#[no_mangle]
pub extern "C" fn pig_governor_abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_new(reference: f64, out: *mut *mut Governor) -> i32 {
    guarded(|| {
        if out.is_null() {
            return Err(INVALID);
        }
        let state = State::new(reference)?;
        *out = Box::into_raw(Box::new(Governor {
            state: Mutex::new(state),
        }));
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_free(handle: *mut Governor) -> i32 {
    guarded(|| {
        if handle.is_null() {
            return Err(INVALID);
        }
        drop(Box::from_raw(handle));
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_observe(
    handle: *mut Governor,
    now: f64,
    delta: u64,
    active_after: u64,
) -> i32 {
    guarded(|| transaction(handle, |state| state.observe(now, delta, active_after)))
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_observe_surface(
    handle: *mut Governor,
    now: f64,
    delta: u64,
    duration: f64,
    concurrency: u32,
    pressure_class: u32,
) -> i32 {
    guarded(|| {
        transaction(handle, |state| {
            state.observe_surface(now, delta, duration, concurrency, pressure_class)
        })
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_prefill(handle: *mut Governor, now: f64, wall: f64) -> i32 {
    guarded(|| transaction(handle, |state| state.prefill(now, wall)))
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_update_reference(
    handle: *mut Governor,
    expected_revision: u64,
    reference: f64,
) -> i32 {
    guarded(|| {
        transaction(handle, |state| {
            state.update_reference(expected_revision, reference)
        })
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_snapshot(
    handle: *mut Governor,
    now: f64,
    out: *mut Snapshot,
) -> i32 {
    guarded(|| {
        if out.is_null() {
            return Err(INVALID);
        }
        *out = transaction(handle, |state| state.snapshot(now))?;
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_admit(
    handle: *mut Governor,
    now: f64,
    projected_concurrency: u32,
    pressure_class: u32,
    out: *mut Admission,
) -> i32 {
    guarded(|| {
        if out.is_null() {
            return Err(INVALID);
        }
        *out = transaction(handle, |state| {
            state.admission(now, projected_concurrency, pressure_class)
        })?;
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_choose(
    handle: *mut Governor,
    now: f64,
    decode: u32,
    prefill: u32,
    age: f64,
    out: *mut u32,
) -> i32 {
    guarded(|| {
        if out.is_null() {
            return Err(INVALID);
        }
        *out = transaction(handle, |state| state.choose(now, decode, prefill, age))?;
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn aggregate_window_rejects_clock_and_token_regressions() {
        let mut s = State::new(100.0).unwrap();
        assert_eq!(s.observe(-1.0, 0, 1), Err(INVALID));
        assert_eq!(s.observe(1.0, 0, 1), Ok(()));
        assert_eq!(s.observe(0.5, 1, 1), Err(INVALID));
        assert_eq!(s.observe(1.0, u64::MAX, 1), Err(INVALID));
        assert_eq!(s.snapshot(1.0).unwrap().active_sequences, 1);
    }

    #[test]
    fn aggregate_first_token_is_excluded_by_python_but_bucket_counts_delta() {
        let mut s = State::new(100.0).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        s.observe(1.0, 1, 1).unwrap();
        let snapshot = s.snapshot(1.0).unwrap();
        assert_eq!(snapshot.tokens_60s, 1);
        assert_eq!(snapshot.sequence_seconds_60s, 0.0);
    }

    #[test]
    fn surface_observation_creates_exact_cell_and_windows() {
        let mut s = State::new(50.0).unwrap();
        s.observe_surface(1.0, 100, 1.0, 4, 0).unwrap();
        let admission = s.admission(1.0, 4, 0).unwrap();
        assert_eq!(admission.allowed, 1);
        assert_eq!(admission.reason, ADMISSION_FIT);
        assert_eq!(admission.evidence_concurrency, 4);
        assert_eq!(admission.evidence_pressure_class, 0);
        assert!((admission.projected_tps - 100.0).abs() < 1e-9);
    }

    #[test]
    fn exact_unsafe_cell_is_risk_even_with_zero_waiting() {
        let mut s = State::new(50.0).unwrap();
        s.observe_surface(1.0, 20, 1.0, 1, 0).unwrap();
        let admission = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(admission.allowed, 0);
        assert_eq!(admission.reason, ADMISSION_TPS_RISK);
        assert_eq!(admission.evidence_concurrency, 1);
    }

    #[test]
    fn heavier_cell_can_supply_conservative_bound() {
        let mut s = State::new(50.0).unwrap();
        s.observe_surface(1.0, 100, 1.0, 4, 1).unwrap();
        let admission = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(admission.allowed, 1);
        assert_eq!(admission.evidence_concurrency, 4);
        assert_eq!(admission.evidence_pressure_class, 1);
    }

    #[test]
    fn lighter_cell_never_extrapolates_upward() {
        let mut s = State::new(50.0).unwrap();
        s.observe_surface(1.0, 100, 1.0, 1, 0).unwrap();
        let admission = s.admission(1.0, 2, 0).unwrap();
        assert_eq!(admission.allowed, 0);
        assert_eq!(admission.reason, ADMISSION_UNKNOWN);
        assert_eq!(admission.evidence_concurrency, 0);
    }

    #[test]
    fn reference_zero_samples_and_cas_preserves_surface() {
        let mut s = State::new(0.0).unwrap();
        let admission = s.admission(1.0, 4, 0).unwrap();
        assert_eq!(admission.allowed, 1);
        assert_eq!(admission.reason, ADMISSION_REFERENCE_DISABLED);
        s.observe_surface(1.0, 100, 1.0, 4, 0).unwrap();
        s.update_reference(1, 50.0).unwrap();
        let admission = s.admission(1.0, 4, 0).unwrap();
        assert_eq!(admission.allowed, 1);
        assert_eq!(admission.reason, ADMISSION_FIT);
        assert_eq!(admission.reference, 50.0);
    }

    #[test]
    fn stale_or_insufficient_surface_is_unknown() {
        let mut s = State::new(50.0).unwrap();
        let admission = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(admission.reason, ADMISSION_UNKNOWN);

        s.observe_surface(1.0, 100, 0.01, 1, 0).unwrap();
        let admission = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(admission.reason, ADMISSION_UNKNOWN);

        s.observe_surface(2.0, 100, 0.2, 1, 0).unwrap();
        let admission = s.admission(2.0, 1, 0).unwrap();
        assert_eq!(admission.reason, ADMISSION_FIT);

        let admission = s.admission(80.0, 1, 0).unwrap();
        assert_eq!(admission.reason, ADMISSION_UNKNOWN);
    }

    #[test]
    fn preference_is_short_ages_and_never_reanchors_to_evaluation() {
        let mut s = State::new(100.0).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        s.observe(10.0, 10, 1).unwrap();
        s.prefill(10.0, 0.5).unwrap();
        assert_eq!(s.choose(10.0, 1, 1, 0.0), Ok(1));
        assert!(s.preference_until <= 10.25);
        let mut old_queue = s.clone();
        assert_eq!(old_queue.choose(10.05, 1, 1, 10.0), Ok(0));
        assert_eq!(s.choose(10.05, 1, 1, 0.0), Ok(1));
        assert_eq!(s.choose(10.25, 1, 1, 0.0), Ok(0));
        assert_eq!(s.choose(11.0, 1, 1, 0.0), Ok(0));
    }

    #[test]
    fn ffi_invalid_calls_and_cas_preserve_actual_history() {
        unsafe {
            let mut h = std::ptr::null_mut();
            assert_eq!(pig_governor_new(30.0, &mut h), OK);
            assert_eq!(pig_governor_observe(h, 0.0, 0, 1), OK);
            assert_eq!(pig_governor_observe(h, 1.0, 12, 1), OK);
            let mut before = Snapshot::default();
            assert_eq!(pig_governor_snapshot(h, 1.0, &mut before), OK);
            let mut admission = Admission::default();
            assert_eq!(pig_governor_admit(h, 1.0, 1, 0, &mut admission), OK);
            assert_eq!(admission.abi_version, ABI_VERSION);
            assert_eq!(pig_governor_observe_surface(h, 1.0, 10, 1.0, 1, 0), OK);
            assert_eq!(pig_governor_observe(h, 0.5, 10, 9), INVALID);
            assert_eq!(pig_governor_observe(h, f64::NAN, 10, 9), INVALID);
            assert_eq!(pig_governor_observe(h, MAX_CLOCK, 10, 9), INVALID);
            assert_eq!(pig_governor_observe(h, 1.5, u64::MAX, 9), INVALID);
            assert_eq!(pig_governor_prefill(h, 2.0, -1.0), INVALID);
            assert_eq!(pig_governor_update_reference(h, 1, f64::INFINITY), INVALID);
            assert_eq!(pig_governor_update_reference(h, 9, 40.0), CONFLICT);
            let mut invalid_output = 7;
            assert_eq!(
                pig_governor_choose(h, 2.0, 2, 1, 0.0, &mut invalid_output),
                INVALID
            );
            assert_eq!(
                pig_governor_choose(h, 2.0, 1, 1, -1.0, &mut invalid_output),
                INVALID
            );
            assert_eq!(invalid_output, 7);
            let mut after = Snapshot::default();
            assert_eq!(pig_governor_snapshot(h, 1.0, &mut after), OK);
            assert_eq!(before, after);
            assert_eq!(pig_governor_update_reference(h, 1, 40.0), OK);
            assert_eq!(pig_governor_snapshot(h, 1.0, &mut after), OK);
            assert_eq!(after.revision, 2);
            assert_eq!(after.reference, 40.0);
            assert_eq!(after.tokens_60s, before.tokens_60s);
            assert_eq!(after.sequence_seconds_60s, before.sequence_seconds_60s);
            assert_eq!(pig_governor_free(h), OK);
        }
    }

    #[test]
    fn abi_rejects_nulls_and_bad_creation_without_touching_outputs() {
        unsafe {
            let mut h = std::ptr::null_mut();
            assert_eq!(pig_governor_abi_version(), 2);
            assert_eq!(pig_governor_new(-1.0, &mut h), INVALID);
            assert!(h.is_null());
            assert_eq!(pig_governor_new(30.0, std::ptr::null_mut()), INVALID);
            assert_eq!(pig_governor_snapshot(h, 0.0, std::ptr::null_mut()), INVALID);
            assert_eq!(
                pig_governor_admit(h, 0.0, 1, 0, std::ptr::null_mut()),
                INVALID
            );
            assert_eq!(pig_governor_observe(h, 0.0, 0, 0), INVALID);
            assert_eq!(pig_governor_observe_surface(h, 0.0, 0, 0.0, 0, 0), INVALID);
            assert_eq!(pig_governor_free(h), INVALID);
        }
        assert_eq!(
            guarded(|| panic!("panic must remain inside Rust")),
            INTERNAL
        );
    }
}
