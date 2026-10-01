"""End-to-end API tests. Run:  python test_api.py"""
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ['DATABASE'] = os.path.join(_tmp, 'test.db')
os.environ['SECRET_KEY'] = 'test-secret'

import app as fasal  # noqa: E402
import manage  # noqa: E402


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manage.cmd_demo(type('A', (), {'reset': True})())
        cls.c = fasal.app.test_client()

    def call(self, method, path, token=None, json=None):
        h = {'Authorization': 'Bearer ' + token} if token else {}
        r = self.c.open(path, method=method, json=json, headers=h)
        return r.status_code, r.get_json()

    def login_farmer(self, phone='0300-1234567', pw='demo123'):
        return self.call('POST', '/api/auth/farmer/login', json={'phone': phone, 'password': pw})

    def login_emp(self, emp, role, pw='demo123'):
        return self.call('POST', '/api/auth/staff/login', json={'empId': emp, 'password': pw, 'role': role})

    def test_1_auth(self):
        self.assertEqual(self.login_farmer(pw='wrong')[0], 401)
        self.assertEqual(self.login_emp('STAFF-101', 'admin')[0], 401)       # wrong role tab
        self.assertEqual(self.call('GET', '/api/bootstrap')[0], 401)          # no token
        code, d = self.login_farmer()
        self.assertEqual(code, 200)
        self.assertNotIn('password', str(d))

    def test_2_register_validation(self):
        bad = {'name': 'Ali Khan', 'phone': '12345', 'village': 'X', 'password': 'abcdef'}
        self.assertEqual(self.call('POST', '/api/auth/farmer/register', json=bad)[0], 400)
        ok = dict(bad, phone='0333-1112223')
        code, d = self.call('POST', '/api/auth/farmer/register', json=ok)
        self.assertEqual(code, 201)
        self.assertEqual(self.call('POST', '/api/auth/farmer/register', json=ok)[0], 409)

    def test_3_full_flow_and_permissions(self):
        _, f = self.login_farmer()
        _, st = self.login_emp('STAFF-101', 'staff')
        _, ins = self.login_emp('INSP-204', 'inspector')
        _, ad = self.login_emp('ADMIN-001', 'admin')
        ft, stt, it, at = f['token'], st['token'], ins['token'], ad['token']

        boot = self.call('GET', '/api/bootstrap', ft)[1]
        self.assertTrue(all('name' not in b for b in boot['bookings'] if b.get('farmerId') is None))
        centre = next(c for c in boot['centres'] if c['code'] == 'JAM')

        # book for tomorrow, any slot with room
        date = fasal.add_days(fasal.today(), 1)
        code, r = self.call('POST', '/api/bookings', ft, {'cropId': 'wheat', 'qty': 2400, 'centreId': centre['id'], 'date': date, 'slot': 7})
        self.assertEqual(code, 201, r)
        bid = r['booking']['id']
        self.assertTrue(r['booking']['token'].startswith('JAM-'))
        # farmers cannot run staff actions; staff cannot check in before the date
        self.assertEqual(self.call('POST', f'/api/bookings/{bid}/check-in', ft)[0], 403)
        self.assertEqual(self.call('POST', f'/api/bookings/{bid}/check-in', stt)[0], 400)

        # pick up a booking that is already checked in today and walk it through the rest
        sb = self.call('GET', '/api/bootstrap', stt)[1]
        today = fasal.today()
        waiting = [b for b in sb['bookings'] if b['date'] == today and b['status'] == 'checked_in']
        self.assertGreaterEqual(len(waiting), 2)
        first = waiting[0]['id']
        last = waiting[-1]['id']
        self.assertEqual(self.call('POST', f'/api/bookings/{last}/queue', stt, {'action': 'front'})[0], 200)
        sb2 = self.call('GET', '/api/bootstrap', stt)[1]
        q = sorted([b for b in sb2['bookings'] if b['date'] == today and b['status'] == 'checked_in'], key=lambda b: b['seq'])
        self.assertEqual(q[0]['id'], last)

        self.assertEqual(self.call('POST', f'/api/bookings/{last}/weigh', stt, {'gross': 1000, 'tare': 1500})[0], 400)
        code, r = self.call('POST', f'/api/bookings/{last}/weigh', stt, {'gross': 6000, 'tare': 2000, 'visual': 'Clean'})
        self.assertEqual(code, 200, r)
        self.assertEqual(r['booking']['net'], 4000)
        self.assertEqual(r['booking']['status'], 'inspection')

        self.assertEqual(self.call('POST', f'/api/bookings/{last}/inspect', ft, {'grade': 'A', 'moisture': 10, 'decision': 'approve'})[0], 403)
        self.assertEqual(self.call('POST', f'/api/bookings/{last}/inspect', it, {'grade': 'A', 'moisture': 15, 'decision': 'reject'})[0], 400)  # no remarks
        code, r = self.call('POST', f'/api/bookings/{last}/inspect', it, {'grade': 'B', 'moisture': 10.5, 'foreign': 1, 'damaged': 1, 'decision': 'approve'})
        self.assertEqual(code, 200, r)
        self.assertEqual(self.call('POST', f'/api/bookings/{last}/complete', stt)[0], 409)   # must unload first
        self.assertEqual(self.call('POST', f'/api/bookings/{last}/unload', stt)[0], 200)
        code, r = self.call('POST', f'/api/bookings/{last}/complete', stt)
        self.assertEqual(code, 200, r)
        rate = next(c for c in sb['crops'] if c['id'] == r['booking']['cropId'])['rate']
        self.assertEqual(r['booking']['payment']['amount'], fasal.js_round(4000 / 40 * rate * 0.96))
        code, r = self.call('POST', f'/api/bookings/{last}/receipt', stt)
        self.assertTrue(r['booking']['receiptNo'].startswith('RC-'))
        self.assertEqual(self.call('POST', f'/api/bookings/{last}/mark-paid', at)[1]['booking']['payment']['status'], 'Paid')

        # another centre's staff cannot touch it
        _, st2 = self.login_emp('STAFF-102', 'staff')
        self.assertEqual(self.call('POST', f'/api/bookings/{first}/unload', st2['token'])[0], 403)

        # farmer cancels own future booking
        code, r = self.call('POST', f'/api/bookings/{bid}/cancel', ft)
        self.assertEqual(r['booking']['status'], 'cancelled')

    def test_4_booking_rules(self):
        _, f = self.login_farmer('0311-5601234')
        ft = f['token']
        boot = self.call('GET', '/api/bootstrap', ft)[1]
        cid = boot['centres'][0]['id']
        date = fasal.add_days(fasal.today(), 3)
        ok = {'cropId': 'wheat', 'qty': 1000, 'centreId': cid, 'date': date, 'slot': 0}
        self.assertEqual(self.call('POST', '/api/bookings', ft, dict(ok, qty=99999))[0], 400)
        self.assertEqual(self.call('POST', '/api/bookings', ft, dict(ok, slot=9))[0], 400)
        self.assertEqual(self.call('POST', '/api/bookings', ft, dict(ok, date=fasal.add_days(fasal.today(), -1)))[0], 400)
        self.assertEqual(self.call('POST', '/api/bookings', ft, dict(ok, date=fasal.add_days(fasal.today(), 30)))[0], 400)
        self.assertEqual(self.call('POST', '/api/bookings', ft, dict(ok, cropId='nope'))[0], 400)

    def test_5_admin(self):
        _, ad = self.login_emp('ADMIN-001', 'admin')
        at = ad['token']
        _, f = self.login_farmer()
        self.assertEqual(self.call('PATCH', '/api/centres/1', f['token'], {'capacity': 50})[0], 403)
        self.assertEqual(self.call('PATCH', '/api/centres/1', at, {'capacity': 5})[0], 400)
        self.assertEqual(self.call('PATCH', '/api/centres/1', at, {'capacity': 50, 'open': False})[1]['centre']['open'], False)
        self.call('PATCH', '/api/centres/1', at, {'open': True})
        code, r = self.call('POST', '/api/crops', at, {'name': 'Sugarcane', 'rate': 400, 'maxMoist': 20})
        self.assertEqual((code, r['crop']['id']), (201, 'sugarcane'))
        self.assertEqual(self.call('POST', '/api/crops', at, {'name': 'sugarcane', 'rate': 400, 'maxMoist': 20})[0], 409)
        code, r = self.call('POST', '/api/centres', at, {'name': 'Larkana Centre', 'district': 'Larkana', 'capacity': 30})
        self.assertEqual((code, r['centre']['code']), (201, 'LAR'))
        code, r = self.call('POST', '/api/farmers', at, {'name': 'Test Farmer', 'phone': '0344-5556667', 'village': 'Hala'})
        fid = r['farmer']['id']
        self.call('PATCH', f'/api/farmers/{fid}', at, {'status': 'suspended'})
        self.assertEqual(self.login_farmer('0344-5556667')[0], 403)
        # a suspended farmer's existing token stops working
        _, d = self.call('POST', '/api/auth/farmer/login', json={'phone': '0300-1234567', 'password': 'demo123'})
        self.call('PATCH', '/api/farmers/1', at, {'status': 'suspended'})
        self.assertEqual(self.call('GET', '/api/bootstrap', d['token'])[0], 401)
        self.call('PATCH', '/api/farmers/1', at, {'status': 'active'})

    def test_6_public_and_static(self):
        code, d = self.call('GET', '/api/public/centres')
        self.assertEqual(code, 200)
        self.assertIn('queueNow', d['centres'][0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
