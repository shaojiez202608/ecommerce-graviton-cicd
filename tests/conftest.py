import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "mcf"))
sys.path.insert(0, str(ROOT / "infra"))

# Never let tests reach a real AWS account.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
for var in ("AWS_PROFILE",):
    os.environ.pop(var, None)
