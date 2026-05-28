"""Legacy DRF routes still served by apps.users.

Everything else (signup, signin, admin-signup, users/, modify-user/,
send-notification/) has migrated to django-ninja and is served by the
NinjaAPI at ``agriBack.api``. The admin sub-tree under
``/auth/admin/...`` remains DRF for now (PR 10 target).
"""
from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("admin/", include("apps.users.admin_urls")),
]
