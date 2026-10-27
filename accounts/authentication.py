"""
How a request proves who it is. We use JWT tokens (signed tickets given at login) and add one
extra check: the user's department must still be active.
"""
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


class ActiveDepartmentJWTAuthentication(JWTAuthentication):
    """
    JWT auth that also rejects users whose department has been deactivated, so a
    deactivated department loses access immediately (not only at next login).
    Inactive users are already rejected by SimpleJWT.
    """

    def get_user(self, validated_token):
        # Admins have no department, so they skip this check. Everyone else needs an active one.
        user = super().get_user(validated_token)
        if user.role != "ADMIN" and (user.department_id is None or not user.department.is_active):
            raise AuthenticationFailed("Your department is deactivated.", code="department_inactive")
        return user
