from django.contrib.auth import authenticate, login
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import CustomUser
from .serializers import (
    AdminCreateUserSerializer,
    AdminModifyUserSerializer,
    AdminUserSerializer,
    UserSerializer,
)


class SignUpAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data.get("username")
            email = serializer.validated_data.get("email")
            password = serializer.validated_data.get("password")

            # Check if username or email already exists
            if CustomUser.objects.filter(email=email).exists():
                return Response(
                    {"email": "This email is already in use."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if CustomUser.objects.filter(username=username).exists():
                return Response(
                    {"username": "This username is already in use."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Validate the password
            try:
                validate_password(password)
            except ValidationError as e:
                return Response(
                    {"password": e.messages}, status=status.HTTP_400_BAD_REQUEST
                )

            # Create the user
            user = serializer.save()
            user.set_password(password)
            user.save()
            return Response(
                {"status": "Account created successfully"},
                status=status.HTTP_201_CREATED,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SignInAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")

        if not username or not password:
            return Response(
                {"error": "Username and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Implement caching to limit login attempts
        cache_key = f"login_attempts_{username}"
        attempts = cache.get(cache_key, 0)

        if attempts >= 5:
            return Response(
                {"error": "Too many login attempts. Please try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Authenticate user
        user = authenticate(request, username=username, password=password)

        if user is not None:
            # Reset failed attempts on successful login
            cache.delete(cache_key)

            # Generate JWT tokens
            refresh = RefreshToken.for_user(user)
            login(request, user)

            return Response(
                {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                    "is_staff": user.is_staff,
                },
                status=status.HTTP_200_OK,
            )

        # Increment failed login attempts
        cache.set(cache_key, attempts + 1, timeout=300)  # Timeout of 5 minutes
        return Response(
            {"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED
        )


from CustomUser.models import CustomUser
from django.contrib.auth import authenticate, login
from django.core.cache import cache
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken


class AdminSignInAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("username")
        password = request.data.get("password")

        if not username or not password:
            return Response(
                {"error": "Username and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if user exists
        try:
            user = CustomUser.objects.get(username=username)
        except CustomUser.DoesNotExist:
            return Response(
                {"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED
            )

        # Implement caching to limit login attempts
        cache_key = f"login_attempts_{username}"
        attempts = cache.get(cache_key, 0)

        if attempts >= 5:
            return Response(
                {"error": "Too many login attempts. Please try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Authenticate user
        authenticated_user = authenticate(request, username=username, password=password)

        if authenticated_user is not None:
            # Reset failed attempts on successful login
            cache.delete(cache_key)

            # Generate JWT tokens
            refresh = RefreshToken.for_user(authenticated_user)
            login(request, authenticated_user)

            return Response(
                {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                },
                status=status.HTTP_200_OK,
            )

        # Increment failed login attempts
        cache.set(cache_key, attempts + 1, timeout=300)  # Timeout of 5 minutes
        return Response(
            {"error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED
        )


class UserListView(APIView):
    # permission_classes = [IsAdminUser]
    permission_classes = [AllowAny]

    def get(self, request):
        # if not request.user.is_staff:  # Double-check admin status
        #     return Response({'error': 'Role non valid'}, status=status.HTTP_401_UNAUTHORIZED)

        # Exclude the logged-in admin from the returned data
        users = CustomUser.objects.exclude(id=request.user.id)
        serializer = AdminUserSerializer(users, many=True)
        return Response(serializer.data)


class ModifyUserView(APIView):
    def get(self, request, *args, **kwargs):
        username = request.query_params.get("username")
        if not username:
            return Response(
                {"error": "Username is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        user = get_object_or_404(CustomUser, username=username)
        serializer = AdminModifyUserSerializer(user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def put(self, request, *args, **kwargs):
        username = request.data.get("username")
        if not username:
            return Response(
                {"error": "Username is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        user = get_object_or_404(CustomUser, username=username)

        serializer = AdminModifyUserSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(
                {"message": "User data updated successfully.", "data": serializer.data},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"error": serializer.errors}, status=status.HTTP_400_BAD_REQUEST
            )


class SignUpAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = UserSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data.get("username")
            email = serializer.validated_data.get("email")
            password = serializer.validated_data.get("password")

            # Check if username or email already exists
            if CustomUser.objects.filter(email=email).exists():
                return Response(
                    {"email": "This email is already in use."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if CustomUser.objects.filter(username=username).exists():
                return Response(
                    {"username": "This username is already in use."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Validate the password
            try:
                validate_password(password)
            except ValidationError as e:
                return Response(
                    {"password": e.messages}, status=status.HTTP_400_BAD_REQUEST
                )

            # Create the user
            user = serializer.save()
            user.set_password(password)
            user.save()
            return Response(
                {"status": "Account created successfully"},
                status=status.HTTP_201_CREATED,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    from django.contrib.auth.password_validation import validate_password


from django.core.exceptions import ValidationError
from rest_framework import status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class AdminSignUpAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def post(self, request):
        serializer = AdminCreateUserSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data.get("username")
            email = serializer.validated_data.get("email")
            password = serializer.validated_data.get("password")

            # Check if username or email already exists
            if (
                CustomUser.objects.filter(email=email).exists()
                or CustomUser.objects.filter(username=username).exists()
            ):
                return Response(
                    {"error": "Username or email already exists."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Validate password
            try:
                validate_password(password)
            except ValidationError as e:
                return Response(
                    {"password": e.messages}, status=status.HTTP_400_BAD_REQUEST
                )

            # Create user
            user = serializer.save()
            user.set_password(password)
            user.save()

            return Response(
                {"status": "Account created successfully"},
                status=status.HTTP_201_CREATED,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils.timezone import now
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .notification_helper import perform_calculations

logger = logging.getLogger(__name__)


class SendNotificationEmailView(APIView):
    """
    Send the current user the latest field-status email on demand.
    Recipient is always request.user.email — no hardcoded fallback.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        recipient = (getattr(user, "email", "") or "").strip()
        if not recipient:
            return Response(
                {
                    "success": False,
                    "error": "user has no email address on file",
                },
                status=400,
            )

        try:
            message = perform_calculations(user)
            send_mail(
                subject="Mise à jour de votre terrain agricole",
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                fail_silently=False,
            )
            user.last_notified = now()
            user.save(update_fields=["last_notified"])
            logger.info("Sent on-demand notification email to %s", recipient)
            return Response({"success": True, "message": "Email envoyé avec succès."})

        except Exception as exc:
            logger.exception("Failed to send notification email to %s", recipient)
            return Response({"success": False, "error": str(exc)}, status=500)
