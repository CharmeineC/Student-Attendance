"""
reports.py
----------
This file generates the monthly attendance report that admin can export.
It creates a nicely formatted Excel file (.xlsx) that can be opened in
Microsoft Excel or LibreOffice.

Required library: openpyxl
Install it by running in Command Prompt:
    pip install openpyxl
"""

import openpyxl
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side
)
from openpyxl.utils import get_column_letter
from datetime import datetime, date
import os

from database import get_logs_for_report, get_setting, ph_now, get_students_for_blast, get_all_sections
from datetime import timedelta


def _school_weekdays(start_date, end_date):
    """
    List of school-day dates (Mon–Fri) between start_date and end_date,
    inclusive, as date objects. Doesn't know about holidays/breaks —
    just excludes weekends, since there's no school calendar built into
    this system to check against.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end   = datetime.strptime(end_date, "%Y-%m-%d").date()
    days  = []
    current = start
    while current <= end:
        if current.weekday() < 5:  # Monday=0 ... Friday=4
            days.append(current)
        current += timedelta(days=1)
    return days


def _get_students_for_report(section=None, grade_level=None):
    """Same section/grade_level filtering logic used for the logs, but
    returns ALL matching students regardless of whether they ever
    scanned — this is what lets an absent student actually show up in
    the per-student summary, instead of just being invisible."""
    if grade_level and not section:
        grade_sections = [s for s in get_all_sections()
                          if s.split("-")[0].strip() == grade_level or s.strip() == grade_level]
        students = []
        for gs in grade_sections:
            students.extend(get_students_for_blast(gs))
        return students
    return get_students_for_blast(section)


def export_monthly_report(year, month, section=None, output_folder=".", grade_level=None):
    """
    Generate an Excel attendance report for a given month.

    year          : int, e.g. 2026
    month         : int, e.g. 5 for May
    section       : str or None — if given, only include that section
    output_folder : where to save the file (defaults to current folder)

    Returns the file path of the saved Excel file.
    """

    # Work out the date range for the chosen month
    start_date = f"{year}-{month:02d}-01"
    # Last day of the month
    if month == 12:
        end_date = f"{year}-12-31"
    else:
        next_month_first = date(year, month + 1, 1)
        last_day = (next_month_first - __import__('datetime').timedelta(days=1)).day
        end_date = f"{year}-{month:02d}-{last_day}"

    # Pull the data from the database
    # If grade_level given, filter by sections starting with that grade
    if grade_level and not section:
        from database import get_all_sections
        grade_sections = [s for s in get_all_sections()
                         if s.split("-")[0].strip() == grade_level or s.strip() == grade_level]
        all_logs = []
        for gs in grade_sections:
            all_logs.extend(get_logs_for_report(start_date, end_date, gs))
        logs = all_logs
    else:
        logs = get_logs_for_report(start_date, end_date, section)

    # Build the Excel workbook
    wb = openpyxl.Workbook()
    ws = wb.active

    school_name = get_setting("school_name") or "School"
    month_name  = datetime(year, month, 1).strftime("%B %Y")  # e.g. "May 2026"
    ws.title = f"Attendance {month_name}"

    # ── Color palette ──────────────────────────────────────────────────────
    COLOR_HEADER_BG   = "1A3C5E"   # dark navy  — header row
    COLOR_HEADER_TEXT = "FFFFFF"   # white
    COLOR_TITLE_BG    = "2E86AB"   # blue       — title row
    COLOR_IN_BG       = "E8F5E9"   # light green — time-in rows
    COLOR_OUT_BG      = "FFF8E1"   # light amber — time-out rows
    COLOR_ALT_BG      = "F9F9F9"   # very light grey — alternating rows
    COLOR_ACCENT      = "2E86AB"   # blue border accent

    # ── Helper: make a thin border ─────────────────────────────────────────
    thin = Side(style="thin", color="CCCCCC")
    full_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # ── Row 1: School name (big title) ─────────────────────────────────────
    ws.merge_cells("A1:H1")
    ws["A1"] = school_name
    ws["A1"].font      = Font(bold=True, size=14, color=COLOR_HEADER_TEXT)
    ws["A1"].fill      = PatternFill("solid", fgColor=COLOR_TITLE_BG)
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    # ── Row 2: Report subtitle ─────────────────────────────────────────────
    ws.merge_cells("A2:H2")
    subtitle = f"Attendance Report — {month_name}"
    if section:
        subtitle += f" — {section}"
    ws["A2"] = subtitle
    ws["A2"].font      = Font(bold=True, size=11, color=COLOR_HEADER_TEXT)
    ws["A2"].fill      = PatternFill("solid", fgColor=COLOR_TITLE_BG)
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 22

    # ── Row 3: Generated date ──────────────────────────────────────────────
    ws.merge_cells("A3:H3")
    ws["A3"] = f"Generated: {ph_now().strftime('%B %d, %Y at %I:%M %p')}"
    ws["A3"].font      = Font(italic=True, size=9, color="777777")
    ws["A3"].alignment = Alignment(horizontal="right")
    ws.row_dimensions[3].height = 16

    # ── Row 4: blank spacer ────────────────────────────────────────────────
    ws.row_dimensions[4].height = 8

    # ── Row 5: Column headers ──────────────────────────────────────────────
    headers = [
        "No.", "Student Name", "Section",
        "Date", "Time In", "Time Out",
        "Status", "Notified Via"
    ]
    for col_num, header in enumerate(headers, start=1):
        cell = ws.cell(row=5, column=col_num, value=header)
        cell.font      = Font(bold=True, size=10, color=COLOR_HEADER_TEXT)
        cell.fill      = PatternFill("solid", fgColor=COLOR_HEADER_BG)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border    = full_border
    ws.row_dimensions[5].height = 20

    # ── Rows 6+: Data ──────────────────────────────────────────────────────
    # Group logs by student + date so we can show time-in and time-out
    # on the SAME row instead of two separate rows
    # Group logs by student + date so we can show time-in and time-out
    # on the SAME row. If a student scans multiple times in one day,
    # we keep the EARLIEST scan as Time In and the LATEST scan as Time Out
    # (scan_time is stored as 24-hour HH:MM:SS so string comparison works).
    grouped = {}  # key: (student_name, scan_date) → {"in": ..., "out": ...}
    for log in logs:
        key = (log["full_name"], log["scan_date"])
        if key not in grouped:
            grouped[key] = {
                "full_name": log["full_name"],
                "section":   log["section"],
                "scan_date": log["scan_date"],
                "time_in":   "",
                "time_out":  "",
                "notified":  log["notified"],
                "channel":   log["notify_channel"] or ""
            }
        entry = grouped[key]
        if log["scan_type"] == "IN":
            # Keep the EARLIEST "in" scan of the day (first arrival)
            if not entry["time_in"] or log["scan_time"] < entry["time_in"]:
                entry["time_in"] = log["scan_time"]
        else:
            # Keep the LATEST "out" scan of the day (last departure)
            if not entry["time_out"] or log["scan_time"] > entry["time_out"]:
                entry["time_out"] = log["scan_time"]
                entry["notified"] = log["notified"]
                entry["channel"]  = log["notify_channel"] or ""

    sorted_rows = sorted(grouped.values(),
                         key=lambda r: (r["scan_date"], r["full_name"]))

    for row_num, entry in enumerate(sorted_rows, start=1):
        excel_row = row_num + 5   # data starts at row 6

        has_in  = bool(entry["time_in"])
        has_out = bool(entry["time_out"])

        if has_in and has_out:
            status   = "Complete"
            row_fill = PatternFill("solid", fgColor=COLOR_ALT_BG if row_num % 2 == 0 else "FFFFFF")
        elif has_in:
            status   = "Still In"
            row_fill = PatternFill("solid", fgColor=COLOR_IN_BG)
        else:
            status   = "Out Only"
            row_fill = PatternFill("solid", fgColor=COLOR_OUT_BG)

        # Format date nicely: "2026-05-30" → "May 30, 2026"
        try:
            date_obj     = datetime.strptime(entry["scan_date"], "%Y-%m-%d")
            date_display = date_obj.strftime("%b %d, %Y")
        except Exception:
            date_display = entry["scan_date"]

        channel_display = (entry["channel"] or "—").title()

        row_data = [
            row_num,
            entry["full_name"],
            entry["section"],
            date_display,
            entry["time_in"]  or "—",
            entry["time_out"] or "—",
            status,
            channel_display,
        ]

        for col_num, value in enumerate(row_data, start=1):
            cell = ws.cell(row=excel_row, column=col_num, value=value)
            cell.fill      = row_fill
            cell.border    = full_border
            cell.font      = Font(size=10)
            cell.alignment = Alignment(vertical="center",
                                       horizontal="center" if col_num in (1, 4, 5, 6, 7, 8) else "left")

        ws.row_dimensions[excel_row].height = 18

    # ── Summary row at the bottom ──────────────────────────────────────────
    total_records = len(sorted_rows)
    summary_row = total_records + 6
    ws.merge_cells(f"A{summary_row}:F{summary_row}")
    ws[f"A{summary_row}"] = f"Total records: {total_records}"
    ws[f"A{summary_row}"].font      = Font(bold=True, size=10)
    ws[f"A{summary_row}"].alignment = Alignment(horizontal="right")

    # ── Set column widths ──────────────────────────────────────────────────
    column_widths = [6, 28, 22, 14, 12, 12, 12, 16]
    for col_num, width in enumerate(column_widths, start=1):
        ws.column_dimensions[get_column_letter(col_num)].width = width

    # ── Freeze the header rows so they stay visible when scrolling ─────────
    ws.freeze_panes = "A6"

    # ── Sheet 2: ALL raw scan logs ───────────────────────────────────────
    # Shows EVERY individual scan (not summarized), so teachers can see if
    # a student scanned multiple times in a day (e.g. playing at the kiosk).
    ws2 = wb.create_sheet("All Scans Log")

    header_font2 = Font(bold=True, size=11, color="FFFFFF")
    header_fill2 = PatternFill(start_color="1565C0", end_color="1565C0", fill_type="solid")

    ws2.merge_cells("A1:F1")
    ws2["A1"] = f"{school_name} — All Scans Log ({month_name})"
    ws2["A1"].font = Font(bold=True, size=13)
    ws2.merge_cells("A2:F2")
    ws2["A2"] = "Complete list of all individual scans."
    ws2["A2"].font = Font(italic=True, size=9, color="666666")

    log_headers = ["#", "Student", "Section", "Date", "Time", "Type"]
    for col_num, htext in enumerate(log_headers, start=1):
        cell = ws2.cell(row=4, column=col_num, value=htext)
        cell.font      = header_font2
        cell.fill      = header_fill2
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border    = full_border
    ws2.row_dimensions[4].height = 20

    # Sort all raw logs chronologically (not grouped/collapsed)
    sorted_logs = sorted(logs, key=lambda l: (l["full_name"], l["scan_date"], l["scan_time"]))

    for row_num, log in enumerate(sorted_logs, start=1):
        excel_row = row_num + 4
        try:
            date_obj = datetime.strptime(log["scan_date"], "%Y-%m-%d")
            date_disp = date_obj.strftime("%b %d, %Y")
        except Exception:
            date_disp = log["scan_date"]

        row_fill2 = PatternFill(start_color="FFFFFF" if row_num % 2 else "F8FAFC",
                                end_color="FFFFFF" if row_num % 2 else "F8FAFC",
                                fill_type="solid")

        scan_type_disp = "🟢 IN" if log["scan_type"] == "IN" else "🟠 OUT"

        row_data2 = [row_num, log["full_name"], log["section"], date_disp,
                     log["scan_time"], scan_type_disp]
        for col_num, value in enumerate(row_data2, start=1):
            cell = ws2.cell(row=excel_row, column=col_num, value=value)
            cell.fill      = row_fill2
            cell.border    = full_border
            cell.font      = Font(size=10)
            cell.alignment = Alignment(vertical="center",
                                       horizontal="center" if col_num in (1, 4, 5, 6) else "left")
        ws2.row_dimensions[excel_row].height = 17

    total_scans = len(sorted_logs)
    summary_row2 = total_scans + 5
    ws2.merge_cells(f"A{summary_row2}:F{summary_row2}")
    ws2[f"A{summary_row2}"] = f"Total individual scans: {total_scans}"
    ws2[f"A{summary_row2}"].font      = Font(bold=True, size=10)
    ws2[f"A{summary_row2}"].alignment = Alignment(horizontal="right")

    log_col_widths = [6, 28, 22, 16, 12, 10]
    for col_num, width in enumerate(log_col_widths, start=1):
        ws2.column_dimensions[get_column_letter(col_num)].width = width

    ws2.freeze_panes = "A5"

    # ── Sheet 3: Per-Student Daily Summary ──────────────────────────────
    # The comprehensive teacher-facing view: EVERY student (not just those
    # who scanned) as a row, one column per school day, marking whether
    # each student had any scan that day at all. This is what actually
    # surfaces absences — a student with zero scans all month previously
    # never appeared anywhere in the report; here they show up as an
    # unbroken row of "A" instead of being silently missing.
    ws3 = wb.create_sheet("Per-Student Daily Summary")

    all_students = _get_students_for_report(section, grade_level)
    school_days  = _school_weekdays(start_date, end_date)

    # Build a fast lookup: which (student_id, date) pairs actually have
    # at least one scan, from the same logs already pulled for this report.
    present_lookup = set()
    for log in logs:
        present_lookup.add((log["student_id"], log["scan_date"]))

    COLOR_PRESENT_BG = "E8F5E9"  # light green
    COLOR_ABSENT_BG  = "FFEBEE"  # light red/pink
    COLOR_PRESENT_TXT = "2E7D32"
    COLOR_ABSENT_TXT  = "C62828"

    title3 = f"{school_name} — Per-Student Daily Summary ({month_name})"
    last_col_letter = get_column_letter(4 + len(school_days) + 2)
    ws3.merge_cells(f"A1:{last_col_letter}1")
    ws3["A1"] = title3
    ws3["A1"].font = Font(bold=True, size=13)
    ws3.merge_cells(f"A2:{last_col_letter}2")
    ws3["A2"] = "P = present (at least one scan that day)   A = absent (no scan recorded)"
    ws3["A2"].font = Font(italic=True, size=9, color="666666")

    # Header row: No. | Student | Section | [one column per school day] | Days Present | Attendance %
    header_row3 = 4
    ws3.cell(row=header_row3, column=1, value="No.")
    ws3.cell(row=header_row3, column=2, value="Student Name")
    ws3.cell(row=header_row3, column=3, value="Section")
    for i, day in enumerate(school_days):
        col = 4 + i
        cell = ws3.cell(row=header_row3, column=col, value=f"{day.month}/{day.day}")
        cell.alignment = Alignment(text_rotation=90, horizontal="center", vertical="center")
    summary_col1 = 4 + len(school_days)
    summary_col2 = summary_col1 + 1
    ws3.cell(row=header_row3, column=summary_col1, value="Days Present")
    ws3.cell(row=header_row3, column=summary_col2, value="Attendance %")

    for col_num in range(1, summary_col2 + 1):
        cell = ws3.cell(row=header_row3, column=col_num)
        cell.font      = header_font2
        cell.fill      = header_fill2
        cell.border    = full_border
        if col_num <= 3 or col_num >= summary_col1:
            cell.alignment = Alignment(horizontal="center", vertical="center")
    ws3.row_dimensions[header_row3].height = 40

    # Sort students by section then name for a teacher-friendly grouping
    sorted_students = sorted(all_students, key=lambda s: (s["section"] or "", s["full_name"]))

    for row_idx, student in enumerate(sorted_students, start=1):
        excel_row = header_row3 + row_idx
        ws3.cell(row=excel_row, column=1, value=row_idx)
        ws3.cell(row=excel_row, column=2, value=student["full_name"])
        ws3.cell(row=excel_row, column=3, value=student["section"])

        days_present = 0
        for i, day in enumerate(school_days):
            col = 4 + i
            day_str = day.strftime("%Y-%m-%d")
            is_present = (student["id"], day_str) in present_lookup
            if is_present:
                days_present += 1
            cell = ws3.cell(row=excel_row, column=col, value="P" if is_present else "A")
            cell.font      = Font(size=9, bold=True,
                                  color=COLOR_PRESENT_TXT if is_present else COLOR_ABSENT_TXT)
            cell.fill      = PatternFill("solid", fgColor=COLOR_PRESENT_BG if is_present else COLOR_ABSENT_BG)
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border    = full_border

        total_days = len(school_days)
        pct = round((days_present / total_days) * 100, 1) if total_days else 0

        for col_num, value in [(1, row_idx), (2, student["full_name"]), (3, student["section"]),
                                (summary_col1, days_present), (summary_col2, f"{pct}%")]:
            cell = ws3.cell(row=excel_row, column=col_num, value=value)
            cell.border = full_border
            cell.font   = Font(size=10, bold=(col_num >= summary_col1))
            cell.alignment = Alignment(horizontal="center" if col_num != 2 else "left", vertical="center")

    ws3.column_dimensions["A"].width = 6
    ws3.column_dimensions["B"].width = 28
    ws3.column_dimensions["C"].width = 22
    for i in range(len(school_days)):
        ws3.column_dimensions[get_column_letter(4 + i)].width = 4
    ws3.column_dimensions[get_column_letter(summary_col1)].width = 13
    ws3.column_dimensions[get_column_letter(summary_col2)].width = 13

    ws3.freeze_panes = ws3.cell(row=header_row3 + 1, column=4).coordinate

    # Overall summary line at the bottom
    summary_row3 = header_row3 + len(sorted_students) + 2
    ws3.merge_cells(f"A{summary_row3}:C{summary_row3}")
    ws3[f"A{summary_row3}"] = f"Total students: {len(sorted_students)}   |   School days this period: {len(school_days)}"
    ws3[f"A{summary_row3}"].font = Font(bold=True, size=10)

    # ── Save the file ──────────────────────────────────────────────────────
    os.makedirs(output_folder, exist_ok=True)
    if grade_level and not section:
        section_tag = f"_{grade_level.replace(' ','_')}"
    elif section:
        section_tag = f"_{section.replace(' ','_')}"
    else:
        section_tag = ""
    filename = f"Attendance_{month_name.replace(' ','_')}{section_tag}.xlsx"
    filepath    = os.path.join(output_folder, filename)

    wb.save(filepath)
    print(f"✅ Report saved: {filepath}")
    return filepath


def export_date_range_report(start_date, end_date, section=None, output_folder="."):
    """
    Generate an Excel report for a custom date range.
    start_date and end_date are strings like "2026-05-01".
    """
    logs = get_logs_for_report(start_date, end_date, section)

    wb = openpyxl.Workbook()
    ws = wb.active

    school_name = get_setting("school_name") or "School"
    ws.title = "Attendance Report"

    # Reuse same logic — simplified version
    ws.append([school_name])
    ws.append([f"Attendance: {start_date} to {end_date}"])
    ws.append([])
    ws.append(["Student", "Section", "Date", "Scan Type", "Time", "Notified Via"])

    for log in logs:
        ws.append([
            log["full_name"],
            log["section"],
            log["scan_date"],
            log["scan_type"],
            log["scan_time"],
            log["notify_channel"] or "—"
        ])

    os.makedirs(output_folder, exist_ok=True)
    filename = f"Attendance_{start_date}_to_{end_date}.xlsx"
    filepath = os.path.join(output_folder, filename)
    wb.save(filepath)
    print(f"✅ Report saved: {filepath}")
    return filepath


# ── QUICK TEST ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from database import setup_database, add_student, record_scan, determine_scan_type, get_student_by_rfid

    print("Setting up test data...")
    setup_database()

    # Add test students
    test_students = [
        ("0004A3F1", "Juan dela Cruz",  "Grade 7 - Sampaguita", "09171234567"),
        ("0008B2E4", "Maria Santos",    "Grade 7 - Sampaguita", "09181234567"),
        ("000C91AA", "Pedro Reyes",     "Grade 8 - Ilang-ilang","09191234567"),
    ]
    for rfid, name, section, phone in test_students:
        add_student(rfid, name, section, parent_phone=phone)

    # Record some test scans
    for rfid, name, section, phone in test_students:
        student = get_student_by_rfid(rfid)
        if student:
            scan_type = determine_scan_type(student["id"])
            record_scan(student["id"], rfid, "IN")
            record_scan(student["id"], rfid, "OUT")

    # Export report for current month
    now = ph_now()
    filepath = export_monthly_report(now.year, now.month, output_folder="./reports")
    print(f"\nOpen this file to see the report:\n  {os.path.abspath(filepath)}")
