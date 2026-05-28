"""URL conf for /auth/admin/* — admin-only user management."""

from django.urls import path

from .admin_views import (
    AdminUserActivateAPIView,
    AdminUserDetailAPIView,
    AdminUserListCreateAPIView,
    AdminUserResetPasswordAPIView,
)

urlpatterns = [
    path("users/", AdminUserListCreateAPIView.as_view(), name="admin-user-list"),
    path(
        "users/<str:username>/",
        AdminUserDetailAPIView.as_view(),
        name="admin-user-detail",
    ),
    path(
        "users/<str:username>/activate/",
        AdminUserActivateAPIView.as_view(),
        name="admin-user-activate",
    ),
    path(
        "users/<str:username>/reset-password/",
        AdminUserResetPasswordAPIView.as_view(),
        name="admin-user-reset-password",
    ),
]
