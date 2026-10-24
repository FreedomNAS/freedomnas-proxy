"""
Every command of every proxy profile formats with the real engine, and the
templates with the values the module gives them.
"""

import json
import shutil

import pytest

from conftest import PROFILES, REPO
from freedomnas.engine import FreedomNASEngine

VALUES = {"group": "caddy", "path": "/p", "cert": "/c.pem", "key": "/k.pem", "file": "/f", "dir": "/d"}
EDITIONS = ["arch", "artix", "debian", "freebsd", "gentoo", "void"]
NOT_COMMANDS = {"packages", "init_packages", "services", "group", "config_file", "tls_dir", "sites_dir", "names_dir",
                "main_template", "site_template", "names_template", "redirect_template"}


def engine_for(profile_dir, edition):
    FreedomNASEngine._instance = None
    FreedomNASEngine._initialized = False
    try:
        return FreedomNASEngine(profile_dir=str(profile_dir), os_override=edition)
    finally:
        FreedomNASEngine._instance = None
        FreedomNASEngine._initialized = False


@pytest.fixture(scope="module")
def profile_dir(tmp_path_factory):
    target = tmp_path_factory.mktemp("profiles") / "profiles"
    shutil.copytree(PROFILES, target)
    shutil.copytree(REPO / "profiles" / "proxy", target / "proxy")
    return target


def section(edition):
    return json.loads((REPO / f"profiles/proxy/proxy-{edition}.json").read_text())["proxy"]


@pytest.mark.parametrize("edition", EDITIONS)
def test_every_command_formats(profile_dir, edition):
    engine = engine_for(profile_dir, edition)
    commands = [k for k in section(edition) if not k.startswith("_") and k not in NOT_COMMANDS]
    assert "reload" in commands and "site_write" in commands
    for action in commands:
        engine.get_command("proxy", action, **VALUES)


@pytest.mark.parametrize("edition", EDITIONS)
def test_templates_format(edition):
    s = section(edition)
    main = s["main_template"].format(hostname="nas", tls_dir=s["tls_dir"], sites_dir=s["sites_dir"])
    assert main.startswith("{\n") and f"import {s['sites_dir']}/*.caddy" in main
    site = s["site_template"].format(owner="site", name="web", port=8443, upstream="127.0.0.1:9")
    assert site.startswith("# freedomnas-site site web 8443 127.0.0.1:9\n")


def test_every_edition_has_the_same_keys():
    keys = set(section("debian"))
    for edition in EDITIONS:
        assert set(section(edition)) == keys, edition
