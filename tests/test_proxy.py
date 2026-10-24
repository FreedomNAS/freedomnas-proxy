"""Caddy's configuration, the sites and the reconcile, against the Debian profile."""

import pytest

import proxy

SITES = "/etc/caddy/freedomnas-sites"


def set_up(engine, config):
    proxy.setup(engine, config)
    engine.calls.clear()


def test_setup_takes_over_caddys_configuration(engine, config):
    engine.files["/etc/caddy/Caddyfile"] = ":80 {\n\troot * /usr/share/caddy\n}\n"
    proxy.setup(engine, config)
    main = engine.files["/etc/caddy/Caddyfile"]
    assert "import /etc/caddy/freedomnas-sites/*.caddy" in main
    assert f"tls {engine.section['tls_dir']}/cert.pem {engine.section['tls_dir']}/key.pem" in main
    assert "default_sni" in main and "admin localhost:2019" in main
    actions = engine.actions()
    assert actions.index("config_backup") < actions.index("file_write")
    assert ["validate", "enable", "restart"] == [a for a in actions if a in ("validate", "enable", "restart")]
    assert config.get_module_config("proxy") == {"enabled": True, "sites": {}}


def test_setup_installs_caddy_when_missing(engine, config):
    engine.installed = False
    proxy.setup(engine, config)
    assert [kw["package_name"] for c, a, kw in engine.calls if (c, a) == ("packages", "install")] == ["caddy", "lego"]


def test_a_site_is_written_checked_and_kept(engine, config):
    set_up(engine, config)
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    text = engine.files[f"{SITES}/site-printer.caddy"]
    assert text.splitlines()[0] == "# freedomnas-site site printer 8443 192.168.1.50:80"
    assert ":8443 {" in text and "reverse_proxy 192.168.1.50:80" in text and "import freedomnas_tls" in text
    assert engine.actions("proxy")[-2:] == ["validate", "reload"]
    assert config.get_module_config("proxy")["sites"] == {"printer": {"port": 8443, "upstream": "192.168.1.50:80"}}
    assert proxy.sites(engine) == [{"owner": "site", "name": "printer", "port": 8443, "upstream": "192.168.1.50:80"}]


def test_a_site_caddy_refuses_leaves_nothing(engine, config):
    set_up(engine, config)
    engine.failing.add(("proxy", "validate"))
    with pytest.raises(Exception):
        proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    assert f"{SITES}/site-printer.caddy" not in engine.files
    assert config.get_module_config("proxy")["sites"] == {}


def test_caddy_comes_back_with_the_previous_sites_when_a_new_one_breaks_it(engine, config):
    set_up(engine, config)
    engine.failing.add(("proxy", "reload"))
    engine.failing.add(("services", "restart"))
    with pytest.raises(Exception):
        proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    assert engine.actions().count("validate") == 2


def test_reload_falls_back_to_a_restart(engine, config):
    set_up(engine, config)
    engine.failing.add(("proxy", "reload"))
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    assert ("services", "restart", {"service_name": "caddy"}) in engine.calls


@pytest.mark.parametrize("name, port, upstream, message", [
    ("Printer", 8443, "1.2.3.4:80", "lowercase"),
    ("printer", 70000, "1.2.3.4:80", "outside"),
    ("printer", "x", "1.2.3.4:80", "not a port"),
    ("printer", 8443, "1.2.3.4", "host:port"),
    ("printer", 8443, "bad host:80", "not a host"),
    ("printer", 8443, "[zz]:80", "IPv6"),
    ("printer", 8443, "1.2.3.4:99999", "outside"),
])
def test_bad_sites_are_refused(engine, config, name, port, upstream, message):
    set_up(engine, config)
    with pytest.raises(proxy.ProxyError, match=message):
        proxy.add_site(engine, config, name, port, upstream)


def test_a_port_in_use_is_refused(engine, config):
    set_up(engine, config)
    engine.listening = "0.0.0.0:8080\n[::]:22\n"
    with pytest.raises(proxy.ProxyError, match="in use"):
        proxy.add_site(engine, config, "web", 8080, "127.0.0.1:9000")


def test_a_port_a_firewall_redirects_is_refused(engine, config):
    set_up(engine, config)
    engine.listening = "*:22\npf:8222\npf:53\n"
    with pytest.raises(proxy.ProxyError, match="in use"):
        proxy.add_site(engine, config, "web", 8222, "127.0.0.1:9000")


def test_a_port_another_site_has_is_refused_but_editing_keeps_it(engine, config):
    set_up(engine, config)
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    engine.listening = "*:8443\n"
    with pytest.raises(proxy.ProxyError, match="already the site 'printer'"):
        proxy.add_site(engine, config, "camera", 8443, "192.168.1.60:80")
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.51:80")
    assert "192.168.1.51:80" in engine.files[f"{SITES}/site-printer.caddy"]


def test_sites_of_other_modules_are_listed_and_left_alone(engine, config):
    set_up(engine, config)
    engine.files[f"{SITES}/app-jellyfin.caddy"] = proxy.site_text(engine, "app", "jellyfin", 8096, "127.0.0.1:18096")
    assert proxy.sites(engine)[0] == {"owner": "app", "name": "jellyfin", "port": 8096, "upstream": "127.0.0.1:18096"}
    with pytest.raises(proxy.ProxyError, match="No site"):
        proxy.remove_site(engine, config, "jellyfin")
    proxy.reconcile(engine, config.get_module_config("proxy"))
    assert f"{SITES}/app-jellyfin.caddy" in engine.files


def test_remove_site(engine, config):
    set_up(engine, config)
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    proxy.remove_site(engine, config, "printer")
    assert f"{SITES}/site-printer.caddy" not in engine.files
    assert config.get_module_config("proxy")["sites"] == {}


def test_reconcile_puts_everything_back(engine, config):
    set_up(engine, config)
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    engine.files.clear()
    engine.calls.clear()
    results = proxy.reconcile(engine, config.get_module_config("proxy"))
    assert [r.target_state for r in results] == ["applied"]
    assert f"{SITES}/site-printer.caddy" in engine.files and "/etc/caddy/Caddyfile" in engine.files
    assert "reload" in engine.actions()


def test_reconcile_with_nothing_changed_does_not_reload(engine, config):
    set_up(engine, config)
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    engine.calls.clear()
    assert proxy.reconcile(engine, config.get_module_config("proxy")) == []
    assert "reload" not in engine.actions() and "tls_copy" not in engine.actions()


def test_reconcile_copies_a_new_certificate(engine, config, certificate):
    set_up(engine, config)
    (certificate / "cert.pem").write_text("new cert")
    proxy.reconcile(engine, config.get_module_config("proxy"))
    assert "tls_copy" in engine.actions() and "reload" in engine.actions()


def test_reconcile_drops_sites_no_longer_configured(engine, config):
    set_up(engine, config)
    engine.files[f"{SITES}/site-old.caddy"] = proxy.site_text(engine, "site", "old", 9443, "127.0.0.1:9")
    proxy.reconcile(engine, config.get_module_config("proxy"))
    assert f"{SITES}/site-old.caddy" not in engine.files


def test_reconcile_additive_keeps_them(engine, config):
    set_up(engine, config)
    engine.files[f"{SITES}/site-old.caddy"] = proxy.site_text(engine, "site", "old", 9443, "127.0.0.1:9")
    proxy.reconcile(engine, config.get_module_config("proxy"), "additive")
    assert f"{SITES}/site-old.caddy" in engine.files


def test_reconcile_without_caddy_says_so(engine, config):
    set_up(engine, config)
    engine.installed = False
    results = proxy.reconcile(engine, config.get_module_config("proxy"))
    assert results[0].target_state == "failed" and "not installed" in results[0].error


def test_nothing_happens_before_setup(engine, config):
    assert proxy.reconcile(engine, {}) == []
    with pytest.raises(proxy.ProxyError, match="Set up"):
        proxy.add_site(engine, config, "printer", 8443, "1.2.3.4:80")


def test_status(engine, config):
    set_up(engine, config)
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    s = proxy.status(engine, config)
    assert s["installed"] and s["running"] and s["enabled"] and s["ca_fingerprint"] == "AA:BB"
    assert [x["name"] for x in s["sites"]] == ["printer"]


def test_a_copy_with_another_group_is_made_again(engine, config, monkeypatch):
    set_up(engine, config)
    monkeypatch.setitem(engine.section, "group", "nogroup-that-differs")
    monkeypatch.setattr(proxy.grp, "getgrnam", lambda name: type("G", (), {"gr_gid": 4242})())
    proxy.reconcile(engine, config.get_module_config("proxy"))
    assert "tls_copy" in engine.actions()


def test_artix_installs_caddy_with_the_service_of_its_init(artix, config):
    artix.installed = False
    proxy.setup(artix, config)
    assert [kw["package_name"] for c, a, kw in artix.calls if (c, a) == ("packages", "install")] == [
        "caddy-bin", "lego-bin", "caddy-dinit"]


def test_disable_stops_caddy_first_then_brings_the_apps_back(engine, config, monkeypatch):
    set_up(engine, config)
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    engine.calls.clear()
    seen = []
    monkeypatch.setattr(proxy, "reconcile_others",
                        lambda e, c: seen.append((e.actions(), c.get_module_config("proxy"))) or [])
    proxy.disable(engine, config)
    actions, section = seen[0]
    assert actions == ["stop", "disable"]
    assert section == {"enabled": False, "sites": {"printer": {"port": 8443, "upstream": "192.168.1.50:80"}}}
    assert proxy.reconcile(engine, config.get_module_config("proxy")) == []


def test_disable_says_which_apps_did_not_come_back(engine, config, monkeypatch):
    set_up(engine, config)
    monkeypatch.setattr(proxy, "reconcile_others", lambda e, c: ["containers.jellyfin.proxy: port 8096 in use"])
    with pytest.raises(proxy.ProxyError, match="jellyfin"):
        proxy.disable(engine, config)
    assert not proxy.enabled(config)


def test_disable_before_setup_is_refused(engine, config):
    with pytest.raises(proxy.ProxyError, match="not set up"):
        proxy.disable(engine, config)
    assert engine.calls == []


def test_setup_after_disable_turns_caddy_back_on_with_the_sites(engine, config, monkeypatch):
    set_up(engine, config)
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    monkeypatch.setattr(proxy, "reconcile_others", lambda e, c: [])
    proxy.disable(engine, config)
    engine.calls.clear()
    proxy.setup(engine, config)
    assert ["enable", "restart"] == [a for a in engine.actions("services") if a in ("enable", "restart")]
    assert config.get_module_config("proxy")["sites"] == {"printer": {"port": 8443, "upstream": "192.168.1.50:80"}}
    assert f"{SITES}/site-printer.caddy" in engine.files


def test_names_add_one_https_site_per_name_and_the_redirect(engine, config, monkeypatch):
    monkeypatch.setattr(proxy, "_hostname", lambda: "nas")
    set_up(engine, config)
    proxy.add_site(engine, config, "printer", 8443, "192.168.1.50:80")
    assert "import /etc/caddy/freedomnas-names/*.caddy printer 192.168.1.50:80" in engine.files[f"{SITES}/site-printer.caddy"]
    status = proxy.set_names(engine, config, True)
    assert status["enabled"] and status["domain"] == "nas.home.arpa"
    names = engine.files["/etc/caddy/freedomnas-names/name.caddy"]
    assert "{args.0}.nas.home.arpa {" in names and "reverse_proxy {args.1}" in names
    assert "http://*.nas.home.arpa {" in engine.files[f"{SITES}/names.caddy"]
    assert engine.actions("proxy")[-2:] == ["validate", "reload"]
    assert config.get_module_config("proxy")["names"] is True
    assert proxy.reconcile(engine, config.get_module_config("proxy"), "additive") == []
    assert engine.actions("proxy").count("names_configure") >= 1
    engine.calls.clear()
    proxy.set_names(engine, config, False)
    assert "/etc/caddy/freedomnas-names/name.caddy" not in engine.files
    assert f"{SITES}/names.caddy" not in engine.files
    assert engine.actions("proxy").count("names_unconfigure") == 1
    engine.calls.clear()
    proxy.reconcile(engine, config.get_module_config("proxy"), "additive")
    assert "names_unconfigure" not in engine.actions("proxy") and "names_configure" not in engine.actions("proxy")


def test_names_need_443_and_80_free(engine, config):
    set_up(engine, config)
    engine.listening = "*:22\n0.0.0.0:80\n"
    with pytest.raises(proxy.ProxyError, match="Port 80"):
        proxy.set_names(engine, config, True)
    assert "/etc/caddy/freedomnas-names/name.caddy" not in engine.files


def test_names_that_caddy_refuses_change_nothing(engine, config):
    set_up(engine, config)
    engine.failing.add(("proxy", "validate"))
    with pytest.raises(Exception):
        proxy.set_names(engine, config, True)
    assert "/etc/caddy/freedomnas-names/name.caddy" not in engine.files
    assert not config.get_module_config("proxy").get("names")


def test_a_site_cannot_take_a_name_or_the_ports_names_use(engine, config):
    set_up(engine, config)
    engine.files[f"{SITES}/app-jellyfin.caddy"] = "# freedomnas-site app jellyfin 8096 127.0.0.1:20001\n"
    with pytest.raises(proxy.ProxyError, match="already the site of a app"):
        proxy.add_site(engine, config, "jellyfin", 9443, "127.0.0.1:9")
    proxy.set_names(engine, config, True)
    with pytest.raises(proxy.ProxyError, match="serves the sites by name"):
        proxy.add_site(engine, config, "web", 443, "127.0.0.1:9")


def test_every_edition_formats_the_name_templates(engine):
    import json
    from conftest import REPO
    for f in sorted((REPO / "profiles/proxy").glob("*.json")):
        d = json.loads(f.read_text())["proxy"]
        site = d["site_template"].format(owner="app", name="web", port=8443, upstream="127.0.0.1:9")
        assert site.rstrip().endswith(f"import {d['names_dir']}/*.caddy web 127.0.0.1:9"), f.name
        assert ".nas.home.arpa {" in d["names_template"].format(domain="nas.home.arpa", tls="import freedomnas_tls"), f.name
        assert "http://*.nas.home.arpa" in d["redirect_template"].format(domain="nas.home.arpa", tls="import freedomnas_tls"), f.name


def test_gentoo_accepts_the_caddy_that_works_before_installing_it(tmp_path, certificate, config):
    from conftest import FakeEngine
    gentoo = FakeEngine(tmp_path, "gentoo", "openrc")
    gentoo.installed = False
    proxy.setup(gentoo, config)
    actions = gentoo.actions()
    assert actions.index("install_prepare") < actions.index("install")
    assert [kw["package_name"] for c, a, kw in gentoo.calls if a == "install"] == [">=www-servers/caddy-2.11", "app-crypt/lego"]


CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
ACME = {"domain": "nas.example.org", "email": "me@example.org", "provider": "desec",
        "env": "DESEC_TOKEN=abc123\n", "server": "staging"}


@pytest.fixture
def lego(engine, monkeypatch):
    """lego and the copy for Caddy, as files in the fake engine."""
    real = engine.execute
    issued = f"{engine.section['acme_dir']}/certificates/nas.example.org.crt"

    def execute(category, action, timeout=30, check=True, **kw):
        result = real(category, action, timeout=timeout, check=check, **kw)
        if (category, action) == ("proxy", "acme_obtain"):
            engine.files[issued] = CERT
        if (category, action) == ("proxy", "acme_install"):
            engine.files[f"{kw['tls_dir']}/cert.pem"] = engine.files[issued]
        if (category, action) == ("proxy", "acme_remove"):
            engine.files.pop(kw["cron"], None)
            engine.files.pop(f"{kw['tls_dir']}/cert.pem", None)
        return result

    monkeypatch.setattr(engine, "execute", execute)
    monkeypatch.setattr(proxy, "acme_status", lambda e, c: (c.get_module_config("proxy").get("acme") or {}).get("domain"))
    return engine


def names_on(engine, config):
    set_up(engine, config)
    proxy.set_names(engine, config, True)


def test_an_own_domain_gets_its_certificate_and_names_every_site(lego, config):
    names_on(lego, config)
    proxy.set_acme(lego, config, dict(ACME))
    s = lego.section
    obtain = next(kw for c, a, kw in lego.calls if a == "acme_obtain")
    assert obtain["server"] == proxy.SERVERS["staging"] and obtain["provider"] == "desec"
    assert obtain["domain"] == "nas.example.org" and obtain["env_file"] == f"{s['acme_dir']}/env"
    assert lego.files[f"{s['acme_dir']}/env"] == "DESEC_TOKEN='abc123'\n"
    secret_writes = [kw["file"] for c, a, kw in lego.calls if a == "acme_secret_write"]
    assert f"{s['acme_dir']}/env" in secret_writes and s["acme_cron_file"] in secret_writes
    cron = lego.files[s["acme_cron_file"]]
    assert "--dns desec" in cron and "renew --days 30" in cron and f"sh {s['acme_dir']}/deploy-hook.sh" in cron
    assert "DESEC_TOKEN" not in cron
    own = lego.files["/etc/caddy/freedomnas-names/name-acme.caddy"]
    assert "{args.0}.nas.example.org {" in own and f"tls {s['acme_tls_dir']}/cert.pem {s['acme_tls_dir']}/key.pem" in own
    assert "http://*.nas.example.org" in lego.files[f"{SITES}/names-acme.caddy"]
    assert config.get_module_config("proxy")["acme"]["env"] == {"DESEC_TOKEN": "abc123"}


def test_a_refused_certificate_changes_nothing_in_caddy(engine, config):
    names_on(engine, config)
    engine.failing.add(("proxy", "acme_obtain"))
    with pytest.raises(proxy.ProxyError, match="did not sign"):
        proxy.set_acme(engine, config, dict(ACME))
    assert "/etc/caddy/freedomnas-names/name-acme.caddy" not in engine.files
    assert "acme" not in config.get_module_config("proxy")
    assert "acme_remove" in engine.actions()


@pytest.mark.parametrize("change, message", [
    ({"domain": "nas.home.arpa"}, "only public"),
    ({"domain": "not a domain"}, "not a domain"),
    ({"email": "nope"}, "not an email"),
    ({"provider": "Bad Provider"}, "DNS provider"),
    ({"server": "http://insecure/dir"}, "certificate authority"),
    ({"env": "TOKEN=has space"}, "spaces"),
    ({"env": "lower=x"}, "variable name"),
    ({"env": ""}, "credentials"),
])
def test_bad_own_domain_settings_are_refused(change, message):
    with pytest.raises(proxy.ProxyError, match=message):
        proxy.check_acme({**ACME, **change})


def test_credentials_left_out_keep_their_saved_values():
    saved = proxy.check_acme(dict(ACME))
    again = proxy.check_acme({**ACME, "env": ""}, saved)
    assert again["env"] == {"DESEC_TOKEN": "abc123"}


def test_an_own_domain_needs_names(engine, config):
    set_up(engine, config)
    with pytest.raises(proxy.ProxyError, match="by name first"):
        proxy.set_acme(engine, config, dict(ACME))


def test_a_reinstall_gets_the_certificate_again_at_reconcile(lego, config):
    names_on(lego, config)
    proxy.set_acme(lego, config, dict(ACME))
    for f in [k for k in lego.files if "freedomnas-acme" in k or "certificates" in k or k.endswith("-acme.caddy")]:
        del lego.files[f]
    lego.calls.clear()
    assert proxy.reconcile(lego, config.get_module_config("proxy"), "additive") == [proxy._result("config")]
    assert "acme_obtain" in lego.actions() and "/etc/caddy/freedomnas-names/name-acme.caddy" in lego.files


def test_removing_the_own_domain_keeps_the_local_names(lego, config):
    names_on(lego, config)
    proxy.set_acme(lego, config, dict(ACME))
    proxy.remove_acme(lego, config)
    assert "/etc/caddy/freedomnas-names/name-acme.caddy" not in lego.files
    assert "/etc/caddy/freedomnas-names/name.caddy" in lego.files
    assert lego.section["acme_cron_file"] not in lego.files
    assert "acme" not in config.get_module_config("proxy")


def test_lego_is_installed_when_missing(lego, config):
    names_on(lego, config)
    lego.failing.add(("proxy", "acme_installed_check"))
    lego.calls.clear()
    proxy.set_acme(lego, config, dict(ACME))
    assert "lego" in [kw["package_name"] for c, a, kw in lego.calls if (c, a) == ("packages", "install")]
