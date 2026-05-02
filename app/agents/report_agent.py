"""
app/agents/report_agent.py

Generate Excel (.xlsx) and PDF reports from Azure VM pricing text.
"""

import io
from datetime import datetime


# ── Excel ──────────────────────────────────────────────────────────────────────

def generate_excel(pricing_text: str, session_id: str = "") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "VM Pricing Summary"

    # ── Branding header ────────────────────────────────────────────────────────
    ws.merge_cells("A1:D1")
    ws["A1"] = "HyperXen.ai — Azure VM Pricing Report"
    ws["A1"].font      = Font(bold=True, size=14, color="FFFFFF")
    ws["A1"].fill      = PatternFill("solid", fgColor="0078D4")
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 32

    ws.merge_cells("A2:D2")
    ws["A2"] = f"Generated: {datetime.now().strftime('%d %b %Y %H:%M')}"
    ws["A2"].font      = Font(italic=True, size=10, color="8B95A2")
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 18

    # ── Parse pricing text into rows ───────────────────────────────────────────
    row = 4
    for line in pricing_text.splitlines():
        stripped = line.strip()

        if not stripped:
            row += 1
            continue

        if stripped.startswith("===") and stripped.endswith("==="):
            # Main section title (e.g. === Azure VM Pricing Estimate ===)
            label = stripped.strip("=").strip()
            ws.merge_cells(f"A{row}:D{row}")
            cell = ws.cell(row=row, column=1, value=label)
            cell.font      = Font(bold=True, size=11, color="FFFFFF")
            cell.fill      = PatternFill("solid", fgColor="005A9E")
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[row].height = 22

        elif stripped.startswith("---") and stripped.endswith("---"):
            # Sub-section header (e.g. --- PAYG ---)
            label = stripped.strip("-").strip()
            ws.merge_cells(f"A{row}:D{row}")
            cell = ws.cell(row=row, column=1, value=label)
            cell.font      = Font(bold=True, size=10, color="005A9E")
            cell.fill      = PatternFill("solid", fgColor="E8F4FD")
            cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            ws.row_dimensions[row].height = 18

        elif ":" in stripped and not stripped.startswith(" "):
            # Key: value pair → split across two columns
            key, _, val = stripped.partition(":")
            kc = ws.cell(row=row, column=1, value=key.strip())
            kc.font = Font(bold=True, size=10)
            vc = ws.cell(row=row, column=2, value=val.strip())
            vc.font = Font(size=10)

        else:
            # Indented detail line — merge across all columns
            ws.merge_cells(f"A{row}:D{row}")
            cell = ws.cell(row=row, column=1, value=stripped)
            cell.font      = Font(size=10, color="444444")
            cell.alignment = Alignment(indent=2)

        row += 1

    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 32
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 18

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── PDF ────────────────────────────────────────────────────────────────────────

def generate_pdf(pricing_text: str, session_id: str = "") -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    AZURE_BLUE = colors.HexColor("#0078D4")
    DARK_BLUE  = colors.HexColor("#005A9E")
    LIGHT_BLUE = colors.HexColor("#E8F4FD")
    GREY       = colors.HexColor("#8B95A2")
    DARK       = colors.HexColor("#1A1A2E")

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "RPTitle", parent=styles["Normal"],
        fontSize=16, fontName="Helvetica-Bold",
        textColor=colors.white, backColor=AZURE_BLUE,
        alignment=TA_CENTER, spaceAfter=0,
        borderPadding=(10, 14, 10, 14),
    )
    sub_style = ParagraphStyle(
        "RPSub", parent=styles["Normal"],
        fontSize=9, fontName="Helvetica-Oblique",
        textColor=GREY, alignment=TA_CENTER, spaceAfter=14,
    )
    main_title_style = ParagraphStyle(
        "RPMainTitle", parent=styles["Normal"],
        fontSize=11, fontName="Helvetica-Bold",
        textColor=colors.white, backColor=DARK_BLUE,
        alignment=TA_CENTER, spaceBefore=8, spaceAfter=4,
        borderPadding=(5, 10, 5, 10),
    )
    section_style = ParagraphStyle(
        "RPSection", parent=styles["Normal"],
        fontSize=10, fontName="Helvetica-Bold",
        textColor=DARK_BLUE, backColor=LIGHT_BLUE,
        spaceBefore=10, spaceAfter=3,
        borderPadding=(4, 8, 4, 8),
    )
    kv_style = ParagraphStyle(
        "RPKV", parent=styles["Normal"],
        fontSize=9, fontName="Helvetica",
        textColor=DARK, spaceBefore=2, spaceAfter=2, leftIndent=8,
    )
    body_style = ParagraphStyle(
        "RPBody", parent=styles["Normal"],
        fontSize=9, fontName="Helvetica",
        textColor=DARK, spaceBefore=1, spaceAfter=1, leftIndent=14,
    )
    footer_style = ParagraphStyle(
        "RPFooter", parent=styles["Normal"],
        fontSize=8, textColor=GREY,
        alignment=TA_CENTER, spaceBefore=6,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm,
    )

    story = [
        Paragraph("HyperXen.ai — Azure VM Pricing Report", title_style),
        Paragraph(f"Generated: {datetime.now().strftime('%d %b %Y %H:%M')}", sub_style),
    ]

    for line in pricing_text.splitlines():
        stripped = line.strip()

        if not stripped:
            story.append(Spacer(1, 4))
            continue

        if stripped.startswith("===") and stripped.endswith("==="):
            label = stripped.strip("=").strip()
            story.append(Paragraph(label, main_title_style))

        elif stripped.startswith("---") and stripped.endswith("---"):
            label = stripped.strip("-").strip()
            story.append(Paragraph(label, section_style))

        elif ":" in stripped and not stripped.startswith(" "):
            key, _, val = stripped.partition(":")
            story.append(Paragraph(
                f"<b>{key.strip()}:</b> {val.strip()}", kv_style
            ))

        else:
            story.append(Paragraph(stripped, body_style))

    story += [
        Spacer(1, 10),
        HRFlowable(width="100%", thickness=0.5, color=GREY),
        Paragraph(
            "All prices are Microsoft Retail RRP. CSP pricing would be lower.",
            footer_style,
        ),
    ]

    doc.build(story)
    return buf.getvalue()
