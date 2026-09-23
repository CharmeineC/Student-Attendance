import re
"""
app.py
------
The main web server for the RFID Attendance System.
Built with Flask — a lightweight Python web framework.

To start the server, run:
    python app.py

Then open a browser on any PC on the same network and go to:
    http://[this PC's IP address]:5000

Pages:
    /           → Scanner page (for gate PC and kiosk)
    /admin      → Admin dashboard (attendance overview)
    /students   → Student list (add, edit, delete, import)
    /reports    → Export attendance reports
    /settings   → Configure school name, SMS, notifications

Install requirements:
    pip install flask openpyxl pillow pyserial requests
"""

from flask import (
    Flask, render_template, request, jsonify,
    send_file, redirect, url_for, send_from_directory
)
from werkzeug.utils import secure_filename
import os
import socket
from datetime import datetime
from database import (
    setup_database, get_student_by_rfid, get_student_by_lrn, record_scan,
    determine_scan_type, get_today_stats, get_all_students,
    add_student, update_student, delete_student,
    get_logs_for_report, get_today_logs, get_setting,
    save_setting, get_all_settings, import_students_from_list,
    ph_now
)
from notifier import send_notification
from reports import export_monthly_report
import threading

# ── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = "rfid_school_2026"

# Always initialize the database when the app loads — regardless of how
# it was started (python app.py, import, batch file, or scheduled task).
# setup_database() uses CREATE TABLE IF NOT EXISTS so it's safe to call
# every time — it won't touch existing data.
setup_database()

UPLOAD_FOLDER   = os.path.join("static", "uploads")
ALLOWED_IMAGES  = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_IMAGE_SIZE  = 5 * 1024 * 1024    # 5MB — kept as the per-file guideline
MAX_REQUEST_SIZE = 200 * 1024 * 1024  # 200MB — total request size, generous
                                       # enough for many photos at once (bulk
                                       # photo import sends them all in one
                                       # request, not one at a time)

app.config["UPLOAD_FOLDER"]    = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_SIZE

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_image(filename):
    return "." in filename and \
           filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGES


def save_resized_photo(file_obj, save_path, max_dimension=900, jpeg_quality=85):
    """
    Resizes and compresses an uploaded photo before saving it. Sized for
    the LARGEST actual display context in this app — the scanner
    confirmation screen shows the photo at up to 70% of the viewport
    height (see scanner.html's .confirm-photo, max-height:70vh), not
    just the small 32-44px thumbnails used in list views. 900px keeps
    that large confirmation view sharp on most screens, while still
    being dramatically smaller than typical full camera-resolution
    photos (which are often 3000-4000px+ for no visible benefit here).
    """
    try:
        from PIL import Image
        img = Image.open(file_obj)
        img_format = img.format  # e.g. "JPEG", "PNG" — preserve as-is

        # Only shrink if actually larger than the target — never upscale
        # a smaller photo, which would just waste space pointlessly.
        img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

        save_kwargs = {}
        if img_format == "JPEG":
            # Flatten any transparency onto white first — JPEG has no
            # alpha channel, and saving RGBA content as JPEG raises an
            # error rather than just quietly dropping it.
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            save_kwargs = {"quality": jpeg_quality, "optimize": True}
        elif img_format == "PNG":
            save_kwargs = {"optimize": True}

        img.save(save_path, format=img_format, **save_kwargs)
    except Exception as e:
        print(f"⚠️  Could not resize photo ({e}) — saving original instead.")
        file_obj.seek(0)
        if hasattr(file_obj, "save"):
            # Werkzeug FileStorage object (from request.files)
            file_obj.save(save_path)
        else:
            # Plain file handle (e.g. open(path, "rb"))
            with open(save_path, "wb") as out:
                out.write(file_obj.read())


def get_local_ip():
    """Get this PC's local IP address for display."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── SCANNER PAGE ─────────────────────────────────────────────────────────────

@app.route("/")
def scanner():
    """Main scanner page — shown on gate PC and kiosk."""
    school_name = get_setting("school_name") or "School"
    announcement = get_setting("announcement") or ""
    school_logo  = get_setting("school_logo")  or ""
    return render_template("scanner.html",
                           school_name=school_name,
                           announcement=announcement,
                           school_logo=school_logo)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """
    Called when a student scans their card.
    Receives the RFID code, looks up the student,
    records the scan, and triggers notifications.
    Returns JSON with student info for the confirmation screen.
    """
    data = request.get_json()
    code = (data.get("rfid_code") or "").strip().upper()

    if not code:
        return jsonify({"success": False, "error": "No card code received."})

    student = get_student_by_rfid(code)
    if not student:
        return jsonify({
            "success":  False,
            "error":    "Card not registered.",
            "rfid_code": code
        })

    scan_type = determine_scan_type(student["id"])
    scan_time = ph_now().strftime("%H:%M:%S")
    log_id    = record_scan(student["id"], code, scan_type)

    # Send notification in background so page responds instantly
    threading.Thread(
        target=send_notification,
        args=(dict(student), log_id, scan_type, scan_time),
        daemon=True
    ).start()

    # Build photo URL
    photo_url = None
    if student["photo"]:
        photo_url = url_for("static", filename=f"uploads/{student['photo']}")

    return jsonify({
        "success":    True,
        "name":       student["full_name"],
        "section":    student["section"],
        "scan_type":  scan_type,
        "scan_time":  ph_now().strftime("%I:%M %p"),
        "scan_date":  ph_now().strftime("%B %d, %Y"),
        "photo_url":  photo_url,
        "rfid_code":  code,
    })


@app.route("/api/today_stats")
def api_today_stats():
    """Live stats for the scanner dashboard — refreshed every 10 seconds."""
    stats = get_today_stats()
    logs  = get_today_logs(limit=8)
    return jsonify({
        "stats": stats,
        "recent": [
            {
                "name":      log["full_name"],
                "section":   log["section"],
                "scan_type": log["scan_type"],
                "scan_time": log["scan_time"],
            }
            for log in logs
        ]
    })


# ── ADMIN DASHBOARD ───────────────────────────────────────────────────────────

@app.route("/admin")
def admin():
    """Admin dashboard — attendance overview for today."""
    stats = get_today_stats()
    logs  = get_today_logs(limit=50)
    school_name = get_setting("school_name") or "School"
    return render_template("admin.html",
                           stats=stats, logs=logs,
                           school_name=school_name,
                           today=ph_now().strftime("%A, %B %d, %Y"))


# ── STUDENTS ──────────────────────────────────────────────────────────────────

@app.route("/students")
def students():
    """Student registry — list all students with section/grade filter."""
    from database import get_all_sections
    school_name     = get_setting("school_name") or "School"
    section_filter  = request.args.get("section", "").strip()
    all_students    = get_all_students()
    sections        = get_all_sections()

    # Extract unique grade levels from sections
    grades = []
    for sec in sections:
        grade = sec.split("-")[0].strip() if "-" in sec else sec.strip()
        if grade and grade not in grades:
            grades.append(grade)

    if section_filter:
        filtered = [s for s in all_students if s["section"] == section_filter]
    else:
        filtered = all_students

    return render_template("students.html",
                           students=filtered,
                           all_count=len(all_students),
                           school_name=school_name,
                           sections=sections,
                           grades=grades,
                           selected_section=section_filter)


@app.route("/students/add", methods=["POST"])
def student_add():
    """Add a single student manually."""
    photo_filename = None

    # Handle photo upload
    if "photo" in request.files:
        photo = request.files["photo"]
        if photo and photo.filename and allowed_image(photo.filename):
            filename = secure_filename(photo.filename)
            # Prepend timestamp to avoid name collisions
            filename = f"{int(datetime.now().timestamp())}_{filename}"
            save_resized_photo(photo, os.path.join(app.config["UPLOAD_FOLDER"], filename))
            photo_filename = filename

    add_student(
        rfid_code    = request.form.get("rfid_code", "").strip().upper(),
        full_name    = request.form.get("full_name", "").strip(),
        section      = request.form.get("section", "").strip(),
        parent_name  = request.form.get("parent_name", "").strip(),
        parent_phone = request.form.get("parent_phone", "").strip(),
        photo        = photo_filename,
        lrn          = request.form.get("lrn", "").strip(),
    )
    section_filter = request.form.get("section_filter", "").strip()
    return redirect(url_for("students", section=section_filter) if section_filter else url_for("students"))


@app.route("/students/edit/<int:student_id>", methods=["POST"])
def student_edit(student_id):
    """Edit an existing student's details."""
    photo_filename = None

    if "photo" in request.files:
        photo = request.files["photo"]
        if photo and photo.filename and allowed_image(photo.filename):
            filename = secure_filename(photo.filename)
            filename = f"{int(datetime.now().timestamp())}_{filename}"
            save_resized_photo(photo, os.path.join(app.config["UPLOAD_FOLDER"], filename))
            photo_filename = filename

    update_student(
        student_id   = student_id,
        rfid_code    = request.form.get("rfid_code", "").strip().upper(),
        full_name    = request.form.get("full_name", "").strip(),
        section      = request.form.get("section", "").strip(),
        parent_name  = request.form.get("parent_name", "").strip(),
        parent_phone = request.form.get("parent_phone", "").strip(),
        photo        = photo_filename,
        lrn          = request.form.get("lrn", "").strip(),
    )
    section_filter = request.form.get("section_filter", "").strip()
    return redirect(url_for("students", section=section_filter) if section_filter else url_for("students"))


@app.route("/students/delete/<int:student_id>", methods=["POST"])
def student_delete(student_id):
    """Delete a student."""
    delete_student(student_id)
    section_filter = request.form.get("section_filter", "").strip()
    return redirect(url_for("students", section=section_filter) if section_filter else url_for("students"))


@app.route("/students/import", methods=["POST"])
def student_import():
    """
    Import students from an Excel (.xlsx) or CSV file.
    Expected columns (in any order):
        full_name, rfid_code, section, parent_name, parent_phone
    First row must be the header row with these column names.
    """
    section_filter = request.form.get("section_filter", "").strip()
    extra = {"section": section_filter} if section_filter else {}

    if "file" not in request.files:
        return redirect(url_for("students", **extra))

    file = request.files["file"]
    if not file or not file.filename:
        return redirect(url_for("students", **extra))

    filename  = secure_filename(file.filename)
    temp_path = os.path.join("static", "uploads", f"import_{filename}")
    file.save(temp_path)

    try:
        count = import_students_from_excel(temp_path)
        os.remove(temp_path)
        return redirect(url_for("students", imported=count, **extra))
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return redirect(url_for("students", error=str(e), **extra))


def import_students_from_excel(filepath):
    """Read Excel or CSV and import students. Returns count of imported rows."""
    import openpyxl
    import csv

    students_data = []

    def normalize_headers(row_dict):
        """Normalize column names: lowercase, strip spaces, replace spaces with underscores.
        This makes 'Full Name', 'full name', 'FULL NAME', 'full_name' all match."""
        return {k.strip().lower().replace(" ", "_"): v for k, v in row_dict.items() if k}

    if filepath.endswith(".csv"):
        with open(filepath, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                students_data.append(normalize_headers(row))
    else:
        wb   = openpyxl.load_workbook(filepath)
        ws   = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return 0
        headers = [str(h).strip().lower().replace(" ", "_") if h else "" for h in rows[0]]
        for row in rows[1:]:
            if not any(row):
                continue
            students_data.append(dict(zip(headers, row)))

    students_list = []
    for row in students_data:
        # Flexible column name matching — handles many header variations
        name   = str(row.get("full_name") or row.get("name") or row.get("student_name") or
                     row.get("fullname") or row.get("student") or "").strip()
        rfid   = str(row.get("rfid_code") or row.get("rfid") or row.get("card_id") or
                     row.get("rfid_no") or row.get("card_no") or row.get("rfidcode") or "").strip().upper()
        sec    = str(row.get("section") or row.get("grade") or row.get("class") or
                     row.get("grade_section") or row.get("grade_&_section") or
                     row.get("grade_and_section") or "").strip()
        pname  = str(row.get("parent_name") or row.get("guardian") or row.get("parent") or
                     row.get("guardian_name") or row.get("emergency_contact_name") or
                     row.get("contact_name") or "").strip()
        pphone = str(row.get("parent_phone") or row.get("phone") or row.get("contact") or
                     row.get("parent_contact") or row.get("contact_number") or
                     row.get("emergency_contact_number") or "").strip()
        lrn    = _fix_lrn(row.get("lrn") or "")

        if not name:
            continue

        students_list.append({
            "rfid_code":    rfid,
            "full_name":    name,
            "section":      sec,
            "parent_name":  pname,
            "parent_phone": pphone,
            "lrn":          lrn,
        })

    # Merge import:
    # - New students → added fresh
    # - Existing students → name/section/parent updated, messenger_id preserved
    # - No students are ever deleted
    from database import import_students_merge
    added, updated = import_students_merge(students_list)
    return added + updated


# ── REPORTS ───────────────────────────────────────────────────────────────────

@app.route("/reports")
def reports():
    from database import get_all_sections
    school_name = get_setting("school_name") or "School"
    sections    = get_all_sections()
    return render_template("reports.html",
                           school_name=school_name,
                           sections=sections,
                           current_month=ph_now().month,
                           current_year=ph_now().year)


@app.route("/reports/export")
def reports_export():
    """Generate and download the Excel report — filtered by section or grade."""
    month       = int(request.args.get("month", ph_now().month))
    year        = int(request.args.get("year",  ph_now().year))
    section     = request.args.get("section",     "").strip() or None
    grade_level = request.args.get("grade_level", "").strip() or None

    # If grade level selected but no specific section, export all sections in that grade
    if grade_level and not section:
        from database import get_all_sections
        grade_sections = [s for s in get_all_sections()
                         if s.split("-")[0].strip() == grade_level or s.strip() == grade_level]
        # Export each section or combine — here we pass grade_level as a prefix filter
        section = grade_level  # reports.py will do a LIKE filter

    os.makedirs("reports", exist_ok=True)
    filepath = export_monthly_report(year, month, section, output_folder="reports",
                                     grade_level=grade_level if not section or section == grade_level else None)

    return send_file(
        filepath,
        as_attachment=True,
        download_name=os.path.basename(filepath)
    )


# ── SETTINGS ─────────────────────────────────────────────────────────────────

@app.route("/settings")
def settings():
    all_settings = get_all_settings()
    school_name  = get_setting("school_name") or "School"
    # Computed from the actual incoming request, so this is always correct —
    # whatever the current public address is (Railway, ngrok, anything else),
    # not a hardcoded placeholder that goes stale.
    live_webhook_url = request.host_url.rstrip("/") + "/webhook"
    return render_template("settings.html",
                           settings=all_settings,
                           school_name=school_name,
                           live_webhook_url=live_webhook_url)


@app.route("/settings/save", methods=["POST"])
def settings_save():
    for key, value in request.form.items():
        save_setting(key, value.strip())
    return redirect(url_for("settings") + "?saved=1")


@app.route("/api/announcement")
def api_announcement():
    """Returns current announcement text for the ticker."""
    return jsonify({"text": get_setting("announcement") or ""})


@app.route("/api/detect_com_port")
def api_detect_com_port():
    """
    Auto-detect the SIM800C COM port. If "sim800c_candidate_ports" is
    set (e.g. "COM8,COM9,COM10"), only those specific ports are tested —
    faster and avoids touching unrelated serial devices. Otherwise falls
    back to scanning every serial port on the system.
    """
    try:
        from notifier import _test_sim800c
        candidates_setting = (get_setting("sim800c_candidate_ports") or "").strip()

        if candidates_setting:
            port_names = [p.strip() for p in candidates_setting.split(",") if p.strip()]
            for port_name in port_names:
                if _test_sim800c(port_name):
                    save_setting("sim800c_port", port_name)
                    return jsonify({"found": True, "port": port_name})
            return jsonify({"found": False, "available": port_names})
        else:
            import serial.tools.list_ports
            ports = list(serial.tools.list_ports.comports())
            for port in ports:
                if _test_sim800c(port.device):
                    save_setting("sim800c_port", port.device)
                    return jsonify({"found": True, "port": port.device})
            available = [p.device for p in ports]
            return jsonify({"found": False, "available": available})
    except Exception as e:
        return jsonify({"found": False, "error": str(e)})


@app.route("/api/test_port/<port_name>")
def api_test_specific_port(port_name):
    """
    Quick, on-demand test of ONE specific port (e.g. /api/test_port/COM9)
    — no server restart needed. Useful for testing each SIM800C
    individually while troubleshooting, without cycling the whole app.
    """
    from notifier import _test_sim800c
    ok = _test_sim800c(port_name)
    return jsonify({"port": port_name, "responded": ok})


@app.route("/api/test_sms", methods=["POST"])
def api_test_sms():
    """Send a test SMS to a given phone number via SIM800C.
    Optional "port" in the request body tests a specific SIM (useful on
    a multi-SIM host to check each one's signal/registration individually).
    """
    data  = request.get_json() or {}
    phone = data.get("phone", "").strip()
    port  = (data.get("port") or "").strip() or None
    if not phone:
        return jsonify({"success": False, "message": "Please enter a phone number."})
    try:
        from notifier import test_sms
        success, message = test_sms(phone, port=port)
        return jsonify({"success": success, "message": message})
    except Exception as e:
        return jsonify({"success": False, "message": f"Error: {str(e)}"})


@app.route("/api/holiday_mode", methods=["GET"])
def api_holiday_mode_status():
    """
    Check whether Holiday Mode is currently active.
    Auto-expires it if an end date was set and has passed — so admins who
    forget to manually turn it off don't leave the scheduler paused
    indefinitely.
    """
    from notifier import _is_holiday_mode_active
    active = _is_holiday_mode_active()
    until = get_setting("holiday_mode_until") or None
    return jsonify({"active": active, "until": until if active else None})


@app.route("/api/holiday_mode", methods=["POST"])
def api_holiday_mode_toggle():
    """
    Turn Holiday Mode on or off.
    Turning ON pauses the automatic inactivity keep-alive scheduler until
    turned back off. By default it ALSO immediately sends one explicit
    announcement to every linked parent (Messenger, with SMS as
    fallback) — reusing the same logic as the Blast feature — but this
    can be skipped entirely (skip_announcement: true) for cases where
    only the internal keep-alive pause is wanted, without notifying
    parents at all.
    Turning OFF just resumes normal automatic behavior; no message is sent.

    Body (JSON, optional):
        {"active": true/false, "message": "custom text",
         "until": "YYYY-MM-DD", "skip_announcement": true/false}
    "until" is optional — if provided, Holiday Mode auto-expires after
    that date even if nobody manually turns it back off.
    """
    data = request.get_json() or {}
    turning_on = data.get("active", True)
    custom_message = (data.get("message") or "").strip() or None
    until_date = (data.get("until") or "").strip() or None
    skip_announcement = bool(data.get("skip_announcement", False))

    save_setting("holiday_mode", "1" if turning_on else "0")
    save_setting("holiday_mode_until", until_date if turning_on else "")

    result = {"success": True, "active": turning_on, "until": until_date}
    if turning_on:
        if skip_announcement:
            result["announcement_skipped"] = True
        else:
            try:
                from notifier import send_holiday_announcement
                sent, failed = send_holiday_announcement(custom_message, until_date)
                result["announcement_sent"] = sent
                result["announcement_failed"] = failed
            except Exception as e:
                result["announcement_error"] = str(e)
    return jsonify(result)


@app.route("/api/blast_recipients/<int:blast_id>")
def api_blast_recipients(blast_id):
    """
    Per-recipient delivery detail for one blast (or Holiday Mode
    announcement) — which channel was used for each parent, and whether
    it actually succeeded, rather than just the blast's aggregate
    sent/failed counts.
    """
    from database import get_blast_recipients, get_blast
    blast = get_blast(blast_id)
    if not blast:
        return jsonify({"error": "not found"}), 404
    rows = get_blast_recipients(blast_id)
    recipients = [{
        "student_name": r["student_name"],
        "section": r["section"],
        "channel": r["channel"],
        "success": bool(r["success"]),
        "sent_at": r["sent_at"],
    } for r in rows]
    return jsonify({"blast_id": blast_id, "recipients": recipients})


@app.route("/api/sms_queue/next")
def api_sms_queue_next():
    """
    Called by a remote PC's sms_worker.py (or the host's own internal
    worker) asking "is there an SMS job for me to send?" Claims and
    returns the oldest pending job, or null if the queue is empty.

    Query param: worker_id (identifies which PC/SIM is claiming this job,
    used for the stale-claim requeue safety net if that worker never
    reports back).
    """
    from database import claim_next_sms_job
    worker_id = request.args.get("worker_id", "unknown")
    job = claim_next_sms_job(worker_id)
    if not job:
        return jsonify({"job": None})
    return jsonify({"job": {
        "id": job["id"],
        "phone_number": job["phone_number"],
        "message": job["message"],
    }})


@app.route("/api/sms_queue/complete", methods=["POST"])
def api_sms_queue_complete():
    """
    Called by a worker after it actually attempted to send a claimed job,
    reporting back whether it succeeded.
    Body (JSON): {"job_id": int, "worker_id": str, "success": bool,
                  "error": str|null, "message_ref": int|null}
    message_ref is the modem's reference number for this SMS, needed to
    later match an asynchronous delivery report back to this exact job.
    """
    from database import mark_sms_job_complete
    data = request.get_json() or {}
    job_id = data.get("job_id")
    success = bool(data.get("success"))
    error = data.get("error")
    message_ref = data.get("message_ref")
    if job_id is None:
        return jsonify({"success": False, "message": "job_id required"}), 400
    mark_sms_job_complete(job_id, success, error, message_ref=message_ref)
    return jsonify({"success": True})


@app.route("/api/sms_queue/stats")
def api_sms_queue_stats():
    """Quick counts for an admin visibility view of the SMS queue."""
    from database import get_sms_queue_stats
    return jsonify(get_sms_queue_stats())


@app.route("/api/sms_queue/cancel/<int:job_id>", methods=["POST"])
def api_sms_queue_cancel_one(job_id):
    """
    Cancel one specific queued SMS before a worker sends it. Only works
    on jobs still 'pending' — one a worker has already claimed can't be
    safely interrupted mid-send.
    """
    from database import cancel_sms_job
    cancelled = cancel_sms_job(job_id)
    if cancelled:
        return jsonify({"success": True, "message": "Cancelled."})
    return jsonify({"success": False,
                     "message": "Couldn't cancel — it may have already been claimed or sent."})


@app.route("/api/sms_queue/cancel_all_pending", methods=["POST"])
def api_sms_queue_cancel_all_pending():
    """
    Cancel every currently-pending SMS at once — e.g. to clear out test
    messages before real workers start picking them up.
    """
    from database import cancel_all_pending_sms_jobs
    count = cancel_all_pending_sms_jobs()
    return jsonify({"success": True, "cancelled": count,
                     "message": f"Cancelled {count} pending SMS job(s)."})


@app.route("/api/sms_queue/clear_cancelled", methods=["POST"])
def api_sms_queue_clear_cancelled():
    """
    Permanently remove cancelled jobs from the SMS Queue Log to tidy it
    up — sent, failed, and pending jobs are untouched.
    """
    from database import clear_cancelled_sms_jobs
    count = clear_cancelled_sms_jobs()
    return jsonify({"success": True, "cleared": count,
                     "message": f"Cleared {count} cancelled job(s)."})


@app.route("/api/sms_queue/recent")
def api_sms_queue_recent():
    """
    Recent SMS jobs with which specific SIM/port handled each one and
    its outcome — answers "which port sent this" and "which port keeps
    failing" directly, for troubleshooting a multi-SIM host setup.
    """
    from database import get_recent_sms_queue_jobs
    rows = get_recent_sms_queue_jobs(limit=100)
    jobs = [{
        "id": r["id"],
        "phone_number": r["phone_number"],
        "message": r["message"][:60] + ("..." if len(r["message"]) > 60 else ""),
        "status": r["status"],
        "sent_via": r["claimed_by"],  # e.g. "host-COM8", or a PC hostname for remote workers
        "created_at": r["created_at"],
        "completed_at": r["completed_at"],
        "error_message": r["error_message"],
    } for r in rows]
    return jsonify({"jobs": jobs})


@app.route("/students/bulk_photo")
def bulk_photo_page():
    """Bulk photo import — upload many photos at once, auto-matched to
    students by reading the LRN printed on each photo via OCR."""
    school_name = get_setting("school_name") or "School"
    return render_template("bulk_photo.html", school_name=school_name)


BULK_PHOTO_TMP = os.path.join(UPLOAD_FOLDER, "_bulk_photo_tmp")


@app.route("/api/bulk_photo/scan", methods=["POST"])
def api_bulk_photo_scan():
    """
    Accepts multiple photo files. For each one, first checks whether the
    FILENAME itself already contains a 12-digit LRN (e.g.
    "129509250161.jpg") — if so, matches directly from that, no OCR
    needed at all. Only falls back to OCR (reading text printed in the
    image itself) for files whose name doesn't already contain an LRN.

    Does NOT save anything permanently yet — photos go to a temp folder
    and the match results are returned for the admin to review/correct
    on screen first. Nothing is attached to a student record until
    /api/bulk_photo/confirm is called.
    """
    import re
    from database import get_student_by_lrn

    os.makedirs(BULK_PHOTO_TMP, exist_ok=True)
    results = []

    # OCR libraries are only imported if actually needed (i.e. at least
    # one file's filename doesn't already contain an LRN) — this means
    # filename-only workflows work perfectly even with no OCR installed.
    ocr_available = None  # None = not checked yet, True/False once known
    pytesseract = None
    Image = None

    for photo in request.files.getlist("photos"):
        if not photo or not photo.filename or not allowed_image(photo.filename):
            continue

        tmp_name = f"{int(datetime.now().timestamp() * 1000)}_{secure_filename(photo.filename)}"
        tmp_path = os.path.join(BULK_PHOTO_TMP, tmp_name)
        photo.save(tmp_path)

        detected_lrn = ""
        matched_name = None
        matched_student_id = None
        error = None
        source = None  # "filename" or "ocr", for the review screen

        # 1. Try the filename first — fast, free, no dependencies.
        filename_matches = re.findall(r"\d{12}", photo.filename)
        if filename_matches:
            detected_lrn = filename_matches[0]
            source = "filename"
        else:
            # 2. Fall back to OCR only if the filename didn't have one.
            if ocr_available is None:
                try:
                    import pytesseract as _pytesseract
                    from PIL import Image as _Image
                    pytesseract = _pytesseract
                    Image = _Image
                    tesseract_path = get_setting("tesseract_path") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
                    if os.path.exists(tesseract_path):
                        pytesseract.pytesseract.tesseract_cmd = tesseract_path
                    ocr_available = True
                except ImportError:
                    ocr_available = False

            if ocr_available:
                try:
                    img = Image.open(tmp_path)
                    text = pytesseract.image_to_string(img)
                    lrn_matches = re.findall(r"\b\d{12}\b", text)
                    if lrn_matches:
                        detected_lrn = lrn_matches[0]
                        source = "ocr"
                except Exception as e:
                    error = str(e)
            else:
                error = ("No LRN found in filename, and OCR isn't installed "
                         "to read it from the image. Either rename the file "
                         "to include the LRN, or install pytesseract + Tesseract OCR.")

        if detected_lrn:
            student = get_student_by_lrn(detected_lrn)
            if student:
                matched_name = student["full_name"]
                matched_student_id = student["id"]

        results.append({
            "tmp_file": tmp_name,
            "original_filename": photo.filename,
            "detected_lrn": detected_lrn,
            "matched_student_id": matched_student_id,
            "matched_name": matched_name,
            "source": source,
            "error": error,
        })

    return jsonify({"success": True, "results": results})


@app.route("/api/bulk_photo/confirm", methods=["POST"])
def api_bulk_photo_confirm():
    """
    Actually attaches photos to students, based on the admin's confirmed
    (and possibly corrected) matches from the review screen.
    Body: {"confirmed": [{"tmp_file": "...", "lrn": "..."}]}
    Moves each temp photo into the real uploads folder and updates that
    student's photo field. Leftover temp files (unconfirmed) are cleaned up.
    """
    from database import get_student_by_lrn, update_student

    data = request.get_json() or {}
    confirmed = data.get("confirmed", [])

    saved, failed = 0, 0
    for item in confirmed:
        tmp_file = item.get("tmp_file", "")
        lrn = (item.get("lrn") or "").strip()
        tmp_path = os.path.join(BULK_PHOTO_TMP, tmp_file)

        if not os.path.exists(tmp_path) or not lrn:
            failed += 1
            continue

        student = get_student_by_lrn(lrn)
        if not student:
            failed += 1
            continue

        ext = tmp_file.rsplit(".", 1)[-1] if "." in tmp_file else "jpg"
        final_name = f"{int(datetime.now().timestamp() * 1000)}_{lrn}.{ext}"
        final_path = os.path.join(UPLOAD_FOLDER, final_name)
        try:
            with open(tmp_path, "rb") as tmp_f:
                save_resized_photo(tmp_f, final_path)
            os.remove(tmp_path)
            update_student(
                student_id=student["id"],
                rfid_code=student["rfid_code"] or "",
                full_name=student["full_name"],
                section=student["section"] or "",
                parent_name=student["parent_name"] or "",
                parent_phone=student["parent_phone"] or "",
                photo=final_name,
                lrn=student["lrn"] or "",
            )
            saved += 1
        except Exception:
            failed += 1

    # Clean up any leftover temp files not confirmed
    if os.path.isdir(BULK_PHOTO_TMP):
        for f in os.listdir(BULK_PHOTO_TMP):
            try:
                os.remove(os.path.join(BULK_PHOTO_TMP, f))
            except Exception:
                pass

    return jsonify({"success": True, "saved": saved, "failed": failed})


@app.route("/students/status/<int:student_id>", methods=["POST"])
def student_status(student_id):
    """Update a student's status — active, dropped, transferred, other."""
    from database import update_student_status
    status = request.form.get("status", "active")
    reason = request.form.get("reason", "").strip()
    update_student_status(student_id, status, reason)
    section_filter = request.form.get("section_filter", "").strip()
    return redirect(url_for("students", section=section_filter) if section_filter else url_for("students"))


@app.route("/students/export_csv")
def students_export_csv():
    """Export students list as CSV — filtered by section if provided."""
    import csv, io
    from database import export_students_to_list
    section_filter = request.args.get("section", "").strip()
    all_students   = export_students_to_list()
    students = [s for s in all_students if s["section"] == section_filter] if section_filter else all_students
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "full_name","rfid_code","section","parent_name",
        "parent_phone","status","status_reason"
    ])
    writer.writeheader()
    for s in students:
        writer.writerow({
            "full_name":     s.get("full_name",""),
            "rfid_code":     s.get("rfid_code",""),
            "section":       s.get("section",""),
            "parent_name":   s.get("parent_name",""),
            "parent_phone":  s.get("parent_phone",""),
            "status":        s.get("status","active"),
            "status_reason": s.get("status_reason",""),
        })
    output.seek(0)
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=students_backup.csv"}
    )


@app.route("/students/import_merge", methods=["POST"])
def students_import_merge():
    """
    Merge import — adds new students and updates existing ones
    WITHOUT deleting attendance records.
    Safe to use at school PC after updating at home.
    """
    section_filter = request.form.get("section_filter", "").strip()
    extra = {"section": section_filter} if section_filter else {}

    if "file" not in request.files:
        return redirect(url_for("students", **extra))
    file = request.files["file"]
    if not file or not file.filename:
        return redirect(url_for("students", **extra))
    filename  = secure_filename(file.filename)
    temp_path = os.path.join("static", "uploads", f"merge_{filename}")
    file.save(temp_path)
    try:
        added, updated = _do_merge_import(temp_path)
        os.remove(temp_path)
        return redirect(url_for("students", merged=1, added=added, updated=updated, **extra))
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return redirect(url_for("students", error=str(e), **extra))


def _normalize_row(row_dict):
    """Normalize CSV/Excel column names to lowercase with underscores.
    Handles any capitalization or spacing variation from Google Sheets."""
    return {k.strip().lower().replace(" ", "_"): v
            for k, v in row_dict.items() if k and str(k).strip()}


def _fix_lrn(value):
    """Fix LRN values that Google Sheets converted to scientific notation.
    e.g. '1.29509E+11' → '129509000000'
    Also strips decimals from numbers stored as floats e.g. '129509000000.0'
    """
    if not value:
        return ""
    s = str(value).strip()
    try:
        # Parse as float first (handles scientific notation like 1.29509E+11)
        f = float(s)
        # Convert to int to remove decimal point, then to string
        return str(int(f))
    except (ValueError, OverflowError):
        # Not a number — return as-is (already correct format)
        return s


def _do_merge_import(filepath):
    from database import import_students_merge
    import csv
    students_list = []
    if filepath.endswith(".csv"):
        with open(filepath, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                norm = _normalize_row(row)
                # Map Google Sheet columns to internal field names
                name   = str(norm.get("name") or norm.get("full_name") or
                             norm.get("student_name") or norm.get("fullname") or "").strip()
                rfid   = str(norm.get("rfid") or norm.get("rfid_code") or
                             norm.get("rfid_no") or norm.get("card_id") or "").strip().upper()
                sec    = str(norm.get("grade_&_section") or norm.get("section") or
                             norm.get("grade_and_section") or norm.get("grade") or "").strip()
                pname  = str(norm.get("emergency_contact_name") or norm.get("parent_name") or
                             norm.get("guardian") or norm.get("contact_name") or "").strip()
                pphone = str(norm.get("emergency_contact_number") or norm.get("parent_phone") or
                             norm.get("phone") or norm.get("contact_number") or "").strip()
                lrn    = _fix_lrn(norm.get("lrn") or "")
                if not name:
                    continue
                students_list.append({
                    "rfid_code":    rfid,
                    "full_name":    name,
                    "section":      sec,
                    "parent_name":  pname,
                    "parent_phone": pphone,
                    "lrn":          lrn,
                })
    else:
        import openpyxl
        wb   = openpyxl.load_workbook(filepath)
        for sheet in wb.worksheets:
            rows = list(sheet.iter_rows(values_only=True))
            if not rows: continue
            headers = [str(h).strip().lower().replace(" ","_") if h else "" for h in rows[0]]
            for row in rows[1:]:
                if not any(row): continue
                norm = dict(zip(headers, row))
                name   = str(norm.get("name") or norm.get("full_name") or
                             norm.get("student_name") or "").strip()
                rfid   = str(norm.get("rfid") or norm.get("rfid_code") or
                             norm.get("card_id") or "").strip().upper()
                sec    = str(norm.get("grade_&_section") or norm.get("section") or
                             norm.get("grade_and_section") or norm.get("grade") or "").strip()
                pname  = str(norm.get("emergency_contact_name") or norm.get("parent_name") or
                             norm.get("guardian") or "").strip()
                pphone = str(norm.get("emergency_contact_number") or norm.get("parent_phone") or
                             norm.get("phone") or "").strip()
                lrn    = _fix_lrn(norm.get("lrn") or "")
                if not name:
                    continue
                students_list.append({
                    "rfid_code":    rfid,
                    "full_name":    name,
                    "section":      sec,
                    "parent_name":  pname,
                    "parent_phone": pphone,
                    "lrn":          lrn,
                })
    return import_students_merge(students_list)


@app.route("/blast")
def blast():
    """Blast message page — send announcements to all parents."""
    from database import get_recent_blasts, get_all_sections
    school_name   = get_setting("school_name") or "School"
    recent_blasts = get_recent_blasts()
    sections      = get_all_sections()
    return render_template("blast.html",
                           school_name=school_name,
                           recent_blasts=recent_blasts,
                           sections=sections)


@app.route("/api/notification_log")
def api_notification_log():
    """
    Recent scan-triggered notification attempts (not bulk blasts) —
    which channel actually delivered, or fell back to, and why, when
    something didn't go as expected. Powers the notification log panel
    on the Blast page. Supports optional section/grade_level/date range
    filtering via query params.
    """
    from database import get_recent_notification_log
    section     = request.args.get("section") or None
    grade_level = request.args.get("grade_level") or None
    start_date  = request.args.get("start_date") or None
    end_date    = request.args.get("end_date") or None

    logs = get_recent_notification_log(
        limit=200, section=section, grade_level=grade_level,
        start_date=start_date, end_date=end_date
    )
    return jsonify({
        "logs": [
            {
                "full_name":     l["full_name"],
                "section":       l["section"],
                "scan_type":     l["scan_type"],
                "scan_date":     l["scan_date"],
                "scan_time":     l["scan_time"],
                "notify_channel": l["notify_channel"],
                "notify_detail":  l["notify_detail"],
                "notified":       l["notified"],
                "sms_status":     l["sms_status"],
            }
            for l in logs
        ]
    })


@app.route("/blast/send", methods=["POST"])
def blast_send():
    """Start a blast — runs in background thread, returns blast_id."""
    from database import (create_blast, update_blast_progress,
                          get_students_for_blast)
    from notifier import send_messenger, send_sms_sim800c

    message  = request.form.get("message", "").strip()
    section  = request.form.get("section", "all").strip()
    channels = request.form.getlist("channels")  # ["sms","messenger"]

    if not message:
        return redirect(url_for("blast") + "?error=No+message+entered")
    if not channels:
        return redirect(url_for("blast") + "?error=Please+select+at+least+one+channel")

    students = get_students_for_blast(section if section != "all" else None)
    blast_id = create_blast(message, ",".join(channels), section, len(students))

    def run_blast():
        sent = 0; failed = 0
        school = get_setting("school_name") or "School"
        full_msg = f"{message}\n— {school}"

        for student in students:
            from notifier import send_blast_to_parent
            channel = send_blast_to_parent(dict(student), full_msg,
                                            blast_id=blast_id, channels=channels)
            if channel != "none": sent += 1
            else: failed += 1
            # Update progress every 10 students
            if (sent + failed) % 10 == 0:
                update_blast_progress(blast_id, sent, failed, 'running')

        update_blast_progress(blast_id, sent, failed, 'done')

    threading.Thread(target=run_blast, daemon=True).start()
    return redirect(url_for("blast_progress", blast_id=blast_id))


@app.route("/blast/progress/<int:blast_id>")
def blast_progress(blast_id):
    """Show live progress of a running blast."""
    from database import get_blast
    school_name = get_setting("school_name") or "School"
    blast = get_blast(blast_id)
    return render_template("blast_progress.html",
                           blast=blast,
                           school_name=school_name)


@app.route("/api/blast_progress/<int:blast_id>")
def api_blast_progress(blast_id):
    """JSON endpoint polled by the progress page."""
    from database import get_blast
    blast = get_blast(blast_id)
    if not blast:
        return jsonify({"error": "not found"})
    return jsonify({
        "sent":    blast["sent"],
        "failed":  blast["failed"],
        "total":   blast["total"],
        "status":  blast["status"],
        "message": blast["message"],
        "section": blast["section"],
    })


@app.route("/settings/upload_logo", methods=["POST"])
def upload_logo():
    """Upload school logo."""
    if "logo" not in request.files:
        return redirect(url_for("settings"))
    logo = request.files["logo"]
    if logo and logo.filename and allowed_image(logo.filename):
        filename = "school_logo_" + secure_filename(logo.filename)
        # Larger max size than student photos — the logo appears in
        # bigger contexts (page headers, printed ID slips), and it's
        # only ever one file, so storage/bandwidth savings don't matter
        # here the way they do across ~2,000 student photos.
        save_resized_photo(logo, os.path.join(app.config["UPLOAD_FOLDER"], filename), max_dimension=800)
        save_setting("school_logo", filename)
    return redirect(url_for("settings") + "?saved=1")




@app.route("/api/setup_messenger_profile", methods=["POST"])
def api_setup_messenger_profile():
    """Set up Facebook Messenger Get Started button and persistent menu."""
    from notifier import setup_messenger_profile
    school_name = get_setting("school_name") or "School"
    success, message = setup_messenger_profile(school_name)
    return jsonify({"success": success, "message": message})


@app.route("/api/delete_messenger_menu", methods=["POST"])
def api_delete_messenger_menu():
    """Remove the persistent menu (☰) from Messenger."""
    from notifier import delete_messenger_menu
    success, message = delete_messenger_menu()
    return jsonify({"success": success, "message": message})


@app.route("/api/subscribe_page_webhook", methods=["POST"])
def api_subscribe_page_webhook():
    """Subscribe the Facebook Page to receive messages and postback events."""
    import requests as req
    token   = get_setting("messenger_token")
    page_id = get_setting("messenger_page_id") or "61590225764767"
    if not token:
        return jsonify({"success": False, "message": "No Page Access Token set. Please save your token in Settings first."})
    try:
        r = req.post(
            f"https://graph.facebook.com/v18.0/{page_id}/subscribed_apps",
            params={"access_token": token},
            data={"subscribed_fields": "messages,messaging_postbacks,messaging_referrals"},
            timeout=15
        )
        data = r.json()
        if data.get("success"):
            return jsonify({"success": True, "message": "✅ Page webhook subscription updated! Button clicks will now work."})
        error = data.get("error", {}).get("message", str(data))
        return jsonify({"success": False, "message": f"Facebook error: {error}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

# ── MESSENGER WEBHOOK ─────────────────────────────────────────────────────────

def _link_one_lrn(rfid_code, sender_id, school_name):
    """
    Attempts to link one LRN to this Messenger sender. Extracted as its
    own function so a message containing MULTIPLE LRNs (e.g. a parent
    linking two children in one message) can call this once per LRN
    found, collecting each result into one combined reply — rather than
    only ever handling exactly one LRN per message.

    Returns the reply text for this one LRN (not sent here — the
    caller combines replies from all LRNs found in the message into a
    single Messenger message).
    """
    from database import get_connection
    student = get_student_by_lrn(rfid_code)

    if not student:
        print("Unknown RFID from Messenger: " + rfid_code)
        return ("Sorry, we could not find a student with LRN: " + rfid_code + "\n"
                "Please check the LRN on your child's ID card and try again.")

    conn = get_connection()
    existing1 = student["messenger_id"] or ""
    existing2 = (student["messenger_id_2"] if "messenger_id_2" in student.keys() else "") or ""

    if existing1 == sender_id or existing2 == sender_id:
        reply = ("You are already linked to " + student["full_name"] +
                 " (" + (student["section"] or "") + ").\n"
                 "You will receive notifications when your child scans.")
    elif not existing1:
        conn.execute("UPDATE students SET messenger_id=? WHERE lrn=?",
                    (sender_id, rfid_code))
        conn.commit()
        reply = ("You are now linked to " + student["full_name"] +
                 " (" + (student["section"] or "") + ").\n"
                 "You will receive a message every time your child "
                 "arrives at or leaves " + school_name + ".")
        print("Parent 1 linked to " + student["full_name"])
    elif not existing2:
        conn.execute("UPDATE students SET messenger_id_2=? WHERE lrn=?",
                    (sender_id, rfid_code))
        conn.commit()
        reply = ("You are now linked to " + student["full_name"] +
                 " (" + (student["section"] or "") + ").\n"
                 "You will receive a message every time your child "
                 "arrives at or leaves " + school_name + ".")
        print("Parent 2 linked to " + student["full_name"])
    else:
        reply = (student["full_name"] + " already has 2 parents linked.\n"
                 "Please contact the school to update this.")
    conn.close()
    return reply


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """
    Handles Facebook webhook verification (GET) and message events (POST).
    GET  — Facebook verifies the webhook URL
    POST — Facebook sends message events when parents message the page
    """
    if request.method == "GET":
        verify_token = request.args.get("hub.verify_token")
        challenge    = request.args.get("hub.challenge")
        mode         = request.args.get("hub.mode")
        our_token    = get_setting("webhook_verify_token") or "rfid_school_verify"
        if mode == "subscribe" and verify_token == our_token:
            print("✅ Webhook verified by Facebook!")
            return challenge, 200
        return "Verification failed", 403

    # POST — receive messages from parents
    from database import (get_student_by_rfid, get_student_by_lrn,
                          update_student_messenger_id, get_connection)
    from notifier import send_messenger, send_messenger_buttons

    data = request.get_json()
    if not data or data.get("object") != "page":
        return "ok", 200

    school_name = get_setting("school_name") or "School"

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")
            message   = event.get("message", {})
            postback  = event.get("postback", {})

            if not sender_id:
                continue

            # ── Quick reply / text command detection ──────────────────────────────
            # Quick reply buttons send a MESSAGES event (not postback),
            # so they work with the standard messages subscription.
            # Check quick_reply payload first, then fall back to raw text.
            qr_payload = ""
            if message:
                qr = message.get("quick_reply", {})
                if qr:
                    qr_payload = qr.get("payload", "").upper()

            # Also handle postback payload (works if messaging_postbacks is enabled)
            postback_payload = postback.get("payload", "").upper() if postback else ""

            # Combine: quick reply OR postback OR text command
            cmd = qr_payload or postback_payload

            # Get text message (for RFID lookup or text commands)
            raw_text = message.get("text", "").strip() if message else ""
            referral = event.get("referral", {})
            if referral and not raw_text:
                raw_text = referral.get("ref", "").strip()

            # Strip ALL non-ASCII characters (emojis, symbols, etc.)
            # so ANY saved response with emoticons matches commands.
            # e.g. "🔒 Unlink my account" → "UNLINKMYACCOUNT" ✅
            text     = "".join(c for c in raw_text if ord(c) < 128).strip()
            text_cmd = text.upper().replace(" ", "").replace("-", "").replace("_", "")

            # Detect UNLINK [LRN] format (e.g. "Unlink 100012345678")
            unlink_lrn = None
            _ul_match = re.match(r"^UNLINK[\s\-:]*([0-9]{6,12})$", text_cmd)
            if _ul_match:
                unlink_lrn = _ul_match.group(1)
                cmd = "UNLINK_SPECIFIC"

            # If text looks like a button command, treat it as cmd
            if not cmd and text_cmd in ("UNLINK", "UNLINKMYACCOUNT", "LINKMYCHILD", "HELP"):
                cmd = text_cmd

            # Handle GET_STARTED (first time opening chat)
            if cmd in ("GET_STARTED",) or postback_payload == "GET_STARTED":
                send_messenger_buttons(sender_id,
                    "Welcome to " + school_name + " Attendance Notifications!\n\n"
                    "Tap a button below to get started:",
                    [
                        {"title": "Link my child", "payload": "LINK_CHILD"},
                        {"title": "Unlink",        "payload": "UNLINK"},
                        {"title": "Help",          "payload": "HELP"},
                    ])
                continue

            # Handle LINK_CHILD command
            if cmd in ("LINK_CHILD", "LINKMYCHILD"):
                send_messenger(sender_id,
                    "To link your account, please send your child's LRN.\n\n"
                    "You can find the LRN (12-digit number) printed on their ID card.\n\n"
                    "Example: 100012345678\n\nJust type the LRN and send it!")
                continue

            # Handle UNLINK command (may be followed by LRN for multi-child)
            if cmd in ("UNLINK", "UNLINKMYACCOUNT", "UNLINK_SPECIFIC"):
                conn = get_connection()
                # Find ALL students linked to this parent
                linked = conn.execute(
                    "SELECT full_name, lrn, messenger_id, messenger_id_2 FROM students "
                    "WHERE messenger_id=? OR messenger_id_2=?",
                    (sender_id, sender_id)
                ).fetchall()

                if not linked:
                    send_messenger_buttons(sender_id,
                        "You are not currently linked to any student.\n"
                        "Send your child\'s LRN to link your account.",
                        [
                            {"title": "Link my child", "payload": "LINK_CHILD"},
                            {"title": "Help",          "payload": "HELP"},
                        ])

                elif len(linked) == 1:
                    # Only one child — unlink directly, no LRN needed
                    s = linked[0]
                    if s["messenger_id"] == sender_id:
                        conn.execute("UPDATE students SET messenger_id='' WHERE messenger_id=?", (sender_id,))
                    else:
                        conn.execute("UPDATE students SET messenger_id_2='' WHERE messenger_id_2=?", (sender_id,))
                    conn.commit()
                    send_messenger(sender_id,
                        "✅ You have been unlinked from " + s["full_name"] + ".\n"
                        "You will no longer receive attendance notifications.\n\n"
                        "To re-link, just send your child\'s LRN anytime.")

                elif cmd == "UNLINK_SPECIFIC" and unlink_lrn:
                    # Parent specified which child to unlink by LRN
                    target = conn.execute(
                        "SELECT full_name, messenger_id, messenger_id_2 FROM students "
                        "WHERE lrn=? AND (messenger_id=? OR messenger_id_2=?)",
                        (unlink_lrn, sender_id, sender_id)
                    ).fetchone()
                    if target:
                        if target["messenger_id"] == sender_id:
                            conn.execute("UPDATE students SET messenger_id='' WHERE lrn=?", (unlink_lrn,))
                        else:
                            conn.execute("UPDATE students SET messenger_id_2='' WHERE lrn=?", (unlink_lrn,))
                        conn.commit()
                        send_messenger(sender_id,
                            "✅ You have been unlinked from " + target["full_name"] + ".\n"
                            "You will no longer receive notifications for this child.\n\n"
                            "To re-link, send their LRN anytime.")
                    else:
                        # Build name list for retry
                        names = "\n".join(
                            "• " + s["full_name"] + " (LRN: " + (s["lrn"] or "N/A") + ")"
                            for s in linked
                        )
                        send_messenger(sender_id,
                            "LRN not found among your linked children.\n\n"
                            "Your linked children:\n" + names + "\n\n"
                            "Send: UNLINK [LRN]\nExample: UNLINK 100012345678")

                else:
                    # Multiple children — ask which one to unlink
                    names = "\n".join(
                        "• " + s["full_name"] + " (LRN: " + (s["lrn"] or "N/A") + ")"
                        for s in linked
                    )
                    send_messenger(sender_id,
                        "You have " + str(len(linked)) + " children linked:\n\n" +
                        names + "\n\n"
                        "Which child do you want to unlink?\n"
                        "Reply with: UNLINK [LRN]\n"
                        "Example: UNLINK 100012345678")

                conn.close()
                continue

            # Handle HELP command
            if cmd == "HELP":
                send_messenger_buttons(sender_id,
                    school_name + " Help\n\n"
                    "• To LINK: Send your child's LRN (12-digit number on their ID card)\n"
                    "• To UNLINK: Tap the Unlink button below\n\n"
                    "For other concerns, please visit the school office.",
                    [
                        {"title": "Link my child", "payload": "LINK_CHILD"},
                        {"title": "Unlink",        "payload": "UNLINK"},
                    ])
                continue

            # Handle "Got it" quick-reply tap (from attendance notifications
            # and the weekend keep-alive ping). Just acknowledges — the tap
            # itself is what matters, since it reopens the messaging window.
            if cmd == "ACK_NOTIFICATION":
                continue

            # No command matched — check if the message contains one or
            # more LRNs. Parents often type things like "LRN: 129509260198"
            # or send two children's LRNs in one message — the old check
            # required the ENTIRE message to be nothing but digits, so
            # either of those completely failed to match at all. This
            # strips common "LRN" labels first, then finds every valid
            # LRN-length number anywhere in the message, however many
            # there are and whatever's between them (spaces, commas,
            # "and", newlines, etc.).
            cleaned = re.sub(r'LRN\s*[:#\-\.]?\s*(?:NO\.?)?\s*[:#\-\.]?\s*', '', text, flags=re.IGNORECASE)
            found_lrns = re.findall(r'\b[0-9]{6,12}\b', cleaned)

            if not found_lrns:
                # Not an RFID code — show quick reply buttons
                send_messenger_buttons(sender_id,
                    "Hi! What would you like to do?",
                    [
                        {"title": "Link my child", "payload": "LINK_CHILD"},
                        {"title": "Unlink",        "payload": "UNLINK"},
                        {"title": "Help",          "payload": "HELP"},
                    ])
                continue

            # One or more LRNs found — link each one, then send back a
            # single combined reply covering all of them together,
            # rather than a separate message per child.
            replies = [_link_one_lrn(rfid_code, sender_id, school_name)
                       for rfid_code in found_lrns]
            send_messenger(sender_id, "\n\n".join(replies))


@app.route("/webhook_guide")
def webhook_guide():
    school_name = get_setting("school_name") or "School"
    verify_token = get_setting("webhook_verify_token") or "rfid_school_verify"
    return render_template("webhook_guide.html",
                           school_name=school_name,
                           verify_token=verify_token)


# ── TRACKER ───────────────────────────────────────────────────────────────────


# ── BACKGROUND SERVICES (scheduler + SMS worker) ────────────────────────────
# Deliberately OUTSIDE the `if __name__ == "__main__":` guard below.
# This app can be launched two different ways:
#   1. Directly:      python app.py                 → __name__ == "__main__"
#   2. Via launcher:  python -c "from app import app; app.run(...)"
#                                                     → __name__ == "app"
# Code inside `if __name__ == "__main__":` ONLY runs under method #1 — under
# method #2 (which this project's start_server.bat actually uses), that
# block is silently skipped entirely, so anything needed for the app to
# function correctly (like these background services) must NOT depend on
# it. A shared, flag-guarded starter function is called from both places
# below, so exactly one of them will actually trigger it, whichever launch
# method is used — and if somehow both did, the flag prevents starting
# everything twice.
_background_services_started = False

def _start_background_services_once():
    global _background_services_started
    if _background_services_started:
        return
    _background_services_started = True
    from notifier import start_inactivity_scheduler, start_sms_queue_worker

    # Weekend Messenger keep-alive: pings linked parents Sat & Sun so their
    # 24-hour messaging window doesn't fully close before Monday's first scan.
    # Messenger keep-alive: if no scans happen for a while (weekend, holiday,
    # unplanned class suspension), linked parents' 24-hour messaging windows
    # would otherwise close before the next real scan. This checks actual
    # scan activity rather than assuming Sat/Sun are the only quiet days.
    # Temporary workaround until pages_utility_messaging is approved.
    start_inactivity_scheduler(inactivity_hours=20, recheck_minutes=60)

    # SMS queue worker: this PC's own SIM800C(s) (if attached) also handle
    # their share of queued SMS jobs, same as remote PCs running
    # sms_worker.py — splits volume across multiple SIM cards.
    start_sms_queue_worker(poll_seconds=5)


# Flask's debug-mode auto-reloader re-imports this whole module in a child
# subprocess, with the environment variable WERKZEUG_RUN_MAIN set to "true"
# ONLY in that child (never in the initial parent/monitor process). Starting
# only when that's "true" means: under the reloader, we start exactly once,
# in the process that's actually serving requests — not the parent monitor,
# which never serves anything and would just waste a duplicate set of
# background threads if we started them there too.
#
# On Railway (or any Gunicorn-based production deploy), there IS no Flask
# reloader at all — Gunicorn just imports this module once per worker
# process. RAILWAY_ENVIRONMENT is a variable Railway itself always sets,
# so checking for it (or WERKZEUG_RUN_MAIN, for the local dev case) covers
# both deployment styles correctly. Running Gunicorn with --workers 1 (see
# the Procfile) is what guarantees this only happens once in production —
# with more than one worker, each would start its own duplicate copy of
# these background threads.
_is_railway = os.environ.get("RAILWAY_ENVIRONMENT") is not None
if _is_railway or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    _start_background_services_once()


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
# Only reached when running "python app.py" directly (not via
# start_server.bat's "python -c \"from app import app; app.run(...)\""
# command, which skips this block entirely — see note above). Also calls
# the same shared starter function, so a plain direct run (no reloader
# involved at all, WERKZEUG_RUN_MAIN never set) still starts the background
# services correctly.

if __name__ == "__main__":
    setup_database()
    local_ip = get_local_ip()
    _start_background_services_once()

    print("\n" + "=" * 55)
    print("  RFID Attendance System — Web Server")
    print("=" * 55)
    print(f"\n  Open on THIS PC:        http://localhost:5000")
    print(f"  Open on OTHER PCs:      http://{local_ip}:5000")
    print(f"  Admin dashboard:        http://{local_ip}:5000/admin")
    print(f"  Student registry:       http://{local_ip}:5000/students")
    print(f"\n  Keep this window open while the system is running.")
    print(f"  Press Ctrl+C to stop the server.\n")
    print("=" * 55 + "\n")

    app.run(
        host  = "0.0.0.0",  # allow connections from other PCs on the network
        port  = 5000,
        debug = False
    )


