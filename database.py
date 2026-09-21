"""
database.py — handles all data storage for the RFID web app.

MIGRATED TO POSTGRESQL for Railway deployment. Connects using the
DATABASE_URL environment variable that Railway automatically provides
when you add a Postgres service to your project — no manual connection
string needed there.

For local development/testing without Railway, set DATABASE_URL yourself,
e.g.:
    postgresql://postgres:yourpassword@localhost:5432/yourdbname
"""

import os
import re
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PH_TZ = ZoneInfo("Asia/Manila")


def ph_now():
    """
    Current time in Philippine local time (UTC+8) — used everywhere
    instead of bare datetime.now(), because this app may run on a
    server (like Railway) whose system clock is UTC, not Philippine
    time. Every recorded scan time, report default, and scheduled
    check needs to reflect actual Philippine time regardless of what
    timezone the underlying server happens to be set to.

    Returns a naive datetime (no tzinfo attached) whose values already
    correctly reflect Philippine wall-clock time — a drop-in
    replacement for datetime.now() everywhere it was previously used,
    since the rest of this codebase works with naive datetimes and
    plain date/time strings throughout.
    """
    return datetime.now(PH_TZ).replace(tzinfo=None)


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:testpass@localhost:5432/rfid_test"  # local dev fallback only
)


class _CompatCursor:
    """
    Thin wrapper so existing code written for sqlite3's row/cursor
    behavior keeps working unchanged against psycopg2:
      - fetchone()/fetchall() return dict-like rows (row["field"] works,
        same as sqlite3.Row)
      - .rowcount works the same way
      - .lastrowid is emulated for the few INSERT statements that need
        it (record_scan, create_blast, queue_sms_job) — see _LASTROWID_TABLES
    """
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount


class _CompatConnection:
    """
    Wraps a psycopg2 connection so the rest of the codebase can keep
    calling conn.execute(sql, params) directly (sqlite3-style) instead
    of the more verbose conn.cursor(); cursor.execute(...) psycopg2
    normally requires — this is what let the ~45 existing functions in
    this file stay almost line-for-line identical during the Postgres
    migration, rather than needing every call site rewritten by hand.
    """
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql, params=()):
        # sqlite3 uses "?" placeholders; psycopg2 uses "%s". This
        # codebase never uses a literal "?" character in actual SQL
        # text (no such column/value anywhere), so a straight
        # replacement is safe here.
        pg_sql = sql.replace("?", "%s")

        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        compat = _CompatCursor(cursor)

        # Emulate sqlite3's cursor.lastrowid for the specific INSERT
        # statements that need the new row's id — Postgres has no such
        # attribute; it requires an explicit "RETURNING id" clause.
        needs_id = (
            "INSERT INTO attendance_logs" in pg_sql
            or "INSERT INTO blast_logs" in pg_sql
            or "INSERT INTO sms_queue" in pg_sql
        )
        if needs_id and "RETURNING" not in pg_sql.upper():
            pg_sql = pg_sql.rstrip().rstrip(";") + " RETURNING id"

        cursor.execute(pg_sql, params)

        if needs_id:
            try:
                result = cursor.fetchone()
                compat.lastrowid = result["id"] if result else None
            except psycopg2.ProgrammingError:
                pass  # no results to fetch (e.g. statement didn't return rows)

        return compat

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_connection():
    pg_conn = psycopg2.connect(DATABASE_URL)
    return _CompatConnection(pg_conn)


def setup_database():
    conn   = get_connection()
    cursor = conn.execute("SELECT 1")  # ensure connection works before proceeding

    conn.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id           SERIAL PRIMARY KEY,
            rfid_code    TEXT    UNIQUE,
            full_name    TEXT    NOT NULL,
            section      TEXT,
            parent_name  TEXT,
            parent_phone TEXT,
            messenger_id TEXT,
            photo        TEXT,
            status       TEXT    DEFAULT 'active',
            status_reason TEXT,
            created_at   TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)

    # Upgrade existing databases — add missing columns. Postgres supports
    # "ADD COLUMN IF NOT EXISTS" directly, so no try/except dance needed.
    for col, definition in [
        ("status",          "TEXT DEFAULT 'active'"),
        ("status_reason",   "TEXT"),
        ("lrn",             "TEXT"),
        ("id_printed",      "INTEGER DEFAULT 0"),
        ("id_distributed",  "INTEGER DEFAULT 0"),
        ("messenger_id_2",  "TEXT"),
        ("parent_name_2",   "TEXT"),
        ("parent_phone_2",  "TEXT"),
    ]:
        conn.execute(f"ALTER TABLE students ADD COLUMN IF NOT EXISTS {col} {definition}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id             SERIAL PRIMARY KEY,
            student_id     INTEGER NOT NULL,
            rfid_code      TEXT    NOT NULL,
            scan_type      TEXT    NOT NULL,
            scan_time      TEXT    NOT NULL,
            scan_date      TEXT    NOT NULL,
            notified       INTEGER DEFAULT 0,
            notify_channel TEXT,
            notify_detail  TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
    """)
    conn.execute("ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS notify_detail TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS blast_logs (
            id           SERIAL PRIMARY KEY,
            message      TEXT    NOT NULL,
            channels     TEXT    NOT NULL,
            section      TEXT,
            total        INTEGER DEFAULT 0,
            sent         INTEGER DEFAULT 0,
            failed       INTEGER DEFAULT 0,
            status       TEXT    DEFAULT 'running',
            blast_type   TEXT    DEFAULT 'manual',
            created_at   TEXT    DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)
    conn.execute("ALTER TABLE blast_logs ADD COLUMN IF NOT EXISTS blast_type TEXT DEFAULT 'manual'")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS blast_recipients (
            id           SERIAL PRIMARY KEY,
            blast_id     INTEGER NOT NULL,
            student_id   INTEGER,
            student_name TEXT,
            section      TEXT,
            channel      TEXT,
            success      INTEGER DEFAULT 0,
            sent_at      TEXT    DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
            FOREIGN KEY (blast_id) REFERENCES blast_logs(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sms_queue (
            id            SERIAL PRIMARY KEY,
            phone_number  TEXT    NOT NULL,
            message       TEXT    NOT NULL,
            status        TEXT    DEFAULT 'pending',
            claimed_by    TEXT,
            claimed_at    TEXT,
            completed_at  TEXT,
            error_message TEXT,
            attempts      INTEGER DEFAULT 0,
            created_at    TEXT    DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    defaults = [
        ("school_name",          "Communal Elementary School"),
        ("admin_name",           "Admin"),
        ("use_messenger",        "1"),
        ("use_sms",              "1"),
        ("sms_fallback_only",    "1"),
        ("sim800c_port",         ""),
        ("sim800c_candidate_ports", ""),
        ("messenger_token",      ""),
        ("announcement",         ""),
        ("school_logo",          ""),
        ("webhook_verify_token", "rfid_school_verify"),
        ("messenger_page_id",    "61590225764767"),
    ]
    for key, value in defaults:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
            (key, value)
        )

    conn.commit()
    conn.close()
    print("✅ Database ready (PostgreSQL).")


# ── STUDENTS ─────────────────────────────────────────────────────────────────

def add_student(rfid_code, full_name, section="",
                parent_name="", parent_phone="", photo=None, lrn=""):
    """Add a new student. Skips silently if RFID already exists."""
    if not full_name:
        return
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO students
                (rfid_code, full_name, section, parent_name, parent_phone, photo, lrn)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            rfid_code.upper() if rfid_code else "",
            full_name, section, parent_name, parent_phone, photo, lrn or ""
        ))
        conn.commit()
    except psycopg2.IntegrityError:
        print(f"⚠️  RFID {rfid_code} already exists — skipped.")
    finally:
        conn.close()


def update_student(student_id, rfid_code, full_name, section,
                   parent_name, parent_phone, photo=None, lrn=""):
    conn = get_connection()
    if photo:
        conn.execute("""
            UPDATE students
            SET rfid_code=?, full_name=?, section=?,
                parent_name=?, parent_phone=?, photo=?, lrn=?
            WHERE id=?
        """, (
            rfid_code.upper() if rfid_code else "",
            full_name, section, parent_name, parent_phone,
            photo, lrn or "", student_id
        ))
    else:
        conn.execute("""
            UPDATE students
            SET rfid_code=?, full_name=?, section=?,
                parent_name=?, parent_phone=?, lrn=?
            WHERE id=?
        """, (
            rfid_code.upper() if rfid_code else "",
            full_name, section, parent_name, parent_phone,
            lrn or "", student_id
        ))
    conn.commit()
    conn.close()


def delete_student(student_id):
    """Delete a student and their attendance logs."""
    conn = get_connection()
    conn.execute("DELETE FROM attendance_logs WHERE student_id = ?", (student_id,))
    conn.execute("DELETE FROM students WHERE id = ?", (student_id,))
    conn.commit()
    conn.close()


def get_student_by_rfid(rfid_code):
    conn = get_connection()
    row  = conn.execute(
        "SELECT * FROM students WHERE rfid_code = ?",
        (rfid_code.upper(),)
    ).fetchone()
    conn.close()
    return row


def get_student_by_lrn(lrn):
    """Look up a student by their LRN (used for Messenger linking)."""
    conn = get_connection()
    row  = conn.execute(
        "SELECT * FROM students WHERE lrn = ?",
        (str(lrn).strip(),)
    ).fetchone()
    conn.close()
    return row


def update_student_messenger_id_by_lrn(lrn, messenger_id):
    """Link a parent's Messenger ID to a student found by LRN."""
    conn = get_connection()
    conn.execute(
        "UPDATE students SET messenger_id=? WHERE lrn=?",
        (messenger_id, str(lrn).strip())
    )
    conn.commit()
    conn.close()


def get_student_by_id(student_id):
    conn = get_connection()
    row  = conn.execute(
        "SELECT * FROM students WHERE id = ?", (student_id,)
    ).fetchone()
    conn.close()
    return row


def get_all_students():
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM students ORDER BY section, full_name"
    ).fetchall()
    conn.close()
    return rows


def import_students_from_list(students_list):
    """
    Bulk import. students_list is a list of dicts with keys:
    full_name, rfid_code, section, parent_name, parent_phone
    """
    count = 0
    for s in students_list:
        add_student(
            rfid_code    = s.get("rfid_code", ""),
            full_name    = s.get("full_name", ""),
            section      = s.get("section", ""),
            parent_name  = s.get("parent_name", ""),
            parent_phone = s.get("parent_phone", ""),
        )
        count += 1
    return count


# ── ATTENDANCE ────────────────────────────────────────────────────────────────

def record_scan(student_id, rfid_code, scan_type, notify_channel=""):
    now       = ph_now()
    scan_time = now.strftime("%H:%M:%S")
    scan_date = now.strftime("%Y-%m-%d")
    conn      = get_connection()
    cursor    = conn.execute("""
        INSERT INTO attendance_logs
            (student_id, rfid_code, scan_type, scan_time, scan_date, notify_channel)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (student_id, rfid_code.upper(), scan_type, scan_time, scan_date, notify_channel))
    log_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return log_id


def get_last_scan_today(student_id):
    today = ph_now().strftime("%Y-%m-%d")
    conn  = get_connection()
    row   = conn.execute("""
        SELECT * FROM attendance_logs
        WHERE student_id = ? AND scan_date = ?
        ORDER BY scan_time DESC LIMIT 1
    """, (student_id, today)).fetchone()
    conn.close()
    return row


def get_last_scan_overall():
    """
    Most recent scan across ALL students, system-wide. Used to detect real
    inactivity (weekends, holidays, unexpected class suspensions) instead of
    assuming a fixed calendar — rather than hardcoding Sat/Sun, we check
    how long it's actually been since anyone scanned.
    Returns a datetime object, or None if there are no scans yet at all.
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT scan_date, scan_time FROM attendance_logs
        ORDER BY scan_date DESC, scan_time DESC LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        return None
    try:
        return datetime.strptime(
            f"{row['scan_date']} {row['scan_time']}", "%Y-%m-%d %H:%M:%S"
        )
    except (ValueError, TypeError):
        return None


def determine_scan_type(student_id):
    last = get_last_scan_today(student_id)
    if last is None:
        return "IN"
    return "OUT" if last["scan_type"] == "IN" else "IN"


def mark_notified(log_id, channel, detail=None):
    """
    Records how a scan's notification actually went. Called for EVERY
    attempted notification, including complete failures — not just
    successful ones — so a genuinely failed notification is visible in
    the log with a reason, instead of silently never being recorded at
    all (which is what happened before this existed).

    channel: the channel(s) that succeeded, comma-joined (e.g.
        "messenger" or "sms_queued"), or exactly "none" if nothing
        worked. The `notified` flag is derived from this automatically
        — 1 if any channel succeeded, 0 if channel == "none".
    detail: optional human-readable explanation, shown to the admin on
        the Blast page's notification log — e.g. "Messenger failed:
        window expired — sent via SMS instead", or the specific reason
        nothing could be sent at all.
    """
    conn = get_connection()
    notified_flag = 0 if channel == "none" else 1
    conn.execute(
        "UPDATE attendance_logs SET notified=?, notify_channel=?, notify_detail=? WHERE id=?",
        (notified_flag, channel, detail, log_id)
    )
    conn.commit()
    conn.close()


def get_today_logs(limit=50):
    today = ph_now().strftime("%Y-%m-%d")
    conn  = get_connection()
    rows  = conn.execute("""
        SELECT a.*, s.full_name, s.section, s.photo
        FROM attendance_logs a
        JOIN students s ON a.student_id = s.id
        WHERE a.scan_date = ?
        ORDER BY a.scan_time DESC
        LIMIT ?
    """, (today, limit)).fetchall()
    conn.close()
    return rows


def get_recent_notification_log(limit=100, section=None, grade_level=None,
                                start_date=None, end_date=None):
    """
    Recent scan-triggered notification attempts, most recent first —
    which channel actually delivered (or fell back to), and why, when
    something didn't go as expected. Includes complete failures too
    (notify_channel = "none"), not just successes.

    Optional filters:
      section     — exact section match (e.g. "Grade 1 - Rose")
      grade_level — matches any section starting with this grade
                    (e.g. "Grade 1" matches "Grade 1 - Rose", "Grade 1 - Camia")
      start_date, end_date — "YYYY-MM-DD", inclusive range on scan_date
    """
    conn = get_connection()

    where_clauses = ["a.notify_channel IS NOT NULL"]
    params = []

    if section:
        where_clauses.append("s.section = ?")
        params.append(section)
    elif grade_level:
        # Exact grade-prefix matching (not a raw LIKE pattern), so
        # "Grade 1" can never accidentally match a "Grade 10" section —
        # same safe approach used for report filtering.
        matching_sections = [s for s in get_all_sections()
                             if s.split("-")[0].strip() == grade_level or s.strip() == grade_level]
        if matching_sections:
            placeholders = ",".join(["?"] * len(matching_sections))
            where_clauses.append(f"s.section IN ({placeholders})")
            params.extend(matching_sections)
        else:
            # No sections match this grade at all — return nothing rather
            # than accidentally showing unfiltered results.
            where_clauses.append("1=0")

    if start_date:
        where_clauses.append("a.scan_date >= ?")
        params.append(start_date)
    if end_date:
        where_clauses.append("a.scan_date <= ?")
        params.append(end_date)

    where_sql = " AND ".join(where_clauses)
    params.append(limit)

    rows = conn.execute(f"""
        SELECT a.*, s.full_name, s.section
        FROM attendance_logs a
        JOIN students s ON a.student_id = s.id
        WHERE {where_sql}
        ORDER BY a.scan_date DESC, a.scan_time DESC
        LIMIT ?
    """, tuple(params)).fetchall()
    conn.close()
    return rows


def get_today_stats():
    today = ph_now().strftime("%Y-%m-%d")
    conn  = get_connection()
    total_students = conn.execute(
        "SELECT COUNT(*) as c FROM students"
    ).fetchone()["c"]
    scanned_today = conn.execute(
        "SELECT COUNT(DISTINCT student_id) as c FROM attendance_logs WHERE scan_date=?",
        (today,)
    ).fetchone()["c"]
    # Students currently inside = last scan was IN
    in_count = conn.execute("""
        SELECT COUNT(*) as c FROM (
            SELECT student_id, scan_type FROM attendance_logs
            WHERE scan_date=? AND id IN (
                SELECT MAX(id) FROM attendance_logs
                WHERE scan_date=?
                GROUP BY student_id
            )
        ) sub WHERE scan_type='IN'
    """, (today, today)).fetchone()["c"]
    out_count = max(0, scanned_today - in_count)
    conn.close()
    return {
        "total":          scanned_today,
        "in":             in_count,
        "out":            out_count,
        "not_yet":        max(0, total_students - scanned_today),
        "total_students": total_students,
    }


def get_logs_for_report(start_date, end_date, section=None):
    conn = get_connection()
    if section:
        rows = conn.execute("""
            SELECT a.*, s.full_name, s.section, s.rfid_code
            FROM attendance_logs a
            JOIN students s ON a.student_id = s.id
            WHERE a.scan_date BETWEEN ? AND ? AND s.section = ?
            ORDER BY a.scan_date DESC, a.scan_time DESC
        """, (start_date, end_date, section)).fetchall()
    else:
        rows = conn.execute("""
            SELECT a.*, s.full_name, s.section, s.rfid_code
            FROM attendance_logs a
            JOIN students s ON a.student_id = s.id
            WHERE a.scan_date BETWEEN ? AND ?
            ORDER BY a.scan_date DESC, a.scan_time DESC
        """, (start_date, end_date)).fetchall()
    conn.close()
    return rows


# ── SETTINGS ─────────────────────────────────────────────────────────────────

def get_setting(key):
    conn = get_connection()
    row  = conn.execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else ""


def save_setting(key, value):
    conn = get_connection()
    conn.execute("""
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """, (key, str(value)))
    conn.commit()
    conn.close()


def get_all_settings():
    conn = get_connection()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


# ── BLAST ─────────────────────────────────────────────────────────────────────

def create_blast(message, channels, section=None, total=0, blast_type='manual'):
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO blast_logs (message, channels, section, total, status, blast_type)
        VALUES (?, ?, ?, ?, 'running', ?)
    """, (message, channels, section, total, blast_type))
    blast_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return blast_id

def update_blast_progress(blast_id, sent, failed, status='running'):
    conn = get_connection()
    conn.execute("""
        UPDATE blast_logs SET sent=?, failed=?, status=? WHERE id=?
    """, (sent, failed, status, blast_id))
    conn.commit()
    conn.close()

def log_blast_recipient(blast_id, student_id, student_name, section, channel, success):
    """
    Records the actual outcome for ONE parent within a blast (or holiday
    announcement) — which channel was used (or 'none' if all failed) and
    whether it succeeded. This is what actually answers "did this specific
    parent get the message", not just the blast's aggregate sent/failed
    counts.
    """
    conn = get_connection()
    conn.execute("""
        INSERT INTO blast_recipients
            (blast_id, student_id, student_name, section, channel, success)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (blast_id, student_id, student_name, section, channel, 1 if success else 0))
    conn.commit()
    conn.close()

def get_blast_recipients(blast_id):
    """All per-parent delivery records for one blast, most recent first."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM blast_recipients
        WHERE blast_id = ?
        ORDER BY sent_at DESC
    """, (blast_id,)).fetchall()
    conn.close()
    return rows


# ── SMS QUEUE (multi-PC SIM800C load-splitting) ────────────────────────────────
# Instead of one PC sending every SMS through one SIM (hitting telco fair-use
# limits fast), SMS jobs are queued here and any of several PCs — each with
# its own SIM800C — can claim and send a job. This spreads outbound SMS
# volume across multiple SIM cards automatically.

def queue_sms_job(phone_number, message):
    """Add an SMS to the shared queue. Any worker (host or remote PC) may
    pick it up. Returns the new job's id."""
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO sms_queue (phone_number, message, status)
        VALUES (?, ?, 'pending')
    """, (phone_number, message))
    job_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return job_id


def claim_next_sms_job(worker_id, stale_minutes=2):
    """
    Called by a worker (host's own background thread, or a remote PC's
    sms_worker.py over the API) asking "is there a job for me?"

    Before looking for a new job, any job that was claimed but never
    completed (e.g. the worker crashed or lost connection) more than
    `stale_minutes` ago is put back to 'pending' so it doesn't get stuck
    forever waiting on a worker that's gone.

    Returns the claimed job row, or None if nothing is pending.
    """
    conn = get_connection()
    now = ph_now().strftime("%Y-%m-%d %H:%M:%S")
    stale_cutoff = (ph_now() - timedelta(minutes=stale_minutes)).strftime("%Y-%m-%d %H:%M:%S")

    # Requeue stale claims first
    conn.execute("""
        UPDATE sms_queue SET status='pending', claimed_by=NULL, claimed_at=NULL
        WHERE status='claimed' AND claimed_at < ?
    """, (stale_cutoff,))
    conn.commit()

    row = conn.execute("""
        SELECT id FROM sms_queue WHERE status='pending'
        ORDER BY created_at ASC LIMIT 1
    """).fetchone()

    if not row:
        conn.close()
        return None

    job_id = row["id"]
    # Atomic-ish claim: only succeeds if still pending (guards against two
    # workers grabbing the same job at nearly the same moment).
    conn.execute("""
        UPDATE sms_queue
        SET status='claimed', claimed_by=?, claimed_at=?, attempts=attempts+1
        WHERE id=? AND status='pending'
    """, (worker_id, now, job_id))
    conn.commit()

    claimed = conn.execute(
        "SELECT * FROM sms_queue WHERE id=? AND claimed_by=?", (job_id, worker_id)
    ).fetchone()
    conn.close()
    return claimed  # None if another worker won the race


def mark_sms_job_complete(job_id, success, error=None):
    """Called by a worker after it actually attempted to send a claimed job."""
    conn = get_connection()
    now = ph_now().strftime("%Y-%m-%d %H:%M:%S")
    status = 'sent' if success else 'failed'
    conn.execute("""
        UPDATE sms_queue SET status=?, completed_at=?, error_message=?
        WHERE id=?
    """, (status, now, error, job_id))
    conn.commit()
    conn.close()


def cancel_sms_job(job_id):
    """
    Cancel ONE specific queued SMS before a worker sends it.
    Only affects jobs still 'pending' — a job a worker has already
    'claimed' (mid-send) can't be safely interrupted, and 'sent'/'failed'
    jobs are already done. Returns True if it was actually cancelled,
    False if it wasn't pending (already claimed/sent/failed/cancelled).
    """
    conn = get_connection()
    now = ph_now().strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute("""
        UPDATE sms_queue SET status='cancelled', completed_at=?
        WHERE id=? AND status='pending'
    """, (now, job_id))
    conn.commit()
    cancelled = cursor.rowcount > 0
    conn.close()
    return cancelled


def cancel_all_pending_sms_jobs():
    """
    Cancel every currently-pending SMS in one go — e.g. to clear out test
    messages queued while troubleshooting, before real workers start
    picking them up. Returns how many were cancelled.
    """
    conn = get_connection()
    now = ph_now().strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute("""
        UPDATE sms_queue SET status='cancelled', completed_at=?
        WHERE status='pending'
    """, (now,))
    conn.commit()
    count = cursor.rowcount
    conn.close()
    return count


def clear_cancelled_sms_jobs():
    """
    Permanently deletes cancelled SMS jobs from the queue log — tidies up
    the SMS Queue Log view without touching sent/failed/pending jobs,
    which stay exactly as they are. Returns how many were removed.
    """
    conn = get_connection()
    cursor = conn.execute("DELETE FROM sms_queue WHERE status='cancelled'")
    conn.commit()
    count = cursor.rowcount
    conn.close()
    return count


def get_sms_queue_stats():
    """Quick counts for an admin visibility view."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT status, COUNT(*) as count FROM sms_queue GROUP BY status
    """).fetchall()
    conn.close()
    stats = {"pending": 0, "claimed": 0, "sent": 0, "failed": 0, "cancelled": 0}
    for r in rows:
        stats[r["status"]] = r["count"]
    return stats


def get_recent_sms_queue_jobs(limit=100):
    """
    Recent SMS queue jobs, most recent first — shows which specific
    worker/port actually handled (or is handling) each one, and its
    status. This is what actually answers "which SIM sent this, and
    which one keeps failing" — the queue table already stores this via
    claimed_by (e.g. "host-COM8"), it just wasn't surfaced anywhere.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM sms_queue
        ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows


def get_blast(blast_id):
    conn = get_connection()
    row  = conn.execute("SELECT * FROM blast_logs WHERE id=?", (blast_id,)).fetchone()
    conn.close()
    return row

def get_recent_blasts(limit=10):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM blast_logs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows

def get_students_for_blast(section=None):
    """Get all students with at least one contact method."""
    conn = get_connection()
    if section and section != 'all':
        rows = conn.execute(
            "SELECT * FROM students WHERE section=? ORDER BY full_name", (section,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM students ORDER BY full_name"
        ).fetchall()
    conn.close()
    return rows

def get_all_sections():
    """Return distinct list of sections for the blast dropdown."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT section FROM students WHERE section != '' ORDER BY section"
    ).fetchall()
    conn.close()
    return [r["section"] for r in rows]


def update_student_messenger_id(rfid_code, messenger_id):
    """Save parent's Messenger ID after they message the page."""
    conn = get_connection()
    conn.execute(
        "UPDATE students SET messenger_id=? WHERE rfid_code=?",
        (messenger_id, rfid_code.upper())
    )
    conn.commit()
    conn.close()


def update_student_status(student_id, status, reason=""):
    """Update student status: active, dropped, transferred, other."""
    conn = get_connection()
    conn.execute(
        "UPDATE students SET status=?, status_reason=? WHERE id=?",
        (status, reason, student_id)
    )
    conn.commit()
    conn.close()


def export_students_to_list():
    """
    Export just the students table as a list of dicts.
    Used for backing up student data without attendance records.
    """
    conn  = get_connection()
    rows  = conn.execute("SELECT * FROM students ORDER BY section, full_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def import_students_merge(students_list):
    """
    Merge new students into the database WITHOUT touching attendance records.

    Matching an existing student:
        1. By LRN first, if the row has one (strongest identifier).
        2. Falling back to an exact full_name match (case-insensitive)
           if no LRN is given, or it doesn't match anyone.
    This correctly recognizes the same student across multiple uploads
    even before they have an RFID code assigned — avoiding duplicate
    entries for students entered without a card on file yet, which
    matching by RFID code alone could not do.

    Updating a match:
        Only fields with actual, non-blank data in the upload overwrite
        the existing value — a blank cell leaves that field untouched
        rather than wiping out previously saved info (e.g. re-uploading
        a row with no phone number filled in won't erase an
        already-saved phone number).

    - Match found  → update only the non-blank fields provided
    - No match     → insert as a new student
    - Never deletes existing students or attendance logs
    Returns (added, updated) counts.
    """
    added = 0; updated = 0
    for s in students_list:
        rfid         = (s.get("rfid_code") or "").strip().upper()
        name         = (s.get("full_name") or "").strip()
        if not name:
            continue
        lrn          = (s.get("lrn") or "").strip()
        section      = (s.get("section") or "").strip()
        parent_name  = (s.get("parent_name") or "").strip()
        parent_phone = (s.get("parent_phone") or "").strip()

        conn = get_connection()

        existing = None
        if lrn:
            existing = conn.execute(
                "SELECT * FROM students WHERE lrn=?", (lrn,)
            ).fetchone()
        if not existing:
            existing = conn.execute(
                "SELECT * FROM students WHERE LOWER(full_name)=LOWER(?)", (name,)
            ).fetchone()

        if existing:
            # Only overwrite a field if the upload actually provided
            # something for it — otherwise keep what's already saved.
            merged_rfid         = rfid or existing["rfid_code"] or ""
            merged_section      = section or existing["section"] or ""
            merged_parent_name  = parent_name or existing["parent_name"] or ""
            merged_parent_phone = parent_phone or existing["parent_phone"] or ""
            merged_lrn          = lrn or existing["lrn"] or ""
            try:
                conn.execute("""
                    UPDATE students SET full_name=?, rfid_code=?, section=?,
                    parent_name=?, parent_phone=?, lrn=?
                    WHERE id=?
                """, (name, merged_rfid, merged_section, merged_parent_name,
                      merged_parent_phone, merged_lrn, existing["id"]))
                updated += 1
            except psycopg2.IntegrityError:
                # merged_rfid collides with a DIFFERENT existing student's
                # RFID code — skip rather than crash the whole import.
                conn._conn.rollback()
                print(f"⚠️  Could not update {name}: RFID {merged_rfid} "
                      f"already belongs to another student.")
        else:
            try:
                conn.execute("""
                    INSERT INTO students
                        (rfid_code, full_name, section, parent_name, parent_phone, lrn)
                    VALUES (?,?,?,?,?,?)
                """, (rfid, name, section, parent_name, parent_phone, lrn))
                added += 1
            except Exception:
                conn._conn.rollback()
        conn.commit()
        conn.close()
    return added, updated


def update_student_tracker(student_id, field, value):
    """Update tracker fields: id_printed, id_distributed."""
    if field not in ("id_printed", "id_distributed"):
        raise ValueError(f"Invalid tracker field: {field}")
    conn = get_connection()
    conn.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS id_printed INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS id_distributed INTEGER DEFAULT 0")
    conn.commit()
    conn.execute(
        f"UPDATE students SET {field}=? WHERE id=?",
        (1 if value else 0, student_id)
    )
    conn.commit()
    conn.close()


def get_students_never_scanned():
    """Return active students who have never scanned."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT s.* FROM students s
        LEFT JOIN attendance_logs a ON s.id = a.student_id
        WHERE s.status = 'active' AND a.id IS NULL
        ORDER BY s.section, s.full_name
    """).fetchall()
    conn.close()
    return rows
