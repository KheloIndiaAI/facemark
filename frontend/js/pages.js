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
    // The footer's Edit button is below the rosters; this one is where the
    // details are, so nobody scrolls past every athlete to find it.
    openModal(c.name, `
        ${isSuperAdmin() ? `<div style="display:flex;justify-content:flex-end;margin-bottom:8px">
            <button class="btn btn-secondary" style="min-height:32px;padding:0 12px;font-size:13px"
                onclick="openEditCentreModal(${c.id})">${Icon('edit', 14)} Edit details</button></div>` : ''}
        <div class="detail-grid">
            <div><span class="ck">Code</span><div>${E(c.code)}</div></div>
            <div><span class="ck">Type</span><div>${E(c.centre_type)}</div></div>
            <div><span class="ck">State</span><div>${E(c.state || '-')}</div></div>
            <div><span class="ck">District</span><div>${E(c.district || '-')}</div></div>
            <div><span class="ck">Pincode</span><div>${E(c.pincode || '-')}</div></div>
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
            ${c.coach_join_code ? `
            <div style="grid-column:1/-1"><span class="ck">Coach registration code</span>
                <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
                    <span class="font-mono" style="font-size:18px;letter-spacing:2px">${E(c.coach_join_code)}</span>
                    <button class="btn btn-secondary" style="min-height:28px;padding:0 10px;font-size:12px"
                        data-rotate-code data-centre-id="${c.id}">Issue a new code</button>
                </div>
                <div class="text-xs text-muted" style="margin-top:4px">
                    Give this to a coach so they can register themselves. A super admin
                    still approves them. Issuing a new code stops the old one working.
                </div></div>` : ''}
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

        ${c.pending_count ? `<div class="notice notice-amber" style="margin-top:20px">
            <strong>${c.pending_count} registration${c.pending_count === 1 ? '' : 's'}
            waiting for approval</strong>
            <div class="text-xs text-muted mt-1">Not counted above and not on any
            roster until somebody approves them. They used to be listed here as
            though they already trained at this centre.</div>
        </div>` : ''}

        `,
        `<button class="btn btn-secondary" onclick="closeModal()">Close</button>`
        + (isSuperAdmin() ? `<button class="btn btn-primary" onclick="openEditCentreModal(${c.id})">Edit details</button>` : ''));
}

/* Form field id -> API field. One list for Add and Edit, so a field added to
   the form cannot reach one and silently miss the other. */
const CENTRE_FIELDS = {
    'c-code': 'code', 'c-name': 'name', 'c-type': 'centre_type',
    'c-state': 'state', 'c-district': 'district', 'c-address': 'address',
    'c-pincode': 'pincode', 'c-capacity': 'capacity', 'c-sports': 'sports',
    'c-lat': 'latitude', 'c-lng': 'longitude', 'c-fence': 'geofence_m',
    'c-incharge': 'incharge_name', 'c-phone': 'contact_phone',
    'c-email': 'contact_email', 'c-established': 'established',
};

function centreFormHtml(c = {}) {
    const v = (x) => x == null ? '' : E(String(x));
    const input = (id, label, val, attrs = '') => `
        <div class="form-group"><label class="form-label" for="${id}">${label}</label>
            <input id="${id}" class="form-input" value="${v(val)}" ${attrs}></div>`;
    return `
        <div class="form-row">
            ${input('c-code', 'Code *', c.code, 'placeholder="KIC-DL-014" autocapitalize="characters"')}
            ${input('c-name', 'Name *', c.name, 'placeholder="Centre name"')}
        </div>
        <div class="form-row">
            ${input('c-type', 'Type', c.centre_type || 'KIC', 'list="c-type-list" autocapitalize="characters"')}
            ${input('c-established', 'Established', c.established, 'placeholder="2020"')}
        </div>
        <datalist id="c-type-list"><option value="KIC"><option value="KISCE"></datalist>
        <div class="form-row">
            ${input('c-state', 'State', c.state)}
            ${input('c-district', 'District', c.district)}
        </div>
        ${input('c-address', 'Address', c.address)}
        <div class="form-row">
            ${input('c-pincode', 'Pincode', c.pincode, 'inputmode="numeric"')}
            ${input('c-capacity', 'Capacity', c.capacity ?? 0, 'type="number" min="0"')}
        </div>
        ${input('c-sports', 'Sports (comma separated)', (c.sports || []).join(', '), 'placeholder="Athletics, Hockey, Boxing"')}
        <div class="form-row">
            ${input('c-lat', 'Latitude', c.latitude, 'type="number" step="any" placeholder="28.5921"')}
            ${input('c-lng', 'Longitude', c.longitude, 'type="number" step="any" placeholder="77.1691"')}
        </div>
        <div class="form-row">
            ${input('c-fence', 'Geo-fence radius (m)', c.geofence_m ?? 300, 'type="number" min="10" max="10000"')}
            ${input('c-incharge', 'In-charge', c.incharge_name)}
        </div>
        <div class="form-row">
            ${input('c-phone', 'Phone', c.contact_phone, 'type="tel"')}
            ${input('c-email', 'Email', c.contact_email, 'type="email"')}
        </div>
        <button class="btn btn-secondary w-full" onclick="fillCentreFromDevice()">Use my current location</button>`;
}

function openAddCentreModal() {
    openModal('Add centre', centreFormHtml(),
        `<button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
         <button class="btn btn-primary" onclick="submitCentre()">Add centre</button>`);
}

async function openEditCentreModal(id) {
    let c;
    try { c = await api.get(`/api/centres/${id}`); } catch { return; }
    openModal(`Edit ${c.name}`, centreFormHtml(c) + `
        <div class="text-xs text-muted" style="margin-top:10px">
            Leave a box empty to clear it. Changing the geo-fence or coordinates
            applies to attendance taken from now on, not to records already saved.
        </div>`,
        `<button class="btn btn-secondary" onclick="openCentreDetail(${c.id})">Cancel</button>
         <button class="btn btn-primary" id="c-save" data-old-code="${E(c.code)}"
             onclick="saveCentreEdit(${c.id})">Save changes</button>`);
}

async function saveCentreEdit(id) {
    const g = (fid) => document.getElementById(fid).value.trim();
    const oldCode = document.getElementById('c-save').dataset.oldCode;
    if (!g('c-code') || !g('c-name')) return showToast('Error', 'Code and name are required', 'error');
    if (!!g('c-lat') !== !!g('c-lng')) {
        return showToast('Error', 'Give both latitude and longitude, or clear both', 'error');
    }
    if (g('c-code').toUpperCase() !== oldCode && !window.confirm(
        `Change the centre code from ${oldCode} to ${g('c-code').toUpperCase()}?\n\n`
        + 'Anything outside the app that refers to the old code, such as a '
        + 'spreadsheet or an import script, will need the new one.')) return;
    // Every field is sent, blanks included: that is how a box emptied here
    // clears the value on the server rather than being ignored.
    const fd = new FormData();
    Object.entries(CENTRE_FIELDS).forEach(([fid, key]) => fd.append(key, g(fid)));
    const btn = document.getElementById('c-save');
    if (btn) btn.disabled = true;
    try {
        const r = await api.postForm(`/api/centres/${id}`, fd, 'PATCH');
        showToast('Centre updated', r.centre.name, 'success');
        renderCentresPage();
        openCentreDetail(id);
    } catch {
        if (btn) btn.disabled = false;     // reason already shown by the api layer
    }
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
    // Blanks are left out so the server's defaults apply. A blank geo-fence
    // used to be sent as "0" - a fence nobody could ever be inside.
    const fd = new FormData();
    Object.entries(CENTRE_FIELDS).forEach(([fid, key]) => { if (g(fid)) fd.append(key, g(fid)); });
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

/* The approval decision and the on/off switch are two different things and
   were being shown as one. An account can be approved and disabled, or
   rejected and still nominally "active" in the old column - which is how a
   rejected registration could sit on this page looking fine. */
function userStatusBadge(x) {
    if (!x.is_active) return '<span class="badge badge-red">disabled</span>';
    // Not "awaiting approval" until a verified face is on file - the account
    // is created before any face is recorded, and the server refuses to
    // approve it until one is (sessions.HAS_VERIFIED_FACE).
    if (x.status === 'pending' && !(Number(x.templates) > 0)) {
        return '<span class="badge badge-red">registration incomplete</span>';
    }
    if (x.status === 'pending') return '<span class="badge badge-amber">awaiting approval</span>';
    if (x.status === 'rejected') return '<span class="badge badge-red">rejected</span>';
    if (x.status && x.status !== 'active') return `<span class="badge badge-red">${E(x.status)}</span>`;
    return '<span class="badge badge-green">active</span>';
}

async function reopenUser(id, name) {
    if (!window.confirm(
        `Send ${name} back to the approval queue?\n\nThey stay unable to sign in `
        + `and their face stays out of the register until somebody decides again.`)) return;
    try {
        await api.postForm(`/api/approvals/${id}/reopen`, new FormData());
        showToast('Back in the queue', `${name} is waiting for a decision again.`, 'success');
        renderUsersPage();
    } catch (err) {
        showToast('Could not do that', (err && err.message) || 'Try again.', 'error');
    }
}

async function decideUser(id, approve, name, role, templates) {
    // The person row is created at the START of self-registration, before any
    // face is ever recorded - so a pending account with 0 templates is not
    // rare, it is what "the capture failed" or "they never got that far"
    // looks like from here. Approving it anyway is sometimes the right call -
    // a coach can register the face in person afterwards - but it must be a
    // decision made with that fact in view, not a default nobody noticed.
    const noFace = approve && !(Number(templates) > 0);
    const faceWarning = noFace
        ? `\n\n⚠ No face has been captured for this person yet. They will `
          + `not be recognised in any capture until one is added.\n`
        : '';
    // Approving a COACH hands over a whole centre, and on this page it sits in
    // a table of ordinary rows where the habit is to click through. Typing
    // breaks that habit; an athlete gets a plain confirm.
    if (approve && role === 'coach') {
        const typed = window.prompt(
            `Approving ${name} as a COACH.\n\nThey will see every athlete at their `
            + `centre, take attendance, and approve athletes themselves.${faceWarning}\n`
            + `Type APPROVE to confirm.`, '');
        if ((typed || '').trim().toUpperCase() !== 'APPROVE') return;
    } else if (approve) {
        if (!window.confirm(`Approve ${name}? They will be able to sign in and be `
                            + `recognised in a capture.${faceWarning}`)) return;
    } else if (!window.confirm(`Reject ${name}? They stay unable to sign in.`)) {
        return;
    }
    try {
        const fd = new FormData();
        fd.append('approve', approve ? 'true' : 'false');
        await api.postForm(`/api/approvals/${id}`, fd);
        showToast(approve ? 'Approved' : 'Rejected',
                  approve ? `${name} can now sign in.` : `${name} was rejected.`,
                  approve ? 'success' : 'info');
        renderUsersPage();
    } catch (err) {
        showToast('Could not do that', (err && err.message) || 'Try again.', 'error');
    }
}

async function deleteUserAccount(id, name, role) {
    // Two steps for a coach or an admin, one for an athlete. Deleting an
    // account now deletes the PERSON too - Directory entry, face, photos and
    // attendance - because a login removed while its face stayed on file left
    // that person matchable and blocked them from ever registering again.
    if (role === 'coach' || role === 'super_admin') {
        const typed = window.prompt(
            `Delete the ${role === 'coach' ? 'coach' : 'super admin'} account "${name}".`
            + `\n\nThis also removes them from the Directory, with their face, photos `
            + `and attendance. This cannot be undone.\n\nType DELETE to confirm.`, '');
        if ((typed || '').trim().toUpperCase() !== 'DELETE') return;
    } else if (!window.confirm(
            `Delete the account "${name}"?\n\nThis also removes them from the `
            + `Directory, with their face, photos and attendance. This cannot be undone.`)) {
        return;
    }
    try {
        // quiet: this handler shows its own message, and two toasts for one
        // failure is one too many.
        await api.delete(`/api/users/${id}`, true);
        showToast('Account deleted', `${name} was removed, along with their Directory entry.`, 'success');
        renderUsersPage();
    } catch (err) {
        showToast('Could not delete', (err && err.message) || 'Try again.', 'error');
    }
}

/* Forgotten-password requests, above the accounts table so they are seen: the
   person asking is locked out until somebody acts. The new password is never
   sent here - the admin approves a PERSON, having checked it is them. */
function passwordResetCard(data) {
    const list = (data && data.requests) || [];
    if (!list.length) return '';
    const btn = 'min-height:30px;padding:0 10px;font-size:12px';
    return `
      <div class="card" style="margin-bottom:16px;border-left:4px solid var(--amber, #d97706)">
        <div class="card-body">
          <h3 style="margin:0 0 4px">Password reset requests (${list.length})</h3>
          <div class="text-xs text-muted" style="margin-bottom:10px">
            Anyone can ask for a new password for any username. Before approving, contact the
            person - by phone, or through their coach - and confirm they asked. Until you approve,
            their old password still works.</div>
          <div class="chart-scroll"><table class="data-table"><thead><tr>
            <th>Name</th><th>Username</th><th>Role</th><th>Centre</th><th>Asked</th><th>Contact note</th><th></th>
          </tr></thead><tbody>${list.map(x => `<tr>
            <td class="cell-primary" data-label="Name">${E(x.full_name)}${x.is_active ? ''
                : ' <span class="badge badge-red">disabled</span>'}</td>
            <td class="font-mono text-sm" data-label="Username">${E(x.username)}</td>
            <td data-label="Role">${E(roleShort(x.role))}</td>
            <td data-label="Centre">${E(x.centre_name || '-')}</td>
            <td class="text-sm text-muted" data-label="Asked">${E((x.requested_at || '').replace('T', ' '))}
              ${Number(x.recent_requests) > 1 ? `<div class="text-xs" style="color:var(--red)">
                ${Number(x.recent_requests)} requests this week</div>` : ''}</td>
            <td class="text-sm" data-label="Contact">${E(x.note || '-')}
              ${x.phone ? `<div class="text-xs text-muted">On file: ${E(x.phone)}</div>` : ''}</td>
            <td style="white-space:nowrap" data-label="">
              <button class="btn btn-primary" style="${btn}" data-decide-reset="approve"
                data-request-id="${x.id}" data-user-role="${E(x.role)}"
                data-username="${E(x.full_name || x.username)}">Approve</button>
              <button class="btn btn-secondary" style="${btn}" data-decide-reset="reject"
                data-request-id="${x.id}" data-user-role="${E(x.role)}"
                data-username="${E(x.full_name || x.username)}">Reject</button>
            </td></tr>`).join('')}</tbody></table></div>
        </div>
      </div>`;
}

async function decidePasswordReset(id, approve, name, role) {
    if (approve) {
        const warning = `Approve the new password for ${name}?\n\nOnly approve if you have `
            + `confirmed with ${name} that they asked for it. Whoever made this request chose `
            + `the password, so approving a request somebody else made hands them the account.`
            + `\n\n${name} will be signed out everywhere and must sign in with the new password.`;
        // A coach or super admin account reaches far more people than an
        // athlete's, so it takes a typed confirmation - the same rule as
        // approving a coach registration.
        if (role === 'coach' || role === 'super_admin') {
            const typed = window.prompt(`${warning}\n\nType APPROVE to confirm.`, '');
            if ((typed || '').trim().toUpperCase() !== 'APPROVE') return;
        } else if (!window.confirm(warning)) {
            return;
        }
    } else if (!window.confirm(`Reject this request for ${name}? Their password stays as it is.`)) {
        return;
    }
    try {
        const fd = new FormData();
        fd.append('approve', approve ? 'true' : 'false');
        await api.postForm(`/api/password-resets/${id}`, fd, 'POST', true);
        showToast(approve ? 'New password approved' : 'Request rejected',
                  approve ? `${name} can now sign in with the password they chose.`
                          : `${name}'s password was not changed.`,
                  approve ? 'success' : 'info');
    } catch (err) {
        showToast('Could not do that', (err && err.message) || 'Try again.', 'error');
    }
    renderUsersPage();
}

async function renderUsersPage() {
    const root = document.getElementById('users-root');
    root.innerHTML = '<div class="empty-state py-12">Loading accounts...</div>';
    let u, c, r;
    try {
        [u, c, r] = await Promise.all([
            api.get('/api/users'), api.get('/api/centres'),
            // Fetched directly, not through api.get: the accounts table should
            // still render, without an error toast, if only this part fails.
            fetch('/api/password-resets').then(x => (x.ok ? x.json() : null)).catch(() => null),
        ]);
    } catch (err) {
        root.innerHTML = `<div class="empty-state py-12">Could not load accounts. `
            + `${E((err && err.message) || 'The server did not answer.')}</div>`;
        return;
    }
    pageState.centres = c.centres;
    root.innerHTML = passwordResetCard(r) + `
      <div class="card"><div class="card-body p-0">
        <table class="data-table"><thead><tr>
          <th>Name</th><th>Username</th><th>Role</th><th>Centre</th><th>Last sign-in</th><th>Status</th><th></th>
        </tr></thead><tbody>${u.users.map(x => `<tr>
          <td>${E(x.full_name)}</td>
          <td class="font-mono text-sm">${E(x.username)}</td>
          <td><span class="badge ${x.role === 'super_admin' ? 'badge-blue'
              : x.role === 'athlete' ? 'badge-amber' : 'badge-green'}">
            ${E(roleShort(x.role))}</span></td>
          <td>${E(x.centre_name || '-')}</td>
          <td class="text-sm text-muted">${E(x.last_login ? x.last_login.replace('T', ' ') : 'never')}</td>
          <td>${userStatusBadge(x)}
            ${x.status === 'pending' && !(Number(x.templates) > 0) ? `
            <div class="text-xs text-muted" style="margin-top:2px">
              No verified face yet - cannot be approved</div>` : ''}
          </td>
          <td style="white-space:nowrap">
            ${x.status === 'pending' && Number(x.templates) > 0 ? `
            <button class="btn btn-primary" style="min-height:30px;padding:0 10px;font-size:12px"
              data-decide-user="approve" data-user-id="${x.id}" data-user-role="${E(x.role)}"
              data-templates="${Number(x.templates) || 0}"
              data-username="${E(x.full_name || x.username)}">Approve</button>` : ''}
            ${x.status === 'pending' ? `
            <button class="btn btn-secondary" style="min-height:30px;padding:0 10px;font-size:12px"
              data-decide-user="reject" data-user-id="${x.id}" data-user-role="${E(x.role)}"
              data-username="${E(x.full_name || x.username)}">Reject</button>` : ''}
            ${x.status === 'rejected' ? `
            <button class="btn btn-secondary" style="min-height:30px;padding:0 10px;font-size:12px"
              data-reopen-user data-user-id="${x.id}" data-username="${E(x.full_name || x.username)}"
              >Reconsider</button>` : ''}
            <button class="btn btn-secondary" style="min-height:30px;padding:0 10px;font-size:12px"
              onclick="toggleUser(${x.id}, ${!x.is_active})">${x.is_active ? 'Disable' : 'Enable'}</button>
            <!-- dataset, not an interpolated handler. E() is HTML escaping,
                 and this was a JavaScript string context: the HTML parser
                 turns &#39; back into a quote before the JS is parsed, so the
                 escape was not merely weak, it was the wrong kind. -->
            <button class="btn btn-secondary" style="min-height:30px;padding:0 10px;font-size:12px"
              data-reset-password data-user-id="${x.id}" data-username="${E(x.username)}">Reset password</button>
            <button class="btn btn-secondary" style="min-height:30px;padding:0 10px;font-size:12px;color:#dc2626"
              data-delete-user data-user-id="${x.id}" data-user-role="${E(x.role)}"
              data-username="${E(x.full_name || x.username)}">Delete</button>
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
    try {
        const res = await fetch(`/api/users/${id}/active`, { method: 'PATCH', body: fd });
        if (!res.ok) {
            let msg = 'The server refused that change';
            try { msg = (await res.json()).detail || msg; } catch { /* not JSON */ }
            showToast('Not changed', msg, 'error');
            return;
        }
        showToast(active ? 'Account enabled' : 'Account disabled', '', 'success');
    } catch {
        showToast('Not changed', 'Could not reach the server', 'error');
        return;
    }
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

// Delegated, so the users table can be re-rendered freely and no username is
// ever interpolated into a handler string. See the note on the button.
document.addEventListener('click', (e) => {
    if (!e.target.closest) return;
    const reset = e.target.closest('[data-reset-password]');
    if (reset) {
        e.preventDefault();
        return resetUserPassword(reset.dataset.userId, reset.dataset.username || '');
    }
    const del = e.target.closest('[data-delete-user]');
    if (del) {
        e.preventDefault();
        return deleteUserAccount(del.dataset.userId, del.dataset.username || '',
                                 del.dataset.userRole || 'athlete');
    }

    const resetDecision = e.target.closest('[data-decide-reset]');
    if (resetDecision) {
        e.preventDefault();
        return decidePasswordReset(resetDecision.dataset.requestId,
                                   resetDecision.dataset.decideReset === 'approve',
                                   resetDecision.dataset.username || '',
                                   resetDecision.dataset.userRole || 'athlete');
    }
    const decide = e.target.closest('[data-decide-user]');
    if (decide) {
        e.preventDefault();
        return decideUser(decide.dataset.userId,
                          decide.dataset.decideUser === 'approve',
                          decide.dataset.username || '',
                          decide.dataset.userRole || 'athlete',
                          decide.dataset.templates);
    }
    const reopen = e.target.closest('[data-reopen-user]');
    if (reopen) {
        e.preventDefault();
        return reopenUser(reopen.dataset.userId, reopen.dataset.username || '');
    }
    const rotate = e.target.closest('[data-rotate-code]');
    if (rotate) {
        e.preventDefault();
        return rotateJoinCode(rotate.dataset.centreId);
    }
});

async function rotateJoinCode(centreId) {
    if (!window.confirm(
        'Issue a new coach registration code?\n\nThe current one stops working '
        + 'straight away, so anyone part-way through registering will have to '
        + 'start again with the new code.')) return;
    try {
        const r = await api.postForm(`/api/centres/${centreId}/join-code`, new FormData());
        showToast('New code issued', r.coach_join_code, 'success');
        openCentreDetail(centreId);
    } catch (err) {
        showToast('Could not do that', (err && err.message) || 'Try again.', 'error');
    }
}


/* ==========================================================================
   Reports - every upload (with its photo) and every attendance record over a
   date range, and the totals per centre. Nothing is cut to a top N.
   ========================================================================== */

const reportState = { data: null };

const UPLOAD_KIND_LABEL = { group: 'Group photo', single: 'Single photo', self: 'Self-mark' };
const pct = (x, digits = 0) => x == null ? '-' : (x * 100).toFixed(digits) + '%';

async function initReportsPage() {
    const from = document.getElementById('rp-from');
    const to = document.getElementById('rp-to');
    const today = localISODate();
    const back = (n) => { const d = new Date(); d.setDate(d.getDate() - n); return localISODate(d); };
    to.value = today;
    from.value = back(6);

    // A coach's report is their own centre; the server pins it either way.
    const centreSel = document.getElementById('rp-centre');
    if (isSuperAdmin()) {
        try {
            const c = await api.get('/api/centres');
            c.centres.forEach(x => centreSel.add(new Option(`${x.name} (${x.code})`, x.id)));
        } catch { /* the report still works for all centres */ }
    } else {
        document.getElementById('rp-centre-wrap').style.display = 'none';
    }

    ['rp-from', 'rp-to', 'rp-centre', 'rp-kind'].forEach(id =>
        document.getElementById(id).addEventListener('change', reportLoad));
    document.querySelectorAll('[data-rp-range]').forEach(b => b.addEventListener('click', () => {
        to.value = today;
        from.value = back(parseInt(b.dataset.rpRange, 10));
        reportLoad();
    }));
    document.getElementById('rp-export').addEventListener('click', () => {
        window.location.href = '/api/reports/export?' + reportQuery(false);
    });
    await reportLoad();
}

function reportQuery(withKind = true) {
    const p = new URLSearchParams();
    p.set('date_from', document.getElementById('rp-from').value);
    p.set('date_to', document.getElementById('rp-to').value);
    const c = document.getElementById('rp-centre').value;
    if (c) p.set('centre_id', c);
    const k = document.getElementById('rp-kind').value;
    if (withKind && k) p.set('kind', k);
    return p.toString();
}

async function reportLoad() {
    const meta = document.getElementById('rp-meta');
    if (meta) meta.textContent = 'Loading…';
    let d;
    try {
        const res = await fetch('/api/reports?' + reportQuery());
        d = await res.json();
        if (!res.ok) throw new Error(d.detail || 'Could not load the report');
    } catch (err) {
        if (meta) meta.textContent = (err && err.message) || 'Could not load the report';
        return;
    }
    reportState.data = d;
    if (meta) meta.textContent = d.date_from === d.date_to
        ? `Showing ${d.date_from}` : `Showing ${d.date_from} to ${d.date_to}`;

    const t = d.totals;
    const tile = (label, value, sub) => `<div class="stat-card">
        <div class="stat-header">${E(label)}</div>
        <div class="stat-value">${value}</div>
        ${sub ? `<div class="text-xs text-muted" style="margin-top:4px">${sub}</div>` : ''}</div>`;
    document.getElementById('rp-tiles').innerHTML =
          tile('Attendance marked', t.confirmed,
               t.drafts ? `+${t.drafts} waiting for the register to be submitted` : 'confirmed records')
        + tile('Uploads', t.uploads,
               `${t.group} group &middot; ${t.single} single &middot; ${t.self} self-mark`)
        + tile('Faces recognised', t.faces_found ? `${t.faces_recognised} / ${t.faces_found}` : '-',
               t.recognised_rate == null ? 'no uploads in this range'
                   : `${pct(t.recognised_rate)} of the faces found in the photos`)
        + tile('Centres marking', d.centres.filter(c => c.confirmed || c.drafts).length,
               `of ${d.centres.length} listed`);

    reportDrawCentres(d);
    reportDrawUploads(d);
    reportDrawRecords(d);
}

function reportDrawCentres(d) {
    const host = document.getElementById('rp-centres');
    if (!isSuperAdmin() && d.centres.length <= 1) { host.innerHTML = ''; return; }
    const rows = d.centres.map(c => {
        const rate = c.faces_found ? c.faces_recognised / c.faces_found : null;
        return `<tr data-rp-centre="${c.centre_id}" style="cursor:pointer" title="Show only this centre">
            <td class="cell-primary"><div style="font-weight:600">${E(c.centre_name)}</div>
                <div class="text-xs text-muted font-mono">${E(c.centre_code)}${c.is_demo ? ' &middot; demo' : ''}</div></td>
            <td data-label="Marked"><strong>${c.confirmed}</strong>${c.drafts ? ` <span class="text-xs text-muted">+${c.drafts} draft</span>` : ''}</td>
            <td data-label="People">${c.people}</td>
            <td data-label="Days">${c.days}</td>
            <td data-label="Group">${c.uploads.group}</td>
            <td data-label="Single">${c.uploads.single}</td>
            <td data-label="Self-mark">${c.uploads.self}</td>
            <td data-label="Recognised">${c.faces_found ? `${c.faces_recognised}/${c.faces_found} (${pct(rate)})` : '-'}</td>
            <td data-label="No face scan">${c.no_face_scan ? `<span class="badge badge-amber">${c.no_face_scan}</span>` : '0'}</td>
        </tr>`;
    }).join('');
    host.innerHTML = `<div class="card" style="margin-bottom:16px">
        <div class="card-header"><h3 class="card-title">Attendance by centre</h3></div>
        <div class="card-body p-0"><div class="data-table-wrapper"><table class="data-table">
            <thead><tr><th>Centre</th><th>Marked</th><th>People</th><th>Days</th>
                <th>Group</th><th>Single</th><th>Self-mark</th><th>Faces recognised</th><th>No face scan</th></tr></thead>
            <tbody>${rows}</tbody></table></div></div></div>`;
    host.querySelectorAll('[data-rp-centre]').forEach(tr => tr.addEventListener('click', () => {
        const sel = document.getElementById('rp-centre');
        if (!sel || !isSuperAdmin()) return;
        sel.value = tr.dataset.rpCentre;
        reportLoad();
    }));
}

function reportMatchChips(matches) {
    if (!matches.length) return '<span class="text-xs text-muted">Nobody recognised</span>';
    return matches.map(m => `<span class="badge ${m.already ? 'badge-blue' : 'badge-green'}"
            title="${m.already ? 'Already on the register - recognised again' : 'Marked present by this photo'}"
            style="margin:0 4px 4px 0">${E(m.name || 'Unknown')} &middot; ${pct(m.confidence)}${m.already ? ' (already)' : ''}</span>`).join('');
}

function reportDrawUploads(d) {
    const host = document.getElementById('rp-uploads');
    const cards = d.uploads.map((u, i) => {
        const when = (u.created_at || '').replace('T', ' ').slice(0, 16);
        const faces = u.faces_detected || 0;
        const img = u.image_url
            ? `<button type="button" data-rp-upload="${i}" style="display:block;width:100%;padding:0;border:0;background:var(--bg-subtle);cursor:zoom-in">
                   <img src="${u.image_url}" loading="lazy" alt="Attendance photo"
                        style="display:block;width:100%;aspect-ratio:4/3;object-fit:cover"></button>`
            : `<div style="aspect-ratio:4/3;display:flex;align-items:center;justify-content:center;background:var(--bg-subtle)"
                    class="text-xs text-muted">Older capture - no photo</div>`;
        return `<div class="card" style="overflow:hidden;margin:0">
            ${img}
            <div style="padding:12px">
                <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:6px">
                    <span class="badge ${u.kind === 'group' ? 'badge-blue' : u.kind === 'self' ? 'badge-amber' : 'badge-green'}">${E(UPLOAD_KIND_LABEL[u.kind] || 'Older capture')}</span>
                    <span class="text-xs text-muted">${E(when)}</span>
                </div>
                <div class="text-sm" style="font-weight:600">${E(u.centre_name || '-')}</div>
                <div class="text-xs text-muted" style="margin-bottom:6px">${E(u.uploaded_by_name || u.coach_name || '')}${u.register_status === 'submitted' ? '' : ' &middot; register not submitted yet'}</div>
                <div class="text-sm" style="margin-bottom:6px">Recognised <strong>${u.recognised || 0} of ${faces}</strong> face${faces === 1 ? '' : 's'}${u.recognised_rate != null ? ` (${pct(u.recognised_rate)})` : ''}</div>
                <div>${reportMatchChips(u.matches)}</div>
            </div></div>`;
    }).join('');
    host.innerHTML = `<div class="card" style="margin-bottom:16px">
        <div class="card-header"><h3 class="card-title">Uploads (${d.uploads.length})</h3></div>
        <div class="card-body">${d.uploads.length
            ? `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px">${cards}</div>`
            : '<div class="empty-state">No photos were uploaded in this range. Photos are kept from this update on - earlier scans were not saved.</div>'}
        </div></div>`;
    host.querySelectorAll('[data-rp-upload]').forEach(b => b.addEventListener('click', () =>
        reportOpenUpload(d.uploads[parseInt(b.dataset.rpUpload, 10)])));
}

function reportOpenUpload(u) {
    if (!u || !u.image_url) return;
    const faces = u.faces_detected || 0;
    openModal(`${UPLOAD_KIND_LABEL[u.kind] || 'Upload'} - ${u.centre_name || ''}`, `
        <img src="${u.image_url}" alt="Attendance photo" style="display:block;width:100%;border-radius:8px;margin-bottom:12px">
        <div class="text-sm" style="margin-bottom:4px">${E((u.created_at || '').replace('T', ' ').slice(0, 16))}
            &middot; ${E(u.uploaded_by_name || u.coach_name || '')}</div>
        <div class="text-sm" style="margin-bottom:8px">Recognised <strong>${u.recognised || 0} of ${faces}</strong>
            face${faces === 1 ? '' : 's'} &middot; ${u.newly_marked} newly marked present</div>
        <div>${reportMatchChips(u.matches)}</div>
        <div class="text-xs text-muted" style="margin-top:10px">"Recognised" is faces matched to somebody on
            the roster, out of faces found in the photo. It is not checked against who was really there.</div>`,
        `<button class="btn btn-secondary" onclick="closeModal()">Close</button>`);
}

function reportHow(r) {
    if (r.capture_kind === 'group') return 'Group photo';
    if (r.capture_kind === 'single') return 'Single photo';
    return { recognised: 'Face scan', self_marked: 'Self-mark', coach_added: 'Ticked by hand',
             late_added: 'Added late', late_approved: 'Added late' }[r.origin] || 'Earlier method';
}

function reportDrawRecords(d) {
    const host = document.getElementById('rp-records');
    const rows = d.records.map(r => `<tr>
        <td data-label="Date" class="font-mono">${E(r.date)} <span class="text-muted">${E(r.time || '')}</span></td>
        <td class="cell-primary"><div style="font-weight:600">${E(r.name)}</div>
            <div class="text-xs text-muted font-mono">${E(r.roll_no || '')}</div></td>
        <td data-label="Centre" class="text-muted">${E(r.centre_name || '-')}</td>
        <td data-label="How">${E(reportHow(r))}</td>
        <td data-label="Match" class="font-mono">${r.confidence == null
            ? '<span class="badge badge-amber">No face scan</span>' : pct(r.confidence, 1)}</td>
        <td data-label="Status">${r.status === 'confirmed' ? '<span class="badge badge-green">Confirmed</span>'
                                                           : '<span class="badge badge-blue">Not submitted</span>'}</td>
        <td data-label="Photo">${r.image_url
            ? `<img src="${r.image_url}" loading="lazy" alt="" data-rp-photo="${E(r.image_url)}"
                    style="width:44px;height:44px;border-radius:6px;object-fit:cover;cursor:zoom-in">` : '-'}</td>
    </tr>`).join('');
    host.innerHTML = `<div class="card" style="margin-bottom:16px">
        <div class="card-header"><h3 class="card-title">All attendance records (${d.records.length})</h3></div>
        <div class="card-body p-0">${d.records.length
            ? `<div class="data-table-wrapper"><table class="data-table">
                <thead><tr><th>Date</th><th>Name</th><th>Centre</th><th>How</th><th>Match</th><th>Status</th><th>Photo</th></tr></thead>
                <tbody>${rows}</tbody></table></div>`
            : '<div class="empty-state py-12">No attendance in this range.</div>'}</div></div>`;
    host.querySelectorAll('[data-rp-photo]').forEach(img => img.addEventListener('click', () =>
        openModal('Attendance photo', `<img src="${img.dataset.rpPhoto}" alt="" style="display:block;width:100%;border-radius:8px">`,
                  `<button class="btn btn-secondary" onclick="closeModal()">Close</button>`)));
}
