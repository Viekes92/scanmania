"""
core/__init__.py — Pure game logic — the half with no I/O in it.

Inputs:  events from core/events.py, an FSMContext
Outputs: (new_state, [SideEffect, ...]) from transition(); elapsed times
Invariant: core/fsm.py is a pure function — no I/O, no await, no clock
           reads. The server owns the stopwatch (monotonic_ns only).
"""
