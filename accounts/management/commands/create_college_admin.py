"""
Command: python manage.py create_college_admin --username NAME

Creates the very first College Admin (the top role). Run once when the system is set up,
because only an Admin can register HODs, and HODs register teachers.
"""
import getpass
import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User


class Command(BaseCommand):
    """Management command that makes one College Admin account."""

    help = (
        "Create the initial College Admin account. The password is typed at a hidden prompt "
        "(or read from the ADMIN_PASSWORD environment variable for non-interactive setups) - "
        "it is never passed on the command line and is stored hashed."
    )

    def add_arguments(self, parser):
        # Note: there is deliberately no --password option (it would show up in shell history).
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", default="")
        parser.add_argument("--first-name", default="College")
        parser.add_argument("--last-name", default="Admin")

    def handle(self, *args, **opts):
        """Check the username is free, read the password safely, then create the Admin."""
        username = opts["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise CommandError(f"User '{username}' already exists.")

        # Prefer the environment variable (for scripts); otherwise ask twice at a hidden prompt.
        password = os.environ.get("ADMIN_PASSWORD")
        if not password:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                raise CommandError("Passwords did not match.")

        # is_staff / is_superuser let this account use the Django admin site as well.
        user = User(
            username=username, email=opts["email"], first_name=opts["first_name"], last_name=opts["last_name"],
            role=User.Role.ADMIN, is_staff=True, is_superuser=True,
        )
        # Run Django's password checks (length, too common...) before saving.
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages))
        user.set_password(password)  # stores a hash, never the plain password
        user.save()
        self.stdout.write(self.style.SUCCESS(f"College Admin '{username}' created."))
