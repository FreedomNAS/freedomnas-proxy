"""
ProxyModule — Caddy as the reverse proxy in front of the NAS's web apps.

The work is done in proxy.py, shared with api_routes.py; both load it by file
path, as freedomnas-core loads this file, so the module needs no importable
package.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import List, Tuple

try:
    from freedomnas.engine import FreedomNASEngine
    from freedomnas.config import ConfigurationManager
    from freedomnas.orchestrator import FreedomNASModule
except ImportError:
    from engine import FreedomNASEngine  # type: ignore[no-redef]
    from config import ConfigurationManager  # type: ignore[no-redef]
    from orchestrator import FreedomNASModule  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Module.Proxy")


def load(stem: str):
    name = f"freedomnas_proxy_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{stem}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


proxy = load("proxy")


class ProxyModule(FreedomNASModule):
    """Caddy with FreedomNAS's certificate, one HTTPS port per site."""

    @property
    def name(self) -> str:
        return proxy.MODULE_ID

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def health_check(self) -> bool:
        return self.health()[0]

    def health(self) -> Tuple[bool, str]:
        try:
            if not proxy.enabled(self._config):
                return True, "Reverse proxy not set up"
            if not proxy.running(self._engine):
                return False, "Caddy is not running"
            return True, f"Caddy running, {len(proxy.sites(self._engine))} sites"
        except Exception as exc:
            logger.warning("Proxy health check failed: %s", exc)
            return False, str(exc)

    def reconcile(self, desired: dict, mode: str = "declarative") -> List:
        return proxy.reconcile(self._engine, desired, mode)
