"""Export utilities: CSV, XLSX, PDF annual report and ZIP archive.

Every text cell passes through ``neutralize`` so user-controlled values that a
spreadsheet would treat as a formula (``=``, ``+``, ``-``, ``@``, tab, CR, as the first
character) are rendered as inert text. This covers values typed into forms *and* values
that arrived via CSV import.
"""
import csv
import io
import zipfile
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as pdf_canvas

FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")


def is_dangerous(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(FORMULA_TRIGGERS)


def neutralize(value: Any) -> Any:
    """Prefix risky strings with an apostrophe (OWASP CSV-injection guidance)."""
    if is_dangerous(value):
        return "'" + value
    return value


def to_csv(headers: list[str], rows: list[list]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([neutralize(h) for h in headers])
    for row in rows:
        writer.writerow([neutralize(v) for v in row])
    return buffer.getvalue().encode("utf-8-sig")


def _xlsx_cell(sheet: Any, value: Any, bold: bool = False) -> Any:
    cell = WriteOnlyCell(sheet, value=neutralize(value))
    if isinstance(cell.value, str):
        # Force a plain string cell: openpyxl would otherwise store "=..." as a formula.
        cell.data_type = "s"
    if bold:
        cell.font = Font(bold=True)
    return cell


def to_xlsx(sheets: dict[str, tuple[list[str], list[list]]]) -> bytes:
    # write_only keeps memory flat for large cohorts (rows are streamed to the zip).
    workbook = Workbook(write_only=True)
    for sheet_name, (headers, rows) in sheets.items():
        sheet = workbook.create_sheet(title=sheet_name[:31])
        sheet.append([_xlsx_cell(sheet, h, bold=True) for h in headers])
        for row in rows:
            sheet.append([_xlsx_cell(sheet, v) for v in row])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def build_pdf_report(
    cohort_name: str,
    year: int,
    sections: list[tuple[str, list[tuple[str, str]]]],
) -> bytes:
    buffer = io.BytesIO()
    pdf = pdf_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    pdf.setTitle(f"{cohort_name} Annual Report {year}")
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawCentredString(width / 2, height - 4 * cm, cohort_name)
    pdf.setFont("Helvetica", 14)
    pdf.drawCentredString(width / 2, height - 5.2 * cm, f"Annual Internship Report {year}")

    y = height - 7 * cm
    for title, items in sections:
        if y < 4 * cm:
            pdf.showPage()
            y = height - 4 * cm
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(2 * cm, y, title)
        y -= 0.8 * cm
        pdf.setFont("Helvetica", 10)
        for label, value in items:
            if y < 3 * cm:
                pdf.showPage()
                y = height - 4 * cm
                pdf.setFont("Helvetica", 10)
            text = f"{label}: {value}" if label else value
            pdf.drawString(2.4 * cm, y, text[:110])
            y -= 0.55 * cm
        y -= 0.5 * cm
    pdf.save()
    return buffer.getvalue()
