import sys


class Plugin:
    def __init__(self, name, module, path, loading_exc_info):
        self.name = name
        self.module = module
        self.path = path
        self.loading_exc_info = loading_exc_info


plugins = []


def load_plugins():
    global plugins
    plugins = _load_pep420_plugins()


def _load_pep420_plugins():
    import importlib
    import os

    debug = os.getenv("ACROBE_PLUGIN_DEBUG")

    try:
        import acrobe_plugin
    except ImportError:
        if debug:
            print("acrobe_plugin namespace package not found")
        return []

    import pkgutil

    result = []
    for finder, name, ispkg in pkgutil.iter_modules(
            acrobe_plugin.__path__, acrobe_plugin.__name__ + "."):
        if debug:
            print(f"Loading plugin {name}...")
        try:
            m = importlib.import_module(name)
            exc_info = None
        except Exception as e:
            m = None
            exc_info = sys.exc_info()
            print(f"Plugin {name} failed to load: {e}")
        result.append(Plugin(name, m, finder.path, exc_info))
    return result
