"""
Role based permission classes. Every protected endpoint uses one of these.

There are three roles: ADMIN (whole college), HOD (one department) and STAFF (a teacher in one
department). Each class below simply says which of those roles may call the endpoint.
The department limit (an HOD only touches their own department) is applied separately in
core/scoping.py.
"""
from rest_framework.permissions import BasePermission

from .models import User


class _RolePermission(BasePermission):
    """Base class: allow only logged-in, active users whose role is listed in `roles`."""

    roles = ()
    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        # An inactive (deactivated) user is refused even if their token is still valid.
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.role in self.roles)


class IsCollegeAdmin(_RolePermission):
    """Only the College Admin."""

    roles = (User.Role.ADMIN,)


class IsHOD(_RolePermission):
    """Only a Head of Department."""

    roles = (User.Role.HOD,)


class IsStaffTeacher(_RolePermission):
    """Only a teacher (STAFF role)."""

    roles = (User.Role.STAFF,)


class IsAdminOrHOD(_RolePermission):
    """College Admin or HOD (not teachers)."""

    roles = (User.Role.ADMIN, User.Role.HOD)


class IsHODOrStaff(_RolePermission):
    """HOD or teacher; the College Admin is NOT allowed (used for day-to-day attendance work)."""

    roles = (User.Role.HOD, User.Role.STAFF)


class IsAnyStaffRole(_RolePermission):
    """Any logged-in role: Admin, HOD or teacher."""

    roles = (User.Role.ADMIN, User.Role.HOD, User.Role.STAFF)
