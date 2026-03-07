from __future__ import annotations
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        FIELD_OFFICER = "FIELD_OFFICER", "Loan Field Officer"
        FINANCE = "FINANCE", "Finance"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.FIELD_OFFICER)
    phone = models.CharField(max_length=20, blank=True)

    def __str__(self) -> str:
        return f"{self.username} ({self.get_role_display()})"

    # Convenience flags
    @property
    def is_admin(self) -> bool:
        return self.is_superuser or self.role == self.Role.ADMIN

    @property
    def is_finance(self) -> bool:
        return self.is_superuser or self.role == self.Role.FINANCE

    @property
    def is_field_officer(self) -> bool:
        return self.is_superuser or self.role == self.Role.FIELD_OFFICER
