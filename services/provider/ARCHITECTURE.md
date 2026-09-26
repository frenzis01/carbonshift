# Provider — architectural decision record

Why the provider is shaped this way, which alternatives were rejected, and
what still has to be decided. `INTERFACE.md` describes *what* the service
exposes; this file describes *why*.

---

## 1. The problem with time as it stands

Today there are **three independent clocks** and no owner:

| Component | Clock | Driven by |
|---|---|---|
| carbonshift | `SharedState::virtual_elapsed_ms` → `current_slot` (an uptime counter from 0) | its own `main_loop`; frozen when `MANUAL_CLOCK=1` |
| executor | `VirtualClock` (wall clock, or frozen) | its own thread; frozen when `EXECUTOR_MANUAL_CLOCK=1` |
| client | derives slots from each request's absolute `start_at` | its own `run_plan` loop |

In manual-clock mode the **client** is today's de-facto clock driver: it issues
`POST /v1/admin/advance-slot` to carbonshift and then to the executor
(`client/app/plan_runner.py::_advance_slot`). Three consequences:

1. **The time owner is a test harness.** The component that decides "the slot
   has changed" is the load generator, which is also the component that *wants*
   time to move. That is a conflict of interest, and it is the same component
   that would otherwise be the customer of a real carbon-intensity feed.
2. **A failed advance is swallowed.** `_advance_slot` logs the exception and
   `return`s. If carbonshift advances but the executor does not, the two drift
   apart and nothing fails loudly; runs just produce quietly wrong metrics.
3. **The forecast is frozen at boot.** carbonshift generates its forecast once
   (`bin/service/main.rs`) and passes it to `BatchScheduler`, which holds an
   `Arc<Vec<f64>>`. A real provider cannot change the scheduler's world view
   without a restarting-length change (`Arc<RwLock<…>>` through every solver
   signature).

So the provider is not merely "a new source of numbers" — it is the natural
place to put **single ownership of time**, because it is the one component
whose job is inherently "what slot is it, and what does the grid look like
now".

---

## 2. Who owns the clock? (the main decision)

### Option A — provider becomes the time master (`POST /v1/advance-slot`)  ✅ chosen

The provider owns the clock; on each rollover it pushes the new slot *and* a
fresh forecast window to the peers, in a fixed order, with retries.

**Why:** it collapses three responsibilities that all derive from the same
fact ("this slot ended and the next began") into one place, and it is the only
option that lets the forecast *refresh* rather than being frozen at boot. It
also removes the conflict of interest in (1): the load generator no longer
decides when time moves.

**Cost:** the provider becomes a single point of failure for the whole
simulation. Mitigated by making the notification idempotent (`expect_slot`) so
a retry is safe, and by reporting partial failure instead of hiding it.

### Option B — peers derive the slot from wall time, provider only serves data  ❌ rejected

Each component computes `floor((now - epoch) / slot_minutes)` independently
from a shared epoch. Elegant: no clock owner, no push protocol.

**Why rejected for emulation:** it makes emulation *impossible*. Emulation's
entire point is to make 24 hours of simulated time pass in seconds, which
requires the clock to be **decoupled from wall time** — and a clock that is a
pure function of wall time cannot be decoupled from it. Option B is however
the right design for **production**, where nobody wants to compress time.

> ⚠️ **This is the key insight for the overall design**: manual-clock
> emulation and real-time operation have *opposite* clock requirements. Any
> architecture that tries to serve both with one mechanism will be worse at
> both. The provider therefore supports both (real mode auto-advances on its
> own; manual mode is driven explicitly) behind one slot contract.

### Option C — keep the client as the driver, provider only serves data  ❌ rejected

Least change. **Why rejected:** it keeps the harness as the time owner, keeps
the swallowed-failure behaviour, and leaves the forecast frozen at boot. It
also means every future consumer of carbon intensity (a real deployment) has
to reinvent the clock protocol, because the client is a test fixture.

### Option D — a message broker (Redis/NATS) with the provider publishing ticks  ❌ deferred

Would give real delivery guarantees and fan-out without hand-rolled retries.

**Why deferred, not rejected on merit:** the project currently has *no*
broker, no shared database, and a deliberate "plain HTTP/JSON with callbacks"
transport (`AGENTS.md`). Introducing one to solve a problem that bounded
retries plus an idempotency token already solve is a much larger change than
the problem justifies. Worth revisiting if a third or fourth consumer appears.

---

## 3. Ordering: why carbonshift is notified before the executor

The fan-out order is a declared policy (`notifications.peers_from_settings`),
not an accident of list order:

| Order | Peer | Path | Why here |
|---|---|---|---|
| 10 | carbonshift | `/v1/admin/advance-slot` | It owns **dispatch**. Its handler blocks until every assignment for the slot just left has been handed to the executor, so advancing it first guarantees nothing is still pending when the executor's clock moves. |
| 20 | executor | `/admin/advance-slot` | Only then may its own clock move, since it is the one that actually runs the work. |

The reverse order is a real bug, not a theoretical one: if the executor
advanced first, a `/dispatch` could arrive for a slot the executor had already
left, and the job would sit in a slot in the past (or be skipped) instead of
running.

**The executor is optional in the fan-out.** It is only needed if the executor
needs to advance with the simulation; in a setup where carbonshift's dispatcher
delivers jobs with no `execute_at` (which is what happens today), jobs execute
on arrival and the executor's own clock matters only for slot bookkeeping. It
is therefore configured rather than assumed.

### The notification contract is *not* a dispatch

Worth stating explicitly because it is easy to conflate: the provider notifies
**clocks**, it does not dispatch work. Sending jobs to the executor remains
carbonshift's dispatcher's job, and the callback chain back to the client is
untouched. The provider sits beside that data path, not in it — which is why
adding it cannot break request/callback/execution routing.

---

## 4. How requests are not lost when someone is behind

The user's specific worry. Four mechanisms, in order of importance:

1. **One advance = exactly one slot.** `ProviderClock.advance()` moves the
   frozen instant by `slot_minutes`, never more. There is no "catch up by N
   slots" operation, because a multi-slot jump is precisely what strands work:
   the peers would be told to skip slots they never processed.
2. **Idempotency token.** `POST /v1/advance-slot` accepts `expect_slot`; if the
   provider is not at that slot it returns 409. A retried rollover therefore
   cannot double-advance the system, which is what makes the retries in (3)
   safe to add.
3. **Ordered fan-out with retries, aborting on first failure.** Each peer gets
   `notify_max_attempts` tries; the loop **stops** at the first peer that
   exhausts them instead of advancing later peers past it. The failure is
   returned in `deliveries` rather than logged and dropped as today.
4. **The notification body carries the whole forecast window.** A peer that
   missed a notification (restart, transient outage) resynchronizes from the
   next one without needing a separate recovery endpoint or a pull protocol.

What is deliberately *not* attempted: exactly-once delivery. That needs
durable queues and transactions, which is disproportionate here. The design
targets **at-least-once with idempotent application**, which is the standard
and achievable answer.

---

## 5. Local vs remote behind one port

The requirement was "structure it so both can coexist cleanly". Implemented as
a port/adapter pair (`app/source.py`):

```
                 ┌────────────────────────────┐
   main.py ──────▶│ CarbonIntensitySource (ABC)│
   clock.py       └────────────┬───────────────┘
   notifications.py            │
                     ┌─────────┴──────────┐
                     ▼                    ▼
        SyntheticCarbonIntensity   RemoteCarbonIntensity
        (local: deterministic)     (remote: carbonintensity.org.uk)
```

Both are selected by `PROVIDER_ROLE` in `build_source()`. Adding the remote
adapter for real is therefore **one class body plus nothing else** — no change
to `main.py`, `clock.py`, `notifications.py`, or any peer. That is the
Open/Closed property the codebase is currently missing for its solver
strategies (see the root `AGENTS.md` architecture assessment), applied at the
seam where it actually pays off.

Three design points that make the seam honest rather than decorative:

* **A measurement is an event, not a property of a slot.** A remote source
  physically cannot know the real intensity of a future slot; the local one
  could fabricate it, but must not. Rather than declare a capability flag
  (`provides_actual_for_future_slots`, which this design originally had and
  then removed — see §10), the *shape* of the API enforces it: `forecast()`
  returns `ForecastPoint` (no `actual` field at all) and `observe()` returns
  an `ObservedPoint` only for the slot being measured. There is no way to ask
  for a future measurement, so "pretend the forecast is a measurement" is not
  expressible. This is what will force the emulation flow to change shape when
  the remote role is enabled — correctly, because that shortcut must not
  survive into production.
* **`actual` is `null`, never a copy of `forecast`.** Making an unknown
  measurement look like a known one is how a carbon-saving metric silently
  becomes fiction.
* **The remote adapter is deliberately a stub that returns `[]`** rather than
  raising. This keeps the clock loop alive and makes the seam testable now,
  while refusing to ship a half-correct measurement as if it were real.

### Why the remote adapter was not implemented

It is held back for reasons that are decisions, not laziness:

1. **Native resolution is half-hourly.** `carbonintensity.org.uk` reports
   fixed 30-minute settlement periods, so it maps 1:1 onto carbonshift slots
   **only at `slot_minutes == 30`**. Any other slot length needs resampling,
   and the right resampling (mean? max? the value at the slot start?) is a
   domain decision about what carbon cost should mean.
2. **It is GB-only**, and needs an attribution/licence decision (CC BY 4.0).
3. **It changes the emulation contract** (see above), which deserves its own
   change rather than being smuggled in with an HTTP client.

---

## 6. Things this design deliberately does NOT do

* **It does not put the provider in the dispatch path.** Jobs, callbacks and
  results never traverse it, so a provider outage degrades *optimisation
  quality*, not *correctness of execution*. This is what makes it safe to add.
* **It does not let callers push a forecast** (`POST /v1/forecast` is not
  implemented). Accepting caller-supplied forecasts would make the scheduler's
  carbon maths unauditable: nobody could later tell whether a saving came from
  good scheduling or from a flattering input.
* **It does not feed the live solver yet.** carbonshift still plans against a
  forecast frozen at boot, and the provider's pushed window is currently only
  usable for the post-hoc `actual_carbon_intensity` correction. Closing that
  gap is a real change to the scheduler (below), not a wiring detail.
* **It does not advance multiple slots to "catch up".** The peers are expected
  to keep up; if they cannot, the failure surfaces instead of being absorbed
  into a slot skip.

---

## 7. Slot alignment — the integration hazard

**This is the one thing that will silently misbehave if ignored.**

carbonshift's `current_slot` starts at **0** and counts up from process start
(`virtual_elapsed_ms` is an uptime counter). The provider publishes **global**
slots aligned to a fixed epoch — measured live, `118015` right now. The two are
off by the process-uptime offset, so a pushed forecast indexed by global slot
would be read by the scheduler at the wrong index — and because it would land
outside its array it would be **silently ignored**, not obviously broken.

The fix chosen: an explicit, documented offset.

* `Config::slot_epoch_offset: i64` (default `0`, so simulations, tests and the
  built-in synthetic forecast are unaffected).
* Conversion happens in **one place**: `engine_slot = global_slot -
  slot_epoch_offset`.

Alternatives considered:

| Alternative | Verdict |
|---|---|
| Make both spaces global (`current_slot` = absolute) | Cleanest end state, but it breaks every existing test that assumes `current_slot` starts at 0 and touches `advance_to_next_slot`, the flush logic and the DP's `window_size` bound. Too invasive to bundle with "add a provider". |
| Have the provider emit engine-relative slots | Pushes a carbonshift-specific concept into a service whose whole purpose is to be reusable (and would break as soon as a second consumer with a different offset exists). |
| Explicit offset, converted in one place (**chosen**) | Small, testable, reversible, and keeps the provider's contract absolute/universal. |

**Until `slot_epoch_offset` is set correctly, the pushed forecast will be
quietly ignored.** That is why it is documented in three places and defaulted
to the backwards-compatible `0`.

---

## 8. Recommended migration path

Do it in stages; each stage is independently useful and verifiable.

**Stage 1 — provider runs beside the client (no behaviour change).**
Start the provider in `local` role. Have `run_plan` read
`GET /v1/forecast` instead of calling `GET /v1/carbon-forecast` and inventing
the jitter itself (`_perturbed_actual_ci` disappears from the client). The
client still issues the two advance calls. *Verify:* identical metrics to
today, and the client no longer computes carbon intensity.

**Stage 2 — move the advance to the provider.**
The client stops calling `advance-slot` and instead calls
`POST /v1/advance-slot` **on the provider**, which fans out in the correct
order with retries. The swallowed-failure behaviour is gone at this point.
*Verify:* same results, but a stopped executor now produces a visible
`all_ok: false` instead of a silent drift.

**Stage 3 — let the forecast reach the solver.**
The real change: replace `BatchScheduler`'s `Arc<Vec<f64>>` forecast with a
shared, refreshable window (e.g. `Arc<RwLock<Vec<f64>>>`), and have
`POST /v1/admin/advance-slot` apply the pushed forecast to it. Then the solver
re-plans each batch against fresh data instead of a boot-time snapshot.
*Verify:* a deliberately inconsistent forecast (one the provider changes
between slots) changes the assignment, which today it cannot.

**Stage 4 — the remote role.**
Implement `RemoteCarbonIntensitySource.forecast()` against
`GET /intensity/{from}/fw24h` and resample if `slot_minutes != 30`. No
capability flag to consult: `observe()` already refuses any target other than
the slot being measured, so the remote adapter simply implements
`_observe_current()` against the live endpoint and inherits the correct
"measurement is an event" behaviour for free.

Stages 1–2 are pure plumbing and safe. **Stage 3 is an architectural change**
and should not be smuggled into a refactor.

---

## 9. Open questions still to settle

1. **Does the client need to be notified?** Yes — it is now peer `order: 10`
   with `role: PRODUCER`, because the client is what *submits* slot N's work
   and must do so before carbonshift is told to process slot N. It is not a
   consumer of the clock (it keeps no counter of its own); it is a producer of
   the slot's work, and the ordering is what makes the fan-out correct. See
   §3 for why the client must come first.
2. **Should a provider outage stop the simulation?** Today it returns
   `all_ok: false` and lets the caller decide. Failing the rollover outright is
   safer but more brittle.
3. **Who sets `slot_epoch_offset`?** It must be derived from the same epoch and
   slot length on both sides; making it a shared env var is the obvious move,
   but then the epoch is configuration in two places and can disagree.
4. **`slot_minutes != 30` with a remote source** needs a resampling policy
   (mean/max/point) — a domain question, not an implementation one.

---

## 10. A measurement is an event, not a property of a slot

This section records a **correction** made after review, because the original
model was subtly wrong in a way that would have quietly undermined the whole
emulation.

### What was wrong

The first version gave every `ForecastPoint` an optional `actual` field, and
exposed a capability flag:

```python
provides_actual_for_future_slots: bool
```

The intent was honest ("a remote source cannot know the future, so declare
it"). But the *shape* was wrong, and the flag papered over it: it implied that
"the actual of slot 118040" is a well-defined quantity that a source may or may
not be able to supply — as though the truth of a future slot were a secret
waiting to be read.

It is not. **A measurement is an event that happens at a moment.** A meter can
only ever tell you about the slot you are standing in; there is no fact of the
matter about the grid's intensity at 3pm tomorrow, so no amount of capability
can supply one. The synthetic adapter *could* compute a number, which is exactly
the problem: it would have let emulation exercise a code path that cannot occur
in production, and the two environments would diverge precisely where the
carbon-saving arithmetic lives.

### What it is now

| Concept | Type | Spans | Exists when |
|---|---|---|---|
| Prediction | `ForecastPoint { slot, forecast }` | many slots | always |
| Measurement | `ObservedPoint { slot, actual, observed_at_slot }` | exactly one slot | only after that slot is entered |

* `ForecastPoint` has **no** `actual` field, so a future actual is not
  representable in a forecast window at all.
* `observe(measuring_slot, target_slot=None)` returns a single reading or
  `None`. The port's default implementation **refuses** `target_slot !=
  measuring_slot` — so asking for a future measurement is an explicit,
  reviewable call rather than something that silently works when a synthetic
  adapter is behind the port.
* The flag is gone. There is nothing left to declare, because the question it
  answered no longer parses.
* Noise is keyed on the **measuring** slot (`observed_value` in `forecast.py`),
  not the target. Consequence: the same target slot has no stable "true" value
  — measure it from two different moments and you get two different numbers,
  exactly as with a real meter. This is what makes "the future's true value" a
  question with no answer, rather than one this model merely declines to expose.

### The win, and a trap it closes

Beyond being honest, this is *architecturally* better for the two-role design,
which is the part worth noting:

> A naive `actual_for(current_slot)`-style provider would force a **stateful
> emulation harness**: the harness would have to precompute and store the
> "true" series at t=0, so that the value it reports on entering slot N is the
> same one it would have reported had it been asked earlier. That is exactly
> the trap the real system must not fall into — and with the corrected
> contract, both roles behave identically without any such machinery.

Both adapters now share the same shape:

* **local**: the reading is `forecast(slot) + a fresh seeded deviation`, drawn
  at the moment of measurement.
* **remote**: the reading is the upstream's `intensity.actual`, which the GB
  API also only populates for *settled* buckets.

Neither can answer for the future. The service therefore **needs no state** to
be able to serve a reading when asked — there is no "truth" to cache — which is
what lets it stay restartable at any slot.

### Ordering became load-bearing

Given the above, the rollover must observe **after** the clock moves:

```
1. enter slot N          (clock advances)
2. take the reading for N (source.observe(N))
3. notify peers           ("you are in N, here is the reading, here is the forecast")
```

Measuring *before* advancing would report a reading for slot N-1, and every
correction the peers apply would be off by one slot — producing plausible
numbers and a very quiet bug. Pinned by
`tests/test_api.py::test_reading_describes_the_slot_entered_not_the_one_left`.

### Consequence for Stage 3 of the migration

This raises the value of the change described in §8: the scheduler currently
corrects committed costs after the fact. With a provider that reports one
reading per slot *at the moment it happens*, the natural evolution is for the
scheduler to absorb each reading as it arrives for the slot being entered —
rather than retroactively rescaling by a ratio whenever a callback happens to
land. That is a change to `executor_callback`'s correction logic, and it is the
point at which the provider stops being an observability adjunct and becomes
part of the control loop.
