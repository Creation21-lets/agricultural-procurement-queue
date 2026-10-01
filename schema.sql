-- Fasal Token database schema (SQLite).
-- Safe to run repeatedly: every object is CREATE ... IF NOT EXISTS.

PRAGMA foreign_keys = ON;

-- Procurement centres run by the organisation.
CREATE TABLE IF NOT EXISTS centres (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  code      TEXT    NOT NULL UNIQUE,                 -- 3 letters, used in tokens, e.g. JAM
  name      TEXT    NOT NULL,
  district  TEXT    NOT NULL,
  capacity  INTEGER NOT NULL CHECK (capacity BETWEEN 8 AND 400),  -- bookings per day
  is_open   INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0,1))
);

-- Crop categories, with the sample rate and moisture limit.
CREATE TABLE IF NOT EXISTS crops (
  id         TEXT    PRIMARY KEY,                    -- slug, e.g. wheat
  name       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  rate       REAL    NOT NULL CHECK (rate > 0),      -- Rs per 40 kg (1 maund)
  max_moist  REAL    NOT NULL CHECK (max_moist > 0),
  active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
);

CREATE TABLE IF NOT EXISTS farmers (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL,
  phone          TEXT NOT NULL,                      -- display form, 0300-1234567
  phone_digits   TEXT NOT NULL UNIQUE,               -- 03001234567, used for sign-in
  cnic           TEXT NOT NULL DEFAULT '',
  village        TEXT NOT NULL,
  district       TEXT NOT NULL,
  password_hash  TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  joined         TEXT NOT NULL                       -- YYYY-MM-DD
);

-- Centre staff, quality inspectors and administrators.
CREATE TABLE IF NOT EXISTS employees (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  emp_id         TEXT NOT NULL UNIQUE COLLATE NOCASE, -- STAFF-101, INSP-204, ADMIN-001
  name           TEXT NOT NULL,
  role           TEXT NOT NULL CHECK (role IN ('staff','inspector','admin')),
  title          TEXT,
  centre_id      INTEGER REFERENCES centres(id),      -- NULL for admins
  password_hash  TEXT NOT NULL,
  active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
);

-- One row per delivery. The columns follow the delivery's life:
-- booked -> checked_in -> inspection -> approved -> unloaded -> completed
-- (or rejected / cancelled).
CREATE TABLE IF NOT EXISTS bookings (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  farmer_id     INTEGER NOT NULL REFERENCES farmers(id),
  crop_id       TEXT    NOT NULL REFERENCES crops(id),
  centre_id     INTEGER NOT NULL REFERENCES centres(id),
  date          TEXT    NOT NULL,                     -- YYYY-MM-DD
  slot          INTEGER NOT NULL CHECK (slot BETWEEN 0 AND 7),
  qty           INTEGER NOT NULL CHECK (qty > 0),     -- expected kg
  token         TEXT    NOT NULL,                     -- JAM-007
  token_seq     INTEGER NOT NULL,                     -- the 7 in JAM-007, per centre per day
  status        TEXT    NOT NULL DEFAULT 'booked'
                CHECK (status IN ('booked','checked_in','inspection','approved','unloaded','completed','rejected','cancelled')),
  created_at    INTEGER NOT NULL,                     -- epoch milliseconds
  -- queue
  seq           INTEGER,                              -- position key while checked in
  check_in_at   INTEGER,
  -- weighing (staff)
  gross         INTEGER,
  tare          INTEGER,
  net           INTEGER,
  visual        TEXT,
  staff_note    TEXT,
  weighed_at    INTEGER,
  wait_min      INTEGER,
  inspector_id  INTEGER REFERENCES employees(id),
  -- quality (inspector)
  q_grade       TEXT CHECK (q_grade IN ('A','B','C')),
  q_moisture    REAL,
  q_foreign     REAL,
  q_damaged     REAL,
  q_remarks     TEXT,
  q_decision    TEXT CHECK (q_decision IN ('approve','reject')),
  q_at          INTEGER,
  -- unloading, payment, receipt
  unloaded_at   INTEGER,
  completed_at  INTEGER,
  pay_amount    INTEGER,
  pay_rate      REAL,
  pay_status    TEXT CHECK (pay_status IN ('Processing','Paid')),
  receipt_no    TEXT UNIQUE,
  UNIQUE (centre_id, date, token_seq)
);
CREATE INDEX IF NOT EXISTS idx_bookings_centre_date ON bookings (centre_id, date);
CREATE INDEX IF NOT EXISTS idx_bookings_farmer      ON bookings (farmer_id);
CREATE INDEX IF NOT EXISTS idx_bookings_inspector   ON bookings (inspector_id, status);

-- Audit trail: who did what to which booking, and when.
CREATE TABLE IF NOT EXISTS booking_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id  INTEGER NOT NULL REFERENCES bookings(id),
  event       TEXT    NOT NULL,
  actor_role  TEXT,
  actor_id    INTEGER,
  at          INTEGER NOT NULL,
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_booking ON booking_events (booking_id);
