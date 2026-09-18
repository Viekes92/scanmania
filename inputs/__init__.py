"""
inputs/__init__.py — Physical inputs: start plate, two checkpoints, stop button.

Inputs:  an Arduino Opta over Modbus TCP, or a Pico over USB serial
Outputs: (input_id, state, host_ns) events, plus input_level() for the
         current level of an input that never sent an edge
Invariant: fake.py is mandatory — the game runs on a laptop with no rig.
"""
