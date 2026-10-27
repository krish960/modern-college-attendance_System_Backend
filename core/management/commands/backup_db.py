import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def fernet():
    key = settings.BACKUP_ENCRYPTION_KEY
    if not key:
        raise CommandError("Set BACKUP_ENCRYPTION_KEY in .env first. Create one with:\n"
                           '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise CommandError("BACKUP_ENCRYPTION_KEY is not a valid key (it must come from Fernet.generate_key()).")


class Command(BaseCommand):
    help = "Make an encrypted copy of the database (safe while the site is running). Keeps the newest --keep files."

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=int, default=14, help="How many backups to keep (default 14).")

    def handle(self, *args, **opts):
        box = fernet()
        out_dir = Path(settings.BACKUP_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_dir.chmod(0o700)
        db = settings.DATABASES["default"]
        # SQLite: copy the file. PostgreSQL: pg_dump in its custom format. Both are then encrypted.
        if db["ENGINE"].endswith("postgresql"):
            data, ext = self.dump_postgres(db), "pgdump"
        else:
            data, ext = self.copy_sqlite(Path(db["NAME"])), "sqlite3"
        data = box.encrypt(data)
        target = out_dir / f"attendance-{datetime.now():%Y%m%d-%H%M%S}.{ext}.enc"
        target.write_bytes(data)
        target.chmod(0o600)
        # Keep only the newest --keep files of this kind.
        old = sorted(out_dir.glob(f"attendance-*.{ext}.enc"))[:-max(opts["keep"], 1)]
        for f in old:
            f.unlink()
        self.stdout.write(self.style.SUCCESS(f"Encrypted backup written: {target} ({len(data) // 1024} KB). Removed {len(old)} old backup(s)."))

    def copy_sqlite(self, source):
        """Consistent snapshot of the SQLite file, even while people are saving."""
        if not source.exists():
            raise CommandError("Database file not found.")
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "copy.sqlite3"
            src, dst = sqlite3.connect(source), sqlite3.connect(copy)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            return copy.read_bytes()

    def dump_postgres(self, db):
        """Run pg_dump (must be installed on the server). The password goes in the environment, not on the command line."""
        cmd = ["pg_dump", "--format=custom", "--no-owner", "-h", db["HOST"], "-p", str(db["PORT"]), "-U", db["USER"], db["NAME"]]
        try:
            done = subprocess.run(cmd, capture_output=True, env={**os.environ, "PGPASSWORD": db["PASSWORD"]}, timeout=600)
        except FileNotFoundError:
            raise CommandError("pg_dump was not found. Install the PostgreSQL client tools on this server.")
        if done.returncode != 0:
            raise CommandError(f"pg_dump failed: {done.stderr.decode(errors='replace').strip()[:300]}")
        return done.stdout
