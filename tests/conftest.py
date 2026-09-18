import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def load_plugin_module():
    """Load this repo's plugin entrypoint standalone, the same way the host
    loads plugins (file-location spec, no sys.modules registration)."""
    init = _REPO_ROOT / "plugin" / "plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("plugin_under_test", init)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
