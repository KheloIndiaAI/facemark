/* Centres and Users pages. */

const E = (s) => Charts.esc(s);
const pageState = { centres: [] };

/* ==========================================================================
   Centres - search + full detail
   ========================================================================== */

async function renderCentresPage() {
    const root = document.getElementById('centres-root');
    root.innerHTML = '<div class="empty-state py-12">Loading centres...</div>';
    try {
        const data = await api.get('/api/centres');
        pageState.centres = data.centres;
        const stateSel = document.getElementById('centre-state-filter');
        const sportSel = document.getElementById('centre-sport-filter');
        if (stateSel && stateSel.options.length <= 1) {
            data.states.forEach(s => stateSel.add(new Option(s, s)));
            data.sports.forEach(s => sportSel.add(new Option(s, s)));
        }
        drawCentreResults(data.centres);
    } catch { root.innerHTML = '<div class="empty-state py-12">Could not load centres</div>'; }
}

let centreSearchTimer = null;
function onCentreSearch() {
    clearTimeout(centreSearchTimer);
    centreSearchTimer = setTimeout(runCentreSearch, 220);   // debounce keystrokes
}

async function runCentreSearch() {
    const q = document.getElementById('centre-search').value.trim();
    const state = document.getElementById('centre-state-filter').value;
    const sport = document.getElementById('centre-sport-filter').value;
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (state) params.set('state', state);
    if (sport) params.set('sport', sport);
    const data = await api.get('/api/centres?' + params.toString());
    pageState.centres = data.centres;
    drawCentreResults(data.centres, q);
}

function drawCentreResults(centres, q) {
    const root = document.getElementById('centres-root');
    const count = document.getElementById('centre-count');
    if (count) count.textContent = `${centres.length} centre${centres.length === 1 ? '' : 's'}`;
    if (!centres.length) {
        root.innerHTML = `<div class="empty-state py-12">
            <div class="empty-state-text">No centres match ${q ? `"${E(q)}"` : 'these filters'}</div>
        </div>`;
        return;
    }
    root.innerHTML = `<div class="centre-grid">` + centres.map(c => `
        <div class="centre-card" onclick="openCentreDetail(${c.id})">
            <div class="centre-card-head">
                <div>
                    <div class="centre-name">${E(c.name)}</div>
                    <div class="centre-code">${E(c.code)}</div>
                </div>
                ${c.is_demo ? '<span class="badge badge-amber" title="Placeholder record, not real Khelo India data">DEMO</span>' : ''}
            </div>
            <div class="centre-meta">
                <div><span class="ck">Location</span> ${E(c.district || '-')}, ${E(c.state || '-')}</div>
                <div><span class="ck">Type</span> ${E(c.centre_type)} &middot; capacity ${c.capacity || '-'}</div>
                <div><span class="ck">Sports</span> ${c.sports.length ? c.sports.map(s => `<span class="tag">${E(s)}</span>`).join('') : '-'}</div>
            </div>
            <div class="centre-card-foot">
                ${c.latitude != null ? `<span class="text-xs text-muted">${Icon('pin', 12)} ${c.latitude.toFixed(4)}, ${c.longitude.toFixed(4)}</span>` : '<span class="text-xs text-muted">No coordinates</span>'}
                <span class="text-xs" style="color:var(--accent);font-weight:600">View details →</span>
            </div>
        </div>`).join('') + `</div>`;
}

async function openCentreDetail(id) {
    const c = await api.get(`/api/centres/${id}`);
    const roster = (list, kind) => list.length ? `
        <div class="detail-people">${list.map(p => `
            <div class="detail-person">
                ${p.photo_url ? `<img src="${p.photo_url}" class="avatar avatar-sm" alt="">`
                              : `<div class="avatar avatar-sm">${E(p.name.charAt(0))}</div>`}
                <div>
                    <div style="font-weight:600;font-size:13px">${E(p.name)}</div>
                    <div class="text-xs text-muted font-mono">${E(p.roll_no)}${p.age ? ` &middot; ${p.age}y` : ''}${p.sport ? ` &middot; ${E(p.sport)}` : ''}</div>
                </div>
            </div>`).join('')}</div>`
        : `<div class="text-sm text-muted">No ${kind} registered at this centre yet.</div>`;

    const attChart = c.recent_attendance.length
        ? Charts.barChart(c.recent_attendance.slice().reverse().map(r => ({
            label: r.date, short: r.date.slice(5), value: r.present })),
            { height: 180, width: 620 })
        : '<div class="text-sm text-muted">No attendance recorded at this centre yet.</div>';

    // openModal sets textContent (XSS-safe, since centre names are user input),
    // so the title must be plain text - the DEMO badge lives in the body instead.
    openModal(c.name, `
        ${c.is_demo ? `<div class="notice notice-amber">
            This is a <strong>placeholder record</strong>, not real Khelo India data. Replace it by
            importing a CSV/JSON of real centres, or delete all demo rows, from the Centres page header.
        </div>` : ''}
        <div class="detail-grid">
            <div><span class="ck">Code</span><div>${E(c.code)}</div></div>
            <div><span class="ck">Type</span><div>${E(c.centre_type)}</div></div>
            <div><span class="ck">State</span><div>${E(c.state || '-')}</div></div>
            <div><span class="ck">District</span><div>${E(c.district || '-')}</div></div>
            <div><span class="ck">Pincode</span><div>${E(c.pincode || '-')}</div></div>
            <div><span class="ck">Established</span><div>${E(c.established || '-')}</div></div>
            <div><span class="ck">Capacity</span><div>${c.capacity || '-'}</div></div>
            <div><span class="ck">Geo-fence</span><div>${c.geofence_m} m</div></div>
            <div style="grid-column:1/-1"><span class="ck">Address</span><div>${E(c.address || '-')}</div></div>
            <div style="grid-column:1/-1"><span class="ck">Sports</span>
                <div>${c.sports.map(s => `<span class="tag">${E(s)}</span>`).join('') || '-'}</div></div>
            <div><span class="ck">In-charge</span><div>${E(c.incharge_name || '-')}</div></div>
            <div><span class="ck">Phone</span><div>${E(c.contact_phone || '-')}</div></div>
            <div><span class="ck">Email</span><div>${E(c.contact_email || '-')}</div></div>
            <div><span class="ck">Coordinates</span><div class="font-mono text-sm">${
                c.latitude != null ? `${c.latitude}, ${c.longitude}` : 'not set'}</div></div>
        </div>

        <div class="stats-grid" style="margin-top:18px">
            <div class="stat-card"><div class="stat-header">Athletes</div><div class="stat-value">${c.athlete_count}</div></div>
            <div class="stat-card"><div class="stat-header">Coaches</div><div class="stat-value">${c.coach_count}</div></div>
            <div class="stat-card"><div class="stat-header">Session days</div><div class="stat-value">${c.attendance_days}</div></div>
            <div class="stat-card"><div class="stat-header">Records</div><div class="stat-value">${c.attendance_records}</div></div>
        </div>

        <h3 style="margin:20px 0 8px">Recent attendance</h3>
        <div class="chart-scroll">${attChart}</div>

        <h3 style="margin:20px 0 8px">Athletes (${c.athlete_count})</h3>
        ${roster(c.athletes, 'athletes')}

        <h3 style="margin:20px 0 8px">Coaches (${c.coach_count})</h3>
        ${roster(c.coaches, 'coaches')}

        <h3 style="margin:20px 0 8px">Staff accounts</h3>
        ${c.staff_accounts.length ? `<div class="detail-people">${c.staff_accounts.map(u => `
            <div class="detail-person">
                <div class="avatar avatar-sm">${E(u.full_name.charAt(0))}</div>
                <div><div style="font-weight:600;font-size:13px">${E(u.full_name)}</div>
                <div class="text-xs text-muted font-mono">${E(u.username)} &middot; ${E(u.role)}
                ${u.is_active ? '' : ' &middot; disabled'}</div></div>
            </div>`).join('')}</div>` : '<div class="text-sm text-muted">No login accounts linked to this centre.</div>'}
        `,
        `<button class="btn btn-secondary" onclick="closeModal()">Close</button>`);
}

function openAddCentreModal() {
    openModal('Add centre', `
        <div class="form-row">
            <div class="form-group"><label class="form-label">Code *</label>
                <input id="c-code" class="form-input" placeholder="KIC-DL-014"></div>
            <div class="form-group"><label class="form-label">Name *</label>
                <input id="c-name" class="form-input" placeholder="Centre name"></div>
        </div>
        <div class="form-row">
            <div class="form-group"><label class="form-label">State</label><input id="c-state" class="form-input"></div>
            <div class="form-group"><label class="form-label">District</label><input id="c-district" class="form-input"></div>
        </div>
        <div class="form-group"><label class="form-label">Address</label><input id="c-address" class="form-input"></div>
        <div class="form-row">
            <div class="form-group"><label class="form-label">Pincode</label><input id="c-pincode" class="form-input"></div>
            <div class="form-group"><label class="form-label">Capacity</label><input id="c-capacity" type="number" class="form-input" value="0"></div>
        </div>
        <div class="form-group"><label class="form-label">Sports (comma separated)</label>
            <input id="c-sports" class="form-input" placeholder="Athletics, Hockey, Boxing"></div>
        <div class="form-row">
            <div class="form-group"><label class="form-label">Latitude</label>
                <input id="c-lat" type="number" step="any" class="form-input" placeholder="28.5921"></div>
            <div class="form-group"><label class="form-label">Longitude</label>
                <input id="c-lng" type="number" step="any" class="form-input" placeholder="77.1691"></div>
        </div>
        <div class="form-row">
            <div class="form-group"><label class="form-label">Geo-fence radius (m)</label>
                <input id="c-fence" type="number" class="form-input" value="300"></div>
            <div class="form-group"><label class="form-label">In-charge</label><input id="c-incharge" class="form-input"></div>
        </div>
        <div class="form-row">
            <div class="form-group"><label class="form-label">Phone</label><input id="c-phone" class="form-input"></div>
            <div class="form-group"><label class="form-label">Email</label><input id="c-email" class="form-input"></div>
        </div>
        <button class="btn btn-secondary w-full" onclick="fillCentreFromDevice()">Use my current location</button>`,
        `<button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
         <button class="btn btn-primary" onclick="submitCentre()">Add centre</button>`);
}

function fillCentreFromDevice() {
    if (!navigator.geolocation) return showToast('Unavailable', 'This browser has no location support', 'error');
    navigator.geolocation.getCurrentPosition(
        pos => {
            document.getElementById('c-lat').value = pos.coords.latitude.toFixed(6);
            document.getElementById('c-lng').value = pos.coords.longitude.toFixed(6);
            showToast('Location captured', `Accurate to about ${Math.round(pos.coords.accuracy)} m`, 'success');
        },
        err => showToast('Location denied', err.message, 'error'),
        { enableHighAccuracy: true, timeout: 10000 });
}

async function submitCentre() {
    const g = id => document.getElementById(id).value.trim();
    if (!g('c-code') || !g('c-name')) return showToast('Error', 'Code and name are required', 'error');
    const fd = new FormData();
    fd.append('code', g('c-code')); fd.append('name', g('c-name'));
    ['state', 'district', 'address', 'pincode', 'sports', 'incharge_name', 'contact_phone', 'contact_email']
        .forEach(k => {
            const map = { incharge_name: 'c-incharge', contact_phone: 'c-phone', contact_email: 'c-email' };
            const v = g(map[k] || 'c-' + k);
            if (v) fd.append(k, v);
        });
    ['capacity', 'geofence_m'].forEach(k => fd.append(k, g(k === 'capacity' ? 'c-capacity' : 'c-fence') || '0'));
    if (g('c-lat')) fd.append('latitude', g('c-lat'));
    if (g('c-lng')) fd.append('longitude', g('c-lng'));
    try {
        await api.postForm('/api/centres', fd);
        closeModal();
        showToast('Centre added', g('c-name'), 'success');
        renderCentresPage();
    } catch { /* surfaced by api layer */ }
}

function openImportCentresModal() {
    openModal('Import real centre data', `
        <div class="notice notice-blue">
            Upload a <strong>CSV</strong> or <strong>JSON</strong> file of real centres. Recognised columns:
            <code>code, name, centre_type, state, district, address, pincode, sports, capacity,
            latitude, longitude, geofence_m, incharge_name, contact_phone, contact_email, established</code>.
            <br><br><code>code</code> and <code>name</code> are required; <code>sports</code> may be a
            comma-separated list. Imported rows are marked as real data, not demo.
        </div>
        <div class="form-group"><label class="form-label">File</label>
            <input type="file" id="import-file" class="form-input" accept=".csv,.json" style="padding-top:10px"></div>`,
        `<button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
         <button class="btn btn-primary" onclick="submitImport()">Import</button>`);
}

async function submitImport() {
    const f = document.getElementById('import-file').files[0];
    if (!f) return showToast('Error', 'Choose a file first', 'error');
    const fd = new FormData();
    fd.append('file', f);
    try {
        const r = await api.postForm('/api/centres/import', fd);
        closeModal();
        showToast('Imported', `${r.imported} centre(s) added, ${r.skipped} skipped`, 'success');
        renderCentresPage();
    } catch { /* surfaced */ }
}

async function purgeDemoCentres() {
    if (!confirm('Delete every DEMO placeholder centre? Real imported centres are untouched.')) return;
    const r = await api.delete('/api/centres/demo/all');
    showToast('Removed', `${r.deleted} demo centre(s) deleted`, 'success');
    renderCentresPage();
}

/* ==========================================================================
   Users (super admin)
   ========================================================================== */

async function renderUsersPage() {
    const root = document.getElementById('users-root');
    root.innerHTML = '<div class="empty-state py-12">Loading accounts...</div>';
    const [u, c] = await Promise.all([api.get('/api/users'), api.get('/api/centres')]);
    pageState.centres = c.centres;
    root.innerHTML = `
      <div class="card"><div class="card-body p-0">
        <table class="data-table"><thead><tr>
          <th>Name</th><th>Username</th><th>Role</th><th>Centre</th><th>Last sign-in</th><th>Status</th><th></th>
        </tr></thead><tbody>${u.users.map(x => `<tr>
          <td>${E(x.full_name)}</td>
          <td class="font-mono text-sm">${E(x.username)}</td>
          <td><span class="badge ${x.role === 'super_admin' ? 'badge-blue' : 'badge-green'}">
            ${x.role === 'super_admin' ? 'Super Admin' : 'Coach'}</span></td>
          <td>${E(x.centre_name || '-')}</td>
          <td class="text-sm text-muted">${E(x.last_login ? x.last_login.replace('T', ' ') : 'never')}</td>
          <td>${x.is_active ? '<span class="badge badge-green">active</span>'
                            : '<span class="badge badge-red">disabled</span>'}</td>
          <td style="white-space:nowrap">
            <button class="btn btn-secondary" style="min-height:30px;padding:0 10px;font-size:12px"
              onclick="toggleUser(${x.id}, ${!x.is_active})">${x.is_active ? 'Disable' : 'Enable'}</button>
            <button class="btn btn-secondary" style="min-height:30px;padding:0 10px;font-size:12px"
              onclick="resetUserPassword(${x.id}, '${E(x.username)}')">Reset password</button>
          </td></tr>`).join('')}</tbody></table>
      </div></div>`;
}

function openAddUserModal() {
    const centres = pageState.centres || [];
    openModal('Create login account', `
        <div class="form-row">
          <div class="form-group"><label class="form-label">Full name *</label>
            <input id="u-name" class="form-input"></div>
          <div class="form-group"><label class="form-label">Username *</label>
            <input id="u-username" class="form-input" autocomplete="off"></div>
        </div>
        <div class="form-group"><label class="form-label">Password * (min 6 characters)</label>
          <input id="u-password" type="password" class="form-input" autocomplete="new-password"></div>
        <div class="form-group"><label class="form-label">Role *</label>
          <select id="u-role" class="form-input" onchange="document.getElementById('u-centre-wrap').style.display = this.value === 'coach' ? 'block' : 'none'">
            <option value="coach">Coach - one centre only</option>
            <option value="super_admin">Super Admin - all centres</option>
          </select></div>
        <div class="form-group" id="u-centre-wrap"><label class="form-label">Centre * (coaches only)</label>
          <select id="u-centre" class="form-input">
            ${centres.map(c => `<option value="${c.id}">${E(c.name)} (${E(c.code)})</option>`).join('')}
          </select></div>
        <div class="form-row">
          <div class="form-group"><label class="form-label">Email</label><input id="u-email" class="form-input"></div>
          <div class="form-group"><label class="form-label">Phone</label><input id="u-phone" class="form-input"></div>
        </div>`,
        `<button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
         <button class="btn btn-primary" onclick="submitUser()">Create account</button>`);
}

async function submitUser() {
    const v = id => document.getElementById(id).value.trim();
    if (!v('u-name') || !v('u-username') || !v('u-password'))
        return showToast('Error', 'Name, username and password are required', 'error');
    const fd = new FormData();
    fd.append('full_name', v('u-name'));
    fd.append('username', v('u-username'));
    fd.append('password', v('u-password'));
    fd.append('role', v('u-role'));
    if (v('u-role') === 'coach') fd.append('centre_id', v('u-centre'));
    if (v('u-email')) fd.append('email', v('u-email'));
    if (v('u-phone')) fd.append('phone', v('u-phone'));
    try {
        await api.postForm('/api/users', fd);
        closeModal();
        showToast('Account created', v('u-username'), 'success');
        renderUsersPage();
    } catch { /* surfaced */ }
}

async function toggleUser(id, active) {
    const fd = new FormData();
    fd.append('active', active ? 'true' : 'false');
    await fetch(`/api/users/${id}/active`, { method: 'PATCH', body: fd });
    renderUsersPage();
}

async function resetUserPassword(id, username) {
    const pw = prompt(`New password for ${username} (min 6 characters):`);
    if (!pw) return;
    const fd = new FormData();
    fd.append('new_password', pw);
    try {
        await api.postForm(`/api/users/${id}/password`, fd);
        showToast('Password reset', `${username} must sign in again`, 'success');
    } catch { /* surfaced */ }
}
