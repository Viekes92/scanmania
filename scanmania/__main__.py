"""Entry point for `python -m scanmania`. Delegates to the repo-root __main__.py."""
import importlib
import sys
from pathlib import Path

# Ensure repo root is on sys.path so all project imports work
_root = str(Path(__file__).resolve().parent.parent)
if _root not in sys.path:
    sys.path.insert(0, _root)

# Load the repo-root __main__.py as a regular module (not as __main__)
_spec = importlib.util.spec_from_file_location(
    "_scanmania_entry", Path(_root) / "__main__.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_mod.main()
