"""
persist/__init__.py — Local storage. There is no cloud — see ADR 0008.

Inputs:  run rows, player rows, config-audit entries
Outputs: SQLite (WAL), rolling snapshots, CSV exports
Invariant: the game path writes and returns; nothing here is awaited from
           a run. A run row is written at GO so a crash leaves a record.
"""
