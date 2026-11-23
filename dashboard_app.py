"""Root shim so `streamlit run dashboard_app.py` works without PYTHONPATH."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from sentinel.dashboard.app import main  # noqa: E402

if __name__ == "__main__":
    main()
