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

With names on, every site also answers as https://<name>.<hostname>.home.arpa
on port 443 (FreedomNAS's certificate names *.<hostname>.home.arpa), and
http:// on port 80 sends there. Each site file imports names_dir/*.caddy with
its name and upstream; names_dir holds the one names_template file while names
are on and nothing while they are off, so the sites are the same files either
way and no other module needs to know about names.

With a domain of the user's own (modules.proxy.acme), lego gets a Let's
Encrypt certificate for <domain> and *.<domain> through the DNS-01 challenge
of the user's DNS provider (no port is opened to the internet), a daily cron
job renews it, and every site also answers as https://<name>.<domain>. The
provider's credentials stay in a root-only env file beside lego's data.

The devices find the names through the network's DNS. When it is a Pi-hole
(v6) of the network (modules.proxy.dns), FreedomNAS keeps one of its local DNS
records per domain in step with the sites: "<address> <hostname>
<hostname>.home.arpa <site>.<hostname>.home.arpa ..." and, with an own domain,
"<address> <site>.<domain> ...",
through Pi-hole's API: Pi-hole takes no wildcard from its API, so the record
names every site. Records naming anything outside these domains are never
touched.

Configuration (modules.proxy): {"enabled": bool, "names": bool,
"sites": {name: {"port", "upstream"}},
"acme": {"domain", "email", "provider", "server", "env": {KEY: value}},
"dns": {"url", "password"}}.
"""

from __future__ import annotations

import grp
import ipaddress
import logging
import os
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from freedomnas import paths, tls
    from freedomnas.reconcile import ActionResult
except ImportError:
    import paths  # type: ignore[no-redef]
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
    if value(engine, "install_prepare"):
        engine.execute(CATEGORY, "install_prepare")
    init = (value(engine, "init_packages", {}) or {}).get(getattr(engine, "init_system", ""), [])
    for name in [*value(engine, "packages", []), *init]:
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


def domain(hostname: Optional[str] = None) -> str:
    return tls.local_domain(hostname or _hostname())


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


def names_file(engine, suffix: str = "") -> str:
    return f"{value(engine, 'names_dir')}/name{suffix}.caddy"


def redirect_file(engine, suffix: str = "") -> str:
    return f"{value(engine, 'sites_dir')}/names{suffix}.caddy"


def _put(engine, file: str, text: Optional[str]) -> bool:
    """The file with this text, or no file for None; True if it changed."""
    current, _ = engine.execute(CATEGORY, "file_read", check=False, file=file)
    if text is None:
        if not current:
            return False
        engine.execute(CATEGORY, "file_remove", file=file)
        return True
    if current.rstrip() == text.rstrip():
        return False
    engine.execute_with_stdin(CATEGORY, "file_write", text, file=file)
    return True


def _names_files(engine, suffix: str, name: Optional[str], tls_line: str) -> bool:
    names = value(engine, "names_template").format(domain=name, tls=tls_line) if name else None
    redirect = value(engine, "redirect_template").format(domain=name, tls=tls_line) if name else None
    changed = _put(engine, names_file(engine, suffix), names)
    return _put(engine, redirect_file(engine, suffix), redirect) or changed


def prepare_names(engine, on: bool, acme: Optional[Dict[str, Any]] = None) -> bool:
    """
    The names' files: the local domain's with FreedomNAS's certificate, the
    own domain's with its Let's Encrypt one once it is there; and what lets
    Caddy open ports 443 and 80 (names_configure, every time names are on;
    names_unconfigure once, when they go off).
    """
    current, _ = engine.execute(CATEGORY, "file_read", check=False, file=names_file(engine))
    if on:
        engine.execute(CATEGORY, "names_configure")
    changed = _names_files(engine, "", domain() if on else None, "import freedomnas_tls")
    own = acme.get("domain") if (on and acme and acme_ready(engine)) else None
    folder = value(engine, "acme_tls_dir")
    changed = _names_files(engine, "-acme", own, f"tls {folder}/cert.pem {folder}/key.pem") or changed
    if not on and current:
        engine.execute(CATEGORY, "names_unconfigure")
    return changed


def prepare(engine, names: bool = False, acme: Optional[Dict[str, Any]] = None) -> bool:
    """The directories, the copy of the certificate and Caddy's configuration; True if any changed."""
    group = value(engine, "group")
    engine.execute(CATEGORY, "service_configure")
    for key in ("tls_dir", "sites_dir", "names_dir"):
        engine.execute(CATEGORY, "dir_prepare", group=group, path=value(engine, key))
    changed = prepare_names(engine, names, acme)
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
    prepare(engine, bool(_section(config).get("names")))
    engine.execute(CATEGORY, "validate", file=value(engine, "config_file"))
    for name in services(engine):
        engine.execute("services", "enable", service_name=name)
        engine.execute("services", "restart", service_name=name)
    section = _section(config)
    section["enabled"] = True
    section.setdefault("sites", {})
    config.set_module_config(MODULE_ID, section)
    reconcile_others(engine, config)


def reconcile_others(engine, config) -> List[str]:
    """
    The other modules' reconcile, so the ones with web apps move behind the
    proxy (or back on their own ports) at once; what failed, one line each.
    """
    try:
        from freedomnas.module_loader import ModuleLoader
    except ImportError:
        from module_loader import ModuleLoader  # type: ignore[no-redef]
    failures = []
    for manifest, module in ModuleLoader(engine, config).instances():
        module_id = manifest.get("id", module.name)
        if module_id == MODULE_ID:
            continue
        try:
            for result in module.reconcile(config.get_module_config(module_id) or {}, "additive") or []:
                if result.target_state == "failed":
                    logger.warning("%s: %s", result.action, result.error)
                    failures.append(f"{result.action}: {result.error}")
        except Exception as exc:
            logger.warning("Module %s did not reconcile after the proxy change: %s", module_id, exc)
            failures.append(f"{module_id}: {exc}")
    return failures


def disable(engine, config) -> None:
    """
    Stop Caddy and keep it off at boot, then let the apps behind it go back
    to their own ports. Caddy stays installed and the sites added by hand
    stay in the configuration, so setting it up again brings them back.
    """
    if not enabled(config):
        raise ProxyError("The reverse proxy is not set up")
    for name in services(engine):
        engine.execute("services", "stop", service_name=name)
        engine.execute("services", "disable", service_name=name)
    section = _section(config)
    section["enabled"] = False
    config.set_module_config(MODULE_ID, section)
    failures = reconcile_others(engine, config)
    if failures:
        raise ProxyError("Caddy is off, but some apps did not come back on their own port: " + "; ".join(failures))


def enabled(config) -> bool:
    return bool(_section(config).get("enabled"))


def add_site(engine, config, name: str, port: Any, upstream: str) -> Dict[str, Any]:
    """A site added by hand: port of the NAS → upstream, checked by Caddy before it is kept."""
    if not enabled(config):
        raise ProxyError("Set up the reverse proxy first")
    name, port, upstream = check_name(name), check_port(port), check_upstream(upstream)
    section = _section(config)
    manual = dict(section.get("sites") or {})
    others = [s for s in sites(engine) if not (s["owner"] == OWNER and s["name"] == name)]
    clash = next((s for s in others if s["name"] == name), None)
    if clash:
        raise ProxyError(f"The name '{name}' is already the site of a {clash['owner']}: choose another one")
    if _section(config).get("names") and port in NAME_PORTS:
        raise ProxyError(f"Port {port} serves the sites by name: choose another one")
    taken = {s["port"]: s for s in others}
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


NAME_PORTS = (443, 80)


def set_names(engine, config, on: bool) -> Dict[str, Any]:
    """
    Serve every site by name too (https://<name>.<hostname>.home.arpa), or
    stop. The sites' files are written again first (here and by the other
    modules), so every one of them imports the names.
    """
    if not enabled(config):
        raise ProxyError("Set up the reverse proxy first")
    section = _section(config)
    was = bool(section.get("names"))
    if on and not was:
        busy = [p for p in NAME_PORTS if p in listening_ports(engine)]
        if busy:
            raise ProxyError(f"Port {' and '.join(map(str, busy))} is in use on the NAS: names need 443 and 80")
    section["names"] = on
    failed = [r.error for r in reconcile(engine, section, "additive") if r.target_state == "failed"]
    if failed:
        section["names"] = was
        reconcile(engine, section, "additive")
        raise ProxyError(f"Caddy refused the names: {failed[0]}")
    config.set_module_config(MODULE_ID, section)
    for failure in reconcile_others(engine, config):
        logger.warning("After turning names %s: %s", "on" if on else "off", failure)
    return names_status(engine, config)


def lan_address() -> Optional[str]:
    """The NAS's address on the network of its default route, where devices find it."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def names_status(engine, config) -> Dict[str, Any]:
    acme = _section(config).get("acme") or {}
    own = acme.get("domain") if acme and acme_ready(engine) else None
    return {"enabled": bool(_section(config).get("names")), "domain": domain(), "own_domain": own,
            "address": lan_address(), "dns": dns_status(engine, config)}


SERVERS = {
    "production": "https://acme-v02.api.letsencrypt.org/directory",
    "staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
}
_DOMAIN = re.compile(r"(?=.{4,200}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}")
_PROVIDER = re.compile(r"[a-z0-9]{1,40}(?:[._-][a-z0-9]{1,40}){0,3}")
_SERVER = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~-]+)*/?")
_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_ENV_VALUE = re.compile(r"[^\s'\\]{1,512}")


def _env_pairs(env: Any) -> Dict[str, str]:
    if isinstance(env, str):
        pairs = {}
        for line in env.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, val = line.partition("=")
            if not sep:
                raise ProxyError(f"'{line}' is not KEY=value")
            pairs[key.strip()] = val.strip()
        env = pairs
    if not isinstance(env, dict):
        raise ProxyError("The provider's credentials are KEY=value lines")
    for key, val in env.items():
        if not _ENV_KEY.fullmatch(str(key)):
            raise ProxyError(f"'{key}' is not a variable name: capitals, digits and _")
        if not _ENV_VALUE.fullmatch(str(val)):
            raise ProxyError(f"The value of {key} is empty or has spaces, quotes or backslashes")
    return {str(k): str(v) for k, v in env.items()}


def check_acme(settings: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The settings as they are kept; credentials left out keep their previous values."""
    name = str(settings.get("domain") or "").strip().lower().rstrip(".")
    if not _DOMAIN.fullmatch(name):
        raise ProxyError(f"'{name}' is not a domain name such as nas.example.org")
    if name.endswith(".home.arpa") or name.endswith(".local"):
        raise ProxyError("Let's Encrypt signs only public domains, not .home.arpa or .local")
    email = str(settings.get("email") or "").strip()
    if not _EMAIL.fullmatch(email):
        raise ProxyError(f"'{email}' is not an email address")
    provider = str(settings.get("provider") or "").strip().lower()
    if not _PROVIDER.fullmatch(provider):
        raise ProxyError("The DNS provider is lego's name for it, such as desec, cloudflare or ovh")
    server = str(settings.get("server") or "production").strip()
    if server not in SERVERS and not _SERVER.fullmatch(server):
        raise ProxyError("The certificate authority is production, staging or an https:// ACME directory")
    env = _env_pairs(settings.get("env") or {})
    if previous and previous.get("provider") == provider:
        env = {**(previous.get("env") or {}), **env}
    if not env:
        raise ProxyError("Give the DNS provider's credentials (KEY=value lines, as lego names them)")
    return {"domain": name, "email": email, "provider": provider, "server": server, "env": env}


def _acme_paths(engine) -> Dict[str, str]:
    path = value(engine, "acme_dir")
    return {"path": path, "env_file": f"{path}/env", "hook": f"{path}/deploy-hook.sh",
            "tls_dir": value(engine, "acme_tls_dir"), "cron": value(engine, "acme_cron_file"),
            "log": value(engine, "acme_log")}


def _server_url(server: str) -> str:
    return SERVERS.get(server, server)


def _put_secret(engine, file: str, text: str) -> bool:
    current, _ = engine.execute(CATEGORY, "file_read", check=False, file=file)
    if current == text:
        return False
    engine.execute_with_stdin(CATEGORY, "acme_secret_write", text, file=file)
    return True


def acme_files(engine, acme: Dict[str, Any]) -> bool:
    """The env file with the credentials, the deploy hook and the renewal's cron file."""
    paths = _acme_paths(engine)
    env = "".join(f"{k}='{v}'\n" for k, v in sorted(acme["env"].items()))
    hook = value(engine, "acme_hook_template").format(
        domain=acme["domain"], group=value(engine, "group"), path=paths["path"], tls_dir=paths["tls_dir"],
        config_file=value(engine, "config_file"))
    cron = value(engine, "acme_cron_template").format(
        domain=acme["domain"], minute=sum(acme["domain"].encode()) % 60, env_file=paths["env_file"],
        email=acme["email"], server=_server_url(acme["server"]), provider=acme["provider"],
        path=paths["path"], hook=paths["hook"], log=paths["log"])
    changed = _put_secret(engine, paths["env_file"], env)
    changed = _put_secret(engine, paths["hook"], hook) or changed
    return _put_secret(engine, paths["cron"], cron) or changed


def acme_ready(engine) -> bool:
    current, _ = engine.execute(CATEGORY, "file_read", check=False, file=f"{value(engine, 'acme_tls_dir')}/cert.pem")
    return "BEGIN CERTIFICATE" in current


def acme_obtain(engine, acme: Dict[str, Any]) -> None:
    paths = _acme_paths(engine)
    try:
        engine.execute(CATEGORY, "acme_obtain", timeout=1200, env_file=paths["env_file"], email=acme["email"],
                       server=_server_url(acme["server"]), provider=acme["provider"], domain=acme["domain"],
                       path=paths["path"])
    except Exception as exc:
        text = str(exc)
        tail = text.split("Stderr:", 1)[-1].strip().splitlines()[-6:]
        raise ProxyError("Let's Encrypt did not sign the certificate: " + " ".join(l.strip() for l in tail))


def acme_install(engine, acme: Dict[str, Any]) -> bool:
    """Copy lego's certificate where Caddy reads it; True if the copy changed."""
    paths = _acme_paths(engine)
    issued, _ = engine.execute(CATEGORY, "file_read", check=False,
                               file=f"{paths['path']}/certificates/{acme['domain']}.crt")
    if "BEGIN CERTIFICATE" not in issued:
        return False
    copy, _ = engine.execute(CATEGORY, "file_read", check=False, file=f"{paths['tls_dir']}/cert.pem")
    if copy == issued:
        return False
    engine.execute(CATEGORY, "acme_install", group=value(engine, "group"), tls_dir=paths["tls_dir"],
                   path=paths["path"], domain=acme["domain"])
    return True


def set_acme(engine, config, settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Use a domain of the user's own: get its Let's Encrypt certificate through
    the DNS provider and serve every site by name under it too.
    """
    if not enabled(config):
        raise ProxyError("Set up the reverse proxy first")
    section = _section(config)
    if not section.get("names"):
        raise ProxyError("Serve the sites by name first: the own domain names them too")
    acme = check_acme(settings, section.get("acme"))
    try:
        engine.execute(CATEGORY, "acme_installed_check")
    except Exception:
        install(engine)
    acme_files(engine, acme)
    issued, _ = engine.execute(CATEGORY, "file_read", check=False,
                               file=f"{_acme_paths(engine)['path']}/certificates/{acme['domain']}.crt")
    previous = section.get("acme") or {}
    if "BEGIN CERTIFICATE" not in issued or any(previous.get(k) != acme[k] for k in ("domain", "server", "provider")):
        try:
            acme_obtain(engine, acme)
        except ProxyError:
            if previous:
                acme_files(engine, previous)
            else:
                paths = _acme_paths(engine)
                engine.execute(CATEGORY, "acme_remove", cron=paths["cron"], tls_dir=paths["tls_dir"])
            raise
    acme_install(engine, acme)
    section["acme"] = acme
    failed = [r.error for r in reconcile(engine, section, "additive") if r.target_state == "failed"]
    if failed:
        raise ProxyError(f"The certificate is there, but Caddy refused it: {failed[0]}")
    config.set_module_config(MODULE_ID, section)
    for failure in reconcile_others(engine, config):
        logger.warning("After the own domain: %s", failure)
    return acme_status(engine, config)


def remove_acme(engine, config) -> None:
    section = _section(config)
    if not section.get("acme"):
        raise ProxyError("No own domain is set up")
    paths = _acme_paths(engine)
    gone = section.pop("acme")["domain"]
    engine.execute(CATEGORY, "acme_remove", cron=paths["cron"], tls_dir=paths["tls_dir"])
    reconcile(engine, section, "additive")
    sync_dns(engine, section, also=[gone])
    config.set_module_config(MODULE_ID, section)
    for failure in reconcile_others(engine, config):
        logger.warning("After removing the own domain: %s", failure)


def acme_status(engine, config) -> Optional[Dict[str, Any]]:
    acme = _section(config).get("acme")
    if not acme:
        return None
    expires = None
    copy, _ = engine.execute(CATEGORY, "file_read", check=False, file=f"{value(engine, 'acme_tls_dir')}/cert.pem")
    if "BEGIN CERTIFICATE" in copy:
        try:
            from cryptography import x509
            expires = x509.load_pem_x509_certificate(copy.encode()).not_valid_after_utc.isoformat()
        except Exception:
            expires = None
    return {"domain": acme["domain"], "email": acme["email"], "provider": acme["provider"],
            "server": acme["server"], "credentials": sorted(acme.get("env") or {}), "expires": expires}


def status(engine, config) -> Dict[str, Any]:
    is_installed = installed(engine)
    return {
        "installed": is_installed,
        "running": running(engine) if is_installed else False,
        "enabled": enabled(config),
        "sites": sites(engine) if is_installed else [],
        "names": names_status(engine, config),
        "acme": acme_status(engine, config) if is_installed else None,
        "ca_fingerprint": tls.ca_fingerprint() if tls.ca_file().exists() else None,
    }


_DNS_URL = re.compile(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?/?")
DNS_STATE_FILE = "proxy-dns.json"


def _dns_state_file(engine) -> Path:
    return paths.config_dir(engine) / DNS_STATE_FILE


def _load_dns_state(engine) -> Dict[str, Any]:
    try:
        return json.loads(_dns_state_file(engine).read_text())
    except (OSError, ValueError):
        return {"error": None, "synced": None, "records": []}


def _save_dns_state(engine, state: Dict[str, Any]) -> None:
    try:
        _dns_state_file(engine).write_text(json.dumps(state))
    except OSError as exc:
        logger.warning("Could not keep the DNS state: %s", exc)


def _pihole(base: str, method: str, path: str, body: Any = None, sid: Optional[str] = None) -> Any:
    """One call to Pi-hole's API; its own certificate is self-signed, so https is not verified."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    headers = {"Content-Type": "application/json", **({"X-FTL-SID": sid} if sid else {})}
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base.rstrip("/") + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and path == "/api/auth":
            raise ProxyError("Pi-hole refused the password")
        try:
            message = json.loads(exc.read().decode())["error"]["message"]
        except Exception:
            message = f"HTTP {exc.code}"
        raise ProxyError(f"Pi-hole answered {exc.code}: {message}")
    except (urllib.error.URLError, OSError) as exc:
        raise ProxyError(f"Pi-hole at {base} cannot be reached: {getattr(exc, 'reason', exc)}")
    return json.loads(text) if text else {}


def _pihole_login(dns: Dict[str, Any]) -> str:
    answer = _pihole(dns["url"], "POST", "/api/auth", {"password": dns["password"]})
    session = answer.get("session") or {}
    if not session.get("valid"):
        raise ProxyError("Pi-hole refused the password")
    return session.get("sid") or ""


def _record_domains(section: Dict[str, Any]) -> List[str]:
    own = (section.get("acme") or {}).get("domain")
    return [domain()] + ([own] if own else [])


def dns_records(engine, section: Dict[str, Any]) -> List[str]:
    """The local DNS records the network's Pi-hole should hold for this NAS."""
    address = lan_address()
    if not (section.get("names") and address):
        return []
    names = sorted({s["name"] for s in sites(engine)})
    local, *own = _record_domains(section)
    records = [f"{address} {_hostname()} {local} " + " ".join(f"{n}.{local}" for n in names)]
    records += [f"{address} " + " ".join(f"{n}.{d}" for n in names) for d in own if names]
    return [r.rstrip() for r in records]


def _ours(record: str, domains: List[str]) -> bool:
    names = record.split()[1:]
    return bool(names) and all(n == _hostname() or any(n == d or n.endswith("." + d) for d in domains)
                               for n in names)


def sync_dns(engine, section: Dict[str, Any], also: Optional[List[str]] = None) -> Optional[str]:
    """
    Bring the network's Pi-hole in step: its records for these domains (and
    `also`, domains this NAS stopped using) become dns_records, nothing else
    changes. Returns the error, or None.
    """
    dns = section.get("dns")
    if not dns:
        return None
    wanted = dns_records(engine, section)
    domains = _record_domains(section) + list(also or [])
    sid = None
    try:
        sid = _pihole_login(dns)
        current = (_pihole(dns["url"], "GET", "/api/config/dns/hosts", sid=sid).get("config") or {}) \
            .get("dns", {}).get("hosts", [])
        for record in current:
            if _ours(record, domains) and record not in wanted:
                _pihole(dns["url"], "DELETE", "/api/config/dns/hosts/" + urllib.parse.quote(record, safe=""), sid=sid)
        for record in wanted:
            if record not in current:
                _pihole(dns["url"], "PUT", "/api/config/dns/hosts/" + urllib.parse.quote(record, safe=""), sid=sid)
        _save_dns_state(engine, {"error": None, "synced": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "records": wanted})
        return None
    except ProxyError as exc:
        _save_dns_state(engine, {**_load_dns_state(engine), "error": str(exc)})
        return str(exc)
    finally:
        if sid:
            try:
                _pihole(dns["url"], "DELETE", "/api/auth", sid=sid)
            except ProxyError:
                pass


def check_dns(settings: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = str(settings.get("url") or "").strip().rstrip("/")
    if not _DNS_URL.fullmatch(url + "/"):
        raise ProxyError("Pi-hole's address is http:// or https:// and its host, such as http://192.168.1.5")
    password = str(settings.get("password") or "")
    if not password and previous and previous.get("url") == url:
        password = previous.get("password") or ""
    if not password or len(password) > 256 or any(c in password for c in "\r\n"):
        raise ProxyError("Give Pi-hole's app password (Settings → Web interface / API → App password)")
    return {"url": url, "password": password}


def set_dns(engine, config, settings: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the names in the network's Pi-hole from now on, starting now."""
    if not enabled(config):
        raise ProxyError("Set up the reverse proxy first")
    section = _section(config)
    dns = check_dns(settings, section.get("dns"))
    sid = _pihole_login(dns)
    try:
        _pihole(dns["url"], "GET", "/api/config/dns/hosts", sid=sid)
    finally:
        try:
            _pihole(dns["url"], "DELETE", "/api/auth", sid=sid)
        except ProxyError:
            pass
    section["dns"] = dns
    error = sync_dns(engine, section)
    if error:
        raise ProxyError(error)
    config.set_module_config(MODULE_ID, section)
    return dns_status(engine, config)


def remove_dns(engine, config) -> None:
    """Take this NAS's records out of the network's Pi-hole and stop keeping them."""
    section = _section(config)
    if not section.get("dns"):
        raise ProxyError("No Pi-hole of the network is set")
    cleared = {**section, "names": False}
    error = sync_dns(engine, cleared)
    section.pop("dns")
    config.set_module_config(MODULE_ID, section)
    _save_dns_state(engine, {"error": None, "synced": None, "records": []})
    if error:
        raise ProxyError(f"Forgotten, but its records could not be taken out: {error}")


def dns_status(engine, config) -> Optional[Dict[str, Any]]:
    dns = _section(config).get("dns")
    if not dns:
        return None
    return {"url": dns["url"], **_load_dns_state(engine)}


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
    acme = desired.get("acme") if desired.get("names") else None
    if acme:
        try:
            acme_files(engine, acme)
            if not acme_ready(engine) and not acme_install(engine, acme):
                acme_obtain(engine, acme)
            acme_install(engine, acme)
        except Exception as exc:
            results.append(_result("acme", str(exc)))
    else:
        cron, _ = engine.execute(CATEGORY, "file_read", check=False, file=value(engine, "acme_cron_file"))
        if cron:
            engine.execute(CATEGORY, "acme_remove", cron=value(engine, "acme_cron_file"),
                           tls_dir=value(engine, "acme_tls_dir"))
    try:
        changed = prepare(engine, bool(desired.get("names")), acme)
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
    if desired.get("dns"):
        previous = list(_load_dns_state(engine).get("records") or [])
        error = sync_dns(engine, desired)
        if error:
            results.append(_result("dns", error))
        elif _load_dns_state(engine).get("records") != previous:
            results.append(_result("dns"))
    return results
