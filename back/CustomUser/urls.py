from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    AdminSignUpAPIView,
    ModifyUserView,
    SendNotificationEmailView,
    SignInAPIView,
    SignUpAPIView,
    UserListView,
)

urlpatterns = [
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("signup/", SignUpAPIView.as_view(), name="signup"),
    path("signin/", SignInAPIView.as_view(), name="signin"),
    path("admin-signup/", AdminSignUpAPIView.as_view(), name="admin-signup"),
    # Legacy admin endpoints — kept for the in-flight frontend migration
    # over to /auth/admin/users/*. Now gated behind IsAdminUser inside the
    # view classes.
    path("users/", UserListView.as_view(), name="user-list"),
    path("modify-user/", ModifyUserView.as_view(), name="modify-user"),
    path(
        "send-notification/",
        SendNotificationEmailView.as_view(),
        name="send-notification",
    ),
    # New admin tree
    path("admin/", include("CustomUser.admin_urls")),
]
