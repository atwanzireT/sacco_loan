from __future__ import annotations
from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    UserCreationForm,
    UserChangeForm,
)
from .models import User


TAILWIND_INPUT = "mt-1 block w-full rounded-xl border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-emerald-500"
TAILWIND_SELECT = "mt-1 block w-full rounded-xl border border-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-emerald-500 bg-white"
TAILWIND_CHECK = "h-4 w-4 rounded border-gray-300 text-emerald-600 focus:ring-emerald-500"

class TailwindFormMixin:
    """Add Tailwind classes to all fields by type."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            w = field.widget
            if isinstance(w, (forms.TextInput, forms.EmailInput, forms.PasswordInput, forms.NumberInput)):
                w.attrs["class"] = (w.attrs.get("class", "") + " " + TAILWIND_INPUT).strip()
            elif isinstance(w, (forms.Select,)):
                w.attrs["class"] = (w.attrs.get("class", "") + " " + TAILWIND_SELECT).strip()
            elif isinstance(w, (forms.CheckboxInput,)):
                w.attrs["class"] = (w.attrs.get("class", "") + " " + TAILWIND_CHECK).strip()


class LoginForm(TailwindFormMixin, AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={"placeholder": "Username"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Password"}))


class CustomUserCreationForm(TailwindFormMixin, UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email", "role", "phone")
        widgets = {
            "username": forms.TextInput(attrs={"placeholder": "Username"}),
            "email": forms.EmailInput(attrs={"placeholder": "you@example.com"}),
            "phone": forms.TextInput(attrs={"placeholder": "07… / +256…"}),
        }


class CustomUserChangeForm(TailwindFormMixin, UserChangeForm):
    class Meta:
        model = User
        fields = ("username", "email", "role", "phone", "first_name", "last_name", "is_active")
