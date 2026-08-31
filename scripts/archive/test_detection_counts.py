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

print('=== TEST 1: DELHI MALLAKHAMB (20 FACES) ===')
with open('data/uploads/group_20260820_172605_557.jpg', 'rb') as f:
    r = requests.post(f'{BASE}/api/attendance/process', files={'photo': ('mallakhamb.jpg', f, 'image/jpeg')}, cookies=cookies)
d1 = r.json()
print('Faces detected:', d1.get('faces_detected'), '| Recognized:', d1.get('recognized_count'), '| Unknown:', d1.get('unknown_count'))
for rec in d1.get('recognized', []):
    print('  FALSE POSITIVE:', rec['name'])

print('\n=== TEST 2: CLASSROOM GROUP (13 STUDENTS) ===')
with open('data/uploads/group_20260820_132732_813.jpg', 'rb') as f:
    r = requests.post(f'{BASE}/api/attendance/process', files={'photo': ('class.jpg', f, 'image/jpeg')}, cookies=cookies)
d2 = r.json()
print('Faces detected:', d2.get('faces_detected'), '| Recognized:', d2.get('recognized_count'), '| Unknown:', d2.get('unknown_count'))
for rec in d2.get('recognized', []):
    print('  MATCH:', rec['name'], '(' + str(int(rec['similarity']*100)) + '%)')
