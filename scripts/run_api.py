"""Start the API locally.

    pip install -r requirements-api.txt
    python scripts/run_api.py                 # http://127.0.0.1:8000/docs

Environment: WHYMOVED_DATA_DIR (default data/app), WHYMOVED_MAX_UPLOAD_MB (25),
WHYMOVED_MAX_ROWS (1000000), WHYMOVED_CORS_ORIGINS (comma-separated, for the React app).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

from whymoved.app.api import create_app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
