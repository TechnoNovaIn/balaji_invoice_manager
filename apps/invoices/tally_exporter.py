import xml.etree.ElementTree as ET
from xml.dom import minidom
from django.utils import timezone
from .models import InvoiceSubmission


def generate_tally_xml(submissions) -> str:
    """
    Generate standard TallyPrime & Tally.ERP 9 compatible XML for importing
    Purchase & Expense vouchers directly into Tally.
    """
    envelope = ET.Element('ENVELOPE')

    # Header
    header = ET.SubElement(envelope, 'HEADER')
    tally_req = ET.SubElement(header, 'TALLYREQUEST')
    tally_req.text = 'Import Data'

    # Body
    body = ET.SubElement(envelope, 'BODY')
    import_data = ET.SubElement(body, 'IMPORTDATA')

    req_desc = ET.SubElement(import_data, 'REQUESTDESC')
    report_name = ET.SubElement(req_desc, 'REPORTNAME')
    report_name.text = 'All Accounting Vouchers'

    static_vars = ET.SubElement(req_desc, 'STATICVARIABLES')
    sv_company = ET.SubElement(static_vars, 'SVCURRENTCOMPANY')
    sv_company.text = 'Balaji Transports'

    req_data = ET.SubElement(import_data, 'REQUESTDATA')

    for sub in submissions:
        tally_msg = ET.SubElement(req_data, 'TALLYMESSAGE', {'xmlns:UDF': 'TallyUDF'})
        voucher = ET.SubElement(
            tally_msg,
            'VOUCHER',
            {'VCHTYPE': 'Purchase', 'ACTION': 'Create', 'OBJVIEW': 'Accounting Voucher View'}
        )

        v_date = sub.invoice_date or sub.created_at.date()
        date_str = v_date.strftime('%Y%m%d')

        ET.SubElement(voucher, 'DATE').text = date_str
        ET.SubElement(voucher, 'EFFECTIVEDATE').text = date_str
        ET.SubElement(voucher, 'VOUCHERTYPENAME').text = 'Purchase'
        ET.SubElement(voucher, 'VOUCHERNUMBER').text = sub.tally_voucher_no or sub.invoice_number or sub.submission_id
        ET.SubElement(voucher, 'REFERENCE').text = sub.invoice_number or sub.submission_id

        party_name = sub.party_name or 'Cash / Unregistered Supplier'
        ET.SubElement(voucher, 'PARTYLEDGERNAME').text = party_name

        narration = f"Balaji InvoiceHub {sub.submission_id} | Employee: {sub.employee.get_full_name() or sub.employee.username}"
        if sub.party_gstin:
            narration += f" | GSTIN: {sub.party_gstin}"
        ET.SubElement(voucher, 'NARRATION').text = narration

        # Total amount credited to Supplier (Party Ledger - Credit)
        total_val = float(sub.total_amount or 0.0)
        taxable_val = float(sub.taxable_amount or total_val)
        cgst_val = float(sub.cgst_amount or 0.0)
        sgst_val = float(sub.sgst_amount or 0.0)
        igst_val = float(sub.igst_amount or 0.0)

        # 1. Party Ledger (Credit = negative in Tally XML)
        party_entry = ET.SubElement(voucher, 'ALLLEDGERENTRIES.LIST')
        ET.SubElement(party_entry, 'LEDGERNAME').text = party_name
        ET.SubElement(party_entry, 'ISDEEMEDPOSITIVE').text = 'No'
        ET.SubElement(party_entry, 'AMOUNT').text = f"{total_val:.2f}"

        # 2. Expense Ledger (Debit = positive in Tally XML)
        expense_ledger_name = sub.expense_ledger or 'Diesel Expenses'
        expense_entry = ET.SubElement(voucher, 'ALLLEDGERENTRIES.LIST')
        ET.SubElement(expense_entry, 'LEDGERNAME').text = expense_ledger_name
        ET.SubElement(expense_entry, 'ISDEEMEDPOSITIVE').text = 'Yes'
        ET.SubElement(expense_entry, 'AMOUNT').text = f"-{taxable_val:.2f}"

        # 3. CGST Input Ledger (if applicable)
        if cgst_val > 0:
            cgst_entry = ET.SubElement(voucher, 'ALLLEDGERENTRIES.LIST')
            ET.SubElement(cgst_entry, 'LEDGERNAME').text = 'Input CGST'
            ET.SubElement(cgst_entry, 'ISDEEMEDPOSITIVE').text = 'Yes'
            ET.SubElement(cgst_entry, 'AMOUNT').text = f"-{cgst_val:.2f}"

        # 4. SGST Input Ledger (if applicable)
        if sgst_val > 0:
            sgst_entry = ET.SubElement(voucher, 'ALLLEDGERENTRIES.LIST')
            ET.SubElement(sgst_entry, 'LEDGERNAME').text = 'Input SGST'
            ET.SubElement(sgst_entry, 'ISDEEMEDPOSITIVE').text = 'Yes'
            ET.SubElement(sgst_entry, 'AMOUNT').text = f"-{sgst_val:.2f}"

        # 5. IGST Input Ledger (if applicable)
        if igst_val > 0:
            igst_entry = ET.SubElement(voucher, 'ALLLEDGERENTRIES.LIST')
            ET.SubElement(igst_entry, 'LEDGERNAME').text = 'Input IGST'
            ET.SubElement(igst_entry, 'ISDEEMEDPOSITIVE').text = 'Yes'
            ET.SubElement(igst_entry, 'AMOUNT').text = f"-{igst_val:.2f}"

    rough_xml = ET.tostring(envelope, 'utf-8')
    reparsed = minidom.parseString(rough_xml)
    return reparsed.toprettyxml(indent="  ")

