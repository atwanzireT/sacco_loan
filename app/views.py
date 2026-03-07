# app/views.py — FULL REWRITE (Matches your urls.py + Loan Officers can create payments)
# -----------------------------------------------------------------------------
# ✅ Members CAN apply for loans even with open loans
# ✅ Processing fee must be paid before repayments
# ✅ Loan Field Officers CAN create repayments (FIELD_OFFICER + FINANCE + ADMIN)
# ✅ Expense visibility:
#    - FINANCE / ADMIN / superuser => see ALL expenses
#    - Others => ONLY see their own expenses
# ✅ Strong filtering for Members & Loans
# ✅ Reports + pagination keep filters (base_query)
# ✅ Includes API endpoints required by urls.py:
#    - check_member_loan_eligibility
#    - get_loan_calculations
# -----------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from math import floor
from typing import Optional
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import (
    Avg,
    Q,
    F,
    Sum,
    Value,
    OuterRef,
    Subquery,
    DecimalField,
    QuerySet,
    Count,
)
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from accounts.decorators import finance_required, field_officer_required, role_required
from accounts.models import User
from .forms import (
    MemberForm,
    LoanForm,
    PaymentForm,
    PaymentFormSet,
    ExpenseForm,
    ExpenseApprovalForm,
    ExpenseSearchForm,
    ProcessingFeePaymentForm,
)
from .models import Member, Loan, Payment, Expense

logger = logging.getLogger(__name__)

# =============================================================================
# Global constants
# =============================================================================
PER_PAGE_OPTIONS = [10, 20, 50, 100]
ZERO = Decimal("0.00")
YOOLA_SMS_URL = "https://yoolasms.com/api/v1/send"

# ✅ Loan Field Officers must be able to create repayments:
PAYMENT_OFFICER_ROLES = [User.Role.FIELD_OFFICER, User.Role.FINANCE, User.Role.ADMIN]


# =============================================================================
# Basic helpers
# =============================================================================
def _clean(s: str | None) -> str:
    return (s or "").strip()


def _parse_int(v: str | None, default: int) -> int:
    v = _clean(v)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _parse_decimal(v: str | None) -> Optional[Decimal]:
    v = _clean(v)
    if not v:
        return None
    try:
        return Decimal(v)
    except (InvalidOperation, ValueError):
        return None


def _parse_date(v: str | None) -> Optional[date]:
    v = _clean(v)
    if not v:
        return None
    try:
        return date.fromisoformat(v)  # YYYY-MM-DD
    except ValueError:
        return None


def _today() -> date:
    return timezone.localdate()


def _is_finance_or_admin(user: User) -> bool:
    return bool(user.is_authenticated and (user.is_superuser or user.role in [User.Role.FINANCE, User.Role.ADMIN]))


def _per_page(request: HttpRequest, default: int = 20) -> int:
    n = _parse_int(request.GET.get("per_page"), default)
    return n if n in PER_PAGE_OPTIONS else default


def _paginate(request: HttpRequest, qs: QuerySet, per_page_default: int = 20):
    paginator = Paginator(qs, _per_page(request, per_page_default))
    return paginator.get_page(request.GET.get("page"))


def build_base_query(params: dict) -> str:
    """
    For pagination links: ?{{ base_query }}&page=2
    Removes empty values and page.
    """
    cleaned: dict = {}
    for k, v in (params or {}).items():
        if k == "page":
            continue
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        cleaned[k] = v
    return urlencode(cleaned)


def dperc(n: Decimal, d: Decimal) -> Decimal:
    if not d or d == ZERO:
        return ZERO
    return (Decimal(n) / Decimal(d) * Decimal("100")).quantize(Decimal("0.01"))


def _can_record_payments(user: User) -> bool:
    return bool(user.is_authenticated and (user.is_superuser or user.role in PAYMENT_OFFICER_ROLES))


# =============================================================================
# SMS helpers (YoolaSMS)
# =============================================================================
def normalize_ug_phone(phone: str) -> str:
    p = (phone or "").strip().replace(" ", "")
    if not p:
        return ""
    if p.startswith("+"):
        return p
    if p.startswith("256"):
        return f"+{p}"
    if p.startswith("0") and len(p) >= 10:
        return "+256" + p[1:]
    return p


def send_sms(phone: str, message: str) -> tuple[bool, str]:
    api_key = getattr(settings, "YOOLA_SMS_API_KEY", "")
    if not api_key:
        return False, "Missing YOOLA_SMS_API_KEY in settings.py"

    phone_norm = normalize_ug_phone(phone)
    if not phone_norm:
        return False, "Missing/invalid phone number"

    payload = {"phone": phone_norm, "message": message, "api_key": api_key}

    try:
        resp = requests.post(YOOLA_SMS_URL, json=payload, timeout=15)
        ok = 200 <= resp.status_code < 300
        return ok, resp.text
    except Exception as e:
        logger.exception("SMS send failed")
        return False, str(e)


# =============================================================================
# Annotation helpers
# =============================================================================
def annotate_loan_totals(qs: QuerySet[Loan]) -> QuerySet[Loan]:
    return qs.annotate(
        amount_paid_a=Coalesce(Sum("payments__amount"), Value(ZERO)),
    ).annotate(
        balance_a=F("expected_total") - F("amount_paid_a")
    )


def annotate_member_totals(qs: QuerySet[Member]) -> QuerySet[Member]:
    return qs.annotate(
        total_expected_a=Coalesce(Sum("loans__expected_total"), Value(ZERO)),
        total_paid_a=Coalesce(Sum("loans__payments__amount"), Value(ZERO)),
        loans_count_a=Count("loans", distinct=True),
        open_loans_a=Count("loans", filter=Q(loans__status=Loan.Status.OPEN), distinct=True),
        closed_loans_a=Count("loans", filter=Q(loans__status=Loan.Status.CLOSED), distinct=True),
    ).annotate(
        balance_a=F("total_expected_a") - F("total_paid_a")
    )


def annotate_member_latest_loan(qs: QuerySet[Member]) -> QuerySet[Member]:
    base = Loan.objects.filter(member=OuterRef("pk")).order_by("-start_date", "-id")

    latest_id = Subquery(base.values("id")[:1])
    latest_start = Subquery(base.values("start_date")[:1])
    latest_due = Subquery(base.values("due_date")[:1])
    latest_status = Subquery(base.values("status")[:1])

    latest_principal = Subquery(
        base.values("principal")[:1],
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )
    latest_expected_total = Subquery(
        base.values("expected_total")[:1],
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )

    latest_amount_paid = Subquery(
        Loan.objects.filter(pk=latest_id)
        .annotate(ap=Coalesce(Sum("payments__amount"), Value(ZERO)))
        .values("ap")[:1],
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )

    latest_balance = Subquery(
        Loan.objects.filter(pk=latest_id)
        .annotate(ap=Coalesce(Sum("payments__amount"), Value(ZERO)))
        .annotate(bal=F("expected_total") - F("ap"))
        .values("bal")[:1],
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )

    return qs.annotate(
        latest_loan_id=latest_id,
        latest_loan_start=latest_start,
        latest_loan_due=latest_due,
        latest_loan_status=latest_status,
        latest_loan_principal=Coalesce(latest_principal, Value(ZERO)),
        latest_loan_expected_total=Coalesce(latest_expected_total, Value(ZERO)),
        latest_loan_amount_paid=Coalesce(latest_amount_paid, Value(ZERO)),
        latest_loan_balance=Coalesce(latest_balance, Value(ZERO)),
    )


# =============================================================================
# Processing fee helper
# =============================================================================
def processing_fee_due(loan: Loan) -> Decimal:
    """
    Prefer model property if it exists.
    """
    due = getattr(loan, "processing_fee_due", None)
    if due is not None:
        try:
            return Decimal(due)
        except Exception:
            pass

    fee = getattr(loan, "processing_fee", ZERO) or ZERO
    paid = bool(getattr(loan, "processing_fee_paid", False))
    return ZERO if (paid or fee <= ZERO) else Decimal(fee)


# =============================================================================
# Schedule helpers (optional; safe if generate_schedule() doesn't exist)
# =============================================================================
def loan_schedule_rows(loan: Loan) -> list[dict]:
    """
    Normalizes Loan.generate_schedule() output to:
      {"idx":1,"due_date":date,"amount":Decimal,"remaining_balance":Decimal,"paid":bool}
    """
    try:
        rows = loan.generate_schedule()  # optional method
    except Exception:
        return []

    out: list[dict] = []
    for i, r in enumerate(rows, start=1):
        due = r.get("due_date") or r.get("date")
        amt = r.get("payment_due") or r.get("amount") or ZERO
        rem = r.get("remaining_balance", ZERO)
        out.append(
            {
                "idx": i,
                "due_date": due,
                "amount": amt,
                "remaining_balance": rem,
                "paid": bool(r.get("paid", False)),
            }
        )
    return out


def next_due_row(loan: Loan) -> Optional[dict]:
    for row in loan_schedule_rows(loan):
        if (row.get("remaining_balance") or ZERO) > ZERO:
            return row
    return None


# =============================================================================
# Timeseries helpers (payments)
# =============================================================================
def payments_timeseries(last_n_days: int = 30) -> dict[str, str]:
    today = _today()
    start = today - timedelta(days=last_n_days - 1)

    raw = (
        Payment.objects.filter(date__gte=start)
        .values("date")
        .annotate(total=Coalesce(Sum("amount"), Value(ZERO)))
        .order_by("date")
    )
    by_day = {row["date"]: row["total"] for row in raw}

    labels, values = [], []
    for i in range(last_n_days):
        day = start + timedelta(days=i)
        labels.append(day.isoformat())
        values.append(float(by_day.get(day, ZERO)))

    return {"labels": json.dumps(labels), "values": json.dumps(values)}


# =============================================================================
# DASHBOARDS
# =============================================================================
@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    today = _today()

    open_stats = Loan.objects.filter(status=Loan.Status.OPEN).aggregate(
        total_expected=Coalesce(Sum("expected_total"), Value(ZERO)),
        total_paid=Coalesce(Sum("payments__amount"), Value(ZERO)),
    )

    expense_stats = Expense.objects.aggregate(
        total_pending=Count("id", filter=Q(status=Expense.Status.PENDING)),
        total_approved=Count("id", filter=Q(status=Expense.Status.APPROVED)),
        total_amount_pending=Coalesce(Sum("amount", filter=Q(status=Expense.Status.PENDING)), Value(ZERO)),
        total_amount_approved=Coalesce(Sum("amount", filter=Q(status=Expense.Status.APPROVED)), Value(ZERO)),
    )

    total_expected = open_stats["total_expected"] or ZERO
    total_paid = open_stats["total_paid"] or ZERO

    context = {
        "member_count": Member.objects.count(),
        "open_loans": Loan.objects.filter(status=Loan.Status.OPEN).count(),
        "overdue_loans": Loan.objects.filter(status=Loan.Status.OPEN, due_date__lt=today).count(),
        "payments_today": Payment.objects.filter(date=today).count(),
        "total_expected": total_expected,
        "total_paid": total_paid,
        "total_balance": total_expected - total_paid,
        "pending_expenses": expense_stats["total_pending"],
        "approved_expenses": expense_stats["total_approved"],
        "pending_expenses_amount": expense_stats["total_amount_pending"],
        "approved_expenses_amount": expense_stats["total_amount_approved"],
    }
    return render(request, "sacco/dashboard.html", context)


@login_required
def finance_dashboard(request: HttpRequest) -> HttpResponse:
    today = _today()
    d30_start = today - timedelta(days=29)
    d7_end = today + timedelta(days=7)

    totals = Loan.objects.aggregate(
        total_principal=Coalesce(Sum("principal"), Value(ZERO)),
        total_expected=Coalesce(Sum("expected_total"), Value(ZERO)),
        total_paid=Coalesce(Sum("payments__amount"), Value(ZERO)),
    )

    total_principal: Decimal = totals["total_principal"]
    total_expected: Decimal = totals["total_expected"]
    total_paid: Decimal = totals["total_paid"]
    total_balance: Decimal = total_expected - total_paid
    collection_rate_pct: Decimal = dperc(total_paid, total_expected)

    open_qs = Loan.objects.filter(status=Loan.Status.OPEN)
    counts = {
        "open_loans": open_qs.count(),
        "closed_loans": Loan.objects.filter(status=Loan.Status.CLOSED).count(),
        "overdue_loans": open_qs.filter(due_date__lt=today).count(),
    }

    def overdue_count(min_days: int | None, max_days: int | None) -> int:
        q = Q(status=Loan.Status.OPEN, due_date__lt=today)
        if min_days is not None:
            q &= Q(due_date__lte=today - timedelta(days=min_days))
        if max_days is not None:
            q &= Q(due_date__gt=today - timedelta(days=max_days))
        return Loan.objects.filter(q).count()

    overdue_buckets = {
        "1_7": overdue_count(1, 7),
        "8_30": overdue_count(8, 30),
        "31_60": overdue_count(31, 60),
        "61_plus": overdue_count(61, None),
    }

    payment_breakdown_30 = list(
        Payment.objects.filter(date__gte=d30_start)
        .values("method")
        .annotate(total=Coalesce(Sum("amount"), Value(ZERO)))
        .order_by("-total")
    )

    payments_ts_30 = payments_timeseries(30)

    due_soon_loans = list(
        annotate_loan_totals(
            Loan.objects.select_related("member")
            .filter(status=Loan.Status.OPEN, due_date__range=[today, d7_end])
        )
        .order_by("due_date", "id")[:10]
    )

    overdue_loans = list(
        annotate_loan_totals(
            Loan.objects.select_related("member")
            .filter(status=Loan.Status.OPEN, due_date__lt=today)
        )
        .order_by("-balance_a", "due_date", "id")[:10]
    )

    top_members_qs = (
        annotate_member_totals(Member.objects.all())
        .order_by("-balance_a", "first_name", "last_name")
        .values(
            "id",
            "first_name",
            "last_name",
            "total_expected_a",
            "total_paid_a",
            "balance_a",
            "loans_count_a",
        )[:10]
    )
    top_members = [
        {**m, "full_name": f"{m.get('first_name','')} {m.get('last_name','')}".strip()}
        for m in top_members_qs
    ]

    recent_payments = (
        Payment.objects.select_related("loan", "loan__member")
        .filter(date__gte=d30_start)
        .order_by("-date", "-id")[:10]
    )

    expense_stats = Expense.objects.aggregate(
        total_pending=Count("id", filter=Q(status=Expense.Status.PENDING)),
        total_approved=Count("id", filter=Q(status=Expense.Status.APPROVED)),
        total_rejected=Count("id", filter=Q(status=Expense.Status.REJECTED)),
        total_paid=Count("id", filter=Q(status=Expense.Status.PAID)),
        total_amount_pending=Coalesce(Sum("amount", filter=Q(status=Expense.Status.PENDING)), Value(ZERO)),
        total_amount_approved=Coalesce(Sum("amount", filter=Q(status=Expense.Status.APPROVED)), Value(ZERO)),
        total_amount_paid=Coalesce(Sum("amount", filter=Q(status=Expense.Status.PAID)), Value(ZERO)),
    )
    pending_expenses = Expense.objects.filter(status=Expense.Status.PENDING).order_by("-created")[:10]

    fee_totals = Loan.objects.aggregate(
        processing_fees_collected=Coalesce(Sum("processing_fee", filter=Q(processing_fee_paid=True)), Value(ZERO)),
        processing_fees_pending=Coalesce(Sum("processing_fee", filter=Q(processing_fee_paid=False, processing_fee__gt=ZERO)), Value(ZERO)),
        processing_fees_loans_paid=Count("id", filter=Q(processing_fee_paid=True)),
        processing_fees_loans_unpaid=Count("id", filter=Q(processing_fee_paid=False, processing_fee__gt=ZERO)),
    )

    context = {
        "now": timezone.now(),
        "total_principal": total_principal,
        "total_expected": total_expected,
        "total_paid": total_paid,
        "total_balance": total_balance,
        "collection_rate_pct": collection_rate_pct,
        "counts": counts,
        "overdue_buckets": overdue_buckets,
        "payment_breakdown_30": payment_breakdown_30,
        "payments_ts_30": payments_ts_30,
        "due_soon_loans": due_soon_loans,
        "overdue_loans": overdue_loans,
        "top_members": top_members,
        "recent_payments": recent_payments,
        "expense_stats": expense_stats,
        "pending_expenses": pending_expenses,
        "processing_fees_collected": fee_totals["processing_fees_collected"],
        "processing_fees_pending": fee_totals["processing_fees_pending"],
        "processing_fees_loans_paid": fee_totals["processing_fees_loans_paid"],
        "processing_fees_loans_unpaid": fee_totals["processing_fees_loans_unpaid"],
    }
    return render(request, "sacco/finance_dashboard.html", context)


@field_officer_required
def field_dashboard(request: HttpRequest) -> HttpResponse:
    members_incomplete_docs = (
        Member.objects.filter(
            Q(id_card_front__isnull=True) | Q(id_card_front="") |
            Q(id_card_back__isnull=True) | Q(id_card_back="") |
            Q(lc1_letter__isnull=True) | Q(lc1_letter="")
        )
        .annotate(annotated_loan_count=Count("loans"))
        .order_by("-joined_on")[:10]
    )

    recent_members = (
        Member.objects.annotate(annotated_loan_count=Count("loans"))
        .order_by("-joined_on")[:10]
    )

    members_without_loans = (
        Member.objects.annotate(annotated_loan_count=Count("loans"))
        .filter(annotated_loan_count=0)[:10]
    )

    members_with_loans_count = (
        Member.objects.annotate(annotated_loan_count=Count("loans"))
        .filter(annotated_loan_count__gt=0)
        .count()
    )

    my_pending_expenses = Expense.objects.filter(submitted_by=request.user, status=Expense.Status.PENDING)[:5]
    my_recent_expenses = Expense.objects.filter(submitted_by=request.user).order_by("-created")[:5]

    context = {
        "members_incomplete_docs": members_incomplete_docs,
        "recent_members": recent_members,
        "members_without_loans": members_without_loans,
        "total_members": Member.objects.count(),
        "members_with_loans": members_with_loans_count,
        "my_pending_expenses": my_pending_expenses,
        "my_recent_expenses": my_recent_expenses,
    }
    return render(request, "sacco/field_dashboard.html", context)


# =============================================================================
# MEMBER VIEWS — Strong Filtering
# =============================================================================
MEMBER_ORDER_CHOICES = [
    ("name", "Name A–Z"),
    ("-name", "Name Z–A"),
    ("joined", "Joined (Oldest)"),
    ("-joined", "Joined (Newest)"),
    ("loans", "Total Borrowed (Low→High)"),
    ("-loans", "Total Borrowed (High→Low)"),
    ("balance", "Balance (Low→High)"),
    ("-balance", "Balance (High→Low)"),
    ("open", "Open Loans (Low→High)"),
    ("-open", "Open Loans (High→Low)"),
]


@login_required
def member_list(request: HttpRequest) -> HttpResponse:
    q = _clean(request.GET.get("q"))
    subcounty = _clean(request.GET.get("subcounty"))
    village = _clean(request.GET.get("village"))
    has_open_loan = _clean(request.GET.get("has_open_loan"))  # "1" / "0" / ""
    joined_from = _parse_date(request.GET.get("joined_from"))
    joined_to = _parse_date(request.GET.get("joined_to"))
    min_balance = _parse_decimal(request.GET.get("min_balance"))
    max_balance = _parse_decimal(request.GET.get("max_balance"))
    order = _clean(request.GET.get("order")) or "name"
    per_page = _per_page(request, 20)

    qs = annotate_member_latest_loan(annotate_member_totals(Member.objects.all()))

    if q:
        qs = qs.filter(
            Q(member_id__icontains=q)
            | Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(phone__icontains=q)
            | Q(nin__icontains=q)
            | Q(address__icontains=q)
            | Q(village__icontains=q)
            | Q(subcounty__icontains=q)
        )

    if subcounty:
        qs = qs.filter(subcounty__icontains=subcounty)
    if village:
        qs = qs.filter(village__icontains=village)

    if joined_from:
        qs = qs.filter(joined_on__gte=joined_from)
    if joined_to:
        qs = qs.filter(joined_on__lte=joined_to)

    if min_balance is not None:
        qs = qs.filter(balance_a__gte=min_balance)
    if max_balance is not None:
        qs = qs.filter(balance_a__lte=max_balance)

    if has_open_loan == "1":
        qs = qs.filter(open_loans_a__gt=0)
    elif has_open_loan == "0":
        qs = qs.filter(open_loans_a=0)

    sort_map = {
        "name": ["first_name", "last_name", "id"],
        "-name": ["-first_name", "-last_name", "-id"],
        "joined": ["joined_on", "id"],
        "-joined": ["-joined_on", "-id"],
        "loans": ["total_expected_a", "id"],
        "-loans": ["-total_expected_a", "-id"],
        "balance": ["balance_a", "id"],
        "-balance": ["-balance_a", "-id"],
        "open": ["open_loans_a", "id"],
        "-open": ["-open_loans_a", "-id"],
    }
    qs = qs.order_by(*sort_map.get(order, sort_map["name"]))

    page = _paginate(request, qs, per_page)
    base_query = build_base_query(request.GET.dict())

    subcounties = list(
        Member.objects.exclude(subcounty="").values_list("subcounty", flat=True).distinct().order_by("subcounty")[:200]
    )

    return render(
        request,
        "sacco/member_list.html",
        {
            "page": page,
            "q": q,
            "subcounty": subcounty,
            "village": village,
            "has_open_loan": has_open_loan,
            "joined_from": joined_from,
            "joined_to": joined_to,
            "min_balance": min_balance,
            "max_balance": max_balance,
            "order": order,
            "per_page": per_page,
            "PER_PAGE_OPTIONS": PER_PAGE_OPTIONS,
            "ORDER_CHOICES": MEMBER_ORDER_CHOICES,
            "base_query": base_query,
            "subcounties": subcounties,
        },
    )


@login_required
def member_detail(request: HttpRequest, pk: int) -> HttpResponse:
    member = get_object_or_404(Member, pk=pk)

    loans = annotate_loan_totals(member.loans.all()).order_by("-created")
    recent_payments = (
        Payment.objects.filter(loan__member=member)
        .select_related("loan")
        .order_by("-date", "-id")[:10]
    )

    total_expected = sum((loan.expected_total for loan in loans), ZERO)
    total_paid = sum((getattr(loan, "amount_paid_a", ZERO) for loan in loans), ZERO)
    total_balance = total_expected - total_paid

    context = {
        "member": member,
        "loans": loans,
        "recent_payments": recent_payments,
        "total_loans": loans.count(),
        "total_expected": total_expected,
        "total_paid": total_paid,
        "total_balance": total_balance,
        "can_apply_for_loan": True,  # always allow
        "qualification_reason": "",
    }
    return render(request, "sacco/member_detail.html", context)


@field_officer_required
def member_create(request: HttpRequest) -> HttpResponse:
    form = MemberForm(request.POST or None, request.FILES or None)

    if request.method == "POST":
        if form.is_valid():
            member = form.save(commit=False)
            member.created_by = request.user
            member.save()
            messages.success(request, f"Member {member.full_name} created successfully.")
            return redirect(member.get_absolute_url())
        messages.error(request, "Please correct the errors below.")

    return render(request, "sacco/member_form.html", {"form": form, "title": "Create New Member"})


@field_officer_required
def member_update(request: HttpRequest, pk: int) -> HttpResponse:
    member = get_object_or_404(Member, pk=pk)
    form = MemberForm(request.POST or None, request.FILES or None, instance=member)

    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, f"Member {member.full_name} updated successfully.")
            return redirect(member.get_absolute_url())
        messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "sacco/member_form.html",
        {"form": form, "member": member, "title": f"Update {member.full_name}"},
    )


# =============================================================================
# LOAN VIEWS — Strong Filtering
# =============================================================================
LOAN_ORDER_CHOICES = [
    ("-created", "Newest"),
    ("created", "Oldest"),
    ("member", "Member A–Z"),
    ("-member", "Member Z–A"),
    ("principal", "Principal (Low→High)"),
    ("-principal", "Principal (High→Low)"),
    ("expected", "Expected Total (Low→High)"),
    ("-expected", "Expected Total (High→Low)"),
    ("balance", "Balance (Low→High)"),
    ("-balance", "Balance (High→Low)"),
    ("due", "Due Date (Soonest)"),
    ("-due", "Due Date (Latest)"),
]


@login_required
def loan_list(request: HttpRequest) -> HttpResponse:
    today = _today()

    q = _clean(request.GET.get("q"))
    status_filter = _clean(request.GET.get("status"))  # OPEN/CLOSED/overdue/""
    payment_mode_filter = _clean(request.GET.get("payment_mode"))
    fee_paid_filter = _clean(request.GET.get("fee_paid"))  # "1"/"0"/""
    start_from = _parse_date(request.GET.get("start_from"))
    start_to = _parse_date(request.GET.get("start_to"))
    due_from = _parse_date(request.GET.get("due_from"))
    due_to = _parse_date(request.GET.get("due_to"))
    min_balance = _parse_decimal(request.GET.get("min_balance"))
    max_balance = _parse_decimal(request.GET.get("max_balance"))
    order = _clean(request.GET.get("order")) or "-created"
    per_page = _per_page(request, 20)

    qs = annotate_loan_totals(Loan.objects.select_related("member"))

    if q:
        qs = qs.filter(
            Q(loan_id__icontains=q)
            | Q(member__member_id__icontains=q)
            | Q(member__first_name__icontains=q)
            | Q(member__last_name__icontains=q)
            | Q(member__phone__icontains=q)
            | Q(member__nin__icontains=q)
            | Q(note__icontains=q)
        )

    if status_filter:
        if status_filter.lower() == "overdue":
            qs = qs.filter(status=Loan.Status.OPEN, due_date__lt=today, balance_a__gt=ZERO)
        else:
            qs = qs.filter(status=status_filter)

    if payment_mode_filter:
        qs = qs.filter(payment_mode=payment_mode_filter)

    if fee_paid_filter == "1":
        qs = qs.filter(processing_fee_paid=True)
    elif fee_paid_filter == "0":
        qs = qs.filter(processing_fee__gt=ZERO, processing_fee_paid=False)

    if start_from:
        qs = qs.filter(start_date__gte=start_from)
    if start_to:
        qs = qs.filter(start_date__lte=start_to)
    if due_from:
        qs = qs.filter(due_date__gte=due_from)
    if due_to:
        qs = qs.filter(due_date__lte=due_to)

    if min_balance is not None:
        qs = qs.filter(balance_a__gte=min_balance)
    if max_balance is not None:
        qs = qs.filter(balance_a__lte=max_balance)

    sort_map = {
        "-created": ["-created", "-id"],
        "created": ["created", "id"],
        "member": ["member__first_name", "member__last_name", "id"],
        "-member": ["-member__first_name", "-member__last_name", "-id"],
        "principal": ["principal", "id"],
        "-principal": ["-principal", "-id"],
        "expected": ["expected_total", "id"],
        "-expected": ["-expected_total", "-id"],
        "balance": ["balance_a", "id"],
        "-balance": ["-balance_a", "-id"],
        "due": ["due_date", "id"],
        "-due": ["-due_date", "-id"],
    }
    qs = qs.order_by(*sort_map.get(order, sort_map["-created"]))

    page = _paginate(request, qs, per_page)
    base_query = build_base_query(request.GET.dict())

    counts = {
        "OPEN": Loan.objects.filter(status=Loan.Status.OPEN).count(),
        "CLOSED": Loan.objects.filter(status=Loan.Status.CLOSED).count(),
        "OVERDUE": Loan.objects.filter(status=Loan.Status.OPEN, due_date__lt=today).count(),
    }

    return render(
        request,
        "sacco/loan_list.html",
        {
            "page": page,
            "q": q,
            "status": status_filter,
            "payment_mode": payment_mode_filter,
            "fee_paid": fee_paid_filter,
            "start_from": start_from,
            "start_to": start_to,
            "due_from": due_from,
            "due_to": due_to,
            "min_balance": min_balance,
            "max_balance": max_balance,
            "order": order,
            "per_page": per_page,
            "PER_PAGE_OPTIONS": PER_PAGE_OPTIONS,
            "ORDER_CHOICES": LOAN_ORDER_CHOICES,
            "counts": counts,
            "Loan": Loan,
            "base_query": base_query,
        },
    )


@login_required
def loan_detail(request: HttpRequest, pk: int) -> HttpResponse:
    loan = get_object_or_404(Loan.objects.select_related("member"), pk=pk)

    payments_qs = loan.payments.select_related("created_by").order_by("-date", "-id")
    last_payment = payments_qs.first()

    schedule_rows = loan_schedule_rows(loan)

    total_paid: Decimal = loan.amount_paid
    balance: Decimal = loan.balance
    expected_total: Decimal = loan.expected_total
    installment_amount: Decimal = loan.installment_amount or ZERO

    if loan.payment_mode == Loan.PaymentMode.DAILY:
        total_installments = int(loan.period) * 30
    elif loan.payment_mode == Loan.PaymentMode.WEEKLY:
        total_installments = int(loan.period) * 4
    else:
        total_installments = int(loan.period)
    total_installments = max(total_installments, 1)

    paid_installments = min(total_installments, floor(total_paid / installment_amount)) if installment_amount > 0 else 0
    remaining_installments = max(total_installments - paid_installments, 0)

    next_due = next_due_row(loan)
    next_due_date = next_due.get("due_date") if next_due else None
    next_due_amount = next_due.get("amount") if next_due else None

    progress_pct = int(min(100, max(0, (total_paid / expected_total) * 100))) if expected_total > 0 else 0
    today = _today()
    due_soon = bool(next_due_date and next_due_date >= today and (next_due_date - today).days <= 7)

    fee_due = processing_fee_due(loan)

    context = {
        "loan": loan,
        "payments": payments_qs,
        "payment_schedule": schedule_rows,
        "total_paid": total_paid,
        "balance": balance,
        "expected_total": expected_total,
        "installment_amount": installment_amount,
        "progress_pct": progress_pct,
        "total_installments": total_installments,
        "paid_installments": paid_installments,
        "remaining_installments": remaining_installments,
        "next_due_date": next_due_date,
        "next_due_amount": next_due_amount,
        "due_soon": due_soon,
        "last_payment": last_payment,
        "processing_fee_due": fee_due,
        "processing_fee_paid": fee_due == ZERO,
        "can_edit_loan": request.user.is_superuser or request.user.role in [User.Role.FINANCE, User.Role.ADMIN],
        "can_add_payment": _can_record_payments(request.user),  # ✅ FIELD_OFFICER included
        "can_pay_processing_fee": request.user.is_superuser or request.user.role in [User.Role.FINANCE, User.Role.ADMIN],
    }
    return render(request, "sacco/loan_detail.html", context)


@field_officer_required
def loan_create(request: HttpRequest) -> HttpResponse:
    """
    Create a new loan.
    IMPORTANT: expected_total excludes processing_fee.
    """
    form = LoanForm(request.POST or None, request.FILES or None)

    if request.method == "POST":
        if form.is_valid():
            loan = form.save(commit=False)
            loan.created_by = request.user
            loan.save()

            member = loan.member
            fee = getattr(loan, "processing_fee", ZERO) or ZERO
            fee_line = f"Processing fee (pay once before repayments): UGX {fee}. " if fee > ZERO else ""

            sms_msg = (
                f"Dear {member.full_name}, your loan {loan.loan_id} has been created. "
                f"Principal: UGX {loan.principal}. Monthly Rate: {loan.rate}%. "
                f"Repayable (Principal+Interest): UGX {loan.expected_total}. "
                f"Installment: UGX {loan.installment_amount} ({loan.payment_mode}). "
                f"Due date: {loan.due_date}. {fee_line}"
                f"Vaulta Capital Growth."
            )

            ok, resp_text = send_sms(member.phone, sms_msg)
            if ok:
                messages.success(request, f"Loan {loan.loan_id} created successfully. SMS sent to {member.phone}.")
            else:
                messages.warning(request, f"Loan {loan.loan_id} created, but SMS failed: {resp_text}")

            return redirect(loan.get_absolute_url())

        messages.error(request, "Please correct the errors below.")

    return render(request, "sacco/loan_form.html", {"form": form, "title": "Create New Loan"})


@role_required([User.Role.FINANCE, User.Role.FIELD_OFFICER, User.Role.ADMIN])
def loan_update(request: HttpRequest, pk: int) -> HttpResponse:
    loan = get_object_or_404(Loan, pk=pk)
    form = LoanForm(request.POST or None, request.FILES or None, instance=loan)

    if request.method == "POST":
        if form.is_valid():
            updated = form.save()
            messages.success(request, f"Loan {updated.loan_id} updated successfully.")
            return redirect(updated.get_absolute_url())
        messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "sacco/loan_form.html",
        {"form": form, "loan": loan, "title": f"Update Loan {loan.loan_id}"},
    )


@finance_required
def loan_close(request: HttpRequest, pk: int) -> HttpResponse:
    loan = get_object_or_404(Loan, pk=pk)

    if loan.status == Loan.Status.CLOSED:
        messages.warning(request, f"Loan {loan.loan_id} is already closed.")
    elif loan.balance > ZERO:
        messages.error(request, f"Cannot close loan {loan.loan_id} - balance of {loan.balance} remains.")
    else:
        loan.status = Loan.Status.CLOSED
        loan.save(update_fields=["status"])
        messages.success(request, f"Loan {loan.loan_id} has been closed.")

    return redirect(loan.get_absolute_url())


@finance_required
def loan_reopen(request: HttpRequest, pk: int) -> HttpResponse:
    loan = get_object_or_404(Loan, pk=pk)

    if loan.status == Loan.Status.OPEN:
        messages.warning(request, f"Loan {loan.loan_id} is already open.")
    else:
        loan.status = Loan.Status.OPEN
        loan.save(update_fields=["status"])
        messages.success(request, f"Loan {loan.loan_id} has been reopened.")

    return redirect(loan.get_absolute_url())


# =============================================================================
# PROCESSING FEE VIEWS
# =============================================================================
@field_officer_required
def processing_fee_pay(request: HttpRequest, pk: int) -> HttpResponse:
    loan = get_object_or_404(Loan.objects.select_related("member"), pk=pk)
    fee_due = processing_fee_due(loan)

    if fee_due <= ZERO:
        messages.info(request, "This loan has no processing fee due (already paid or fee is zero).")
        return redirect(loan.get_absolute_url())

    form = ProcessingFeePaymentForm(request.POST or None, loan=loan)

    if request.method == "POST":
        if form.is_valid():
            paid_on = form.cleaned_data["paid_on"]
            method = form.cleaned_data["method"]
            receipt = form.cleaned_data.get("receipt", "")
            note = form.cleaned_data.get("note", "")

            loan.processing_fee_paid = True
            loan.processing_fee_paid_on = paid_on
            loan.processing_fee_method = method
            loan.processing_fee_receipt = receipt
            loan.processing_fee_note = note
            loan.save(
                update_fields=[
                    "processing_fee_paid",
                    "processing_fee_paid_on",
                    "processing_fee_method",
                    "processing_fee_receipt",
                    "processing_fee_note",
                ]
            )

            member = loan.member
            sms_msg = (
                f"Dear {member.full_name}, your processing fee for loan {loan.loan_id} has been received. "
                f"Amount: UGX {loan.processing_fee} on {paid_on}. "
                f"Vaulta Capital Growth."
            )
            ok, resp_text = send_sms(member.phone, sms_msg)
            if ok:
                messages.success(request, f"Processing fee marked as paid. SMS sent to {member.phone}.")
            else:
                messages.warning(request, f"Processing fee marked as paid, but SMS failed: {resp_text}")

            return redirect(loan.get_absolute_url())

        messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "sacco/processing_fee_pay.html",
        {"loan": loan, "fee_due": fee_due, "form": form, "title": f"Pay Processing Fee - {loan.loan_id}"},
    )


# =============================================================================
# PAYMENT VIEWS
# =============================================================================
@role_required(PAYMENT_OFFICER_ROLES)
def payment_create(request: HttpRequest, loan_id: int) -> HttpResponse:
    """
    ✅ FIELD_OFFICER + FINANCE + ADMIN can record repayments.
    """
    loan = get_object_or_404(Loan.objects.select_related("member"), pk=loan_id)

    if processing_fee_due(loan) > ZERO:
        messages.error(request, "Processing fee must be paid before receiving repayments.")
        return redirect("sacco:processing_fee_pay", pk=loan.pk)

    due_row = next_due_row(loan)
    suggested_amount = (due_row.get("amount") if due_row else None) or loan.installment_amount
    suggested_due_date = (due_row.get("due_date") if due_row else None) or loan.due_date

    initial = {"loan": loan, "date": _today()}
    if request.method != "POST" and suggested_amount:
        initial["amount"] = suggested_amount

    payment_instance = Payment(loan=loan)
    form = PaymentForm(request.POST or None, instance=payment_instance, initial=initial)

    if request.method == "POST":
        if form.is_valid():
            payment = form.save(commit=False)
            payment.created_by = request.user

            remaining_balance = loan.balance
            if payment.amount > remaining_balance:
                messages.error(request, f"Payment amount ({payment.amount}) exceeds remaining balance ({remaining_balance}).")
            else:
                payment.save()

                member = loan.member
                sms_msg = (
                    f"Dear {member.full_name}, payment received for loan {loan.loan_id}. "
                    f"Amount: UGX {payment.amount} on {payment.date}. "
                    f"Total paid: UGX {loan.amount_paid}. Balance: UGX {loan.balance}. "
                    f"Vaulta Capital Growth."
                )
                ok, resp_text = send_sms(member.phone, sms_msg)
                if ok:
                    messages.success(request, f"Payment recorded. SMS sent to {member.phone}.")
                else:
                    messages.warning(request, f"Payment recorded, but SMS failed: {resp_text}")

                return redirect(loan.get_absolute_url())
        else:
            messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "sacco/payment_form.html",
        {
            "form": form,
            "loan": loan,
            "remaining_balance": loan.balance,
            "title": f"Add Payment for Loan {loan.loan_id}",
            "suggested_amount": suggested_amount,
            "suggested_due_date": suggested_due_date,
        },
    )


@finance_required
def payment_update(request: HttpRequest, pk: int) -> HttpResponse:
    payment = get_object_or_404(Payment.objects.select_related("loan", "loan__member"), pk=pk)
    loan = payment.loan

    if processing_fee_due(loan) > ZERO:
        messages.error(request, "Processing fee must be paid before managing repayments.")
        return redirect("sacco:processing_fee_pay", pk=loan.pk)

    form = PaymentForm(request.POST or None, instance=payment)

    if request.method == "POST":
        if form.is_valid():
            updated_payment = form.save()
            messages.success(request, "Payment updated successfully.")
            return redirect(updated_payment.loan.get_absolute_url())
        messages.error(request, "Please correct the errors below.")

    return render(
        request,
        "sacco/payment_form.html",
        {
            "form": form,
            "loan": loan,
            "title": f"Update Payment for Loan {loan.loan_id}",
            "remaining_balance": loan.balance,
        },
    )


@finance_required
def payment_delete(request: HttpRequest, pk: int) -> HttpResponse:
    payment = get_object_or_404(Payment.objects.select_related("loan"), pk=pk)
    loan = payment.loan

    if request.method == "POST":
        payment.delete()
        messages.success(request, "Payment deleted successfully.")
        return redirect(loan.get_absolute_url())

    return render(request, "sacco/payment_confirm_delete.html", {"payment": payment, "loan": loan})


@finance_required
def loan_payments_inline(request: HttpRequest, pk: int) -> HttpResponse:
    loan = get_object_or_404(Loan, pk=pk)

    if processing_fee_due(loan) > ZERO:
        messages.error(request, "Processing fee must be paid before receiving repayments.")
        return redirect("sacco:processing_fee_pay", pk=loan.pk)

    formset = PaymentFormSet(request.POST or None, instance=loan)

    if request.method == "POST":
        if formset.is_valid():
            formset.save()
            messages.success(request, "Payments updated successfully.")
            return redirect(loan.get_absolute_url())
        messages.error(request, "Please correct the errors below.")

    return render(request, "sacco/loan_payments_inline.html", {"loan": loan, "formset": formset})


# =============================================================================
# EXPENSE VIEWS
# =============================================================================
@login_required
def expense_list(request: HttpRequest) -> HttpResponse:
    form = ExpenseSearchForm(request.GET or None)
    qs = Expense.objects.select_related("submitted_by", "approved_by")

    if not _is_finance_or_admin(request.user):
        qs = qs.filter(submitted_by=request.user)

    if form.is_valid():
        data = form.cleaned_data
        if data.get("status"):
            qs = qs.filter(status=data["status"])
        if data.get("category"):
            qs = qs.filter(category=data["category"])
        if data.get("date_from"):
            qs = qs.filter(date_incurred__gte=data["date_from"])
        if data.get("date_to"):
            qs = qs.filter(date_incurred__lte=data["date_to"])
        if data.get("search"):
            qs = qs.filter(
                Q(title__icontains=data["search"])
                | Q(purpose__icontains=data["search"])
                | Q(expense_id__icontains=data["search"])
            )

    order = _clean(request.GET.get("order")) or "-created"
    sorting_map = {
        "created": ["created"], "-created": ["-created"],
        "amount": ["amount"], "-amount": ["-amount"],
        "date": ["date_incurred"], "-date": ["-date_incurred"],
        "status": ["status"], "-status": ["-status"],
        "category": ["category"], "-category": ["-category"],
    }
    qs = qs.order_by(*sorting_map.get(order, sorting_map["-created"]))
    page = _paginate(request, qs, 20)

    stats_base = Expense.objects.all() if _is_finance_or_admin(request.user) else Expense.objects.filter(submitted_by=request.user)
    expense_stats = stats_base.aggregate(
        total_pending=Count("id", filter=Q(status=Expense.Status.PENDING)),
        total_approved=Count("id", filter=Q(status=Expense.Status.APPROVED)),
        total_rejected=Count("id", filter=Q(status=Expense.Status.REJECTED)),
        total_paid=Count("id", filter=Q(status=Expense.Status.PAID)),
        amount_pending=Coalesce(Sum("amount", filter=Q(status=Expense.Status.PENDING)), Value(ZERO)),
        amount_approved=Coalesce(Sum("amount", filter=Q(status=Expense.Status.APPROVED)), Value(ZERO)),
        amount_paid=Coalesce(Sum("amount", filter=Q(status=Expense.Status.PAID)), Value(ZERO)),
    )

    base_query = build_base_query(request.GET.dict())

    return render(
        request,
        "sacco/expense_list.html",
        {
            "page": page,
            "form": form,
            "order": order,
            "PER_PAGE_OPTIONS": PER_PAGE_OPTIONS,
            "expense_stats": expense_stats,
            "can_approve_expenses": _is_finance_or_admin(request.user),
            "base_query": base_query,
        },
    )


@login_required
def expense_create(request: HttpRequest) -> HttpResponse:
    form = ExpenseForm(request.POST or None, request.FILES or None, user=request.user)

    if request.method == "POST":
        if form.is_valid():
            expense = form.save(commit=False)
            expense.submitted_by = request.user
            expense.save()
            messages.success(request, f"Expense {expense.expense_id} submitted successfully.")
            return redirect(expense.get_absolute_url())
        messages.error(request, "Please correct the errors below.")

    return render(request, "sacco/expense_form.html", {"form": form, "title": "Submit New Expense"})


@login_required
def expense_detail(request: HttpRequest, pk: int) -> HttpResponse:
    expense = get_object_or_404(Expense.objects.select_related("submitted_by", "approved_by"), pk=pk)

    if (not _is_finance_or_admin(request.user)) and expense.submitted_by != request.user:
        messages.error(request, "You can only view your own expenses.")
        return redirect("sacco:expense_list")

    can_edit = expense.can_edit(request.user)
    can_approve = expense.can_approve(request.user) and _is_finance_or_admin(request.user)

    approval_form = None
    if can_approve:
        approval_form = ExpenseApprovalForm(request.POST or None, instance=expense, user=request.user)
        if request.method == "POST" and approval_form.is_valid():
            approval_form.save()
            messages.success(request, f"Expense {expense.expense_id} updated successfully.")
            return redirect(expense.get_absolute_url())

    return render(
        request,
        "sacco/expense_detail.html",
        {"expense": expense, "approval_form": approval_form, "can_edit": can_edit, "can_approve": can_approve},
    )


@login_required
def expense_update(request: HttpRequest, pk: int) -> HttpResponse:
    expense = get_object_or_404(Expense, pk=pk)

    if not _is_finance_or_admin(request.user):
        if expense.submitted_by != request.user:
            messages.error(request, "You can only edit your own expenses.")
            return redirect("sacco:expense_list")
        if expense.status != Expense.Status.PENDING:
            messages.error(request, "You can only edit pending expenses.")
            return redirect(expense.get_absolute_url())

    form = ExpenseForm(request.POST or None, request.FILES or None, instance=expense, user=request.user)

    if request.method == "POST":
        if form.is_valid():
            updated = form.save(commit=False)
            if not _is_finance_or_admin(request.user):
                updated.submitted_by = request.user
            updated.save()
            messages.success(request, f"Expense {updated.expense_id} updated successfully.")
            return redirect(updated.get_absolute_url())
        messages.error(request, "Please correct the errors below.")

    return render(request, "sacco/expense_form.html", {"form": form, "expense": expense, "title": f"Update Expense {expense.expense_id}"})


@finance_required
def expense_approve(request: HttpRequest, pk: int) -> HttpResponse:
    expense = get_object_or_404(Expense, pk=pk)
    if expense.approve(request.user):
        messages.success(request, f"Expense {expense.expense_id} approved successfully.")
    else:
        messages.error(request, "Unable to approve expense. It may have already been processed.")
    return redirect(expense.get_absolute_url())


@finance_required
def expense_reject(request: HttpRequest, pk: int) -> HttpResponse:
    expense = get_object_or_404(Expense, pk=pk)

    if request.method == "POST":
        reason = _clean(request.POST.get("rejection_reason"))
        if not reason:
            messages.error(request, "Rejection reason is required.")
        elif expense.reject(request.user, reason):
            messages.success(request, f"Expense {expense.expense_id} rejected successfully.")
        else:
            messages.error(request, "Unable to reject expense. It may have already been processed.")

    return redirect(expense.get_absolute_url())


@finance_required
def expense_mark_paid(request: HttpRequest, pk: int) -> HttpResponse:
    expense = get_object_or_404(Expense, pk=pk)

    if expense.mark_as_paid():
        messages.success(request, f"Expense {expense.expense_id} marked as paid.")
    else:
        messages.error(request, "Unable to mark expense as paid. It must be approved first.")

    return redirect(expense.get_absolute_url())


@login_required
def expense_reopen(request: HttpRequest, pk: int) -> HttpResponse:
    expense = get_object_or_404(Expense, pk=pk)

    if not _is_finance_or_admin(request.user) and expense.submitted_by != request.user:
        messages.error(request, "You can only manage your own expenses.")
        return redirect("sacco:expense_list")

    if expense.reopen():
        messages.success(request, f"Expense {expense.expense_id} reopened for resubmission.")
    else:
        messages.error(request, "Unable to reopen expense. Only rejected expenses can be reopened.")

    return redirect(expense.get_absolute_url())


# =============================================================================
# API VIEWS (required by urls.py)
# =============================================================================
@require_GET
@login_required
def check_member_loan_eligibility(request: HttpRequest, member_id: int) -> JsonResponse:
    member = get_object_or_404(Member, pk=member_id)
    return JsonResponse({"can_apply": True, "reason": "", "has_open_loan": bool(member.has_open_loan)})


@require_GET
@login_required
def get_loan_calculations(request: HttpRequest) -> JsonResponse:
    """
    Loan preview calculations.
    IMPORTANT:
      - expected_total EXCLUDES processing_fee.
      - rate is MONTHLY to match your model.
    """
    try:
        principal = Decimal(_clean(request.GET.get("principal")) or "0")
        period = int(_clean(request.GET.get("period")) or "1")
        rate = Decimal(_clean(request.GET.get("rate")) or "0")
        payment_mode = _clean(request.GET.get("payment_mode")) or Loan.PaymentMode.MONTHLY
        processing_fee = Decimal(_clean(request.GET.get("processing_fee")) or "0")

        temp_loan = Loan(
            principal=principal,
            period=period,
            rate=rate,
            payment_mode=payment_mode,
            processing_fee=processing_fee,
        )

        expected_total = temp_loan.calculate_expected_total()
        installment_amount = temp_loan.calculate_installment_amount()
        total_interest = temp_loan.calculate_interest_amount()

        return JsonResponse(
            {
                "success": True,
                "expected_total": str(expected_total),
                "installment_amount": str(installment_amount),
                "total_interest": str(total_interest),
                "processing_fee": str(processing_fee),
            }
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})


# =============================================================================
# REPORTS / STATEMENTS
# =============================================================================
@login_required
def reports_home(request: HttpRequest) -> HttpResponse:
    return render(request, "sacco/reports_home.html")


@login_required
def general_report(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=6)

    member_stats = Member.objects.aggregate(
        total_members=Count("id"),
        members_with_loans=Count("id", filter=Q(loans__isnull=False), distinct=True),
        members_without_loans=Count("id", filter=Q(loans__isnull=True), distinct=True),
    )

    loan_stats = Loan.objects.aggregate(
        total_loans=Count("id"),
        total_principal=Coalesce(Sum("principal"), Value(ZERO)),
        total_expected=Coalesce(Sum("expected_total"), Value(ZERO)),
        total_paid=Coalesce(Sum("payments__amount"), Value(ZERO)),
        average_loan_size=Coalesce(Avg("principal"), Value(ZERO)),
        open_loans=Count("id", filter=Q(status=Loan.Status.OPEN)),
        closed_loans=Count("id", filter=Q(status=Loan.Status.CLOSED)),
        overdue_loans=Count("id", filter=Q(status=Loan.Status.OPEN, due_date__lt=today)),
        due_today_loans=Count("id", filter=Q(status=Loan.Status.OPEN, due_date=today)),
    )

    total_balance = (loan_stats["total_expected"] or ZERO) - (loan_stats["total_paid"] or ZERO)

    collection_stats = Payment.objects.aggregate(
        total_collections=Coalesce(Sum("amount"), Value(ZERO)),
        payments_count=Count("id"),
        average_payment=Coalesce(Avg("amount"), Value(ZERO)),
        today_collections=Coalesce(Sum("amount", filter=Q(date=today)), Value(ZERO)),
        today_payments=Count("id", filter=Q(date=today)),
        week_collections=Coalesce(Sum("amount", filter=Q(date__gte=week_start, date__lte=today)), Value(ZERO)),
        week_payments=Count("id", filter=Q(date__gte=week_start, date__lte=today)),
        month_collections=Coalesce(Sum("amount", filter=Q(date__gte=month_start, date__lte=today)), Value(ZERO)),
        month_payments=Count("id", filter=Q(date__gte=month_start, date__lte=today)),
    )

    expense_stats = Expense.objects.aggregate(
        total_expenses=Count("id"),
        pending_expenses=Count("id", filter=Q(status=Expense.Status.PENDING)),
        approved_expenses=Count("id", filter=Q(status=Expense.Status.APPROVED)),
        paid_expenses=Count("id", filter=Q(status=Expense.Status.PAID)),
        rejected_expenses=Count("id", filter=Q(status=Expense.Status.REJECTED)),
        total_expense_amount=Coalesce(Sum("amount"), Value(ZERO)),
        pending_expense_amount=Coalesce(Sum("amount", filter=Q(status=Expense.Status.PENDING)), Value(ZERO)),
        approved_expense_amount=Coalesce(Sum("amount", filter=Q(status=Expense.Status.APPROVED)), Value(ZERO)),
        paid_expense_amount=Coalesce(Sum("amount", filter=Q(status=Expense.Status.PAID)), Value(ZERO)),
        rejected_expense_amount=Coalesce(Sum("amount", filter=Q(status=Expense.Status.REJECTED)), Value(ZERO)),
    )

    overdue_loans_qs = (
        Loan.objects.filter(status=Loan.Status.OPEN, due_date__lt=today)
        .annotate(amount_paid_a=Coalesce(Sum("payments__amount"), Value(ZERO)))
        .annotate(balance_a=F("expected_total") - F("amount_paid_a"))
        .filter(balance_a__gt=ZERO)
        .select_related("member")
        .order_by("due_date")
    )

    overdue_summary = overdue_loans_qs.aggregate(
        overdue_count=Count("id"),
        overdue_expected=Coalesce(Sum("expected_total"), Value(ZERO)),
        overdue_paid=Coalesce(Sum("amount_paid_a"), Value(ZERO)),
        overdue_balance=Coalesce(Sum("balance_a"), Value(ZERO)),
    )

    total_expected = loan_stats["total_expected"] or ZERO
    total_paid = loan_stats["total_paid"] or ZERO
    total_expenses = expense_stats["total_expense_amount"] or ZERO
    overdue_balance = overdue_summary["overdue_balance"] or ZERO

    collection_rate = Decimal("0.00")
    overdue_rate = Decimal("0.00")
    expense_to_collection_rate = Decimal("0.00")
    recovery_gap = Decimal("0.00")
    net_cash_position = total_paid - total_expenses

    if total_expected > 0:
        collection_rate = (total_paid / total_expected) * Decimal("100")
        overdue_rate = (overdue_balance / total_expected) * Decimal("100")
        recovery_gap = ((total_expected - total_paid) / total_expected) * Decimal("100")

    if total_paid > 0:
        expense_to_collection_rate = (total_expenses / total_paid) * Decimal("100")

    top_members = (
        Member.objects.annotate(
            loan_count=Count("loans", distinct=True),
            total_borrowed=Coalesce(Sum("loans__principal"), Value(ZERO)),
            total_expected=Coalesce(Sum("loans__expected_total"), Value(ZERO)),
            total_paid=Coalesce(Sum("loans__payments__amount"), Value(ZERO)),
        )
        .annotate(total_balance=F("total_expected") - F("total_paid"))
        .filter(loan_count__gt=0)
        .order_by("-total_borrowed", "-loan_count")[:10]
    )

    recent_payments = Payment.objects.select_related("loan", "loan__member").order_by("-date", "-id")[:10]
    recent_loans = Loan.objects.select_related("member").order_by("-start_date", "-id")[:10]
    recent_expenses = Expense.objects.select_related("submitted_by", "approved_by").order_by("-date_incurred", "-id")[:10]

    context = {
        "today": today,
        "month_start": month_start,
        "week_start": week_start,
        "member_count": member_stats["total_members"],
        "member_stats": member_stats,
        "loan_stats": loan_stats,
        "expense_stats": expense_stats,
        "collection_stats": collection_stats,
        "total_balance": total_balance,
        "overdue_summary": overdue_summary,
        "collection_rate": collection_rate,
        "overdue_rate": overdue_rate,
        "expense_to_collection_rate": expense_to_collection_rate,
        "recovery_gap": recovery_gap,
        "net_cash_position": net_cash_position,
        "top_members": top_members,
        "recent_payments": recent_payments,
        "recent_loans": recent_loans,
        "recent_expenses": recent_expenses,
        "overdue_loans": overdue_loans_qs[:10],
    }
    return render(request, "sacco/reports/general_report.html", context)


@login_required
def collections_report(request: HttpRequest) -> HttpResponse:
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")

    payments = Payment.objects.select_related("loan", "loan__member").all()

    if date_from:
        payments = payments.filter(date__gte=date_from)
    if date_to:
        payments = payments.filter(date__lte=date_to)

    totals = payments.aggregate(
        total_payments=Count("id"),
        total_amount=Coalesce(Sum("amount"), Value(ZERO)),
    )

    by_method = (
        payments.values("method")
        .annotate(total=Coalesce(Sum("amount"), Value(ZERO)), count=Count("id"))
        .order_by("-total")
    )

    by_day = (
        payments.values("date")
        .annotate(total=Coalesce(Sum("amount"), Value(ZERO)), count=Count("id"))
        .order_by("-date")
    )

    context = {
        "payments": payments.order_by("-date", "-id"),
        "totals": totals,
        "by_method": by_method,
        "by_day": by_day,
        "date_from": date_from,
        "date_to": date_to,
    }
    return render(request, "sacco/reports/collections_report.html", context)


@login_required
def arrears_report(request: HttpRequest) -> HttpResponse:
    today = timezone.localdate()

    loans = (
        Loan.objects.select_related("member")
        .filter(status=Loan.Status.OPEN, due_date__lt=today)
        .annotate(amount_paid_a=Coalesce(Sum("payments__amount"), Value(ZERO)))
        .annotate(balance_a=F("expected_total") - F("amount_paid_a"))
        .filter(balance_a__gt=ZERO)
        .order_by("due_date")
    )

    rows = []
    for loan in loans:
        days_overdue = (today - loan.due_date).days if loan.due_date else 0
        rows.append({"loan": loan, "days_overdue": days_overdue})

    context = {"rows": rows, "today": today}
    return render(request, "sacco/reports/arrears_report.html", context)


@login_required
def expenses_report(request: HttpRequest) -> HttpResponse:
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    status = request.GET.get("status")
    category = request.GET.get("category")

    expenses = Expense.objects.select_related("submitted_by", "approved_by").all()

    if date_from:
        expenses = expenses.filter(date_incurred__gte=date_from)
    if date_to:
        expenses = expenses.filter(date_incurred__lte=date_to)
    if status:
        expenses = expenses.filter(status=status)
    if category:
        expenses = expenses.filter(category=category)

    totals = expenses.aggregate(
        total_count=Count("id"),
        total_amount=Coalesce(Sum("amount"), Value(ZERO)),
        pending_amount=Coalesce(Sum("amount", filter=Q(status=Expense.Status.PENDING)), Value(ZERO)),
        approved_amount=Coalesce(Sum("amount", filter=Q(status=Expense.Status.APPROVED)), Value(ZERO)),
        paid_amount=Coalesce(Sum("amount", filter=Q(status=Expense.Status.PAID)), Value(ZERO)),
        rejected_amount=Coalesce(Sum("amount", filter=Q(status=Expense.Status.REJECTED)), Value(ZERO)),
    )

    context = {
        "expenses": expenses.order_by("-date_incurred", "-id"),
        "totals": totals,
        "date_from": date_from,
        "date_to": date_to,
        "status": status,
        "category": category,
        "status_choices": Expense.Status.choices,
        "category_choices": Expense.Category.choices,
    }
    return render(request, "sacco/reports/expenses_report.html", context)


@login_required
def member_statement(request: HttpRequest, pk: int) -> HttpResponse:
    member = get_object_or_404(Member, pk=pk)

    loans = (
        member.loans.all()
        .annotate(amount_paid_a=Coalesce(Sum("payments__amount"), Value(ZERO)))
        .annotate(balance_a=F("expected_total") - F("amount_paid_a"))
        .order_by("-created")
    )

    payments = (
        Payment.objects.filter(loan__member=member)
        .select_related("loan")
        .order_by("-date", "-id")
    )

    summary = {
        "total_loans": loans.count(),
        "total_borrowed": sum((loan.principal for loan in loans), ZERO),
        "total_expected": sum((loan.expected_total for loan in loans), ZERO),
        "total_paid": sum((loan.amount_paid_a for loan in loans), ZERO),
        "total_balance": sum((loan.balance_a for loan in loans), ZERO),
    }

    context = {
        "member": member,
        "loans": loans,
        "payments": payments,
        "summary": summary,
    }
    return render(request, "sacco/statements/member_statement.html", context)


@login_required
def loan_statement(request: HttpRequest, pk: int) -> HttpResponse:
    loan = get_object_or_404(Loan.objects.select_related("member"), pk=pk)
    payments = loan.payments.order_by("-date", "-id")

    context = {
        "loan": loan,
        "member": loan.member,
        "payments": payments,
        "amount_paid": loan.amount_paid,
        "balance": loan.balance,
        "processing_fee_due": loan.processing_fee_due,
    }
    return render(request, "sacco/statements/loan_statement.html", context)
