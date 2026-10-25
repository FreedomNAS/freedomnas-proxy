import grp
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CORE = REPO.parent / "freedomnas-core"
PROFILES = REPO.parent / "freedomnas-profiles"
_LINK = Path(tempfile.mkdtemp(prefix="fnas-core-")) / "freedomnas"
_LINK.symlink_to(CORE)
sys.path.insert(0, str(_LINK.parent))

from freedomnas.engine import CommandExecutionError  # noqa: E402

sys.path.insert(0, str(REPO / "modules" / "proxy"))
import proxy  # noqa: E402


class FakeConfig:
    def __init__(self, section=None):
        self.modules = {"proxy": dict(section or {})}

    def get_module_config(self, module_id):
        return json.loads(json.dumps(self.modules.get(module_id, {})))

    def set_module_config(self, module_id, section):
        self.modules[module_id] = json.loads(json.dumps(section))


class FakeEngine:
    """
    The Debian profile's values, with Caddy's files kept in memory (the TLS
    copy is real files under tmp, the module compares them). Commands are
    recorded; `failing` makes one raise.
    """

    def __init__(self, tmp: Path, edition: str = "debian", init_system: str = "systemd"):
        self.init_system = init_system
        section = json.loads((REPO / f"profiles/proxy/proxy-{edition}.json").read_text())["proxy"]
        section["tls_dir"] = str(tmp / "caddy-tls")
        section["group"] = grp.getgrgid(os.getgid()).gr_name
        self.section = section
        self.profile = {"paths": {"config_dir": str(tmp)}}
        self.files = {}
        self.calls = []
        self.failing = set()
        self.installed = True
        self.running = True
        self.listening = ""

    def get_profile_section(self, category):
        return self.section if category == "proxy" else {}

    def list_actions(self, category):
        return list(self.section) if category == "proxy" else []

    def _fail(self, category, action):
        if (category, action) in self.failing:
            raise CommandExecutionError(f"Command failed\nStderr: {category}.{action} refused")

    def execute(self, category, action, timeout=30, check=True, **kw):
        self.calls.append((category, action, kw))
        self._fail(category, action)
        if (category, action) == ("proxy", "installed_check") and not self.installed:
            raise CommandExecutionError("caddy: not found")
        if (category, action) == ("proxy", "file_read"):
            return self.files.get(kw["file"], ""), ""
        if (category, action) == ("proxy", "file_remove"):
            self.files.pop(kw["file"], None)
        if (category, action) == ("proxy", "sites_list"):
            heads = [text.splitlines()[0] for name, text in sorted(self.files.items())
                     if name.startswith(kw["dir"] + "/") and name.endswith(".caddy") and text]
            return "\n".join(heads) + "\n", ""
        if (category, action) == ("proxy", "tls_copy"):
            target = Path(kw["path"])
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy(kw["cert"], target / "cert.pem")
            shutil.copy(kw["key"], target / "key.pem")
        if (category, action) == ("proxy", "listening"):
            return self.listening, ""
        if (category, action) == ("services", "is_running"):
            return ("yes" if self.running else "no"), ""
        return "", ""

    def execute_with_stdin(self, category, action, stdin_data, timeout=30, **kw):
        self.calls.append((category, action, kw))
        self._fail(category, action)
        self.files[kw["file"]] = stdin_data
        return "", ""

    def actions(self, category=None):
        return [a for c, a, kw in self.calls if category is None or c == category]


@pytest.fixture
def certificate(tmp_path, monkeypatch):
    tls_dir = tmp_path / "fnas-tls"
    tls_dir.mkdir()
    (tls_dir / "cert.pem").write_text("cert")
    (tls_dir / "key.pem").write_text("key")
    (tls_dir / "ca.pem").write_text("ca")
    monkeypatch.setattr(proxy.tls, "cert_file", lambda: tls_dir / "cert.pem")
    monkeypatch.setattr(proxy.tls, "key_file", lambda: tls_dir / "key.pem")
    monkeypatch.setattr(proxy.tls, "ca_file", lambda: tls_dir / "ca.pem")
    monkeypatch.setattr(proxy.tls, "ca_fingerprint", lambda: "AA:BB")
    return tls_dir


@pytest.fixture
def engine(tmp_path, certificate):
    return FakeEngine(tmp_path)


@pytest.fixture
def artix(tmp_path, certificate):
    return FakeEngine(tmp_path, "artix", "dinit")


@pytest.fixture
def config():
    return FakeConfig()
