from django.core.management.base import BaseCommand
from django.conf import settings
import os
from pathlib import Path


class Command(BaseCommand):
    help = "Tests the Gemini Vision AI API connection and verifies invoice extraction."

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            type=str,
            default='',
            help='Path to an invoice image/PDF to test. Defaults to recent uploaded bill.'
        )

    def handle(self, *args, **options):
        api_key = getattr(settings, 'GEMINI_API_KEY', '') or os.getenv('GEMINI_API_KEY', '').strip()
        
        self.stdout.write(self.style.MIGRATE_HEADING("=== Gemini Vision AI Diagnostics ==="))

        if not api_key:
            self.stdout.write(self.style.WARNING("⚠️  No GEMINI_API_KEY configured in your .env file."))
            self.stdout.write("To get your 100% Free Gemini API key (takes 30 seconds):")
            self.stdout.write("1. Open https://aistudio.google.com/app/apikey in your browser.")
            self.stdout.write("2. Sign in with your Google account and click 'Create API key'.")
            self.stdout.write("3. Paste the key into d:\\balaji_invoice_manager\\.env as:")
            self.stdout.write("   GEMINI_API_KEY=AIzaSy...\n")
            self.stdout.write(self.style.SUCCESS("✓ RapidOCR local fallback is currently active and protecting operations."))
            return

        self.stdout.write(self.style.SUCCESS(f"✓ GEMINI_API_KEY detected: {api_key[:6]}...{api_key[-4:]}"))

        test_file = options['file']
        if not test_file:
            fixture_sample = Path(settings.BASE_DIR) / 'tests' / 'fixtures' / 'sample_invoice.jpg'
            default_sample = Path(settings.MEDIA_ROOT) / 'invoices' / 'originals' / '2026' / '11' / '0ecad71652164f9c9221fc954c068aa2.jpg'
            if fixture_sample.exists():
                test_file = str(fixture_sample)
            elif default_sample.exists():
                test_file = str(default_sample)

        if not test_file or not os.path.exists(test_file):
            self.stdout.write(self.style.WARNING("No sample image found to test live extraction."))
            self.stdout.write("Provide an image via: python manage.py test_gemini --file <path_to_invoice.jpg>")
            return

        self.stdout.write(f"Testing extraction on: {test_file}")
        self.stdout.write("Connecting to Gemini Vision AI...")

        from apps.invoices.gemini_extractor import extract_with_gemini
        result = extract_with_gemini(test_file)

        if not result:
            self.stdout.write(self.style.ERROR("❌ Extraction failed. Please verify your GEMINI_API_KEY and internet connection."))
            return

        self.stdout.write(self.style.SUCCESS("\n🎉 Gemini Vision Extraction Successful!"))
        self.stdout.write(f" - Vendor Name   : {result['party_name']}")
        self.stdout.write(f" - Vendor GSTIN  : {result['party_gstin']}")
        self.stdout.write(f" - Invoice Number: {result['invoice_number']}")
        self.stdout.write(f" - Invoice Date  : {result['invoice_date']}")
        self.stdout.write(f" - Total Amount  : ₹{result['total_amount']}")
        self.stdout.write(f" - Expense Ledger: {result['expense_ledger']}")
        self.stdout.write(f" - Confidence    : {result['confidence'] * 100}%")

