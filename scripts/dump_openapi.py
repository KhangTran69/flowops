"""Dump the FastAPI OpenAPI schema for the committed snapshot check.

Usage:
    python scripts/dump_openapi.py [output-path]

The output is deterministic (sorted keys, fixed indent). CI compares this
against ``tests/openapi_snapshot.json``; a diff means the public API surface
changed. Intentional API changes must regenerate the snapshot in the same PR:

    python scripts/dump_openapi.py tests/openapi_snapshot.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow `python scripts/dump_openapi.py` from anywhere: run against the repo
# root that contains this script, not the interpreter's cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
    # Importing src.api runs the full app assembly; routers register at the
    # bottom of that module, so nothing else is needed here.
    from src.api import app

    spec = app.openapi()
    output.write_text(json.dumps(spec, sort_keys=True, indent=1) + "\n")
    print(f"wrote {output} ({len(spec['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
