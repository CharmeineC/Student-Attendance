"""
diagnose_lrn.py
----------------
READ-ONLY diagnostic. Looks up a specific student by name and by LRN to
show EXACTLY what's stored in the production database — including
hidden whitespace, leading zeros, or formatting differences that would
be invisible just looking at a spreadsheet or the admin page, but would
silently break the "exact match" lookup used when a parent tries to
link via Messenger.

Makes NO changes to the database. Safe to run anytime.

HOW TO RUN (same as before, from your own computer):
    railway run python3 diagnose_lrn.py
"""

import os
import sys

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Missing dependency. Run: pip install psycopg2-binary --break-system-packages")
    sys.exit(1)

DATABASE_URL = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
if DATABASE_URL and "railway.internal" in DATABASE_URL:
    print("❌ Only the INTERNAL database address is available here.")
    print("   Turn on 'Public Networking' on the Postgres service (Settings -> Networking) and retry.")
    sys.exit(1)
if not DATABASE_URL:
    print("❌ DATABASE_URL not set. Run this with: railway run python3 diagnose_lrn.py")
    sys.exit(1)

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

TYPED_LRN = "129509230110"
NAME_SEARCH = "ORILLA"

print("=" * 70)
print(f"Looking for LRN exactly matching: {TYPED_LRN!r}")
print("=" * 70)
cur.execute("SELECT id, full_name, lrn, rfid_code, section, messenger_id, messenger_id_2 "
            "FROM students WHERE lrn = %s", (TYPED_LRN,))
exact = cur.fetchall()
if exact:
    for row in exact:
        print(dict(row))
else:
    print("No exact match found for that LRN.")

print()
print("=" * 70)
print(f"Searching by name containing: {NAME_SEARCH!r}")
print("=" * 70)
like_pattern = "%" + NAME_SEARCH + "%"
cur.execute("SELECT id, full_name, lrn, rfid_code, section, messenger_id, messenger_id_2 "
            "FROM students WHERE full_name ILIKE %s", (like_pattern,))
by_name = cur.fetchall()
if by_name:
    for row in by_name:
        r = dict(row)
        stored_lrn = r.get("lrn") or ""
        print(r)
        print(f"   -> stored LRN repr: {stored_lrn!r}  (length: {len(stored_lrn)})")
        print(f"   -> typed LRN repr:  {TYPED_LRN!r}  (length: {len(TYPED_LRN)})")
        print(f"   -> exact match with typed LRN? {stored_lrn == TYPED_LRN}")
        print(f"   -> same digits, ignoring leading zeros? "
              f"{stored_lrn.lstrip('0') == TYPED_LRN.lstrip('0')}")
else:
    print("No student found with that name at all — possible the student "
          "was never imported into this app's database.")

conn.close()
