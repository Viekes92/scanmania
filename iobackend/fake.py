"""
io/fake.py — fake relay board backend for development without hardware.

Inputs:  HardwareConfig (to know which board ids exist)
Outputs: in-memory coil state per board; all writes logged at DEBUG level
Invariant: implements the same interface as ModbusBoard / the IOBackend protocol.
           MANDATORY for laptop development — the entire game must run with no
           real hardware attached when --fake-all is passed.
"""

from __future__ import annotations

import logging

from config.loader import HardwareConfig

log = logging.getLogger(__name__)


class FakeBoard:
    """
    Simulates a single 16-channel relay board entirely in memory.

    All write_coils and read_coils calls succeed instantly. The coil state is
    publicly readable so tests and fake_run.py can inspect it directly.
    """

    def __init__(self, board_id: str) -> None:
        self.board_id = board_id
        self.coils: list[bool] = [False] * 16
        self._status: str = "OK"
        self.write_count: int = 0

    async def connect(self) -> bool:
        log.debug("FakeBoard '%s': connect()", self.board_id)
        return True

    async def write_coils(self, coils: list[bool]) -> bool:
        if len(coils) != 16:
            log.error("FakeBoard '%s': write_coils got %d values, expected 16", self.board_id, len(coils))
            return False
        self.coils = list(coils)
        self.write_count += 1
        on_channels = [i + 1 for i, v in enumerate(coils) if v]
        log.debug(
            "FakeBoard '%s': write_coils #%d on=%s",
            self.board_id, self.write_count, on_channels,
        )
        return True

    async def read_coils(self) -> list[bool]:
        log.debug("FakeBoard '%s': read_coils → %s", self.board_id, self.coils)
        return list(self.coils)

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = value


class FakeIOBackend:
    """
    Collection of FakeBoards, one per board declared in HardwareConfig.

    Satisfies the IOBackend protocol consumed by PresetResolver and ReconcileLoop.
    Also exposes dump_state() for test assertions and fake_run.py inspection.
    """

    def __init__(self, hardware_config: HardwareConfig) -> None:
        self._boards: dict[str, FakeBoard] = {
            b.id: FakeBoard(b.id) for b in hardware_config.relay_boards
        }
        log.info(
            "FakeIOBackend initialised with boards: %s",
            list(self._boards.keys()),
        )

    def get_board(self, board_id: str) -> FakeBoard:
        if board_id not in self._boards:
            raise KeyError(f"FakeIOBackend: unknown board_id '{board_id}'")
        return self._boards[board_id]

    def all_boards(self) -> list[FakeBoard]:
        return list(self._boards.values())

    def dump_state(self) -> dict:
        """
        Return a serialisable snapshot of all board coil states.

        Format: {board_id: {"coils": [bool*16], "write_count": int, "status": str}}
        Used by tools/fake_run.py and tests.
        """
        return {
            bid: {
                "coils": list(board.coils),
                "on_channels": [i + 1 for i, v in enumerate(board.coils) if v],
                "write_count": board.write_count,
                "status": board.status,
            }
            for bid, board in self._boards.items()
        }
