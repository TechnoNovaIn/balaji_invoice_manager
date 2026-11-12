import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from datetime import datetime
from .export_security import safe_xlsx_value, sanitize_spreadsheet_text


def create_gst_portal_excel(invoices_data: list) -> io.BytesIO:
    """
    Generate an official GST Portal (GSTR-2B / Purchase ITC) compliant Excel workbook (.xlsx).
    Follows the exact column specifications required by the Goods and Services Tax Network (GSTN) offline utility.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "b2b"

    # GST Portal B2B Header definitions
    headers = [
        "GSTIN of Supplier",
        "Trade/Legal Name",
        "Invoice number",
        "Invoice date",
        "Invoice Value",
        "Place of Supply",
        "Reverse Charge",
        "Applicable % of Tax Rate",
        "Invoice Type",
        "E-Commerce GSTIN",
        "Rate",
        "Taxable Value",
        "Cess Amount",
    ]

    # Header styling (GST blue fill with white bold text)
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    thin_border = Border(
        left=Side(style='thin', color='CCCCCC'),
        right=Side(style='thin', color='CCCCCC'),
        top=Side(style='thin', color='CCCCCC'),
        bottom=Side(style='thin', color='CCCCCC')
    )

    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for item in invoices_data:
        gstin = (item.get('party_gstin') or '').strip().upper()
        party_name = (item.get('party_name') or 'Supplier').strip()
        inv_no = (item.get('invoice_number') or item.get('submission_id') or '').strip()
        inv_date = item.get('invoice_date')
        if isinstance(inv_date, str) and inv_date:
            try:
                inv_date_str = datetime.strptime(inv_date[:10], '%Y-%m-%d').strftime('%d-%b-%Y')
            except Exception:
                inv_date_str = inv_date
        elif hasattr(inv_date, 'strftime'):
            inv_date_str = inv_date.strftime('%d-%b-%Y')
        else:
            inv_date_str = datetime.now().strftime('%d-%b-%Y')

        inv_value = float(item.get('total_amount') or 0.0)
        taxable_value = float(item.get('taxable_amount') or inv_value)
        
        # Calculate effective rate if total > taxable
        if taxable_value > 0 and inv_value > taxable_value:
            tax_diff = inv_value - taxable_value
            rate = round((tax_diff / taxable_value) * 100)
            if rate not in [5, 12, 18, 28]:
                rate = 18
        else:
            rate = 18

        pos = "29-Karnataka"
        if gstin and len(gstin) >= 2 and gstin[:2].isdigit():
            state_code = gstin[:2]
            state_map = {
                '29': '29-Karnataka',
                '33': '33-Tamil Nadu',
                '36': '36-Telangana',
                '37': '37-Andhra Pradesh',
                '32': '32-Kerala',
                '27': '27-Maharashtra',
            }
            pos = state_map.get(state_code, f"{state_code}-Other")

        row = [
            safe_xlsx_value(gstin),
            safe_xlsx_value(party_name),
            safe_xlsx_value(inv_no),
            safe_xlsx_value(inv_date_str),
            round(inv_value, 2),
            safe_xlsx_value(pos),
            "N",
            "",
            "Regular",
            "",
            rate,
            round(taxable_value, 2),
            0.0
        ]
        ws.append(row)

    # Style data rows and auto-fit columns
    data_font = Font(name="Calibri", size=9)
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=len(headers)):
        for cell in row:
            cell.font = data_font
            cell.border = thin_border
            if isinstance(cell.value, (int, float)):
                cell.alignment = Alignment(horizontal="right")
            else:
                cell.alignment = Alignment(horizontal="left")

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def convert_uploaded_excel_to_gst_format(uploaded_file) -> io.BytesIO:
    """
    Takes an uploaded Excel spreadsheet (e.g. from Tally, vendor report, or manual sheet)
    and maps/converts it into the official GST Portal compliant Excel format.
    """
    in_wb = openpyxl.load_workbook(uploaded_file, data_only=True)
    in_ws = in_wb.active

    extracted_rows = []
    header_map = {}

    # Inspect first 5 rows to locate headers
    header_row_idx = None
    for r_idx, row in enumerate(in_ws.iter_rows(values_only=True), start=1):
        if r_idx > 6:
            break
        row_str = " ".join([str(v).lower() for v in row if v is not None])
        if any(keyword in row_str for keyword in ['invoice', 'bill', 'party', 'supplier', 'gstin', 'amount', 'taxable']):
            header_row_idx = r_idx
            for c_idx, val in enumerate(row):
                if val:
                    header_map[str(val).strip().lower()] = c_idx
            break

    if header_row_idx is None:
        header_row_idx = 1
        for c_idx, val in enumerate(in_ws.iter_rows(values_only=True).__next__()):
            if val:
                header_map[str(val).strip().lower()] = c_idx

    def get_val(row_vals, aliases):
        for alias in aliases:
            for k, col_idx in header_map.items():
                if alias in k and col_idx < len(row_vals):
                    val = row_vals[col_idx]
                    if val is not None and str(val).strip() != '':
                        return val
        return None

    # Read data rows
    for r_idx, row_vals in enumerate(in_ws.iter_rows(values_only=True), start=1):
        if r_idx <= header_row_idx:
            continue
        if not any(row_vals):
            continue

        gstin = get_val(row_vals, ['gstin', 'gst no', 'gst number', 'tax id']) or ''
        party = get_val(row_vals, ['party', 'supplier', 'vendor', 'name', 'particulars']) or 'Supplier'
        inv_no = get_val(row_vals, ['inv no', 'invoice no', 'bill no', 'voucher no', 'reference']) or f"INV-{r_idx}"
        inv_date = get_val(row_vals, ['date', 'inv date', 'bill date']) or datetime.now()
        total_val = get_val(row_vals, ['total', 'gross', 'invoice value', 'bill amount', 'net amount']) or 0.0
        taxable_val = get_val(row_vals, ['taxable', 'basic', 'assessable']) or total_val

        rate_val = get_val(row_vals, ['rate', 'tax rate', '%'])

        try:
            total_float = float(str(total_val).replace(',', '').strip())
        except Exception:
            total_float = 0.0

        try:
            taxable_float = float(str(taxable_val).replace(',', '').strip())
        except Exception:
            taxable_float = total_float

        clean_gstin = str(gstin or '').strip().upper()
        clean_party = str(party or '').strip()

        # Enrich using official Balaji vendor directory if available
        try:
            from .vendor_directory import find_vendor_by_gstin, find_vendor_by_name
            if clean_gstin and (not clean_party or clean_party == 'Supplier'):
                v_info = find_vendor_by_gstin(clean_gstin)
                if v_info:
                    clean_party = v_info['name']
            elif clean_party and not clean_gstin:
                v_match = find_vendor_by_name(clean_party)
                if v_match:
                    clean_gstin = v_match['gstin']
        except Exception:
            pass

        extracted_rows.append({
            'party_gstin': clean_gstin,
            'party_name': clean_party or 'Supplier',
            'invoice_number': str(inv_no).strip(),
            'invoice_date': inv_date,
            'total_amount': total_float,
            'taxable_amount': taxable_float,
        })

    return create_gst_portal_excel(extracted_rows)

