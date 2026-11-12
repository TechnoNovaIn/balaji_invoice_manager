"""
Spreadsheet (CSV / XLSX) formula-injection sanitization utilities.
Protects Excel / LibreOffice / Google Sheets exports from DDE & formula execution
while keeping internally generated numeric values and app-generated summary formulas intact.
"""
from datetime import date, datetime
from decimal import Decimal
import re

FORMULA_DANGER_CHARS = ('=', '+', '-', '@', '\t', '\r')
_STRICT_NEGATIVE_NUMBER_RE = re.compile(r'^-\d+(?:\.\d+)?$')


def sanitize_spreadsheet_text(value, allow_negative_number: bool = True) -> str:
    """
    Neutralize untrusted text that could be evaluated as a spreadsheet formula.
    Prefixes a single quote when the string begins with dangerous formula triggers
    (=, +, -, @, tab, CR) — including after leading whitespace.
    """
    if value is None:
        return ''
    text = str(value)
    if not text:
        return ''

    stripped = text.lstrip()
    if not stripped:
        return text

    if allow_negative_number and _STRICT_NEGATIVE_NUMBER_RE.match(stripped):
        return text

    if text[0] in FORMULA_DANGER_CHARS or stripped[0] in FORMULA_DANGER_CHARS:
        return "'" + text
    return text


def safe_xlsx_value(value):
    """
    Sanitize an untrusted cell value for openpyxl while preserving numeric, boolean,
    and date/datetime types cleanly. Do NOT wrap trusted internal formulas like '=SUM(...)'.
    """
    if value is None:
        return ''
    if isinstance(value, (bool, int, float, Decimal, date, datetime)):
        return value
    return sanitize_spreadsheet_text(value, allow_negative_number=True)

