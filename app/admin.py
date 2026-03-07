from __future__ import annotations

from decimal import Decimal

from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import Member, Loan, Payment, Expense


# =============================================================================
# Helpers
# =============================================================================
def money(v) -> str:
    """Format UGX (no decimals)."""
    try:
        v = Decimal(v or 0)
    except Exception:
        v = Decimal("0")
    return f"{v:,.0f}"


def file_link(field) -> str:
    """Return an HTML link for FileField if exists."""
    if not field:
        return "-"
    try:
        url = field.url
    except Exception:
        return "-"
    name = str(field).split("/")[-1]
    return format_html('<a href="{}" target="_blank" rel="noopener">📎 {}</a>', url, name)


# =============================================================================
# Inlines
# =============================================================================
class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("date", "amount", "method", "receipt", "note", "created_by", "created")
    readonly_fields = ("created_by", "created")
    ordering = ("-date", "-id")


# =============================================================================
# Member Admin
# =============================================================================
@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = (
        "member_id",
        "full_name",
        "phone",
        "joined_on",
        "loan_count_display",
        "has_open_loan_display",
        "created_by",
    )
    list_filter = ("joined_on", "subcounty")
    search_fields = ("member_id", "first_name", "last_name", "phone", "nin", "village", "subcounty")
    ordering = ("first_name", "last_name")
    readonly_fields = ("member_id", "created_by", "docs_preview")

    fieldsets = (
        ("Member", {"fields": ("member_id", "first_name", "last_name", "phone", "joined_on")}),
        ("Details", {"fields": ("address", "next_of_kin", "nin", "village", "subcounty")}),
        (
            "Documents",
            {
                "fields": (
                    "id_card_front",
                    "id_card_back",
                    "lc1_letter",
                    "recommendation_letter_1",
                    "recommendation_letter_2",
                    "docs_preview",
                )
            },
        ),
        ("Audit", {"fields": ("created_by",)}),
    )

    @admin.display(description="Loans")
    def loan_count_display(self, obj: Member) -> int:
        return obj.loans.count()

    @admin.display(description="Open Loan")
    def has_open_loan_display(self, obj: Member):
        has_open = obj.loans.filter(status=Loan.Status.OPEN).exists()
        if has_open:
            return format_html(
                '<span style="padding:2px 8px;border-radius:999px;background:#fde8e8;color:#9b1c1c;font-weight:600;">YES</span>'
            )
        return format_html(
            '<span style="padding:2px 8px;border-radius:999px;background:#e7f7ef;color:#116b3a;font-weight:600;">NO</span>'
        )

    @admin.display(description="Documents")
    def docs_preview(self, obj: Member):
        return format_html(
            "<div style='display:grid;gap:6px;'>"
            "<div><b>ID Front:</b> {}</div>"
            "<div><b>ID Back:</b> {}</div>"
            "<div><b>LC1:</b> {}</div>"
            "<div><b>Reco 1:</b> {}</div>"
            "<div><b>Reco 2:</b> {}</div>"
            "</div>",
            file_link(obj.id_card_front),
            file_link(obj.id_card_back),
            file_link(obj.lc1_letter),
            file_link(obj.recommendation_letter_1),
            file_link(obj.recommendation_letter_2),
        )

    def save_model(self, request, obj: Member, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


# =============================================================================
# Loan Admin
# =============================================================================
@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    inlines = [PaymentInline]

    list_display = (
        "loan_id",
        "member_link",
        "principal_ugx",
        "rate_monthly_display",
        "period",
        "payment_mode",
        "processing_fee_ugx",
        "fee_paid_badge",
        "expected_total_ugx",
        "amount_paid_ugx",
        "balance_ugx",
        "status_badge",
        "start_date",
        "due_date",
        "days_overdue_display",
        "created",
    )

    list_filter = (
        "status",
        "payment_mode",
        "processing_fee_paid",
        "start_date",
        "due_date",
        "created",
    )
    search_fields = ("loan_id", "member__member_id", "member__first_name", "member__last_name", "member__phone")
    date_hierarchy = "created"
    ordering = ("-created",)

    readonly_fields = (
        "loan_id",
        "expected_total",
        "installment_amount",
        "amount_paid_ugx",
        "balance_ugx",
        "processing_fee_due_ugx",
        "days_overdue_display",
        "created",
        "created_by",
        "evidence_preview",
    )

    fieldsets = (
        ("Loan Identity", {"fields": ("loan_id", "member", "status", "note")}),
        ("Terms", {"fields": ("principal", "rate", "period", "payment_mode", "start_date", "due_date")}),
        (
            "Processing Fee (Paid Upfront)",
            {
                "fields": (
                    "processing_fee",
                    "processing_fee_paid",
                    "processing_fee_paid_on",
                    "processing_fee_method",
                    "processing_fee_receipt",
                    "processing_fee_note",
                    "processing_fee_due_ugx",
                )
            },
        ),
        (
            "Calculated (Fee Excluded)",
            {
                "fields": (
                    "expected_total",
                    "installment_amount",
                    "amount_paid_ugx",
                    "balance_ugx",
                    "days_overdue_display",
                )
            },
        ),
        ("Evidence", {"fields": ("evidence_letter_1", "evidence_letter_2", "evidence_letter_3", "evidence_preview")}),
        ("Audit", {"fields": ("created", "created_by")}),
    )

    actions = ("mark_processing_fee_paid_today", "close_if_settled_action", "reopen_loans_action")

    @admin.display(description="Member", ordering="member__member_id")
    def member_link(self, obj: Loan):
        m = obj.member
        if not m:
            return "-"
        url = reverse(f"admin:{m._meta.app_label}_{m._meta.model_name}_change", args=[m.pk])
        return format_html('<a href="{}">{} • {}</a>', url, m.member_id, m.full_name)

    @admin.display(description="Principal (UGX)")
    def principal_ugx(self, obj: Loan):
        return money(obj.principal)

    @admin.display(description="Rate")
    def rate_monthly_display(self, obj: Loan):
        try:
            return f"{Decimal(obj.rate):.2f}% / month"
        except Exception:
            return f"{obj.rate}% / month"

    @admin.display(description="Fee (UGX)")
    def processing_fee_ugx(self, obj: Loan):
        return money(obj.processing_fee)

    @admin.display(description="Fee Due (UGX)")
    def processing_fee_due_ugx(self, obj: Loan):
        return money(obj.processing_fee_due)

    @admin.display(description="Expected (UGX)")
    def expected_total_ugx(self, obj: Loan):
        return money(obj.expected_total)

    @admin.display(description="Paid (UGX)")
    def amount_paid_ugx(self, obj: Loan):
        return money(obj.amount_paid)

    @admin.display(description="Balance (UGX)")
    def balance_ugx(self, obj: Loan):
        return money(obj.balance)

    @admin.display(description="Status")
    def status_badge(self, obj: Loan):
        if obj.status == Loan.Status.CLOSED:
            return format_html(
                '<span style="padding:2px 8px;border-radius:999px;background:#e7f7ef;color:#116b3a;font-weight:600;">CLOSED</span>'
            )
        return format_html(
            '<span style="padding:2px 8px;border-radius:999px;background:#fff7e6;color:#7a4e00;font-weight:600;">OPEN</span>'
        )

    @admin.display(description="Processing Fee")
    def fee_paid_badge(self, obj: Loan):
        try:
            fee = Decimal(obj.processing_fee or 0)
        except Exception:
            fee = Decimal("0")

        if fee <= 0:
            return format_html(
                '<span style="padding:2px 8px;border-radius:999px;background:#f3f4f6;color:#374151;font-weight:600;">NO FEE</span>'
            )
        if obj.processing_fee_paid:
            return format_html(
                '<span style="padding:2px 8px;border-radius:999px;background:#e7f7ef;color:#116b3a;font-weight:600;">PAID</span>'
            )
        return format_html(
            '<span style="padding:2px 8px;border-radius:999px;background:#fde8e8;color:#9b1c1c;font-weight:600;">NOT PAID</span>'
        )

    @admin.display(description="Days overdue")
    def days_overdue_display(self, obj: Loan) -> int:
        if obj.status != Loan.Status.OPEN or not obj.due_date:
            return 0
        today = timezone.localdate()
        if obj.due_date >= today:
            return 0
        return (today - obj.due_date).days

    @admin.display(description="Evidence")
    def evidence_preview(self, obj: Loan):
        return format_html(
            "<div style='display:grid;gap:6px;'>"
            "<div><b>Evidence 1:</b> {}</div>"
            "<div><b>Evidence 2:</b> {}</div>"
            "<div><b>Evidence 3:</b> {}</div>"
            "</div>",
            file_link(obj.evidence_letter_1),
            file_link(obj.evidence_letter_2),
            file_link(obj.evidence_letter_3),
        )

    @admin.action(description="Mark processing fee as PAID today (selected loans)")
    def mark_processing_fee_paid_today(self, request, queryset):
        updated = 0
        for loan in queryset:
            if loan.processing_fee and not loan.processing_fee_paid:
                loan.mark_processing_fee_paid(paid_on=timezone.localdate(), save=True)
                updated += 1
        self.message_user(
            request,
            f"Marked processing fee paid for {updated} loan(s)." if updated else "No loans updated.",
            level=messages.SUCCESS if updated else messages.WARNING,
        )

    @admin.action(description="Close selected loans if settled (balance = 0)")
    def close_if_settled_action(self, request, queryset):
        count = 0
        for loan in queryset:
            if loan.close_if_settled(persist=True):
                count += 1
        self.message_user(
            request,
            f"Closed {count} loan(s)." if count else "No loans were closed.",
            level=messages.SUCCESS if count else messages.WARNING,
        )

    @admin.action(description="Reopen selected loans if unpaid (balance > 0)")
    def reopen_loans_action(self, request, queryset):
        count = 0
        for loan in queryset:
            if loan.reopen_if_unpaid(persist=True):
                count += 1
        self.message_user(
            request,
            f"Reopened {count} loan(s)." if count else "No loans were reopened.",
            level=messages.SUCCESS if count else messages.WARNING,
        )

    def save_model(self, request, obj: Loan, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


# =============================================================================
# Payment Admin
# =============================================================================
@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("loan_link", "date", "amount_ugx", "method", "receipt", "created_by", "created")
    list_filter = ("method", "date", "created")
    search_fields = ("loan__loan_id", "loan__member__member_id", "loan__member__phone", "receipt")
    ordering = ("-date", "-id")
    readonly_fields = ("created", "created_by")

    @admin.display(description="Loan")
    def loan_link(self, obj: Payment):
        l = obj.loan
        if not l:
            return "-"
        url = reverse(f"admin:{l._meta.app_label}_{l._meta.model_name}_change", args=[l.pk])
        return format_html('<a href="{}">{}</a>', url, l.loan_id)

    @admin.display(description="Amount (UGX)")
    def amount_ugx(self, obj: Payment):
        return money(obj.amount)

    def save_model(self, request, obj: Payment, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


# =============================================================================
# Expense Admin
# =============================================================================
@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "expense_id",
        "title",
        "category",
        "amount_ugx",
        "status_badge",
        "date_incurred",
        "submitted_by",
        "approved_by",
        "approved_at",
        "created",
    )
    list_filter = ("status", "category", "date_incurred", "created")
    search_fields = ("expense_id", "title", "submitted_by__username", "approved_by__username")
    ordering = ("-created",)

    readonly_fields = ("expense_id", "created", "updated", "approved_at", "docs_preview")
    actions = ("approve_expenses", "reject_expenses", "mark_paid")

    fieldsets = (
        ("Expense", {"fields": ("expense_id", "title", "category", "amount", "date_incurred", "status")}),
        ("Docs", {"fields": ("receipt", "supporting_docs", "docs_preview")}),
        ("Notes", {"fields": ("purpose", "finance_notes", "rejection_reason")}),
        ("Workflow", {"fields": ("submitted_by", "approved_by", "approved_at")}),
        ("Audit", {"fields": ("created", "updated")}),
    )

    @admin.display(description="Amount (UGX)")
    def amount_ugx(self, obj: Expense):
        return money(obj.amount)

    @admin.display(description="Status")
    def status_badge(self, obj: Expense):
        if obj.status == Expense.Status.APPROVED:
            return format_html(
                '<span style="padding:2px 8px;border-radius:999px;background:#e7f7ef;color:#116b3a;font-weight:600;">APPROVED</span>'
            )
        if obj.status == Expense.Status.PAID:
            return format_html(
                '<span style="padding:2px 8px;border-radius:999px;background:#eef2ff;color:#3730a3;font-weight:600;">PAID</span>'
            )
        if obj.status == Expense.Status.REJECTED:
            return format_html(
                '<span style="padding:2px 8px;border-radius:999px;background:#fde8e8;color:#9b1c1c;font-weight:600;">REJECTED</span>'
            )
        return format_html(
            '<span style="padding:2px 8px;border-radius:999px;background:#fff7e6;color:#7a4e00;font-weight:600;">PENDING</span>'
        )

    @admin.display(description="Documents")
    def docs_preview(self, obj: Expense):
        return format_html(
            "<div style='display:grid;gap:6px;'>"
            "<div><b>Receipt:</b> {}</div>"
            "<div><b>Supporting Docs:</b> {}</div>"
            "</div>",
            file_link(obj.receipt),
            file_link(obj.supporting_docs),
        )

    @admin.action(description="Approve selected expenses (Finance only)")
    def approve_expenses(self, request, queryset):
        count = 0
        for exp in queryset:
            if exp.can_approve(request.user):
                exp.approve(request.user)
                count += 1
        self.message_user(
            request,
            f"Approved {count} expense(s)." if count else "No expenses approved.",
            level=messages.SUCCESS if count else messages.WARNING,
        )

    @admin.action(description="Reject selected expenses (Finance only)")
    def reject_expenses(self, request, queryset):
        default_reason = f"Rejected by {request.user.get_username()} on {timezone.localdate()}"
        count = 0
        for exp in queryset:
            if exp.can_approve(request.user):
                exp.reject(request.user, reason=default_reason)
                count += 1
        self.message_user(
            request,
            f"Rejected {count} expense(s)." if count else "No expenses rejected.",
            level=messages.SUCCESS if count else messages.WARNING,
        )

    @admin.action(description="Mark selected expenses as PAID")
    def mark_paid(self, request, queryset):
        count = 0
        for exp in queryset:
            if exp.status == Expense.Status.APPROVED and exp.mark_as_paid():
                count += 1
        self.message_user(
            request,
            f"Marked {count} expense(s) as PAID." if count else "No expenses marked paid.",
            level=messages.SUCCESS if count else messages.WARNING,
        )

    def save_model(self, request, obj: Expense, form, change):
        if not change and not obj.submitted_by_id:
            obj.submitted_by = request.user
        super().save_model(request, obj, form, change)
