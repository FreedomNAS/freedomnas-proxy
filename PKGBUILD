# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgbase=freedomnas-module-proxy
pkgname=(freedomnas-module-proxy freedomnas-module-proxy-openrc freedomnas-module-proxy-dinit freedomnas-module-proxy-runit)
pkgver=0.1.0
pkgrel=1
pkgdesc="Reverse proxy for FreedomNAS: HTTPS for every web app"
arch=('any')
url="https://codeberg.org/FreedomNAS/freedomnas-proxy"
license=('MPL-2.0')
depends=('freedomnas-core>0.9.12' 'freedomnas-webui>0.9.12' 'caddy' 'lego')
source=("git+https://codeberg.org/FreedomNAS/freedomnas-proxy.git#tag=v${pkgver}")
sha256sums=('SKIP')

_install() {
    cd "${srcdir}/freedomnas-proxy"

    mkdir -p "${pkgdir}/usr/lib/freedomnas/modules"
    mkdir -p "${pkgdir}/usr/lib/freedomnas/profiles"
    cp -r modules/proxy  "${pkgdir}/usr/lib/freedomnas/modules/proxy"
    cp -r profiles/proxy "${pkgdir}/usr/lib/freedomnas/profiles/proxy"

    if [ -f LICENSE ]; then install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"; fi
    if [ -f README.md ]; then install -Dm 644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"; fi
}

package_freedomnas-module-proxy() {
    _install
}

package_freedomnas-module-proxy-openrc() {
    pkgdesc+=" (openrc services, Artix)"
    depends+=('caddy-openrc')
    provides=("freedomnas-module-proxy=${pkgver}")
    conflicts=(freedomnas-module-proxy)
    _install
}

package_freedomnas-module-proxy-dinit() {
    pkgdesc+=" (dinit services, Artix)"
    depends+=('caddy-dinit')
    provides=("freedomnas-module-proxy=${pkgver}")
    conflicts=(freedomnas-module-proxy)
    _install
}

package_freedomnas-module-proxy-runit() {
    pkgdesc+=" (runit services, Artix)"
    depends+=('caddy-runit')
    provides=("freedomnas-module-proxy=${pkgver}")
    conflicts=(freedomnas-module-proxy)
    _install
}
