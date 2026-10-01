"""Fasal Token backend: Flask + SQLite.

Run:  python app.py        (development)
The same server also serves ../frontend/index.html at "/", so the browser
talks to the API on the same origin and no CORS setup is needed.
"""
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(path):
    """Tiny .env reader (KEY=VALUE per line) so no extra package is needed."""
    if not os.path.exists(path):
        return
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file(os.path.join(BASE_DIR, '.env'))

# ---------------------------------------------------------------- config
DB_PATH = os.environ.get('DATABASE', os.path.join(BASE_DIR, 'fasal.db'))
FRONTEND_DIR = os.path.abspath(os.environ.get('FRONTEND_DIR', os.path.join(BASE_DIR, '..', 'frontend')))
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print('WARNING: SECRET_KEY is not set. A random one is used, so everyone is signed out '
          'whenever the server restarts. Set SECRET_KEY in backend/.env.')
TOKEN_HOURS = int(os.environ.get('TOKEN_HOURS', '12'))
TZ = timezone(timedelta(hours=float(os.environ.get('TZ_OFFSET_HOURS', '5'))))  # Pakistan: UTC+5, no DST
CORS_ORIGINS = [o.strip() for o in os.environ.get('CORS_ORIGINS', '').split(',') if o.strip()]
DEFAULT_FARMER_PASSWORD = os.environ.get('DEFAULT_FARMER_PASSWORD', 'demo123')
MAX_ACTIVE_BOOKINGS = int(os.environ.get('MAX_ACTIVE_BOOKINGS', '5'))

SLOT_COUNT = 8            # 8-9 am ... 3-4 pm
FIRST_HOUR = 8
MAX_QTY_KG = 30000
BOOKING_DAYS_AHEAD = 6    # today + 6 days, matching the 7 date buttons in the front end
HISTORY_DAYS = 60         # how far back staff / inspectors load bookings
ADMIN_HISTORY_DAYS = 120
FUTURE_DAYS = 14
GRADE_FACTOR = {'A': 1.0, 'B': 0.96, 'C': 0.92}
VISUAL_OPTIONS = ('Clean', 'Minor impurities', 'Visible damage or moisture')
ACTIVE_STATUSES = ('booked', 'checked_in', 'inspection', 'approved', 'unloaded')
PHONE_RE = re.compile(r'^03\d{9}$')

app = Flask(__name__, static_folder=None)
app.json.sort_keys = False
serializer = URLSafeTimedSerializer(SECRET_KEY, salt='fasal-token-auth')
DUMMY_HASH = generate_password_hash('not-a-real-password')


# ---------------------------------------------------------------- time helpers
def now_ms():
    return int(time.time() * 1000)


def local_now():
    return datetime.now(TZ)


def today():
    return local_now().strftime('%Y-%m-%d')


def add_days(date_str, n):
    d = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=n)
    return d.strftime('%Y-%m-%d')


def js_round(x):
    """Round half up, like JavaScript's Math.round (Python's round() rounds half to even)."""
    return int(math.floor(x + 0.5))


# ---------------------------------------------------------------- database
def connect():
    con = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)  # autocommit; we use BEGIN explicitly
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    con.execute('PRAGMA journal_mode = WAL')
    return con


def init_db():
    with open(os.path.join(BASE_DIR, 'schema.sql'), encoding='utf-8') as fh:
        schema = fh.read()
    con = connect()
    try:
        con.executescript(schema)
    finally:
        con.close()


def db():
    if 'db' not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop('db', None)
    if con is not None:
        con.close()


@contextmanager
def tx():
    """Write transaction. BEGIN IMMEDIATE takes the write lock up front so two
    farmers cannot grab the last place in a slot at the same moment."""
    con = db()
    con.execute('BEGIN IMMEDIATE')
    try:
        yield con
    except BaseException:
        con.execute('ROLLBACK')
        raise
    else:
        con.execute('COMMIT')


def q1(sql, *args):
    return db().execute(sql, args).fetchone()


def qa(sql, *args):
    return db().execute(sql, args).fetchall()


def log_event(con, booking_id, event, detail=None):
    con.execute('INSERT INTO booking_events (booking_id, event, actor_role, actor_id, at, detail) VALUES (?,?,?,?,?,?)',
                (booking_id, event, getattr(g, 'role', None), g.me['id'] if getattr(g, 'me', None) else None,
                 now_ms(), detail))


# ---------------------------------------------------------------- errors
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def on_api_error(e):
    return jsonify(error=e.message), e.status


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    app.logger.exception('Unhandled error')
    return jsonify(error='Something went wrong on the server. Please try again.'), 500


@app.after_request
def add_headers(resp):
    origin = request.headers.get('Origin')
    if origin and ('*' in CORS_ORIGINS or origin in CORS_ORIGINS):
        resp.headers['Access-Control-Allow-Origin'] = origin
        resp.headers['Vary'] = 'Origin'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, OPTIONS'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


# ---------------------------------------------------------------- input helpers
def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def text(v, name, min_len=0, max_len=200):
    s = str(v if v is not None else '').strip()
    if len(s) < min_len:
        raise ApiError(f'Enter {name}.')
    if len(s) > max_len:
        raise ApiError(f'{name[:1].upper() + name[1:]} is too long.')
    return s


def num(v, name, lo=None, hi=None, integer=False):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ApiError(f'{name} must be a number.')
    if x != x or x in (float('inf'), float('-inf')):
        raise ApiError(f'{name} must be a number.')
    if lo is not None and x < lo:
        raise ApiError(f'{name} must be at least {lo:g}.')
    if hi is not None and x > hi:
        raise ApiError(f'{name} must be at most {hi:g}.')
    return js_round(x) if integer else x


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def valid_date(v):
    s = str(v or '')
    try:
        datetime.strptime(s, '%Y-%m-%d')
    except ValueError:
        raise ApiError('Choose a valid date.')
    return s


def digits(s):
    return re.sub(r'\D', '', str(s or ''))


def fmt_phone(d):
    return d[:4] + '-' + d[4:]


def slot_cap(capacity):
    return max(1, math.ceil(capacity / SLOT_COUNT))


# ---------------------------------------------------------------- auth
_fails = {}


def throttle_check(key):
    now = time.time()
    recent = [t for t in _fails.get(key, []) if now - t < 900]
    _fails[key] = recent
    if len(recent) >= 8:
        raise ApiError('Too many failed attempts. Please wait 15 minutes and try again.', 429)


def throttle_fail(key):
    _fails.setdefault(key, []).append(time.time())


def issue_token(role, uid):
    return serializer.dumps({'r': role, 'u': uid})


def current_user():
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        raise ApiError('Please sign in.', 401)
    try:
        data = serializer.loads(header[7:], max_age=TOKEN_HOURS * 3600)
    except SignatureExpired:
        raise ApiError('Your session has expired. Please sign in again.', 401)
    except BadSignature:
        raise ApiError('Please sign in.', 401)
    role, uid = data.get('r'), data.get('u')
    if role == 'farmer':
        row = q1('SELECT * FROM farmers WHERE id = ?', uid)
        if not row:
            raise ApiError('Please sign in.', 401)
        if row['status'] != 'active':
            raise ApiError('Your account is suspended. Please contact your procurement centre.', 401)
    else:
        row = q1('SELECT * FROM employees WHERE id = ? AND active = 1', uid)
        if not row or row['role'] != role:
            raise ApiError('Please sign in.', 401)
    return role, row


def auth(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            role, row = current_user()
            if roles and role not in roles:
                raise ApiError('You do not have permission to do that.', 403)
            g.role, g.me = role, row
            return fn(*a, **kw)
        return wrapper
    return deco


# ---------------------------------------------------------------- serializers (JSON shapes the front end expects)
def sid(v):
    return None if v is None else str(v)


def clean(d):
    return {k: v for k, v in d.items() if v is not None}


def farmer_out(r, full=False):
    d = {'id': sid(r['id']), 'name': r['name'], 'phone': r['phone'], 'village': r['village'],
         'district': r['district'], 'status': r['status'], 'joined': r['joined']}
    if full:
        d['cnic'] = r['cnic']
    return d


def employee_out(r):
    return clean({'id': sid(r['id']), 'name': r['name'], 'empId': r['emp_id'], 'role': r['role'],
                  'title': r['title'], 'centreId': sid(r['centre_id'])})


def crop_out(r):
    return {'id': r['id'], 'name': r['name'], 'rate': r['rate'], 'maxMoist': r['max_moist'], 'active': bool(r['active'])}


def centre_out(r):
    d = {'id': sid(r['id']), 'code': r['code'], 'name': r['name'], 'district': r['district'],
         'capacity': r['capacity'], 'open': bool(r['is_open'])}
    if 'queue_now' in r.keys():
        d['queueNow'] = r['queue_now']
        d['loadToday'] = r['load_today']
    return d


def booking_out(r):
    b = {'id': sid(r['id']), 'farmerId': sid(r['farmer_id']), 'cropId': r['crop_id'], 'qty': r['qty'],
         'centreId': sid(r['centre_id']), 'date': r['date'], 'slot': r['slot'], 'token': r['token'],
         'status': r['status'], 'createdAt': r['created_at'], 'seq': r['seq'], 'checkInAt': r['check_in_at'],
         'gross': r['gross'], 'tare': r['tare'], 'net': r['net'], 'visual': r['visual'],
         'staffNote': r['staff_note'], 'weighedAt': r['weighed_at'], 'waitMin': r['wait_min'],
         'inspectorId': sid(r['inspector_id']), 'unloadedAt': r['unloaded_at'],
         'completedAt': r['completed_at'], 'receiptNo': r['receipt_no']}
    if r['q_decision']:
        b['quality'] = {'grade': r['q_grade'], 'moisture': r['q_moisture'], 'foreign': r['q_foreign'],
                        'damaged': r['q_damaged'], 'remarks': r['q_remarks'] or '',
                        'decision': r['q_decision'], 'at': r['q_at']}
    if r['pay_amount'] is not None:
        b['payment'] = {'amount': r['pay_amount'], 'rate': r['pay_rate'], 'status': r['pay_status']}
    return clean(b)


def booking_public(r):
    """What a farmer may see of someone else's booking: enough to compute slot
    availability and queue position, with no name, crop or weights."""
    return clean({'id': sid(r['id']), 'centreId': sid(r['centre_id']), 'date': r['date'], 'slot': r['slot'],
                  'token': r['token'], 'status': r['status'], 'seq': r['seq']})


def centres_out():
    t = today()
    rows = qa('''SELECT c.*,
                   (SELECT COUNT(*) FROM bookings b WHERE b.centre_id = c.id AND b.date = ? AND b.status = 'checked_in') AS queue_now,
                   (SELECT COUNT(*) FROM bookings b WHERE b.centre_id = c.id AND b.date = ? AND b.status != 'cancelled') AS load_today
                 FROM centres c ORDER BY c.id''', t, t)
    return [centre_out(r) for r in rows]


def booking_response(bid, status=200):
    return jsonify(booking=booking_out(q1('SELECT * FROM bookings WHERE id = ?', bid))), status


# ---------------------------------------------------------------- public + bootstrap
@app.get('/api/health')
def health():
    return jsonify(ok=True, today=today())


@app.get('/api/public/centres')
def public_centres():
    return jsonify(centres=centres_out())


@app.get('/api/bootstrap')
@auth()
def bootstrap():
    t = today()
    role, me = g.role, g.me
    crops = [crop_out(r) for r in qa('SELECT * FROM crops ORDER BY rowid')]
    inspectors = [employee_out(r) for r in qa("SELECT * FROM employees WHERE role = 'inspector' AND active = 1 ORDER BY id")]
    out = {'role': role, 'today': t, 'centres': centres_out(), 'crops': crops, 'inspectors': inspectors}

    if role == 'farmer':
        own = qa('SELECT * FROM bookings WHERE farmer_id = ? ORDER BY date, slot, id', me['id'])
        others = qa("""SELECT * FROM bookings WHERE farmer_id != ? AND status != 'cancelled'
                       AND date BETWEEN ? AND ?""", me['id'], t, add_days(t, BOOKING_DAYS_AHEAD))
        out['me'] = farmer_out(me, full=True)
        out['farmers'] = [out['me']]
        out['bookings'] = [booking_out(r) for r in own] + [booking_public(r) for r in others]
        return jsonify(out)

    if role == 'admin':
        lo, hi = add_days(t, -ADMIN_HISTORY_DAYS), add_days(t, FUTURE_DAYS)
        rows = qa('SELECT * FROM bookings WHERE date BETWEEN ? AND ? ORDER BY date, slot, id', lo, hi)
        out['farmers'] = [farmer_out(r, full=True) for r in qa('SELECT * FROM farmers ORDER BY id')]
    else:
        lo, hi = add_days(t, -HISTORY_DAYS), add_days(t, FUTURE_DAYS)
        if role == 'staff':
            rows = qa('SELECT * FROM bookings WHERE centre_id = ? AND date BETWEEN ? AND ? ORDER BY date, slot, id',
                      me['centre_id'], lo, hi)
        else:  # inspector
            rows = qa('SELECT * FROM bookings WHERE inspector_id = ? AND date BETWEEN ? AND ? ORDER BY date, slot, id',
                      me['id'], lo, hi)
        ids = sorted({r['farmer_id'] for r in rows})
        farmers = qa(f"SELECT * FROM farmers WHERE id IN ({','.join('?' * len(ids))})", *ids) if ids else []
        out['farmers'] = [farmer_out(r) for r in farmers]
    out['me'] = employee_out(me)
    out['bookings'] = [booking_out(r) for r in rows]
    return jsonify(out)


# ---------------------------------------------------------------- sign in / sign up
@app.post('/api/auth/farmer/login')
def farmer_login():
    d = body()
    ph = digits(d.get('phone'))
    key = (request.remote_addr, ph)
    throttle_check(key)
    row = q1('SELECT * FROM farmers WHERE phone_digits = ?', ph)
    ok = check_password_hash(row['password_hash'] if row else DUMMY_HASH, str(d.get('password') or ''))
    if not row or not ok:
        throttle_fail(key)
        raise ApiError('Mobile number or password is not correct.', 401)
    if row['status'] != 'active':
        raise ApiError('This account is suspended. Please contact your procurement centre.', 403)
    _fails.pop(key, None)
    return jsonify(token=issue_token('farmer', row['id']), role='farmer', user=farmer_out(row, full=True))


@app.post('/api/auth/farmer/register')
def farmer_register():
    d = body()
    name = text(d.get('name'), 'your full name', 3)
    ph = digits(d.get('phone'))
    cnic = digits(d.get('cnic'))
    village = text(d.get('village'), 'your village or town', 1)
    district = text(d.get('district') or 'Other', 'district', 1, 60)
    pw = str(d.get('password') or '')
    if not PHONE_RE.match(ph):
        raise ApiError('Enter an 11 digit mobile number starting with 03.')
    if cnic and len(cnic) != 13:
        raise ApiError('CNIC must be 13 digits, or leave it empty.')
    if len(pw) < 6 or len(pw) > 128:
        raise ApiError('Password must be at least 6 characters.')
    with tx() as con:
        if con.execute('SELECT 1 FROM farmers WHERE phone_digits = ?', (ph,)).fetchone():
            raise ApiError('An account with this mobile number already exists. Sign in instead.', 409)
        cur = con.execute('''INSERT INTO farmers (name, phone, phone_digits, cnic, village, district, password_hash, status, joined)
                             VALUES (?,?,?,?,?,?,?, 'active', ?)''',
                          (name, fmt_phone(ph), ph, cnic, village, district, generate_password_hash(pw), today()))
        fid = cur.lastrowid
    row = q1('SELECT * FROM farmers WHERE id = ?', fid)
    return jsonify(token=issue_token('farmer', fid), role='farmer', user=farmer_out(row, full=True)), 201


@app.post('/api/auth/staff/login')
def staff_login():
    d = body()
    emp = str(d.get('empId') or '').strip()
    role = d.get('role')
    key = (request.remote_addr, emp.lower())
    throttle_check(key)
    row = q1('SELECT * FROM employees WHERE emp_id = ? AND active = 1', emp)
    ok = check_password_hash(row['password_hash'] if row else DUMMY_HASH, str(d.get('password') or ''))
    if not row or not ok or (role and row['role'] != role):
        throttle_fail(key)
        raise ApiError('Employee ID or password is not correct for this role.', 401)
    _fails.pop(key, None)
    return jsonify(token=issue_token(row['role'], row['id']), role=row['role'], user=employee_out(row))


@app.post('/api/auth/change-password')
@auth()
def change_password():
    d = body()
    new = str(d.get('newPassword') or '')
    if len(new) < 6 or len(new) > 128:
        raise ApiError('New password must be at least 6 characters.')
    if not check_password_hash(g.me['password_hash'], str(d.get('currentPassword') or '')):
        raise ApiError('Current password is not correct.', 403)
    table = 'farmers' if g.role == 'farmer' else 'employees'
    with tx() as con:
        con.execute(f'UPDATE {table} SET password_hash = ? WHERE id = ?', (generate_password_hash(new), g.me['id']))
    return jsonify(ok=True)


# ---------------------------------------------------------------- farmer: bookings
@app.post('/api/bookings')
@auth('farmer')
def create_booking():
    d = body()
    crop = q1('SELECT * FROM crops WHERE id = ?', str(d.get('cropId') or ''))
    if not crop or not crop['active']:
        raise ApiError('This crop is not being accepted right now.')
    centre = q1('SELECT * FROM centres WHERE id = ?', to_int(d.get('centreId')))
    if not centre:
        raise ApiError('Choose a procurement centre.')
    if not centre['is_open']:
        raise ApiError('This centre is closed for bookings.')
    qty = num(d.get('qty'), 'Quantity', lo=1, integer=True)
    if qty > MAX_QTY_KG:
        raise ApiError('Quantity is too large for one booking. Split it into two bookings.')
    slot = num(d.get('slot'), 'Time slot', lo=0, hi=SLOT_COUNT - 1, integer=True)
    date = valid_date(d.get('date'))
    t = today()
    if date < t or date > add_days(t, BOOKING_DAYS_AHEAD):
        raise ApiError('Choose a date from today to the next 6 days.')
    if date == t and local_now().hour >= FIRST_HOUR + slot + 1:
        raise ApiError('That time slot has already passed. Choose a later one.')

    with tx() as con:
        placeholders = ','.join('?' * len(ACTIVE_STATUSES))
        active = con.execute(f'SELECT COUNT(*) FROM bookings WHERE farmer_id = ? AND status IN ({placeholders})',
                             (g.me['id'], *ACTIVE_STATUSES)).fetchone()[0]
        if active >= MAX_ACTIVE_BOOKINGS:
            raise ApiError(f'You already have {active} open bookings. Finish or cancel one before booking another.', 409)
        slot_load = con.execute("SELECT COUNT(*) FROM bookings WHERE centre_id=? AND date=? AND slot=? AND status!='cancelled'",
                                (centre['id'], date, slot)).fetchone()[0]
        if slot_load >= slot_cap(centre['capacity']):
            raise ApiError('That slot just filled up. Choose another.', 409)
        day_load = con.execute("SELECT COUNT(*) FROM bookings WHERE centre_id=? AND date=? AND status!='cancelled'",
                               (centre['id'], date)).fetchone()[0]
        if day_load >= centre['capacity']:
            raise ApiError('This centre is fully booked on that date. Choose another date.', 409)
        seq = con.execute('SELECT COALESCE(MAX(token_seq), 0) + 1 FROM bookings WHERE centre_id = ? AND date = ?',
                          (centre['id'], date)).fetchone()[0]
        cur = con.execute('''INSERT INTO bookings (farmer_id, crop_id, centre_id, date, slot, qty, token, token_seq, status, created_at)
                             VALUES (?,?,?,?,?,?,?,?, 'booked', ?)''',
                          (g.me['id'], crop['id'], centre['id'], date, slot, qty,
                           f"{centre['code']}-{seq:03d}", seq, now_ms()))
        bid = cur.lastrowid
        log_event(con, bid, 'booked')
    return booking_response(bid, 201)


@app.post('/api/bookings/<int:bid>/cancel')
@auth('farmer')
def cancel_booking(bid):
    with tx() as con:
        b = con.execute('SELECT * FROM bookings WHERE id = ? AND farmer_id = ?', (bid, g.me['id'])).fetchone()
        if not b:
            raise ApiError('Booking not found.', 404)
        if b['status'] != 'booked':
            raise ApiError('Only bookings that have not been checked in can be cancelled.', 409)
        con.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (bid,))
        log_event(con, bid, 'cancelled')
    return booking_response(bid)


# ---------------------------------------------------------------- staff: the procurement flow
def get_booking(bid, centre_scope=False):
    b = q1('SELECT * FROM bookings WHERE id = ?', bid)
    if not b:
        raise ApiError('Booking not found.', 404)
    if centre_scope and g.role == 'staff' and b['centre_id'] != g.me['centre_id']:
        raise ApiError('This booking belongs to another centre.', 403)
    return b


def require_status(b, status, message):
    if b['status'] != status:
        raise ApiError(message, 409)


@app.post('/api/bookings/<int:bid>/check-in')
@auth('staff')
def check_in(bid):
    with tx() as con:
        b = get_booking(bid, True)
        require_status(b, 'booked', 'Only farmers with a booked token can be checked in.')
        if b['date'] != today():
            raise ApiError('Check-in opens on the booking date.')
        seq = con.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM bookings WHERE centre_id = ? AND date = ?',
                          (b['centre_id'], b['date'])).fetchone()[0]
        con.execute("UPDATE bookings SET status = 'checked_in', seq = ?, check_in_at = ? WHERE id = ?", (seq, now_ms(), bid))
        log_event(con, bid, 'checked_in')
    return booking_response(bid)


@app.post('/api/bookings/<int:bid>/queue')
@auth('staff')
def queue_move(bid):
    action = body().get('action')
    if action not in ('up', 'down', 'front', 'end'):
        raise ApiError('Unknown queue action.')
    with tx() as con:
        b = get_booking(bid, True)
        require_status(b, 'checked_in', 'Only farmers waiting in the queue can be moved.')
        q = con.execute("SELECT id, seq FROM bookings WHERE centre_id = ? AND date = ? AND status = 'checked_in' ORDER BY seq, id",
                        (b['centre_id'], b['date'])).fetchall()
        i = [r['id'] for r in q].index(bid)
        if action in ('up', 'down'):
            j = i - 1 if action == 'up' else i + 1
            if 0 <= j < len(q):
                con.execute('UPDATE bookings SET seq = ? WHERE id = ?', (q[j]['seq'], q[i]['id']))
                con.execute('UPDATE bookings SET seq = ? WHERE id = ?', (q[i]['seq'], q[j]['id']))
        elif action == 'front' and i > 0:
            con.execute('UPDATE bookings SET seq = ? WHERE id = ?', (q[0]['seq'] - 1, bid))
        elif action == 'end' and i < len(q) - 1:
            con.execute('UPDATE bookings SET seq = ? WHERE id = ?', (q[-1]['seq'] + 1, bid))
        log_event(con, bid, 'queue_' + action)
    return booking_response(bid)


@app.post('/api/bookings/<int:bid>/weigh')
@auth('staff')
def weigh(bid):
    d = body()
    gross = num(d.get('gross'), 'Gross weight', lo=1, hi=200000, integer=True)
    tare = num(d.get('tare'), 'Tare weight', lo=0, hi=200000, integer=True)
    if gross - tare <= 0:
        raise ApiError('Gross weight must be more than tare weight.')
    visual = d.get('visual') if d.get('visual') in VISUAL_OPTIONS else 'Clean'
    note = text(d.get('note'), 'note', 0, 500)
    with tx() as con:
        b = get_booking(bid, True)
        require_status(b, 'checked_in', 'Only farmers in the queue can be weighed.')
        ins = con.execute('''SELECT e.id FROM employees e
                             WHERE e.role = 'inspector' AND e.active = 1 AND e.centre_id = ?
                             ORDER BY (SELECT COUNT(*) FROM bookings x WHERE x.inspector_id = e.id AND x.status = 'inspection'), e.id
                             LIMIT 1''', (b['centre_id'],)).fetchone()
        if not ins:
            raise ApiError('No quality inspector is assigned to this centre yet. Ask an administrator to add one.', 409)
        at = now_ms()
        wait = max(0, js_round((at - (b['check_in_at'] or at)) / 60000))
        con.execute('''UPDATE bookings SET gross=?, tare=?, net=?, visual=?, staff_note=?, weighed_at=?, wait_min=?,
                       inspector_id=?, status='inspection', seq=NULL WHERE id=?''',
                    (gross, tare, gross - tare, visual, note, at, wait, ins['id'], bid))
        log_event(con, bid, 'weighed', f'net {gross - tare} kg')
    return booking_response(bid)


@app.post('/api/bookings/<int:bid>/unload')
@auth('staff')
def unload(bid):
    with tx() as con:
        b = get_booking(bid, True)
        require_status(b, 'approved', 'Only approved loads can be unloaded.')
        con.execute("UPDATE bookings SET status = 'unloaded', unloaded_at = ? WHERE id = ?", (now_ms(), bid))
        log_event(con, bid, 'unloaded')
    return booking_response(bid)


def pay_amount(net, rate, grade):
    return js_round(net / 40 * rate * GRADE_FACTOR[grade])


@app.post('/api/bookings/<int:bid>/complete')
@auth('staff')
def complete(bid):
    with tx() as con:
        b = get_booking(bid, True)
        require_status(b, 'unloaded', 'Confirm unloading before completing the procurement.')
        crop = con.execute('SELECT rate FROM crops WHERE id = ?', (b['crop_id'],)).fetchone()
        amount = pay_amount(b['net'], crop['rate'], b['q_grade'] or 'A')
        con.execute("""UPDATE bookings SET status='completed', completed_at=?, pay_amount=?, pay_rate=?, pay_status='Processing'
                       WHERE id=?""", (now_ms(), amount, crop['rate'], bid))
        log_event(con, bid, 'completed', f'Rs {amount}')
    return booking_response(bid)


@app.post('/api/bookings/<int:bid>/receipt')
@auth('staff')
def receipt(bid):
    with tx() as con:
        b = get_booking(bid, True)
        require_status(b, 'completed', 'A receipt can only be generated for a completed procurement.')
        if not b['receipt_no']:
            con.execute('UPDATE bookings SET receipt_no = ? WHERE id = ?',
                        ('RC-' + b['date'].replace('-', '') + '-' + b['token'], bid))
            log_event(con, bid, 'receipt')
    return booking_response(bid)


@app.post('/api/bookings/<int:bid>/mark-paid')
@auth('staff', 'admin')
def mark_paid(bid):
    with tx() as con:
        b = get_booking(bid, True)
        require_status(b, 'completed', 'Only completed procurements can be marked as paid.')
        con.execute("UPDATE bookings SET pay_status = 'Paid' WHERE id = ?", (bid,))
        log_event(con, bid, 'paid')
    return booking_response(bid)


# ---------------------------------------------------------------- inspector
@app.post('/api/bookings/<int:bid>/inspect')
@auth('inspector')
def inspect(bid):
    d = body()
    grade = d.get('grade')
    if grade not in GRADE_FACTOR:
        raise ApiError('Choose a quality grade.')
    decision = d.get('decision')
    if decision not in ('approve', 'reject'):
        raise ApiError('Choose approve or reject.')
    moisture = round(num(d.get('moisture'), 'Moisture', lo=0, hi=60), 1)
    foreign = round(num(d.get('foreign') or 0, 'Foreign matter', lo=0, hi=100), 1)
    damaged = round(num(d.get('damaged') or 0, 'Damaged grain', lo=0, hi=100), 1)
    remarks = text(d.get('remarks'), 'remarks', 0, 1000)
    if decision == 'reject' and not remarks:
        raise ApiError('Add remarks explaining why the produce is rejected.')
    with tx() as con:
        b = get_booking(bid)
        if b['inspector_id'] != g.me['id']:
            raise ApiError('This load is assigned to another inspector.', 403)
        require_status(b, 'inspection', 'This load is not waiting for inspection.')
        con.execute('''UPDATE bookings SET q_grade=?, q_moisture=?, q_foreign=?, q_damaged=?, q_remarks=?, q_decision=?, q_at=?,
                       status=? WHERE id=?''',
                    (grade, moisture, foreign, damaged, remarks, decision, now_ms(),
                     'approved' if decision == 'approve' else 'rejected', bid))
        log_event(con, bid, decision + 'd')
    return booking_response(bid)


# ---------------------------------------------------------------- admin: farmers, centres, crops
@app.post('/api/farmers')
@auth('admin')
def admin_add_farmer():
    d = body()
    name = text(d.get('name'), 'the farmer\u2019s full name', 3)
    ph = digits(d.get('phone'))
    if not PHONE_RE.match(ph):
        raise ApiError('Enter an 11 digit mobile number starting with 03.')
    village = text(d.get('village'), 'village', 0, 100) or 'Not given'
    with tx() as con:
        if con.execute('SELECT 1 FROM farmers WHERE phone_digits = ?', (ph,)).fetchone():
            raise ApiError('This mobile number is already registered.', 409)
        cur = con.execute('''INSERT INTO farmers (name, phone, phone_digits, cnic, village, district, password_hash, status, joined)
                             VALUES (?,?,?,'',?,?,?, 'active', ?)''',
                          (name, fmt_phone(ph), ph, village, 'Not given', generate_password_hash(DEFAULT_FARMER_PASSWORD), today()))
        fid = cur.lastrowid
    return jsonify(farmer=farmer_out(q1('SELECT * FROM farmers WHERE id = ?', fid), full=True)), 201


@app.patch('/api/farmers/<int:fid>')
@auth('admin')
def admin_patch_farmer(fid):
    status = body().get('status')
    if status not in ('active', 'suspended'):
        raise ApiError('Status must be active or suspended.')
    with tx() as con:
        if not con.execute('SELECT 1 FROM farmers WHERE id = ?', (fid,)).fetchone():
            raise ApiError('Farmer not found.', 404)
        con.execute('UPDATE farmers SET status = ? WHERE id = ?', (status, fid))
    return jsonify(farmer=farmer_out(q1('SELECT * FROM farmers WHERE id = ?', fid), full=True))


def centre_code(con, name):
    code = re.sub(r'[^A-Za-z]', '', name)[:3].upper()
    code = (code + 'XXX')[:3]
    n = 1
    while con.execute('SELECT 1 FROM centres WHERE code = ?', (code,)).fetchone():
        code = code[:2] + str(n % 10)
        n += 1
        if n > 40:
            raise ApiError('Could not create a unique centre code.', 409)
    return code


@app.post('/api/centres')
@auth('admin')
def admin_add_centre():
    d = body()
    name = text(d.get('name'), 'the centre name', 3, 100)
    district = text(d.get('district'), 'district', 0, 60) or 'Not given'
    cap = num(d.get('capacity'), 'Capacity', lo=8, hi=400, integer=True)
    with tx() as con:
        cur = con.execute('INSERT INTO centres (code, name, district, capacity, is_open) VALUES (?,?,?,?,1)',
                          (centre_code(con, name), name, district, cap))
        cid = cur.lastrowid
    return jsonify(centre=centre_out(q1('SELECT * FROM centres WHERE id = ?', cid))), 201


@app.patch('/api/centres/<int:cid>')
@auth('admin')
def admin_patch_centre(cid):
    d = body()
    with tx() as con:
        if not con.execute('SELECT 1 FROM centres WHERE id = ?', (cid,)).fetchone():
            raise ApiError('Centre not found.', 404)
        if 'capacity' in d:
            con.execute('UPDATE centres SET capacity = ? WHERE id = ?',
                        (num(d['capacity'], 'Capacity', lo=8, hi=400, integer=True), cid))
        if 'open' in d:
            con.execute('UPDATE centres SET is_open = ? WHERE id = ?', (1 if d['open'] else 0, cid))
    return jsonify(centre=centre_out(q1('SELECT * FROM centres WHERE id = ?', cid)))


def crop_slug(con, name):
    base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'crop'
    slug, n = base, 2
    while con.execute('SELECT 1 FROM crops WHERE id = ?', (slug,)).fetchone():
        slug = f'{base}-{n}'
        n += 1
    return slug


@app.post('/api/crops')
@auth('admin')
def admin_add_crop():
    d = body()
    name = text(d.get('name'), 'the crop name', 2, 60)
    rate = num(d.get('rate'), 'Sample rate', lo=1)
    moist = num(d.get('maxMoist'), 'Moisture limit', lo=0.5, hi=30)
    with tx() as con:
        if con.execute('SELECT 1 FROM crops WHERE name = ?', (name,)).fetchone():
            raise ApiError('This crop already exists.', 409)
        cid = crop_slug(con, name)
        con.execute('INSERT INTO crops (id, name, rate, max_moist, active) VALUES (?,?,?,?,1)', (cid, name, rate, moist))
    return jsonify(crop=crop_out(q1('SELECT * FROM crops WHERE id = ?', cid))), 201


@app.patch('/api/crops/<cid>')
@auth('admin')
def admin_patch_crop(cid):
    d = body()
    with tx() as con:
        if not con.execute('SELECT 1 FROM crops WHERE id = ?', (cid,)).fetchone():
            raise ApiError('Crop not found.', 404)
        if 'rate' in d:
            con.execute('UPDATE crops SET rate = ? WHERE id = ?', (num(d['rate'], 'Sample rate', lo=1), cid))
        if 'maxMoist' in d:
            con.execute('UPDATE crops SET max_moist = ? WHERE id = ?', (num(d['maxMoist'], 'Moisture limit', lo=0.5, hi=30), cid))
        if 'active' in d:
            con.execute('UPDATE crops SET active = ? WHERE id = ?', (1 if d['active'] else 0, cid))
    return jsonify(crop=crop_out(q1('SELECT * FROM crops WHERE id = ?', cid)))


# ---------------------------------------------------------------- front end
@app.get('/')
def index():
    return send_from_directory(FRONTEND_DIR, 'index.html')


init_db()

if __name__ == '__main__':
    app.run(host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '3000')),
            debug=os.environ.get('FLASK_DEBUG') == '1')
