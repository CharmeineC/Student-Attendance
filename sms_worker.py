"""
sms_worker.py
-------------
Standalone SMS-sending worker for a MULTI-PC RFID attendance setup.

Run this on any secondary PC that has its OWN SIM800C module attached
(e.g. PC #2, PC #3) — NOT on the host PC, which handles its own SIM800C
automatically inside app.py.

WHY THIS EXISTS:
Sending every parent's SMS through a single SIM card gets throttled by
telco fair-use policies once volume gets high. This script lets multiple
PCs, each with their own SIM800C, share the job — the host queues SMS
centrally, and whichever worker (this script, or the host's own internal
one) is free grabs the next job and sends it through its own local SIM.

This script has NO dependency on the main app's database.py or
attendance.db — it only needs the `pyserial` and `requests` packages,
and talks to the host purely over the network (same WiFi).

SETUP:
    pip install pyserial requests --break-system-packages

USAGE:
    python sms_worker.py --host http://192.168.100.186:5000

    Optional flags:
        --port COM4          Force a specific COM port (auto-detected if omitted)
        --worker-id pc2       Name shown in logs/queue (defaults to this PC's hostname)
        --poll-seconds 5      How often to check for new jobs when idle

    Leave this running in its own terminal window on each secondary PC
    while the school day is in session. Press Ctrl+C to stop.
"""

import argparse
import socket
import time

import requests
import serial
import serial.tools.list_ports


def find_sim800c_port():
    """Auto-detect a likely SIM800C/CH340 serial port on this PC."""
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").upper()
        if "CH340" in desc or "USB-SERIAL" in desc or "USB SERIAL" in desc:
            return p.device
    return None


def format_phone_number(raw):
    """Handles Philippine numbers (09xx -> +639xx automatically)."""
    raw = (raw or "").strip().replace(" ", "").replace("-", "")
    if raw.startswith("+63"):
        return raw
    if raw.startswith("09") and len(raw) == 11:
        return "+63" + raw[1:]
    if raw.startswith("63"):
        return "+" + raw
    return raw


def send_sms(port, phone_number, message_text):
    """
    Sends one SMS via this PC's locally-attached SIM800C.
    Self-contained copy of the same AT-command flow used in the main
    app's notifier.py, kept independent here so this script has zero
    dependency on the rest of the project.
    Returns (success: bool, error_message: str, message_ref: int or None).
    message_ref is the modem's reference number for this SMS (from the
    +CMGS response) — needed to later match a delivery report back to
    this exact message, if the delivery-report listener is set up.
    """
    number = format_phone_number(phone_number)
    print(f"  📱 Sending SMS to {number} via {port}...")

    ser = None
    try:
        ser = serial.Serial(
            port, baudrate=9600, timeout=10,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE
        )
        time.sleep(0.5)
        ser.flushInput()

        def send_at(cmd, wait=1):
            ser.flushInput()
            ser.write((cmd + "\r\n").encode())
            time.sleep(wait)
            return ser.read(ser.in_waiting or 100).decode("utf-8", errors="ignore")

        if "OK" not in send_at("AT", wait=1):
            print("  ❌ SIM800C not responding to AT command.")
            return False, "modem not responding", None

        resp = send_at("AT+CREG?", wait=1)
        if ",1" not in resp and ",5" not in resp:
            print("  ❌ SIM not registered on network. Check SIM card.")
            return False, "SIM not registered on network", None

        send_at("AT+CMGF=1", wait=0.5)
        send_at('AT+CSCS="GSM"', wait=0.5)
        # Request a delivery report from the network — see notifier.py's
        # send_sms_sim800c for the same setting and its caveats (whether
        # Globe actually honors this can only be confirmed by real testing).
        send_at("AT+CSMP=49,167,0,0", wait=0.5)

        resp = send_at(f'AT+CMGS="{number}"', wait=2)
        if ">" not in resp:
            print(f"  ❌ No prompt from modem: {resp.strip()!r}")
            return False, "no prompt from modem", None

        ser.write((message_text + chr(26)).encode("utf-8", errors="replace"))
        time.sleep(6)
        resp = ser.read(ser.in_waiting or 300).decode("utf-8", errors="ignore")

        if "+CMGS:" in resp:
            message_ref = None
            try:
                ref_part = resp.split("+CMGS:")[1].strip()
                message_ref = int(ref_part.split()[0].strip())
            except (IndexError, ValueError):
                pass
            print(f"  ✅ SMS sent successfully! (ref: {message_ref})")
            return True, "", message_ref
        elif "ERROR" in resp:
            print(f"  ❌ SMS failed: {resp.strip()}")
            return False, resp.strip()[:200], None
        else:
            print(f"  ⚠️  Unclear result: {resp.strip()!r}")
            return False, "unclear modem response", None

    except Exception as e:
        print(f"  ❌ SMS error: {e}")
        return False, str(e)[:200], None

    finally:
        # Settle delay so Windows/the CH340 driver fully releases the port
        # before this loop tries to open it again for the next job.
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(1)


def main():
    import random
    parser = argparse.ArgumentParser(
        description="SMS worker for a multi-PC RFID attendance system"
    )
    parser.add_argument("--host", required=True,
                         help="Host server URL, e.g. http://192.168.100.186:5000")
    parser.add_argument("--port", default=None,
                         help="COM port for this PC's SIM800C, e.g. COM4. Auto-detected if omitted.")
    parser.add_argument("--worker-id", default=None,
                         help="Name for this worker (defaults to this PC's hostname)")
    parser.add_argument("--poll-seconds", type=int, default=5,
                         help="How often to check for new jobs when idle (default 5s)")
    parser.add_argument("--min-pace-seconds", type=int, default=3,
                         help="Minimum random pause after each send, before claiming the next job (default 5s)")
    parser.add_argument("--max-pace-seconds", type=int, default=15,
                         help="Maximum random pause after each send, before claiming the next job (default 30s)")
    args = parser.parse_args()

    worker_id = args.worker_id or socket.gethostname()
    host = args.host.rstrip("/")
    port = args.port or find_sim800c_port()

    if not port:
        print("❌ No SIM800C port found. Plug it in, or pass --port COMx explicitly.")
        return

    print("=" * 55)
    print(f"  SMS Worker — '{worker_id}'")
    print("=" * 55)
    print(f"  Using port:     {port}")
    print(f"  Polling host:   {host}")
    print(f"  Poll interval:  {args.poll_seconds}s (when idle)")
    print(f"  Send pacing:    {args.min_pace_seconds}-{args.max_pace_seconds}s random pause after each send")
    print("  Press Ctrl+C to stop.\n")

    while True:
        try:
            r = requests.get(
                f"{host}/api/sms_queue/next",
                params={"worker_id": worker_id},
                timeout=10
            )
            data = r.json()
            job = data.get("job")

            if job:
                job_id = job["id"]
                phone = job["phone_number"]
                message = job["message"]
                print(f"📥 Job #{job_id}: sending to {phone}")

                success, error, message_ref = send_sms(port, phone, message)

                requests.post(f"{host}/api/sms_queue/complete", json={
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "success": success,
                    "error": error,
                    "message_ref": message_ref,
                }, timeout=10)

                # Random pacing delay after a real send, so a backlog of
                # many queued messages doesn't fire in an obviously
                # mechanical, bot-like rhythm — see notifier.py's
                # start_sms_queue_worker for the same logic on the host.
                pace = random.uniform(args.min_pace_seconds, args.max_pace_seconds)
                time.sleep(pace)
            else:
                time.sleep(args.poll_seconds)

        except requests.exceptions.RequestException as e:
            print(f"⚠️  Could not reach host ({e}). Retrying in {args.poll_seconds}s...")
            time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            print("\n👋 Worker stopped.")
            break
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
