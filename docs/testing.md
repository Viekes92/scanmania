# Testing

```bash
pytest tests/ -v
```

## What green actually means

The pure-logic core is well covered. Every I/O boundary is not.

| Area | Covered | Test file |
|---|---|---|
| `core/fsm.py` | yes — every transition, no mocks | `test_fsm.py` |
| `core/scoring.py` | yes | `test_scoring.py` |
| `core/stopwatch.py` | yes | `test_stopwatch.py` |
| `core/runner.py` | side-effect dispatch | `test_runner.py` |
| `iobackend/presets.py` + `reconcile.py` | desired-state drift | `test_reconcile.py` |
| `vision/detect.py` + `baseline.py` | yes — count rule, maze swap, suppression, baselines | `test_maze_dots.py` |
| `vision/service.py` | stall handling only | `test_vision_service.py` |
| `tools/capture.py` | dot finding, validation, the write | `test_capture.py` |
| audit regressions | run integrity, config clamps, day boundary, rate limit, name sanitisation | `test_audit_fixes.py` |
| `vision/camera.py`, `evidence.py`, `mjpeg.py` | **no tests** | — |
| `web/ratelimit.py` + name sanitisation | yes | `test_audit_fixes.py` |
| `web/` routes | **no tests** | — |
| `inputs/` | **no tests** | — |
| `iobackend/modbus.py` | **no tests** | — |
| `config/loader.py` | round-trip of the maze captures only | `test_capture.py` |
| `persist/db.py` | **no tests** | — |
| `__main__.py` | **no tests** | — |

The detection tests run against synthetic frames, not cameras. They prove the
decision logic — that a maze change is not a break, that a handful of dark dots
busts and dozens does not, that an uncalibrated preset watches nothing. They
prove nothing about whether a real camera can see a real dot. Only a capture
on-site does that.

Everything below the line above is still every place the system talks to
hardware or the network.

## The fakes are stubs, not drivers

`tests/conftest.py` builds `FakeInputs` and `FakeVision` and hands them to
`GameRunner`. No test calls `trigger_input()`, `trigger_break()` or
`trigger_clear()`, and no test runs a fake's `.run()` or `.events()` loop.
Every test pushes events straight into the runner instead.

So the fakes' own queue plumbing, arm/disarm gating and ratio thresholds are
constructed but never exercised. The `--fake-all` laptop workflow has no
automated coverage; `tools/fake_run.py` drives it manually.

Two consequences worth remembering:

- A bug in a fake backend will not be caught here.
- A fake that is **more forgiving than production** hides real bugs. This
  happened: `tools/fake_run.py` uses a `MemoryDB.upsert_run`, while production
  used `insert_run`. The GM void bug — a swallowed `IntegrityError` that made
  the VOID button silently inert — passed `--scenario voided` for exactly that
  reason, with 293 tests green.

When you add a fake, give it production's failure modes, not the happy path.

## Writing a test that can actually fail

Assert on the state the production code owns, not on a value the test body just
computed. `test_pause_stops_drain` used to re-implement the pause check inside
the test and then assert that a number equalled itself; deleting the production
guard left it green.

Two habits that prevent it:

1. **Call the real path.** Invoke the function under test, not a copy of its
   logic.
2. **Pick an assertion that discriminates.** Ask what the test would report if
   the guard were deleted. The clearest example was the outbox pause test
   (removed with cloud sync): queue depth was not a discriminator, because with
   no endpoint the push failed and the row stayed either way. The `attempts`
   counter was, because a paused worker must not even try.

Mutation-check anything security- or safety-relevant: delete the guard, confirm
the test goes red, restore it.

## Regression tests worth keeping

- `test_reconcile.py::test_all_off_is_not_undone_by_the_reconciler` — the runner
  turns coils off for the count-in dark windows; the reconciler must not re-light
  them. A direct `write_coils()` leaves `_desired` lit and fails this.
- `test_fsm.py::TestGmVoid::test_gm_void_emits_void_run_not_save_run` — a void
  must update the existing row, never re-insert it.
- `test_fsm.py::TestAttractShowIsStopped` — the attract show must stop on
  registration, or it fights the ready blink and the count-in ramp.
