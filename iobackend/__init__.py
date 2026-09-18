"""
iobackend/__init__.py — The things that switch: relays, and one DMX universe.

Inputs:  presets from mazes.yaml, FSM side effects, light cues
Outputs: Modbus coil writes; Art-Net frames to the hazer and room lights
Invariant: every coil write goes through presets.py (reconcile.py is the
           one sanctioned exception). One universe, one owner: dmx.py.
"""
