# Maintainer: FreedomNAS Team <dev@freedomnas.io>
pkgname=freedomnas-module-proxy
pkgver=0.1.0
pkgrel=1
pkgdesc="Reverse proxy for FreedomNAS: HTTPS for every web app"
arch=('any')
url="https://codeberg.org/FreedomNAS/freedomnas-proxy"
license=('MPL-2.0')
depends=('freedomnas-core>0.9.12' 'freedomnas-webui>0.9.12')
optdepends=("caddy: the reverse proxy itself, installed from the WebUI otherwise")
source=("git+https://codeberg.org/FreedomNAS/freedomnas-proxy.git#tag=v${pkgver}")
sha256sums=('SKIP')

package() {
    cd "${srcdir}/freedomnas-proxy"

    mkdir -p "${pkgdir}/usr/lib/freedomnas/modules"
    mkdir -p "${pkgdir}/usr/lib/freedomnas/profiles"
    cp -r modules/proxy  "${pkgdir}/usr/lib/freedomnas/modules/proxy"
    cp -r profiles/proxy "${pkgdir}/usr/lib/freedomnas/profiles/proxy"

    if [ -f LICENSE ]; then install -Dm 644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"; fi
    if [ -f README.md ]; then install -Dm 644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"; fi
}
