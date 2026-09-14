"""
io/reconcile.py — periodically reads actual coil state and re-asserts if it differs.

Inputs:  desired state from PresetResolver, IOBackend to read/write coils
Outputs: log warning + metrics on mismatch; re-asserts coils by calling board.write_coils
Invariant: the re-assert write_coils call here is the ONE permitted exception to the
           "only presets.py calls write_coils" rule — reconcile.py is part of the io/
           layer and acts as a safety backstop, not a preset path. All preset-driven
           writes still go through PresetResolver. Each board check is independent so
           a slow or dead board never stalls others.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from iobackend.presets import IOBackend, PresetResolver

log = logging.getLogger(__name__)

_RECONCILE_INTERVAL_S = 0.5  # 500 ms


class ReconcileLoop:
    """
    Periodically reads actual coil state from each board and re-asserts if it
    differs from what PresetResolver believes the desired state to be.

    A separate asyncio task is spawned per board so one unresponsive board never
    delays checks on the others.
    """

    def __init__(
        self,
        get_resolver: Callable[[], PresetResolver | None],
        backend: IOBackend,
        metrics_emit: Callable,
    ) -> None:
        # A callable, not the resolver itself: GameRunner.reload_config() builds a
        # new PresetResolver, and a captured reference would leave us reconciling
        # against the pre-edit desired state forever.
        self._get_resolver = get_resolver
        self._backend = backend
        self._metrics_emit = metrics_emit
        # board_id → cumulative mismatch count, surfaced on the admin hardware page
        self.mismatch_counts: dict[str, int] = {}

    async def run(self) -> None:
        """
        Main loop: every 500 ms launch per-board checks concurrently and wait for
        all to finish before sleeping until the next cycle.
        """
        log.info("ReconcileLoop started (interval=%.0f ms)", _RECONCILE_INTERVAL_S * 1000)
        while True:
            start = time.monotonic()
            boards = self._backend.all_boards()
            await asyncio.gather(
                *(self._check_board(board.board_id) for board in boards),
                return_exceptions=True,
            )
            elapsed = time.monotonic() - start
            sleep_for = max(0.0, _RECONCILE_INTERVAL_S - elapsed)
            await asyncio.sleep(sleep_for)

    async def _check_board(self, board_id: str) -> None:
        """
        Read actual coil state from one board and compare to desired.
        Re-assert via write_coils (through PresetResolver's backend call) if different.
        """
        resolver = self._get_resolver()
        if resolver is None:
            return
        board = self._backend.get_board(board_id)
        desired = resolver.desired_state().get(board_id)
        if desired is None:
            return  # board not tracked by resolver (shouldn't happen)

        actual = await board.read_coils()
        if actual is None:
            # Board unreachable — ModbusBoard will handle DEGRADED tracking
            log.debug("reconcile: board '%s' read_coils returned None", board_id)
            return

        # Re-read desired AFTER the await. apply_preset sets _desired and then
        # writes the boards one at a time, so a read landing inside that window
        # compared a brand-new desired against a board that had not been written
        # yet — a guaranteed mismatch on every preset change, which meant a
        # warning and a redundant full-board write several times a second during
        # a show, and a permanently unhealthy-looking admin page. It also let
        # the reconciler revert a preset it had raced.
        still_desired = resolver.desired_state().get(board_id)
        if still_desired != desired:
            log.debug("reconcile: board '%s' desired changed mid-check — skipping",
                      board_id)
            return

        if actual != desired:
            diff = [
                f"ch{i+1}(want={desired[i]},got={actual[i]})"
                for i in range(len(desired))
                if i < len(actual) and actual[i] != desired[i]
            ]
            log.warning(
                "reconcile MISMATCH board='%s': %s — re-asserting",
                board_id, ", ".join(diff),
            )
            self.mismatch_counts[board_id] = self.mismatch_counts.get(board_id, 0) + 1
            self._metrics_emit(
                "relay.mismatch",
                value=len(diff),
                tags={"board_id": board_id},
            )
            ok = await board.write_coils(desired)
            if not ok:
                log.error(
                    "reconcile: re-assert write_coils failed on board '%s'", board_id
                )
