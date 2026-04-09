from __future__ import annotations

from django.urls import path
from . import views

app_name = "sacco"

urlpatterns = [
    # =====================================================
    # Dashboards
    # =====================================================
    path("", views.dashboard, name="dashboard"),
    path("finance/", views.finance_dashboard, name="finance_dashboard"),
    path("field/", views.field_dashboard, name="field_dashboard"),

    # =====================================================
    # Members
    # =====================================================
    path("members/", views.member_list, name="member_list"),
    path("members/new/", views.member_create, name="member_create"),
    path("members/<int:pk>/", views.member_detail, name="member_detail"),
    path("members/<int:pk>/edit/", views.member_update, name="member_update"),
    path("members/<int:pk>/statement/", views.member_statement, name="member_statement"),

    # =====================================================
    # Loans
    # =====================================================
    path("loans/", views.loan_list, name="loan_list"),
    path("loans/new/", views.loan_create, name="loan_create"),
    path("loans/<int:pk>/", views.loan_detail, name="loan_detail"),
    path("loans/<int:pk>/edit/", views.loan_update, name="loan_update"),
    path("loans/<int:pk>/close/", views.loan_close, name="loan_close"),
    path("loans/<int:pk>/reopen/", views.loan_reopen, name="loan_reopen"),
    path("loans/<int:pk>/statement/", views.loan_statement, name="loan_statement"),

    # =====================================================
    # Processing Fee
    # =====================================================
    path("loans/<int:pk>/processing-fee/pay/", views.processing_fee_pay, name="processing_fee_pay"),

    # =====================================================
    # Payments
    # =====================================================
    path("loans/<int:loan_id>/payments/new/", views.payment_create, name="payment_create"),
    path("payments/<int:pk>/edit/", views.payment_update, name="payment_update"),
    path("payments/<int:pk>/delete/", views.payment_delete, name="payment_delete"),
    path("loans/<int:pk>/payments/inline/", views.loan_payments_inline, name="loan_payments_inline"),

    # =====================================================
    # Expenses
    # =====================================================
    path("expenses/", views.expense_list, name="expense_list"),
    path("expenses/new/", views.expense_create, name="expense_create"),
    path("expenses/<int:pk>/", views.expense_detail, name="expense_detail"),
    path("expenses/<int:pk>/edit/", views.expense_update, name="expense_update"),
    path("expenses/<int:pk>/approve/", views.expense_approve, name="expense_approve"),
    path("expenses/<int:pk>/reject/", views.expense_reject, name="expense_reject"),
    path("expenses/<int:pk>/mark-paid/", views.expense_mark_paid, name="expense_mark_paid"),
    path("expenses/<int:pk>/reopen/", views.expense_reopen, name="expense_reopen"),

    # =====================================================
    # Reports
    # =====================================================
    path("reports/", views.reports_home, name="reports_home"),
    path("reports/general/", views.general_report, name="general_report"),
    path("reports/collections/", views.collections_report, name="collections_report"),
    path("reports/arrears/", views.arrears_report, name="arrears_report"),
    path("reports/expenses/", views.expenses_report, name="expenses_report"),

    # =====================================================
    # CSV Exports (Finance only)
    # =====================================================
    path("export/", views.export_page, name="export_page"),  # ← THIS MUST BE PRESENT
    path("export/members/csv/", views.export_members_csv, name="export_members_csv"),
    path("export/loans/csv/", views.export_loans_csv, name="export_loans_csv"),
    path("export/payments/csv/", views.export_payments_csv, name="export_payments_csv"),
    path("export/expenses/csv/", views.export_expenses_csv, name="export_expenses_csv"),

    # =====================================================
    # API endpoints
    # =====================================================
    path(
        "api/members/<int:member_id>/loan-eligibility/",
        views.check_member_loan_eligibility,
        name="api_member_loan_eligibility",
    ),
    path(
        "api/loan-calculations/",
        views.get_loan_calculations,
        name="api_loan_calculations",
    ),
]