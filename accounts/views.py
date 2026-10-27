"""
API views for accounts.

Part 1: login, logout, "me" (own profile) and change password.
Part 2: HOD and staff management. Only the College Admin registers HODs; an HOD registers
teachers of their own department. Every change is written to the audit log.
"""
from django.core.cache import cache
from django.db import IntegrityError
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from core.audit import log_action
from core.scoping import department_scope

from .models import User
from .throttles import LoginIPThrottle, LoginUsernameThrottle
from .permissions import IsAdminOrHOD, IsCollegeAdmin, IsHOD
from .serializers import (
    ChangePasswordSerializer,
    HODSerializer,
    LoginSerializer,
    MeUpdateSerializer,
    ResetPasswordSerializer,
    StaffSerializer,
    UserSerializer,
)

# ----------------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------------


class LoginView(TokenObtainPairView):
    """POST username + password -> access, refresh, user. Rate limited."""

    serializer_class = LoginSerializer
    throttle_classes = [LoginUsernameThrottle, LoginIPThrottle]
    # Open to everyone (the person is not signed in yet), so no auth or permission checks here.
    authentication_classes = []
    permission_classes = []


class LogoutView(APIView):
    """POST the refresh token to blacklist it (the access token expires on its own)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get("refresh")
        if not token:
            raise ValidationError({"refresh": "This field is required."})
        try:
            refresh = RefreshToken(token)
            if str(refresh.get("user_id")) != str(request.user.pk):
                raise PermissionDenied("This token does not belong to you.")
            refresh.blacklist()
        except TokenError:
            pass  # already invalid/expired - the goal (no usable token) is met
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(APIView):
    """GET my own details; PATCH to edit my name, email and phone (never role or department)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        serializer = MeUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()  # role / department are not in the serializer, so they are ignored
        return Response(UserSerializer(request.user).data)


class ChangePasswordView(APIView):
    """
    POST old_password, new_password (+ confirm_password). A wrong current password is counted: after
    MAX_WRONG tries in WINDOW seconds the account must wait, so a stolen open session cannot be used to guess it.
    """

    permission_classes = [IsAuthenticated]
    MAX_WRONG = 5
    WINDOW = 15 * 60  # seconds (15 minutes)

    def post(self, request):
        # The counter lives in the cache, one per user, and expires by itself after WINDOW.
        key = f"pwchange:{request.user.pk}"
        if cache.get(key, 0) >= self.MAX_WRONG:
            raise ValidationError("Too many wrong current passwords. Wait 15 minutes and try again.")
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        if not serializer.is_valid():
            if "old_password" in serializer.errors:  # count only wrong current passwords
                cache.set(key, cache.get(key, 0) + 1, self.WINDOW)
            serializer.is_valid(raise_exception=True)
        cache.delete(key)  # success: forget the earlier wrong tries
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        log_action(request.user, "PASSWORD_CHANGED", request.user, department=request.user.department)
        return Response({"detail": "Password changed."})


# ----------------------------------------------------------------------------
# HOD and staff management
# ----------------------------------------------------------------------------


class AccountViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Shared behaviour for the HOD and staff endpoints: list, create, edit, activate, deactivate and
    reset password. There is no delete: accounts are deactivated (soft delete) so history is kept.
    Subclasses set `role` (which kind of user) and `audit_prefix` (used in audit action names).
    """

    role = None
    audit_prefix = ""
    search_fields = ["username", "first_name", "last_name", "email", "profile__employee_id"]
    filterset_fields = ["is_active", "department"]
    ordering_fields = ["first_name", "username", "date_joined"]

    def get_queryset(self):
        # Department scoping: an HOD sees only their own department; the Admin sees all.
        qs = User.objects.filter(role=self.role).select_related("department", "profile").order_by("first_name", "username")
        return department_scope(qs, self.request.user)

    def _audit(self, action_name, obj, **kwargs):
        # Writes e.g. STAFF_CREATED or HOD_DEACTIVATED into the audit log, tagged with the department.
        log_action(self.request.user, f"{self.audit_prefix}_{action_name}", obj, department=obj.department, **kwargs)

    def perform_create(self, serializer):
        obj = serializer.save()
        self._audit("CREATED", obj, new={"username": obj.username, "name": obj.display_name})

    def perform_update(self, serializer):
        obj = serializer.save()
        self._audit("UPDATED", obj, new={"username": obj.username, "name": obj.display_name})

    def _set_active(self, value):
        """Turn an account on (True) or off (False). This is the soft delete: the row stays in the database."""
        obj = self.get_object()
        if obj.is_active == value:
            return Response(UserSerializer(obj).data)
        if value:
            # A person cannot be switched on inside a department that is switched off.
            if not obj.department.is_active:
                raise ValidationError("Activate the department first.")
        obj.is_active = value
        # The database allows only one active HOD per department; breaking that raises IntegrityError.
        try:
            obj.save(update_fields=["is_active"])
        except IntegrityError:
            raise ValidationError("This department already has another active HOD.")
        self._audit("ACTIVATED" if value else "DEACTIVATED", obj, old={"is_active": not value}, new={"is_active": value})
        return Response(UserSerializer(obj).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """POST .../activate/ : switch the account back on."""
        return self._set_active(True)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        """POST .../deactivate/ : switch the account off (the person can no longer sign in)."""
        return self._set_active(False)

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        """POST .../reset-password/ : set a new password for someone who forgot theirs (no old password needed)."""
        obj = self.get_object()
        serializer = ResetPasswordSerializer(data=request.data, context={"target": obj})
        serializer.is_valid(raise_exception=True)
        obj.set_password(serializer.validated_data["password"])
        obj.save(update_fields=["password"])
        self._audit("PASSWORD_RESET", obj)
        return Response({"detail": "Password reset."})


class HODViewSet(AccountViewSet):
    """College Admin only: register HODs and activate / deactivate them."""

    role = User.Role.HOD
    audit_prefix = "HOD"
    serializer_class = HODSerializer
    permission_classes = [IsCollegeAdmin]


class StaffViewSet(AccountViewSet):
    """HOD manages staff in their own department. Admin can read all staff."""

    role = User.Role.STAFF
    audit_prefix = "STAFF"
    serializer_class = StaffSerializer

    def get_permissions(self):
        # Only an HOD may CREATE staff. Admin and HOD may view and manage them.
        if self.action in ("list", "retrieve", "update", "partial_update", "activate", "deactivate", "reset_password"):
            return [IsAdminOrHOD()]  # the college admin can correct and manage staff, but registers them through the HOD
        return [IsHOD()]
