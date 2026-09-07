"""
tests/test_runner.py — integration tests for core/runner.py GameRunner.

Exercises the async event loop wiring: boot sequence, player registration,
full clean-run dispatch, GM actions, timeout task lifecycle, master mode,
dev trigger injection, and the state message builder.
Invariant: no real hardware; uses fake_config, fake_io, fake_inputs, fake_vision,
           and in-memory SQLite from conftest.py fixtures.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from core.events import (
    # States
    BOOT, SELF_TEST, ATTRACT, REGISTERED, ARM, COUNTDOWN,
    RUN_SEG_1, RUN_SEG_2, RUN_SEG_3,
    FINISHED, BUSTED, ABORTED, RESULT, RESET, FAULT, MASTER,
    # Input events
    BootComplete, SelfTestPass, PlayerRegistered,
    PlateHigh, PlateLow, PreflightPass, PreflightFail,
    CountInRequested, RampComplete,
    Cp1Pressed, Cp2Pressed, StopPressed,
    BreakConfirmed, GmBust, GmAbort, GmForceReset, GmCancel, GmVoid,
    GmConfirmBreak, GmVetoBreak,
    MasterModeEngage, MasterModeExit,
    DetectionModeChanged, ArmTimeout, MaxRunExceeded, ResultDisplayTimeout,
    ProcessRestart,
)
from core.runner import GameRunner, MasterModeRequired


# ---------------------------------------------------------------------------
# Runner fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def runner(fake_config, fake_io, fake_inputs, fake_vision, db):
    """
    Fresh GameRunner wired to all fake backends and an in-memory DB.
    No hub is attached (WebSocket broadcast is a no-op).
    """
    r = GameRunner(
        config=fake_config,
        io_backend=fake_io,
        inputs_backend=fake_inputs,
        vision_backend=fake_vision,
        db=db,
    )
    yield r
    # Cancel any lingering timer tasks so the event loop is clean afterward.
    for attr in ("_arm_timeout_task", "_result_timeout_task",
                 "_max_run_task", "_count_in_task"):
        r._cancel_task(attr)


# ---------------------------------------------------------------------------
# Helper: drain the event queue for a bounded number of iterations
# ---------------------------------------------------------------------------

async def _drain(runner: GameRunner, iterations: int = 20, pause: float = 0.02) -> None:
    """
    Run the event drain loop for `iterations` cycles with `pause` seconds
    between each, giving enqueued side-effect events a chance to be processed.
    """
    for _ in range(iterations):
        while not runner._event_queue.empty():
            event = await runner._event_queue.get()
            await runner.dispatch(event)
            runner._event_queue.task_done()
        await asyncio.sleep(pause)


# ===========================================================================
# Boot sequence
# ===========================================================================

@pytest.mark.asyncio
async def test_boot_complete_transitions_to_self_test(runner):
    """BootComplete in BOOT → SELF_TEST."""
    assert runner.state == BOOT
    await runner.dispatch(BootComplete())
    assert runner.state == SELF_TEST


@pytest.mark.asyncio
async def test_self_test_pass_transitions_to_attract(runner):
    """SelfTestPass in SELF_TEST → ATTRACT."""
    await runner.dispatch(BootComplete())
    assert runner.state == SELF_TEST
    await runner.dispatch(SelfTestPass())
    assert runner.state == ATTRACT


@pytest.mark.asyncio
async def test_boot_sequence_ends_in_attract(runner):
    """Full boot sequence: BOOT → SELF_TEST → ATTRACT."""
    assert runner.state == BOOT
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    assert runner.state == ATTRACT


# ===========================================================================
# Player registration
# ===========================================================================

@pytest.mark.asyncio
async def test_player_registered_via_dispatch(runner):
    """PlayerRegistered event dispatched directly → REGISTERED."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    assert runner.state == ATTRACT

    await runner.dispatch(PlayerRegistered(player_id="p-001", nickname="Alice"))
    assert runner.state == REGISTERED
    assert runner.context.player_id == "p-001"
    assert runner.context.player_nickname == "Alice"


@pytest.mark.asyncio
async def test_on_player_registered_enqueues_event(runner):
    """on_player_registered() puts a PlayerRegistered on the queue."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())

    runner.on_player_registered("p-002", "Bob")
    assert runner._event_queue.qsize() == 1

    # Drain the queue so the event is actually processed.
    await _drain(runner, iterations=1, pause=0)
    assert runner.state == REGISTERED
    assert runner.context.player_nickname == "Bob"


@pytest.mark.asyncio
async def test_context_cleared_on_new_player(runner):
    """Registering a new player clears run_id and segment from previous run."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())

    # Simulate leftover context from a prior run.
    runner.context.run_id = "old-run-id"
    runner.context.segment = 3

    await runner.dispatch(PlayerRegistered(player_id="p-003", nickname="Carol"))
    assert runner.context.run_id is None
    assert runner.context.segment == 1


# ===========================================================================
# Full clean run (dispatching events directly, no background drain task)
# ===========================================================================

@pytest.mark.asyncio
async def test_full_clean_run_direct_dispatch(runner):
    """
    Drive a complete clean run by dispatching every event manually.
    Verifies state at each step and that the run is saved to DB.
    """
    # Boot
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    assert runner.state == ATTRACT

    # Register
    await runner.dispatch(PlayerRegistered(player_id="p-run", nickname="Runner"))
    assert runner.state == REGISTERED

    # Step on start plate
    await runner.dispatch(PlateHigh())
    assert runner.state == ARM

    # Preflight auto-injected (BeamPreflightCheck side effect puts PreflightPass on queue)
    await _drain(runner, iterations=5, pause=0)
    assert runner.state == ARM  # ARM stays ARM after PreflightPass

    # GM triggers count-in
    await runner.dispatch(CountInRequested())
    assert runner.state == COUNTDOWN

    # Ramp completes (would normally come from the count-in task after sleeping)
    await runner.dispatch(RampComplete())
    assert runner.state == RUN_SEG_1

    # Checkpoint 1
    await runner.dispatch(Cp1Pressed())
    assert runner.state == RUN_SEG_2

    # Checkpoint 2
    await runner.dispatch(Cp2Pressed())
    assert runner.state == RUN_SEG_3

    # Stop button
    run_id_before_stop = runner.context.run_id
    await runner.dispatch(StopPressed())
    assert runner.state == FINISHED

    # Verify run_id was assigned
    assert run_id_before_stop is not None


# We need `db` accessible inside the test; redefine with explicit dependency:
@pytest.mark.asyncio
async def test_full_clean_run_saves_to_db(fake_config, fake_io, fake_inputs, fake_vision, db):
    """Run saved with correct fields after a clean run."""
    r = GameRunner(
        config=fake_config,
        io_backend=fake_io,
        inputs_backend=fake_inputs,
        vision_backend=fake_vision,
        db=db,
    )
    # Insert player into DB first (web routes do this; we simulate it here)
    await db.upsert_player("p-save", "Saver")

    await r.dispatch(BootComplete())
    await r.dispatch(SelfTestPass())
    await r.dispatch(PlayerRegistered(player_id="p-save", nickname="Saver"))
    await r.dispatch(PlateHigh())
    await _drain(r, iterations=5, pause=0)
    await r.dispatch(CountInRequested())
    await r.dispatch(RampComplete())
    await r.dispatch(Cp1Pressed())
    await r.dispatch(Cp2Pressed())
    await r.dispatch(StopPressed())

    run_id = r.context.run_id  # still set until RESET
    # It is possible run_id is still set (FINISHED state hasn't reset yet).
    # Find run by scanning — run_id assigned at RampComplete/StartStopwatch.
    saved = await db.get_run(run_id)
    assert saved is not None
    assert saved["outcome"] == "clean"
    assert saved["player_id"] == "p-save"
    assert saved["detection_mode"] == "auto"

    for attr in ("_arm_timeout_task", "_result_timeout_task",
                 "_max_run_task", "_count_in_task"):
        r._cancel_task(attr)


# ===========================================================================
# RESET → ATTRACT transition
# ===========================================================================

@pytest.mark.asyncio
async def test_gm_force_reset_triggers_attract(runner):
    """GmForceReset from any state → RESET → auto-transition to ATTRACT."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-r", nickname="R"))
    assert runner.state == REGISTERED

    await runner.dispatch(GmForceReset())
    # RESET is transient; _do_reset_to_attract runs immediately inside dispatch.
    assert runner.state == ATTRACT


@pytest.mark.asyncio
async def test_reset_clears_context(runner):
    """After RESET → ATTRACT, run context fields are cleared."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-ctx", nickname="Ctx"))
    runner.context.run_id = "some-run-id"
    runner.context.segment = 2

    await runner.dispatch(GmForceReset())
    assert runner.state == ATTRACT
    assert runner.context.run_id is None
    assert runner.context.player_id is None
    assert runner.context.player_nickname is None
    assert runner.context.segment == 1
    assert runner.context.pending_break is None


# ===========================================================================
# GM actions via on_gm_action()
# ===========================================================================

@pytest.mark.asyncio
async def test_gm_action_count_in_enqueues_event(runner):
    """on_gm_action('count_in') puts CountInRequested on the queue."""
    runner.on_gm_action("count_in", {})
    assert runner._event_queue.qsize() == 1
    event = await runner._event_queue.get()
    assert isinstance(event, CountInRequested)


@pytest.mark.asyncio
async def test_gm_action_force_reset_enqueues_event(runner):
    """on_gm_action('force_reset') puts GmForceReset on the queue."""
    runner.on_gm_action("force_reset", {})
    assert runner._event_queue.qsize() == 1
    event = await runner._event_queue.get()
    assert isinstance(event, GmForceReset)


@pytest.mark.asyncio
async def test_gm_action_detection_mode_enqueues_event(runner):
    """on_gm_action('detection_mode') puts DetectionModeChanged with the right mode."""
    runner.on_gm_action("detection_mode", {"mode": "manual"})
    assert runner._event_queue.qsize() == 1
    event = await runner._event_queue.get()
    assert isinstance(event, DetectionModeChanged)
    assert event.mode == "manual"


@pytest.mark.asyncio
async def test_gm_action_abort_enqueues_gm_abort(runner):
    """on_gm_action('abort') puts GmAbort on the queue."""
    runner.on_gm_action("abort", {})
    assert runner._event_queue.qsize() == 1
    event = await runner._event_queue.get()
    assert isinstance(event, GmAbort)


@pytest.mark.asyncio
async def test_gm_action_void_enqueues_gm_void_with_reason(runner):
    """on_gm_action('void') passes the reason field through."""
    runner.on_gm_action("void", {"reason": "cheated"})
    event = await runner._event_queue.get()
    assert isinstance(event, GmVoid)
    assert event.reason == "cheated"


@pytest.mark.asyncio
async def test_gm_action_bust_enqueues_gm_bust(runner):
    """on_gm_action('bust') puts GmBust on the queue."""
    runner.on_gm_action("bust", {})
    event = await runner._event_queue.get()
    assert isinstance(event, GmBust)


@pytest.mark.asyncio
async def test_gm_action_cancel_enqueues_gm_cancel(runner):
    """on_gm_action('cancel') puts GmCancel on the queue."""
    runner.on_gm_action("cancel", {})
    event = await runner._event_queue.get()
    assert isinstance(event, GmCancel)


@pytest.mark.asyncio
async def test_gm_action_confirm_break_enqueues_event(runner):
    """on_gm_action('confirm_break') puts GmConfirmBreak on the queue."""
    runner.on_gm_action("confirm_break", {})
    event = await runner._event_queue.get()
    assert isinstance(event, GmConfirmBreak)


@pytest.mark.asyncio
async def test_gm_action_veto_break_enqueues_event(runner):
    """on_gm_action('veto_break') puts GmVetoBreak on the queue."""
    runner.on_gm_action("veto_break", {})
    event = await runner._event_queue.get()
    assert isinstance(event, GmVetoBreak)


@pytest.mark.asyncio
async def test_gm_action_master_mode_engage(runner):
    """on_gm_action('master_mode', engage=True) puts MasterModeEngage on the queue."""
    runner.on_gm_action("master_mode", {"engage": True})
    event = await runner._event_queue.get()
    assert isinstance(event, MasterModeEngage)


@pytest.mark.asyncio
async def test_gm_action_master_mode_exit(runner):
    """on_gm_action('master_mode', engage=False) puts MasterModeExit on the queue."""
    runner.on_gm_action("master_mode", {"engage": False})
    event = await runner._event_queue.get()
    assert isinstance(event, MasterModeExit)


@pytest.mark.asyncio
async def test_gm_action_unknown_does_not_raise(runner):
    """An unknown GM action is silently ignored (logged but no exception)."""
    runner.on_gm_action("totally_unknown_action", {})
    assert runner._event_queue.empty()


# ===========================================================================
# Timeout task lifecycle
# ===========================================================================

@pytest.mark.asyncio
async def test_arm_timeout_task_created_on_arm_detection(runner):
    """
    ArmDetection side effect (triggered by RampComplete → RUN_SEG_1) creates
    the _arm_timeout_task.
    """
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-t", nickname="T"))
    await runner.dispatch(PlateHigh())
    await _drain(runner, iterations=5, pause=0)
    await runner.dispatch(CountInRequested())
    await runner.dispatch(RampComplete())
    # RampComplete → RUN_SEG_1 executes ArmDetection side effect.
    assert runner._arm_timeout_task is not None
    assert not runner._arm_timeout_task.done()
    runner._cancel_task("_arm_timeout_task")


@pytest.mark.asyncio
async def test_max_run_task_created_on_start_stopwatch(runner):
    """
    StartStopwatch side effect (triggered by RampComplete) creates
    the _max_run_task.
    """
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-max", nickname="Max"))
    await runner.dispatch(PlateHigh())
    await _drain(runner, iterations=5, pause=0)
    await runner.dispatch(CountInRequested())
    await runner.dispatch(RampComplete())
    assert runner._max_run_task is not None
    assert not runner._max_run_task.done()
    runner._cancel_task("_max_run_task")
    runner._cancel_task("_arm_timeout_task")


@pytest.mark.asyncio
async def test_result_timeout_task_created_on_stop_stopwatch(runner):
    """
    StopStopwatch side effect (triggered by StopPressed) creates
    the _result_timeout_task.
    """
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-res", nickname="Res"))
    await runner.dispatch(PlateHigh())
    await _drain(runner, iterations=5, pause=0)
    await runner.dispatch(CountInRequested())
    await runner.dispatch(RampComplete())
    await runner.dispatch(Cp1Pressed())
    await runner.dispatch(Cp2Pressed())
    await runner.dispatch(StopPressed())
    # StopStopwatch starts the result display timer.
    assert runner._result_timeout_task is not None
    assert not runner._result_timeout_task.done()
    runner._cancel_task("_result_timeout_task")


# ===========================================================================
# Timer cancellation
# ===========================================================================

@pytest.mark.asyncio
async def test_cancel_task_cancels_running_task(runner):
    """_cancel_task cancels a running asyncio.Task and sets the attr to None."""
    async def _long_sleep():
        await asyncio.sleep(9999)

    task = asyncio.create_task(_long_sleep())
    runner._arm_timeout_task = task
    assert not task.done()

    runner._cancel_task("_arm_timeout_task")
    await asyncio.sleep(0)  # let cancellation propagate

    assert runner._arm_timeout_task is None
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_cancel_task_on_none_does_not_raise(runner):
    """_cancel_task on an attr that is None is a safe no-op."""
    runner._arm_timeout_task = None
    runner._cancel_task("_arm_timeout_task")  # must not raise
    assert runner._arm_timeout_task is None


@pytest.mark.asyncio
async def test_do_reset_to_attract_cancels_all_timers(runner):
    """_do_reset_to_attract cancels all four timer tasks."""
    async def _long():
        await asyncio.sleep(9999)

    runner._arm_timeout_task    = asyncio.create_task(_long())
    runner._result_timeout_task = asyncio.create_task(_long())
    runner._max_run_task        = asyncio.create_task(_long())
    runner._count_in_task       = asyncio.create_task(_long())

    runner.state = RESET  # pre-condition for _do_reset_to_attract
    await runner._do_reset_to_attract()

    assert runner._arm_timeout_task is None
    assert runner._result_timeout_task is None
    assert runner._max_run_task is None
    assert runner._count_in_task is None
    assert runner.state == ATTRACT


# ===========================================================================
# Master mode via on_admin_action()
# ===========================================================================

@pytest.mark.asyncio
async def test_admin_action_master_engage_enqueues_event(runner):
    """on_admin_action('master_engage') puts MasterModeEngage on the queue."""
    runner.on_admin_action("master_engage", {})
    assert runner._event_queue.qsize() == 1
    event = await runner._event_queue.get()
    assert isinstance(event, MasterModeEngage)


@pytest.mark.asyncio
async def test_admin_action_master_exit_enqueues_event(runner):
    """on_admin_action('master_exit') puts MasterModeExit on the queue."""
    runner.on_admin_action("master_exit", {})
    event = await runner._event_queue.get()
    assert isinstance(event, MasterModeExit)


@pytest.mark.asyncio
async def test_master_stopwatch_start(runner):
    """_master_stopwatch('start') starts the stopwatch while in MASTER."""
    runner.state = "MASTER"
    runner._master_stopwatch("start")
    assert runner.stopwatch.is_running


@pytest.mark.asyncio
async def test_master_stopwatch_stop(runner):
    """_master_stopwatch('stop') stops the stopwatch while in MASTER."""
    runner.state = "MASTER"
    runner.stopwatch.start()
    runner._master_stopwatch("stop")
    assert not runner.stopwatch.is_running


@pytest.mark.asyncio
async def test_master_stopwatch_reset(runner):
    """_master_stopwatch('reset') resets elapsed to zero while in MASTER."""
    runner.state = "MASTER"
    runner.stopwatch.start()
    await asyncio.sleep(0.01)
    runner.stopwatch.stop()
    runner._master_stopwatch("reset")
    assert runner.stopwatch.elapsed_ms() == 0


@pytest.mark.asyncio
async def test_master_stopwatch_rejected_outside_master(runner):
    """Invariant 2: the stopwatch cannot be driven from admin outside MASTER."""
    runner.stopwatch.start()
    with pytest.raises(MasterModeRequired):
        runner._master_stopwatch("reset")
    assert runner.stopwatch.is_running


@pytest.mark.asyncio
async def test_master_stopwatch_unknown_action_raises(runner):
    """An unknown stopwatch action is a 422-worthy ValueError, not a silent no-op."""
    runner.state = "MASTER"
    with pytest.raises(ValueError):
        runner._master_stopwatch("rewind")


@pytest.mark.asyncio
async def test_master_apply_preset_rejected_when_not_master(runner):
    """_master_apply_preset raises outside MASTER instead of touching coils."""
    # Runner starts in BOOT; not MASTER.
    with pytest.raises(MasterModeRequired):
        await runner._master_apply_preset("attract")
    assert runner.state == BOOT


@pytest.mark.asyncio
async def test_admin_action_unknown_does_not_raise(runner):
    """An unknown admin action is silently ignored."""
    runner.on_admin_action("completely_unknown", {})
    assert runner._event_queue.empty()


# ===========================================================================
# Dev trigger via on_admin_action('dev_trigger', ...)
# ===========================================================================

@pytest.mark.asyncio
async def test_dev_trigger_plate_high(runner):
    """dev_trigger PlateHigh puts PlateHigh on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "PlateHigh"})
    event = await runner._event_queue.get()
    assert isinstance(event, PlateHigh)


@pytest.mark.asyncio
async def test_dev_trigger_plate_low(runner):
    """dev_trigger PlateLow puts PlateLow on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "PlateLow"})
    event = await runner._event_queue.get()
    assert isinstance(event, PlateLow)


@pytest.mark.asyncio
async def test_dev_trigger_count_in_requested(runner):
    """dev_trigger CountInRequested puts CountInRequested on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "CountInRequested"})
    event = await runner._event_queue.get()
    assert isinstance(event, CountInRequested)


@pytest.mark.asyncio
async def test_dev_trigger_ramp_complete(runner):
    """dev_trigger RampComplete puts RampComplete on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "RampComplete"})
    event = await runner._event_queue.get()
    assert isinstance(event, RampComplete)


@pytest.mark.asyncio
async def test_dev_trigger_cp1_pressed(runner):
    """dev_trigger Cp1Pressed puts Cp1Pressed on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "Cp1Pressed"})
    event = await runner._event_queue.get()
    assert isinstance(event, Cp1Pressed)


@pytest.mark.asyncio
async def test_dev_trigger_cp2_pressed(runner):
    """dev_trigger Cp2Pressed puts Cp2Pressed on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "Cp2Pressed"})
    event = await runner._event_queue.get()
    assert isinstance(event, Cp2Pressed)


@pytest.mark.asyncio
async def test_dev_trigger_stop_pressed(runner):
    """dev_trigger StopPressed puts StopPressed on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "StopPressed"})
    event = await runner._event_queue.get()
    assert isinstance(event, StopPressed)


@pytest.mark.asyncio
async def test_dev_trigger_break_confirmed(runner):
    """dev_trigger BreakConfirmed builds the event with beam_id and ratio."""
    runner.on_admin_action("dev_trigger", {"event": "BreakConfirmed", "beam_id": "b01", "ratio": "0.15"})
    event = await runner._event_queue.get()
    assert isinstance(event, BreakConfirmed)
    assert event.beam_id == "b01"
    assert abs(event.ratio - 0.15) < 1e-9


@pytest.mark.asyncio
async def test_dev_trigger_gm_bust(runner):
    """dev_trigger GmBust puts GmBust on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "GmBust"})
    event = await runner._event_queue.get()
    assert isinstance(event, GmBust)


@pytest.mark.asyncio
async def test_dev_trigger_gm_abort(runner):
    """dev_trigger GmAbort puts GmAbort on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "GmAbort"})
    event = await runner._event_queue.get()
    assert isinstance(event, GmAbort)


@pytest.mark.asyncio
async def test_dev_trigger_gm_force_reset(runner):
    """dev_trigger GmForceReset puts GmForceReset on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "GmForceReset"})
    event = await runner._event_queue.get()
    assert isinstance(event, GmForceReset)


@pytest.mark.asyncio
async def test_dev_trigger_player_registered(runner):
    """dev_trigger PlayerRegistered builds the event with player_id and nickname."""
    runner.on_admin_action("dev_trigger", {
        "event": "PlayerRegistered",
        "player_id": "dev-p",
        "nickname": "Dev",
    })
    event = await runner._event_queue.get()
    assert isinstance(event, PlayerRegistered)
    assert event.player_id == "dev-p"
    assert event.nickname == "Dev"


@pytest.mark.asyncio
async def test_dev_trigger_master_mode_engage(runner):
    """dev_trigger MasterModeEngage puts MasterModeEngage on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "MasterModeEngage"})
    event = await runner._event_queue.get()
    assert isinstance(event, MasterModeEngage)


@pytest.mark.asyncio
async def test_dev_trigger_master_mode_exit(runner):
    """dev_trigger MasterModeExit puts MasterModeExit on the queue."""
    runner.on_admin_action("dev_trigger", {"event": "MasterModeExit"})
    event = await runner._event_queue.get()
    assert isinstance(event, MasterModeExit)


@pytest.mark.asyncio
async def test_dev_trigger_unknown_event_does_not_raise(runner):
    """An unknown dev_trigger event name is silently logged and dropped."""
    runner.on_admin_action("dev_trigger", {"event": "NoSuchEvent"})
    assert runner._event_queue.empty()


# ===========================================================================
# State message builder
# ===========================================================================

@pytest.mark.asyncio
async def test_get_state_message_returns_required_keys(runner):
    """_get_state_message returns a dict with all keys the frontends rely on."""
    msg = runner._get_state_message()
    required_keys = {
        "state",
        "detection_mode",
        "run_id",
        "player_nickname",
        "elapsed_ms",
        "started_at_mono_ns",
        "server_mono_now_ns",
        "segment",
        "beams_masked",
        "pending_break",
        "timestamp",
    }
    assert required_keys.issubset(msg.keys())


@pytest.mark.asyncio
async def test_get_state_message_reflects_current_state(runner):
    """_get_state_message.state matches runner.state."""
    await runner.dispatch(BootComplete())
    msg = runner._get_state_message()
    assert msg["state"] == SELF_TEST


@pytest.mark.asyncio
async def test_get_state_message_reflects_detection_mode(runner):
    """_get_state_message.detection_mode matches context.detection_mode."""
    runner.context.detection_mode = "manual"
    msg = runner._get_state_message()
    assert msg["detection_mode"] == "manual"


@pytest.mark.asyncio
async def test_get_state_message_beams_masked_is_list(runner):
    """beams_masked is serialised as a list (not a set), for JSON compatibility."""
    runner.context.beams_masked = {"b01", "b02"}
    msg = runner._get_state_message()
    assert isinstance(msg["beams_masked"], list)
    assert set(msg["beams_masked"]) == {"b01", "b02"}


@pytest.mark.asyncio
async def test_get_state_message_player_nickname(runner):
    """player_nickname in the message matches context.player_nickname."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-msg", nickname="MsgTest"))
    msg = runner._get_state_message()
    assert msg["player_nickname"] == "MsgTest"


@pytest.mark.asyncio
async def test_get_state_message_timestamp_is_numeric(runner):
    """timestamp is a float (wall-clock seconds, for JS)."""
    msg = runner._get_state_message()
    assert isinstance(msg["timestamp"], float)
    assert msg["timestamp"] > 0


# ===========================================================================
# Detection mode changes via GM action
# ===========================================================================

@pytest.mark.asyncio
async def test_detection_mode_change_updates_context(runner):
    """DetectionModeChanged event updates context.detection_mode."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    assert runner.context.detection_mode == "auto"

    await runner.dispatch(DetectionModeChanged(mode="manual"))
    assert runner.context.detection_mode == "manual"


@pytest.mark.asyncio
async def test_gm_action_detection_mode_flows_through_queue(runner):
    """on_gm_action('detection_mode') → queue → dispatch updates detection mode."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())

    runner.on_gm_action("detection_mode", {"mode": "assisted"})
    await _drain(runner, iterations=3, pause=0)

    assert runner.context.detection_mode == "assisted"


# ===========================================================================
# Beam break during a run
# ===========================================================================

@pytest.mark.asyncio
async def test_beam_break_in_auto_mode_busts_player(runner):
    """BreakConfirmed in auto mode during RUN_SEG_1 → BUSTED."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-brk", nickname="Breaker"))
    await runner.dispatch(PlateHigh())
    await _drain(runner, iterations=5, pause=0)
    await runner.dispatch(CountInRequested())
    await runner.dispatch(RampComplete())
    assert runner.state == RUN_SEG_1

    run_id = runner.context.run_id
    await runner.dispatch(BreakConfirmed(beam_id="b01", ratio=0.1, run_id=run_id))
    assert runner.state == BUSTED
    runner._cancel_task("_result_timeout_task")


@pytest.mark.asyncio
async def test_gm_abort_during_run_transitions_to_aborted(runner):
    """GmAbort during RUN_SEG_1 → ABORTED."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-ab", nickname="Aborter"))
    await runner.dispatch(PlateHigh())
    await _drain(runner, iterations=5, pause=0)
    await runner.dispatch(CountInRequested())
    await runner.dispatch(RampComplete())
    assert runner.state == RUN_SEG_1

    await runner.dispatch(GmAbort())
    assert runner.state == ABORTED
    runner._cancel_task("_result_timeout_task")


# ===========================================================================
# put_event (thread-safe submission)
# ===========================================================================

@pytest.mark.asyncio
async def test_put_event_places_event_on_queue(runner):
    """put_event() adds the event to the internal queue."""
    assert runner._event_queue.empty()
    await runner.put_event(BootComplete())
    assert runner._event_queue.qsize() == 1


@pytest.mark.asyncio
async def test_put_event_then_drain_dispatches_event(runner):
    """An event placed via put_event() is dispatched when queue is drained."""
    await runner.put_event(BootComplete())
    await _drain(runner, iterations=2, pause=0)
    assert runner.state == SELF_TEST


# ===========================================================================
# Preflight path
# ===========================================================================

@pytest.mark.asyncio
async def test_preflight_pass_keeps_arm_state(runner):
    """PreflightPass in ARM stays in ARM (enables count-in button on GM console)."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-pf", nickname="PF"))
    await runner.dispatch(PlateHigh())
    assert runner.state == ARM

    await runner.dispatch(PreflightPass())
    assert runner.state == ARM


@pytest.mark.asyncio
async def test_plate_high_enqueues_preflight_pass_via_side_effect(runner):
    """
    PlateHigh → ARM executes BeamPreflightCheck side effect, which puts
    PreflightPass on the queue. After draining, runner is still in ARM with
    preflight completed.
    """
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-pfq", nickname="PFQ"))
    await runner.dispatch(PlateHigh())
    # BeamPreflightCheck side effect has been executed — PreflightPass is on queue.
    assert runner._event_queue.qsize() >= 1

    await _drain(runner, iterations=5, pause=0)
    assert runner.state == ARM  # still ARM after PreflightPass


# ===========================================================================
# ARM timeout
# ===========================================================================

@pytest.mark.asyncio
async def test_arm_timeout_task_created_after_plate_high(runner):
    """
    PlateHigh → ARM executes ArmDetection... wait, ARM entry does not arm detection.
    But RampComplete does. However, the ARM state itself spawns the arm timeout.
    Verify that after PlateHigh the _arm_timeout_task is created.
    """
    # Note: _handle_arm_detection is triggered by ArmDetection side effect from
    # RampComplete, NOT PlateHigh. PlateHigh triggers ReadyBlink + BeamPreflightCheck.
    # The arm_timeout is created inside _handle_arm_detection.
    # So after PlateHigh we should NOT have an arm_timeout_task yet.
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-arm", nickname="Arm"))
    await runner.dispatch(PlateHigh())
    # ARM entry now starts the arm inactivity timeout
    assert runner._arm_timeout_task is not None


@pytest.mark.asyncio
async def test_arm_timeout_event_resets_to_attract(runner):
    """ArmTimeout in ARM state → RESET → ATTRACT."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-ato", nickname="ATO"))
    await runner.dispatch(PlateHigh())
    await _drain(runner, iterations=5, pause=0)
    assert runner.state == ARM

    await runner.dispatch(ArmTimeout())
    # RESET is transient → ATTRACT
    assert runner.state == ATTRACT


# ===========================================================================
# Master mode full flow
# ===========================================================================

@pytest.mark.asyncio
async def test_master_mode_engage_and_exit(runner):
    """MasterModeEngage → MASTER; MasterModeExit → SELF_TEST."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    assert runner.state == ATTRACT

    await runner.dispatch(MasterModeEngage())
    assert runner.state == MASTER

    await runner.dispatch(MasterModeExit())
    assert runner.state == SELF_TEST


@pytest.mark.asyncio
async def test_master_mode_engage_from_any_state(runner):
    """MasterModeEngage works from REGISTERED state as well."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-m", nickname="M"))
    assert runner.state == REGISTERED

    await runner.dispatch(MasterModeEngage())
    assert runner.state == MASTER


# ===========================================================================
# set_hub
# ===========================================================================

@pytest.mark.asyncio
async def test_set_hub_attaches_hub(runner):
    """set_hub() stores the hub so BroadcastState calls it."""
    class _FakeHub:
        def __init__(self):
            self.messages = []
        async def broadcast(self, msg):
            self.messages.append(msg)

    hub = _FakeHub()
    runner.set_hub(hub)
    assert runner._hub is hub

    # Trigger a BroadcastState — dispatching BootComplete produces one.
    await runner.dispatch(BootComplete())
    assert len(hub.messages) >= 1
    assert hub.messages[-1]["state"] == SELF_TEST


# ===========================================================================
# Stopwatch integration
# ===========================================================================

@pytest.mark.asyncio
async def test_stopwatch_starts_on_ramp_complete(runner):
    """Stopwatch is running after RampComplete."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-sw", nickname="SW"))
    await runner.dispatch(PlateHigh())
    await _drain(runner, iterations=5, pause=0)
    await runner.dispatch(CountInRequested())
    await runner.dispatch(RampComplete())

    assert runner.stopwatch.is_running
    runner._cancel_task("_arm_timeout_task")
    runner._cancel_task("_max_run_task")


@pytest.mark.asyncio
async def test_stopwatch_stops_on_stop_pressed(runner):
    """Stopwatch is frozen after StopPressed."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-sw2", nickname="SW2"))
    await runner.dispatch(PlateHigh())
    await _drain(runner, iterations=5, pause=0)
    await runner.dispatch(CountInRequested())
    await runner.dispatch(RampComplete())
    await runner.dispatch(Cp1Pressed())
    await runner.dispatch(Cp2Pressed())
    await runner.dispatch(StopPressed())

    assert not runner.stopwatch.is_running
    runner._cancel_task("_result_timeout_task")


@pytest.mark.asyncio
async def test_run_id_assigned_after_start_stopwatch(runner):
    """runner.context.run_id is set once the stopwatch starts (RampComplete)."""
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await runner.dispatch(PlayerRegistered(player_id="p-rid", nickname="RID"))
    await runner.dispatch(PlateHigh())
    await _drain(runner, iterations=5, pause=0)
    await runner.dispatch(CountInRequested())

    assert runner.context.run_id is None  # not yet assigned before RampComplete

    await runner.dispatch(RampComplete())
    assert runner.context.run_id is not None
    runner._cancel_task("_arm_timeout_task")
    runner._cancel_task("_max_run_task")


# ===========================================================================
# Config reload
# ===========================================================================

@pytest.mark.asyncio
async def test_reload_config_rearms_running_show(runner, fake_config):
    """
    A show holds a reference to its own step list, so swapping the config alone
    would leave it playing the pre-edit sequence. reload_config() must restart it.
    """
    await runner.dispatch(BootComplete())
    await runner.dispatch(SelfTestPass())
    await _drain(runner, iterations=3, pause=0)

    assert runner._show_name == "attract"
    old_task = runner._show_task
    assert old_task is not None and not old_task.done()

    from config.loader import ShowStep
    fake_config.mazes.shows["attract"].steps = [ShowStep("blackout", 50)]
    await runner.reload_config(fake_config)

    assert runner._show_task is not old_task, "show task was not re-armed"
    assert runner._show_name == "attract"
    runner._cancel_task("_show_task")


@pytest.mark.asyncio
async def test_reload_config_does_not_start_a_show_when_none_playing(runner, fake_config):
    """Reloading in BOOT must not spontaneously light the maze."""
    assert runner._show_name is None
    await runner.reload_config(fake_config)
    assert runner._show_task is None
