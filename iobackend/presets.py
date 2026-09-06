"""
io/presets.py — resolves preset names to per-board coil arrays and applies them.

Inputs:  preset name (str) or direct channel toggle, MazesConfig, HardwareConfig
Outputs: calls backend.write_coils(board_id, coil_array) — one call per board per apply
Invariant: this is the ONLY module that may call write_coils. All coil writes go through
           apply_preset() or apply_direct(). No other module may touch relay state.
"""

from __future__ import annotations

import logging
from typing import Protocol

from config.loader import HardwareConfig, MazesConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend protocol — both ModbusBoard and FakeBoard satisfy this
# ---------------------------------------------------------------------------

class BoardBackend(Protocol):
    async def write_coils(self, coils: list[bool]) -> bool: ...
    async def read_coils(self) -> list[bool] | None: ...
    @property
    def status(self) -> str: ...


class IOBackend(Protocol):
    """Collection of boards — FakeIOBackend and the real Modbus backend both satisfy this."""
    def get_board(self, board_id: str) -> BoardBackend: ...
    def all_boards(self) -> list[BoardBackend]: ...


# ---------------------------------------------------------------------------
# PresetResolver
# ---------------------------------------------------------------------------

class PresetResolver:
    """
    Resolves preset names to per-board coil arrays and tracks desired coil state.

    Channel numbering is 1-indexed global: channels 1-16 go to board[0], 17-32 to board[1],
    and so on, matching the comment in mazes.yaml. Boards are ordered as they appear in
    hardware_config.relay_boards.
    """

    def __init__(self, mazes_config: MazesConfig, hardware_config: HardwareConfig) -> None:
        self._mazes = mazes_config
        # Ordered list of board ids, determines channel→board mapping
        self._board_ids: list[str] = [b.id for b in hardware_config.relay_boards]
        self._channels_per_board: int = 16  # always 16 for Waveshare boards

        # Desired state: board_id → [bool]*16; initialised to all-off
        self._desired: dict[str, list[bool]] = {
            board_id: [False] * 16 for board_id in self._board_ids
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _channel_to_board(self, channel: int) -> tuple[str, int]:
        """
        Map a 1-indexed global channel number to (board_id, 0-indexed local channel).
        Raises ValueError if channel is out of range.
        """
        if channel < 1:
            raise ValueError(f"Channel must be >= 1, got {channel}")
        zero = channel - 1
        board_index = zero // self._channels_per_board
        local_index = zero % self._channels_per_board
        if board_index >= len(self._board_ids):
            raise ValueError(
                f"Channel {channel} exceeds known boards "
                f"({len(self._board_ids)} boards × {self._channels_per_board} channels)"
            )
        return self._board_ids[board_index], local_index

    def _empty_board_state(self) -> dict[str, list[bool]]:
        return {bid: [False] * 16 for bid in self._board_ids}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, preset_name: str) -> dict[str, list[bool]]:
        """
        Resolve a preset name to a full board state dict.

        Returns {board_id: [bool]*16} for every known board.
        '*' means all channels True on all boards.
        Unknown presets raise KeyError.
        """
        preset = self._mazes.presets.get(preset_name)
        if preset is None:
            raise KeyError(f"Unknown preset: '{preset_name}'")

        state = self._empty_board_state()

        channels = preset.channels
        if channels == "*":
            # All channels on all boards
            for bid in self._board_ids:
                state[bid] = [True] * 16
        else:
            for ch in channels:
                try:
                    board_id, local_idx = self._channel_to_board(ch)
                    state[board_id][local_idx] = True
                except ValueError as exc:
                    log.warning("Preset '%s': %s — skipping channel %d", preset_name, exc, ch)

        return state

    async def apply_preset(self, preset_name: str, backend: IOBackend) -> None:
        """
        Resolve preset and write coils to all boards via backend.

        Updates desired state. One write_coils call per board.
        """
        state = self.resolve(preset_name)
        self._desired = state
        log.debug("Applying preset '%s'", preset_name)

        for board_id, coils in state.items():
            board = backend.get_board(board_id)
            ok = await board.write_coils(coils)
            if not ok:
                log.error(
                    "apply_preset '%s': write_coils failed on board '%s' (status=%s)",
                    preset_name, board_id, board.status,
                )

    async def apply_direct(
        self, board_id: str, channel: int, state: bool, backend: IOBackend
    ) -> None:
        """
        Toggle a single channel directly (master-mode use only).

        channel is 1-indexed local to the specified board (1–16).
        Updates desired state and writes the full board atomically.
        """
        if board_id not in self._board_ids:
            raise ValueError(f"Unknown board_id: '{board_id}'")
        local_idx = channel - 1
        if not (0 <= local_idx < 16):
            raise ValueError(f"Channel {channel} out of range 1–16 for board '{board_id}'")

        self._desired[board_id][local_idx] = state
        log.debug("Direct toggle board='%s' ch=%d state=%s", board_id, channel, state)

        board = backend.get_board(board_id)
        ok = await board.write_coils(list(self._desired[board_id]))
        if not ok:
            log.error(
                "apply_direct: write_coils failed on board '%s' (status=%s)",
                board_id, board.status,
            )

    def desired_state(self) -> dict[str, list[bool]]:
        """Return a copy of the current desired coil state for reconciliation."""
        return {bid: list(coils) for bid, coils in self._desired.items()}
