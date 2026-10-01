# Fasal Token: backend, database and connected front end

```
fasal-token/
├── backend/
│   ├── app.py            Flask API server (also serves the front end at "/")
│   ├── schema.sql        SQLite database schema
│   ├── manage.py         load demo data, create admin, add staff/inspectors, reset passwords
│   ├── test_api.py       automated API tests
│   ├── requirements.txt  just Flask
│   └── .env.example      settings (copy to .env)
└── frontend/
    └── index.html        your front end, now talking to the API
```

## 1. Run it (5 minutes)

You need Python 3.10 or newer.

```bash
cd backend
python -m venv venv
venv\Scripts\activate            # Windows        (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
python manage.py demo            # creates fasal.db with sample data
python app.py                    # starts the server
```

Open **http://127.0.0.1:3000**. Demo logins (password `demo123` for all):

| Role | ID |
|---|---|
| Farmer | 0300-1234567 |
| Centre staff (Jamshoro) | STAFF-101 |
| Inspector (Jamshoro) | INSP-204 |
| Administrator | ADMIN-001 |

Run the tests any time with `python test_api.py`.

## 2. How the front end and backend are connected

```
Browser (frontend/index.html)  ──fetch /api/...──▶  Flask (backend/app.py)  ──SQL──▶  fasal.db
        ▲                         JSON + Bearer token                                    (SQLite)
        └──────────────────────────────────────────────────────────────────────────────────┘
```

1. **Same server, same address.** Flask serves `index.html` at `/` and the API under `/api/...`, so the browser calls the API with relative URLs. No CORS setup, no hard-coded address.
2. **Sign in gives a token.** `POST /api/auth/...` checks the password (hashed with scrypt) and returns a signed token. The page keeps it in `sessionStorage` and sends `Authorization: Bearer <token>` with every request. The server checks the token and the user's role on every call, so hiding a button in the page is never the only protection.
3. **One call loads everything the screen needs.** `GET /api/bootstrap` returns the data for the signed-in role only (a farmer gets their own bookings; staff get their centre's; inspectors get loads assigned to them; admins get everything). The JSON uses the same field names your front end already used (`centreId`, `checkInAt`, `quality`, `payment`...), so every screen renders unchanged.
4. **Every button calls the API, then reloads.** For example "Check in" runs `POST /api/bookings/:id/check-in`; the server validates and updates the database, then the page reloads state and redraws. Queue screens refresh by themselves every 15 seconds.
5. **The server owns the rules.** Token numbers, slot capacity, who may do what, weights, payment amounts, and the order of steps are all enforced in `app.py`. The page only gives quick messages.

### What I changed in your `index.html`
- Removed the in-memory sample data and local business logic; added a small `api()` layer and `loadState()`.
- Every handler (`H.checkIn`, `H.decide`, `FORMS.farmerIn`, ...) now calls the matching endpoint.
- Sign-in uses real accounts. The "switch demo view" dropdown is gone (it would not work with real roles).
- Public home page reads live centre numbers from `/api/public/centres`.
- Slots for today are treated as passed once their hour ends (the old rule stopped working after 4 pm).
- Demo logins are only pre-filled while `window.FASAL_DEMO = true` (top of the file).

## 3. API summary

| Method and path | Who | Does |
|---|---|---|
| `POST /api/auth/farmer/login`, `/register` | public | farmer sign in / create account |
| `POST /api/auth/staff/login` | public | staff, inspector, admin sign in |
| `POST /api/auth/change-password` | any | change own password (no screen for it yet) |
| `GET /api/public/centres` | public | centres with today's queue and load |
| `GET /api/bootstrap` | signed in | everything the current role needs |
| `POST /api/bookings`, `/:id/cancel` | farmer | book a slot, cancel |
| `POST /api/bookings/:id/check-in`, `/queue`, `/weigh`, `/unload`, `/complete`, `/receipt`, `/mark-paid` | staff | run the delivery |
| `POST /api/bookings/:id/inspect` | inspector | grade, moisture, approve or reject |
| `POST /api/farmers`, `PATCH /api/farmers/:id` | admin | add, suspend |
| `POST /api/centres`, `PATCH /api/centres/:id` | admin | add, set capacity, open/close |
| `POST /api/crops`, `PATCH /api/crops/:id` | admin | add, rate, moisture limit, pause |

Errors come back as `{"error": "message"}` with a proper status code; the page shows the message.

## 4. Database

SQLite file `backend/fasal.db`, created from `schema.sql` on first start.

- `centres`, `crops`, `farmers`, `employees` (staff, inspectors and admins in one table with a `role`)
- `bookings`: one row per delivery, holding its whole life (booking, queue, weights, quality, unloading, payment, receipt). A unique key on `(centre, date, token number)` prevents duplicate tokens.
- `booking_events`: audit trail of who did what and when.

Back up by copying `fasal.db` while the server is stopped (or use `sqlite3 fasal.db ".backup copy.db"` while it runs). To inspect it, use DB Browser for SQLite.

## 5. If the front end is hosted on a different address

Example: page on `https://fasaltoken.example.com`, API on `https://api.example.com`.
1. In `frontend/index.html` set `window.FASAL_API_BASE='https://api.example.com';`
2. In `backend/.env` set `CORS_ORIGINS=https://fasaltoken.example.com`

## 6. Before real farmers use it

1. Create `backend/.env` from `.env.example` and set a long random `SECRET_KEY`.
2. Start from a clean database: `python manage.py init` (crops plus one admin, you choose the password) instead of `demo`. Then sign in as admin, add your centres, and add staff and inspectors:
   `python manage.py add-employee --role staff --emp-id STAFF-105 --name "Ali Raza" --centre 2`
3. Set `window.FASAL_DEMO=false` in `index.html`.
4. Run it behind HTTPS with a production server, not `python app.py`: `pip install gunicorn` then `gunicorn -w 2 -b 127.0.0.1:8000 app:app` (Linux), or `pip install waitress` then `waitress-serve --port=8000 app:app` (Windows), with Nginx, Caddy or your host providing HTTPS.
5. Change `DEFAULT_FARMER_PASSWORD` (given to farmers an admin adds) and decide how those farmers will change it: the `change-password` endpoint exists but has no screen yet.
6. Back up `fasal.db` on a schedule.

## 7. Known limits (worth planning for)
- No SMS or payment gateway: "Processing" becomes "Paid" only through `mark-paid` (no button for it yet).
- No password reset by SMS; use `python manage.py set-password`.
- Staff, inspectors and admins are created by command line, since the admin screen has no employee management yet.
- Staff and inspectors load the last 60 days of bookings, admins 120 (settings at the top of `app.py`). Admin reports are calculated in the browser from that window; for large volumes move them to SQL.
- SQLite suits one server and moderate traffic. If you later need several servers, move to PostgreSQL: the schema is plain SQL and the queries are simple.
- The page's marketing text still says "working prototype with sample data".
