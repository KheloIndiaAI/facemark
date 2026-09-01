/* Login gate and role-aware chrome.
 *
 * The server is the actual authority - every protected endpoint checks the
 * session cookie. This file only decides what to *show*, so hiding a nav item
 * is a convenience, never the security boundary.
 */

const session = { user: null };

function isSuperAdmin() { return session.user && session.user.role === 'super_admin'; }
function isCoach() { return session.user && session.user.role === 'coach'; }

async function checkSession() {
    try {
        const res = await fetch('/api/auth/me');
        if (!res.ok) return null;
        const data = await res.json();
        session.user = data.user;
        return data.user;
    } catch { return null; }
}

function showLogin(message) {
    document.getElementById('app-shell').classList.add('hidden');
    const gate = document.getElementById('login-gate');
    gate.classList.remove('hidden');
    const err = document.getElementById('login-error');
    if (message) { err.textContent = message; err.classList.remove('hidden'); }
    else { err.classList.add('hidden'); }
    setTimeout(() => document.getElementById('login-username')?.focus(), 60);
}

function hideLogin() {
    document.getElementById('login-gate').classList.add('hidden');
    document.getElementById('app-shell').classList.remove('hidden');
}

async function doLogin(ev) {
    if (ev) ev.preventDefault();
    const btn = document.getElementById('login-submit');
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;
    if (!username || !password) return showLogin('Enter a username and password');

    btn.disabled = true;
    btn.textContent = 'Signing in...';
    try {
        const fd = new FormData();
        fd.append('username', username);
        fd.append('password', password);
        const res = await fetch('/api/auth/login', { method: 'POST', body: fd });
        const data = await res.json();
        if (!res.ok) {
            showLogin(data.detail || 'Sign in failed');
            return;
        }
        session.user = data.user;
        document.getElementById('login-password').value = '';
        hideLogin();
        applyRoleChrome();
        await initApp();
        // Signing in lands on Mark Attendance, the job the app is opened to do.
        // Set here as well as in the router: this assignment overrides whatever
        // default handleRoute() would have picked, so changing one without the
        // other silently keeps the old landing page.
        window.location.hash = '#/mark';
        handleRoute();
        showToast('Welcome', `Signed in as ${data.user.full_name}`, 'success');
    } catch {
        showLogin('Could not reach the server');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Sign in';
    }
}

async function doLogout() {
    try { await fetch('/api/auth/logout', { method: 'POST' }); } catch { /* sign out locally anyway */ }
    session.user = null;
    showLogin();
}

/* Show/hide nav by role and stamp the identity chip. */
function applyRoleChrome() {
    const u = session.user;
    if (!u) return;

    document.querySelectorAll('[data-role-only]').forEach(el => {
        const allowed = el.getAttribute('data-role-only').split(',').map(s => s.trim());
        el.classList.toggle('hidden', !allowed.includes(u.role));
    });

    const chip = document.getElementById('user-chip');
    if (chip) {
        chip.innerHTML = `
            <div class="user-chip-avatar">${Charts.esc(u.full_name.charAt(0).toUpperCase())}</div>
            <div class="user-chip-text">
                <div class="user-chip-name">${Charts.esc(u.full_name)}</div>
                <div class="user-chip-role">${u.role === 'super_admin' ? 'Super Admin'
                    : 'Coach - ' + Charts.esc(u.centre_name || 'unassigned')}</div>
            </div>`;
    }
    const badge = document.getElementById('role-badge');
    if (badge) {
        badge.textContent = u.role === 'super_admin' ? 'Super Admin' : 'Coach';
        badge.className = 'badge ' + (u.role === 'super_admin' ? 'badge-blue' : 'badge-green');
    }
}

/* Any 401 from anywhere drops straight back to the login gate. */
function handleUnauthorized() {
    session.user = null;
    showLogin('Your session expired. Sign in again.');
}

async function openPasswordModal() {
    openModal('Change password', `
        <div class="form-group">
            <label class="form-label">Current password</label>
            <input type="password" id="pw-current" class="form-input" autocomplete="current-password">
        </div>
        <div class="form-group">
            <label class="form-label">New password (min 6 characters)</label>
            <input type="password" id="pw-new" class="form-input" autocomplete="new-password">
        </div>
        <div class="form-group">
            <label class="form-label">Confirm new password</label>
            <input type="password" id="pw-confirm" class="form-input" autocomplete="new-password">
        </div>`,
        `<button class="btn btn-secondary" onclick="closeModal()">Cancel</button>
         <button class="btn btn-primary" onclick="submitPasswordChange()">Update password</button>`);
}

async function submitPasswordChange() {
    const cur = document.getElementById('pw-current').value;
    const nw = document.getElementById('pw-new').value;
    const cf = document.getElementById('pw-confirm').value;
    if (nw !== cf) return showToast('Error', 'The new passwords do not match', 'error');
    if (nw.length < 6) return showToast('Error', 'Use at least 6 characters', 'error');
    const fd = new FormData();
    fd.append('current_password', cur);
    fd.append('new_password', nw);
    try {
        await api.postForm('/api/auth/password', fd);
        closeModal();
        showToast('Password changed', 'Sign in again with your new password', 'success');
        setTimeout(doLogout, 1200);
    } catch { /* api layer already surfaced the error */ }
}
