# College Subject-Wise Attendance Management System

**Modern College of Arts, Science & Commerce, Ganeshkhind Road, Pune**

Developed by **Krushna Thombare**, M.Sc. Computer Science (MCS).

What changed in each version: `CHANGELOG.md`. Installing on a server: `DEPLOYMENT.md`.

A full-stack web app for taking, reviewing and correcting **subject-wise attendance** in a college.

- **Backend:** Python, Django, Django REST Framework, JWT (SimpleJWT), SQLite (`db.sqlite3`)
- **Frontend:** React (Vite, JavaScript), React Router, Axios, plain CSS (responsive)
- **Reports:** CSV and PDF export (ReportLab)

```
College Admin → Departments → HOD → Staff/Teachers → Students → Subject-wise attendance
```

Students do not sign in (Version 1.1).

| URL | What |
|---|---|
| `http://127.0.0.1:5173` | React app (local frontend URL) |
| `http://127.0.0.1:8000/api/` | REST API (API base URL) |

---

## 1. What each role can do

**College Admin** – dashboard with live totals; create / edit / view / deactivate departments (duplicate names blocked, case-insensitive); register HODs and activate / deactivate them (one active HOD per department); see staff and student counts per department, overall attendance, lectures pending today; read-only access to staff, students, reports and the audit log of every department.

**HOD** – sees only their own department. Registers and manages staff; manages subjects, classes (semester + division), teacher ↔ subject ↔ class assignments, and the weekly timetable; generates dated lectures from the timetable; views students registered by staff; daily report (submitted / pending) with filters for date, subject, teacher, semester, class and status; attendance history; reviews correction requests (approve / reject with remarks); exports reports to CSV and PDF.

**Staff / Teacher** – sees their assigned subjects and classes; registers students (roll number, name, class); searches and edits students; marks each student Present or Absent for a lecture and submits; views previous lectures and student-wise percentages; sends correction requests (a reason is mandatory) and sees their status.

### Rules the backend enforces (not just the UI)

- Every endpoint has a permission class; data is filtered by department in every queryset, so cross-department access returns `404`/`403`.
- Roles are never read from request data. Only the College Admin can create HODs; only a department's HOD can create its staff.
- Roll numbers are unique within a class (case-insensitive). Registering an existing roll number returns `409` with the existing student, so a second teacher of the same class sees the student instead of creating a duplicate.
- A teacher can mark attendance only for their own active assignment, only for students of that class, and not for future dates.
- `AttendanceRecord` has a unique constraint on `(lecture, student)`. Submitting requires every active student to be marked.
- Submitted attendance is read-only: there is no update or delete endpoint for it. The only way to change a mark is an **approved correction request**, which writes old value, new value, requester, approver, timestamp and reason to the audit log.
- Audit log entries cannot be edited or deleted.

### How "pending" is decided

Pending is **never guessed**. The HOD enters the weekly **Timetable** and presses **Generate lectures** for a date range. That creates one draft lecture per scheduled slot. A draft lecture whose date has arrived is **Pending**; a submitted one is **Submitted**; a cancelled one is **Cancelled**. A date with no scheduled lecture has no rows at all. (Teachers can also add an extra lecture themselves.)

---

### Features that go beyond the basics

- **Low-attendance alert.** A student under 75% is marked "Low attendance" on the Student summary page (with an "Only low attendance" filter, also in the CSV/PDF export view). The HOD dashboard shows a "Low attendance" card and a list of the worst students over the last 30 days; a student needs at least 3 marked lectures in that window, so one absence on day one is not an alarm. Change the limits with `LOW_ATTENDANCE_THRESHOLD`, `LOW_ATTENDANCE_DAYS`, `LOW_ATTENDANCE_MIN_LECTURES` in `.env`.
- **CSV student import.** Students page, "Import CSV": choose the class and a CSV with the columns `roll_number` and `name` (a sample can be downloaded in the dialog). "Check file" shows what would happen without saving anything; then "Import". Existing roll numbers are skipped, never changed; bad rows are listed with their line number; at most 2000 rows / 1 MB per file; every import is written to the audit log. HODs can import into any class of their department, teachers only into classes they teach.
- **Dashboard charts.** Admin and HOD dashboards show the last 14 days of attendance (columns, red below the limit, dashed limit line) and a bar comparison of departments (admin) or subjects (HOD). Numbers come from the stored lecture totals, so they stay fast with a lot of data. The charts are plain SVG, no extra library.
- **One sign-in page with a role toggle.** `/login` has an Admin / HOD / Teacher toggle (it remembers your last choice). The server also checks it: an account can only sign in under its own role (a teacher typing the right password under HOD is refused). Students do not sign in at all.
- **Monthly attendance sheet.** The class register for one month: a row per student, a column per submitted lecture (day over subject code), red A / green P, and each student's present, total and percentage. Choose class, optional subject and month; students under the limit are shaded. A teacher sees only their own lectures, the HOD all of the department's. Export CSV or a landscape PDF (long months are split into blocks of columns). Also: the teacher dashboard's *Today's lectures* marks the lecture that is on right now.
- **Removing a student needs the HOD.** Students are never hard-deleted (their attendance history must stay correct). A teacher uses *Request removal* on the Students page with a reason; the HOD sees it under *Removal requests* and approves (the student becomes inactive and drops off new attendance sheets, past attendance is kept) or rejects it with remarks. The teacher is notified and every step is in the audit log. The HOD can also *Deactivate* a student directly, and reactivate one with Edit.
- **Staff attendance.** Teachers are recorded in three ways, and the HOD decides which to use. (1) *My attendance* (teacher menu): a **Check in now** button; a check-in after the cut-off time (`STAFF_LATE_AFTER` in `.env`, default `10:00`) is recorded as Late. (2) *Staff attendance* (HOD menu): the HOD picks a date and marks each teacher P / L (late) / H (half day) / A / V (on leave) by hand, or presses *Start scanning* and shows the teacher's ID card (register the card once under *Staff*, Edit, "ID card"; typing the Staff ID works too). A repeat scan never changes an existing Present/Late/Half-day mark. (3) **Automatic:** a teacher who submits at least one lecture on a day is marked Present for that day (source "Took a lecture"). This replaces an Absent or On-leave mark, because the lecture proves the teacher was in college, and never touches Present, Late or Half day. The HOD can correct any day later; every mark and every automatic change is in the audit log. *Month sheet* shows the teachers by day with totals and exports CSV / PDF. The College Admin sees it read-only after choosing a department. Teachers see only their own days. **Leave:** a teacher uses *My leave* → *Request leave* (first day, last day, and a reason of at least 5 characters; at most 31 days, at most 7 days back, no overlap with another pending/approved leave) and can cancel it while it is pending. The HOD is notified and sees it under *Leave requests*; *Approve* or *Reject* needs remarks. On approval every day in the range except Sundays becomes **On leave** in staff attendance (a day the teacher already came, i.e. Present / Late / Half day, is left alone); the teacher is notified and everything is in the audit log. The HOD can still mark leave by hand for older days.
- **Notes for students, by link (no sign-in).** Teachers open *Notes for students* (HODs do not have this page), choose a class, choose for how long the link stays open (30 minutes up to 30 days, or a date and time), press *Turn on student link* and get one link such as `https://your-site/s/<long random code>`. They share a note: a title, an optional message, an optional file (PDF, Word, PowerPoint, Excel, text, PNG/JPG, up to 10 MB, `NOTE_MAX_MB`). Students open the link on any phone, with no account and no password, and see that class's notes and can download the files; nothing else (no names, no attendance). The link works only until the time the teacher set: after it the page and the files stop, and turning it on again makes a NEW address (an old link never wakes up); the teacher can change the time, make a new link or turn it off at once. The student page is plain HTML made by the server, with no app, no script files, no sign-in link and no address of the system, and it is not indexed by search engines. Safety: the code is 192 bits of randomness and cannot be guessed; *Make a new link* kills the old link at once (use it if the link leaks); *Turn off link* closes it; the public page is read-only and rate-limited (`PUBLIC_THROTTLE_RATE`, 1500/min per IP, high on purpose because a whole class on the college Wi-Fi looks like one IP); a link shows only its own class's notes; uploads are checked (allowed types only, the content must match the type, size limit, odd file names cleaned), stored under random names outside any web folder (`backend/uploads/`, git-ignored) and handed out as downloads with `nosniff` and a sandbox policy. A teacher can only share with classes they teach and remove their own notes. Sharing, removing and link changes are in the audit log. Anyone holding the link can read the notes, so tell students not to post it publicly; that is the trade-off of "no sign-in".
- **HOD edits a student fully.** In *Students*, *Edit* lets the HOD change the roll number, name, photo, ID card, active state and the **class** (any active class of the department). Past attendance stays with the old class's lectures; a roll number already used in the new class is refused with a clear message. Teachers cannot change a student's class.
- **Staff ID is automatic.** Registering a teacher or HOD gives the next Employee/Staff ID in that department (`EMP-001`, `EMP-002`, ...); it can still be edited later.
- **Register page layout.** While taking attendance, the scanner (camera and the list of scan results with photos) takes the left half of the page and the student list the right half; the list shows about 12 students and then has its own scroll bar. On a phone the two stack.
- **Student photos.** A photo can be added when a student is registered (take it with the camera or choose a file) or later with Edit, and while registering ID cards. The photo is checked, rotated upright, shrunk to at most 480 px and stored as a JPEG (this also removes any location data); it is shown only to signed-in users who may see that student. It appears next to the name in the student list, in the attendance register and, enlarged, right beside the result when a card is scanned, so the teacher can compare the face with the person holding the card. A student without a photo shows a plain dummy picture.
- **Register ID cards once, then take attendance by scanning them.** On the Students page, *Register ID cards*: pick the class and show each student's own college ID card (the barcode or QR on it, e.g. the Code 128 barcode that holds the Member ID) to the camera. The card's code is linked to the student who is highlighted (a USB/Bluetooth barcode scanner, or typing the code, works too: in the scanning box switch off *Use the camera* and just scan, the box stays ready after every card), then the next student without a card is selected. One card can belong to only one student; a wrong link can be removed. Afterwards, in the attendance register the teacher presses *Start scanning* and shows the cards: the linked student of that class is marked Present with a beep and a line saying who. Cards not yet registered still match by the roll number in the code (`MCS-2025-011`, `...?roll=11` or `11`). Unknown cards, other classes, inactive students and repeat scans are reported, not marked. Only the teacher scans, so students do not sign in and a friend cannot mark for someone. The camera needs `https` or `localhost`; scanning only fills in the register, the teacher still saves and submits as usual. Manual marking (P/A buttons, *Mark all present*, *Mark remaining absent*) always works, so a card that will not scan never blocks attendance. The *Register student* and *Edit student* forms also have a *Scan card* button, so a card can be linked at the moment the student is registered. No QR codes are generated: the college's own ID cards are used.
- **Search bars** on every list where you would look for something: students, staff, HODs, departments, subjects, classes, teacher assignments, timetable, lecture history, take attendance, daily report (also applied to its CSV/PDF export), correction requests, audit log, notifications, plus "find student" inside the attendance register and the student summary.

---

## 2. Project layout

```
college-attendance-system/
├── backend/
│   ├── manage.py
│   ├── requirements.txt
│   ├── .env.example
│   ├── config/         settings, root urls
│   ├── core/           audit log, notifications, permissions helpers, seed_demo command
│   ├── accounts/       User, StaffProfile, JWT login/logout, HOD & staff management
│   ├── departments/    Department
│   ├── academics/      ClassRoom, Subject, TeacherSubjectAssignment
│   ├── students/       Student
│   ├── attendance/     Lecture, AttendanceRecord, corrections, timetable, services.py (business rules)
│   └── reports/        dashboards, daily report, student summary, CSV/PDF exporters
└── frontend/
    ├── package.json, index.html, vite.config.js, .env.example
    └── src/  api/ components/ context/ hooks/ layouts/ pages/ routes/ utils/ App.jsx main.jsx
```

---

## 3. Setup and running

You need **Python 3.12+** and **Node.js 20+** (tested with Python 3.13 and Node 22).

### Backend (terminal 1)

```bash
cd college-attendance-system/backend

# 1. create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1

# 2. install dependencies
pip install -r requirements.txt

# 3. configuration (optional for local development; defaults are safe for DEBUG)
cp .env.example .env                 # then edit .env if you like

# 4. create the SQLite database (db.sqlite3)
python manage.py migrate

# 5. create the first College Admin. The password is typed at a hidden prompt, stored hashed.
python manage.py create_college_admin --username principal --email principal@college.example

# 6. start the API on http://127.0.0.1:8000
python manage.py runserver
```

For scripted setups you can supply the password without a prompt: `ADMIN_PASSWORD='...' python manage.py create_college_admin --username principal`.

### Frontend (terminal 2)

```bash
cd college-attendance-system/frontend
npm install
npm run dev                          # http://127.0.0.1:5173
```

If the API is not at `http://127.0.0.1:8000/api`, copy `frontend/.env.example` to `frontend/.env` and set `VITE_API_BASE_URL`.

Open `http://127.0.0.1:5173` and sign in with the admin account. Create a department, register its HOD, then sign in as the HOD to add staff, subjects, classes, assignments and the timetable.

### Scanning ID cards with a phone over USB (Android)

Browsers allow the camera only on `https` or on `localhost`/`127.0.0.1`. To use the phone's camera while the app runs on the laptop, without Wi-Fi or certificates:

1. On the phone: enable *Developer options* and *USB debugging*, then connect the USB cable and allow the computer.
2. On the laptop (with Android platform-tools, `adb`, installed), with the backend and frontend running:
   ```bash
   adb reverse tcp:5173 tcp:5173
   adb reverse tcp:8000 tcp:8000
   ```
3. Open `http://127.0.0.1:5173` in Chrome on the phone, sign in as the teacher, open a lecture and press *Start scanning*. The phone's `127.0.0.1` is now the laptop, so the page counts as secure and the camera works.

Not tested on a physical phone in this project; the camera and barcode reading were tested in a headless browser with a recorded card image. An iPhone cannot use `adb`; use the laptop's webcam, or serve the app over `https`.

### Optional: demo data (development only)

```bash
python manage.py seed_demo           # refuses to run when DJANGO_DEBUG=false
```

Creates two departments (Computer Science, Mathematics) with an HOD, teachers, classes, students, a Monday–Saturday timetable and the last five days of lectures, most of them submitted, with a few left pending today. All demo accounts (`hod_cs`, `hod_math`, `prof_rane`, `prof_gokhale`, `prof_iyer`, `prof_sathe`, `prof_bapat`) share the password printed by the command (`Demo#Pass2026` by default; change it with `--password`). The College Admin is not created by the seed; use step 5 above. Do not use demo data in a real deployment.

### Run the tests

```bash
cd backend
source .venv/bin/activate
python manage.py test                # 134 tests, about 3 seconds

cd ../frontend
npm run build                        # production build = frontend verification (compiles every page)
```

### Back up / export the SQLite database

```bash
cd backend
# consistent copy, safe even while the server is running (works without the sqlite3 CLI)
python -c "import sqlite3; s=sqlite3.connect('db.sqlite3'); d=sqlite3.connect('backup-$(date +%F).sqlite3'); s.backup(d); d.close(); s.close()"

# portable JSON export of all data
python manage.py dumpdata --exclude contenttypes --exclude auth.permission --indent 2 > backup.json
```

To restore the file copy, stop the server and replace `db.sqlite3` with the backup. To restore JSON: `python manage.py migrate` on an empty database, then `python manage.py loaddata backup.json`.

---

## 4. API overview

All endpoints live under `/api/` and need `Authorization: Bearer <access token>` except login.

| Area | Endpoints |
|---|---|
| Auth | `POST auth/login/` · `POST auth/refresh/` · `POST auth/logout/` (blacklists the refresh token) · `GET/PATCH auth/me/` · `POST auth/change-password/` |
| Departments (admin writes) | `departments/` · `departments/{id}/` · `…/activate/` · `…/deactivate/` |
| HODs (admin) | `hods/` · `hods/{id}/` · `…/activate/` · `…/deactivate/` · `…/reset-password/` |
| Staff (HOD writes, admin reads) | `staff/` and the same actions |
| Academics (HOD writes) | `classes/` · `subjects/` · `assignments/` (+ `activate/`, `deactivate/`) · `timetable/` (+ `POST timetable/generate/`) |
| Students | `students/` (409 on duplicate roll number) |
| Attendance | `lectures/` · `lectures/{id}/roster/` · `…/mark/` (save draft) · `…/submit/` · `…/cancel/` · `attendance-records/` (read-only history) |
| Staff attendance | `staff-attendance/check-in/` (teacher) · `staff-attendance/me/?month=` (teacher) · `staff-attendance/day/?date=` · `staff-attendance/mark/` (HOD) · `staff-attendance/monthly/?month=` (`&export=csv` or `pdf`) |
| Leave | `leave-requests/` (teacher creates, HOD / admin list) · `…/{id}/approve/` · `…/reject/` (HOD, remarks required) · `…/{id}/cancel/` (teacher, while pending) |
| Notes | `notes/` (teacher only: list, `POST` multipart upload, `DELETE`) · `notes/share/` (`GET ?classroom=`, `POST {classroom, action: enable / new_link / disable, minutes: 5 to 43200}`) · students, no sign-in (plain pages outside `/api/`): `/s/{token}/` and `/s/{token}/f/{note_id}/` |
| Corrections | `corrections/` · `corrections/{id}/approve/` · `…/reject/` (HOD, remarks required) |
| Reports | `reports/daily/` · `reports/student-summary/` (both accept `?export=csv` or `?export=pdf`) · `dashboard/` |
| Other | `notifications/` (+ `read/`, `read-all/`, `unread-count/`) · `audit-logs/` |

List endpoints are paginated (`?page=`, `?page_size=` up to 200) and support `?search=` plus filters.

---

## 5. Security notes and production settings

Already in place: PBKDF2 password hashing and Django's password validators; JWT access (30 min) and rotating, blacklisted refresh tokens; login rate limiting; permission classes on every endpoint plus department-scoped querysets; secrets read from environment variables; CORS limited to the listed origins; the server refuses to start with `DJANGO_DEBUG=false` and no secret key.

**Security checklist (what this project does for each point).**

- *Passwords:* only Django's PBKDF2 hashes are stored (`set_password`), never plain text; Django's password validators apply.
- *Nothing secret in the URL:* sign-in is a `POST` body, the token travels in the `Authorization` header; no password, token or key is ever put in a URL or query string.
- *API:* every endpoint requires a valid token by default and has its own permission class; unauthenticated calls get 401.
- *Roles:* Admin, HOD and Staff have separate permission classes and department-scoped data; the role is never taken from the request.
- *Database and backups:* the database is one SQLite file that only the server process opens; keep it (and `.env`) readable by the server user only (`chmod 600`). `python manage.py backup_db` writes an **encrypted** snapshot (Fernet/AES, key `BACKUP_ENCRYPTION_KEY` in `.env`, taken safely while the site is running, owner-only file permissions, keeps the newest 14); `python manage.py restore_db <file> --output restored.sqlite3` decrypts it (it never overwrites the live database). Wrong key or a changed file is refused. The shared files in `backend/uploads/` are not inside the database backup: copy that folder as well. Schedule `backup_db` daily (cron / Task Scheduler) and keep the key somewhere other than the backups.
- *Errors:* the user sees a plain message with a short reference number (for example "quote reference a1b2c3d4"); the real traceback goes to `backend/logs/server.log` together with that reference. Failed sign-ins (username and IP, never the password) go to `backend/logs/security.log`. Logs rotate at 5 MB, keep 5 files, and `logs/` is git-ignored.
- *Secrets:* `DJANGO_SECRET_KEY`, the backup key and everything else live in `backend/.env`, which is git-ignored (only `.env.example` is committed); with `DJANGO_DEBUG=false` the server will not start without a secret key.
- *HTTPS and sessions:* with `DJANGO_DEBUG=false` the app redirects to HTTPS, uses secure cookies and HSTS; short-lived tokens (30 min access, 1 day refresh, rotated and blacklisted on logout); login rate limit per username (10/min) and per IP (300/min). `X-Frame-Options: DENY`, `nosniff` and a strict referrer policy are sent always.

**Before deploying for real**, set these in `backend/.env` (or the host's environment):

```
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<new random value: python -c "from django.core.management.utils import get_random_secret_key as g; print(g())">
DJANGO_ALLOWED_HOSTS=attendance.yourcollege.example
CORS_ALLOWED_ORIGINS=https://attendance.yourcollege.example
DJANGO_SSL_REDIRECT=true
DJANGO_HSTS_SECONDS=31536000
```

Step-by-step install instructions for the college IT team (gunicorn, nginx, HTTPS, backups) are in `DEPLOYMENT.md`. Then serve Django with a production server (for example gunicorn behind nginx with HTTPS), build the frontend with `npm run build` and serve `frontend/dist`, set `VITE_API_BASE_URL` to the public API URL before building, and back up `db.sqlite3` regularly. SQLite is fine for a college with many departments; move to a server database only if hundreds of people save at the same moment.

**Many users at once.** Every teacher, HOD and admin talks to the same Django server, and the server reads and writes one `db.sqlite3` file, so everybody sees the same data. SQLite is set up in WAL mode with a 20 second wait, so many people can read while one saves, and simultaneous submissions queue up instead of failing. This is comfortable for one college. If you later have hundreds of people saving at the same moment, or want the database on a separate machine, move to PostgreSQL: create an empty database and user, then put `DB_ENGINE=postgres`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST` and `DB_PORT` in `.env` (see `.env.example`) and run `python manage.py migrate`. No application code changes. `backup_db` then uses `pg_dump`, so install the PostgreSQL client tools on the server. To move existing SQLite data, use `dumpdata` on the old database and `loaddata` on the new one.

**Many departments / large data.** Each lecture stores its own present/absent totals when it is submitted (and they are adjusted when a correction is approved), so dashboards and reports add up lectures instead of scanning millions of attendance rows. Lists use indexed queries and subquery counts. If the totals ever look wrong (for example after editing the database by hand), run `python manage.py recount_attendance` to rebuild them from the records. Reports are capped (20,000 lectures on screen, 50,000 in CSV, 2,000 in PDF) and ask the user to narrow the dates instead of timing out. Measured on a copy with 30 departments and about 2.5 million attendance records: admin dashboard about 50 ms, department list about 15 ms, lecture list about 40 ms, single save/submit 12-35 ms. These timings are from one test machine, not a guarantee.

**Extra settings** (all optional, see `backend/.env.example`): `LOGIN_THROTTLE_RATE` (per username, default 10/min), `LOGIN_IP_THROTTLE_RATE` (per IP, default 300/min, so one student lab behind one IP is not locked out), `DRF_NUM_PROXIES` (number of reverse proxies, so the real client IP is used), `DJANGO_BEHIND_PROXY=true` (when nginx terminates HTTPS), `ATTENDANCE_BACKDATE_DAYS` (how far back a teacher may add a lecture, default 30). The Django admin site is not exposed; administration happens in the app. Only the HOD can deactivate a student. For production run gunicorn with several workers (for example `gunicorn config.wsgi -w 4`), because password hashing is deliberately slow (about 1 s per login).

Known trade-off: the React app keeps the JWT in `localStorage` so a refresh keeps you signed in. That is simple and common, but a cross-site-scripting bug would expose it. The app renders all data through React (escaped by default) and the access token is short-lived. For stricter setups, move the refresh token into an `HttpOnly` cookie.

---

## 6. Verification status

Checked on the build machine before packaging:

- `python manage.py check` and `makemigrations --check`: no issues, no missing migrations.
- `python manage.py test`: 318 tests, all passing (API network lock, electives: a student left out of one subject, downloads of students/staff/HODs by role and the admin ZIP, notes link for students, security hardening and encrypted backups, leave requests, staff attendance incl. automatic present from a submitted lecture, student photos, monthly sheet, student removal requests, automatic staff IDs, ID card linking, role-specific login, search, low-attendance flags, dashboard trend, CSV import, stored totals and recount, timetable clashes, deactivated-teacher lectures not pending, login throttling, report caps, login and roles, department isolation, HOD and staff restrictions, student registration and duplicate roll numbers, roll numbers sorted 1, 2, 10, attendance submission, duplicate prevention, present/absent counts, pending-lecture reports, correction approval and rejection).
- `npm run build`: succeeds.
- API run against a live server on a fresh database with demo data: 49 of 49 checks passed (logins and role limits, cross-department blocking, 409 on duplicate roll number, only the assigned teacher can mark, submission needs every student, submitted attendance cannot be edited or deleted, corrections with approve and reject, audit entries, report counts, CSV and PDF export, logout blacklists the refresh token).
- Real browser run (headless Chromium, Vite dev server plus Django): every page of the Admin, HOD and Staff roles opened with correct role redirects; a full teacher flow (add lecture, mark, submit, request correction), HOD approval with remarks, the audit entry, a PDF download, and a phone-width pass with no horizontal scrolling. No browser console errors and no failed API calls after the fixes.
- Concurrency run: 5 teachers submitting 30 registers at the same time while 2 HODs loaded dashboards and reports (410 requests, all succeeded, no "database is locked").

Not covered: the frontend has no unit tests of its own, and the concurrency run used 7 simultaneous users, not hundreds.
