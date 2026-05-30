"""
Shared admin-side permissions for CustomUser and analytics.

`IsAdminOrSelf` lets an admin act on any user while a normal user can
only act on their own record (matched against the URL kwarg
`username` — fallback to `pk`).
"""

from rest_framework.permissions import BasePermission, IsAdminUser


class IsAdminOrSelf(BasePermission):
    """Admin → any user. Normal user → self only."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_staff:
            return True
        target = view.kwargs.get("username") or view.kwargs.get("pk")
        if target is None:
            return False
        return str(target) == str(request.user.username) or str(target) == str(
            request.user.pk
        )

    def has_object_permission(self, request, view, obj):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_staff:
            return True
        return (
            getattr(obj, "username", None) == request.user.username
            or getattr(obj, "pk", None) == request.user.pk
        )


__all__ = ["IsAdminOrSelf", "IsAdminUser"]
