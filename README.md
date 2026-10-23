# FreedomNAS Reverse Proxy

FreedomNAS community module that puts [Caddy](https://caddyserver.com) in
front of the NAS's web apps and serves each of them over HTTPS, on its own
port, with FreedomNAS's own certificate (the one its certificate authority
signs for the NAS's hostname and addresses). Install that authority once on
a device and no app warns any more.

Package: `freedomnas-module-proxy`. Editions: Debian, Arch, Artix, Void,
Gentoo, FreeBSD. Caddy comes from the edition's own packages; Artix packages
none, so it comes from FreedomNAS's `freedomnas-arch` repository: `caddy-bin`
(upstream's static build) and the service of the running init
(`caddy-openrc`, `caddy-dinit`, `caddy-runit`; profile `init_packages`).

## How it works

- **Set up** (Reverse proxy page, Advanced level) installs Caddy from the
  edition's own packages if needed, takes over Caddy's configuration file (the
  one it came with is kept as `<file>.pre-freedomnas`), copies FreedomNAS's
  certificate where the Caddy user can read it and starts Caddy at boot.
- A **site** listens on a port of the NAS, for every name and address, and
  passes the requests on to a service (`host:port`) over plain HTTP. Sites
  added by hand on the page are kept in the configuration
  (`modules.proxy.sites`); other modules write their own: the Containers
  module puts its catalogue apps behind the proxy, the app moving to a port
  only the proxy reaches.
- At every boot the reconciler writes Caddy's configuration and the sites
  again, copies a certificate FreedomNAS issued again (new address or
  hostname) and reloads Caddy.

Configuration (`modules.proxy`):

```yaml
modules:
  proxy:
    enabled: true
    sites:
      printer: {port: 8443, upstream: 192.168.1.50:80}
```

## Files Caddy reads

| Profile value | Debian, Arch, Void, Gentoo | FreeBSD |
|---|---|---|
| `config_file` | `/etc/caddy/Caddyfile` | `/usr/local/etc/caddy/Caddyfile` |
| `tls_dir` | `/etc/caddy/freedomnas-tls` | `/usr/local/etc/caddy/freedomnas-tls` |
| `sites_dir` | `/etc/caddy/freedomnas-sites` | `/usr/local/etc/caddy/freedomnas-sites` |

`config_file` comes from the profile's `main_template`: Caddy's admin
endpoint on `localhost:2019`, no HTTP→HTTPS redirect on port 80,
`default_sni` the hostname (a browser opening the NAS by address sends no
name), the snippet `freedomnas_tls` with the certificate, and
`import <sites_dir>/*.caddy`.

Each site is `<owner>-<name>.caddy` from `site_template`; its first line,
`# freedomnas-site <owner> <name> <port> <upstream>`, is how the module lists
every site whichever module wrote it. This module owns `site-*`.

On FreeBSD Caddy runs as `www` (`service_configure`): the port's rc script
starts it with `su -m <caddy_user>`, which a locked root refuses.

## For another module

Write a site with this module's profile, then check and reload:

```python
proxy = engine.get_profile_section("proxy")
text = proxy["site_template"].format(owner="app", name=name, port=port, upstream=upstream)
engine.execute_with_stdin("proxy", "site_write", text, file=f"{proxy['sites_dir']}/app-{name}.caddy")
engine.execute("proxy", "validate", file=proxy["config_file"])
engine.execute("proxy", "reload", file=proxy["config_file"])
```

## Profile commands (`profiles/proxy/proxy-<os>.json`)

| Command | What |
|---|---|
| `installed_check` | Caddy is there |
| `service_configure` | how the init system runs Caddy (FreeBSD: as `www`) |
| `dir_prepare` | `tls_dir` / `sites_dir`, root-owned, group `{group}` (the group Caddy runs as) |
| `tls_copy` | FreedomNAS's certificate and key into `tls_dir`, mode 640 |
| `file_read`, `file_write`, `site_write`, `file_remove` | the configuration and the sites (written to a temporary file, then moved) |
| `config_backup` | the distribution's configuration, once, as `<file>.pre-freedomnas` |
| `sites_list` | the first line of every site |
| `validate`, `reload` | Caddy checks the configuration, then loads it through its admin endpoint |
| `listening` | the TCP ports something listens on, so a site never takes one in use |

Values: `packages`, `services`, `group`, `config_file`, `tls_dir`,
`sites_dir`, `main_template`, `site_template` (str.format strings, literal
braces doubled).

## Tests

```
python3 -m pytest
```

They need the `freedomnas-core` and `freedomnas-profiles` checkouts beside
this one.
