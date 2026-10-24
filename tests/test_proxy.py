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
    assert [kw["package_name"] for c, a, kw in engine.calls if (c, a) == ("packages", "install")] == ["caddy"]


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
        "caddy-bin", "caddy-dinit"]


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
