"""
Serializers for accounts. A serializer turns database objects into JSON for the browser and
checks incoming JSON before anything is saved. Most business rules for users live here
(unique usernames, strong passwords, which department a new account goes to).
"""
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from departments.models import Department

from core.audit import log_action
from core.validators import card_text, code_text, person_name, title_text

from .models import StaffProfile, User
from .passwords import check_strong


def clean_phone(value):
    """A phone number as people write it: digits with an optional +, spaces, dashes and brackets (7 to 15 digits)."""
    value = (value or "").strip()
    if not value:
        return ""
    digits = [c for c in value if c.isdigit()]
    if not (7 <= len(digits) <= 15) or any(not (c.isdigit() or c in "+-() ") for c in value) or "+" in value[1:]:
        raise serializers.ValidationError("Enter a phone number with 7 to 15 digits, for example 98765 43210.")
    return value


class UserSerializer(serializers.ModelSerializer):
    """Read-only view of a user, safe to return to the browser."""

    full_name = serializers.CharField(source="get_full_name", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True, default=None)
    employee_id = serializers.CharField(source="profile.employee_id", read_only=True, default="")
    designation = serializers.CharField(source="profile.designation", read_only=True, default="")
    card_code = serializers.CharField(source="profile.card_code", read_only=True, default="")
    employment = serializers.CharField(source="profile.employment", read_only=True, default="PERMANENT")
    employment_label = serializers.CharField(source="profile.get_employment_display", read_only=True, default="Permanent")

    class Meta:
        model = User
        fields = [
            "id", "username", "first_name", "last_name", "full_name", "email", "phone", "role",
            "department", "department_name", "employee_id", "designation", "employment", "employment_label", "card_code", "is_active",
            "date_joined", "last_login",
        ]
        read_only_fields = fields  # nothing here can be written; the password is never included


class LoginSerializer(TokenObtainPairSerializer):
    """
    Username + password. The sign-in pages are separate for College Admin, HOD and Teacher and each one sends
    its `role`: an account may only sign in on the page of its own role.
    """

    role = serializers.ChoiceField(choices=User.Role.choices, required=False, write_only=True)
    MAX_WRONG = 5  # wrong passwords in a row before the username is locked
    LOCK_SECONDS = 15 * 60  # how long the lock lasts (the counter also expires after this)

    def validate(self, attrs):
        """Check the password (parent class), then the role page and the department status."""
        wanted_role = attrs.pop("role", None)
        # Lock-out: after MAX_WRONG wrong passwords in a row for one username, that username is refused
        # for LOCK_SECONDS, even with the right password. This stops slow password guessing.
        key = "loginfail:" + str(attrs.get(self.username_field, "")).strip().lower()
        if cache.get(key, 0) >= self.MAX_WRONG:
            raise AuthenticationFailed("Too many wrong attempts. Wait 15 minutes and try again.")
        try:
            data = super().validate(attrs)  # raises for wrong password / inactive user
        except AuthenticationFailed:
            cache.set(key, cache.get(key, 0) + 1, self.LOCK_SECONDS)
            if cache.get(key) == self.MAX_WRONG:  # write to the audit log once, when the lock starts
                account = User.objects.filter(username__iexact=attrs.get(self.username_field, "")).first()
                if account:
                    log_action(account, "LOGIN_LOCKED", account, department=account.department)
            raise
        cache.delete(key)  # correct password: forget the earlier wrong tries
        user = self.user
        if wanted_role and user.role != wanted_role:
            raise AuthenticationFailed(
                f"This is not a {User.Role(wanted_role).label} account. Use the sign-in page for your own role.")
        if user.role != User.Role.ADMIN and not user.department.is_active:
            raise AuthenticationFailed("Your department is deactivated.")
        data["user"] = UserSerializer(user).data  # the browser gets the user's details together with the tokens
        return data


class MeUpdateSerializer(serializers.ModelSerializer):
    """What a user may change about themselves. Role / department are NOT included."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone"]

    def validate_email(self, value):
        # Emails must be unique (ignoring upper/lower case), but my own current email is fine.
        if value and User.objects.filter(email__iexact=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate_phone(self, value):
        return clean_phone(value)

    def validate_first_name(self, value):
        return person_name(value, "First name") if value.strip() else ""

    def validate_last_name(self, value):
        return person_name(value, "Last name") if value.strip() else ""


class ChangePasswordSerializer(serializers.Serializer):
    """Change your own password: the CURRENT password must be given first, then the new one twice."""

    old_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False, required=False)

    def validate_old_password(self, value):
        # Proves the person at the keyboard knows the password, not just someone using an open session.
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        return check_strong(value, self.context["request"].user)

    def validate(self, attrs):
        # Checked here (not per field) because it compares two fields with each other.
        if "confirm_password" in attrs and attrs["confirm_password"] != attrs["new_password"]:
            raise serializers.ValidationError({"confirm_password": "The two new passwords are not the same."})
        if attrs["old_password"] == attrs["new_password"]:
            raise serializers.ValidationError({"new_password": "The new password must be different from the current one."})
        return attrs


class ResetPasswordSerializer(serializers.Serializer):
    """New password typed by an Admin/HOD for another user. Must pass the same strength rules."""

    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_password(self, value):
        # `target` is the user whose password is being reset (so it cannot be too like their username).
        return check_strong(value, self.context.get("target"))


class AccountWriteSerializer(serializers.ModelSerializer):
    """
    Shared create/update logic for HOD and staff accounts.
    `role` is fixed by the subclass - it can never come from request data - so a
    caller cannot register a privileged account or change anyone's role.
    """

    role_value = None  # set by subclasses

    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    employee_id = serializers.CharField(required=False, allow_blank=True, max_length=30)
    designation = serializers.CharField(required=False, allow_blank=True, max_length=60)
    card_code = serializers.CharField(required=False, allow_blank=True, max_length=100)
    employment = serializers.ChoiceField(choices=StaffProfile.Employment.choices, required=False)

    class Meta:
        model = User
        fields = [
            "id", "username", "password", "first_name", "last_name", "email", "phone",
            "employee_id", "designation", "employment", "card_code", "is_active",
        ]
        read_only_fields = ["id", "is_active"]
        extra_kwargs = {
            "first_name": {"required": True, "allow_blank": False},
            "last_name": {"required": True, "allow_blank": False},
        }

    # --- hooks -----------------------------------------------------------
    def target_department(self, attrs):
        """Which department the account belongs to. Each subclass decides (see below)."""
        raise NotImplementedError

    # --- validation ------------------------------------------------------
    def validate_phone(self, value):
        return clean_phone(value)

    def validate_first_name(self, value):
        return person_name(value, "First name")

    def validate_last_name(self, value):
        return person_name(value, "Last name")

    def validate_employee_id(self, value):
        return code_text(value, "Staff ID")

    def validate_designation(self, value):
        return title_text(value, "Designation") if value.strip() else ""

    def validate_username(self, value):
        # Usernames are the login name, so they are fixed after creation and unique ignoring case.
        value = value.strip()
        if self.instance is not None and value.lower() != self.instance.username.lower():
            raise serializers.ValidationError("A username cannot be changed after the account is created.")
        # "Prof_Rane" and "prof_rane" would be two accounts that look identical at login.
        qs = User.objects.filter(username__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this username already exists.")
        return value

    def validate_password(self, value):
        # Strength rules for a new account's first password (see accounts/passwords.py).
        return check_strong(value)

    def validate_card_code(self, value):
        # Clean the scanned code (no spaces, upper case) and make sure no other person owns this card.
        value = card_text(value)
        if value:
            clash = StaffProfile.objects.filter(card_code=value)
            if self.instance is not None:
                clash = clash.exclude(user=self.instance)
            other = clash.select_related("user").first()
            if other:
                raise serializers.ValidationError(f"This ID card is already registered to {other.user.get_full_name() or other.user.username}.")
        return value

    def validate_email(self, value):
        # Email may be blank, but if given it must not belong to anyone else.
        qs = User.objects.filter(email__iexact=value) if value else User.objects.none()
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate(self, attrs):
        """Checks that need several fields: password required on create, employee ID unique per department."""
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError({"password": "This field is required."})
        employee_id = attrs.get("employee_id")
        if employee_id:
            dept = self.target_department(attrs)
            qs = StaffProfile.objects.filter(user__department=dept, employee_id__iexact=employee_id)
            if self.instance:
                qs = qs.exclude(user=self.instance)
            if qs.exists():
                raise serializers.ValidationError({"employee_id": "This employee ID is already used in the department."})
        return attrs

    # --- persistence -----------------------------------------------------
    @staticmethod
    def next_employee_id(department):
        """EMP-001, EMP-002 ... the next number in this department (IDs typed by hand in another style are ignored)."""
        taken = StaffProfile.objects.filter(user__department=department, employee_id__regex=r"^EMP-[0-9]+$").values_list("employee_id", flat=True)
        highest = max((int(v.split("-", 1)[1]) for v in taken), default=0)
        return f"EMP-{highest + 1:03d}"

    # transaction.atomic: the user and the profile are saved together, or not at all.
    @transaction.atomic
    def create(self, validated_data):
        """Make the User (with the role fixed by the subclass) and its StaffProfile."""
        profile = {k: validated_data.pop(k, "") for k in ("employee_id", "designation", "card_code")}
        profile["employment"] = validated_data.pop("employment", StaffProfile.Employment.PERMANENT)
        password = validated_data.pop("password")
        department = self.target_department(validated_data)
        if not profile["employee_id"]:
            profile["employee_id"] = self.next_employee_id(department)
        validated_data.pop("department", None)  # passed explicitly below
        user = User(role=self.role_value, department=department, **validated_data)
        user.set_password(password)  # hashed, never stored as plain text
        user.save()
        StaffProfile.objects.create(user=user, **profile)
        return user

    @transaction.atomic
    def update(self, instance, validated_data):
        """Edit name/contact/profile details. Password, username and department cannot change here."""
        validated_data.pop("password", None)  # passwords change via the reset endpoint
        validated_data.pop("username", None)  # usernames are fixed after creation
        validated_data.pop("department", None)
        profile = {k: validated_data.pop(k) for k in ("employee_id", "designation", "employment", "card_code") if k in validated_data}
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if profile:
            StaffProfile.objects.update_or_create(user=instance, defaults=profile)
            instance._state.fields_cache.pop("profile", None)  # the response must show the saved profile, not a stale copy
        return instance

    def to_representation(self, instance):
        # Always reply with the read-only UserSerializer shape, so the password never comes back.
        return UserSerializer(instance, context=self.context).data


class StaffSerializer(AccountWriteSerializer):
    """Teacher accounts. A new teacher always joins the department of the HOD who creates them."""

    role_value = User.Role.STAFF

    def target_department(self, attrs):
        if self.instance is not None:  # the college admin edits staff of any department
            return self.instance.department
        return self.context["request"].user.department


class HODSerializer(AccountWriteSerializer):
    """HOD accounts, created by the College Admin, who picks an active department for each one."""

    role_value = User.Role.HOD

    department = serializers.PrimaryKeyRelatedField(
        queryset=Department.objects.filter(is_active=True), required=False
    )

    class Meta(AccountWriteSerializer.Meta):
        fields = AccountWriteSerializer.Meta.fields + ["department"]

    def target_department(self, attrs):
        # On create the department comes from the form; on edit it stays what it was.
        return attrs.get("department") or (self.instance.department if self.instance else None)

    def validate(self, attrs):
        # Rule: one active HOD per department (the database also enforces it, this gives a clear message).
        if self.instance is None:
            dept = attrs.get("department")
            if dept is None:
                raise serializers.ValidationError({"department": "This field is required."})
            if User.objects.filter(role=User.Role.HOD, department=dept, is_active=True).exists():
                raise serializers.ValidationError(
                    {"department": "This department already has an active HOD. Deactivate that HOD first."}
                )
        return super().validate(attrs)
