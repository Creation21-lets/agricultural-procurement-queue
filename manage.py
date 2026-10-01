"""Management commands for Fasal Token.

  python manage.py demo [--reset]           load sample data (4 centres, 12 farmers, 10 days of history)
  python manage.py init                     production start: crops + one admin (needs ADMIN_PASSWORD)
  python manage.py add-employee --role staff --emp-id STAFF-105 --name "Ali Raza" --centre 2
  python manage.py set-password --emp-id STAFF-105      (or --phone 0300-1234567 for a farmer)

Run `demo` on the day you want to show it: all dates are relative to today.
"""
import argparse
import getpass
import os
import random
import sys
from datetime import datetime, timedelta

os.environ.setdefault('SECRET_KEY', 'management-script')  # tokens are not issued here
import app as fasal
from werkzeug.security import generate_password_hash

DEMO_PASSWORD = 'demo123'

CENTRES = [
    ('JAM', 'Jamshoro Procurement Centre', 'Jamshoro', 48),
    ('HYD', 'Hyderabad Grain Market Centre', 'Hyderabad', 64),
    ('DAD', 'Dadu Procurement Centre', 'Dadu', 40),
    ('KOT', 'Kotri Storage Centre', 'Jamshoro', 32),
]
CROPS = [  # id, name, Rs per 40 kg, max moisture %
    ('wheat', 'Wheat', 3500, 12), ('rice', 'Rice (paddy)', 3200, 14), ('cotton', 'Seed cotton', 7800, 10),
    ('maize', 'Maize', 2300, 14), ('chickpea', 'Chickpea', 8500, 12),
]
FARMERS = [
    ('Ghulam Mustafa Khaskheli', '0300-1234567', 'Manjhand', 'Jamshoro'), ('Abdul Rehman Memon', '0301-2345671', 'Kotri', 'Jamshoro'),
    ('Muhammad Ali Jatoi', '0302-3456712', 'Sehwan', 'Jamshoro'), ('Sanaullah Kalhoro', '0303-4567123', 'Bhan Syedabad', 'Jamshoro'),
    ('Nazir Ahmed Soomro', '0304-5671234', 'Johi', 'Dadu'), ('Ghulam Rasool Chandio', '0305-6712345', 'Dadu', 'Dadu'),
    ('Imtiaz Ali Shaikh', '0306-7123456', 'Hala', 'Matiari'), ('Mehboob Ali Bhutto', '0307-1234560', 'Tando Allahyar', 'Tando Allahyar'),
    ('Allah Bux Mallah', '0308-2345601', 'Thatta', 'Thatta'), ('Dildar Hussain Leghari', '0309-3456012', 'Tando Muhammad Khan', 'Hyderabad'),
    ('Zulfiqar Ali Brohi', '0310-4560123', 'Kotri', 'Jamshoro'), ('Rahim Bux Talpur', '0311-5601234', 'Hyderabad', 'Hyderabad'),
]
STAFF = [  # emp_id, name, centre number (1-based, in CENTRES order)
    ('STAFF-101', 'Imran Ali Qureshi', 1), ('STAFF-102', 'Kamran Ali Shaikh', 2),
    ('STAFF-103', 'Fayaz Hussain Samejo', 3), ('STAFF-104', 'Rubina Parveen', 4),
]
INSPECTORS = [
    ('INSP-204', 'Bilal Ahmed Siddiqui', 1), ('INSP-205', 'Farah Naz Abbasi', 2),
    ('INSP-206', 'Asif Raza Lashari', 3), ('INSP-207', 'Nasreen Akhtar', 4),
]


def tables_empty(con):
    return con.execute('SELECT COUNT(*) FROM centres').fetchone()[0] == 0 and \
        con.execute('SELECT COUNT(*) FROM employees').fetchone()[0] == 0


def reset_db():
    for suffix in ('', '-wal', '-shm'):
        try:
            os.remove(fasal.DB_PATH + suffix)
        except FileNotFoundError:
            pass
    fasal.init_db()


def insert_crops(con):
    con.executemany('INSERT INTO crops (id, name, rate, max_moist, active) VALUES (?,?,?,?,1)', CROPS)


# ------------------------------------------------------------------ demo bookings
class Demo:
    def __init__(self, con):
        self.con = con
        self.rnd = random.Random(20261001)
        self.today = fasal.today()
        self.code = {}
        self.inspector = {}

    def ri(self, a, b):
        return self.rnd.randint(a, b)

    def day(self, n):
        return fasal.add_days(self.today, n)

    def at(self, date, slot, minute):
        y, m, d = map(int, date.split('-'))
        return int(datetime(y, m, d, 8 + slot, minute, tzinfo=fasal.TZ).timestamp() * 1000)

    def new(self, farmer, crop, centre, qty, date, slot, created):
        con = self.con
        seq = con.execute('SELECT COALESCE(MAX(token_seq),0)+1 FROM bookings WHERE centre_id=? AND date=?', (centre, date)).fetchone()[0]
        cur = con.execute('''INSERT INTO bookings (farmer_id,crop_id,centre_id,date,slot,qty,token,token_seq,status,created_at)
                             VALUES (?,?,?,?,?,?,?,?, 'booked', ?)''',
                          (farmer, crop, centre, date, slot, qty, f'{self.code[centre]}-{seq:03d}', seq, created))
        return cur.lastrowid

    def check_in(self, bid, t):
        c = self.con
        row = c.execute('SELECT centre_id, date FROM bookings WHERE id=?', (bid,)).fetchone()
        seq = c.execute('SELECT COALESCE(MAX(seq),0)+1 FROM bookings WHERE centre_id=? AND date=?', (row[0], row[1])).fetchone()[0]
        c.execute("UPDATE bookings SET status='checked_in', seq=?, check_in_at=? WHERE id=?", (seq, t, bid))

    def weigh(self, bid, t, gross, tare, visual='Clean', note=''):
        c = self.con
        row = c.execute('SELECT centre_id, check_in_at FROM bookings WHERE id=?', (bid,)).fetchone()
        wait = max(0, round((t - row[1]) / 60000))
        c.execute("""UPDATE bookings SET gross=?, tare=?, net=?, visual=?, staff_note=?, weighed_at=?, wait_min=?,
                     inspector_id=?, status='inspection', seq=NULL WHERE id=?""",
                  (gross, tare, gross - tare, visual, note, t, wait, self.inspector[row[0]], bid))

    def inspect(self, bid, t, grade, moisture, foreign, damaged, remarks, decision):
        self.con.execute("""UPDATE bookings SET q_grade=?, q_moisture=?, q_foreign=?, q_damaged=?, q_remarks=?, q_decision=?, q_at=?,
                            status=? WHERE id=?""",
                         (grade, moisture, foreign, damaged, remarks, decision, t,
                          'approved' if decision == 'approve' else 'rejected', bid))

    def unload(self, bid, t):
        self.con.execute("UPDATE bookings SET status='unloaded', unloaded_at=? WHERE id=?", (t, bid))

    def complete(self, bid, t, paid, receipt=False):
        c = self.con
        b = c.execute('SELECT b.*, k.rate AS crop_rate FROM bookings b JOIN crops k ON k.id=b.crop_id WHERE b.id=?', (bid,)).fetchone()
        amount = fasal.pay_amount(b['net'], b['crop_rate'], b['q_grade'] or 'A')
        c.execute("UPDATE bookings SET status='completed', completed_at=?, pay_amount=?, pay_rate=?, pay_status=? WHERE id=?",
                  (t, amount, b['crop_rate'], 'Paid' if paid else 'Processing', bid))
        if receipt:
            c.execute('UPDATE bookings SET receipt_no=? WHERE id=?', ('RC-' + b['date'].replace('-', '') + '-' + b['token'], bid))

    # one past delivery, ending completed / rejected / cancelled
    def past(self, centre, date, farmers, crops, crop_info):
        r = self.rnd
        f = 1 if r.random() < .07 else self.ri(2, len(farmers))
        crop = r.choice(crops)
        slot, qty = self.ri(0, 7), self.ri(8, 60) * 100
        t0 = self.at(date, slot, self.ri(0, 40))
        bid = self.new(f, crop, centre, qty, date, slot, t0 - 86400000)
        roll = r.random()
        if roll < .03:
            self.con.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
            return
        self.check_in(bid, t0)
        base = {1: 24, 2: 38, 3: 14, 4: 20}[centre]
        wait = max(3, round(base + (10 if 1 <= slot <= 3 else 0) + self.ri(-10, 12)))
        tare = self.ri(18, 32) * 100
        net = round(qty * (.92 + r.random() * .16) / 10) * 10
        self.weigh(bid, t0 + wait * 60000, net + tare, tare)
        rej = roll > .93
        maxm = crop_info[crop]
        moist = round(maxm + self.ri(1, 4) + r.random(), 1) if rej else round(max(7, maxm - self.ri(0, 3) - r.random() * .9), 1)
        grade = 'C' if rej else ('A' if r.random() < .5 else ('B' if r.random() < .7 else 'C'))
        self.inspect(bid, t0 + (wait + 12) * 60000, grade, moist, round(.4 + r.random() * 2, 1), round(.3 + r.random() * 2.5, 1),
                     'Moisture is above the allowed limit.' if rej else '', 'reject' if rej else 'approve')
        if rej:
            return
        self.unload(bid, t0 + (wait + 30) * 60000)
        self.complete(bid, t0 + (wait + 40) * 60000, True, receipt=True)

    # one delivery today, advanced to the requested stage
    def now(self, centre, farmer, crop, qty, slot, status, crop_info, ago=90, wait=None, visual='Clean', note='', grade='A', receipt=False):
        now = fasal.now_ms()
        bid = self.new(farmer, crop, centre, qty, self.today, slot, now - 86400000)
        if status == 'booked':
            return
        t0 = now - ago * 60000
        self.check_in(bid, t0)
        if status == 'checked_in':
            return
        wait = wait or self.ri(8, 24)
        tare = self.ri(18, 30) * 100
        net = round(qty * (.95 + self.rnd.random() * .08) / 10) * 10
        self.weigh(bid, t0 + wait * 60000, net + tare, tare, visual, note)
        if status == 'inspection':
            return
        rej = status == 'rejected'
        maxm = crop_info[crop]
        self.inspect(bid, t0 + (wait + 10) * 60000, 'C' if rej else grade, round(maxm + 2.6 if rej else maxm - 1.4, 1), .8, 1.1,
                     'Moisture is well above the limit. Load returned for drying.' if rej else '', 'reject' if rej else 'approve')
        if status in ('approved', 'rejected'):
            return
        self.unload(bid, t0 + (wait + 22) * 60000)
        if status == 'unloaded':
            return
        self.complete(bid, t0 + (wait + 30) * 60000, False, receipt=receipt)

    def free_slot(self, centre, date, capacity):
        cap = fasal.slot_cap(capacity)
        for _ in range(30):
            s = self.ri(0, 7)
            n = self.con.execute("SELECT COUNT(*) FROM bookings WHERE centre_id=? AND date=? AND slot=? AND status!='cancelled'",
                                 (centre, date, s)).fetchone()[0]
            if n < cap:
                return s
        return s


def cmd_demo(args):
    if args.reset:
        reset_db()
    con = fasal.connect()
    if not tables_empty(con):
        sys.exit('The database already has data. Use "python manage.py demo --reset" to wipe it and reload the demo.')
    pw = generate_password_hash(DEMO_PASSWORD)
    today = fasal.today()
    con.execute('BEGIN')
    con.executemany('INSERT INTO centres (code,name,district,capacity,is_open) VALUES (?,?,?,?,1)', CENTRES)
    insert_crops(con)
    for i, (name, phone, village, district) in enumerate(FARMERS):
        d = fasal.digits(phone)
        con.execute('''INSERT INTO farmers (name,phone,phone_digits,cnic,village,district,password_hash,status,joined)
                       VALUES (?,?,?,'',?,?,?, 'active', ?)''', (name, phone, d, village, district, pw, fasal.add_days(today, -120 + i * 7)))
    for emp, name, centre in STAFF:
        con.execute("INSERT INTO employees (emp_id,name,role,title,centre_id,password_hash) VALUES (?,?, 'staff', 'Centre staff', ?, ?)", (emp, name, centre, pw))
    demo = Demo(con)
    for emp, name, centre in INSPECTORS:
        cur = con.execute("INSERT INTO employees (emp_id,name,role,title,centre_id,password_hash) VALUES (?,?, 'inspector', 'Quality inspector', ?, ?)", (emp, name, centre, pw))
        demo.inspector[centre] = cur.lastrowid
    con.execute("INSERT INTO employees (emp_id,name,role,title,centre_id,password_hash) VALUES ('ADMIN-001','Sadia Perveen','admin','Regional manager',NULL,?)", (pw,))

    crop_info = {k: m for k, _, _, m in CROPS}
    crops = [k for k, *_ in CROPS]
    farmers = FARMERS
    for n, (code, *_rest) in enumerate(CENTRES, start=1):
        demo.code[n] = code
    caps = {n: c[3] for n, c in enumerate(CENTRES, start=1)}

    for d in range(-10, 0):
        for c in range(1, 5):
            for _ in range(demo.ri(3, 5) if c == 2 else demo.ri(2, 4)):
                demo.past(c, demo.day(d), farmers, crops, crop_info)

    kw = lambda **k: k
    # Jamshoro, today: a story the staff and inspector screens can follow
    plan = [
        (2, 'wheat', 4800, 0, 'completed', kw(ago=190)), (3, 'cotton', 2200, 0, 'completed', kw(ago=170, receipt=True, grade='B')),
        (4, 'rice', 3600, 1, 'rejected', kw(ago=130)), (5, 'wheat', 5200, 1, 'approved', kw(ago=75)),
        (6, 'maize', 3000, 1, 'inspection', kw(ago=60, visual='Minor impurities', note='Some dust and husk. Moisture feels slightly high.')),
        (7, 'wheat', 2600, 2, 'inspection', kw(ago=48)), (8, 'chickpea', 1800, 2, 'checked_in', kw(ago=26)),
        (9, 'wheat', 6000, 2, 'checked_in', kw(ago=17)), (10, 'rice', 4200, 2, 'checked_in', kw(ago=9)),
        (1, 'wheat', 2400, 3, 'booked', {}), (11, 'maize', 2000, 4, 'booked', {}),
        (12, 'cotton', 3400, 4, 'booked', {}), (2, 'rice', 2800, 5, 'booked', {}),
    ]
    for f, crop, qty, slot, status, opts in plan:
        demo.now(1, f, crop, qty, slot, status, crop_info, **opts)
    for c, stages in {2: ['completed', 'completed', 'inspection', 'checked_in', 'checked_in', 'booked', 'booked'],
                      3: ['completed', 'checked_in', 'booked'], 4: ['completed', 'booked']}.items():
        for i, st in enumerate(stages):
            demo.now(c, demo.ri(2, 12), demo.rnd.choice(crops), demo.ri(10, 50) * 100, demo.free_slot(c, today, caps[c]), st,
                     crop_info, ago=150 - i * 20, receipt=(i == 0))
    for d in (1, 2):
        for c in range(1, 5):
            for _ in range(demo.ri(3, 5)):
                date = demo.day(d)
                demo.new(demo.ri(2, 12), demo.rnd.choice(crops), c, demo.ri(10, 50) * 100, date,
                         demo.free_slot(c, date, caps[c]), fasal.now_ms() - 3600000)
    con.execute('COMMIT')
    n = con.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]
    print(f'Demo data loaded ({n} bookings). Every demo account uses the password "{DEMO_PASSWORD}".')
    print('  Farmer     0300-1234567')
    print('  Staff      STAFF-101   Inspector  INSP-204   Admin  ADMIN-001')


def cmd_init(args):
    con = fasal.connect()
    if not tables_empty(con):
        sys.exit('The database already has data, nothing to do.')
    password = os.environ.get('ADMIN_PASSWORD') or getpass.getpass('Choose an admin password (min 8 characters): ')
    if len(password) < 8:
        sys.exit('Admin password must be at least 8 characters.')
    con.execute('BEGIN')
    insert_crops(con)
    con.execute("INSERT INTO employees (emp_id,name,role,title,centre_id,password_hash) VALUES ('ADMIN-001','Administrator','admin','Administrator',NULL,?)",
                (generate_password_hash(password),))
    con.execute('COMMIT')
    print('Created crops (sample rates: edit them in the admin screen) and ADMIN-001.')
    print('Next: sign in as admin, add your centres, then run add-employee for staff and inspectors.')


def cmd_add_employee(args):
    con = fasal.connect()
    if args.role != 'admin':
        if not args.centre or not con.execute('SELECT 1 FROM centres WHERE id=?', (args.centre,)).fetchone():
            sys.exit('--centre must be the number of an existing centre (see the Centres tab in the admin screen).')
    password = os.environ.get('EMPLOYEE_PASSWORD') or getpass.getpass('Password for the new employee (min 8 characters): ')
    if len(password) < 8:
        sys.exit('Password must be at least 8 characters.')
    title = {'staff': 'Centre staff', 'inspector': 'Quality inspector', 'admin': 'Administrator'}[args.role]
    con.execute('INSERT INTO employees (emp_id,name,role,title,centre_id,password_hash) VALUES (?,?,?,?,?,?)',
                (args.emp_id, args.name, args.role, title, args.centre if args.role != 'admin' else None, generate_password_hash(password)))
    print(f'Added {args.role} {args.emp_id}.')


def cmd_set_password(args):
    con = fasal.connect()
    password = getpass.getpass('New password (min 8 characters): ')
    if len(password) < 8:
        sys.exit('Password must be at least 8 characters.')
    h = generate_password_hash(password)
    if args.emp_id:
        n = con.execute('UPDATE employees SET password_hash=? WHERE emp_id=?', (h, args.emp_id)).rowcount
    elif args.phone:
        n = con.execute('UPDATE farmers SET password_hash=? WHERE phone_digits=?', (h, fasal.digits(args.phone))).rowcount
    else:
        sys.exit('Give --emp-id or --phone.')
    print('Password updated.' if n else 'No matching account found.')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('demo')
    d.add_argument('--reset', action='store_true', help='delete the existing database first')
    d.set_defaults(fn=cmd_demo)
    sub.add_parser('init').set_defaults(fn=cmd_init)
    e = sub.add_parser('add-employee')
    e.add_argument('--role', required=True, choices=['staff', 'inspector', 'admin'])
    e.add_argument('--emp-id', required=True)
    e.add_argument('--name', required=True)
    e.add_argument('--centre', type=int)
    e.set_defaults(fn=cmd_add_employee)
    s = sub.add_parser('set-password')
    s.add_argument('--emp-id')
    s.add_argument('--phone')
    s.set_defaults(fn=cmd_set_password)
    args = p.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
