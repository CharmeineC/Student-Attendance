"""
notifier.py
-----------
Smart notification system:
1. Messenger (parent 1 + parent 2 simultaneously)
2. SMS ONLY if no Messenger linked
"""

import requests
import time
import threading
from database import get_setting, save_setting, mark_notified


def build_message(student_name, scan_type, scan_time, school_name):
    time_formatted = _format_time(scan_time)
    action = "has ARRIVED at school" if scan_type == "IN" else "has LEFT school"
    return f"{student_name} {action} at {time_formatted}. - {school_name}"


def send_notification(student, log_id, scan_type, scan_time):
    """
    Smart notification — sends to all linked channels simultaneously.
    SMS only fires if NO Messenger is linked.
    """
    school_name   = get_setting("school_name") or "School"
    message       = build_message(student["full_name"], scan_type, scan_time, school_name)
    use_messenger = get_setting("use_messenger") == "1"
    use_sms       = get_setting("use_sms")       == "1"

    sent_digital = False
    channels_used = []

    # ── Messenger — parent 1 and 2 simultaneously ─────────────────────────
    if use_messenger:
        threads = []
        results = []

        def send_msg(mid, label):
            if send_messenger(mid, message):
                results.append(label)

        if student.get("messenger_id"):
            t = threading.Thread(target=send_msg, args=(student["messenger_id"], "messenger"))
            threads.append(t)
        if student.get("messenger_id_2"):
            t = threading.Thread(target=send_msg, args=(student["messenger_id_2"], "messenger_2"))
            threads.append(t)

        for t in threads: t.start()
        for t in threads: t.join()

        if results:
            sent_digital = True
            channels_used.extend(results)
            print(f"  💬 Messenger sent ({len(results)} parent/s) for {student['full_name']}")
        elif not threads:
            print(f"  ⚠️  No Messenger ID linked for {student['full_name']} (messenger_id and messenger_id_2 both empty in database)")
        else:
            print(f"  ⚠️  Messenger send failed for {student['full_name']} (see error above)")

    # ── SMS — ONLY if no digital channel worked ───────────────────────────
    # Queued rather than sent directly, so volume can be picked up and split
    # across multiple PCs' SIM800C modules instead of one SIM handling
    # everything (which is what triggers telco fair-use throttling).
    if use_sms and not sent_digital and student.get("parent_phone"):
        if queue_sms(student["parent_phone"], message):
            channels_used.append("sms_queued")
            print(f"  ✉️  SMS queued for parent of {student['full_name']}")

    channel = ",".join(channels_used) if channels_used else "none"
    if channels_used:
        mark_notified(log_id, channel)
    else:
        print(f"  ⚠️  No channel available for {student['full_name']}")

    return channel


def send_blast_to_parent(student, message, blast_id=None, channels=None):
    """
    Smart blast — same priority logic as attendance notifications.
    Returns channel used ("none" if every attempt failed).

    blast_id: optional. If provided, logs this specific parent's outcome
        (channel used, success/fail) to the blast_recipients table, so
        "who actually got the message" can be checked later — rather than
        only ever seeing aggregate sent/failed counts for the whole blast.

    channels: optional list restricting which channels are attempted at
        all, e.g. ["messenger"] to test Messenger only. None (default)
        means all available channels are tried — used by
        send_holiday_announcement(), which always wants every channel.
        Without this, a blast filtered to "Messenger only" in the UI
        would silently still fall through to SMS for every recipient
        without Messenger linked, which is slow.
    """
    allow_messenger = channels is None or "messenger" in channels
    allow_sms = channels is None or "sms" in channels

    sent_digital = False
    channels_used = []

    # Messenger
    if allow_messenger:
        threads = []
        results = []
        def send_msg(mid, label):
            if send_messenger(mid, message):
                results.append(label)

        if student.get("messenger_id"):
            t = threading.Thread(target=send_msg, args=(student["messenger_id"], "messenger"))
            threads.append(t)
        if student.get("messenger_id_2"):
            t = threading.Thread(target=send_msg, args=(student["messenger_id_2"], "messenger_2"))
            threads.append(t)
        for t in threads: t.start()
        for t in threads: t.join()
        if results:
            sent_digital = True
            channels_used.extend(results)

    # SMS only if no digital AND sms is an allowed channel — queued so
    # volume can be split across multiple PCs' SIM800C modules.
    if allow_sms and not sent_digital and student.get("parent_phone"):
        if queue_sms(student["parent_phone"], message):
            channels_used.append("sms_queued")

    result_channel = ",".join(channels_used) if channels_used else "none"

    if blast_id is not None:
        try:
            from database import log_blast_recipient
            log_blast_recipient(
                blast_id=blast_id,
                student_id=student.get("id"),
                student_name=student.get("full_name") or "Unknown",
                section=student.get("section") or "",
                channel=result_channel,
                success=(result_channel != "none"),
            )
        except Exception as e:
            print(f"  ⚠️ Could not log blast recipient: {e}")

    return result_channel


# ── FACEBOOK MESSENGER ────────────────────────────────────────────────────────

def send_messenger(recipient_id, message_text, with_quick_reply=True):
    """Send a Messenger text message.
    with_quick_reply=True attaches a 'Got it' button. Tapping it sends a
    reply back to the bot, which reopens the recipient's 24-hour standard
    messaging window for the next notification. This is a workaround while
    pages_utility_messaging is pending App Review — it is not a guarantee,
    since it depends on the parent actually tapping the button.
    """
    token = get_setting("messenger_token")
    if not token:
        print("  ❌ Messenger: No Page Access Token configured in Settings.")
        return False
    try:
        message_obj = {"text": message_text}
        if with_quick_reply:
            message_obj["quick_replies"] = [
                {"content_type": "text", "title": "👍 Got it", "payload": "ACK_NOTIFICATION"}
            ]
        r = requests.post(
            "https://graph.facebook.com/v18.0/me/messages",
            json={"recipient": {"id": recipient_id}, "message": message_obj},
            params={"access_token": token}, timeout=10
        )
        if r.status_code == 200:
            return True
        else:
            print(f"  ❌ Messenger failed (HTTP {r.status_code}): {r.text[:300]}")
            return False
    except Exception as e:
        print(f"  ❌ Messenger error: {e}")
        return False


def send_inactivity_keepalive(is_final_ping=False):
    """
    Sends a keep-alive ping (with quick-reply button) to every linked
    parent. Triggered when there's been no scan activity for a while,
    regardless of what day it is — covers weekends and short unplanned
    gaps alike, not just Sat/Sun.

    is_final_ping=True changes the wording to acknowledge the gap may be
    longer than a normal weekend, since this is the last automatic ping
    before the scheduler goes quiet for this gap (see MAX_PINGS_PER_GAP).
    """
    from database import get_all_students
    school_name = get_setting("school_name") or "School"
    if is_final_ping:
        text = (
            f"👋 Hi! This is {school_name}'s attendance system.\n\n"
            f"There's been no school activity for a while, so you may not "
            f"receive attendance updates for some time. Tap the button "
            f"below to stay linked — you'll get updates again as soon as "
            f"scanning resumes."
        )
    else:
        text = (
            f"👋 Hi! This is {school_name}'s attendance system checking in.\n\n"
            f"Tap the button below so you keep receiving your child's attendance "
            f"updates without interruption. No action needed otherwise."
        )
    students = get_all_students()
    sent, failed = 0, 0
    seen_ids = set()
    for student in students:
        student = dict(student)  # sqlite3.Row has no .get(); convert like app.py does
        for field in ("messenger_id", "messenger_id_2"):
            mid = student.get(field)
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                if send_messenger(mid, text, with_quick_reply=True):
                    sent += 1
                else:
                    failed += 1
                time.sleep(0.3)  # gentle pacing, avoid rate limits
    print(f"  📅 Inactivity keep-alive: {sent} sent, {failed} failed, "
          f"{len(seen_ids)} unique parents.")
    return sent, failed


def _is_holiday_mode_active():
    """
    Shared check used by both the background scheduler and the API routes,
    so end-date auto-expiry logic lives in exactly one place. If a
    "holiday_mode_until" date has passed, this clears the setting and
    returns False — so the scheduler resumes on its own even if nobody
    opens the Settings page to trigger the expiry check there.
    """
    import datetime as _dt
    active = (get_setting("holiday_mode") or "0") == "1"
    if not active:
        return False
    until = get_setting("holiday_mode_until")
    if until:
        try:
            until_dt = _dt.datetime.strptime(until, "%Y-%m-%d")
            if _dt.datetime.now() > until_dt + _dt.timedelta(days=1):
                save_setting("holiday_mode", "0")
                save_setting("holiday_mode_until", "")
                return False
        except ValueError:
            pass  # malformed date, don't crash — just treat as still active
    return True


def send_holiday_announcement(custom_message=None, until_date=None):
    """
    Manually triggered (e.g. from the Settings page 'Holiday Mode' button),
    NOT by the automatic scheduler. Sends one clear, explicit message to
    EVERY parent across all available channels (Messenger, with SMS as
    fallback) using the same priority logic as the Blast feature — so one
    Holiday Mode toggle covers what would otherwise be a separate manual
    blast, and shows up properly in Blast History with a real date.

    until_date: optional "YYYY-MM-DD" string, included in the message if
        provided, so parents know roughly when to expect updates again.
    """
    from database import get_all_students, create_blast, update_blast_progress

    school_name = get_setting("school_name") or "School"
    if custom_message:
        text = custom_message
    elif until_date:
        text = (
            f"📢 {school_name} attendance system: classes are on break "
            f"until {until_date}. You will not receive attendance updates "
            f"until then. Tap below to stay linked in the meantime (Messenger only)."
        )
    else:
        text = (
            f"📢 {school_name} attendance system: classes are on break. "
            f"You will not receive attendance updates until classes resume. "
            f"Tap below to stay linked in the meantime (Messenger only)."
        )

    students = get_all_students()

    blast_id = create_blast(
        message=text,
        channels="messenger,sms",
        section="All (Holiday Mode)",
        total=len(students),
        blast_type="holiday",
    )

    sent, failed = 0, 0
    for student in students:
        channel = send_blast_to_parent(dict(student), text, blast_id=blast_id)
        if channel != "none":
            sent += 1
        else:
            failed += 1

    update_blast_progress(blast_id, sent, failed, status="done")
    print(f"  📢 Holiday announcement (blast #{blast_id}): {sent} parents reached, "
          f"{failed} unreachable.")
    return sent, failed


MAX_PINGS_PER_GAP = 3  # after this many automatic pings in one silence
                        # period, stop nagging — use Holiday Mode instead
                        # for known long breaks.


def start_inactivity_scheduler(inactivity_hours=20, recheck_minutes=60,
                                sunday_ping_hour=18):
    """
    Lightweight background scheduler (no extra dependencies).
    Splits behaviour into two cases rather than treating every silence
    gap the same:

    WEEKEND (Saturday/Sunday): a normal, expected weekly gap. Sends just
    ONE ping, on Sunday evening (default 6 PM), to reopen parents'
    24-hour window right before Monday's first scan — no Saturday
    message at all, since a whole weekend of silence is routine, not
    something parents need reassurance about.

    WEEKDAY (Mon-Fri): an unplanned gap during a normal school day is a
    genuine anomaly (bad weather, unexpected suspension, etc.), so this
    uses the more responsive repeat-ping logic: pings after
    `inactivity_hours` hours of silence, repeating roughly every
    `inactivity_hours` again, up to MAX_PINGS_PER_GAP times.

    Skips entirely while Holiday Mode is active (setting "holiday_mode"),
    since that's already handled by an explicit manual announcement —
    use send_holiday_announcement() for known long breaks.

    Call this once at app startup (e.g. in app.py's __main__ block).
    """
    import datetime as _dt
    from database import get_last_scan_overall

    def _loop():
        last_scan_seen = None      # last-scan-time as of our previous check
        last_pinged_at = None      # when we last sent a WEEKDAY keep-alive
        ping_count = 0              # weekday pings sent so far in this gap
        last_sunday_ping_date = None  # date() we last sent the Sunday ping

        while True:
            try:
                if _is_holiday_mode_active():
                    time.sleep(recheck_minutes * 60)
                    continue

                last_scan = get_last_scan_overall()
                now = _dt.datetime.now()

                # A new scan happened since we last checked — activity
                # resumed, so forget any prior weekday pings; a fresh gap
                # starts from here. (Sunday-ping tracking is separate,
                # date-based, and doesn't need resetting here.)
                if last_scan != last_scan_seen:
                    last_pinged_at = None
                    ping_count = 0
                    last_scan_seen = last_scan

                if last_scan is not None:
                    is_weekend_today = now.weekday() in (5, 6)  # Sat, Sun

                    if is_weekend_today:
                        # Lightweight path: single Sunday-evening ping only.
                        is_sunday = now.weekday() == 6
                        already_sent_today = (last_sunday_ping_date == now.date())
                        if (is_sunday and now.hour == sunday_ping_hour
                                and not already_sent_today):
                            print(f"  📅 Sunday evening — sending single "
                                  f"weekend keep-alive...")
                            send_inactivity_keepalive(is_final_ping=False)
                            last_sunday_ping_date = now.date()
                        # No Saturday ping, no repeated Sunday pings.

                    else:
                        # Weekday path: unexpected gap, respond more actively.
                        if ping_count < MAX_PINGS_PER_GAP:
                            hours_since_scan = (
                                (now - last_scan).total_seconds() / 3600
                            )
                            hours_since_ping = (
                                (now - last_pinged_at).total_seconds() / 3600
                                if last_pinged_at else None
                            )
                            should_ping = (
                                hours_since_scan >= inactivity_hours
                                and (last_pinged_at is None
                                     or hours_since_ping >= inactivity_hours)
                            )
                            if should_ping:
                                ping_count += 1
                                is_final = ping_count >= MAX_PINGS_PER_GAP
                                print(f"  📅 Weekday: no scans for "
                                      f"{hours_since_scan:.1f}h (last: "
                                      f"{last_scan}) — sending keep-alive "
                                      f"({ping_count}/{MAX_PINGS_PER_GAP})...")
                                send_inactivity_keepalive(is_final_ping=is_final)
                                last_pinged_at = now
            except Exception as e:
                print(f"  ❌ Inactivity keep-alive check error: {e}")
            time.sleep(recheck_minutes * 60)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print(f"  ✅ Inactivity-based Messenger keep-alive scheduler started "
          f"(weekday pings after {inactivity_hours}h idle, max "
          f"{MAX_PINGS_PER_GAP}/gap; single Sunday {sunday_ping_hour}:00 "
          f"ping for weekends; checks every {recheck_minutes}min).")


def setup_messenger_profile(school_name="School", contact_info=""):
    """
    Set up the Facebook Messenger Get Started button, greeting,
    and persistent menu. Call this once after setting the token.
    """
    token = get_setting("messenger_token")
    if not token:
        return False, "No Messenger token configured"

    contact_text = f"\n\nFor help contact: {contact_info}" if contact_info else ""

    payload = {
        "get_started": {"payload": "GET_STARTED"},
        "greeting": [
            {
                "locale": "default",
                "text": f"Welcome to {school_name} Attendance Notifications! 👋\n\nTap a button below to get started."
            }
        ],
        "persistent_menu": [
            {
                "locale": "default",
                "composer_input_disabled": False,
                "call_to_actions": [
                    {
                        "type": "postback",
                        "title": "Link my child",
                        "payload": "LINK_CHILD"
                    },
                    {
                        "type": "postback",
                        "title": "Unlink",
                        "payload": "UNLINK"
                    },
                    {
                        "type": "postback",
                        "title": "Help",
                        "payload": "HELP"
                    }
                ]
            }
        ]
    }

    try:
        r = requests.post(
            "https://graph.facebook.com/v18.0/me/messenger_profile",
            json=payload,
            params={"access_token": token},
            timeout=15
        )
        if r.status_code == 200:
            return True, "Messenger profile set up successfully!"
        return False, f"Error: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def send_messenger_buttons(recipient_id, text, buttons):
    """Send a message with quick reply buttons."""
    token = get_setting("messenger_token")
    if not token:
        return False
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "text": text,
            "quick_replies": [
                {"content_type": "text", "title": b["title"], "payload": b["payload"]}
                for b in buttons
            ]
        }
    }
    try:
        r = requests.post(
            "https://graph.facebook.com/v18.0/me/messages",
            json=payload,
            params={"access_token": token},
            timeout=10
        )
        return r.status_code == 200
    except Exception:
        return False


def delete_messenger_menu():
    """Remove the persistent menu (☰) from Messenger completely."""
    token = get_setting("messenger_token")
    if not token:
        return False, "No Messenger token configured"
    try:
        r = requests.delete(
            "https://graph.facebook.com/v18.0/me/messenger_profile",
            json={"fields": ["persistent_menu"]},
            params={"access_token": token},
            timeout=15
        )
        if r.status_code == 200:
            return True, "✅ Messenger menu removed! The ☰ button will no longer appear."
        return False, f"Error: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


# ── SIM800C ───────────────────────────────────────────────────────────────────

def queue_sms(phone_number, message):
    """
    Adds an SMS to the shared queue instead of sending it directly from
    this machine. Any PC running an SMS worker (the host's own background
    thread, or a remote PC's sms_worker.py) — each with its own SIM800C —
    can pick this job up. This spreads outbound SMS volume across multiple
    SIM cards, which is what actually avoids telco fair-use throttling on
    a single SIM.

    Returns True if the job was queued (essentially always, barring a
    database error) — this does NOT mean the SMS was delivered yet, only
    that it's waiting for a worker to send it. Check sms_queue (or
    get_sms_queue_stats()) for real delivery status.
    """
    from database import queue_sms_job
    try:
        queue_sms_job(phone_number, message)
        return True
    except Exception as e:
        print(f"  ❌ Could not queue SMS: {e}")
        return False


def start_sms_queue_worker(poll_seconds=5):
    """
    Runs ON THE HOST, in the background — one independent worker thread
    per SIM800C module actually detected on this PC, exactly like a
    remote PC's sms_worker.py, but calling the database functions
    directly instead of going over HTTP, since it's already on the same
    machine.

    If 3 SIM800C modules are plugged into the host, this spins up 3
    separate workers (host-COM4, host-COM5, host-COM6, for example),
    each pulling from the same shared queue and sending through its OWN
    pinned port — splitting load across all of them, the same way
    separate PCs would, without needing separate physical computers.

    If no SIM800C is found at startup, prints a warning but doesn't
    crash — the host still runs fine without SMS if none is attached.

    Call this once at app startup (e.g. in app.py's __main__ block).
    """
    from database import claim_next_sms_job, mark_sms_job_complete

    ports = find_all_sim800c_ports()
    if not ports:
        print("  ⚠️  No SIM800C detected on this PC at startup — host will "
              "not send SMS itself (remote PC workers, if any, still can).")
        return

    def _loop(worker_id, pinned_port):
        while True:
            try:
                job = claim_next_sms_job(worker_id)
                if job:
                    success = send_sms_sim800c(
                        job["phone_number"], job["message"], port=pinned_port
                    )
                    mark_sms_job_complete(
                        job["id"], success,
                        error=None if success else "send failed (see server log)"
                    )
                else:
                    time.sleep(poll_seconds)
            except Exception as e:
                print(f"  ❌ SMS queue worker ({worker_id}) error: {e}")
                time.sleep(poll_seconds)

    for p in ports:
        worker_id = f"host-{p}"
        t = threading.Thread(target=_loop, args=(worker_id, p), daemon=True)
        t.start()

    port_list = ", ".join(ports)
    print(f"  ✅ SMS queue workers started: {len(ports)} SIM800C device(s) "
          f"found ({port_list}). Each will handle its own share of "
          f"queued SMS, polling every {poll_seconds}s.")


def find_all_sim800c_ports():
    """
    Tests either a specific list of candidate ports (if configured via
    the "sim800c_candidate_ports" setting, e.g. "COM8,COM9,COM10"), or
    every serial port on the system if none is set. Returns ALL ports
    that actually respond to a SIM800C AT command — not just the first
    one found — so each can get its own dedicated worker.

    Restricting to known candidate ports is faster and safer once you
    know which COM numbers your SIM800C devices actually use, since it
    skips testing unrelated serial devices (Bluetooth virtual ports,
    etc.) entirely.

    Prints a result for EVERY port tested (pass or fail), not just
    successes — a port that fails silently (no exception, just no "OK"
    response) previously left no trace in the log at all.
    """
    found = []
    try:
        candidates_setting = (get_setting("sim800c_candidate_ports") or "").strip()

        if candidates_setting:
            port_names = [p.strip() for p in candidates_setting.split(",") if p.strip()]
            print(f"  🔍 Testing {len(port_names)} configured port(s): "
                  f"{', '.join(port_names)}...")
            for port_name in port_names:
                ok = _test_sim800c(port_name)
                status = "✅ responded" if ok else "❌ no response"
                print(f"     {port_name}: {status}")
                if ok:
                    found.append(port_name)
        else:
            import serial.tools.list_ports
            all_ports = list(serial.tools.list_ports.comports())
            print(f"  🔍 Scanning {len(all_ports)} serial port(s) for SIM800C devices... "
                  f"(tip: set 'sim800c_candidate_ports' in Settings to test only "
                  f"specific ports and skip this full scan)")
            for port in all_ports:
                ok = _test_sim800c(port.device)
                status = "✅ responded" if ok else "❌ no response"
                print(f"     {port.device} ({port.description}): {status}")
                if ok:
                    found.append(port.device)
    except Exception as e:
        print(f"  ❌ Error scanning for SIM800C devices: {e}")
    return found


def find_sim800c_port():
    """
    Uses the saved port from Settings if it's still actually valid —
    verified with a real AT command test, not just trusted blindly —
    since a saved port can go stale (e.g. after a driver reinstall or
    replugging into a different USB port changes the COM number).
    Falls back to scanning all ports if the saved one no longer responds.
    """
    saved = get_setting("sim800c_port")
    if saved and _test_sim800c(saved):
        return saved
    try:
        import serial.tools.list_ports
        for port in serial.tools.list_ports.comports():
            if _test_sim800c(port.device):
                return port.device
    except Exception:
        pass
    return None


def _test_sim800c(port, attempts=3, settle_wait=2.0, response_wait=2.0):
    """
    Tests whether a port responds to a basic AT command.
    Multiple attempts with generous waits, since several SIM800C dongles
    all booting at once (e.g. at server startup with 3 plugged in) can
    genuinely need more time to respond than a single device tested
    alone — a device that just needs a moment longer would otherwise
    look identical to one that's truly not there.
    """
    try:
        import serial
        with serial.Serial(port, baudrate=9600, timeout=3) as ser:
            time.sleep(settle_wait)  # Let the port settle after opening
            for attempt in range(1, attempts + 1):
                ser.flushInput()
                ser.write(b"AT\r\n")
                time.sleep(response_wait)
                resp = ser.read(ser.in_waiting or 20).decode("utf-8", errors="ignore")
                if "OK" in resp:
                    return True
            return False
    except Exception as e:
        print(f"  (SIM800C test on {port} failed: {e})")
        return False


def send_sms_sim800c(phone_number, message_text, port=None):
    """Send SMS via SIM800C plugged into USB port.
    Handles Philippine numbers (09xx → +639xx automatically).
    Uses GSM-7 text mode for maximum compatibility.

    port: optional. If given, sends via THIS specific port without
        re-detecting — used when multiple SIM800C modules are attached
        to the same PC, so each worker thread stays pinned to its own
        device instead of all of them racing to auto-detect and
        potentially colliding on the same port. If omitted, falls back
        to the original single-device auto-detect behavior.
    """
    if not port:
        port = find_sim800c_port()
    if not port:
        print("  ❌ SIM800C not found. Check USB connection and port in Settings.")
        return False

    number = _format_phone_number(phone_number)
    print(f"  📱 Sending SMS to {number} via {port}...")

    ser = None
    try:
        import serial
        ser = serial.Serial(
            port, baudrate=9600, timeout=10,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE
        )
        time.sleep(0.5)  # Let port stabilize
        ser.flushInput()

        def send_at(cmd, wait=1, expect=None):
            ser.flushInput()
            ser.write((cmd + "\r\n").encode())
            time.sleep(wait)
            resp = ser.read(ser.in_waiting or 100).decode("utf-8", errors="ignore")
            if expect and expect not in resp:
                print(f"  ⚠️  AT cmd {cmd!r} → unexpected: {resp.strip()!r}")
            return resp

        # Basic check — retry a few times before giving up, since a
        # momentary hiccup here shouldn't fail the whole send when the
        # device is otherwise fine (matches the tolerance _test_sim800c
        # already uses during the startup scan).
        at_ok = False
        for _at_attempt in range(3):
            if "OK" in send_at("AT", wait=1):
                at_ok = True
                break
            time.sleep(1)
        if not at_ok:
            print("  ❌ SIM800C not responding to AT command (after 3 attempts).")
            return False

        # Check network registration
        resp = send_at("AT+CREG?", wait=1)
        if ",1" not in resp and ",5" not in resp:
            print("  ❌ SIM not registered on network. Check SIM card.")
            return False

        # Check signal quality
        resp = send_at("AT+CSQ", wait=0.5)
        print(f"  📶 Signal: {resp.strip()}")

        # Set SMS text mode
        send_at("AT+CMGF=1", wait=0.5)

        # Set character set to GSM for best compatibility
        send_at('AT+CSCS="GSM"', wait=0.5)

        # Send SMS
        resp = send_at(f'AT+CMGS="{number}"', wait=2)
        if ">" not in resp:
            print(f"  ❌ No prompt from modem: {resp.strip()!r}")
            return False

        # Write message body + Ctrl-Z to send
        ser.write((message_text + chr(26)).encode("utf-8", errors="replace"))

        # Poll for the confirmation instead of one fixed-length read —
        # a slightly slow modem can send "+CMGS:" a moment after a single
        # fixed wait would have already given up, which previously showed
        # up as a false "Unclear SMS result" even though the SMS was
        # likely still sent successfully.
        resp = ""
        max_wait = 12  # total seconds to wait for confirmation
        waited = 0
        while waited < max_wait:
            time.sleep(1)
            waited += 1
            resp += ser.read(ser.in_waiting or 300).decode("utf-8", errors="ignore")
            if "+CMGS:" in resp or "ERROR" in resp:
                break

        if "+CMGS:" in resp:
            print("  ✅ SMS sent successfully!")
            return True
        elif "ERROR" in resp:
            print(f"  ❌ SMS failed with error: {resp.strip()}")
            return False
        else:
            print(f"  ⚠️  Unclear SMS result after {waited}s: {resp.strip()!r}")
            return False

    except Exception as e:
        print(f"  ❌ SMS error: {e}")
        return False

    finally:
        # Always close, then give Windows/the CH340 driver a moment to
        # actually release the port handle. Without this pause, rapid
        # back-to-back sends (e.g. during a blast to many parents) can
        # hit the next open() before the previous close() has fully
        # completed at the OS level, producing a misleading
        # "PermissionError: Access is denied" on the port that has
        # nothing to do with the SIM or signal itself.
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(1)


def list_available_ports():
    try:
        import serial.tools.list_ports
        for port in serial.tools.list_ports.comports():
            print(f"  {port.device} — {port.description}")
            if _test_sim800c(port.device):
                print(f"    ✅ SIM800C detected!")
    except Exception as e:
        print(f"Error: {e}")


def test_sms(phone_number, port=None):
    """
    Send a test SMS. Returns (success, message) tuple.
    port: optional. If given, tests THIS specific SIM800C instead of
        whichever one auto-detect happens to find first — needed on a
        multi-SIM host to check each SIM's signal/network registration
        individually.
    """
    school = get_setting("school_name") or "School"
    number = _format_phone_number(phone_number)
    msg    = f"TEST: CES RFID Attendance System is working. - {school}"
    label  = f" via {port}" if port else ""
    print(f"\n📱 Sending test SMS to {number}{label}...")
    result = send_sms_sim800c(phone_number, msg, port=port)
    if result:
        print("✅ Test SMS sent!")
        return True, f"✅ Test SMS sent to {number}{label}!"
    else:
        return False, f"❌ Test SMS failed{label}. Check SIM800C connection, signal, and SIM registration."


def _format_phone_number(number):
    number = number.strip().replace(" ", "").replace("-", "")
    if number.startswith("+63"): return number
    if number.startswith("63"):  return "+" + number
    if number.startswith("09"):  return "+63" + number[1:]
    if number.startswith("9") and len(number) == 10: return "+63" + number
    return number


def _format_time(time_str):
    try:
        from datetime import datetime
        return datetime.strptime(time_str, "%H:%M:%S").strftime("%I:%M %p")
    except Exception:
        return time_str
