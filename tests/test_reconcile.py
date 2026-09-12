"""
tests/test_reconcile.py — the invariant-4 safety backstop.

Inputs:  FakeIOBackend boards whose coils are poked behind the resolver's back
Outputs: assertions that ReconcileLoop notices the drift, re-asserts, counts it
         and emits relay.mismatch
Invariant: the reconciler must always read the *current* resolver — a config
           reload swaps it, and a captured reference would silently re-assert
           the pre-edit desired state forever.
"""

from __future__ import annotations

import pytest

from iobackend.presets import PresetResolver
from iobackend.reconcile import ReconcileLoop


@pytest.fixture
def wiring(fake_config, fake_io):
    """Resolver + reconciler over the fake backend, with metrics captured."""
    resolver = PresetResolver(fake_config.mazes, fake_config.hardware)
    emitted: list[tuple] = []
    holder = {"resolver": resolver}
    loop = ReconcileLoop(
        get_resolver=lambda: holder["resolver"],
        backend=fake_io,
        metrics_emit=lambda name, value=1.0, tags=None: emitted.append((name, value, tags)),
    )
    return loop, holder, emitted, resolver


@pytest.mark.asyncio
async def test_no_mismatch_when_board_matches_desired(wiring, fake_io):
    loop, _, emitted, resolver = wiring
    await resolver.apply_preset("all_on", fake_io)
    board = fake_io.all_boards()[0]
    writes_before = board.write_count

    await loop._check_board(board.board_id)

    assert board.write_count == writes_before, "reconciler wrote when nothing had drifted"
    assert emitted == []
    assert loop.mismatch_counts == {}


@pytest.mark.asyncio
async def test_drifted_coil_is_re_asserted_and_counted(wiring, fake_io):
    loop, _, emitted, resolver = wiring
    await resolver.apply_preset("all_on", fake_io)
    board = fake_io.all_boards()[0]

    # Simulate a relay dropping out on its own — no write went through presets.py.
    board.coils[3] = False

    await loop._check_board(board.board_id)

    assert board.coils == resolver.desired_state()[board.board_id]
    assert loop.mismatch_counts[board.board_id] == 1
    assert emitted and emitted[0][0] == "relay.mismatch"
    assert emitted[0][2] == {"board_id": board.board_id}


@pytest.mark.asyncio
async def test_reads_the_current_resolver_after_a_swap(wiring, fake_config, fake_io):
    """
    reload_config() builds a new PresetResolver. The reconciler must follow it,
    otherwise it fights the new config back to the old desired state.
    """
    loop, holder, _, old_resolver = wiring
    await old_resolver.apply_preset("all_on", fake_io)
    board = fake_io.all_boards()[0]

    new_resolver = PresetResolver(fake_config.mazes, fake_config.hardware)
    await new_resolver.apply_preset("blackout", fake_io)
    holder["resolver"] = new_resolver

    board.coils[0] = True  # drift away from blackout
    await loop._check_board(board.board_id)

    assert board.coils == [False] * 16, "reconciler re-asserted the stale resolver's state"


@pytest.mark.asyncio
async def test_all_off_is_not_undone_by_the_reconciler(wiring, fake_io):
    """
    The count-in dark windows and the ready blink turn every coil off.

    They must go through the resolver. A direct board.write_coils() leaves
    _desired lit, the reconciler sees drift, and it re-lights the maze inside
    the dark gap — up to three minutes in ARM, which has no ApplyPreset.
    """
    loop, _, _, resolver = wiring
    await resolver.apply_preset("all_on", fake_io)
    board = fake_io.all_boards()[0]

    await resolver.apply_all_off(fake_io)
    assert board.coils == [False] * 16

    await loop._check_board(board.board_id)

    assert board.coils == [False] * 16, "reconciler re-lit the maze during a dark window"
    assert loop.mismatch_counts == {}


@pytest.mark.asyncio
async def test_all_off_does_not_depend_on_the_blackout_preset(wiring, fake_io):
    """'blackout' is user-editable. The dark windows must not depend on it."""
    _, _, _, resolver = wiring
    resolver._mazes.presets.pop("blackout", None)

    await resolver.apply_preset("all_on", fake_io)
    await resolver.apply_all_off(fake_io)

    assert all(v == [False] * 16 for v in resolver.desired_state().values())


@pytest.mark.asyncio
async def test_missing_resolver_is_a_no_op(fake_io):
    """Before the runner has a resolver there is no desired state to enforce."""
    loop = ReconcileLoop(
        get_resolver=lambda: None,
        backend=fake_io,
        metrics_emit=lambda *a, **k: None,
    )
    board = fake_io.all_boards()[0]
    board.coils[0] = True
    await loop._check_board(board.board_id)
    assert board.coils[0] is True
    assert loop.mismatch_counts == {}
