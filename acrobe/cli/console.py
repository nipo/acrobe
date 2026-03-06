from .. import plugin
from . import base  # noqa: F401 — cli/__init__.py triggers info registration


def main():
    plugin.load_plugins()
    base.cli()
