//! Version 4 C ABI. The caller owns pointer validity, output storage and handle
//! lifetime; never free a handle during another call. Calls on a live handle
//! serialize internally. All numeric validation precedes committing state.
//! Request identity, epoch and exactly-once committed deltas belong to Python.
//! This core never owns requests, allocates KV, or predicts output tokens. Its
//! admission result is a conservative TPS forecast over observed Decode work.

use std::collections::BTreeMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Mutex;

pub const ABI_VERSION: u32 = 4;
pub const OK: i32 = 0;
pub const INVALID: i32 = 1;
pub const CONFLICT: i32 = 2;
pub const INTERNAL: i32 = 3;
pub const ADMISSION_FIT: u32 = 0;
pub const ADMISSION_REFERENCE_DISABLED: u32 = 1;
pub const ADMISSION_TPS_RISK: u32 = 2;
pub const ADMISSION_COLD_PRIOR: u32 = 3;
pub const ADMISSION_UNKNOWN: u32 = 4;
/// Reserved for the Scheduler-owned post-admit waiting gate. The Rust TPS
/// predictor never emits this value, but ABI v4 consumers may report it in the
/// composed admission decision.
pub const ADMISSION_WAITING_LIMIT: u32 = 5;
pub const ADMISSION_AGGREGATE_TPS_RISK: u32 = 6;

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
#[cfg_attr(test, derive(Debug))]
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

/// Portable response-surface evidence. Imported cells are conservative priors
/// with an independent deadline; they never populate the live rolling window.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct ProfileCellV1 {
    pub concurrency: u32,
    pub pressure_class: u32,
    pub long_tokens: u64,
    pub long_seconds: f64,
    pub short_tokens: u64,
    pub short_seconds: f64,
    pub approved_lower_tps: f64,
}

impl ProfileCellV1 {
    fn key(&self) -> (u32, u32) {
        (self.concurrency, self.pressure_class)
    }

    fn validate(&self, max_running_requests: u32) -> Result<()> {
        if self.concurrency == 0
            || self.concurrency > max_running_requests
            || self.pressure_class >= PRESSURE_CLASSES
            || !nonnegative(self.long_seconds)
            || !nonnegative(self.short_seconds)
            || !nonnegative(self.approved_lower_tps)
            || self.long_seconds < MIN_EXPOSURE_SECONDS
            || self.short_seconds > self.long_seconds
            || self.short_tokens > self.long_tokens
            || (self.short_seconds == 0.0 && self.short_tokens != 0)
        {
            return Err(INVALID);
        }
        let long_tps = self.long_tokens as f64 / self.long_seconds;
        let derived_lower = if self.short_seconds > 0.0 {
            (self.short_tokens as f64 / self.short_seconds).min(long_tps)
        } else {
            long_tps
        };
        if !derived_lower.is_finite() || self.approved_lower_tps > derived_lower {
            return Err(INVALID);
        }
        Ok(())
    }
}

#[derive(Clone, Copy)]
struct SurfaceBound {
    value: f64,
    used_live: bool,
    used_prior: bool,
}

#[derive(Clone)]
#[cfg_attr(test, derive(Debug))]
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

    fn observe(&mut self, now: f64, delta: u64, sequence_seconds: f64) -> Result<()> {
        if !nonnegative(sequence_seconds) || (delta > 0 && sequence_seconds == 0.0) {
            return Err(INVALID);
        }
        let bucket = self.bucket(tick(now));
        bucket.seconds += sequence_seconds;
        if !bucket.seconds.is_finite() {
            return Err(INVALID);
        }
        bucket.tokens = bucket.tokens.checked_add(delta).ok_or(INVALID)?;
        self.exposure_seconds += sequence_seconds;
        if !self.exposure_seconds.is_finite() {
            return Err(INVALID);
        }
        self.last_time = Some(now);
        let (_, recent_seconds) = self.evidence(now, WINDOW_SECONDS)?;
        if sequence_seconds > 0.0 && recent_seconds >= MIN_EXPOSURE_SECONDS {
            self.last_qualified = Some(now);
        }
        Ok(())
    }

    fn qualified(&self, now: f64) -> Result<bool> {
        let (_, recent_seconds) = self.evidence(now, WINDOW_SECONDS)?;
        Ok(self
            .last_qualified
            .is_some_and(|qualified| now - qualified <= MAX_SURFACE_AGE_SECONDS)
            && recent_seconds >= MIN_EXPOSURE_SECONDS)
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

    /// Live bound consulted by admission. The raw lower bound is the lower of
    /// fresh short/long evidence, so a transient short burst cannot lift the
    /// forecast above the sustained rate. Once the fresh short window itself
    /// carries the minimum qualified exposure it is fresh real Decode evidence
    /// of the current sustained rate: a one-time cold-start ramp retained in
    /// the long window no longer vetoes recovery, and the bound follows the
    /// short window. A sub-exposure burst never recovers the bound, and the
    /// long-only fallback after an idle gap is unchanged.
    fn admission_bound(&self, now: f64) -> Result<Option<f64>> {
        let (short_tokens, short_seconds) = self.evidence(now, SHORT_WINDOW_SECONDS)?;
        if short_seconds >= MIN_EXPOSURE_SECONDS {
            return Ok(Some(short_tokens as f64 / short_seconds));
        }
        self.lower_bound(now)
    }
}

#[derive(Clone)]
#[cfg_attr(test, derive(Debug))]
struct State {
    buckets: [Bucket; BUCKETS],
    active_run_buckets: [Bucket; BUCKETS],
    max_running_requests: u32,
    last_time: Option<f64>,
    observed: bool,
    active: u64,
    active_run_has_tokens: bool,
    revision: u64,
    reference: f64,
    prefill_end: Option<f64>,
    prefill_wall: f64,
    preference_until: f64,
    surface: BTreeMap<(u32, u32), SurfaceCell>,
    profile_prior: BTreeMap<(u32, u32), ProfileCellV1>,
    profile_deadline: Option<f64>,
}

fn nonnegative(value: f64) -> bool {
    value.is_finite() && value >= 0.0
}

fn tick(now: f64) -> i64 {
    (now / BUCKET_SECONDS).floor() as i64
}

impl State {
    fn new(reference: f64, max_running_requests: u32) -> Result<Self> {
        if max_running_requests == 0 || !nonnegative(reference) {
            return Err(INVALID);
        }
        Ok(Self {
            buckets: [EMPTY; BUCKETS],
            active_run_buckets: [EMPTY; BUCKETS],
            max_running_requests,
            last_time: None,
            observed: false,
            active: 0,
            active_run_has_tokens: false,
            revision: 1,
            reference,
            prefill_end: None,
            prefill_wall: 0.0,
            preference_until: 0.0,
            surface: BTreeMap::new(),
            profile_prior: BTreeMap::new(),
            profile_deadline: None,
        })
    }

    fn new_with_profile(
        reference: f64,
        max_running_requests: u32,
        now: f64,
        ttl: f64,
        cells: &[ProfileCellV1],
    ) -> Result<Self> {
        if !nonnegative(now) || now >= MAX_CLOCK || !ttl.is_finite() || ttl <= 0.0 {
            return Err(INVALID);
        }
        let deadline = now + ttl;
        let max_cells = (max_running_requests as usize)
            .checked_mul(PRESSURE_CLASSES as usize)
            .ok_or(INVALID)?;
        if cells.len() > max_cells || !deadline.is_finite() || deadline >= MAX_CLOCK {
            return Err(INVALID);
        }
        let mut state = Self::new(reference, max_running_requests)?;
        for cell in cells {
            cell.validate(max_running_requests)?;
            if state.profile_prior.insert(cell.key(), *cell).is_some() {
                return Err(INVALID);
            }
        }
        state.last_time = Some(now);
        state.profile_deadline = Some(deadline);
        Ok(state)
    }

    fn bucket(&mut self, tick: i64) -> &mut Bucket {
        let bucket = &mut self.buckets[tick as usize % BUCKETS];
        if bucket.tick != tick {
            *bucket = Bucket { tick, ..EMPTY };
        }
        bucket
    }

    fn active_run_bucket(&mut self, tick: i64) -> &mut Bucket {
        let bucket = &mut self.active_run_buckets[tick as usize % BUCKETS];
        if bucket.tick != tick {
            *bucket = Bucket { tick, ..EMPTY };
        }
        bucket
    }

    fn advance(&mut self, now: f64) -> Result<()> {
        self.advance_clock(now, true)
    }

    fn advance_clock(&mut self, now: f64, accumulate_active: bool) -> Result<()> {
        if !nonnegative(now) || now >= MAX_CLOCK || self.last_time.is_some_and(|last| now < last) {
            return Err(INVALID);
        }
        if let Some(last) = self.last_time {
            let oldest = (tick(now - WINDOW_SECONDS) as f64 * BUCKET_SECONDS).max(0.0);
            let mut start = last.max(oldest);
            while accumulate_active && self.active > 0 && start < now {
                let index = tick(start);
                let end = (((index + 1) as f64) * BUCKET_SECONDS).min(now);
                if end <= start {
                    return Err(INVALID);
                }
                let exposure = (end - start) * self.active as f64;
                self.bucket(index).seconds += exposure;
                self.active_run_bucket(index).seconds += exposure;
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

    fn record_tokens(&mut self, now: f64, delta: u64) -> Result<()> {
        let bucket = self.bucket(tick(now));
        bucket.tokens = bucket.tokens.checked_add(delta).ok_or(INVALID)?;
        self.evidence(now, WINDOW_SECONDS)?;
        Ok(())
    }

    fn record_active_run_tokens(&mut self, now: f64, delta: u64) -> Result<()> {
        let bucket = self.active_run_bucket(tick(now));
        bucket.tokens = bucket.tokens.checked_add(delta).ok_or(INVALID)?;
        Ok(())
    }

    fn observe_in_place(&mut self, now: f64, delta: u64, active_after: u64) -> Result<()> {
        if active_after > u64::from(self.max_running_requests) {
            return Err(INVALID);
        }
        let active_before = self.active;
        self.advance(now)?;
        self.record_tokens(now, delta)?;
        self.update_active_run(active_before, active_after, now, delta)?;
        self.active = active_after;
        self.observed = true;
        Ok(())
    }

    #[cfg(test)]
    fn observe(&mut self, now: f64, delta: u64, active_after: u64) -> Result<()> {
        // Keep the public observer transactional: a rejected update must not
        // advance the clock or partially append evidence.
        let mut next = self.clone();
        next.observe_in_place(now, delta, active_after)?;
        *self = next;
        Ok(())
    }

    fn observe_surface(
        &mut self,
        now: f64,
        delta: u64,
        sequence_seconds: f64,
        concurrency: u32,
        pressure_class: u32,
    ) -> Result<()> {
        if concurrency == 0
            || concurrency > self.max_running_requests
            || pressure_class >= PRESSURE_CLASSES
            || !nonnegative(sequence_seconds)
            || (delta > 0 && sequence_seconds == 0.0)
        {
            return Err(INVALID);
        }
        self.advance(now)?;
        let cell = self
            .surface
            .entry((concurrency, pressure_class))
            .or_insert_with(SurfaceCell::new);
        cell.observe(now, delta, sequence_seconds)?;
        self.observed = true;
        Ok(())
    }

    fn observe_batch_in_place(
        &mut self,
        now: f64,
        delta: u64,
        sequence_seconds: f64,
        concurrency: u32,
        pressure_class: u32,
        active_after: u64,
    ) -> Result<()> {
        if concurrency == 0
            || concurrency > self.max_running_requests
            || pressure_class >= PRESSURE_CLASSES
            || active_after > u64::from(self.max_running_requests)
            || !nonnegative(sequence_seconds)
            || (delta > 0 && sequence_seconds == 0.0)
        {
            return Err(INVALID);
        }

        let active_before = self.active;
        self.advance(now)?;
        let cell = self
            .surface
            .entry((concurrency, pressure_class))
            .or_insert_with(SurfaceCell::new);
        cell.observe(now, delta, sequence_seconds)?;
        self.record_tokens(now, delta)?;
        self.update_active_run(active_before, active_after, now, delta)?;
        self.active = active_after;
        self.observed = true;
        Ok(())
    }

    #[cfg(test)]
    fn observe_batch(
        &mut self,
        now: f64,
        delta: u64,
        sequence_seconds: f64,
        concurrency: u32,
        pressure_class: u32,
        active_after: u64,
    ) -> Result<()> {
        let mut next = self.clone();
        next.observe_batch_in_place(
            now,
            delta,
            sequence_seconds,
            concurrency,
            pressure_class,
            active_after,
        )?;
        *self = next;
        Ok(())
    }

    // The caller owns request identities and signals that every old request
    // retired. Preserve public history and surface cells, but exclude all
    // retiring-set evidence from the newly active set's private live bound.
    fn observe_replacement_in_place(
        &mut self,
        now: f64,
        delta: u64,
        sequence_seconds: f64,
        concurrency: u32,
        pressure_class: u32,
        active_after: u64,
    ) -> Result<()> {
        if self.active == 0
            || u64::from(concurrency) != self.active
            || active_after > u64::from(self.max_running_requests)
            || pressure_class >= PRESSURE_CLASSES
            || !nonnegative(sequence_seconds)
            || (sequence_seconds == 0.0 && delta != 0)
        {
            return Err(INVALID);
        }
        if sequence_seconds > 0.0 {
            self.observe_batch_in_place(
                now,
                delta,
                sequence_seconds,
                concurrency,
                pressure_class,
                active_after,
            )?;
        } else {
            self.observe_in_place(now, 0, active_after)?;
        }
        self.active_run_buckets = [EMPTY; BUCKETS];
        self.active_run_has_tokens = false;
        Ok(())
    }

    #[cfg(test)]
    fn observe_replacement(
        &mut self,
        now: f64,
        delta: u64,
        sequence_seconds: f64,
        concurrency: u32,
        pressure_class: u32,
        active_after: u64,
    ) -> Result<()> {
        let mut next = self.clone();
        next.observe_replacement_in_place(
            now,
            delta,
            sequence_seconds,
            concurrency,
            pressure_class,
            active_after,
        )?;
        *self = next;
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

    fn start_surface_epoch(&mut self, now: f64, active_after: u64) -> Result<()> {
        if !nonnegative(now) || now >= MAX_CLOCK || active_after > self.max_running_requests as u64
        {
            return Err(INVALID);
        }
        self.buckets = [EMPTY; BUCKETS];
        self.active_run_buckets = [EMPTY; BUCKETS];
        self.last_time = Some(now);
        self.observed = false;
        self.active = active_after;
        self.active_run_has_tokens = false;
        self.prefill_end = None;
        self.prefill_wall = 0.0;
        self.preference_until = 0.0;
        self.surface.clear();
        self.profile_prior.clear();
        self.profile_deadline = None;
        Ok(())
    }

    fn prior_active(&self, now: f64) -> bool {
        self.profile_deadline.is_some_and(|deadline| now < deadline)
    }

    fn bound_for_key(&self, now: f64, key: (u32, u32)) -> Result<Option<SurfaceBound>> {
        let (live, live_qualified, last_live_at) = match self.surface.get(&key) {
            Some(cell) => (
                cell.admission_bound(now)?,
                cell.qualified(now)?,
                cell.last_time,
            ),
            None => (None, false, None),
        };
        let prior = if self.prior_active(now) {
            self.profile_prior
                .get(&key)
                .map(|cell| cell.approved_lower_tps)
        } else {
            None
        };
        // A retired slow cell cannot measure its own recovery. Reprobe one
        // vacated slot only when the current active run is healthy.
        let prior_reprobe = match (live, prior, last_live_at) {
            (Some(live), Some(prior), Some(last_at)) => {
                live < self.reference
                    && prior >= self.reference
                    && now - last_at >= SHORT_WINDOW_SECONDS
                    && u64::from(key.0) == self.active + 1
                    && self.aggregate_live_lower_bound(now)?.is_some()
            }
            _ => false,
        };
        Ok(match (live, prior) {
            (Some(live), Some(prior)) if live < prior => Some(SurfaceBound {
                value: if prior_reprobe { prior } else { live },
                used_live: !prior_reprobe,
                used_prior: prior_reprobe,
            }),
            (Some(_), Some(prior)) => Some(SurfaceBound {
                value: prior,
                used_live: live_qualified,
                used_prior: true,
            }),
            (Some(live), None) if live_qualified => Some(SurfaceBound {
                value: live,
                used_live: true,
                used_prior: false,
            }),
            (None, Some(prior)) => Some(SurfaceBound {
                value: prior,
                used_live: false,
                used_prior: true,
            }),
            (None, None) => None,
            (Some(_), None) => None,
        })
    }

    fn has_qualified_live(&self, now: f64) -> Result<bool> {
        for cell in self.surface.values() {
            if cell.qualified(now)? && cell.lower_bound(now)?.is_some() {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn update_active_run(
        &mut self,
        active_before: u64,
        active_after: u64,
        now: f64,
        delta: u64,
    ) -> Result<()> {
        if active_before == 0 && active_after > 0 {
            self.active_run_buckets = [EMPTY; BUCKETS];
            self.active_run_has_tokens = false;
        }
        if delta > 0 && (active_before > 0 || active_after > 0) {
            self.record_active_run_tokens(now, delta)?;
            self.active_run_has_tokens = true;
        }
        Ok(())
    }

    fn active_run_evidence(&self, now: f64, lookback: f64) -> Result<(u64, f64)> {
        let first = tick(now - lookback);
        let last = tick(now);
        let mut tokens: u64 = 0;
        let mut seconds = 0.0;
        for bucket in &self.active_run_buckets {
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

    fn aggregate_live_lower_bound(&self, now: f64) -> Result<Option<f64>> {
        if self.active == 0 || !self.active_run_has_tokens {
            return Ok(None);
        }
        let (long_tokens, long_seconds) = self.active_run_evidence(now, WINDOW_SECONDS)?;
        if long_seconds < MIN_EXPOSURE_SECONDS {
            return Ok(None);
        }
        let long_tps = long_tokens as f64 / long_seconds;
        let (short_tokens, short_seconds) = self.active_run_evidence(now, SHORT_WINDOW_SECONDS)?;
        // As for per-cell admission, qualified fresh evidence can recover from
        // a cold transient retained in the long window. Keep the conservative
        // fallback for sub-exposure bursts; profile export remains unchanged.
        if short_seconds >= MIN_EXPOSURE_SECONDS {
            return Ok(Some(short_tokens as f64 / short_seconds));
        }
        let bound = if short_seconds > 0.0 {
            long_tps.min(short_tokens as f64 / short_seconds)
        } else {
            long_tps
        };
        Ok(Some(bound))
    }

    fn export_profile(&mut self, now: f64, capacity: usize) -> Result<Vec<ProfileCellV1>> {
        let mut next = self.clone();
        next.advance(now)?;
        let mut cells = Vec::new();
        for (&(concurrency, pressure_class), cell) in &next.surface {
            if !cell.qualified(now)? {
                continue;
            }
            let (long_tokens, long_seconds) = cell.evidence(now, WINDOW_SECONDS)?;
            let (short_tokens, short_seconds) = cell.evidence(now, SHORT_WINDOW_SECONDS)?;
            let Some(approved_lower_tps) = cell.lower_bound(now)? else {
                continue;
            };
            cells.push(ProfileCellV1 {
                concurrency,
                pressure_class,
                long_tokens,
                long_seconds,
                short_tokens,
                short_seconds,
                approved_lower_tps,
            });
        }
        if cells.len() > capacity {
            return Err(INVALID);
        }
        *self = next;
        Ok(cells)
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
        if projected_concurrency == 0
            || projected_concurrency > self.max_running_requests
            || pressure_class >= PRESSURE_CLASSES
        {
            return Err(INVALID);
        }
        self.advance(now)?;
        let reference = self.reference;
        let observed = u32::from(self.has_qualified_live(now)?);
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
        let mut selected = self
            .bound_for_key(now, exact_key)?
            .map(|bound| (exact_key, bound));

        if selected.is_none() {
            let mut keys = BTreeMap::new();
            for &key in self.surface.keys() {
                keys.insert(key, ());
            }
            if self.prior_active(now) {
                for &key in self.profile_prior.keys() {
                    keys.insert(key, ());
                }
            }
            for (&key, ()) in &keys {
                if key.0 < projected_concurrency || key.1 < pressure_class {
                    continue;
                }
                let Some(bound) = self.bound_for_key(now, key)? else {
                    continue;
                };
                let replace = match selected {
                    Some((_, best_bound)) => bound.value < best_bound.value,
                    None => true,
                };
                if replace {
                    selected = Some((key, bound));
                }
            }
        }

        if selected
            .as_ref()
            .is_some_and(|(_, bound)| bound.value >= reference)
        {
            if let Some(aggregate) = self.aggregate_live_lower_bound(now)? {
                if aggregate < reference {
                    return Ok(Admission {
                        abi_version: ABI_VERSION,
                        allowed: 0,
                        reason: ADMISSION_AGGREGATE_TPS_RISK,
                        observed: 1,
                        reference,
                        conservative_tps: aggregate,
                        projected_tps: aggregate,
                        projected_concurrency,
                        pressure_class,
                        evidence_concurrency: 0,
                        evidence_pressure_class: 0,
                        active_sequences: self.active,
                    });
                }
            }
        }

        match selected {
            Some(((evidence_concurrency, evidence_pressure), bound)) => {
                let allowed = bound.value >= reference;
                Ok(Admission {
                    abi_version: ABI_VERSION,
                    allowed: u32::from(allowed),
                    reason: if allowed {
                        if bound.used_prior && !bound.used_live {
                            ADMISSION_COLD_PRIOR
                        } else {
                            ADMISSION_FIT
                        }
                    } else {
                        ADMISSION_TPS_RISK
                    },
                    observed: u32::from(bound.used_live),
                    reference,
                    conservative_tps: bound.value,
                    projected_tps: bound.value,
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
                observed: 0,
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
        self.preference_until = 0.0;
        let Some(end) = self.prefill_end else {
            return Ok(0);
        };
        if decode == 0
            || prefill == 0
            || self.reference == 0.0
            || !self.observed
            || view.sequence_seconds_60s == 0.0
        {
            return Ok(0);
        }
        let mean = view.tokens_60s as f64 / view.sequence_seconds_60s;
        let signal = if view.sequence_seconds_2s > 0.0 {
            let weight = view.sequence_seconds_2s / (view.sequence_seconds_2s + 1.0);
            mean * (1.0 - weight) + (view.tokens_2s as f64 / view.sequence_seconds_2s) * weight
        } else {
            mean
        };
        let requested = if signal == 0.0 {
            MAX_PREFERENCE_SECONDS
        } else {
            (self.prefill_wall * (self.reference / signal - 1.0).max(0.0))
                .min(MAX_PREFERENCE_SECONDS)
        };
        let mass = view.sequence_seconds_60s;
        let duration = requested * (mass / (mass + 1.0)) / (1.0 + age / MAX_PREFERENCE_SECONDS);
        self.preference_until = end + duration;
        Ok(u32::from(now - end < duration))
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
    let governor = unsafe { &*handle };
    let mut state = governor.state.lock().map_err(|_| INTERNAL)?;
    let mut next = state.clone();
    let output = action(&mut next)?;
    *state = next;
    Ok(output)
}

#[no_mangle]
pub extern "C" fn pig_governor_abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_new(
    reference: f64,
    max_running_requests: u32,
    out: *mut *mut Governor,
) -> i32 {
    guarded(|| {
        if out.is_null() {
            return Err(INVALID);
        }
        let state = State::new(reference, max_running_requests)?;
        *out = Box::into_raw(Box::new(Governor {
            state: Mutex::new(state),
        }));
        Ok(())
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_new_with_profile(
    reference: f64,
    max_running_requests: u32,
    now: f64,
    ttl: f64,
    cells: *const ProfileCellV1,
    count: u32,
    out: *mut *mut Governor,
) -> i32 {
    guarded(|| {
        if out.is_null() || (count == 0) != cells.is_null() {
            return Err(INVALID);
        }
        let max_cells = (max_running_requests as usize)
            .checked_mul(PRESSURE_CLASSES as usize)
            .ok_or(INVALID)?;
        if count as usize > max_cells {
            return Err(INVALID);
        }
        let cells = if count == 0 {
            &[]
        } else {
            std::slice::from_raw_parts(cells, count as usize)
        };
        let state = State::new_with_profile(reference, max_running_requests, now, ttl, cells)?;
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
    guarded(|| {
        transaction(handle, |state| {
            state.observe_in_place(now, delta, active_after)
        })
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_observe_surface(
    handle: *mut Governor,
    now: f64,
    delta: u64,
    sequence_seconds: f64,
    concurrency: u32,
    pressure_class: u32,
) -> i32 {
    guarded(|| {
        transaction(handle, |state| {
            state.observe_surface(now, delta, sequence_seconds, concurrency, pressure_class)
        })
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_observe_batch(
    handle: *mut Governor,
    now: f64,
    delta: u64,
    sequence_seconds: f64,
    concurrency: u32,
    pressure_class: u32,
    active_after: u64,
) -> i32 {
    guarded(|| {
        transaction(handle, |state| {
            state.observe_batch_in_place(
                now,
                delta,
                sequence_seconds,
                concurrency,
                pressure_class,
                active_after,
            )
        })
    })
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_observe_replacement(
    handle: *mut Governor,
    now: f64,
    delta: u64,
    sequence_seconds: f64,
    concurrency: u32,
    pressure_class: u32,
    active_after: u64,
) -> i32 {
    guarded(|| {
        transaction(handle, |state| {
            state.observe_replacement_in_place(
                now,
                delta,
                sequence_seconds,
                concurrency,
                pressure_class,
                active_after,
            )
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
pub unsafe extern "C" fn pig_governor_start_surface_epoch(
    handle: *mut Governor,
    now: f64,
    active_after: u64,
) -> i32 {
    guarded(|| transaction(handle, |state| state.start_surface_epoch(now, active_after)))
}

#[no_mangle]
pub unsafe extern "C" fn pig_governor_export_profile(
    handle: *mut Governor,
    now: f64,
    buffer: *mut ProfileCellV1,
    capacity: u32,
    out_count: *mut u32,
) -> i32 {
    guarded(|| {
        if buffer.is_null() || out_count.is_null() {
            return Err(INVALID);
        }
        let cells = transaction(handle, |state| state.export_profile(now, capacity as usize))?;
        std::ptr::copy_nonoverlapping(cells.as_ptr(), buffer, cells.len());
        *out_count = cells.len() as u32;
        Ok(())
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
        let mut s = State::new(100.0, 4).unwrap();
        assert_eq!(s.observe(-1.0, 0, 1), Err(INVALID));
        assert_eq!(s.observe(1.0, 1, 1), Ok(()));
        assert_eq!(s.observe(0.5, 1, 1), Err(INVALID));
        let mut overflow = s.clone();
        assert_eq!(overflow.observe(1.5, u64::MAX, 1), Err(INVALID));
        assert_eq!(s.snapshot(1.0).unwrap().active_sequences, 1);
    }

    #[test]
    fn observation_rejects_active_count_above_native_capacity_atomically() {
        let mut s = State::new(50.0, 2).unwrap();
        s.observe(1.0, 3, 1).unwrap();
        let before = s.snapshot(1.0).unwrap();

        assert_eq!(s.observe(2.0, 7, 3), Err(INVALID));
        assert_eq!(s.last_time, Some(1.0));
        assert_eq!(s.snapshot(1.0).unwrap(), before);
        assert_eq!(s.observe_batch(2.0, 7, 1.0, 1, 0, 3), Err(INVALID));
        assert_eq!(s.last_time, Some(1.0));
        assert_eq!(s.snapshot(1.0).unwrap(), before);
    }

    #[test]
    fn aggregate_first_token_is_excluded_by_python_but_bucket_counts_delta() {
        let mut s = State::new(100.0, 4).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        s.observe(1.0, 1, 1).unwrap();
        let snapshot = s.snapshot(1.0).unwrap();
        assert_eq!(snapshot.tokens_60s, 1);
        assert_eq!(snapshot.sequence_seconds_60s, 1.0);
    }

    #[test]
    fn surface_observation_creates_exact_cell_and_windows() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_surface(1.0, 100, 1.0, 4, 0).unwrap();
        let admission = s.admission(1.0, 4, 0).unwrap();
        assert_eq!(admission.allowed, 1);
        assert_eq!(admission.reason, ADMISSION_FIT);
        assert_eq!(admission.evidence_concurrency, 4);
        assert_eq!(admission.evidence_pressure_class, 0);
        assert!((admission.projected_tps - 100.0).abs() < 1e-9);
    }

    #[test]
    fn batch_sequence_seconds_are_mass_not_elapsed_wall_time() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe(0.0, 0, 4).unwrap();
        s.observe_batch(1.0, 160, 4.0, 4, 0, 4).unwrap();
        let snapshot = s.snapshot(1.0).unwrap();
        assert_eq!(snapshot.tokens_60s, 160);
        assert_eq!(snapshot.sequence_seconds_60s, 4.0);
        assert_eq!(snapshot.tokens_2s, 160);
        assert_eq!(snapshot.sequence_seconds_2s, 4.0);
        let admission = s.admission(1.0, 4, 0).unwrap();
        assert_eq!(admission.allowed, 0);
        assert_eq!(admission.reason, ADMISSION_TPS_RISK);
        assert_eq!(admission.projected_tps, 40.0);
    }

    #[test]
    fn batch_after_snapshot_does_not_recount_prior_active_exposure() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe(1.0, 0, 1).unwrap();
        assert_eq!(s.snapshot(2.0).unwrap().sequence_seconds_60s, 1.0);

        s.observe_batch(3.0, 0, 2.0, 1, 0, 0).unwrap();
        let snapshot = s.snapshot(3.0).unwrap();
        assert_eq!(snapshot.sequence_seconds_60s, 2.0);
        assert_eq!(snapshot.active_sequences, 0);
    }

    #[test]
    fn exact_unsafe_cell_is_risk_even_with_zero_waiting() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_surface(1.0, 20, 1.0, 1, 0).unwrap();
        let admission = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(admission.allowed, 0);
        assert_eq!(admission.reason, ADMISSION_TPS_RISK);
        assert_eq!(admission.evidence_concurrency, 1);
    }

    #[test]
    fn heavier_cell_can_supply_conservative_bound() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_surface(1.0, 100, 1.0, 4, 1).unwrap();
        let admission = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(admission.allowed, 1);
        assert_eq!(admission.evidence_concurrency, 4);
        assert_eq!(admission.evidence_pressure_class, 1);
    }

    #[test]
    fn lighter_cell_never_extrapolates_upward() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_surface(1.0, 100, 1.0, 1, 0).unwrap();
        let admission = s.admission(1.0, 2, 0).unwrap();
        assert_eq!(admission.allowed, 0);
        assert_eq!(admission.reason, ADMISSION_UNKNOWN);
        assert_eq!(admission.evidence_concurrency, 0);
    }

    #[test]
    fn reference_zero_samples_and_cas_preserves_surface() {
        let mut s = State::new(0.0, 4).unwrap();
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
    fn zero_sequence_token_observation_cannot_raise_surface() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_surface(1.0, 4, 0.1, 1, 0).unwrap();
        let before = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(before.reason, ADMISSION_TPS_RISK);
        assert_eq!(before.projected_tps, 40.0);

        assert_eq!(s.observe_surface(1.1, 1000, 0.0, 1, 0), Err(INVALID));
        let after = s.admission(1.1, 1, 0).unwrap();
        assert_eq!(after.reason, ADMISSION_TPS_RISK);
        assert_eq!(after.projected_tps, 40.0);
    }

    #[test]
    fn zero_sequence_tokens_cannot_pollute_long_only_fallback() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_surface(1.0, 4, 0.1, 1, 0).unwrap();
        let before = s.admission(3.6, 1, 0).unwrap();
        assert_eq!(before.reason, ADMISSION_TPS_RISK);
        assert_eq!(before.projected_tps, 40.0);
        assert_eq!(s.observe_surface(3.6, 1000, 0.0, 1, 0), Err(INVALID));
        let after = s.admission(3.6, 1, 0).unwrap();
        assert_eq!(after.reason, ADMISSION_TPS_RISK);
        assert_eq!(after.projected_tps, 40.0);
    }

    #[test]
    fn invalid_zero_sequence_batch_is_fully_atomic() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_batch(1.0, 4, 0.1, 1, 0, 1).unwrap();
        let before = s.snapshot(1.0).unwrap();
        assert_eq!(s.observe_batch(1.1, 1000, 0.0, 1, 0, 4), Err(INVALID));
        let after = s.snapshot(1.0).unwrap();
        assert_eq!(after, before);
        let admission = s.admission(1.1, 1, 0).unwrap();
        assert_eq!(admission.reason, ADMISSION_TPS_RISK);
        assert_eq!(admission.projected_tps, 40.0);
    }

    #[test]
    fn zero_duration_without_tokens_does_not_extend_surface_qualification() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_surface(1.0, 100, 0.1, 1, 0).unwrap();
        s.observe_surface(59.0, 0, 0.0, 1, 0).unwrap();
        let admission = s.admission(62.0, 1, 0).unwrap();
        assert_eq!(admission.reason, ADMISSION_UNKNOWN);
    }

    #[test]
    fn aged_surface_requires_fresh_minimum_exposure() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_surface(1.0, 100, 0.1, 1, 0).unwrap();
        s.observe_surface(80.0, 1000, 0.001, 1, 0).unwrap();
        let admission = s.admission(80.0, 1, 0).unwrap();
        assert_eq!(admission.reason, ADMISSION_UNKNOWN);
    }

    #[test]
    fn sliding_window_rechecks_current_surface_exposure() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe_surface(1.0, 4, 0.1, 1, 0).unwrap();
        s.observe_surface(60.9, 1, 0.001, 1, 0).unwrap();
        assert_eq!(s.admission(60.9, 1, 0).unwrap().reason, ADMISSION_TPS_RISK);

        let admission = s.admission(61.6, 1, 0).unwrap();
        assert_eq!(admission.allowed, 0);
        assert_eq!(admission.reason, ADMISSION_UNKNOWN);
    }

    #[test]
    fn stale_or_insufficient_surface_is_unknown() {
        let mut s = State::new(50.0, 4).unwrap();
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

    fn profile_cell(concurrency: u32, pressure_class: u32, lower: f64) -> ProfileCellV1 {
        ProfileCellV1 {
            concurrency,
            pressure_class,
            long_tokens: 100,
            long_seconds: 1.0,
            short_tokens: 100,
            short_seconds: 1.0,
            approved_lower_tps: lower,
        }
    }

    #[test]
    fn aggregate_live_vetoes_cross_cell_prior_bypass() {
        let cells = [profile_cell(2, 0, 56.0)];
        let mut s = State::new_with_profile(50.0, 4, 0.0, 120.0, &cells).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        s.observe_batch(6.84046809701249, 25, 6.84046809701249, 1, 0, 1)
            .unwrap();

        let current = s.admission(6.84046809701249, 1, 0).unwrap();
        assert_eq!(current.reason, ADMISSION_TPS_RISK);
        let projected = s.admission(6.84046809701249, 2, 0).unwrap();
        assert_eq!(projected.allowed, 0);
        assert_eq!(projected.reason, ADMISSION_AGGREGATE_TPS_RISK);
        assert_eq!(projected.evidence_concurrency, 0);
        assert_eq!(projected.observed, 1);
        // Qualified short-window evidence still rejects the cross-cell bypass.
        assert!((projected.projected_tps - 10.681623916135177).abs() < 1e-12);
    }

    #[test]
    fn aggregate_live_does_not_replace_post_rejection_or_unknown() {
        let cells = [profile_cell(2, 0, 56.0)];
        let mut exact = State::new_with_profile(50.0, 4, 0.0, 120.0, &cells).unwrap();
        exact.observe(0.0, 0, 2).unwrap();
        exact.observe_batch(1.0, 80, 2.0, 2, 0, 2).unwrap();
        let rejected = exact.admission(1.0, 2, 0).unwrap();
        assert_eq!(rejected.reason, ADMISSION_TPS_RISK);
        assert_eq!(rejected.evidence_concurrency, 2);

        let mut unknown = State::new(50.0, 4).unwrap();
        unknown.observe(0.0, 0, 1).unwrap();
        unknown.observe_batch(1.0, 10, 1.0, 1, 0, 1).unwrap();
        let rejected = unknown.admission(1.0, 2, 0).unwrap();
        assert_eq!(rejected.reason, ADMISSION_UNKNOWN);
        assert_eq!(rejected.evidence_concurrency, 0);
    }

    #[test]
    fn aggregate_live_requires_current_run_tokens_and_ignores_idle_history() {
        let cells = [profile_cell(2, 0, 56.0)];
        let mut s = State::new_with_profile(50.0, 4, 0.0, 120.0, &cells).unwrap();

        s.observe(0.0, 0, 1).unwrap();
        s.observe_batch(1.0, 10, 1.0, 1, 0, 0).unwrap();
        let idle = s.admission(1.0, 2, 0).unwrap();
        assert_eq!(idle.reason, ADMISSION_COLD_PRIOR);

        s.observe(2.0, 0, 1).unwrap();
        let before_first_token = s.admission(2.2, 2, 0).unwrap();
        assert_eq!(before_first_token.reason, ADMISSION_COLD_PRIOR);

        s.observe_batch(2.3, 30, 0.1, 1, 0, 1).unwrap();
        let after_good_token = s.admission(2.3, 2, 0).unwrap();
        assert_eq!(after_good_token.reason, ADMISSION_COLD_PRIOR);
        assert_eq!(after_good_token.projected_tps, 56.0);
    }

    #[test]
    fn full_replacement_resets_only_private_run_evidence() {
        for duration in [0.0, 1.0] {
            let cells = [profile_cell(2, 0, 56.0)];
            let mut s = State::new_with_profile(50.0, 4, 0.0, 120.0, &cells).unwrap();
            s.observe(0.0, 0, 1).unwrap();
            s.observe_batch(1.0, 1000, 1.0, 1, 0, 1).unwrap();
            let revision = s.revision;
            let now = 1.0 + duration;
            let delta = if duration > 0.0 { 10 } else { 0 };
            s.observe_replacement(now, delta, duration, 1, 0, 1)
                .unwrap();
            assert_eq!(s.revision, revision);
            assert_eq!(s.snapshot(now).unwrap().tokens_60s, 1000 + delta);
            assert_eq!(s.admission(now, 1, 0).unwrap().allowed, 1);
            assert_eq!(s.admission(now, 2, 0).unwrap().reason, ADMISSION_COLD_PRIOR);
            s.observe_batch(now + 1.0, 1, 1.0, 1, 0, 1).unwrap();
            let result = s.admission(now + 1.0, 2, 0).unwrap();
            assert_eq!(result.reason, ADMISSION_AGGREGATE_TPS_RISK);
            assert_eq!(result.conservative_tps, 1.0);
            assert_eq!(s.snapshot(now + 1.0).unwrap().tokens_60s, 1001 + delta);
        }
    }

    #[test]
    fn invalid_replacement_preserves_all_native_state() {
        let mut s = State::new(50.0, 4).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        s.observe_batch(1.0, 1000, 1.0, 1, 0, 1).unwrap();
        let before = format!("{:?}", s);
        for (now, delta, duration, concurrency, pressure, active) in [
            (0.5, 1, 1.0, 1, 0, 1),
            (2.0, 1, 0.0, 1, 0, 1),
            (2.0, 1, 1.0, 2, 0, 1),
            (2.0, 1, 1.0, 1, 4, 1),
            (2.0, 1, 1.0, 1, 0, 5),
            (2.0, u64::MAX, 1.0, 1, 0, 1),
        ] {
            assert_eq!(
                s.observe_replacement(now, delta, duration, concurrency, pressure, active),
                Err(INVALID)
            );
            assert_eq!(format!("{:?}", s), before);
        }
    }

    #[test]
    fn aggregate_live_detects_continuing_stall_and_recovers_with_live_work() {
        let cells = [profile_cell(2, 0, 56.0)];
        let mut s = State::new_with_profile(50.0, 4, 0.0, 120.0, &cells).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        s.observe_batch(1.0, 100, 1.0, 1, 0, 1).unwrap();
        assert_eq!(s.admission(1.0, 2, 0).unwrap().reason, ADMISSION_COLD_PRIOR);

        let stalled = s.admission(3.1, 2, 0).unwrap();
        assert_eq!(stalled.reason, ADMISSION_AGGREGATE_TPS_RISK);
        assert!(stalled.projected_tps < 50.0);

        s.observe_batch(4.1, 300, 3.1, 1, 0, 1).unwrap();
        let recovered = s.admission(4.1, 2, 0).unwrap();
        assert_eq!(recovered.reason, ADMISSION_COLD_PRIOR);
        assert!(recovered.allowed != 0);
    }

    #[test]
    fn aggregate_live_recovers_while_long_window_is_still_slow() {
        let cells = [profile_cell(2, 0, 56.0)];
        let mut s = State::new_with_profile(50.0, 43, 0.0, 120.0, &cells).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        for (now, delta) in [(1.0, 5), (1.5, 5), (2.0, 55), (2.5, 55)] {
            s.observe_batch(now, delta, 0.5, 1, 0, 1).unwrap();
        }
        let recovered = s.admission(4.0, 2, 0).unwrap();
        assert_eq!(recovered.allowed, 1);
        assert_eq!(recovered.reason, ADMISSION_COLD_PRIOR);
        assert_eq!(recovered.projected_tps, 56.0);
    }

    #[test]
    fn aggregate_live_recovery_does_not_allow_sustained_slow_work() {
        let cells = [profile_cell(2, 0, 56.0)];
        let mut s = State::new_with_profile(50.0, 43, 0.0, 120.0, &cells).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        for now in [1.0, 1.5, 2.0, 2.5] {
            s.observe_batch(now, 5, 0.5, 1, 0, 1).unwrap();
        }
        let rejected = s.admission(4.0, 2, 0).unwrap();
        assert_eq!(rejected.allowed, 0);
        assert_eq!(rejected.reason, ADMISSION_AGGREGATE_TPS_RISK);
        assert!(rejected.projected_tps < 50.0);
    }

    #[test]
    fn aggregate_live_waits_for_minimum_real_exposure() {
        let cells = [profile_cell(2, 0, 56.0)];
        let mut s = State::new_with_profile(50.0, 43, 0.0, 120.0, &cells).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        s.observe_batch(0.05, 1, 0.05, 1, 0, 1).unwrap();
        assert_eq!(
            s.admission(0.05, 2, 0).unwrap().reason,
            ADMISSION_COLD_PRIOR
        );
        let qualified_slow = s.admission(0.1, 2, 0).unwrap();
        assert_eq!(qualified_slow.allowed, 0);
        assert_eq!(qualified_slow.reason, ADMISSION_AGGREGATE_TPS_RISK);
        assert_eq!(qualified_slow.projected_tps, 10.0);
    }

    #[test]
    fn aggregate_live_stays_zero_after_tokens_age_out_and_can_recover() {
        let cells = [profile_cell(2, 0, 56.0)];
        let mut s = State::new_with_profile(50.0, 4, 0.0, 180.0, &cells).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        s.observe_batch(1.0, 100, 1.0, 1, 0, 1).unwrap();

        let stalled = s.admission(62.1, 2, 0).unwrap();
        assert_eq!(stalled.reason, ADMISSION_AGGREGATE_TPS_RISK);
        assert_eq!(stalled.projected_tps, 0.0);

        s.observe_batch(63.1, 4_000, 1.0, 1, 0, 1).unwrap();
        let recovered = s.admission(63.1, 2, 0).unwrap();
        assert_eq!(recovered.reason, ADMISSION_COLD_PRIOR);
        assert!(recovered.allowed != 0);
    }

    #[test]
    fn aggregate_live_reference_zero_and_cas_keep_evidence() {
        let cells = [profile_cell(2, 0, 56.0)];
        let mut s = State::new_with_profile(0.0, 4, 0.0, 120.0, &cells).unwrap();
        s.observe(0.0, 0, 1).unwrap();
        s.observe_batch(1.0, 10, 1.0, 1, 0, 1).unwrap();
        assert_eq!(
            s.admission(1.0, 2, 0).unwrap().reason,
            ADMISSION_REFERENCE_DISABLED
        );

        assert_eq!(s.update_reference(9, 50.0), Err(CONFLICT));
        s.update_reference(1, 50.0).unwrap();
        let rejected = s.admission(1.0, 2, 0).unwrap();
        assert_eq!(rejected.reason, ADMISSION_AGGREGATE_TPS_RISK);
        assert_eq!(rejected.projected_tps, 10.0);
    }

    #[test]
    fn profile_exact_precedes_heavier_and_live_can_lower_it() {
        let cells = [profile_cell(1, 0, 80.0), profile_cell(4, 1, 20.0)];
        let mut s = State::new_with_profile(50.0, 4, 1.0, 10.0, &cells).unwrap();

        let exact = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(exact.allowed, 1);
        assert_eq!(exact.reason, ADMISSION_COLD_PRIOR);
        assert_eq!(exact.observed, 0);
        assert_eq!(exact.evidence_concurrency, 1);
        assert_eq!(exact.projected_tps, 80.0);

        s.observe_surface(1.05, 10, 0.01, 2, 0).unwrap();
        let unrelated = s.admission(1.05, 1, 0).unwrap();
        assert_eq!(unrelated.reason, ADMISSION_COLD_PRIOR);
        assert_eq!(unrelated.observed, 0);
        assert_eq!(unrelated.projected_tps, 80.0);

        s.observe_surface(1.1, 0, 0.01, 1, 0).unwrap();
        let lowered = s.admission(1.1, 1, 0).unwrap();
        assert_eq!(lowered.allowed, 0);
        assert_eq!(lowered.reason, ADMISSION_TPS_RISK);
        assert_eq!(lowered.observed, 1);
        assert_eq!(lowered.projected_tps, 0.0);
        assert_eq!(lowered.evidence_concurrency, 1);
    }

    #[test]
    fn unqualified_high_live_cannot_relax_prior_but_qualified_live_is_provenance() {
        let cells = [profile_cell(1, 0, 80.0)];
        let mut s = State::new_with_profile(50.0, 4, 1.0, 10.0, &cells).unwrap();
        s.observe_surface(1.01, 50, 0.05, 1, 0).unwrap();
        let unqualified = s.admission(1.01, 1, 0).unwrap();
        assert_eq!(unqualified.reason, ADMISSION_COLD_PRIOR);
        assert_eq!(unqualified.observed, 0);
        assert_eq!(unqualified.projected_tps, 80.0);

        s.observe_surface(1.1, 50, 0.05, 1, 0).unwrap();
        let qualified = s.admission(1.1, 1, 0).unwrap();
        assert_eq!(qualified.reason, ADMISSION_FIT);
        assert_eq!(qualified.observed, 1);
        assert_eq!(qualified.projected_tps, 80.0);

        let expired = s.admission(11.0, 1, 0).unwrap();
        assert_eq!(expired.reason, ADMISSION_FIT);
        assert_eq!(expired.observed, 1);
        assert_eq!(expired.projected_tps, 1000.0);
    }

    // Cold-start transient replayed from GPU805 run 20260922t174940z-1975864:
    // 17 slow ticks then a healthy 64 tps tail, cumulative 23.52 tps. The next
    // admission must recover from the fresh short window instead of being
    // vetoed by the transient retained in the long window.
    fn cold_transient_cell() -> ProfileCellV1 {
        ProfileCellV1 {
            concurrency: 1,
            pressure_class: 0,
            long_tokens: 1125,
            long_seconds: 20.0,
            short_tokens: 113,
            short_seconds: 2.0,
            approved_lower_tps: 56.25,
        }
    }

    fn feed_cold_run(s: &mut State, start: f64, healthy_tail: bool) {
        let ticks = if healthy_tail { 21 } else { 17 };
        for index in 0..ticks {
            let delta = if index < 17 { 7 } else { 32 };
            s.observe_surface(start + 0.5 * f64::from(index + 1), delta, 0.5, 1, 0)
                .unwrap();
        }
    }

    #[test]
    fn fresh_short_window_recovers_cold_transient_with_prior() {
        let cells = [cold_transient_cell()];
        let mut s = State::new_with_profile(50.0, 43, 1000.0, 1_000_000.0, &cells).unwrap();
        let first = s.admission(1000.1, 1, 0).unwrap();
        assert_eq!(first.allowed, 1);
        assert_eq!(first.reason, ADMISSION_COLD_PRIOR);

        feed_cold_run(&mut s, 1000.1, true);
        let recurring = s.admission(1011.6, 1, 0).unwrap();
        assert_eq!(recurring.allowed, 1);
        assert_eq!(recurring.reason, ADMISSION_FIT);
        assert_eq!(recurring.observed, 1);
        assert!(recurring.conservative_tps >= 50.0);
        assert_eq!(recurring.conservative_tps, 56.25);
    }

    #[test]
    fn fresh_short_window_recovers_live_only_cell() {
        let mut s = State::new(50.0, 43).unwrap();
        feed_cold_run(&mut s, 1000.1, true);
        let recurring = s.admission(1011.6, 1, 0).unwrap();
        assert_eq!(recurring.allowed, 1);
        assert_eq!(recurring.reason, ADMISSION_FIT);
        assert_eq!(recurring.observed, 1);
        assert_eq!(recurring.conservative_tps, 64.0);
    }

    #[test]
    fn sustained_slow_surface_never_recovers() {
        let cells = [cold_transient_cell()];
        let mut s = State::new_with_profile(50.0, 43, 1000.0, 1_000_000.0, &cells).unwrap();
        feed_cold_run(&mut s, 1000.1, false);
        let recurring = s.admission(1010.1, 1, 0).unwrap();
        assert_eq!(recurring.allowed, 0);
        assert_eq!(recurring.reason, ADMISSION_TPS_RISK);
        assert_eq!(recurring.conservative_tps, 14.0);
    }

    #[test]
    fn idle_gap_beyond_short_window_keeps_stale_bound() {
        let cells = [cold_transient_cell()];
        let mut s = State::new_with_profile(50.0, 43, 1000.0, 1_000_000.0, &cells).unwrap();
        feed_cold_run(&mut s, 1000.1, true);
        let recurring = s.admission(1014.1, 1, 0).unwrap();
        assert_eq!(recurring.allowed, 0);
        assert_eq!(recurring.reason, ADMISSION_TPS_RISK);
        assert!((recurring.conservative_tps - 23.523809523809526).abs() < 1e-9);
    }

    #[test]
    fn stale_low_cell_can_refill_one_step_from_prior_after_healthy_lower_load() {
        let cells = [profile_cell(39, 2, 58.0)];
        let mut s = State::new_with_profile(50.0, 43, 1.0, 100.0, &cells).unwrap();
        s.observe_batch(2.0, 0, 0.5, 39, 2, 38).unwrap();
        assert_eq!(s.admission(2.1, 39, 2).unwrap().reason, ADMISSION_TPS_RISK);
        s.observe(2.5, 1200, 38).unwrap();
        s.observe(3.0, 1200, 38).unwrap();
        assert_eq!(s.admission(3.1, 39, 2).unwrap().reason, ADMISSION_TPS_RISK);
        s.observe(4.1, 2200, 38).unwrap();
        let recovered = s.admission(4.1, 39, 2).unwrap();
        assert_eq!(recovered.allowed, 1);
        assert_eq!(recovered.reason, ADMISSION_COLD_PRIOR);
        assert_eq!(recovered.conservative_tps, 58.0);
    }

    #[test]
    fn stale_low_cell_reprobe_still_obeys_aggregate_veto() {
        let cells = [profile_cell(39, 2, 58.0)];
        let mut s = State::new_with_profile(50.0, 43, 1.0, 100.0, &cells).unwrap();
        s.observe_batch(2.0, 0, 0.5, 39, 2, 38).unwrap();
        s.observe(2.5, 100, 38).unwrap();
        s.observe(3.0, 100, 38).unwrap();
        s.observe(4.1, 100, 38).unwrap();
        let denied = s.admission(4.1, 39, 2).unwrap();
        assert_eq!(denied.allowed, 0);
        assert_eq!(denied.reason, ADMISSION_AGGREGATE_TPS_RISK);
    }

    #[test]
    fn stale_low_cell_without_current_run_tokens_cannot_reprobe() {
        let cells = [profile_cell(39, 2, 58.0)];
        let mut s = State::new_with_profile(50.0, 43, 1.0, 100.0, &cells).unwrap();
        s.observe_batch(2.0, 0, 0.5, 39, 2, 38).unwrap();
        let denied = s.admission(4.1, 39, 2).unwrap();
        assert_eq!(denied.allowed, 0);
        assert_eq!(denied.reason, ADMISSION_TPS_RISK);
    }

    #[test]
    fn stale_low_cell_reprobe_cannot_skip_a_slot_or_use_expired_prior() {
        let cells = [profile_cell(39, 2, 58.0)];
        let mut skipped = State::new_with_profile(50.0, 43, 1.0, 100.0, &cells).unwrap();
        skipped.observe_batch(2.0, 0, 0.5, 39, 2, 37).unwrap();
        skipped.observe(2.5, 1200, 37).unwrap();
        skipped.observe(3.0, 1200, 37).unwrap();
        skipped.observe(4.1, 2200, 37).unwrap();
        assert_eq!(
            skipped.admission(4.1, 39, 2).unwrap().reason,
            ADMISSION_TPS_RISK
        );

        let mut expired = State::new_with_profile(50.0, 43, 1.0, 3.0, &cells).unwrap();
        expired.observe_batch(2.0, 0, 0.5, 39, 2, 38).unwrap();
        expired.observe(2.5, 1200, 38).unwrap();
        expired.observe(3.0, 1200, 38).unwrap();
        expired.observe(4.1, 2200, 38).unwrap();
        assert_eq!(
            expired.admission(4.1, 39, 2).unwrap().reason,
            ADMISSION_TPS_RISK
        );
    }

    #[test]
    fn stale_low_live_only_cell_does_not_reprobe() {
        let mut s = State::new(50.0, 43).unwrap();
        s.observe_batch(2.0, 0, 0.5, 39, 2, 38).unwrap();
        s.observe(2.5, 1200, 38).unwrap();
        s.observe(3.0, 1200, 38).unwrap();
        s.observe(4.1, 2200, 38).unwrap();
        assert_eq!(s.admission(4.1, 39, 2).unwrap().reason, ADMISSION_TPS_RISK);
    }

    #[test]
    fn sub_exposure_burst_never_lifts_stale_bound() {
        let mut s = State::new(50.0, 4).unwrap();
        for at in [0.5, 1.0, 1.4] {
            s.observe_surface(at, 40, 1.0, 1, 0).unwrap();
        }
        // 500 tps for 0.05 sequence-seconds: fresh but below the minimum
        // qualified exposure, so it must not recover the stale low bound.
        s.observe_surface(3.5, 25, 0.05, 1, 0).unwrap();
        let admission = s.admission(3.5, 1, 0).unwrap();
        assert_eq!(admission.allowed, 0);
        assert_eq!(admission.reason, ADMISSION_TPS_RISK);
        assert!((admission.conservative_tps - 145.0 / 3.05).abs() < 1e-9);

        // Crossing the minimum exposure in the same short window recovers.
        s.observe_surface(3.55, 30, 0.06, 1, 0).unwrap();
        let recovered = s.admission(3.55, 1, 0).unwrap();
        assert_eq!(recovered.allowed, 1);
        assert_eq!(recovered.reason, ADMISSION_FIT);
        assert_eq!(recovered.conservative_tps, 500.0);
    }

    #[test]
    fn export_keeps_raw_lower_bound_after_recovery() {
        let mut s = State::new(50.0, 43).unwrap();
        feed_cold_run(&mut s, 1000.1, true);
        let cells = s.export_profile(1011.6, 172).unwrap();
        assert_eq!(cells.len(), 1);
        // The exported approved rate stays the raw conservative bound so the
        // profile never attests more than its stored windows derive.
        assert!((cells[0].approved_lower_tps - 23.523809523809526).abs() < 1e-9);
        cells[0].validate(43).unwrap();
    }

    #[test]
    fn profile_heavier_fallback_is_most_conservative_and_expires() {
        let cells = [profile_cell(2, 0, 70.0), profile_cell(4, 1, 30.0)];
        let mut s = State::new_with_profile(50.0, 4, 1.0, 2.0, &cells).unwrap();
        let fallback = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(fallback.allowed, 0);
        assert_eq!(fallback.projected_tps, 30.0);
        assert_eq!(fallback.evidence_concurrency, 4);
        assert_eq!(fallback.evidence_pressure_class, 1);

        assert_eq!(s.admission(2.999, 1, 0).unwrap().projected_tps, 30.0);
        let expired = s.admission(3.0, 1, 0).unwrap();
        assert_eq!(expired.reason, ADMISSION_UNKNOWN);
        assert_eq!(expired.observed, 0);
    }

    #[test]
    fn reference_zero_and_cas_preserve_profile_prior() {
        let cells = [profile_cell(1, 0, 80.0)];
        let mut s = State::new_with_profile(0.0, 4, 1.0, 10.0, &cells).unwrap();
        assert_eq!(
            s.admission(1.0, 1, 0).unwrap().reason,
            ADMISSION_REFERENCE_DISABLED
        );
        assert_eq!(s.update_reference(9, 50.0), Err(CONFLICT));
        s.update_reference(1, 50.0).unwrap();
        let admission = s.admission(1.0, 1, 0).unwrap();
        assert_eq!(admission.allowed, 1);
        assert_eq!(admission.projected_tps, 80.0);
    }

    #[test]
    fn invalid_profile_evidence_is_rejected_atomically() {
        let valid = profile_cell(1, 0, 80.0);
        let duplicate = [valid, valid];
        assert!(State::new_with_profile(50.0, 4, 1.0, 10.0, &duplicate).is_err());

        let mut invalid = valid;
        invalid.short_tokens = 101;
        assert!(State::new_with_profile(50.0, 4, 1.0, 10.0, &[invalid]).is_err());
        invalid = valid;
        invalid.long_seconds = 0.09;
        assert!(State::new_with_profile(50.0, 4, 1.0, 10.0, &[invalid]).is_err());
        invalid = valid;
        invalid.approved_lower_tps = 100.1;
        assert!(State::new_with_profile(50.0, 4, 1.0, 10.0, &[invalid]).is_err());
        assert!(State::new_with_profile(50.0, 4, 1.0, 0.0, &[valid]).is_err());
    }

    #[test]
    fn surface_epoch_reset_preserves_policy_and_clears_all_evidence() {
        let cells = [profile_cell(1, 0, 80.0)];
        let mut s = State::new_with_profile(50.0, 4, 10.0, 10.0, &cells).unwrap();
        s.update_reference(1, 60.0).unwrap();
        s.observe_batch(11.0, 100, 1.0, 1, 0, 1).unwrap();
        s.prefill(11.0, 0.5).unwrap();

        assert_eq!(s.start_surface_epoch(2.0, 5), Err(INVALID));
        assert_eq!(s.snapshot(11.0).unwrap().tokens_60s, 100);
        s.start_surface_epoch(2.0, 3).unwrap();
        let snapshot = s.snapshot(2.0).unwrap();
        assert_eq!(snapshot.reference, 60.0);
        assert_eq!(snapshot.revision, 2);
        assert_eq!(snapshot.tokens_60s, 0);
        assert_eq!(snapshot.active_sequences, 3);
        assert_eq!(snapshot.last_prefill_end, -1.0);
        assert!(s.surface.is_empty());
        assert!(s.profile_prior.is_empty());
        assert_eq!(s.admission(2.0, 1, 0).unwrap().reason, ADMISSION_UNKNOWN);
    }

    #[test]
    fn export_contains_only_qualified_live_evidence() {
        let cells = [profile_cell(2, 0, 20.0)];
        let mut s = State::new_with_profile(50.0, 4, 1.0, 10.0, &cells).unwrap();
        assert!(s.export_profile(1.0, 16).unwrap().is_empty());
        s.observe_surface(1.0, 40, 1.0, 1, 0).unwrap();
        let exported = s.export_profile(1.0, 16).unwrap();
        assert_eq!(exported.len(), 1);
        assert_eq!(exported[0].concurrency, 1);
        assert_eq!(exported[0].long_tokens, 40);
        assert_eq!(exported[0].approved_lower_tps, 40.0);

        let before = s.clone();
        assert_eq!(s.export_profile(2.0, 0), Err(INVALID));
        assert_eq!(s.last_time, before.last_time);
        assert_eq!(s.active, before.active);
    }

    #[test]
    fn preference_is_short_ages_and_never_reanchors_to_evaluation() {
        let mut s = State::new(100.0, 4).unwrap();
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
            assert_eq!(pig_governor_new(30.0, 4, &mut h), OK);
            assert_eq!(pig_governor_observe(h, 0.0, 0, 1), OK);
            assert_eq!(pig_governor_observe(h, 1.0, 12, 1), OK);
            let mut before = Snapshot::default();
            assert_eq!(pig_governor_snapshot(h, 1.0, &mut before), OK);
            let mut admission = Admission::default();
            assert_eq!(pig_governor_admit(h, 1.0, 1, 0, &mut admission), OK);
            assert_eq!(admission.abi_version, ABI_VERSION);
            assert_eq!(pig_governor_observe_surface(h, 1.0, 10, 1.0, 1, 0), OK);
            // These fail after clock advancement and surface mutation inside
            // the transaction. None may publish the partial state.
            assert_eq!(pig_governor_observe(h, 2.0, u64::MAX, 1), INVALID);
            assert_eq!(
                pig_governor_observe_batch(h, 2.0, u64::MAX, 1.0, 1, 0, 1),
                INVALID
            );
            assert_eq!(
                pig_governor_observe_replacement(h, 2.0, u64::MAX, 1.0, 1, 0, 1),
                INVALID
            );
            assert_eq!(
                pig_governor_observe_batch(h, 2.0, 10, 1.0, 5, 0, 1),
                INVALID
            );
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
    fn ffi_profile_creation_is_all_or_nothing() {
        assert_eq!(std::mem::size_of::<ProfileCellV1>(), 48);
        unsafe {
            let cell = profile_cell(1, 0, 80.0);
            let mut untouched = 1usize as *mut Governor;
            assert_eq!(
                pig_governor_new_with_profile(
                    50.0,
                    4,
                    0.0,
                    10.0,
                    std::ptr::null(),
                    1,
                    &mut untouched,
                ),
                INVALID
            );
            assert_eq!(untouched, 1usize as *mut Governor);
            assert_eq!(
                pig_governor_new_with_profile(50.0, 4, 0.0, 10.0, &cell, 0, &mut untouched),
                INVALID
            );
            assert_eq!(
                pig_governor_new_with_profile(50.0, 1, 0.0, 10.0, &cell, 5, &mut untouched),
                INVALID
            );
            assert_eq!(untouched, 1usize as *mut Governor);

            let mut empty = std::ptr::null_mut();
            assert_eq!(
                pig_governor_new_with_profile(50.0, 4, 0.0, 10.0, std::ptr::null(), 0, &mut empty,),
                OK
            );
            assert_eq!(pig_governor_free(empty), OK);

            let duplicate = [cell, cell];
            let mut h = std::ptr::null_mut();
            assert_eq!(
                pig_governor_new_with_profile(
                    50.0,
                    4,
                    0.0,
                    10.0,
                    duplicate.as_ptr(),
                    duplicate.len() as u32,
                    &mut h,
                ),
                INVALID
            );
            assert!(h.is_null());
            assert_eq!(
                pig_governor_new_with_profile(50.0, 4, 0.0, 10.0, &cell, 1, &mut h),
                OK
            );
            let mut admission = Admission::default();
            assert_eq!(pig_governor_admit(h, 0.0, 1, 0, &mut admission), OK);
            assert_eq!(admission.projected_tps, 80.0);
            assert_eq!(pig_governor_free(h), OK);
        }
    }

    #[test]
    fn ffi_export_failure_preserves_output_and_clock() {
        unsafe {
            let mut h = std::ptr::null_mut();
            assert_eq!(pig_governor_new(50.0, 4, &mut h), OK);
            assert_eq!(pig_governor_observe_surface(h, 1.0, 40, 1.0, 1, 0), OK);
            let mut cell = profile_cell(99, 3, 7.0);
            let mut count = 77;
            assert_eq!(
                pig_governor_export_profile(h, 2.0, &mut cell, 0, &mut count),
                INVALID
            );
            assert_eq!(cell.concurrency, 99);
            assert_eq!(count, 77);
            assert_eq!(pig_governor_observe_surface(h, 1.5, 4, 0.1, 1, 0), OK);
            assert_eq!(
                pig_governor_export_profile(h, 1.5, std::ptr::null_mut(), 1, &mut count),
                INVALID
            );
            assert_eq!(
                pig_governor_export_profile(h, 1.5, &mut cell, 1, std::ptr::null_mut()),
                INVALID
            );
            assert_eq!(cell.concurrency, 99);
            assert_eq!(count, 77);
            assert_eq!(
                pig_governor_export_profile(h, 1.5, &mut cell, 1, &mut count),
                OK
            );
            assert_eq!(count, 1);
            assert_eq!(cell.concurrency, 1);
            assert_eq!(cell.long_tokens, 44);
            assert_eq!(pig_governor_free(h), OK);
        }
    }

    #[test]
    fn abi_rejects_nulls_and_bad_creation_without_touching_outputs() {
        unsafe {
            let mut h = std::ptr::null_mut();
            assert_eq!(pig_governor_abi_version(), 4);
            assert_eq!(pig_governor_new(-1.0, 4, &mut h), INVALID);
            assert_eq!(pig_governor_new(30.0, 0, &mut h), INVALID);
            assert!(h.is_null());
            assert_eq!(pig_governor_new(30.0, 4, std::ptr::null_mut()), INVALID);
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
