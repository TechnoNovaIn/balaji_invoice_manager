import math
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from django.views.generic import CreateView, ListView, View

from apps.accounts.forms import (
    AdminResetPasswordForm,
    CustomUserCreationForm,
    ExternalUserEditForm,
    LoginForm,
    ProfileUpdateForm,
)
from apps.accounts.models import AccountStatus, AuditLog, User, UserRole
from apps.accounts.permissions import admin_required
from apps.accounts.security import (
    check_login_rate_limit,
    clear_login_rate_limit,
    get_client_ip,
    get_session_expiry_for_user,
    invalidate_user_sessions,
    record_audit_log,
    record_login_failure,
)


class UserLoginView(LoginView):
    """
    Hardened Login view with account+IP rate limiting, temporary cooldowns,
    session fixation cycling, role-based session expiry, and audit logging.
    """
    form_class = LoginForm
    template_name = 'accounts/login.html'
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        attempted_username = request.POST.get('username', '').strip()
        is_blocked, retry_after = check_login_rate_limit(request, attempted_username)
        if is_blocked:
            wait_mins = max(1, math.ceil(retry_after / 60))
            record_audit_log(
                None,
                "USER_LOGIN_RATE_LIMITED",
                request=request,
                details={
                    "attempted_username": attempted_username or "unknown",
                    "retry_after_seconds": retry_after,
                },
            )
            form = self.get_form()
            form.add_error(
                None,
                f"Too many failed sign-in attempts. Please wait {wait_mins} minute(s) before trying again.",
            )
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        if not user.is_operational_active:
            messages.error(
                self.request,
                "Your account is deactivated or suspended. Please contact the administrator.",
            )
            record_audit_log(
                user,
                "USER_LOGIN_BLOCKED_INACTIVE",
                request=self.request,
                details={"username": user.username, "account_status": user.account_status},
            )
            return self.render_to_response(self.get_context_data(form=form), status=200)

        login(self.request, user)
        clear_login_rate_limit(self.request, user.username)
        attempted = self.request.POST.get('username', '').strip()
        if attempted and attempted.lower() != user.username.lower():
            clear_login_rate_limit(self.request, attempted)

        remember_me = form.cleaned_data.get('remember_me', True)
        expiry_seconds = get_session_expiry_for_user(user, remember_me)
        self.request.session.set_expiry(expiry_seconds)

        record_audit_log(
            user,
            "USER_LOGIN_SUCCESS",
            request=self.request,
            details={
                "username": user.username,
                "role": user.role,
                "remember_me": remember_me,
                "session_expiry_seconds": expiry_seconds,
            },
        )

        if user.must_change_password:
            messages.info(
                self.request,
                "Welcome to Balaji InvoiceHub! Please set your personal password now so you can access your account securely.",
            )
            return redirect('accounts:change_password')

        messages.success(self.request, f"Welcome back, {user.first_name or user.username}!")
        return redirect('core:dashboard_router')

    def form_invalid(self, form):
        username = form.data.get('username', 'unknown').strip()
        cooldown_triggered, retry_after = record_login_failure(self.request, username)
        record_audit_log(
            None,
            "USER_LOGIN_FAILED",
            request=self.request,
            details={
                "attempted_username": username,
                "cooldown_triggered": cooldown_triggered,
                "retry_after_seconds": retry_after,
            },
        )
        if cooldown_triggered:
            wait_mins = max(1, math.ceil(retry_after / 60))
            form.add_error(
                None,
                f"Too many failed sign-in attempts. Please wait {wait_mins} minute(s) before trying again.",
            )
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().form_invalid(form)


@method_decorator(csrf_protect, name='dispatch')
class UserLogoutView(View):
    """
    Secure POST-only Logout flushing session and recording audit trail.
    """
    def post(self, request):
        return self._do_logout(request)

    def _do_logout(self, request):
        if request.user.is_authenticated:
            record_audit_log(
                request.user,
                "USER_LOGOUT",
                request=request,
                details={"username": request.user.username},
            )
        logout(request)
        messages.info(request, "You have been logged out securely.")
        return redirect('accounts:login')


@login_required
def profile_view(request):
    """
    Displays current user's profile and active permissions, and allows updating
    only permitted self-service fields (never role, status, or permissions).
    """
    if request.method == 'POST':
        form = ProfileUpdateForm(request.POST, instance=request.user)
        if form.is_valid():
            changed = list(form.changed_data)
            form.save()
            if changed:
                record_audit_log(
                    request.user,
                    "USER_PROFILE_UPDATED",
                    request=request,
                    details={"username": request.user.username, "updated_fields": changed},
                )
            messages.success(request, "Your profile details have been updated.")
            return redirect('accounts:profile')
    else:
        form = ProfileUpdateForm(instance=request.user)

    return render(request, 'accounts/profile.html', {
        'user': request.user,
        'profile_form': form,
    })


@login_required
def change_password_view(request):
    """
    Password setup and change view enforcing Django password validators.
    """
    user = request.user
    is_first_time = user.must_change_password or (user.is_submitter and user.check_password('BalajiEmp@2026'))

    if request.method == 'POST':
        new_password = request.POST.get('new_password', '').strip()
        confirm_password = request.POST.get('confirm_password', '').strip()

        if new_password != confirm_password:
            messages.error(request, "Passwords do not match. Please re-enter carefully.")
            return render(request, 'accounts/change_password.html', {'is_first_time': is_first_time})

        if new_password == 'BalajiEmp@2026':
            messages.error(request, "Please choose a personal password different from the initial demo password.")
            return render(request, 'accounts/change_password.html', {'is_first_time': is_first_time})

        try:
            validate_password(new_password, user=user)
        except ValidationError as exc:
            for err_msg in exc.messages:
                messages.error(request, err_msg)
            return render(request, 'accounts/change_password.html', {'is_first_time': is_first_time})

        user.set_password(new_password)
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])

        update_session_auth_hash(request, user)

        record_audit_log(
            user,
            "USER_PASSWORD_CHANGE",
            request=request,
            details={"username": user.username},
        )

        messages.success(request, "✓ Password successfully saved! Your account security has been updated.")
        return redirect('core:dashboard_router')

    return render(request, 'accounts/change_password.html', {
        'is_first_time': is_first_time,
        'page_title': "Set Your Personal Password" if is_first_time else "Change Password",
    })


@method_decorator(admin_required, name='dispatch')
class UserManagementListView(ListView):
    """
    Admin-only user and external submitter account management console.
    """
    model = User
    template_name = 'accounts/user_list.html'
    context_object_name = 'users_list'
    paginate_by = 30

    def get_queryset(self):
        qs = (
            User.objects.select_related('department')
            .annotate(submission_count=Count('invoice_submissions'))
            .order_by('role', 'username')
        )
        q = self.request.GET.get('q', '').strip()
        role_filter = self.request.GET.get('role', '').strip()
        status_filter = self.request.GET.get('status', '').strip()
        if q:
            qs = qs.filter(
                Q(username__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(email__icontains=q)
                | Q(phone_number__icontains=q)
            )
        if role_filter:
            qs = qs.filter(role=role_filter)
        if status_filter:
            qs = qs.filter(account_status=status_filter)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['total_users_count'] = User.objects.count()
        ctx['external_count'] = User.objects.filter(role__in=[UserRole.EXTERNAL_SUBMITTER, UserRole.EMPLOYEE]).count()
        ctx['active_count'] = User.objects.filter(is_active=True, account_status=AccountStatus.ACTIVE).count()
        ctx['suspended_count'] = User.objects.filter(account_status=AccountStatus.SUSPENDED).count()
        ctx['q'] = self.request.GET.get('q', '').strip()
        ctx['role_filter'] = self.request.GET.get('role', '').strip()
        ctx['status_filter'] = self.request.GET.get('status', '').strip()
        return ctx


@method_decorator(admin_required, name='dispatch')
class UserCreateView(CreateView):
    """
    Admin-only external submitter / employee user creation.
    """
    model = User
    form_class = CustomUserCreationForm
    template_name = 'accounts/user_form.html'
    success_url = reverse_lazy('accounts:user_list')

    def form_valid(self, form):
        response = super().form_valid(form)
        record_audit_log(
            self.request.user,
            "ADMIN_CREATE_USER",
            request=self.request,
            details={
                "created_user": self.object.username,
                "role": self.object.role,
                "can_upload_invoices": self.object.can_upload_invoices,
                "can_resubmit_invoices": self.object.can_resubmit_invoices,
            },
        )
        messages.success(self.request, f"User account '{self.object.username}' created successfully.")
        return response


@admin_required
def edit_user_view(request, user_id):
    """
    Admin-only view to edit an external/employee user's profile details, status,
    and submitter permissions.
    """
    target_user = get_object_or_404(User, id=user_id)
    if target_user == request.user and not request.user.is_superuser:
        # Prevent non-superuser admin from locking themselves out via edit form
        messages.info(request, "Use 'My Profile' to update your own contact details.")
        return redirect('accounts:user_list')

    if target_user.is_privileged_user and target_user != request.user and not request.user.is_superuser:
        raise PermissionDenied("Privileged role settings cannot be modified here.")

    old_snapshot = {
        'account_status': target_user.account_status,
        'can_upload_invoices': target_user.can_upload_invoices,
        'can_resubmit_invoices': target_user.can_resubmit_invoices,
    }

    if request.method == 'POST':
        form = ExternalUserEditForm(request.POST, instance=target_user)
        if form.is_valid():
            updated_user = form.save()
            changed = list(form.changed_data)
            if updated_user.account_status != AccountStatus.ACTIVE:
                invalidate_user_sessions(updated_user)

            perm_changed = (
                old_snapshot['can_upload_invoices'] != updated_user.can_upload_invoices
                or old_snapshot['can_resubmit_invoices'] != updated_user.can_resubmit_invoices
            )
            record_audit_log(
                request.user,
                "ADMIN_PERMISSION_CHANGE" if perm_changed else "ADMIN_UPDATE_USER",
                request=request,
                details={
                    "target_user": updated_user.username,
                    "changed_fields": changed,
                    "account_status": updated_user.account_status,
                    "can_upload_invoices": updated_user.can_upload_invoices,
                    "can_resubmit_invoices": updated_user.can_resubmit_invoices,
                },
            )
            messages.success(request, f"Updated account details and permissions for '{updated_user.username}'.")
            return redirect('accounts:user_list')
    else:
        form = ExternalUserEditForm(instance=target_user)

    return render(request, 'accounts/user_edit.html', {
        'form': form,
        'target_user': target_user,
        'page_title': f"Edit User - {target_user.username}",
    })


@admin_required
def toggle_user_active(request, user_id):
    """
    Admin-only activation/deactivation toggle for user accounts.
    Immediately revokes active sessions on deactivation.
    """
    if request.method == 'POST':
        user_to_toggle = get_object_or_404(User, id=user_id)
        if user_to_toggle == request.user:
            messages.error(request, "You cannot deactivate your own administrative account.")
            return redirect('accounts:user_list')

        activating = not user_to_toggle.is_operational_active
        if activating:
            user_to_toggle.is_active = True
            user_to_toggle.account_status = AccountStatus.ACTIVE
            user_to_toggle.suspended_at = None
            user_to_toggle.suspended_reason = ''
            state = "activated"
        else:
            user_to_toggle.is_active = False
            user_to_toggle.account_status = AccountStatus.DEACTIVATED
            state = "deactivated"
            invalidate_user_sessions(user_to_toggle)

        user_to_toggle.save(update_fields=['is_active', 'account_status', 'suspended_at', 'suspended_reason'])

        record_audit_log(
            request.user,
            f"ADMIN_USER_{state.upper()}",
            request=request,
            details={
                "target_user": user_to_toggle.username,
                "is_active": user_to_toggle.is_active,
                "account_status": user_to_toggle.account_status,
            },
        )
        messages.success(request, f"User '{user_to_toggle.username}' has been {state}.")
    return redirect('accounts:user_list')


@admin_required
def suspend_user_view(request, user_id):
    """
    Admin-only suspension / unsuspension endpoint for external user accounts.
    """
    if request.method != 'POST':
        return redirect('accounts:user_list')

    target_user = get_object_or_404(User, id=user_id)
    if target_user == request.user:
        messages.error(request, "You cannot suspend your own administrative account.")
        return redirect('accounts:user_list')

    action = request.POST.get('action', 'suspend').strip().lower()
    reason = request.POST.get('reason', '').strip()[:255]

    if action == 'unsuspend':
        target_user.account_status = AccountStatus.ACTIVE
        target_user.is_active = True
        target_user.suspended_at = None
        target_user.suspended_reason = ''
        audit_action = "ADMIN_USER_UNSUSPENDED"
        msg = f"Account '{target_user.username}' suspension has been lifted."
    else:
        target_user.account_status = AccountStatus.SUSPENDED
        target_user.is_active = False
        target_user.suspended_at = timezone.now()
        target_user.suspended_reason = reason or "Suspended by Administrator"
        invalidate_user_sessions(target_user)
        audit_action = "ADMIN_USER_SUSPENDED"
        msg = f"Account '{target_user.username}' has been suspended."

    target_user.save(update_fields=['account_status', 'is_active', 'suspended_at', 'suspended_reason'])
    record_audit_log(
        request.user,
        audit_action,
        request=request,
        details={
            "target_user": target_user.username,
            "account_status": target_user.account_status,
            "reason": target_user.suspended_reason,
        },
    )
    messages.warning(request, msg)
    return redirect('accounts:user_list')


@admin_required
def reset_user_password_view(request, user_id):
    """
    Admin-only password reset workflow for external user accounts.
    """
    target_user = get_object_or_404(User, id=user_id)
    if target_user == request.user:
        messages.info(request, "Use 'Change Password' to update your own password.")
        return redirect('accounts:change_password')

    if request.method == 'POST':
        form = AdminResetPasswordForm(target_user, request.POST)
        if form.is_valid():
            new_pw = form.cleaned_data['new_password']
            target_user.set_password(new_pw)
            target_user.must_change_password = form.cleaned_data.get('must_change_password', True)
            target_user.save(update_fields=['password', 'must_change_password'])

            invalidate_user_sessions(target_user)
            clear_login_rate_limit(None, target_user.username)

            record_audit_log(
                request.user,
                "ADMIN_PASSWORD_RESET",
                request=request,
                details={
                    "target_user": target_user.username,
                    "must_change_password": target_user.must_change_password,
                },
            )
            messages.success(request, f"Password has been reset securely for '{target_user.username}'.")
            return redirect('accounts:user_list')
    else:
        form = AdminResetPasswordForm(target_user)

    return render(request, 'accounts/user_reset_password.html', {
        'form': form,
        'target_user': target_user,
        'page_title': f"Reset Password - {target_user.username}",
    })


@admin_required
def user_submission_history_view(request, user_id):
    """
    Admin-only inspection of a specific user's invoice submission history & recent audit activity.
    """
    from apps.invoices.models import InvoiceSubmission

    target_user = get_object_or_404(User.objects.select_related('department'), id=user_id)
    submissions = (
        InvoiceSubmission.objects.filter(employee=target_user)
        .prefetch_related('documents')
        .order_by('-created_at')
    )
    user_audits = AuditLog.objects.filter(
        Q(user=target_user) | Q(details__target_user=target_user.username) | Q(details__created_user=target_user.username)
    ).order_by('-timestamp')[:30]

    paginator = Paginator(submissions, 25)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'accounts/user_history.html', {
        'target_user': target_user,
        'page_obj': page_obj,
        'total_submissions': submissions.count(),
        'user_audits': user_audits,
        'page_title': f"Submission History - {target_user.username}",
    })


@admin_required
def audit_logs_view(request):
    """
    Admin-only Security & Operational Audit Trail viewer.
    Strictly inaccessible to external submitters and non-admin roles.
    """
    from apps.invoices.models import PurchaseAuditTrail

    q = request.GET.get('q', '').strip()
    action_filter = request.GET.get('action', '').strip()

    logs_qs = AuditLog.objects.select_related('user').order_by('-timestamp')
    if q:
        logs_qs = logs_qs.filter(
            Q(action__icontains=q)
            | Q(user__username__icontains=q)
            | Q(ip_address__icontains=q)
        )
    if action_filter:
        logs_qs = logs_qs.filter(action=action_filter)

    paginator = Paginator(logs_qs, 50)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    distinct_actions = list(AuditLog.objects.values_list('action', flat=True).distinct().order_by('action'))
    purchase_audits = PurchaseAuditTrail.objects.select_related('submission', 'changed_by').order_by('-changed_at')[:30]

    return render(request, 'accounts/audit_logs.html', {
        'page_obj': page_obj,
        'distinct_actions': distinct_actions,
        'purchase_audits': purchase_audits,
        'q': q,
        'action_filter': action_filter,
        'page_title': "Security & Operations Audit Logs",
    })
