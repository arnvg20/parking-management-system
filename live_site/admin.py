from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

_COOKIE = "admin_sid"

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Login</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  background:#f0f2f5;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:#fff;border-radius:10px;box-shadow:0 2px 20px rgba(0,0,0,.1);padding:44px;width:370px}}
h1{{font-size:1.35rem;color:#1a1a2e;margin-bottom:6px}}
.sub{{color:#999;font-size:.875rem;margin-bottom:28px}}
label{{display:block;font-size:.875rem;font-weight:500;color:#444;margin-bottom:6px}}
input{{width:100%;padding:10px 13px;border:1px solid #ddd;border-radius:6px;font-size:.95rem;outline:none}}
input:focus{{border-color:#4f6ef7;box-shadow:0 0 0 3px rgba(79,110,247,.15)}}
.field{{margin-bottom:18px}}
button{{width:100%;padding:11px;background:#4f6ef7;color:#fff;border:none;border-radius:6px;
  font-size:1rem;cursor:pointer;font-weight:500;margin-top:4px}}
button:hover{{background:#3a5be0}}
.err{{background:#fff0f0;color:#c0392b;border:1px solid #f5c6cb;border-radius:6px;
  padding:10px 13px;font-size:.875rem;margin-bottom:18px}}
</style>
</head>
<body>
<div class="card">
  <h1>&#x1F17F; Admin Panel</h1>
  <p class="sub">Parking Management System</p>
  {error_block}
  <form method="POST" action="/admin/login">
    <div class="field">
      <label for="u">Username</label>
      <input type="text" id="u" name="username" autocomplete="username" required autofocus>
    </div>
    <div class="field">
      <label for="p">Password</label>
      <input type="password" id="p" name="password" autocomplete="current-password" required>
    </div>
    <button type="submit">Sign In</button>
  </form>
</div>
</body>
</html>"""

_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin &#x2013; Parking System</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f0f2f5;color:#1a1a2e}
.topbar{background:#1a1a2e;color:#fff;padding:0 24px;display:flex;align-items:center;height:52px;gap:14px}
.topbar h1{font-size:1.05rem;font-weight:600;flex:1}
.topbar a{color:#8899cc;font-size:.85rem;text-decoration:none;padding:5px 10px;border-radius:5px}
.topbar a:hover{background:rgba(255,255,255,.1);color:#fff}
.tabs{background:#fff;border-bottom:2px solid #e8e8e8;padding:0 24px;display:flex}
.tab{padding:13px 18px;cursor:pointer;font-size:.9rem;color:#777;border-bottom:2px solid transparent;
  margin-bottom:-2px;user-select:none;transition:color .15s}
.tab:hover{color:#1a1a2e}
.tab.active{color:#4f6ef7;border-bottom-color:#4f6ef7;font-weight:500}
.content{padding:24px;max-width:1260px;margin:0 auto}
.panel{display:none}.panel.active{display:block}
.toolbar{display:flex;align-items:center;margin-bottom:14px;gap:8px}
.toolbar h2{font-size:1rem;font-weight:600;flex:1;color:#333}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
  overflow:hidden;box-shadow:0 1px 5px rgba(0,0,0,.07)}
th{background:#f8f9fa;text-align:left;padding:11px 14px;font-size:.78rem;
  text-transform:uppercase;letter-spacing:.05em;color:#999;border-bottom:1px solid #eee}
td{padding:10px 14px;font-size:.875rem;border-bottom:1px solid #f4f4f4;vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafbff}
.badge{display:inline-block;padding:3px 9px;border-radius:12px;font-size:.73rem;font-weight:600}
.b-green{background:#e6f4ea;color:#1e7e34}
.b-red{background:#fdecea;color:#c0392b}
.b-gray{background:#f0f0f0;color:#666}
.b-yellow{background:#fff8e1;color:#856404}
.b-blue{background:#e8f0fe;color:#1a73e8}
.btn{display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border-radius:5px;
  font-size:.8rem;cursor:pointer;border:none;font-weight:500;transition:background .12s;white-space:nowrap}
.btn-red{background:#fdecea;color:#c0392b}.btn-red:hover{background:#fad6d3}
.btn-blue{background:#e8f0fe;color:#1a73e8}.btn-blue:hover{background:#d2e3fc}
.btn-green{background:#e6f4ea;color:#1e7e34}.btn-green:hover{background:#ceead6}
.btn-gray{background:#f0f0f0;color:#555}.btn-gray:hover{background:#e0e0e0}
.btn-orange{background:#fff3e0;color:#e65100}.btn-orange:hover{background:#ffe0b2}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:14px;margin-bottom:24px}
.stat-card{background:#fff;border-radius:8px;padding:20px 22px;box-shadow:0 1px 5px rgba(0,0,0,.07)}
.stat-card .num{font-size:1.9rem;font-weight:700;color:#4f6ef7}
.stat-card .lbl{font-size:.78rem;color:#999;margin-top:4px;text-transform:capitalize}
.uploads-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px}
.upload-card{background:#fff;border-radius:7px;overflow:hidden;box-shadow:0 1px 5px rgba(0,0,0,.07)}
.upload-card img{width:100%;height:105px;object-fit:cover;background:#f0f0f0;display:block}
.upload-card .info{padding:8px 9px;font-size:.73rem;color:#888}
.upload-card .info strong{display:block;color:#333;font-size:.8rem;margin-bottom:2px}
.danger{background:#fff;border-radius:8px;padding:22px;box-shadow:0 1px 5px rgba(0,0,0,.07);
  margin-top:20px;border-left:4px solid #e74c3c}
.danger h3{color:#c0392b;font-size:.95rem;margin-bottom:6px}
.danger p{color:#777;font-size:.85rem;margin-bottom:14px}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:200;
  align-items:center;justify-content:center}
.modal-bg.show{display:flex}
.modal{background:#fff;border-radius:10px;padding:30px;width:380px;box-shadow:0 8px 40px rgba(0,0,0,.18)}
.modal h3{margin-bottom:16px;font-size:1rem;color:#1a1a2e}
.modal input{width:100%;padding:9px 12px;border:1px solid #ddd;border-radius:6px;
  font-size:.95rem;outline:none;margin-bottom:16px}
.modal input:focus{border-color:#4f6ef7;box-shadow:0 0 0 3px rgba(79,110,247,.15)}
.modal-btns{display:flex;gap:8px;justify-content:flex-end}
.flash{padding:10px 15px;border-radius:6px;margin-bottom:14px;font-size:.875rem;
  animation:fadeIn .2s ease}
.flash-ok{background:#e6f4ea;color:#1e7e34;border:1px solid #b7dfbe}
.flash-err{background:#fdecea;color:#c0392b;border:1px solid #f5c6cb}
@keyframes fadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.empty{color:#bbb;padding:24px 0;font-size:.875rem}
code{background:#f4f4f8;padding:2px 6px;border-radius:4px;font-size:.82rem}
</style>
</head>
<body>
<div class="topbar">
  <h1>&#x1F17F; Parking Admin</h1>
  <a href="/" target="_blank">&#x1F30D; Live Site</a>
  <a href="/admin/logout">Sign out</a>
</div>
<div class="tabs" id="tab-bar">
  <div class="tab active" data-panel="spaces">Spaces</div>
  <div class="tab" data-panel="uploads">Uploads</div>
  <div class="tab" data-panel="observations">Observations</div>
  <div class="tab" data-panel="commands">Commands</div>
  <div class="tab" data-panel="stats">Stats &amp; DB</div>
</div>
<div class="content">
  <div id="flash-bar"></div>

  <!-- SPACES -->
  <div id="panel-spaces" class="panel active">
    <div class="toolbar">
      <h2>Parking Spaces</h2>
      <button class="btn btn-blue" onclick="loadSpaces()">&#x21BB; Refresh</button>
    </div>
    <div id="spaces-content"><p class="empty">Loading&#x2026;</p></div>
  </div>

  <!-- UPLOADS -->
  <div id="panel-uploads" class="panel">
    <div class="toolbar">
      <h2>Uploaded Images</h2>
      <button class="btn btn-blue" onclick="loadUploads()">&#x21BB; Refresh</button>
    </div>
    <div id="uploads-content"><p class="empty">Loading&#x2026;</p></div>
  </div>

  <!-- OBSERVATIONS -->
  <div id="panel-observations" class="panel">
    <div class="toolbar">
      <h2>Recent Observations</h2>
      <button class="btn btn-blue" onclick="loadObservations()">&#x21BB; Refresh</button>
    </div>
    <div id="observations-content"><p class="empty">Loading&#x2026;</p></div>
  </div>

  <!-- COMMANDS -->
  <div id="panel-commands" class="panel">
    <div class="toolbar">
      <h2>Command Queue</h2>
      <button class="btn btn-blue" onclick="loadCommands()">&#x21BB; Refresh</button>
    </div>
    <div id="commands-content"><p class="empty">Loading&#x2026;</p></div>
  </div>

  <!-- STATS -->
  <div id="panel-stats" class="panel">
    <div class="toolbar">
      <h2>Database Stats</h2>
      <button class="btn btn-blue" onclick="loadStats()">&#x21BB; Refresh</button>
    </div>
    <div id="stats-content"><p class="empty">Loading&#x2026;</p></div>
  </div>
</div>

<!-- Override modal -->
<div class="modal-bg" id="override-modal">
  <div class="modal">
    <h3>Override Space <span id="modal-sid" style="color:#4f6ef7"></span></h3>
    <input type="text" id="modal-plate" placeholder="License plate (e.g. ABCD123)"
           maxlength="20" style="text-transform:uppercase">
    <div class="modal-btns">
      <button class="btn btn-gray" onclick="closeModal()">Cancel</button>
      <button class="btn btn-green" onclick="submitOverride()">Set Occupied</button>
    </div>
  </div>
</div>

<script>
'use strict';
let _overrideSid = null;
let _autoRefresh = null;
const _loaders = {spaces:loadSpaces,uploads:loadUploads,observations:loadObservations,commands:loadCommands,stats:loadStats};

// ── Tab routing ───────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById('panel-' + t.dataset.panel).classList.add('active');
    clearInterval(_autoRefresh); _autoRefresh = null;
    _loaders[t.dataset.panel]?.();
    if (t.dataset.panel === 'spaces') _autoRefresh = setInterval(loadSpaces, 15000);
  });
});

// ── Flash messages ────────────────────────────────────────────────────────────
function flash(msg, ok=true) {
  const bar = document.getElementById('flash-bar');
  bar.innerHTML = `<div class="flash flash-${ok?'ok':'err'}">${msg}</div>`;
  setTimeout(() => bar.innerHTML='', 4500);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
const fmt = iso => iso ? new Date(iso).toLocaleString() : '—';
const badge = (s, map) => { const [bg,fg] = (map[s]||['#f0f0f0','#666']); return `<span class="badge" style="background:${bg};color:${fg}">${s}</span>`; };
const statusBadge = s => badge(s, {OCCUPIED:['#fdecea','#c0392b'],EMPTY:['#e6f4ea','#1e7e34'],UNCERTAIN:['#fff8e1','#856404']});
const cmdBadge = s => badge(s, {queued:['#fff8e1','#856404'],dispatched:['#e8f0fe','#1a73e8'],completed:['#e6f4ea','#1e7e34'],failed:['#fdecea','#c0392b']});

// ── Spaces ────────────────────────────────────────────────────────────────────
async function loadSpaces() {
  const r = await fetch('/admin/api/spaces');
  const data = await r.json();
  const rows = data.map(s => `
    <tr>
      <td><strong>${s.space_id}</strong></td>
      <td>${statusBadge(s.status)}</td>
      <td>${s.plate ? `<code>${s.plate}</code>` : '<span style="color:#ccc">—</span>'}</td>
      <td>${s.confidence != null ? (s.confidence*100).toFixed(0)+'%' : '—'}</td>
      <td style="font-size:.8rem;color:#aaa">${fmt(s.last_updated)}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-red" onclick="clearSpace('${s.space_id}')">Clear</button>
        <button class="btn btn-orange" onclick="openOverride('${s.space_id}')" style="margin-left:5px">Override</button>
      </td>
    </tr>`).join('');
  document.getElementById('spaces-content').innerHTML = `
    <table>
      <thead><tr><th>Space</th><th>Status</th><th>Plate</th><th>Conf</th><th>Last Updated</th><th>Actions</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="empty">No spaces found.</td></tr>'}</tbody>
    </table>`;
}

async function clearSpace(sid) {
  if (!confirm(`Clear space ${sid}? This will mark it as EMPTY.`)) return;
  const r = await fetch(`/admin/api/spaces/${sid}/clear`, {method:'POST'});
  const d = await r.json();
  r.ok ? (flash(`Space ${sid} cleared.`), loadSpaces()) : flash(d.error||'Error', false);
}

function openOverride(sid) {
  _overrideSid = sid;
  document.getElementById('modal-sid').textContent = sid;
  document.getElementById('modal-plate').value = '';
  document.getElementById('override-modal').classList.add('show');
  setTimeout(() => document.getElementById('modal-plate').focus(), 60);
}
function closeModal() {
  document.getElementById('override-modal').classList.remove('show');
  _overrideSid = null;
}
async function submitOverride() {
  const plate = document.getElementById('modal-plate').value.trim().toUpperCase();
  if (!plate) { alert('Enter a plate number.'); return; }
  const r = await fetch(`/admin/api/spaces/${_overrideSid}/override`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({plate})
  });
  const d = await r.json();
  closeModal();
  r.ok ? (flash(`Space ${_overrideSid} set to <code>${plate}</code>.`), loadSpaces())
       : flash(d.error||'Error', false);
}
document.getElementById('override-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('override-modal')) closeModal();
});
document.getElementById('modal-plate').addEventListener('keydown', e => {
  if (e.key==='Enter') submitOverride();
  if (e.key==='Escape') closeModal();
});

// ── Uploads ───────────────────────────────────────────────────────────────────
async function loadUploads() {
  const r = await fetch('/admin/api/uploads?limit=24');
  const data = await r.json();
  if (!data.length) {
    document.getElementById('uploads-content').innerHTML = '<p class="empty">No uploads yet.</p>';
    return;
  }
  const cards = data.map(u => `
    <div class="upload-card">
      <a href="/api/uploads/${u.id}" target="_blank">
        <img src="/api/uploads/${u.id}" alt="upload" loading="lazy">
      </a>
      <div class="info">
        <strong>${u.device_id}</strong>
        <span>${fmt(u.created_at)}</span>
        <span style="color:#ccc;font-size:.7rem">${u.id.slice(0,12)}&hellip;</span>
      </div>
    </div>`).join('');
  document.getElementById('uploads-content').innerHTML = `<div class="uploads-grid">${cards}</div>`;
}

// ── Observations ──────────────────────────────────────────────────────────────
async function loadObservations() {
  const r = await fetch('/admin/api/observations?limit=60');
  const data = await r.json();
  if (!data.length) {
    document.getElementById('observations-content').innerHTML = '<p class="empty">No observations yet.</p>';
    return;
  }
  const rows = data.map(o => {
    const s = o.summary || {};
    return `<tr>
      <td style="font-size:.8rem;color:#aaa;white-space:nowrap">${fmt(s.timestamp || o.created_at)}</td>
      <td><code>${o.device_id}</code></td>
      <td>${s.plate_text ? `<code>${s.plate_text}</code>` : '—'}</td>
      <td>${s.space_id||'—'}</td>
      <td>${s.space_status ? statusBadge(s.space_status) : '—'}</td>
      <td>${s.confidence!=null?(s.confidence*100).toFixed(0)+'%':'—'}</td>
      <td><a href="/api/devices/${o.device_id}/observations/${o.id}" target="_blank"
             style="color:#4f6ef7;font-size:.8rem">Raw</a></td>
    </tr>`;
  }).join('');
  document.getElementById('observations-content').innerHTML = `
    <table>
      <thead><tr><th>Time</th><th>Device</th><th>Plate</th><th>Space</th><th>Status</th><th>Conf</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── Commands ──────────────────────────────────────────────────────────────────
async function loadCommands() {
  const r = await fetch('/admin/api/commands?limit=60');
  const data = await r.json();
  if (!data.length) {
    document.getElementById('commands-content').innerHTML = '<p class="empty">No commands yet.</p>';
    return;
  }
  const rows = data.map(c => `
    <tr>
      <td>#${c.id}</td>
      <td><code>${c.device_id}</code></td>
      <td><code>${c.command}</code></td>
      <td>${c.requested_by||'—'}</td>
      <td>${cmdBadge(c.status)}</td>
      <td style="font-size:.8rem;color:#aaa">${fmt(c.created_at)}</td>
      <td style="font-size:.8rem;color:#aaa">${fmt(c.completed_at)}</td>
    </tr>`).join('');
  document.getElementById('commands-content').innerHTML = `
    <table>
      <thead><tr><th>ID</th><th>Device</th><th>Command</th><th>By</th><th>Status</th><th>Created</th><th>Completed</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── Stats ─────────────────────────────────────────────────────────────────────
async function loadStats() {
  const r = await fetch('/admin/api/stats');
  const d = await r.json();
  const counts = d.row_counts || {};
  const kb = (d.db_size_bytes / 1024).toFixed(1);
  const labels = {parking_spaces:'Parking Spaces',devices:'Devices',commands:'Commands',uploads:'Uploads',observations:'Observations'};
  const cards = Object.entries(counts).map(([t,n]) => `
    <div class="stat-card">
      <div class="num">${n}</div>
      <div class="lbl">${labels[t]||t}</div>
    </div>`).join('');
  document.getElementById('stats-content').innerHTML = `
    <div class="stats-grid">
      ${cards}
      <div class="stat-card"><div class="num">${kb}</div><div class="lbl">DB size (KB)</div></div>
    </div>
    <div class="danger">
      <h3>&#x26A0; Danger Zone</h3>
      <p>Reset to a clean slate: marks every space as EMPTY, deletes all uploaded images, and wipes observations and commands. The 40 parking spaces are kept. This cannot be undone.</p>
      <button class="btn btn-red" onclick="purgeAll()">&#x1F5D1; Reset to Clean Slate</button>
    </div>`;
}

async function purgeAll() {
  if (!confirm('Reset to clean slate?\\n\\nThis will:\\n• Mark all 40 spaces as EMPTY\\n• Delete ALL uploaded images from disk\\n• Wipe observations and commands\\n\\nThis cannot be undone.')) return;
  const r = await fetch('/admin/api/purge', {method:'POST'});
  const d = await r.json();
  if (r.ok) {
    flash(`Clean slate done — ${d.spaces_cleared} spaces cleared, all images and history deleted.`);
    await Promise.all([loadSpaces(), loadUploads(), loadObservations(), loadCommands(), loadStats()]);
  } else {
    flash(d.error||'Purge failed', false);
  }
}

// init
loadSpaces();
_autoRefresh = setInterval(loadSpaces, 15000);
</script>
</body>
</html>"""


def _render_login(error: bool = False) -> str:
    block = '<div class="err">Invalid username or password.</div>' if error else ""
    return _LOGIN_HTML.format(error_block=block)


def create_admin_router(state: Any, reset_runtime_state: Any | None = None) -> APIRouter:
    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "admin123")
    secret = os.getenv("ADMIN_SESSION_SECRET", "parking-admin-secret-change-me")
    expected_token = hmac.new(
        secret.encode(),
        f"{username}:{password}".encode(),
        hashlib.sha256,
    ).hexdigest()

    router = APIRouter(prefix="/admin", include_in_schema=False)

    def _auth(request: Request) -> bool:
        tok = request.cookies.get(_COOKIE, "")
        return bool(tok) and hmac.compare_digest(tok, expected_token)

    def _unauth() -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if _auth(request):
            return RedirectResponse("/admin", status_code=302)
        return HTMLResponse(_render_login(bool(request.query_params.get("error"))))

    @router.post("/login")
    async def login_post(
        request: Request,
        username_in: str = Form(alias="username"),
        password_in: str = Form(alias="password"),
    ):
        if username_in == username and password_in == password:
            resp = RedirectResponse("/admin", status_code=302)
            resp.set_cookie(_COOKIE, expected_token, httponly=True, samesite="lax")
            return resp
        return RedirectResponse("/admin/login?error=1", status_code=302)

    @router.get("/logout")
    async def logout():
        resp = RedirectResponse("/admin/login", status_code=302)
        resp.delete_cookie(_COOKIE)
        return resp

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not _auth(request):
            return RedirectResponse("/admin/login", status_code=302)
        return HTMLResponse(_ADMIN_HTML)

    # ── Data API ──────────────────────────────────────────────────────────────

    @router.get("/api/spaces")
    async def api_spaces(request: Request):
        if not _auth(request):
            return _unauth()
        spaces = await run_in_threadpool(state.get_parking_spaces)
        return JSONResponse([
            {
                "space_id": sid,
                "status": s.get("status", "EMPTY"),
                "occupied": s.get("occupied", False),
                "plate": (s.get("vehicle_data") or {}).get("license_plate"),
                "confidence": s.get("decision_confidence"),
                "reason": s.get("decision_reason"),
                "last_updated": s.get("last_resolved_at"),
                "source_time": s.get("source_detection_time"),
            }
            for sid, s in sorted(spaces.items())
        ])

    @router.post("/api/spaces/{space_id}/clear")
    async def api_clear_space(request: Request, space_id: str):
        if not _auth(request):
            return _unauth()
        result = await run_in_threadpool(
            state.apply_manual_parking_update, {"space_id": space_id, "occupied": False}
        )
        if result is None:
            return JSONResponse({"error": "space not found"}, status_code=404)
        return JSONResponse({"status": "cleared", "space_id": space_id})

    @router.post("/api/spaces/{space_id}/override")
    async def api_override_space(request: Request, space_id: str):
        if not _auth(request):
            return _unauth()
        body = await request.json()
        plate = (body.get("plate") or "MANUAL").upper().strip()
        result = await run_in_threadpool(
            state.apply_manual_parking_update,
            {"space_id": space_id, "occupied": True, "license_plate": plate, "confidence": 1.0},
        )
        if result is None:
            return JSONResponse({"error": "space not found"}, status_code=404)
        return JSONResponse({"status": "overridden", "space_id": space_id, "plate": plate})

    @router.get("/api/uploads")
    async def api_uploads(request: Request, limit: int = 60):
        if not _auth(request):
            return _unauth()
        uploads = await run_in_threadpool(state.list_uploads, limit)
        return JSONResponse(uploads)

    @router.get("/api/observations")
    async def api_observations(request: Request, limit: int = 60):
        if not _auth(request):
            return _unauth()
        device_id = state.get_default_device_id()
        obs = await run_in_threadpool(state.get_observations_for_device, device_id, limit)
        return JSONResponse(obs)

    @router.get("/api/commands")
    async def api_commands(request: Request, limit: int = 60):
        if not _auth(request):
            return _unauth()
        cmds = await run_in_threadpool(state.get_recent_commands, limit)
        return JSONResponse(cmds)

    @router.get("/api/stats")
    async def api_stats(request: Request):
        if not _auth(request):
            return _unauth()
        stats = await run_in_threadpool(state.get_db_stats)
        return JSONResponse(stats)

    @router.post("/api/purge")
    async def api_purge(request: Request):
        if not _auth(request):
            return _unauth()
        result = await run_in_threadpool(state.purge_all_data)
        if reset_runtime_state is not None:
            await run_in_threadpool(reset_runtime_state)
        return JSONResponse(result)

    return router
