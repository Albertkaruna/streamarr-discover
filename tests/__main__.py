# python -m tests — runs every test_* function; no pytest needed (also runs during docker build).
import importlib
import pkgutil
import sys

import tests

failed = 0
for mod in pkgutil.iter_modules(tests.__path__):
    if mod.name.startswith("test_"):
        m = importlib.import_module(f"tests.{mod.name}")
        for name in sorted(n for n in dir(m) if n.startswith("test_")):
            try:
                getattr(m, name)()
                print("ok  ", name)
            except Exception as ex:
                failed += 1
                print("FAIL", name, repr(ex))
sys.exit(1 if failed else 0)
