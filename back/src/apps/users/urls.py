"""Legacy DRF routes still served by apps.users.

Everything else (signup, signin, admin-signup, users/, modify-user/,
send-notification/, /auth/admin/*) has migrated to django-ninja and is
served by the NinjaAPI at ``agriapi.api``. Only the simplejwt token
endpoints remain DRF.
"""

from django.contrib.auth import get_user_model
from django.urls import path
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView


class RevocationAwareTokenRefreshView(TokenRefreshView):
    """Refresh that honours the admin session kill switch.

    Before issuing a new access token we check the incoming refresh token
    against ``CustomUser.sessions_revoked_at`` (and ``is_active``). This
    closes the loop on "force logout": revoking a user's sessions blocks
    both their current access token (via the authenticators) and their
    ability to mint a fresh one here.
    """

    def post(self, request, *args, **kwargs):
        raw = request.data.get("refresh")
        if raw:
            try:
                refresh = RefreshToken(raw)
            except TokenError:
                refresh = None
            if refresh is not None:
                user_model = get_user_model()
                try:
                    user = user_model.objects.get(pk=refresh.get("user_id"))
                except user_model.DoesNotExist:
                    user = None
                if user is not None:
                    if not user.is_active:
                        return Response({"detail": "Account is disabled."}, status=401)
                    revoked_at = getattr(user, "sessions_revoked_at", None)
                    iat = refresh.get("iat")
                    if (
                        revoked_at is not None
                        and iat is not None
                        and iat < revoked_at.timestamp()
                    ):
                        return Response(
                            {"detail": "Session has been revoked."}, status=401
                        )
        return super().post(request, *args, **kwargs)


urlpatterns = [
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path(
        "token/refresh/",
        RevocationAwareTokenRefreshView.as_view(),
        name="token_refresh",
    ),
]
