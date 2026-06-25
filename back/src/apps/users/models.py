from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.utils import timezone


class CustomUserManager(BaseUserManager):
    def create_user(self, username, email, password=None, **extra_fields):
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(username=username, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(username, email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    username = models.CharField(max_length=100, unique=True)
    firstname = models.CharField(max_length=100)
    lastname = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    latitude = models.FloatField(blank=True, null=True)
    payement_status = models.CharField(
        max_length=100,
        choices=[("actif", "Actif"), ("suspended", "Suspended")],
        default="actif",
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    # A technician is a real login whose read scope is another user's (the
    # owner's) data, narrowed to granted zones + graphs. They never write.
    is_technician = models.BooleanField(default=False)

    # Preferred language for outbound notifications (and the web UI). Drives
    # the FR/AR notification template in agri-core. Schema-of-record in
    # agri-db (migration f3a4b5c6d7e8).
    LANG_FR = "fr"
    LANG_AR = "ar"
    LANGUAGE_CHOICES = [(LANG_FR, "Français"), (LANG_AR, "العربية")]
    preferred_language = models.CharField(
        max_length=8,
        choices=LANGUAGE_CHOICES,
        default=LANG_FR,
        help_text="Language for notification emails (and UI): 'fr' or 'ar'.",
    )

    # Notification cadence: how often (in minutes) to send the periodic
    # field-status email, and when it last went out. Stored in minutes so
    # sub-hour cadences (e.g. every 10 min) are expressible; 240 = 4 h.
    notify_every = models.PositiveSmallIntegerField(
        default=240,
        help_text="Minutes between automated notification emails.",
    )
    last_notified = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the most recent notification email was dispatched.",
    )

    date_joined = models.DateTimeField(
        default=timezone.now,
        help_text="When the user account was created.",
    )

    # Admin session kill switch. Any access/refresh token issued (iat) before
    # this moment is rejected, forcing the user to log out on the next request.
    # NULL = sessions have never been revoked. Set by the admin "force logout"
    # action and whenever the account is disabled.
    sessions_revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Tokens issued before this time are rejected (forced logout).",
    )

    objects = CustomUserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["email"]

    class Meta:
        ordering = ["-date_joined"]
        indexes = [
            models.Index(fields=["-date_joined"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self):
        return self.username
