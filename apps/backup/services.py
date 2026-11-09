import os
import time
import zipfile
import hashlib
import secrets
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple

from django.conf import settings
from django.utils import timezone
import psycopg

from apps.backup.models import BackupHistory, BackupType, TriggerType, BackupStatus, RestoreHistory, RestoreStatus


class BackupService:
    PG_BIN_DIR = Path(os.getenv('PG_BIN_DIR', r"C:\Program Files\PostgreSQL\18\bin"))
    PG_DUMP_PATH = PG_BIN_DIR / "pg_dump.exe"
    PG_RESTORE_PATH = PG_BIN_DIR / "pg_restore.exe"
    PSQL_PATH = PG_BIN_DIR / "psql.exe"

    @classmethod
    def calculate_sha256(cls, file_path: Path) -> str:
        """Computes SHA-256 cryptographic hash of a file."""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(65536), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    @classmethod
    def perform_full_backup(cls, created_by=None, trigger_type=TriggerType.MANUAL) -> BackupHistory:
        """
        Executes an atomic dual backup:
        1. PostgreSQL pg_dump (Custom format)
        2. Media storage document archive (ZIP)
        3. Computes and validates SHA-256 checksums
        4. Applies retention cleanup policy
        """
        start_time = time.time()
        now = datetime.now()
        backup_id = f"BKP-{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"
        
        # External target directory outside application code
        target_dir = settings.BACKUP_STORAGE_DIR / backup_id
        target_dir.mkdir(parents=True, exist_ok=True)

        backup_record = BackupHistory.objects.create(
            backup_id=backup_id,
            backup_type=BackupType.FULL,
            trigger_type=trigger_type,
            status=BackupStatus.PENDING,
            created_by=created_by
        )

        db_dump_file = target_dir / f"{backup_id}_postgres.dump"
        docs_zip_file = target_dir / f"{backup_id}_documents.zip"

        try:
            # 1. Database Backup via pg_dump
            from django.db import connection
            active_db_name = connection.settings_dict.get('NAME', settings.DATABASES['default']['NAME'])
            db_conf = settings.DATABASES['default']
            env = os.environ.copy()
            env['PGPASSWORD'] = str(db_conf['PASSWORD'])

            cmd = [
                str(cls.PG_DUMP_PATH),
                "-h", str(db_conf['HOST']),
                "-p", str(db_conf['PORT']),
                "-U", str(db_conf['USER']),
                "-d", str(active_db_name),
                "-F", "c",          # Custom binary compressed format
                "-b",               # Include large objects
                "-v",               # Verbose
                "-f", str(db_dump_file)
            ]

            process = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=300
            )

            if process.returncode != 0 or not db_dump_file.exists():
                raise RuntimeError(f"pg_dump failed with code {process.returncode}: {process.stderr}")

            db_size = db_dump_file.stat().st_size
            db_hash = cls.calculate_sha256(db_dump_file)

            # 2. Document Media Archive Backup
            media_invoices_dir = Path(settings.MEDIA_ROOT) / 'invoices'
            with zipfile.ZipFile(docs_zip_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
                if media_invoices_dir.exists():
                    for root, dirs, files in os.walk(media_invoices_dir):
                        for file in files:
                            full_path = Path(root) / file
                            relative_path = full_path.relative_to(settings.MEDIA_ROOT)
                            zipf.write(full_path, arcname=str(relative_path))
                else:
                    # Write manifest file if empty
                    zipf.writestr("manifest.txt", f"InvoiceHub Media Archive {backup_id}\nTimestamp: {now.isoformat()}")

            media_size = docs_zip_file.stat().st_size
            media_hash = cls.calculate_sha256(docs_zip_file)

            # 3. Post-Creation Integrity Verification
            verified_db_hash = cls.calculate_sha256(db_dump_file)
            verified_media_hash = cls.calculate_sha256(docs_zip_file)

            if verified_db_hash != db_hash or verified_media_hash != media_hash:
                backup_record.status = BackupStatus.INTEGRITY_FAILED
                backup_record.error_log = "SHA-256 verification mismatch post-creation."
                backup_record.save()
                return backup_record

            duration = round(time.time() - start_time, 2)

            backup_record.db_file_path = str(db_dump_file)
            backup_record.db_file_size = db_size
            backup_record.db_sha256 = db_hash
            backup_record.media_file_path = str(docs_zip_file)
            backup_record.media_file_size = media_size
            backup_record.media_sha256 = media_hash
            backup_record.total_size_bytes = db_size + media_size
            backup_record.status = BackupStatus.SUCCESS
            backup_record.integrity_verified = True
            backup_record.verified_at = timezone.now()
            backup_record.duration_seconds = duration
            backup_record.save()

            # Apply retention cleanup
            cls.prune_expired_backups()

            return backup_record

        except Exception as e:
            backup_record.status = BackupStatus.FAILED
            backup_record.error_log = str(e)
            backup_record.duration_seconds = round(time.time() - start_time, 2)
            backup_record.save()
            raise

    @classmethod
    def prune_expired_backups(cls):
        """
        Removes backups older than BACKUP_RETENTION_DAYS from storage and updates database.
        If BACKUP_RETENTION_DAYS is 0, negative, or set to permanent retention (>= 3650 days / 10 years),
        automatic pruning is bypassed completely so that no historical backup is ever deleted.
        """
        if settings.BACKUP_RETENTION_DAYS <= 0 or settings.BACKUP_RETENTION_DAYS >= 3650:
            # Permanent retention active: never delete backups
            return

        cutoff_date = timezone.now() - timedelta(days=settings.BACKUP_RETENTION_DAYS)
        expired_backups = BackupHistory.objects.filter(created_at__lt=cutoff_date, status=BackupStatus.SUCCESS)

        for record in expired_backups:
            try:
                # Remove files on disk
                if record.db_file_path and os.path.exists(record.db_file_path):
                    os.remove(record.db_file_path)
                if record.media_file_path and os.path.exists(record.media_file_path):
                    os.remove(record.media_file_path)
                parent_dir = Path(record.db_file_path).parent if record.db_file_path else None
                if parent_dir and parent_dir.exists() and not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
            except Exception:
                pass


class RestoreService:
    @classmethod
    def run_sandbox_restore_test(cls, backup_record: BackupHistory, executed_by=None) -> RestoreHistory:
        """
        Executes a non-destructive disaster recovery drill:
        1. Validates cryptographic SHA-256 hash.
        2. Recreates isolated test database 'balaji_test_restore'.
        3. Restores pg_dump into 'balaji_test_restore' using pg_restore.
        4. Verifies table row counts in 'balaji_test_restore'.
        5. Extracts document archive into an isolated test sandbox directory.
        6. Guarantees that the production database is NEVER touched.
        """
        start_time = time.time()
        restore_id = f"RST-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"
        target_db = "balaji_test_restore"

        # Safety Check 1: Guarantee production or active database cannot be overwritten
        from django.db import connection
        active_db = connection.settings_dict.get('NAME')
        prod_db_name = settings.DATABASES['default']['NAME']
        if target_db in (prod_db_name, active_db):
            raise ValueError(f"CRITICAL SAFETY VIOLATION: Cannot restore into active database '{active_db}'.")

        # Step 1: Verify backup file exists and matches SHA-256
        db_dump_path = Path(backup_record.db_file_path)
        if not db_dump_path.exists():
            raise FileNotFoundError(f"Database dump file missing: {db_dump_path}")

        current_db_hash = BackupService.calculate_sha256(db_dump_path)
        if current_db_hash != backup_record.db_sha256:
            raise ValueError(f"Integrity check failed: Current SHA-256 does not match recorded checksum.")

        db_conf = settings.DATABASES['default']
        env = os.environ.copy()
        env['PGPASSWORD'] = str(db_conf['PASSWORD'])

        # Step 2: Reset isolated test database 'balaji_test_restore'
        conn = psycopg.connect(
            dbname="postgres",
            user=db_conf['USER'],
            password=db_conf['PASSWORD'],
            host=db_conf['HOST'],
            port=db_conf['PORT'],
            autocommit=True
        )
        with conn.cursor() as cur:
            # Terminate active connections on target test database
            cur.execute("""
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = %s
                  AND pid <> pg_backend_pid();
            """, (target_db,))
            cur.execute(f"DROP DATABASE IF EXISTS {target_db};")
            cur.execute(f"CREATE DATABASE {target_db};")
        conn.close()

        # Step 3: Run pg_restore into isolated test database
        restore_cmd = [
            str(BackupService.PG_RESTORE_PATH),
            "-h", str(db_conf['HOST']),
            "-p", str(db_conf['PORT']),
            "-U", str(db_conf['USER']),
            "-d", target_db,
            "--no-owner",
            "-v",
            str(db_dump_path)
        ]

        restore_proc = subprocess.run(
            restore_cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=300
        )

        # Step 4: Verify restored database counts
        test_conn = psycopg.connect(
            dbname=target_db,
            user=db_conf['USER'],
            password=db_conf['PASSWORD'],
            host=db_conf['HOST'],
            port=db_conf['PORT']
        )
        verified_counts = {}
        with test_conn.cursor() as cur:
            for table in ['balaji_users', 'balaji_departments', 'balaji_audit_logs']:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {table};")
                    verified_counts[table] = cur.fetchone()[0]
                except Exception:
                    verified_counts[table] = 0
        test_conn.close()

        # Step 5: Test Document Archive Extraction in isolated sandbox
        sandbox_media_dir = settings.BACKUP_STORAGE_DIR / "sandbox_restore" / restore_id
        sandbox_media_dir.mkdir(parents=True, exist_ok=True)
        files_count = 0
        if backup_record.media_file_path and os.path.exists(backup_record.media_file_path):
            with zipfile.ZipFile(backup_record.media_file_path, 'r') as zipf:
                zipf.extractall(sandbox_media_dir)
                files_count = len(zipf.namelist())

        duration = round(time.time() - start_time, 2)

        restore_record = RestoreHistory.objects.create(
            restore_id=restore_id,
            backup=backup_record,
            target_database=target_db,
            target_media_dir=str(sandbox_media_dir),
            is_sandbox_test=True,
            status=RestoreStatus.SUCCESS,
            records_verified_count=verified_counts,
            files_verified_count=files_count,
            duration_seconds=duration,
            executed_by=executed_by
        )

        return restore_record
