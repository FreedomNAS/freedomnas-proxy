"""
The reverse proxy: Caddy, installed from the edition's packages, serving
every site over HTTPS with FreedomNAS's own certificate.

What it writes, at the paths the profile names:

  config_file  Caddy's configuration, the whole file, from main_template; the
               distribution's own is kept beside it as <file>.pre-freedomnas
  tls_dir      a copy of FreedomNAS's certificate and key the Caddy user can
               read, copied again whenever FreedomNAS issues a new certificate
  sites_dir    one <owner>-<name>.caddy per site, from site_template, whose
               first line describes the site. This module owns the "site-*"
               files (the sites added by hand); another module writes its own
               with the same template and the profile's site_write, then runs
               the profile's reload (containers: "app-*" for its apps)

A site listens on a port of the NAS, for every name and address, and passes
the requests on to an upstream (host:port) over plain HTTP: the app behind it
keeps its own port, moved where only the proxy reaches it.

Configuration (modules.proxy): {"enabled": bool, "sites": {name: {"port", "upstream"}}}.
"""

from __future__ import annotations

import grp
import ipaddress
import logging
import os
import re
import socket
from typing import Any, Dict, List, Optional

try:
    from freedomnas import tls
    from freedomnas.reconcile import ActionResult
except ImportError:
    import tls  # type: ignore[no-redef]
    from reconcile import ActionResult  # type: ignore[no-redef]

logger = logging.getLogger("FreedomNAS.Module.Proxy")

MODULE_ID = "proxy"
CATEGORY = "proxy"
OWNER = "site"
HEADER = "# freedomnas-site "
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?")
_LISTEN = re.compile(r"[:.](\d+)$")


class ProxyError(Exception):
    """A request the proxy refuses: the message says why."""


def value(engine, key: str, default: Any = None) -> Any:
    try:
        return engine.get_profile_section(CATEGORY).get(key, default)
    except Exception:
        return default


def services(engine) -> List[str]:
    return list(value(engine, "services", []))


def installed(engine) -> bool:
    try:
        engine.execute(CATEGORY, "installed_check")
        return True
    except Exception:
        return False


def install(engine) -> None:
    for name in value(engine, "packages", []):
        engine.execute("packages", "install", timeout=7200, package_name=name)


def running(engine) -> bool:
    names = services(engine)
    if not names:
        return False
    for name in names:
        try:
            out, _ = engine.execute("services", "is_running", check=False, service_name=name)
        except Exception:
            return False
        if out.strip() != "yes":
            return False
    return True


def _hostname() -> str:
    return socket.gethostname() or "freedomnas"


def check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ProxyError("A site name is lowercase letters, digits and dashes, up to 31, starting with a letter or digit")
    return name


def check_port(port: Any) -> int:
    try:
        number = int(str(port).strip())
    except ValueError:
        raise ProxyError(f"'{port}' is not a port number")
    if not 1 <= number <= 65535:
        raise ProxyError(f"Port {number} is outside 1-65535")
    return number


def check_upstream(upstream: str) -> str:
    text = str(upstream or "").strip()
    host, sep, port = text.rpartition(":")
    if not sep or not host:
        raise ProxyError("The upstream is host:port, such as 127.0.0.1:8096")
    check_port(port)
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise ProxyError(f"'{host}' is not an IPv6 address")
    elif not _HOST.fullmatch(host):
        raise ProxyError(f"'{host}' is not a host name or address")
    return text


def main_text(engine, hostname: Optional[str] = None) -> str:
    return value(engine, "main_template").format(
        hostname=hostname or _hostname(), tls_dir=value(engine, "tls_dir"), sites_dir=value(engine, "sites_dir"))


def site_text(engine, owner: str, name: str, port: int, upstream: str) -> str:
    return value(engine, "site_template").format(owner=owner, name=name, port=port, upstream=upstream)


def site_file(engine, owner: str, name: str) -> str:
    return f"{value(engine, 'sites_dir')}/{owner}-{name}.caddy"


def sites(engine) -> List[Dict[str, Any]]:
    """Every site in sites_dir, whichever module wrote it, from its first line."""
    try:
        out, _ = engine.execute(CATEGORY, "sites_list", dir=value(engine, "sites_dir"))
    except Exception:
        return []
    found = []
    for line in out.splitlines():
        if not line.startswith(HEADER):
            continue
        words = line[len(HEADER):].split()
        if len(words) == 4 and words[2].isdigit():
            found.append({"owner": words[0], "name": words[1], "port": int(words[2]), "upstream": words[3]})
    return sorted(found, key=lambda s: (s["port"], s["owner"], s["name"]))


def listening_ports(engine) -> set:
    ports = set()
    try:
        out, _ = engine.execute(CATEGORY, "listening")
    except Exception:
        return ports
    for line in out.splitlines():
        match = _LISTEN.search(line.strip())
        if match:
            ports.add(int(match.group(1)))
    return ports


def _tls_current(engine) -> bool:
    folder = value(engine, "tls_dir")
    try:
        gid = grp.getgrnam(value(engine, "group")).gr_gid
        with open(f"{folder}/cert.pem", "rb") as cert, open(f"{folder}/key.pem", "rb") as key:
            return (cert.read() == tls.cert_file().read_bytes()
                    and key.read() == tls.key_file().read_bytes()
                    and os.fstat(cert.fileno()).st_gid == gid == os.fstat(key.fileno()).st_gid)
    except (OSError, KeyError):
        return False


def prepare(engine) -> bool:
    """The directories, the copy of the certificate and Caddy's configuration; True if any changed."""
    group = value(engine, "group")
    engine.execute(CATEGORY, "service_configure")
    for key in ("tls_dir", "sites_dir"):
        engine.execute(CATEGORY, "dir_prepare", group=group, path=value(engine, key))
    changed = False
    if not _tls_current(engine):
        engine.execute(CATEGORY, "tls_copy", group=group, path=value(engine, "tls_dir"),
                       cert=str(tls.cert_file()), key=str(tls.key_file()))
        changed = True
    config_file = value(engine, "config_file")
    wanted = main_text(engine)
    current, _ = engine.execute(CATEGORY, "file_read", check=False, file=config_file)
    if current.rstrip() != wanted.rstrip():
        engine.execute(CATEGORY, "config_backup", file=config_file)
        engine.execute_with_stdin(CATEGORY, "file_write", wanted, file=config_file)
        changed = True
    return changed


def apply(engine) -> None:
    """Check the configuration, then have Caddy load it (started if it is not running)."""
    config_file = value(engine, "config_file")
    engine.execute(CATEGORY, "validate", file=config_file)
    try:
        engine.execute(CATEGORY, "reload", file=config_file)
    except Exception:
        for name in services(engine):
            engine.execute("services", "restart", service_name=name)


def _section(config) -> Dict[str, Any]:
    return dict(config.get_module_config(MODULE_ID) or {})


def setup(engine, config) -> None:
    """Install Caddy if needed, take over its configuration and start it at boot."""
    if not installed(engine):
        install(engine)
    prepare(engine)
    engine.execute(CATEGORY, "validate", file=value(engine, "config_file"))
    for name in services(engine):
        engine.execute("services", "enable", service_name=name)
        engine.execute("services", "restart", service_name=name)
    section = _section(config)
    section["enabled"] = True
    section.setdefault("sites", {})
    config.set_module_config(MODULE_ID, section)
    reconcile_others(engine, config)


def reconcile_others(engine, config) -> None:
    """The other modules' reconcile, so the ones with web apps put them behind the proxy at once."""
    try:
        from freedomnas.module_loader import ModuleLoader
    except ImportError:
        from module_loader import ModuleLoader  # type: ignore[no-redef]
    for manifest, module in ModuleLoader(engine, config).instances():
        module_id = manifest.get("id", module.name)
        if module_id == MODULE_ID:
            continue
        try:
            for result in module.reconcile(config.get_module_config(module_id) or {}, "additive") or []:
                if result.target_state == "failed":
                    logger.warning("%s: %s", result.action, result.error)
        except Exception as exc:
            logger.warning("Module %s did not reconcile after the proxy setup: %s", module_id, exc)


def enabled(config) -> bool:
    return bool(_section(config).get("enabled"))


def add_site(engine, config, name: str, port: Any, upstream: str) -> Dict[str, Any]:
    """A site added by hand: port of the NAS → upstream, checked by Caddy before it is kept."""
    if not enabled(config):
        raise ProxyError("Set up the reverse proxy first")
    name, port, upstream = check_name(name), check_port(port), check_upstream(upstream)
    section = _section(config)
    manual = dict(section.get("sites") or {})
    taken = {s["port"]: s for s in sites(engine) if not (s["owner"] == OWNER and s["name"] == name)}
    if port in taken:
        other = taken[port]
        raise ProxyError(f"Port {port} is already the site '{other['name']}' ({other['owner']})")
    own = {s["port"] for s in sites(engine)}
    if port in listening_ports(engine) - own:
        raise ProxyError(f"Port {port} is in use on the NAS")
    file = site_file(engine, OWNER, name)
    previous = manual.get(name)
    engine.execute_with_stdin(CATEGORY, "site_write", site_text(engine, OWNER, name, port, upstream), file=file)
    try:
        apply(engine)
    except Exception:
        if previous:
            engine.execute_with_stdin(CATEGORY, "site_write",
                                      site_text(engine, OWNER, name, previous["port"], previous["upstream"]), file=file)
        else:
            engine.execute(CATEGORY, "file_remove", file=file)
        try:
            apply(engine)
        except Exception as exc:
            logger.error("Caddy did not come back with the previous sites: %s", exc)
        raise
    manual[name] = {"port": port, "upstream": upstream}
    section["sites"] = manual
    config.set_module_config(MODULE_ID, section)
    return {"name": name, "port": port, "upstream": upstream}


def remove_site(engine, config, name: str) -> None:
    name = check_name(name)
    section = _section(config)
    manual = dict(section.get("sites") or {})
    if name not in manual:
        raise ProxyError(f"No site '{name}'")
    engine.execute(CATEGORY, "file_remove", file=site_file(engine, OWNER, name))
    apply(engine)
    del manual[name]
    section["sites"] = manual
    config.set_module_config(MODULE_ID, section)


def status(engine, config) -> Dict[str, Any]:
    is_installed = installed(engine)
    return {
        "installed": is_installed,
        "running": running(engine) if is_installed else False,
        "enabled": enabled(config),
        "sites": sites(engine) if is_installed else [],
        "ca_fingerprint": tls.ca_fingerprint() if tls.ca_file().exists() else None,
    }


def _result(action: str, error: Optional[str] = None) -> ActionResult:
    return ActionResult(action=f"proxy.{action}", params={},
                        target_state="failed" if error else "applied", error=error)


def reconcile(engine, desired: Dict[str, Any], mode: str = "declarative") -> List[ActionResult]:
    """Caddy's configuration, the certificate and the sites added by hand back as config.yaml says."""
    if not desired.get("enabled"):
        return []
    if not installed(engine):
        return [_result("installed", "Caddy is not installed: install it from the Reverse proxy page")]
    results: List[ActionResult] = []
    try:
        changed = prepare(engine)
        manual = desired.get("sites") or {}
        for name, site in sorted(manual.items()):
            file = site_file(engine, OWNER, check_name(name))
            text = site_text(engine, OWNER, name, check_port(site["port"]), check_upstream(site["upstream"]))
            current, _ = engine.execute(CATEGORY, "file_read", check=False, file=file)
            if current.rstrip() != text.rstrip():
                engine.execute_with_stdin(CATEGORY, "site_write", text, file=file)
                changed = True
        if mode == "declarative":
            for site in sites(engine):
                if site["owner"] == OWNER and site["name"] not in manual:
                    engine.execute(CATEGORY, "file_remove", file=site_file(engine, OWNER, site["name"]))
                    changed = True
        for name in services(engine):
            engine.execute("services", "enable", service_name=name)
        if changed or not running(engine):
            apply(engine)
            results.append(_result("config"))
    except Exception as exc:
        results.append(_result("config", str(exc)))
    return results
