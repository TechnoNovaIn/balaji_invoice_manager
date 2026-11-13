import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings

from apps.invoices.db_utils import is_sqlite_lock_error, run_with_sqlite_retry
from apps.invoices.models import InvoiceSequence, InvoiceSubmission, UploadedDocument
from apps.invoices.tasks import (
    _acquire_worker_lock,
    _has_live_external_worker,
    _release_worker_lock,
    _worker_lock_dir,
    process_uploaded_document_task,
    recover_incomplete_documents,
)

User = get_user_model()


class Phase1SQLiteRetryAndLockTests(TestCase):
    def test_is_sqlite_lock_error_and_retry_recovers_full_transaction(self):
        self.assertTrue(is_sqlite_lock_error(OperationalError("database is locked")))
        self.assertTrue(is_sqlite_lock_error(OperationalError("SQLite busy")))
        self.assertFalse(is_sqlite_lock_error(OperationalError("no such table: foo")))
        self.assertFalse(is_sqlite_lock_error(ValueError("database is locked")))

        attempts = {"count": 0}

        def flaky_tx():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise OperationalError("database is locked")
            return "committed"

        # Simulate non-nested transaction context so retry wrapper exercises backoff loop
        with patch("apps.invoices.db_utils.connection") as mock_conn, patch("apps.invoices.db_utils.time.sleep") as mock_sleep:
            mock_conn.in_atomic_block = False
            result = run_with_sqlite_retry(flaky_tx, max_attempts=4, base_delay=0.01, max_delay=0.05)

        self.assertEqual(result, "committed")
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_atomic_lock_file_creation_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):
                doc_id = 91001
                self.assertTrue(_acquire_worker_lock(doc_id))
                lock_file = _worker_lock_dir() / f"doc_{doc_id}.lock"
                self.assertTrue(lock_file.exists())
                payload = json.loads(lock_file.read_text(encoding="utf-8"))
                self.assertEqual(payload["pid"], os.getpid())
                self.assertEqual(payload["doc_id"], doc_id)

                # Another process owning the lock cannot be stolen while alive
                other_payload = {
                    "pid": os.getpid() + 999999,
                    "process_create_time": 123456789,
                    "doc_id": doc_id,
                }
                lock_file.write_text(json.dumps(other_payload), encoding="utf-8")
                with patch("apps.invoices.tasks._is_pid_alive", return_value=True):
                    self.assertFalse(_acquire_worker_lock(doc_id))
                    self.assertTrue(_has_live_external_worker(doc_id))

                # _release_worker_lock must NOT delete a lock owned by a different PID
                _release_worker_lock(doc_id)
                self.assertTrue(lock_file.exists())

                # Restore own PID and release cleanly
                other_payload["pid"] = os.getpid()
                lock_file.write_text(json.dumps(other_payload), encoding="utf-8")
                _release_worker_lock(doc_id)
                self.assertFalse(lock_file.exists())

    def test_stale_lock_recovery_dead_pid_pid_reuse_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):
                doc_id = 91002
                lock_file = _worker_lock_dir() / f"doc_{doc_id}.lock"

                # 1. Dead PID lock is cleaned up and re-acquired
                lock_file.write_text(json.dumps({"pid": 99999999, "doc_id": doc_id}), encoding="utf-8")
                with patch("apps.invoices.tasks._is_pid_alive", return_value=False):
                    self.assertFalse(_has_live_external_worker(doc_id))
                    self.assertFalse(lock_file.exists())
                    self.assertTrue(_acquire_worker_lock(doc_id))
                _release_worker_lock(doc_id)

                # 2. PID reuse (process_create_time mismatch) is treated as stale
                lock_file.write_text(
                    json.dumps({"pid": 12345, "process_create_time": 111111, "doc_id": doc_id}),
                    encoding="utf-8",
                )
                with patch("apps.invoices.tasks._get_process_start_token", return_value=222222):
                    if os.name == "nt":
                        # On Windows, creation time mismatch must treat lock as stale
                        with patch("apps.invoices.tasks.os.name", "nt"):
                            self.assertTrue(_acquire_worker_lock(doc_id))
                        _release_worker_lock(doc_id)

                # 3. Old corrupt lock file (>5s old) is cleaned up safely
                lock_file.write_text("{corrupt-json", encoding="utf-8")
                old_ts = time.time() - 10.0
                os.utime(lock_file, (old_ts, old_ts))
                self.assertFalse(_has_live_external_worker(doc_id))
                self.assertTrue(_acquire_worker_lock(doc_id))
                _release_worker_lock(doc_id)

    def test_windows_service_script_has_no_postgres_dependency_or_duplicate_startup_task(self):
        ps1_path = Path(__file__).resolve().parent.parent / "scripts" / "windows" / "install_windows_services.ps1"
        content = ps1_path.read_text(encoding="utf-8")
        self.assertNotIn("DependOnService postgresql-x64-18", content)
        self.assertNotIn("Register-ScheduledTask -TaskName \"BalajiInvoiceHub-StartupOcrRecovery\"", content)
        self.assertIn("Unregister-ScheduledTask -TaskName \"BalajiInvoiceHub-StartupOcrRecovery\"", content)
        self.assertIn("AppRotateFiles", content)
        self.assertIn("SERVICE_DELAYED_AUTO_START", content)
        self.assertIn("Wait-ForServiceStopped", content)
        self.assertIn("Wait-ForServiceRemoved", content)


class Phase1SQLiteConcurrencyAndWorkerTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="phase1_submitter",
            password="StrongPass123!@#",
            role="EMPLOYEE",
        )

    def test_simultaneous_invoice_sequence_generation_no_duplicates(self):
        def worker(_idx):
            close_old_connections()
            try:
                return InvoiceSequence.get_next_submission_id()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(worker, range(24)))

        self.assertEqual(len(ids), 24)
        self.assertEqual(len(set(ids)), 24)

    def test_two_workers_claiming_same_document_only_one_processes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):
                import pymupdf as fitz

                pdf_doc = fitz.open()
                page = pdf_doc.new_page()
                page.insert_text((72, 72), "TAX INVOICE\nKARNATAKA TYRE CARE CENTRE\nInvoice No: 5501\nTotal: 1180.00")
                pdf_bytes = pdf_doc.tobytes()
                pdf_doc.close()

                up = UploadedDocument.objects.create(
                    uploader=self.user,
                    original_file=SimpleUploadedFile("concurrent.pdf", pdf_bytes, content_type="application/pdf"),
                    file_name="concurrent.pdf",
                    file_sha256="a" * 64,
                    file_size_bytes=len(pdf_bytes),
                    content_type="application/pdf",
                    processing_status=UploadedDocument.STATUS_QUEUED,
                )

                barrier = threading.Barrier(2)
                results = []
                errors = []

                def claim_and_run():
                    close_old_connections()
                    try:
                        barrier.wait(timeout=5)
                        res = process_uploaded_document_task(up.id)
                        results.append(res)
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        close_old_connections()

                t1 = threading.Thread(target=claim_and_run)
                t2 = threading.Thread(target=claim_and_run)
                t1.start()
                t2.start()
                t1.join(timeout=30)
                t2.join(timeout=30)

                self.assertEqual(errors, [])
                up.refresh_from_db()
                self.assertEqual(up.processing_status, UploadedDocument.STATUS_DETECTION_COMPLETED)
                self.assertEqual(InvoiceSubmission.objects.filter(source_document=up).count(), 1)
                non_empty_runs = [r for r in results if r]
                self.assertEqual(len(non_empty_runs), 1)

    def test_worker_failure_marks_failed_and_releases_lock_and_recovery_resumes_orphaned_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(MEDIA_ROOT=tmpdir):
                # 1. Missing file causes FAILED status and lock release
                missing_up = UploadedDocument.objects.create(
                    uploader=self.user,
                    file_name="missing.pdf",
                    file_sha256="b" * 64,
                    file_size_bytes=100,
                    content_type="application/pdf",
                    processing_status=UploadedDocument.STATUS_QUEUED,
                )
                with self.assertRaises(FileNotFoundError):
                    process_uploaded_document_task(missing_up.id)

                missing_up.refresh_from_db()
                self.assertEqual(missing_up.processing_status, UploadedDocument.STATUS_FAILED)
                self.assertFalse((_worker_lock_dir() / f"doc_{missing_up.id}.lock").exists())

                # 2. Orphaned PROCESSING job with stale lock recovers cleanly
                import pymupdf as fitz

                pdf_doc = fitz.open()
                page = pdf_doc.new_page()
                page.insert_text((72, 72), "TAX INVOICE\nTest Vendor\nInvoice No: 9002\nTotal: 500.00")
                pdf_bytes = pdf_doc.tobytes()
                pdf_doc.close()

                orphan = UploadedDocument.objects.create(
                    uploader=self.user,
                    original_file=SimpleUploadedFile("orphan.pdf", pdf_bytes, content_type="application/pdf"),
                    file_name="orphan.pdf",
                    file_sha256="c" * 64,
                    file_size_bytes=len(pdf_bytes),
                    content_type="application/pdf",
                    processing_status=UploadedDocument.STATUS_PROCESSING,
                )
                stale_lock = _worker_lock_dir() / f"doc_{orphan.id}.lock"
                stale_lock.write_text(json.dumps({"pid": 99999999, "doc_id": orphan.id}), encoding="utf-8")

                with patch("apps.invoices.tasks._is_pid_alive", return_value=False):
                    summary = recover_incomplete_documents(stale_minutes=0, requeue_only=True)

                self.assertIn(orphan.id, summary["recovered_ids"])
                orphan.refresh_from_db()
                self.assertEqual(orphan.processing_status, UploadedDocument.STATUS_QUEUED)
                self.assertFalse(stale_lock.exists())

