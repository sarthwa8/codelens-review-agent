import os

# Settings are read at import time by some modules, so configure the environment first.
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
