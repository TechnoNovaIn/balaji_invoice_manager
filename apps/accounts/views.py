from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.views import LoginView
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.generic import View, ListView, CreateView, UpdateView
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator

from apps.accounts.models import User, Department, UserRole, AuditLog
from apps.accounts.forms import LoginForm, CustomUserCreationForm, CustomUserChangeForm
from apps.accounts.permissions import RoleRequiredMixin, admin_required


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


class UserLoginView(LoginView):
    """
    Secure Login view with session fixation prevention and audit logging.
    """
    form_class = LoginForm
    template_name = 'accounts/login.html'
    redirect_authenticated_user = True

    def form_valid(self, form):
        user = form.get_user()
        if not user.is_active:
            messages.error(self.request, "Your account has been deactivated. Please contact the administrator.")
            return self.form_invalid(form)

        login(self.request, user)

        # Persistent session handling: 'Keep me signed in on this phone'
        remember_me = form.cleaned_data.get('remember_me', True)
        if remember_me:
            # Persistent 1-year session so employees never need to re-login every day
            self.request.session.set_expiry(31536000)
        else:
            self.request.session.set_expiry(0)

        # Record audit log
        AuditLog.objects.create(
            user=user,
            action="USER_LOGIN_SUCCESS",
            ip_address=get_client_ip(self.request),
            user_agent=self.request.META.get('HTTP_USER_AGENT', '')[:500],
            details={"username": user.username, "role": user.role, "remember_me": remember_me}
        )

        # First-Time Password Change Detection:
        # If user has must_change_password flag set (e.g. initial demo password setup)
        if user.must_change_password:
            messages.info(
                self.request,
                "Welcome to Balaji InvoiceHub! Please set your personal password now so your phone can save it safely."
            )
            return redirect('accounts:change_password')

        messages.success(self.request, f"Welcome back, {user.first_name or user.username}!")
        return redirect('core:dashboard_router')

    def form_invalid(self, form):
        username = form.data.get('username', 'unknown')
        AuditLog.objects.create(
            user=None,
            action="USER_LOGIN_FAILED",
            ip_address=get_client_ip(self.request),
            user_agent=self.request.META.get('HTTP_USER_AGENT', '')[:500],
            details={"attempted_username": username}
        )
        return super().form_invalid(form)


class UserLogoutView(View):
    """
    Secure Logout flushing session and recording audit trail.
    """
    def post(self, request):
        return self._do_logout(request)

    def get(self, request):
        return self._do_logout(request)

    def _do_logout(self, request):
        if request.user.is_authenticated:
            AuditLog.objects.create(
                user=request.user,
                action="USER_LOGOUT",
                ip_address=get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
                details={"username": request.user.username}
            )
        logout(request)
        messages.info(request, "You have been logged out securely.")
        return redirect('accounts:login')


@login_required
def profile_view(request):
    """
    Displays current user's profile and active permissions.
    """
    return render(request, 'accounts/profile.html', {
        'user': request.user,
    })


@login_required
def change_password_view(request):
    """
    Mobile-first, user-friendly password setup and change view.
    Designed for employees to effortlessly replace initial demo credentials
    and save their personalized login on mobile browsers.
    """
    user = request.user
    is_first_time = user.must_change_password or (user.is_employee and user.check_password('BalajiEmp@2026'))

    if request.method == 'POST':
        new_password = request.POST.get('new_password', '').strip()
        confirm_password = request.POST.get('confirm_password', '').strip()

        if len(new_password) < 6:
            messages.error(request, "Password must be at least 6 characters long.")
            return render(request, 'accounts/change_password.html', {'is_first_time': is_first_time})

        if new_password != confirm_password:
            messages.error(request, "Passwords do not match. Please re-enter carefully.")
            return render(request, 'accounts/change_password.html', {'is_first_time': is_first_time})

        if new_password == 'BalajiEmp@2026':
            messages.error(request, "Please choose a personal password different from the initial demo password.")
            return render(request, 'accounts/change_password.html', {'is_first_time': is_first_time})

        user.set_password(new_password)
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])

        from django.contrib.auth import update_session_auth_hash
        update_session_auth_hash(request, user)  # Keep user session logged in!

        AuditLog.objects.create(
            user=user,
            action="USER_PASSWORD_CHANGE",
            ip_address=get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            details={"username": user.username}
        )

        messages.success(request, "✓ Password successfully saved! Your phone can now remember this password for instant 1-tap logins.")
        return redirect('core:dashboard_router')

    return render(request, 'accounts/change_password.html', {
        'is_first_time': is_first_time,
        'page_title': "Set Your Personal Password" if is_first_time else "Change Password",
    })


@method_decorator(admin_required, name='dispatch')
class UserManagementListView(ListView):
    """
    Admin-only user and employee account management.
    """
    model = User
    template_name = 'accounts/user_list.html'
    context_object_name = 'users_list'
    paginate_by = 25

    def get_queryset(self):
        return User.objects.select_related('department').order_by('role', 'username')


@method_decorator(admin_required, name='dispatch')
class UserCreateView(CreateView):
    """
    Admin-only user creation.
    """
    model = User
    form_class = CustomUserCreationForm
    template_name = 'accounts/user_form.html'
    success_url = reverse_lazy('accounts:user_list')

    def form_valid(self, form):
        response = super().form_valid(form)
        AuditLog.objects.create(
            user=self.request.user,
            action="ADMIN_CREATE_USER",
            ip_address=get_client_ip(self.request),
            details={"created_user": self.object.username, "role": self.object.role}
        )
        messages.success(self.request, f"User account '{self.object.username}' created successfully.")
        return response


@admin_required
def toggle_user_active(request, user_id):
    """
    Admin-only activation/deactivation toggle for user accounts.
    """
    if request.method == 'POST':
        user_to_toggle = get_object_or_404(User, id=user_id)
        if user_to_toggle == request.user:
            messages.error(request, "You cannot deactivate your own administrative account.")
            return redirect('accounts:user_list')

        user_to_toggle.is_active = not user_to_toggle.is_active
        user_to_toggle.save(update_fields=['is_active'])

        state = "activated" if user_to_toggle.is_active else "deactivated"
        AuditLog.objects.create(
            user=request.user,
            action=f"ADMIN_USER_{state.upper()}",
            ip_address=get_client_ip(request),
            details={"target_user": user_to_toggle.username, "is_active": user_to_toggle.is_active}
        )
        messages.success(request, f"User '{user_to_toggle.username}' has been {state}.")
    return redirect('accounts:user_list')

