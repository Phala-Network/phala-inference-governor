//! Version 1 C ABI. The caller owns pointer validity, output storage and handle
//! lifetime; never free a handle during another call. Calls on a live handle
//! serialize internally. All numeric validation precedes committing state.
//! Request identity, epoch and exactly-once committed deltas belong to Python.
//! This core never owns requests, allocates KV, rejects work or predicts tokens.

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Mutex;

pub const ABI_VERSION: u32 = 1;
pub const OK: i32 = 0;
pub const INVALID: i32 = 1;
pub const CONFLICT: i32 = 2;
pub const INTERNAL: i32 = 3;
const BUCKET_SECONDS: f64 = 0.5;
const WINDOW_SECONDS: f64 = 60.0;
const BUCKETS: usize = 121;
const MAX_PREFERENCE_SECONDS: f64 = 0.25;
// Keeps half-second bucket boundaries exactly representable, including +1.
const MAX_CLOCK: f64 = 1_125_899_906_842_624.0;
type Result<T> = std::result::Result<T, i32>;

#[derive(Clone, Copy)]
struct Bucket {
    tick: i64,
    tokens: u64,
    seconds: f64,
}
const EMPTY: Bucket = Bucket { tick: -1, tokens: 0, seconds: 0.0 };

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
}

fn nonnegative(value: f64) -> bool { value.is_finite() && value >= 0.0 }
fn tick(now: f64) -> i64 { (now / BUCKET_SECONDS).floor() as i64 }

impl State {
    fn new(reference: f64) -> Result<Self> {
        if !nonnegative(reference) { return Err(INVALID); }
        Ok(Self {
            buckets: [EMPTY; BUCKETS], last_time: None, observed: false,
            active: 0, revision: 1, reference, prefill_end: None,
            prefill_wall: 0.0, preference_until: 0.0,
        })
    }

    fn bucket(&mut self, tick: i64) -> &mut Bucket {
        let bucket = &mut self.buckets[tick as usize % BUCKETS];
        if bucket.tick != tick { *bucket = Bucket { tick, ..EMPTY }; }
        bucket
    }

    fn advance(&mut self, now: f64) -> Result<()> {
        if !nonnegative(now) || now >= MAX_CLOCK
            || self.last_time.is_some_and(|last| now < last) {
            return Err(INVALID);
        }
        if let Some(last) = self.last_time {
            // At most 121 iterations even after arbitrarily long inactivity.
            // The oldest bucket is retained whole, quantizing both numerator
            // and denominator by less than 0.5 s, without invented credit.
            let oldest = (tick(now - WINDOW_SECONDS) as f64 * BUCKET_SECONDS).max(0.0);
            let mut start = last.max(oldest);
            while self.active > 0 && start < now {
                let index = tick(start);
                let end = now.min((index + 1) as f64 * BUCKET_SECONDS);
                if end <= start { return Err(INVALID); }
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
        if !seconds.is_finite() { return Err(INVALID); }
        Ok((tokens, seconds))
    }

    fn observe(&mut self, now: f64, delta: u64, active_after: u64) -> Result<()> {
        self.advance(now)?;
        let bucket = self.bucket(tick(now));
        bucket.tokens = bucket.tokens.checked_add(delta).ok_or(INVALID)?;
        self.evidence(now, WINDOW_SECONDS)?;
        self.active = active_after;
        self.observed = true;
        Ok(())
    }

    fn prefill(&mut self, now: f64, wall: f64) -> Result<()> {
        if !wall.is_finite() || wall <= 0.0 || wall > now { return Err(INVALID); }
        self.advance(now)?;
        self.prefill_end = Some(now);
        self.prefill_wall = wall;
        self.preference_until = 0.0;
        Ok(())
    }

    fn update_reference(&mut self, revision: u64, reference: f64) -> Result<()> {
        if !nonnegative(reference) { return Err(INVALID); }
        if revision != self.revision { return Err(CONFLICT); }
        self.revision = self.revision.checked_add(1).ok_or(INVALID)?;
        self.reference = reference;
        self.preference_until = 0.0;
        Ok(())
    }

    fn snapshot(&mut self, now: f64) -> Result<Snapshot> {
        self.advance(now)?;
        let (tokens_60s, sequence_seconds_60s) = self.evidence(now, 60.0)?;
        let (tokens_2s, sequence_seconds_2s) = self.evidence(now, 2.0)?;
        Ok(Snapshot {
            abi_version: ABI_VERSION, observed: u32::from(self.observed),
            revision: self.revision, reference: self.reference,
            tokens_60s, sequence_seconds_60s, tokens_2s, sequence_seconds_2s,
            active_sequences: self.active,
            last_prefill_end: self.prefill_end.unwrap_or(-1.0),
            last_prefill_wall: self.prefill_wall,
            preference_until: self.preference_until,
        })
    }

    fn choose(&mut self, now: f64, decode: u32, prefill: u32, age: f64) -> Result<u32> {
        if decode > 1 || prefill > 1 || !nonnegative(age) { return Err(INVALID); }
        let view = self.snapshot(now)?;
        self.preference_until = 0.0;
        let Some(end) = self.prefill_end else { return Ok(0); };
        if decode == 0 || prefill == 0 || self.reference == 0.0
            || !self.observed || view.sequence_seconds_60s == 0.0 {
            return Ok(0);
        }
        let mean = view.tokens_60s as f64 / view.sequence_seconds_60s;
        let signal = if view.sequence_seconds_2s > 0.0 {
            let weight = view.sequence_seconds_2s / (view.sequence_seconds_2s + 1.0);
            mean * (1.0 - weight)
                + (view.tokens_2s as f64 / view.sequence_seconds_2s) * weight
        } else { mean };
        let requested = if signal == 0.0 { MAX_PREFERENCE_SECONDS } else {
            (self.prefill_wall * (self.reference / signal - 1.0).max(0.0))
                .min(MAX_PREFERENCE_SECONDS)
        };
        let mass = view.sequence_seconds_60s;
        let duration = requested * (mass / (mass + 1.0))
            / (1.0 + age / MAX_PREFERENCE_SECONDS);
        // Repeated evaluation cannot move the origin to now. Compare elapsed
        // duration to avoid rounding an absolute deadline into a longer hold.
        self.preference_until = end + duration;
        Ok(u32::from(now - end < duration))
    }
}

/// Opaque to C. Only pointers returned by pig_governor_new may be passed in.
pub struct Governor { state: Mutex<State> }

fn guarded(action: impl FnOnce() -> Result<()>) -> i32 {
    match catch_unwind(AssertUnwindSafe(action)) {
        Ok(Ok(())) => OK,
        Ok(Err(status)) => status,
        Err(_) => INTERNAL,
    }
}

// Commit a small fixed-size clone only after validation and output calculation.
// Failed calls (including panics) cannot partly advance the metric or policy.
unsafe fn transaction<T>(handle: *mut Governor, action: impl FnOnce(&mut State) -> Result<T>) -> Result<T> {
    let handle = handle.as_ref().ok_or(INVALID)?;
    let mut state = handle.state.lock().map_err(|_| INTERNAL)?;
    let mut next = state.clone();
    let output = action(&mut next)?;
    *state = next;
    Ok(output)
}

#[no_mangle]
pub extern "C" fn pig_governor_abi_version() -> u32 { ABI_VERSION }

#[no_mangle]
pub unsafe extern "C" fn pig_governor_new(reference: f64, out: *mut *mut Governor) -> i32 {
    guarded(|| {
        if out.is_null() { return Err(INVALID); }
        let state = State::new(reference)?;
        *out = Box::into_raw(Box::new(Governor { state: Mutex::new(state) }));
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_free(handle: *mut Governor) -> i32 {
    guarded(|| {
        if handle.is_null() { return Err(INVALID); }
        drop(Box::from_raw(handle));
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_observe(handle: *mut Governor, now: f64, delta: u64, active_after: u64) -> i32 {
    guarded(|| transaction(handle, |state| state.observe(now, delta, active_after)))
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_prefill(handle: *mut Governor, now: f64, wall: f64) -> i32 {
    guarded(|| transaction(handle, |state| state.prefill(now, wall)))
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_update_reference(handle: *mut Governor, revision: u64, reference: f64) -> i32 {
    guarded(|| transaction(handle, |state| state.update_reference(revision, reference)))
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_snapshot(handle: *mut Governor, now: f64, out: *mut Snapshot) -> i32 {
    guarded(|| {
        if out.is_null() { return Err(INVALID); }
        *out = transaction(handle, |state| state.snapshot(now))?;
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_choose(handle: *mut Governor, now: f64, decode: u32, prefill: u32, age: f64, out: *mut u32) -> i32 {
    guarded(|| {
        if out.is_null() { return Err(INVALID); }
        *out = transaction(handle, |state| state.choose(now, decode, prefill, age))?;
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_accrue_actual_time_and_expire_together() {
        let mut s = State::new(30.0).unwrap();
        s.observe(0.0, 0, 2).unwrap();
        s.observe(1.0, 20, 2).unwrap();
        s.observe(2.0, 20, 0).unwrap();
        let v = s.snapshot(2.0).unwrap();
        assert_eq!((v.tokens_60s, v.sequence_seconds_60s), (40, 4.0));
        assert_eq!((v.tokens_2s, v.sequence_seconds_2s), (40, 4.0));
        let v = s.snapshot(62.5).unwrap();
        assert_eq!((v.tokens_60s, v.sequence_seconds_60s), (0, 0.0));
    }

    #[test]
    fn cancellation_changes_only_subsequent_exposure() {
        let mut s = State::new(30.0).unwrap();
        s.observe(0.0, 0, 2).unwrap();
        s.observe(1.0, 8, 1).unwrap();
        s.observe(3.0, 4, 0).unwrap();
        let v = s.snapshot(10.0).unwrap();
        assert_eq!((v.tokens_60s, v.sequence_seconds_60s), (12, 4.0));
        assert_eq!(v.active_sequences, 0);
    }

    #[test]
    fn long_pause_accounts_retained_real_time_without_token_credit() {
        let mut s = State::new(30.0).unwrap();
        s.observe(0.0, 0, 3).unwrap();
        s.observe(1.0, 100, 3).unwrap();
        let v = s.snapshot(1_000_000.25).unwrap();
        assert_eq!((v.tokens_60s, v.tokens_2s), (0, 0));
        assert_eq!(v.sequence_seconds_60s, 60.25 * 3.0);
        assert_eq!(v.sequence_seconds_2s, 2.25 * 3.0);
    }

    #[test]
    fn cold_disabled_and_missing_work_follow_native() {
        let mut s = State::new(30.0).unwrap();
        assert_eq!(s.choose(0.0, 1, 1, 0.0), Ok(0));
        s.prefill(1.0, 0.5).unwrap();
        assert_eq!(s.choose(1.0, 1, 1, 0.0), Ok(0));
        s.observe(1.0, 0, 1).unwrap();
        s.prefill(2.0, 0.5).unwrap();
        assert_eq!(s.choose(2.0, 0, 1, 0.0), Ok(0));
        assert_eq!(s.choose(2.0, 1, 0, 0.0), Ok(0));
        s.update_reference(1, 0.0).unwrap();
        assert_eq!(s.choose(2.0, 1, 1, 0.0), Ok(0));
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
            assert_eq!(pig_governor_observe(h, 0.5, 10, 9), INVALID);
            assert_eq!(pig_governor_observe(h, f64::NAN, 10, 9), INVALID);
            assert_eq!(pig_governor_observe(h, MAX_CLOCK, 10, 9), INVALID);
            assert_eq!(pig_governor_observe(h, 1.5, u64::MAX, 9), INVALID);
            assert_eq!(pig_governor_prefill(h, 2.0, -1.0), INVALID);
            assert_eq!(pig_governor_update_reference(h, 1, f64::INFINITY), INVALID);
            assert_eq!(pig_governor_update_reference(h, 9, 40.0), CONFLICT);
            let mut invalid_output = 7;
            assert_eq!(pig_governor_choose(h, 2.0, 2, 1, 0.0, &mut invalid_output), INVALID);
            assert_eq!(pig_governor_choose(h, 2.0, 1, 1, -1.0, &mut invalid_output), INVALID);
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
            assert_eq!(pig_governor_abi_version(), 1);
            assert_eq!(pig_governor_new(-1.0, &mut h), INVALID);
            assert!(h.is_null());
            assert_eq!(pig_governor_new(30.0, std::ptr::null_mut()), INVALID);
            assert_eq!(pig_governor_snapshot(h, 0.0, std::ptr::null_mut()), INVALID);
            assert_eq!(pig_governor_observe(h, 0.0, 0, 0), INVALID);
            assert_eq!(pig_governor_free(h), INVALID);
        }
        assert_eq!(guarded(|| panic!("panic must remain inside Rust")), INTERNAL);
    }
}
