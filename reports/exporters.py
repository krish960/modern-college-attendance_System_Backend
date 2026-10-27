"""CSV, Excel (.xlsx) and PDF export helpers (PDF uses ReportLab, Excel uses openpyxl).

Every report and download view hands its rows to export_response(), which makes the CSV, Excel or PDF file to send back."""
import csv
from io import BytesIO
from xml.sax.saxutils import escape

from django.http import HttpResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _text(value):
    """Any value as text; None becomes an empty string."""
    return "" if value is None else str(value)


def _csv_safe(value):
    """Stop spreadsheet formula injection: a cell starting with = + - @ would run as a formula."""
    text = _text(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(filename, headers, rows):
    """Make a CSV download from headers and rows. Cells are made formula-safe."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    response.write("\ufeff")  # the byte-order mark makes Excel read Marathi (UTF-8) names correctly
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_safe(cell) for cell in row])
    return response


def _letterhead(styles):
    """College name and address at the top of every PDF."""
    from django.conf import settings
    name = ParagraphStyle("college", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=13, leading=16, alignment=1)
    addr = ParagraphStyle("collegeaddr", parent=styles["Normal"], fontSize=9, leading=11, alignment=1, textColor=colors.HexColor("#555555"))
    return [Paragraph(escape(settings.COLLEGE_NAME), name), Paragraph(escape(settings.COLLEGE_ADDRESS), addr), Spacer(1, 3 * mm)]


def export_pdf(filename, title, subtitle, headers, rows):
    """Make a landscape A4 PDF with the college heading, a title and one table (the header row repeats on each page)."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=8, leading=10)
    head = ParagraphStyle("head", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")

    story = _letterhead(styles) + [Paragraph(escape(title), styles["Title"])]
    if subtitle:
        story += [Paragraph(escape(subtitle), styles["Normal"]), Spacer(1, 6 * mm)]

    data = [[Paragraph(escape(h), head) for h in headers]]  # escape() so characters like < and & cannot break the PDF text
    for row in rows:
        data.append([Paragraph(escape(_text(c)) or "-", cell) for c in row])
    if len(data) == 1:
        story.append(Paragraph("No records for the selected filters.", styles["Normal"]))
    else:
        table = Table(data, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3b4a9c")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f4fb")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9cfe6")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(table)
    doc.build(story)

    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    return response


def export_sheet_pdf(filename, title, subtitle, columns, rows, tail_headers=("P", "Total", "%")):
    """
    A register-style page: roll, name, one narrow column per lecture (day over subject code), then totals.
    `columns` = [(day, code)], `rows` = [(roll, name, [cells], [tail cells])]. Many lectures are split into blocks of
    columns that repeat the roll and name, so nothing gets squeezed.
    """
    from reportlab.platypus import PageBreak

    per_block = 31  # at most 31 lecture columns in one block (about one month)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=12 * mm, bottomMargin=12 * mm, title=title,
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["BodyText"], fontSize=7, leading=8.5)
    story = _letterhead(styles) + [Paragraph(escape(title), styles["Title"])]
    if subtitle:
        story += [Paragraph(escape(subtitle), styles["Normal"]), Spacer(1, 4 * mm)]
    if not rows:
        story.append(Paragraph("No students in this class.", styles["Normal"]))
    elif not columns:
        story.append(Paragraph("No submitted lectures for the selected month.", styles["Normal"]))
    blocks = [columns[i:i + per_block] for i in range(0, len(columns), per_block)] if columns else []
    for b, block in enumerate(blocks):
        start = b * per_block
        last = b == len(blocks) - 1
        head = ["Roll", "Name"] + [f"{d}\n{escape(str(code))[:4]}" for d, code in block] + (list(tail_headers) if last else [])
        data = [head]
        for roll, name, cells, tail in rows:
            data.append([_text(roll), Paragraph(escape(_text(name)), small)] + list(cells[start:start + per_block]) + (list(tail) if last else []))
        col_w = [11 * mm, 44 * mm] + [6.2 * mm] * len(block) + ([9 * mm, 10 * mm, 11 * mm] if last else [])
        table = Table(data, colWidths=col_w, repeatRows=1)
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 7), ("LEADING", (0, 0), (-1, -1), 8.5),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f3240")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ALIGN", (2, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f3ee")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c4c9bd")),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ]
        # colour every absent mark (A) red
        for r, (_, _, cells, _) in enumerate(rows, start=1):
            for c, v in enumerate(cells[start:start + per_block], start=2):
                if v == "A":
                    style.append(("TEXTCOLOR", (c, r), (c, r), colors.HexColor("#b3261e")))
        table.setStyle(TableStyle(style))
        story.append(table)
        if not last:
            story.append(PageBreak())
    doc.build(story)
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    return response


def export_xlsx(filename, title, subtitle, headers, rows):
    """
    Make a real Excel file: college name, title and subtitle on top, then the table with a bold coloured
    header row, sensible column widths and the header frozen while scrolling. Text cells are formula-safe.
    """
    from django.conf import settings
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = "".join(ch for ch in title if ch not in "[]:*?/\\")[:31] or "Report"
    sheet.append([settings.COLLEGE_NAME])
    sheet.append([title])
    sheet.append([subtitle or ""])
    sheet.append([])  # empty row between the heading and the table
    sheet["A1"].font = Font(bold=True, size=13)
    sheet["A2"].font = Font(bold=True, size=11)
    header_row = 5
    sheet.append(list(headers))
    for cell in sheet[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="3B4A9C")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        # numbers stay numbers (so Excel can sort and add them); everything else is plain safe text
        sheet.append([c if isinstance(c, (int, float)) and not isinstance(c, bool) else _csv_safe(c) for c in row])
    # column width: as wide as the longest value, between 8 and 45 characters
    for i, head in enumerate(headers, start=1):
        longest = max([len(_text(head))] + [len(_text(r[i - 1])) for r in rows if i - 1 < len(r)])
        sheet.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 8), 45)
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
    buffer = BytesIO()
    book.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    return response


def export_response(kind, filename, title, subtitle, headers, rows):
    """Send the rows as CSV when kind is "csv", as an Excel file for "xlsx", otherwise as a PDF."""
    if kind == "csv":
        return export_csv(filename, headers, rows)
    if kind == "xlsx":
        return export_xlsx(filename, title, subtitle, headers, rows)
    return export_pdf(filename, title, subtitle, headers, rows)
