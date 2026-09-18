"""
vision/__init__.py — Watches the ceiling dots and decides when a beam broke.

Inputs:  RTSP frames from eight cameras, ROIs and baselines from beams.json
Outputs: break and clear events, stall/fault signals, evidence crops
Invariant: suppress when unsure — a frame gap, a mass-dark, or an
           uncalibrated preset means silence, never a guessed bust.
"""
