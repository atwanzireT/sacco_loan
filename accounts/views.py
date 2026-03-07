from __future__ import annotations
from django.contrib.auth.views import LoginView, LogoutView
from django.views.generic import CreateView
from django.urls import reverse_lazy
from django.contrib import messages
from django.views.decorators.csrf import csrf_protect
from django.utils.decorators import method_decorator
from .forms import LoginForm, CustomUserCreationForm
from .models import User


class AuthLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def get_success_url(self):
        user: User = self.request.user  # type: ignore
        if user.is_superuser or user.role == User.Role.ADMIN:
            return reverse_lazy("sacco:dashboard")
        if user.role == User.Role.FINANCE:
            return reverse_lazy("sacco:finance_dashboard")
        if user.role == User.Role.FIELD_OFFICER:
            return reverse_lazy("sacco:field_dashboard")
        return reverse_lazy("sacco:dashboard")


@method_decorator(csrf_protect, name='dispatch')
class AuthLogoutView(LogoutView):
    next_page = reverse_lazy("accounts:login")
    
    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        # Add success message after logout
        messages.success(request, "You have been successfully logged out.")
        return response


class RegisterView(CreateView):
    form_class = CustomUserCreationForm
    template_name = "accounts/register.html"
    success_url = reverse_lazy("accounts:login")

    def form_valid(self, form):
        messages.success(self.request, "Account created. You can log in now.")
        return super().form_valid(form)