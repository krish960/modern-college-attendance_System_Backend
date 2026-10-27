"""
URL routes for accounts: login, token refresh, logout, my profile, change password,
and the HOD / staff management endpoints (created by the router).
"""
from django.urls import path
from rest_framework.routers import SimpleRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import ChangePasswordView, HODViewSet, LoginView, LogoutView, MeView, StaffViewSet

# The router builds list/create/detail/update URLs for each viewset automatically.
router = SimpleRouter()
router.register("hods", HODViewSet, basename="hod")
router.register("staff", StaffViewSet, basename="staff")

urlpatterns = [
    # TokenRefreshView swaps a refresh token for a new access token (built into SimpleJWT).
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("auth/change-password/", ChangePasswordView.as_view(), name="change-password"),
] + router.urls
