"""Management command `restore_db`: decrypts a backup made by `backup_db` into a normal file so you can restore it yourself."""
from pathlib import Path

from cryptography.fernet import InvalidToken
from django.core.management.base import BaseCommand, CommandError

from .backup_db import fernet


class Command(BaseCommand):
    """`python manage.py restore_db <backup.enc>`: writes the decrypted copy next to you and never touches the live database."""

    help = "Decrypt a backup into a new file (SQLite file, or a pg_dump file for PostgreSQL). It never touches the live database; you restore it yourself."

    def add_arguments(self, parser):
        """Arguments: the encrypted backup file, and an optional --output name."""
        parser.add_argument("backup", help="Path of the .enc file")
        parser.add_argument("--output", default=None, help="Where to write the decrypted file (default restored.sqlite3 or restored.pgdump)")

    def handle(self, *args, **opts):
        """Check the files, decrypt the backup and write the result with owner-only permissions."""
        box = fernet()  # the encryption key comes from the same helper that made the backup
        src = Path(opts["backup"])
        is_pg = src.name.endswith(".pgdump.enc")  # PostgreSQL backups end with .pgdump.enc, SQLite ones do not
        out = Path(opts["output"] or ("restored.pgdump" if is_pg else "restored.sqlite3"))
        if not src.is_file():
            raise CommandError("Backup file not found.")
        if out.exists():  # never overwrite an existing file
            raise CommandError(f"{out} already exists. Choose another --output name.")
        try:
            data = box.decrypt(src.read_bytes())
        except InvalidToken:
            raise CommandError("Could not decrypt: wrong key or the file was changed.")
        out.write_bytes(data)
        out.chmod(0o600)  # only the owner may read the decrypted data
        if is_pg:
            self.stdout.write(self.style.SUCCESS(f"Decrypted dump written to {out}. Stop the server, then: pg_restore --clean --if-exists --no-owner -d <db name> -U <db user> -h <host> {out}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Decrypted database written to {out}. Stop the server, replace db.sqlite3 with it, start again."))
