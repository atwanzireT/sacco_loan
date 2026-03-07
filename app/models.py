from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, cast
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils import timezone

# =============================================================================
# Money & Percent helpers
# =============================================================================
MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _to_decimal(value, *, label: str) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid {label} value: {value!r}")


def quantize_money(value) -> Decimal:
    d = _to_decimal(value, label="money")
    q = d.quantize(MONEY, rounding=ROUND_HALF_UP)
    return ZERO if q == -ZERO else q


def quantize_percent(value) -> Decimal:
    d = _to_decimal(value, label="percent")
    q = d.quantize(PERCENT, rounding=ROUND_HALF_UP)
    return ZERO if q == -ZERO else q


# =============================================================================
# Upload helpers
# =============================================================================
ALLOWED_DOC_EXTS = ["pdf", "jpg", "jpeg", "png", "webp"]
file_validators = [FileExtensionValidator(ALLOWED_DOC_EXTS)]


def _ext(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def member_doc_upload(instance: "Member", filename: str) -> str:
    base = instance.phone or instance.member_id or f"member-{uuid4().hex[:8]}"
    ext = _ext(filename) or ".bin"
    return f"uploads/members/{base}/docs/{uuid4().hex}{ext}"


def loan_doc_upload(instance: "Loan", filename: str) -> str:
    loan_key = instance.loan_id or str(instance.pk) or f"new-{uuid4().hex[:8]}"
    ext = _ext(filename) or ".bin"
    return f"uploads/loans/{loan_key}/evidence/{uuid4().hex}{ext}"


def expense_doc_upload(instance: "Expense", filename: str) -> str:
    expense_key = instance.expense_id or str(instance.pk) or f"new-{uuid4().hex[:8]}"
    ext = _ext(filename) or ".bin"
    return f"uploads/expenses/{expense_key}/receipts/{uuid4().hex}{ext}"


# =============================================================================
# Member Manager
# =============================================================================
class MemberManager(models.Manager):
    def get_next_member_id(self) -> str:
        last = self.all().order_by("-id").only("member_id").first()
        if last and last.member_id:
            try:
                last_num = int(last.member_id.replace("VL", ""))
                next_num = last_num + 1
            except Exception:
                next_num = 1
        else:
            next_num = 1
        return f"VL{next_num:03d}"


# =============================================================================
# Member
# =============================================================================
class Member(models.Model):
    member_id = models.CharField(max_length=10, unique=True, blank=True, db_index=True)
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    phone = models.CharField(max_length=20, unique=True)
    joined_on = models.DateField(default=timezone.localdate)

    address = models.CharField(max_length=200, blank=True)
    next_of_kin = models.CharField(max_length=120, blank=True)
    nin = models.CharField("NIN", max_length=30, blank=True)
    village = models.CharField(max_length=80, blank=True)
    subcounty = models.CharField(max_length=80, blank=True)

    id_card_front = models.FileField(upload_to=member_doc_upload, validators=file_validators, null=True, blank=True)
    id_card_back = models.FileField(upload_to=member_doc_upload, validators=file_validators, null=True, blank=True)
    lc1_letter = models.FileField(upload_to=member_doc_upload, validators=file_validators, null=True, blank=True)
    recommendation_letter_1 = models.FileField(upload_to=member_doc_upload, validators=file_validators, null=True, blank=True)
    recommendation_letter_2 = models.FileField(upload_to=member_doc_upload, validators=file_validators, null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_members"
    )

    objects: MemberManager = MemberManager()

    class Meta:
        ordering = ["first_name", "last_name"]
        indexes = [
            models.Index(fields=["member_id"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["nin"]),
            models.Index(fields=["subcounty", "village"]),
        ]
        constraints = [
            models.CheckConstraint(name="member_first_name_not_blank", check=~Q(first_name="")),
            models.CheckConstraint(name="member_last_name_not_blank", check=~Q(last_name="")),
        ]

    def __str__(self) -> str:
        return f"{self.member_id or 'VL???'} • {self.first_name} {self.last_name} ({self.phone})"

    def get_absolute_url(self) -> str:
        return reverse("sacco:member_detail", args=[self.pk])

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def has_open_loan(self) -> bool:
        return self.loans.filter(status=Loan.Status.OPEN).exists()

    def can_apply_for_loan(self) -> tuple[bool, str]:
        """
        ✅ You requested: allow applying even with open loans.
        We still return helpful info for the UI.
        """
        if self.has_open_loan:
            return True, "Member has an open loan, but multiple loans are allowed."
        return True, "Member qualifies for a new loan."

    @property
    def loan_count(self) -> int:
        return self.loans.count()

    def save(self, *args, **kwargs) -> None:
        if not self.member_id:
            for _ in range(10):
                self.member_id = Member.objects.get_next_member_id()
                try:
                    with transaction.atomic():
                        super().save(*args, **kwargs)
                    return
                except IntegrityError:
                    self.member_id = ""
            raise IntegrityError("Could not generate a unique member_id after multiple attempts.")
        super().save(*args, **kwargs)


# =============================================================================
# Loan QuerySet / Manager
# =============================================================================
class LoanQuerySet(models.QuerySet):
    def with_paid_and_balance(self) -> "LoanQuerySet":
        return self.annotate(amount_paid=Sum("payments__amount", default=ZERO)).annotate(
            balance=F("expected_total") - F("amount_paid")
        )

    def open(self) -> "LoanQuerySet":
        return self.filter(status=Loan.Status.OPEN)

    def closed(self) -> "LoanQuerySet":
        return self.filter(status=Loan.Status.CLOSED)

    def overdue(self, on_date: Optional[date] = None) -> "LoanQuerySet":
        on_date = on_date or timezone.localdate()
        return self.filter(status=Loan.Status.OPEN, due_date__lt=on_date)


class LoanManager(models.Manager.from_queryset(LoanQuerySet)):
    pass


# =============================================================================
# Loan
# =============================================================================
class Loan(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    class PaymentMode(models.TextChoices):
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"

    loan_id = models.CharField(max_length=20, unique=True, blank=True, db_index=True)
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="loans")

    principal = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(MONEY)], default=ZERO)
    period = models.PositiveIntegerField(validators=[MinValueValidator(1)], default=1)  # months
    rate = models.DecimalField(
        max_digits=5, decimal_places=2, validators=[MinValueValidator(ZERO)], default=Decimal("15.00")
    )  # monthly %

    payment_mode = models.CharField(max_length=10, choices=PaymentMode.choices, default=PaymentMode.MONTHLY)

    processing_fee = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)], default=ZERO)
    processing_fee_paid = models.BooleanField(default=False)
    processing_fee_paid_on = models.DateField(null=True, blank=True)
    processing_fee_method = models.CharField(max_length=20, blank=True)
    processing_fee_receipt = models.CharField(max_length=40, blank=True)
    processing_fee_note = models.CharField(max_length=160, blank=True)

    expected_total = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(ZERO)], default=ZERO)
    installment_amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(MONEY)], default=ZERO)

    start_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)

    note = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    evidence_letter_1 = models.FileField(upload_to=loan_doc_upload, validators=file_validators, null=True, blank=True)
    evidence_letter_2 = models.FileField(upload_to=loan_doc_upload, validators=file_validators, null=True, blank=True)
    evidence_letter_3 = models.FileField(upload_to=loan_doc_upload, validators=file_validators, null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_loans"
    )

    objects: LoanManager = LoanManager()

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["loan_id"]),
            models.Index(fields=["member", "status"]),
            models.Index(fields=["start_date"]),
            models.Index(fields=["due_date"]),
            models.Index(fields=["payment_mode"]),
            models.Index(fields=["processing_fee_paid"]),
        ]
        constraints = [
            models.CheckConstraint(
                name="loan_due_after_start_or_null",
                check=Q(due_date__isnull=True) | Q(due_date__gte=F("start_date")),
            ),
        ]

    def __str__(self) -> str:
        return f"Loan {self.loan_id or 'VL???:???'} • {self.member.member_id} • UGX {self.principal}"

    def get_absolute_url(self) -> str:
        return reverse("sacco:loan_detail", args=[self.pk])

    def clean(self) -> None:
        super().clean()

        # Fee paid requires date
        if self.processing_fee_paid and not self.processing_fee_paid_on:
            raise ValidationError({"processing_fee_paid_on": "Provide the date the processing fee was paid."})

        if not self.processing_fee_paid and self.processing_fee_paid_on:
            raise ValidationError({"processing_fee_paid": "Uncheck fee paid or clear paid date."})

        # ✅ IMPORTANT: we DO NOT block multiple open loans anymore.

    def _next_member_loan_suffix(self) -> str:
        last_loan_id = (
            Loan.objects.filter(member=self.member)
            .exclude(pk=self.pk)
            .order_by("-id")
            .values_list("loan_id", flat=True)
            .first()
        )
        if last_loan_id and ":" in last_loan_id:
            try:
                last_suffix = int(last_loan_id.split(":")[-1])
                return f"{last_suffix + 1:03d}"
            except Exception:
                pass
        return "001"

    def calculate_interest_amount(self) -> Decimal:
        monthly_rate_percent = quantize_percent(self.rate)
        months = Decimal(str(self.period))
        return quantize_money(self.principal * (monthly_rate_percent / Decimal("100")) * months)

    def calculate_expected_total(self) -> Decimal:
        return quantize_money(self.principal + self.calculate_interest_amount())

    def _total_installments(self) -> int:
        if self.payment_mode == self.PaymentMode.DAILY:
            return int(self.period) * 30
        if self.payment_mode == self.PaymentMode.WEEKLY:
            return int(self.period) * 4
        return int(self.period)

    def calculate_installment_amount(self) -> Decimal:
        total = self.calculate_expected_total()
        n = max(1, self._total_installments())
        return quantize_money(total / Decimal(n))

    def calculate_due_date(self) -> date:
        return self.start_date + relativedelta(months=int(self.period))

    @property
    def processing_fee_due(self) -> Decimal:
        fee = quantize_money(self.processing_fee)
        if fee == ZERO:
            return ZERO
        return ZERO if self.processing_fee_paid else fee

    @property
    def amount_paid(self) -> Decimal:
        total = cast(Optional[Decimal], self.payments.aggregate(s=Sum("amount"))["s"]) or ZERO
        return quantize_money(total)

    @property
    def balance(self) -> Decimal:
        bal = quantize_money(self.expected_total - self.amount_paid)
        return bal if bal > ZERO else ZERO

    def close_if_settled(self, *, persist: bool = True) -> bool:
        if self.status == self.Status.OPEN and self.balance == ZERO:
            self.status = self.Status.CLOSED
            if persist:
                self.save(update_fields=["status"])
            return True
        return False

    def reopen_if_unpaid(self, *, persist: bool = True) -> bool:
        if self.status == self.Status.CLOSED and self.balance > ZERO:
            self.status = self.Status.OPEN
            if persist:
                self.save(update_fields=["status"])
            return True
        return False

    def save(self, *args, **kwargs) -> None:
        if self.member and not getattr(self.member, "member_id", None):
            self.member.save(update_fields=["member_id"])

        if not self.loan_id and self.member and getattr(self.member, "member_id", None):
            for _ in range(10):
                self.loan_id = f"{self.member.member_id}:{self._next_member_loan_suffix()}"
                try:
                    with transaction.atomic():
                        return self._save_with_calculations(*args, **kwargs)
                except IntegrityError:
                    self.loan_id = ""
            raise IntegrityError("Could not generate a unique loan_id after multiple attempts.")

        return self._save_with_calculations(*args, **kwargs)

    def _save_with_calculations(self, *args, **kwargs) -> None:
        self.principal = quantize_money(self.principal)
        self.rate = quantize_percent(self.rate)
        self.processing_fee = quantize_money(self.processing_fee)

        if not self.processing_fee_paid:
            self.processing_fee_paid_on = None
            self.processing_fee_method = ""
            self.processing_fee_receipt = ""
            self.processing_fee_note = ""

        self.expected_total = self.calculate_expected_total()
        self.installment_amount = self.calculate_installment_amount()
        self.due_date = self.calculate_due_date()

        super().save(*args, **kwargs)


# =============================================================================
# Payment (repayments only — fee is not paid here)
# =============================================================================
class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        BANK = "BANK", "Bank"
        MOBILE = "MOBILE", "Mobile"

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="payments")
    date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(MONEY)])
    method = models.CharField(max_length=20, choices=Method.choices, blank=True)
    receipt = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=160, blank=True)

    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_payments"
    )

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [models.Index(fields=["loan", "date"]), models.Index(fields=["method"])]

    def __str__(self) -> str:
        return f"{self.loan.loan_id} • UGX {self.amount} on {self.date}"

    def clean(self) -> None:
        super().clean()

        if not self.loan_id:
            return

        # Block repayments if processing fee required but not paid
        if self.loan.processing_fee_due > ZERO:
            raise ValidationError("Processing fee must be paid before receiving loan repayments.")

        remaining = self.loan.balance

        # Editing existing payment: add back previous amount
        if self.pk:
            previous = Payment.objects.filter(pk=self.pk).values_list("amount", flat=True).first() or ZERO
            remaining = quantize_money(remaining + quantize_money(previous))

        incoming = quantize_money(self.amount)
        if incoming > remaining:
            raise ValidationError({"amount": f"Amount exceeds remaining loan balance (UGX {remaining})."})

    def save(self, *args, **kwargs) -> None:
        creating = self._state.adding
        self.amount = quantize_money(self.amount)
        super().save(*args, **kwargs)

        if creating:
            self.loan.close_if_settled(persist=True)
        else:
            if not self.loan.close_if_settled(persist=True):
                self.loan.reopen_if_unpaid(persist=True)

    def delete(self, *args, **kwargs) -> None:
        loan = self.loan
        super().delete(*args, **kwargs)
        if not loan.close_if_settled(persist=True):
            loan.reopen_if_unpaid(persist=True)


# =============================================================================
# Expense
# =============================================================================
class ExpenseQuerySet(models.QuerySet):
    def pending(self) -> "ExpenseQuerySet":
        return self.filter(status=Expense.Status.PENDING)

    def approved(self) -> "ExpenseQuerySet":
        return self.filter(status=Expense.Status.APPROVED)

    def rejected(self) -> "ExpenseQuerySet":
        return self.filter(status=Expense.Status.REJECTED)

    def paid(self) -> "ExpenseQuerySet":
        return self.filter(status=Expense.Status.PAID)

    def by_field_officer(self, user) -> "ExpenseQuerySet":
        return self.filter(submitted_by=user)


class ExpenseManager(models.Manager.from_queryset(ExpenseQuerySet)):
    pass


class Expense(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending Approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        PAID = "PAID", "Paid"

    class Category(models.TextChoices):
        TRANSPORT = "TRANSPORT", "Transport"
        COMMUNICATION = "COMMUNICATION", "Communication"
        OFFICE_SUPPLIES = "OFFICE_SUPPLIES", "Office Supplies"
        MEALS = "MEALS", "Meals & Entertainment"
        ACCOMMODATION = "ACCOMMODATION", "Accommodation"
        TRAINING = "TRAINING", "Training & Development"
        MAINTENANCE = "MAINTENANCE", "Vehicle Maintenance"
        FUEL = "FUEL", "Fuel"
        OTHER = "OTHER", "Other"

    expense_id = models.CharField(max_length=20, unique=True, blank=True)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)

    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(MONEY)])
    date_incurred = models.DateField(default=timezone.localdate)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)

    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="submitted_expenses")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_expenses"
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    receipt = models.FileField(upload_to=expense_doc_upload, validators=file_validators, null=True, blank=True)
    supporting_docs = models.FileField(upload_to=expense_doc_upload, validators=file_validators, null=True, blank=True)

    purpose = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    finance_notes = models.TextField(blank=True)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    objects: ExpenseManager = ExpenseManager()

    class Meta:
        ordering = ["-created"]
        indexes = [
            models.Index(fields=["expense_id"]),
            models.Index(fields=["status"]),
            models.Index(fields=["category"]),
            models.Index(fields=["date_incurred"]),
            models.Index(fields=["submitted_by"]),
            models.Index(fields=["approved_by"]),
        ]

    def __str__(self) -> str:
        return f"Expense {self.expense_id or 'EXP????'} • {self.title} • UGX {self.amount}"

    def get_absolute_url(self) -> str:
        return reverse("sacco:expense_detail", args=[self.pk])

    def _generate_expense_id(self) -> str:
        last = Expense.objects.all().order_by("-id").only("expense_id").first()
        if last and last.expense_id:
            try:
                last_number = int(last.expense_id[3:])
                next_number = last_number + 1
            except Exception:
                next_number = 1
        else:
            next_number = 1
        return f"EXP{next_number:04d}"

    def save(self, *args, **kwargs) -> None:
        if not self.expense_id:
            self.expense_id = self._generate_expense_id()

        self.amount = quantize_money(self.amount)

        if self.pk:
            old_status = Expense.objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if old_status != self.Status.APPROVED and self.status == self.Status.APPROVED:
                self.approved_at = timezone.now()

        super().save(*args, **kwargs)

    def can_approve(self, user) -> bool:
        is_finance_user = user.groups.filter(name="Finance").exists()
        is_own_expense = self.submitted_by == user
        return is_finance_user and not is_own_expense and self.status == self.Status.PENDING

    def can_edit(self, user) -> bool:
        return self.submitted_by == user and self.status == self.Status.PENDING

    def approve(self, user, notes: str = "") -> bool:
        if not self.can_approve(user):
            return False
        self.status = self.Status.APPROVED
        self.approved_by = user
        self.approved_at = timezone.now()
        if notes:
            self.finance_notes = notes
        self.save()
        return True

    def reject(self, user, reason: str) -> bool:
        if not self.can_approve(user):
            return False
        self.status = self.Status.REJECTED
        self.approved_by = user
        self.approved_at = timezone.now()
        self.rejection_reason = reason
        self.save()
        return True

    def mark_as_paid(self) -> bool:
        if self.status == self.Status.APPROVED:
            self.status = self.Status.PAID
            self.save()
            return True
        return False

    def reopen(self) -> bool:
        if self.status == self.Status.REJECTED:
            self.status = self.Status.PENDING
            self.rejection_reason = ""
            self.save()
            return True
        return False
