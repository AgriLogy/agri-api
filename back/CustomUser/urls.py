from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    AdminSignInAPIView,
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
    path("admin-signup/", AdminSignUpAPIView.as_view(), name="admin-signin"),
    path("users/", UserListView.as_view(), name="user-list"),
    path("modify-user/", ModifyUserView.as_view(), name="modify-user"),
    path(
        "send-notification/",
        SendNotificationEmailView.as_view(),
        name="send-notification",
    ),
]
