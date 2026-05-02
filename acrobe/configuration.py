"""User-level configuration loaded from `~/.config/acrobe.conf` (YAML).

Reusable across modules — each component declares its own top-level
section. Example:

    wire:
      servers:
        server0:
          base: http://10.0.4.9:1234

    # other sections may be added here as the project grows.

The file is loaded lazily on first access. Sections that aren't
present return an empty dict — modules treat that as "no
configuration" and use defaults rather than failing.

Path resolution:

* `ACROBE_CONFIG` env var, if set, takes precedence (full file path).
* Otherwise `$XDG_CONFIG_HOME/acrobe.conf`, falling back to
  `~/.config/acrobe.conf`.

The default global instance (via `get_configuration()`) loads from
the resolved path. Tests construct their own `Configuration(path=...)`
to stay isolated.
"""

import os
from pathlib import Path
from typing import Any

import yaml


class ConfigurationError(Exception):
    """Raised when the config file is present but malformed."""


class Configuration:
    """In-memory view of the user's acrobe config file.

    Construct with an explicit path for tests; the global default
    instance resolves the path from env vars and XDG conventions.
    Sections are returned as plain dicts — callers that want
    typed views layer their own parsing on top.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else self._default_path()
        self._data: dict | None = None

    @staticmethod
    def _default_path() -> Path:
        env = os.environ.get("ACROBE_CONFIG")
        if env:
            return Path(env)
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        return base / "acrobe.conf"

    def reload(self) -> None:
        """Force re-read of the config file. Otherwise loaded lazily."""
        self._data = None
        self._load()

    def _load(self) -> None:
        if self._data is not None:
            return
        if not self.path.exists():
            self._data = {}
            return
        try:
            with open(self.path) as f:
                parsed = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ConfigurationError(
                f"{self.path}: failed to parse YAML — {exc}") from exc
        if parsed is None:
            self._data = {}
        elif isinstance(parsed, dict):
            self._data = parsed
        else:
            raise ConfigurationError(
                f"{self.path}: top-level YAML must be a mapping, "
                f"got {type(parsed).__name__}")

    def section(self, name: str) -> dict[str, Any]:
        """Return the named top-level section, or {} if absent.

        Always returns a dict; modules can rely on dict-shaped access
        without having to None-check.
        """
        self._load()
        value = self._data.get(name, {})
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigurationError(
                f"{self.path}: section {name!r} must be a mapping, "
                f"got {type(value).__name__}")
        return value


_default: Configuration | None = None


def get_configuration() -> Configuration:
    """Return the process-wide Configuration instance.

    Lazily constructs one on first call. Callers that need
    isolation (tests, alternate config paths) construct their own
    `Configuration(path=...)` directly.
    """
    global _default
    if _default is None:
        _default = Configuration()
    return _default
