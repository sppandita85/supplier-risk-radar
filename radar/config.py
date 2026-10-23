"""Settings, loaded from environment variables (or a .env file in the project root)."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

PG_DSN = os.getenv("PG_DSN", "dbname=risk_radar")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.5"))

INPUT_DIR = ROOT / "data" / "input"
PROCESSED_DIR = ROOT / "data" / "processed"
FAILED_DIR = ROOT / "data" / "failed"


def load_suppliers() -> list[dict]:
    return yaml.safe_load((ROOT / "config" / "suppliers.yaml").read_text())["suppliers"]


def load_rules() -> dict:
    return yaml.safe_load((ROOT / "config" / "rules.yaml").read_text())
