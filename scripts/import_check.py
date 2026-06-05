"""Import every integration module against an installed Home Assistant.

Catches wrong import paths / removed-API usage that hassfest does not — e.g.
importing a symbol from the wrong module. Requires `homeassistant` and
`uiprotect` to be installed. Exits non-zero on the first failure.

Run: pip install homeassistant uiprotect && python scripts/import_check.py
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODULES = [
    "custom_components.protect_media_viewer",  # __init__ pulls in api/frontend/cache/ws/...
    "custom_components.protect_media_viewer.config_flow",
    "custom_components.protect_media_viewer.api",
    "custom_components.protect_media_viewer.frontend",
    "custom_components.protect_media_viewer.cache",
    "custom_components.protect_media_viewer.websocket",
    "custom_components.protect_media_viewer.playback",
    "custom_components.protect_media_viewer.protect",
    "custom_components.protect_media_viewer.models",
]


def main() -> int:
    ok = True
    for mod in MODULES:
        try:
            importlib.import_module(mod)
            print(f"  OK   {mod}")
        except Exception as err:  # noqa: BLE001
            ok = False
            print(f"  FAIL {mod}: {type(err).__name__}: {err}")
            traceback.print_exc()
    print("\nALL IMPORTS OK" if ok else "\nIMPORT FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
