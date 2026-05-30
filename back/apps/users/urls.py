"""Legacy DRF routes still served by apps.users.

Everything else (signup, signin, admin-signup, users/, modify-user/,
send-notification/, /auth/admin/*) has migrated to django-ninja and is
served by the NinjaAPI at ``agriapi.api``. Only the simplejwt token
endpoints remain DRF.
"""

from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]
