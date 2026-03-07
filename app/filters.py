from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlencode

from django.http import HttpRequest
from django.utils import timezone


ZERO = Decimal("0.00")


def _clean(s: str | None) -> str:
    return (s or "").strip()


def parse_int(v: str | None, default: int | None = None) -> int | None:
    v = _clean(v)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def parse_decimal(v: str | None, default: Decimal | None = None) -> Decimal | None:
    v = _clean(v)
    if not v:
        return default
    try:
        return Decimal(v)
    except (InvalidOperation, ValueError):
        return default


def parse_date(v: str | None) -> Optional[date]:
    v = _clean(v)
    if not v:
        return None
    try:
        # expects YYYY-MM-DD (HTML date input)
        return date.fromisoformat(v)
    except ValueError:
        return None


def build_base_query(params: dict) -> str:
    """
    For pagination links: "?{base_query}&page=2"
    Removes empty values and 'page'.
    """
    cleaned = {}
    for k, val in params.items():
        if k == "page":
            continue
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        cleaned[k] = val
    return urlencode(cleaned)


@dataclass(frozen=True)
class MemberFilter:
    q: str
    subcounty: str
    village: str
    has_open_loan: str  # "1" / "0" / ""
    min_balance: Optional[Decimal]
    max_balance: Optional[Decimal]
    joined_from: Optional[date]
    joined_to: Optional[date]
    order: str
    per_page: int

    @classmethod
    def from_request(cls, request: HttpRequest, *, per_page_default: int = 20) -> "MemberFilter":
        q = _clean(request.GET.get("q"))
        subcounty = _clean(request.GET.get("subcounty"))
        village = _clean(request.GET.get("village"))
        has_open_loan = _clean(request.GET.get("has_open_loan"))
        min_balance = parse_decimal(request.GET.get("min_balance"))
        max_balance = parse_decimal(request.GET.get("max_balance"))
        joined_from = parse_date(request.GET.get("joined_from"))
        joined_to = parse_date(request.GET.get("joined_to"))
        order = _clean(request.GET.get("order")) or "name"
        per_page = parse_int(request.GET.get("per_page"), per_page_default) or per_page_default
        return cls(
            q=q,
            subcounty=subcounty,
            village=village,
            has_open_loan=has_open_loan,
            min_balance=min_balance,
            max_balance=max_balance,
            joined_from=joined_from,
            joined_to=joined_to,
            order=order,
            per_page=per_page,
        )


@dataclass(frozen=True)
class LoanFilter:
    q: str
    status: str  # OPEN/CLOSED/overdue/""  (we treat overdue as a special computed filter)
    payment_mode: str
    fee_paid: str  # "1"/"0"/""
    min_balance: Optional[Decimal]
    max_balance: Optional[Decimal]
    start_from: Optional[date]
    start_to: Optional[date]
    due_from: Optional[date]
    due_to: Optional[date]
    order: str
    per_page: int

    @classmethod
    def from_request(cls, request: HttpRequest, *, per_page_default: int = 20) -> "LoanFilter":
        q = _clean(request.GET.get("q"))
        status = _clean(request.GET.get("status"))
        payment_mode = _clean(request.GET.get("payment_mode"))
        fee_paid = _clean(request.GET.get("fee_paid"))
        min_balance = parse_decimal(request.GET.get("min_balance"))
        max_balance = parse_decimal(request.GET.get("max_balance"))
        start_from = parse_date(request.GET.get("start_from"))
        start_to = parse_date(request.GET.get("start_to"))
        due_from = parse_date(request.GET.get("due_from"))
        due_to = parse_date(request.GET.get("due_to"))
        order = _clean(request.GET.get("order")) or "-created"
        per_page = parse_int(request.GET.get("per_page"), per_page_default) or per_page_default
        return cls(
            q=q,
            status=status,
            payment_mode=payment_mode,
            fee_paid=fee_paid,
            min_balance=min_balance,
            max_balance=max_balance,
            start_from=start_from,
            start_to=start_to,
            due_from=due_from,
            due_to=due_to,
            order=order,
            per_page=per_page,
        )


def today_local() -> date:
    return timezone.localdate()
