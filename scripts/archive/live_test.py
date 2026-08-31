import os, requests

BASE = 'http://127.0.0.1:8000'
ADMIN_PW = os.environ.get('FACEMARK_ADMIN_PASSWORD', 'facemark-test-123')

def auth_session():
    r = requests.post(f'{BASE}/api/auth/login',
                      data={'username': 'admin', 'password': ADMIN_PW}, timeout=30)
    r.raise_for_status()
    return r.cookies

cookies = auth_session()
print('Authenticated as admin (session cookie set)')

print('=== TEST 1: UNKNOWN GROUP PHOTO ===')
with open('data/uploads/group_20260820_160116_156.jpg', 'rb') as f:
    r = requests.post(f'{BASE}/api/attendance/process', files={'photo': ('unknown.jpg', f, 'image/jpeg')}, cookies=cookies)
d1 = r.json()
print('Faces:', d1['faces_detected'], '| Recognized:', d1['recognized_count'], '| Unknown:', d1['unknown_count'])
for rec in d1.get('recognized', []):
    print('  FALSE POSITIVE:', rec['name'], 'sim=', rec['similarity'])

print()
print('=== TEST 2: KNOWN CLASS PHOTO ===')
with open('data/uploads/group_20260820_132732_813.jpg', 'rb') as f:
    r = requests.post(f'{BASE}/api/attendance/process', files={'photo': ('class.jpg', f, 'image/jpeg')}, cookies=cookies)
d2 = r.json()
print('Faces:', d2['faces_detected'], '| Recognized:', d2['recognized_count'], '| Unknown:', d2['unknown_count'])
for rec in d2.get('recognized', []):
    name = rec['name']
    sim = rec['similarity']
    raw = rec.get('raw_similarity', '?')
    print('  MATCH: %-15s sim=%.2f%% raw=%s' % (name, sim*100, raw))
for u in d2.get('unknown', []):
    print('  UNKNOWN: face index', u['face_index'])
