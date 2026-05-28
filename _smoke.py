#!/usr/bin/env python3
"""Smoke test runner — always runs from the repo root."""
import subprocess, sys, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

cmd = sys.argv[1] if len(sys.argv) > 1 else "import_test"

if cmd == "import_test":
    from core import agent, keys_manager, memory, intelligent_router
    from api import endpoints
    import run
    print("baseline ok")
elif cmd == "import_agent":
    from core import agent
    print("ok")
elif cmd == "import_run":
    import run
    print("ok")
elif cmd == "test_makedirs":
    # Test data/ creation via _resolve_db_url
    from core.memory import _resolve_db_url
    url, is_postgres = _resolve_db_url()
    print(f"URL: {url}")
    import os as _os
    if _os.path.exists("data"):
        print("PASS: data/ created automatically")
    else:
        print("FAIL: data/ not created!")
elif cmd == "server":
    import run
