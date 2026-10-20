from __future__ import annotations

"""V2 entry: validate config, serve trader API + UI."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "trader", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.core.config import load_settings, validate_settings  # noqa: E402


def main() -> int:
    s = load_settings()
    errs = validate_settings(s)
    if errs:
        print("Blocking configuration errors:")
        for e in errs:
            print(f"  - {e}")
        return 1
    import uvicorn
    print(f"Trader V2 on http://{s.api_host}:{s.api_port} (paper, live disabled)")
    uvicorn.run("trader.api.server:app", host=s.api_host, port=s.api_port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
