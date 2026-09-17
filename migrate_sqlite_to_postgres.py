"""
migrate_sqlite_to_postgres.py
------------------------------
One-time migration script: copies all existing data from your local
attendance.db (SQLite) into the new Railway Postgres database.

Run this ONCE, from the host PC where attendance.db actually lives,
AFTER you've deployed the new Postgres-based app to Railway (so the
tables already exist there), but BEFORE you start relying on Railway
for real use.

Usage:
    pip install psycopg2-binary --break-system-packages
    python migrate_sqlite_to_postgres.py

You'll be prompted for:
  1. Path to your local attendance.db (defaults to the same folder)
  2. Your Railway Postgres DATABASE_URL (copy this from Railway's
     dashboard → your Postgres service → "Connect" tab → look for a
     variable like DATABASE_URL or DATABASE_PUBLIC_URL)

This migrates: students, attendance_logs, and settings — the data that
actually matters for continuity. It does NOT migrate blast_logs,
blast_recipients, or sms_queue, since those are transient operational
logs, not records you need to preserve long-term.

Safe to run more than once if something goes wrong partway — it clears
out any previously-migrated rows in the target tables first, so you
won't end up with duplicates from a re-run.
"""

import sqlite3
import psycopg2
import psycopg2.extras
import sys
import os


def migrate(sqlite_path, postgres_url):
    print(f"Connecting to SQLite: {sqlite_path}")
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    print("Connecting to Postgres...")
    pg_conn = psycopg2.connect(postgres_url)
    pg_cursor = pg_conn.cursor()

    # ── STUDENTS ─────────────────────────────────────────────────────────
    print("\n=== Migrating students ===")
    pg_cursor.execute("DELETE FROM students")  # clear any previous migration attempt
    students = sqlite_conn.execute("SELECT * FROM students").fetchall()
    print(f"Found {len(students)} students in SQLite.")

    max_id = 0
    for s in students:
        s = dict(s)
        pg_cursor.execute("""
            INSERT INTO students
                (id, rfid_code, full_name, section, parent_name, parent_phone,
                 messenger_id, photo, status, status_reason, created_at, lrn,
                 id_printed, id_distributed, messenger_id_2, parent_name_2, parent_phone_2)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            s.get("id"), s.get("rfid_code"), s.get("full_name"), s.get("section"),
            s.get("parent_name"), s.get("parent_phone"), s.get("messenger_id"),
            s.get("photo"), s.get("status") or "active", s.get("status_reason"),
            s.get("created_at"), s.get("lrn"), s.get("id_printed") or 0,
            s.get("id_distributed") or 0, s.get("messenger_id_2"),
            s.get("parent_name_2"), s.get("parent_phone_2"),
        ))
        max_id = max(max_id, s.get("id") or 0)

    if max_id:
        pg_cursor.execute(f"SELECT setval('students_id_seq', {max_id})")
    pg_conn.commit()
    print(f"✅ Migrated {len(students)} students.")

    # ── ATTENDANCE LOGS ──────────────────────────────────────────────────
    print("\n=== Migrating attendance logs ===")
    pg_cursor.execute("DELETE FROM attendance_logs")
    logs = sqlite_conn.execute("SELECT * FROM attendance_logs").fetchall()
    print(f"Found {len(logs)} attendance log entries in SQLite.")

    max_log_id = 0
    batch = []
    for log in logs:
        log = dict(log)
        batch.append((
            log.get("id"), log.get("student_id"), log.get("rfid_code"),
            log.get("scan_type"), log.get("scan_time"), log.get("scan_date"),
            log.get("notified") or 0, log.get("notify_channel"),
        ))
        max_log_id = max(max_log_id, log.get("id") or 0)

    if batch:
        psycopg2.extras.execute_batch(pg_cursor, """
            INSERT INTO attendance_logs
                (id, student_id, rfid_code, scan_type, scan_time, scan_date, notified, notify_channel)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, batch, page_size=500)

    if max_log_id:
        pg_cursor.execute(f"SELECT setval('attendance_logs_id_seq', {max_log_id})")
    pg_conn.commit()
    print(f"✅ Migrated {len(logs)} attendance log entries.")

    # ── SETTINGS ─────────────────────────────────────────────────────────
    print("\n=== Migrating settings ===")
    settings = sqlite_conn.execute("SELECT * FROM settings").fetchall()
    print(f"Found {len(settings)} settings in SQLite.")

    for row in settings:
        row = dict(row)
        pg_cursor.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (row.get("key"), row.get("value")))
    pg_conn.commit()
    print(f"✅ Migrated {len(settings)} settings.")

    sqlite_conn.close()
    pg_conn.close()

    print("\n🎉 Migration complete!")
    print(f"   {len(students)} students")
    print(f"   {len(logs)} attendance log entries")
    print(f"   {len(settings)} settings")
    print("\nDouble-check the numbers above match what you expect before")
    print("relying on the Railway deployment for real use.")


if __name__ == "__main__":
    print("=" * 55)
    print("  SQLite → Postgres Migration")
    print("=" * 55)

    default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db")
    sqlite_path = input(f"Path to attendance.db [{default_path}]: ").strip() or default_path

    if not os.path.exists(sqlite_path):
        print(f"❌ File not found: {sqlite_path}")
        sys.exit(1)

    postgres_url = input("Railway Postgres DATABASE_URL: ").strip()
    if not postgres_url:
        print("❌ A Postgres connection URL is required.")
        sys.exit(1)

    confirm = input(
        "\n⚠️  This will REPLACE any existing students/attendance_logs data "
        "already in the target Postgres database. Continue? (yes/no): "
    ).strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        sys.exit(0)

    migrate(sqlite_path, postgres_url)
