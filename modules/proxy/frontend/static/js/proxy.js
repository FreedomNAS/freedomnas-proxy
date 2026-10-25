'use strict';

let _status = null;
let _poll = null;

const OWNERS = { site: 'Added here', app: 'Containers app' };
const JOBS = {
  setup: { running: 'Setting up Caddy…', failed: 'Set up failed', done: 'Set up finished.' },
  disable: { running: 'Turning the reverse proxy off…', failed: 'Turning off failed', done: 'The reverse proxy is off.' },
  certificate: { running: 'Asking Let\'s Encrypt for the certificate… (a minute or two)', failed: 'No certificate',
                 done: 'The certificate is there.' },
};

document.addEventListener('DOMContentLoaded', () => {
  if (!initShared()) return;
  document.getElementById('btn-setup').addEventListener('click', setupProxy);
  document.getElementById('btn-disable').addEventListener('click', disableProxy);
  document.getElementById('btn-names').addEventListener('click', toggleNames);
  document.getElementById('form-acme').addEventListener('submit', saveAcme);
  document.getElementById('form-pihole').addEventListener('submit', savePihole);
  document.getElementById('btn-pihole-forget').addEventListener('click', forgetPihole);
  document.getElementById('btn-acme-remove').addEventListener('click', removeAcme);
  document.getElementById('f-acme-server').addEventListener('change', () => {
    document.getElementById('acme-custom').hidden = document.getElementById('f-acme-server').value !== 'custom';
  });
  document.getElementById('form-site').addEventListener('submit', addSite);
  document.getElementById('sites-body').addEventListener('click', onSiteAction);
  loadStatus();
});

async function loadStatus() {
  try {
    _status = await apiJSON('GET', '/api/proxy/status');
  } catch (e) {
    document.getElementById('proxy-body').innerHTML =
      `<tr><td colspan="2" class="text-muted">Could not read the reverse proxy: ${esc(e.message)}</td></tr>`;
    return;
  }
  render();
  const busy = (_status.job || {}).state === 'running';
  if (busy && !_poll) _poll = setInterval(loadStatus, 3000);
  if (!busy && _poll) { clearInterval(_poll); _poll = null; }
}

function render() {
  const s = _status;
  let state;
  if (!s.installed) state = '<span class="badge badge-muted">Not installed</span>';
  else if (!s.enabled) state = '<span class="badge badge-warn">Installed, off</span>';
  else if (s.running) state = '<span class="badge badge-ok">Running</span>';
  else state = '<span class="badge badge-err">Not running</span>';
  document.getElementById('proxy-body').innerHTML = `
    <tr><td class="text-muted" style="padding:5px 0;width:140px">State</td><td>${state}</td></tr>
    <tr><td class="text-muted" style="padding:5px 0">Certificate authority</td>
        <td class="text-sm" style="word-break:break-all">${esc(s.ca_fingerprint || '—')}</td></tr>`;
  const job = s.job || {};
  const certificate = job.kind === 'certificate';
  const line = document.getElementById(certificate ? 'acme-job-line' : 'job-line');
  document.getElementById(certificate ? 'job-line' : 'acme-job-line').hidden = true;
  line.hidden = !job.kind || job.state === 'idle' || (certificate && !s.acme && job.state === 'done');
  const words = JOBS[job.kind] || JOBS.setup;
  if (job.state === 'running') line.textContent = words.running;
  else if (job.state === 'failed') line.textContent = `${words.failed}: ${job.error}`;
  else if (job.state === 'done') line.textContent = words.done;
  document.getElementById('setup-section').hidden = s.enabled || job.state === 'running';
  document.getElementById('btn-setup').disabled = job.state === 'running';
  document.getElementById('disable-row').hidden = !s.enabled || job.state === 'running';
  document.getElementById('sites-section').hidden = !s.enabled;
  renderNames(s);
  renderAcme(s);
  renderSites(s.sites || []);
}

let _acmeFilled = false;

function renderAcme(s) {
  const names = s.names || {};
  document.getElementById('acme-section').hidden = !(s.enabled && names.enabled);
  const acme = s.acme;
  const busy = (s.job || {}).state === 'running';
  document.getElementById('acme-state').hidden = !acme;
  document.getElementById('btn-acme-remove').hidden = !acme;
  document.getElementById('btn-acme').textContent = acme ? 'Save and check the certificate' : 'Get the certificate';
  document.getElementById('btn-acme').disabled = busy;
  if (!acme) return;
  const cas = { production: "Let's Encrypt", staging: "Let's Encrypt staging" };
  const expires = acme.expires ? new Date(acme.expires).toLocaleDateString() : '—';
  document.getElementById('acme-body').innerHTML = `
    <tr><td class="text-muted" style="padding:5px 0;width:140px">Domain</td><td class="font-mono text-sm">${esc(acme.domain)}${
      names.own_domain ? ' <span class="badge badge-ok">In use</span>' : ' <span class="badge badge-warn">No certificate yet</span>'}</td></tr>
    <tr><td class="text-muted" style="padding:5px 0">Certificate</td><td>${esc(cas[acme.server] || acme.server)}, valid until ${esc(expires)}</td></tr>
    <tr><td class="text-muted" style="padding:5px 0">DNS provider</td><td class="font-mono text-sm">${esc(acme.provider)} (${esc((acme.credentials || []).join(', '))})</td></tr>`;
  if (!_acmeFilled) {
    _acmeFilled = true;
    document.getElementById('f-acme-domain').value = acme.domain;
    document.getElementById('f-acme-email').value = acme.email;
    document.getElementById('f-acme-provider').value = acme.provider;
    const known = acme.server === 'production' || acme.server === 'staging';
    document.getElementById('f-acme-server').value = known ? acme.server : 'custom';
    document.getElementById('f-acme-url').value = known ? '' : acme.server;
    document.getElementById('acme-custom').hidden = known;
  }
}

async function saveAcme(e) {
  e.preventDefault();
  const choice = document.getElementById('f-acme-server').value;
  const body = {
    domain: document.getElementById('f-acme-domain').value.trim(),
    email: document.getElementById('f-acme-email').value.trim(),
    provider: document.getElementById('f-acme-provider').value.trim(),
    server: choice === 'custom' ? document.getElementById('f-acme-url').value.trim() : choice,
    env: document.getElementById('f-acme-env').value,
  };
  const btn = document.getElementById('btn-acme');
  setLoading(btn, true, 'Starting…');
  try {
    await apiJSON('POST', '/api/proxy/acme', body);
    document.getElementById('f-acme-env').value = '';
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  setLoading(btn, false);
  loadStatus();
}

async function removeAcme() {
  if (!await confirm('Stop using this domain',
    'The sites stop answering under your own domain; the local names stay. The certificate is no longer renewed.',
    'Stop using it')) return;
  try {
    await apiJSON('DELETE', '/api/proxy/acme');
    _acmeFilled = false;
    toast('The own domain is no longer used.', 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  loadStatus();
}

function renderNames(s) {
  const names = s.names || {};
  const section = document.getElementById('names-section');
  section.hidden = !s.enabled;
  if (!s.enabled) return;
  const first = (s.sites || [])[0];
  document.getElementById('names-example').textContent = `https://${first ? first.name : 'jellyfin'}.${names.domain}/`;
  const state = names.enabled
    ? '<span class="badge badge-ok">On</span>' : '<span class="badge badge-muted">Off</span>';
  document.getElementById('names-body').innerHTML = `
    <tr><td class="text-muted" style="padding:5px 0;width:140px">State</td><td>${state}</td></tr>
    <tr><td class="text-muted" style="padding:5px 0">Domain</td><td class="font-mono text-sm">${esc(names.domain || '')}</td></tr>`;
  const btn = document.getElementById('btn-names');
  btn.textContent = names.enabled ? 'Stop serving by name' : 'Serve the sites by name';
  btn.className = names.enabled ? 'btn btn-danger btn-sm' : 'btn btn-primary btn-sm';
  const dns = document.getElementById('names-dns');
  dns.hidden = !names.enabled;
  dns.innerHTML = `Your devices find these names through your DNS: point every name under
    <span class="font-mono">${esc(names.domain || '')}</span> (a wildcard record
    <span class="font-mono">*.${esc(names.domain || '')}</span>) at this NAS's address on your network${
    names.address ? `, <span class="font-mono">${esc(names.address)}</span>` : ''}. Pi-hole from the Containers
    catalogue does it by itself, for the devices that use it as DNS server; a Pi-hole of your network can be kept up
    to date below; otherwise add that record in your router.`;
  renderPihole(names);
}

let _piholeFilled = false;

function renderPihole(names) {
  document.getElementById('pihole-box').hidden = !names.enabled;
  const dns = names.dns;
  const state = document.getElementById('pihole-state');
  document.getElementById('btn-pihole-forget').hidden = !dns;
  document.getElementById('btn-pihole').textContent = dns ? 'Save' : 'Keep the names there';
  state.hidden = !dns;
  if (!dns) return;
  if (!_piholeFilled) {
    _piholeFilled = true;
    document.getElementById('f-pihole-url').value = dns.url;
  }
  if (dns.error) {
    state.innerHTML = `<span class="badge badge-err">Not up to date</span> ${esc(dns.error)}`;
  } else if (dns.synced) {
    state.innerHTML = `<span class="badge badge-ok">Up to date</span> ${(dns.records || []).map(r => `<span class="font-mono">${esc(r)}</span>`).join('<br>') || 'no records (no sites)'}`;
  } else {
    state.innerHTML = '<span class="badge badge-muted">Kept at the next change</span>';
  }
}

async function savePihole(e) {
  e.preventDefault();
  const btn = document.getElementById('btn-pihole');
  setLoading(btn, true, 'Checking…');
  try {
    await apiJSON('POST', '/api/proxy/dns', {
      url: document.getElementById('f-pihole-url').value.trim(),
      password: document.getElementById('f-pihole-password').value,
    });
    document.getElementById('f-pihole-password').value = '';
    toast('The names are in your Pi-hole.', 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  setLoading(btn, false);
  loadStatus();
}

async function forgetPihole() {
  if (!await confirm('Forget the Pi-hole', 'Its records for this NAS are taken out and no longer kept.', 'Forget')) return;
  try {
    await apiJSON('DELETE', '/api/proxy/dns');
    _piholeFilled = false;
    document.getElementById('f-pihole-url').value = '';
    toast('Pi-hole forgotten.', 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  loadStatus();
}

async function toggleNames() {
  const on = !((_status.names || {}).enabled);
  const btn = document.getElementById('btn-names');
  setLoading(btn, true, on ? 'Turning on…' : 'Turning off…');
  try {
    await apiJSON('POST', '/api/proxy/names', { enabled: on });
    toast(on ? 'The sites now answer by name too.' : 'The sites answer on their ports only.', 'ok');
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
  }
  setLoading(btn, false);
  loadStatus();
}

function renderSites(sites) {
  const body = document.getElementById('sites-body');
  if (!sites.length) {
    body.innerHTML = '<tr><td colspan="5" class="text-muted">No sites yet.</td></tr>';
    return;
  }
  const host = location.hostname;
  const names = (_status && _status.names) || {};
  body.innerHTML = sites.map(site => `
    <tr>
      <td><a href="https://${esc(host)}:${site.port}/" target="_blank" rel="noopener">${site.port}</a></td>
      <td>${names.enabled
        ? `<a href="https://${esc(site.name)}.${esc(names.domain)}/" target="_blank" rel="noopener">${esc(site.name)}</a>`
        : esc(site.name)}</td>
      <td class="font-mono text-sm">${esc(site.upstream)}</td>
      <td class="text-muted">${esc(OWNERS[site.owner] || site.owner)}</td>
      <td style="text-align:right">${site.owner === 'site'
        ? `<button class="btn btn-danger btn-sm advanced-only" data-remove="${esc(site.name)}">Remove</button>` : ''}</td>
    </tr>`).join('');
}

async function setupProxy() {
  const btn = document.getElementById('btn-setup');
  setLoading(btn, true, 'Setting up…');
  try {
    await apiJSON('POST', '/api/proxy/setup');
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
  }
  setLoading(btn, false);
  loadStatus();
}

async function disableProxy() {
  if (!await confirm('Turn off the reverse proxy',
    'Caddy stops and stays off at boot. The apps behind it go back to their own ports over plain HTTP, ' +
    'and the sites added here stop answering until you set the proxy up again.', 'Turn off')) return;
  const btn = document.getElementById('btn-disable');
  setLoading(btn, true, 'Turning off…');
  try {
    await apiJSON('POST', '/api/proxy/disable');
  } catch (e) {
    toast(`Error: ${e.message}`, 'err');
  }
  setLoading(btn, false);
  loadStatus();
}

async function addSite(e) {
  e.preventDefault();
  const btn = document.getElementById('btn-site-add');
  const body = {
    name: document.getElementById('f-site-name').value.trim(),
    port: Number(document.getElementById('f-site-port').value),
    upstream: document.getElementById('f-site-upstream').value.trim(),
  };
  setLoading(btn, true, 'Adding…');
  try {
    await apiJSON('POST', '/api/proxy/sites', body);
    toast(`Site ${body.name} added on port ${body.port}.`, 'ok');
    document.getElementById('form-site').reset();
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  }
  setLoading(btn, false);
  loadStatus();
}

async function onSiteAction(e) {
  const btn = e.target.closest('[data-remove]');
  if (!btn) return;
  const name = btn.dataset.remove;
  if (!await confirm('Remove site', `Remove the site ${name}? Its port stops answering.`, 'Remove')) return;
  setLoading(btn, true, '…');
  try {
    await apiJSON('DELETE', `/api/proxy/sites/${encodeURIComponent(name)}`);
    toast(`Site ${name} removed.`, 'ok');
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
    setLoading(btn, false);
  }
  loadStatus();
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
