'use strict';

let _status = null;
let _poll = null;

const OWNERS = { site: 'Added here', app: 'Containers app' };
const JOBS = {
  setup: { running: 'Setting up Caddy…', failed: 'Set up failed', done: 'Set up finished.' },
  disable: { running: 'Turning the reverse proxy off…', failed: 'Turning off failed', done: 'The reverse proxy is off.' },
};

document.addEventListener('DOMContentLoaded', () => {
  if (!initShared()) return;
  document.getElementById('btn-setup').addEventListener('click', setupProxy);
  document.getElementById('btn-disable').addEventListener('click', disableProxy);
  document.getElementById('btn-names').addEventListener('click', toggleNames);
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
  const line = document.getElementById('job-line');
  line.hidden = !job.kind || job.state === 'idle';
  const words = JOBS[job.kind] || JOBS.setup;
  if (job.state === 'running') line.textContent = words.running;
  else if (job.state === 'failed') line.textContent = `${words.failed}: ${job.error}`;
  else if (job.state === 'done') line.textContent = words.done;
  document.getElementById('setup-section').hidden = s.enabled || job.state === 'running';
  document.getElementById('btn-setup').disabled = job.state === 'running';
  document.getElementById('disable-row').hidden = !s.enabled || job.state === 'running';
  document.getElementById('sites-section').hidden = !s.enabled;
  renderNames(s);
  renderSites(s.sites || []);
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
    names.address ? `, <span class="font-mono">${esc(names.address)}</span>` : ''}, in your router or in Pi-hole.`;
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
