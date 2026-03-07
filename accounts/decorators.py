from __future__ import annotations
from functools import wraps
from typing import Iterable
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponseForbidden
from .models import User

def role_required(allowed: Iterable[str]):
    def deco(view):
        @login_required
        @wraps(view)
        def _w(request: HttpRequest, *a, **kw):
            u: User = request.user  # type: ignore
            if u.is_superuser or u.role in allowed:
                return view(request, *a, **kw)
            return HttpResponseForbidden("You don't have permission to access this page.")
        return _w
    return deco

# Shortcuts
finance_required = role_required([User.Role.FINANCE, User.Role.ADMIN])
field_officer_required = role_required([User.Role.FIELD_OFFICER, User.Role.ADMIN])
