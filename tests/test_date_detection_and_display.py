import pytest
from datetime import date, datetime
from decimal import Decimal
from django.urls import reverse
from apps.accounts.models import User, UserRole
from apps.invoices.models import InvoiceSubmission, InvoiceDocument
from apps.invoices.reader import parse_invoice_date_from_text
from apps.invoices.views import get_document_highlight_boxes


class TestDateDetectionAndDisplay:
    """Test suite verifying invoice date extraction and UI display."""

    def test_01_parse_invoice_date_formats(self):
        """Verifies date extraction across various Indian invoice formats and patterns."""
        # 1. Transport permit with time stamp (as in user screenshot)
        text1 = "License Number: 6718023 Date/Time: 14-Aug-2026 12:11 PM Permit: AP202612110118"
        assert parse_invoice_date_from_text(text1) == date(2026, 8, 14)

        # 2. Prefixed numeric with slashes
        text2 = "Tax Invoice Date: 25/11/2026 Total: 15000"
        assert parse_invoice_date_from_text(text2) == date(2026, 11, 25)

        # 3. Prefixed numeric with dashes
        text3 = "Bill Date: 05-09-2026 Amount: 4200"
        assert parse_invoice_date_from_text(text3) == date(2026, 9, 5)

        # 4. Prefixed with dots
        text4 = "Dated : 18.07.2026 Karnataka Tyre Care"
        assert parse_invoice_date_from_text(text4) == date(2026, 7, 18)

        # 5. Prefixed with full month name
        text5 = "Invoice Date: 15 October 2026"
        assert parse_invoice_date_from_text(text5) == date(2026, 10, 15)

        # 6. Prefixed Month Day, Year
        text6 = "Dt: August 14, 2026"
        assert parse_invoice_date_from_text(text6) == date(2026, 8, 14)

        # 7. ISO format
        text7 = "Date of Issue: 2026-08-14"
        assert parse_invoice_date_from_text(text7) == date(2026, 8, 14)

        # 8. Standalone date in body text
        text8 = "Consignment delivered on 14-Aug-2026 successfully"
        assert parse_invoice_date_from_text(text8) == date(2026, 8, 14)

    @pytest.mark.django_db
    def test_02_highlight_boxes_removed_and_empty(self):
        """Verifies that get_document_highlight_boxes returns empty list since highlights are disabled."""
        emp = User.objects.create_user(username='emp_test', password='password123', role=UserRole.EMPLOYEE)
        sub = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-99001',
            employee=emp,
            party_name='TEST VENDOR LTD',
            party_gstin='29AAICB6937G2ZA',
            invoice_number='INV-99001',
            invoice_date=date(2026, 8, 14),
            total_amount=Decimal('70802.68'),
            taxable_amount=Decimal('60000.00'),
            cgst_amount=Decimal('5401.34'),
            sgst_amount=Decimal('5401.34'),
        )

        boxes = get_document_highlight_boxes(sub)
        assert boxes == []

    @pytest.mark.django_db
    def test_03_naveen_dashboard_displays_bill_date(self, client):
        """Verifies that Naveen's dashboard tables render the detected Bill Date with calendar icon."""
        naveen = User.objects.create_user(username='naveen_test', password='password123', role=UserRole.NAVEEN)
        emp = User.objects.create_user(username='emp_test2', password='password123', role=UserRole.EMPLOYEE)
        sub = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-99002',
            employee=emp,
            party_name='BALAJI MOVERS AND SIDING',
            party_gstin='29AAICB6937G2ZA',
            invoice_number='943',
            invoice_date=date(2026, 8, 14),
            total_amount=Decimal('70802.68'),
        )

        client.force_login(naveen)
        res = client.get(reverse('core:naveen_dashboard'))
        assert res.status_code == 200
        content = res.content.decode('utf-8')
        assert '14 Aug 2026' in content
        assert 'bi-calendar-event' in content

    @pytest.mark.django_db
    def test_04_submission_detail_displays_bill_date(self, client):
        """Verifies that submission detail displays the detected Bill Date in the accounting card."""
        naveen = User.objects.create_user(username='naveen_test2', password='password123', role=UserRole.NAVEEN)
        emp = User.objects.create_user(username='emp_test3', password='password123', role=UserRole.EMPLOYEE)
        sub = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-99003',
            employee=emp,
            party_name='BALAJI MOVERS AND SIDING',
            party_gstin='29AAICB6937G2ZA',
            invoice_number='943',
            invoice_date=date(2026, 8, 14),
            total_amount=Decimal('70802.68'),
        )

        client.force_login(naveen)
        res = client.get(reverse('invoices:submission_detail', args=[sub.submission_id]))
        assert res.status_code == 200
        content = res.content.decode('utf-8')
        assert '14 Aug 2026' in content
        assert 'Bill Date' in content

    def test_05_unclear_written_invoice_returns_none(self):
        """Verifies that unclear handwritten text or random numbers do not falsely match as dates."""
        # Random dimension, cell number, item code without date label
        unclear_text1 = "Tyre Puncture 10.00-20 Qty 2 Amount 790 Cell 9845123456"
        assert parse_invoice_date_from_text(unclear_text1) is None

        unclear_text2 = "Sl No: 2314 KA-06-B-4091 Total Rs 1062"
        assert parse_invoice_date_from_text(unclear_text2) is None

    @pytest.mark.django_db
    def test_06_unclear_date_displays_na_on_ui(self, client):
        """Verifies that submissions with unclear/unreadable date display N/A across UI views."""
        naveen = User.objects.create_user(username='naveen_na_test', password='password123', role=UserRole.NAVEEN)
        emp = User.objects.create_user(username='emp_na_test', password='password123', role=UserRole.EMPLOYEE)
        sub = InvoiceSubmission.objects.create(
            submission_id='BTI-202611-99004',
            employee=emp,
            party_name='HANDWRITTEN TYRE SHOP',
            invoice_number='2314',
            invoice_date=None,  # Unclear date on paper
            total_amount=Decimal('1062.00'),
        )

        client.force_login(naveen)

        # 1. Submission Detail displays N/A under Bill Date
        res_detail = client.get(reverse('invoices:submission_detail', args=[sub.submission_id]))
        assert res_detail.status_code == 200
        content_detail = res_detail.content.decode('utf-8')
        assert 'N/A' in content_detail

        # 2. Naveen Dashboard displays N/A in the date column
        res_dash = client.get(reverse('core:naveen_dashboard'))
        assert res_dash.status_code == 200
        content_dash = res_dash.content.decode('utf-8')
        assert 'N/A' in content_dash
