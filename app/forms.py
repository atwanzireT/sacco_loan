from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from django import forms
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory
from django.utils import timezone

from django_select2 import forms as s2forms

from .models import (
    Member,
    Loan,
    Payment,
    Expense,
    MONEY,
    ZERO,
    quantize_money,
)

# =============================================================================
# Tailwind helpers (gold theme)
# =============================================================================
INPUT_CLS = (
    "w-full rounded-xl border border-slate-300 px-3 py-2 text-sm "
    "focus:outline-none focus:ring-2 focus:ring-[#D59C2E] focus:border-[#D59C2E]"
)
SELECT_CLS = (
    "w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm "
    "focus:outline-none focus:ring-2 focus:ring-[#D59C2E] focus:border-[#D59C2E]"
)
TEXTAREA_CLS = (
    "w-full rounded-xl border border-slate-300 px-3 py-2 text-sm "
    "focus:outline-none focus:ring-2 focus:ring-[#D59C2E] focus:border-[#D59C2E]"
)
FILE_CLS = (
    "block w-full text-sm text-slate-700 file:mr-3 file:rounded-lg file:border-0 "
    "file:bg-slate-100 file:px-3 file:py-2 hover:file:bg-slate-200 "
    "focus:outline-none focus:ring-2 focus:ring-[#D59C2E]"
)


def _append_class(widget: forms.Widget, class_name: str) -> None:
    existing = widget.attrs.get("class", "")
    widget.attrs["class"] = (existing + " " + class_name).strip()


class TailwindFormMixin:
    """Automatically apply Tailwind classes to common widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for name, field in self.fields.items():
            w = field.widget

            if isinstance(w, (forms.TextInput, forms.NumberInput)) and "placeholder" not in w.attrs:
                w.attrs["placeholder"] = field.label or name.replace("_", " ").title()

            if isinstance(
                w,
                (forms.TextInput, forms.NumberInput, forms.EmailInput, forms.URLInput, forms.PasswordInput),
            ):
                _append_class(w, INPUT_CLS)
            elif isinstance(w, forms.Select):
                _append_class(w, SELECT_CLS)
            elif isinstance(w, forms.Textarea):
                _append_class(w, TEXTAREA_CLS)
                w.attrs.setdefault("rows", 3)
            elif isinstance(w, (forms.FileInput, forms.ClearableFileInput)):
                _append_class(w, FILE_CLS)
            elif isinstance(w, forms.DateInput):
                _append_class(w, INPUT_CLS)
            else:
                _append_class(w, INPUT_CLS)


# =============================================================================
# Widgets
# =============================================================================
class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, *args, **kwargs):
        attrs = kwargs.pop("attrs", {})
        attrs.setdefault("placeholder", "YYYY-MM-DD")
        super().__init__(attrs=attrs, *args, **kwargs)


class MoneyInput(forms.NumberInput):
    def __init__(self, *args, **kwargs):
        attrs = kwargs.pop("attrs", {})
        attrs.setdefault("step", "0.01")
        attrs.setdefault("min", "0.00")
        super().__init__(attrs=attrs, *args, **kwargs)


class PercentageInput(forms.NumberInput):
    def __init__(self, *args, **kwargs):
        attrs = kwargs.pop("attrs", {})
        attrs.setdefault("step", "0.01")
        attrs.setdefault("min", "0.00")
        attrs.setdefault("max", "100.00")
        super().__init__(attrs=attrs, *args, **kwargs)


class DocInput(forms.ClearableFileInput):
    def __init__(self, *args, **kwargs):
        attrs = kwargs.pop("attrs", {})
        attrs.setdefault("accept", ".pdf,.jpg,.jpeg,.png,.webp")
        super().__init__(attrs=attrs, *args, **kwargs)


# =============================================================================
# Mixins / validators
# =============================================================================
class CleanStrMixin:
    """Trim whitespace on all CharField inputs."""

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        for name, field in self.fields.items():
            if isinstance(field, forms.CharField):
                val = cleaned.get(name)
                if isinstance(val, str):
                    cleaned[name] = val.strip()
        return cleaned


def validate_max_filesize(file, *, max_mb: int = 5):
    if not file:
        return
    limit_bytes = max_mb * 1024 * 1024
    if getattr(file, "size", 0) > limit_bytes:
        raise ValidationError(f"File too large (>{max_mb} MB).")


# =============================================================================
# Select2 widgets
# =============================================================================
class MemberWidget(s2forms.ModelSelect2Widget):
    search_fields = [
        "member_id__icontains",
        "first_name__icontains",
        "last_name__icontains",
        "phone__icontains",
        "nin__icontains",
    ]


class LoanWidget(s2forms.ModelSelect2Widget):
    search_fields = [
        "loan_id__icontains",
        "member__member_id__icontains",
        "member__first_name__icontains",
        "member__last_name__icontains",
        "member__phone__icontains",
    ]


# =============================================================================
# Member form
# =============================================================================
class MemberForm(TailwindFormMixin, CleanStrMixin, forms.ModelForm):
    id_card_front = forms.FileField(required=False, widget=DocInput())
    id_card_back = forms.FileField(required=False, widget=DocInput())
    lc1_letter = forms.FileField(required=False, widget=DocInput())
    recommendation_letter_1 = forms.FileField(required=False, widget=DocInput())
    recommendation_letter_2 = forms.FileField(required=False, widget=DocInput())

    class Meta:
        model = Member
        fields = [
            "first_name",
            "last_name",
            "phone",
            "joined_on",
            "nin",
            "address",
            "village",
            "subcounty",
            "next_of_kin",
            "id_card_front",
            "id_card_back",
            "lc1_letter",
            "recommendation_letter_1",
            "recommendation_letter_2",
        ]
        widgets = {
            "joined_on": DateInput(),
        }

    def clean_phone(self) -> str:
        phone = (self.cleaned_data.get("phone") or "").strip()
        if not any(phone.startswith(p) for p in ("07", "03", "+256")):
            raise ValidationError("Enter a valid Ugandan phone (07…, 03…, or +256…).")
        return phone

    def clean_nin(self) -> str:
        nin = (self.cleaned_data.get("nin") or "").strip().upper()
        if nin:
            if not nin.isalnum():
                raise ValidationError("NIN must be alphanumeric.")
            if not (10 <= len(nin) <= 20):
                raise ValidationError("NIN length looks invalid (expected 10–20 characters).")
        return nin

    def _clean_file(self, key: str):
        f = self.cleaned_data.get(key)
        validate_max_filesize(f)
        return f

    def clean_id_card_front(self): return self._clean_file("id_card_front")
    def clean_id_card_back(self): return self._clean_file("id_card_back")
    def clean_lc1_letter(self): return self._clean_file("lc1_letter")
    def clean_recommendation_letter_1(self): return self._clean_file("recommendation_letter_1")
    def clean_recommendation_letter_2(self): return self._clean_file("recommendation_letter_2")


# =============================================================================
# Loan form
# =============================================================================
class LoanForm(TailwindFormMixin, CleanStrMixin, forms.ModelForm):
    principal = forms.DecimalField(min_value=MONEY, widget=MoneyInput(), help_text="Loan principal amount")

    processing_fee = forms.DecimalField(
        min_value=ZERO,
        required=False,
        widget=MoneyInput(),
        help_text="One-time processing fee (paid once before repayments).",
    )

    processing_fee_paid = forms.BooleanField(required=False, help_text="Tick if the processing fee has been paid.")

    processing_fee_paid_on = forms.DateField(required=False, widget=DateInput(), label="Processing fee paid on")

    processing_fee_method = forms.ChoiceField(
        required=False,
        choices=[("", "---------")] + list(Payment.Method.choices),
        widget=forms.Select(),
        label="Processing fee method",
    )

    processing_fee_receipt = forms.CharField(
        required=False,
        max_length=40,
        label="Processing fee receipt",
        widget=forms.TextInput(attrs={"placeholder": "Receipt/Ref # (optional)"}),
    )

    processing_fee_note = forms.CharField(
        required=False,
        max_length=160,
        label="Processing fee note",
        widget=forms.TextInput(attrs={"placeholder": "Optional note (max 160 chars)"}),
    )

    period = forms.IntegerField(min_value=1, widget=forms.NumberInput(), help_text="Loan duration in months")

    rate = forms.DecimalField(
        min_value=Decimal("0.00"),
        max_value=Decimal("100.00"),
        widget=PercentageInput(),
        help_text="Monthly interest rate (%)",  # ✅ match your model logic
        label="Monthly interest rate (%)",
    )

    payment_mode = forms.ChoiceField(choices=Loan.PaymentMode.choices, widget=forms.Select())

    start_date = forms.DateField(widget=DateInput(), required=False)

    expected_total = forms.DecimalField(
        required=False,
        widget=MoneyInput(attrs={"readonly": "readonly"}),
        label="Expected Total",
        help_text="Principal + Interest (processing fee excluded)",
    )

    installment_amount = forms.DecimalField(
        required=False,
        widget=MoneyInput(attrs={"readonly": "readonly"}),
        label="Installment Amount",
        help_text="Per payment period (auto)",
    )

    due_date = forms.DateField(
        required=False,
        widget=DateInput(attrs={"readonly": "readonly"}),
        label="Due Date",
    )

    evidence_letter_1 = forms.FileField(required=False, widget=DocInput())
    evidence_letter_2 = forms.FileField(required=False, widget=DocInput())
    evidence_letter_3 = forms.FileField(required=False, widget=DocInput())

    class Meta:
        model = Loan
        fields = [
            "member",
            "principal",
            "processing_fee",
            "processing_fee_paid",
            "processing_fee_paid_on",
            "processing_fee_method",
            "processing_fee_receipt",
            "processing_fee_note",
            "period",
            "rate",
            "payment_mode",
            "start_date",
            "due_date",
            "expected_total",
            "installment_amount",
            "status",
            "note",
            "evidence_letter_1",
            "evidence_letter_2",
            "evidence_letter_3",
        ]
        widgets = {
            "member": MemberWidget(),  # ✅ must be instantiated
            "status": forms.Select(),
            "note": forms.TextInput(attrs={"placeholder": "Optional note (max 200 chars)"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Computed fields: display only
        for fname in ("expected_total", "installment_amount", "due_date"):
            if fname in self.fields:
                self.fields[fname].disabled = True
                self.fields[fname].required = False

        # Default start date
        if not (self.data or getattr(self.instance, "start_date", None)):
            self.initial.setdefault("start_date", timezone.localdate())

        # Seed for edit
        if self.instance and self.instance.pk:
            self.initial.setdefault("expected_total", self.instance.expected_total)
            self.initial.setdefault("installment_amount", self.instance.installment_amount)
            self.initial.setdefault("due_date", self.instance.due_date)

            self.initial.setdefault("processing_fee_paid", self.instance.processing_fee_paid)
            self.initial.setdefault("processing_fee_paid_on", self.instance.processing_fee_paid_on)
            self.initial.setdefault("processing_fee_method", self.instance.processing_fee_method)
            self.initial.setdefault("processing_fee_receipt", self.instance.processing_fee_receipt)
            self.initial.setdefault("processing_fee_note", self.instance.processing_fee_note)

        # ✅ IMPORTANT FIX:
        # DO NOT exclude members with open loans anymore.
        self.fields["member"].queryset = Member.objects.order_by("member_id", "first_name", "last_name")
        self.fields["member"].help_text = "Search by member ID, name, phone or NIN."

    def _clean_file(self, key: str):
        f = self.cleaned_data.get(key)
        validate_max_filesize(f)
        return f

    def clean_evidence_letter_1(self): return self._clean_file("evidence_letter_1")
    def clean_evidence_letter_2(self): return self._clean_file("evidence_letter_2")
    def clean_evidence_letter_3(self): return self._clean_file("evidence_letter_3")

    def clean_processing_fee(self) -> Decimal:
        fee = self.cleaned_data.get("processing_fee")
        return quantize_money(fee or ZERO)

    def clean_rate(self):
        rate = self.cleaned_data.get("rate")
        if rate is None:
            return rate
        # keep 2dp, but do not use quantize_money name-wise; still okay
        rate = quantize_money(rate)
        if rate > Decimal("100.00"):
            raise ValidationError("Interest rate cannot exceed 100%.")
        return rate

    def clean(self):
        cleaned = super().clean()

        cleaned["start_date"] = cleaned.get("start_date") or timezone.localdate()

        if cleaned.get("principal") is not None:
            cleaned["principal"] = quantize_money(cleaned["principal"])

        fee = quantize_money(cleaned.get("processing_fee") or ZERO)
        paid = bool(cleaned.get("processing_fee_paid"))
        paid_on = cleaned.get("processing_fee_paid_on")

        if fee == ZERO:
            cleaned["processing_fee_paid"] = False
            cleaned["processing_fee_paid_on"] = None
            cleaned["processing_fee_method"] = ""
            cleaned["processing_fee_receipt"] = ""
            cleaned["processing_fee_note"] = ""
        else:
            if paid and not paid_on:
                self.add_error("processing_fee_paid_on", "Provide the date the processing fee was paid.")
            if not paid:
                cleaned["processing_fee_paid_on"] = None
                cleaned["processing_fee_method"] = ""
                cleaned["processing_fee_receipt"] = ""
                cleaned["processing_fee_note"] = ""

        return cleaned


# =============================================================================
# Processing Fee Payment form
# =============================================================================
class ProcessingFeePaymentForm(TailwindFormMixin, CleanStrMixin, forms.Form):
    paid_on = forms.DateField(required=False, widget=DateInput(), label="Payment date")
    method = forms.ChoiceField(required=True, choices=Payment.Method.choices, widget=forms.Select(), label="Payment method")
    amount = forms.DecimalField(required=False, min_value=ZERO, widget=MoneyInput(), label="Amount paid")
    receipt = forms.CharField(required=False, max_length=40, widget=forms.TextInput(attrs={"placeholder": "Receipt/Ref # (optional)"}))
    note = forms.CharField(required=False, max_length=160, widget=forms.TextInput(attrs={"placeholder": "Optional note"}))

    def __init__(self, *args, loan: Loan, **kwargs):
        super().__init__(*args, **kwargs)
        self.loan = loan
        self.initial.setdefault("paid_on", timezone.localdate())
        self.initial.setdefault("amount", loan.processing_fee)

    def clean_amount(self):
        amt = self.cleaned_data.get("amount")
        if amt in (None, ""):
            return None
        return quantize_money(amt)

    def clean(self):
        cleaned = super().clean()
        loan = self.loan

        if loan.processing_fee <= ZERO:
            raise ValidationError("This loan has no processing fee set.")

        if loan.processing_fee_paid:
            raise ValidationError("Processing fee is already marked as paid for this loan.")

        cleaned["paid_on"] = cleaned.get("paid_on") or timezone.localdate()

        amount = cleaned.get("amount")
        if amount is None:
            amount = quantize_money(loan.processing_fee)
            cleaned["amount"] = amount

        fee = quantize_money(loan.processing_fee)
        if amount != fee:
            raise ValidationError(f"Processing fee must be paid in full: UGX {fee:,.2f}")

        return cleaned


# =============================================================================
# Payment form (repayments only)
# =============================================================================
class PaymentForm(TailwindFormMixin, CleanStrMixin, forms.ModelForm):
    amount = forms.DecimalField(min_value=MONEY, widget=MoneyInput())
    date = forms.DateField(widget=DateInput(), required=False)

    class Meta:
        model = Payment
        fields = ["loan", "date", "amount", "method", "receipt", "note"]
        widgets = {
            "loan": LoanWidget(),  # ✅ instantiate
            "method": forms.Select(),
            "receipt": forms.TextInput(attrs={"placeholder": "Receipt/Ref # (optional)"}),
            "note": forms.TextInput(attrs={"placeholder": "Optional note (max 160 chars)"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not (self.data or getattr(self.instance, "date", None)):
            self.initial.setdefault("date", timezone.localdate())

        # Only OPEN loans should receive repayments
        self.fields["loan"].queryset = Loan.objects.filter(status=Loan.Status.OPEN).select_related("member")

        # ✅ FIX: Payment has no loan_id, so check loan
        if self.instance and self.instance.pk and getattr(self.instance, "loan", None):
            self.fields["loan"].help_text = f"Current balance: UGX {self.instance.loan.balance:,.2f}"

    def clean_amount(self) -> Decimal:
        return quantize_money(self.cleaned_data["amount"])

    def clean(self):
        cleaned = super().clean()
        cleaned["date"] = cleaned.get("date") or timezone.localdate()

        if cleaned.get("amount") is not None:
            cleaned["amount"] = quantize_money(cleaned["amount"])

        # Run the MODEL validation rules safely (fee rule + overpay)
        loan = cleaned.get("loan") or getattr(self.instance, "loan", None)
        if loan:
            temp = Payment(
                pk=self.instance.pk,
                loan=loan,
                date=cleaned["date"],
                amount=cleaned.get("amount") or ZERO,
                method=cleaned.get("method") or "",
                receipt=cleaned.get("receipt") or "",
                note=cleaned.get("note") or "",
            )
            try:
                temp.clean()
            except ValidationError as e:
                if hasattr(e, "message_dict"):
                    for field, msgs in e.message_dict.items():
                        for msg in msgs:
                            self.add_error(field, msg)
                else:
                    for msg in e.messages:
                        self.add_error(None, msg)

        return cleaned


# =============================================================================
# Inline Payment form (NO loan field)
# =============================================================================
class InlinePaymentForm(TailwindFormMixin, CleanStrMixin, forms.ModelForm):
    amount = forms.DecimalField(min_value=MONEY, widget=MoneyInput())
    date = forms.DateField(widget=DateInput(), required=False)

    class Meta:
        model = Payment
        fields = ["date", "amount", "method", "receipt", "note"]
        widgets = {
            "method": forms.Select(),
            "receipt": forms.TextInput(attrs={"placeholder": "Receipt/Ref #"}),
            "note": forms.TextInput(attrs={"placeholder": "Optional note"}),
        }

    def clean_amount(self) -> Decimal:
        return quantize_money(self.cleaned_data["amount"])

    def clean(self):
        cleaned = super().clean()
        cleaned["date"] = cleaned.get("date") or timezone.localdate()
        if cleaned.get("amount") is not None:
            cleaned["amount"] = quantize_money(cleaned["amount"])
        return cleaned


# =============================================================================
# Payment formset under Loan (uses InlinePaymentForm)
# =============================================================================
PaymentFormSet = inlineformset_factory(
    parent_model=Loan,
    model=Payment,
    form=InlinePaymentForm,
    fields=["date", "amount", "method", "receipt", "note"],
    extra=1,
    can_delete=True,
)


# =============================================================================
# Expense form
# =============================================================================
class ExpenseForm(TailwindFormMixin, CleanStrMixin, forms.ModelForm):
    amount = forms.DecimalField(min_value=MONEY, widget=MoneyInput())
    date_incurred = forms.DateField(widget=DateInput(), required=False)

    receipt = forms.FileField(required=False, widget=DocInput())
    supporting_docs = forms.FileField(required=False, widget=DocInput())

    class Meta:
        model = Expense
        fields = ["title", "category", "amount", "date_incurred", "purpose", "receipt", "supporting_docs"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "Brief description of expense"}),
            "category": forms.Select(),
            "purpose": forms.Textarea(attrs={"placeholder": "Explain why this expense was necessary...", "rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if not (self.data or getattr(self.instance, "date_incurred", None)):
            self.initial.setdefault("date_incurred", timezone.localdate())

        if self.instance._state.adding and self.user:
            self.instance.submitted_by = self.user

    def clean_amount(self):
        amt = self.cleaned_data.get("amount")
        return quantize_money(amt) if amt is not None else amt

    def clean_date_incurred(self):
        return self.cleaned_data.get("date_incurred") or timezone.localdate()

    def clean_receipt(self):
        f = self.cleaned_data.get("receipt")
        validate_max_filesize(f)
        return f

    def clean_supporting_docs(self):
        f = self.cleaned_data.get("supporting_docs")
        validate_max_filesize(f)
        return f

    def clean(self):
        cleaned = super().clean()

        if self.instance._state.adding and not cleaned.get("receipt"):
            self.add_error("receipt", "Receipt is required for new expense claims.")

        d = cleaned.get("date_incurred")
        if d and d > timezone.localdate():
            self.add_error("date_incurred", "Expense date cannot be in the future.")

        return cleaned


# =============================================================================
# Expense Approval form (finance team)
# =============================================================================
class ExpenseApprovalForm(TailwindFormMixin, forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["status", "rejection_reason", "finance_notes"]
        widgets = {
            "status": forms.Select(),
            "rejection_reason": forms.Textarea(attrs={"placeholder": "Explain why rejected...", "rows": 3}),
            "finance_notes": forms.Textarea(attrs={"placeholder": "Internal notes...", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        current = self.instance.status
        if current == Expense.Status.PENDING:
            self.fields["status"].choices = [(Expense.Status.APPROVED, "Approve"), (Expense.Status.REJECTED, "Reject")]
        elif current == Expense.Status.APPROVED:
            self.fields["status"].choices = [(Expense.Status.PAID, "Mark as Paid"), (Expense.Status.REJECTED, "Reject")]
        elif current == Expense.Status.REJECTED:
            self.fields["status"].choices = [(Expense.Status.PENDING, "Reopen for Resubmission")]
        else:
            self.fields["status"].choices = [(Expense.Status.APPROVED, "Revert to Approved")]

        self.fields["rejection_reason"].required = False

    def clean(self):
        cleaned = super().clean()
        new_status = cleaned.get("status")

        if new_status == Expense.Status.REJECTED and not cleaned.get("rejection_reason"):
            self.add_error("rejection_reason", "Rejection reason is required when rejecting an expense.")

        if self.user and not self.instance.can_approve(self.user):
            raise ValidationError("You do not have permission to approve or reject this expense.")

        return cleaned

    def save(self, commit=True):
        expense = super().save(commit=False)
        new_status = self.cleaned_data.get("status")

        if new_status == Expense.Status.APPROVED and self.instance.status != Expense.Status.APPROVED:
            expense.approved_by = self.user
            expense.approved_at = timezone.now()

        if commit:
            expense.save()
        return expense


# =============================================================================
# Expense Search form
# =============================================================================
class ExpenseSearchForm(TailwindFormMixin, forms.Form):
    STATUS_CHOICES = [("", "All Statuses")] + list(Expense.Status.choices)
    CATEGORY_CHOICES = [("", "All Categories")] + list(Expense.Category.choices)

    status = forms.ChoiceField(choices=STATUS_CHOICES, required=False, widget=forms.Select())
    category = forms.ChoiceField(choices=CATEGORY_CHOICES, required=False, widget=forms.Select())
    date_from = forms.DateField(required=False, widget=DateInput(), label="From Date")
    date_to = forms.DateField(required=False, widget=DateInput(), label="To Date")
    search = forms.CharField(required=False, widget=forms.TextInput(attrs={"placeholder": "Search..."}), label="Search")

    def clean(self):
        cleaned = super().clean()
        df, dt = cleaned.get("date_from"), cleaned.get("date_to")
        if df and dt and df > dt:
            self.add_error("date_to", "End date cannot be before start date.")
        return cleaned


# =============================================================================
# Loan Search form
# =============================================================================
class LoanSearchForm(TailwindFormMixin, forms.Form):
    STATUS_CHOICES = [("", "All Statuses")] + list(Loan.Status.choices)
    PAYMENT_MODE_CHOICES = [("", "All Payment Modes")] + list(Loan.PaymentMode.choices)

    status = forms.ChoiceField(choices=STATUS_CHOICES, required=False, widget=forms.Select())
    payment_mode = forms.ChoiceField(choices=PAYMENT_MODE_CHOICES, required=False, widget=forms.Select())

    member = forms.ModelChoiceField(
        queryset=Member.objects.all().order_by("member_id", "first_name", "last_name"),
        required=False,
        widget=MemberWidget(),
        label="Member",
    )

    date_from = forms.DateField(required=False, widget=DateInput(), label="From Date")
    date_to = forms.DateField(required=False, widget=DateInput(), label="To Date")

    search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Search by loan ID, member ID, name..."}),
        label="Search",
    )

    def clean(self):
        cleaned = super().clean()
        df, dt = cleaned.get("date_from"), cleaned.get("date_to")
        if df and dt and df > dt:
            self.add_error("date_to", "End date cannot be before start date.")
        return cleaned
