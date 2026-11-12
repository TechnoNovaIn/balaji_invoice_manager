from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm, UserChangeForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from apps.accounts.models import AccountStatus, Department, User, UserRole


SUBMITTER_ROLE_CHOICES = [
    (UserRole.EXTERNAL_SUBMITTER, 'External Submitter'),
    (UserRole.EMPLOYEE, 'Employee (Invoice Collector)'),
]


class LoginForm(AuthenticationForm):
    """
    Clean, responsive login form styled for mobile and desktop screens.
    """
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Enter your username',
            'autocomplete': 'username',
            'autofocus': True,
        }),
        label="Username"
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Enter your password',
            'autocomplete': 'current-password',
        }),
        label="Password"
    )
    remember_me = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={
            'class': 'form-check-input',
            'id': 'rememberMeCheck',
            'style': 'transform: scale(1.2); cursor: pointer;'
        }),
        label="Keep me signed in on this phone"
    )


class CustomUserCreationForm(UserCreationForm):
    role = forms.ChoiceField(
        choices=SUBMITTER_ROLE_CHOICES,
        initial=UserRole.EXTERNAL_SUBMITTER,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Assigned Role",
        help_text="External submitters can only upload and track their own invoices.",
    )

    class Meta:
        model = User
        fields = (
            'username', 'first_name', 'last_name', 'email', 'phone_number',
            'role', 'department', 'can_upload_invoices', 'can_resubmit_invoices',
            'must_change_password', 'is_active',
        )
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'department': forms.Select(attrs={'class': 'form-select'}),
            'can_upload_invoices': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'can_resubmit_invoices': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'must_change_password': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['password1'].widget.attrs.update({'class': 'form-control'})
        self.fields['password2'].widget.attrs.update({'class': 'form-control'})
        self.fields['must_change_password'].initial = True

    def clean_role(self):
        role = self.cleaned_data.get('role')
        allowed = {choice[0] for choice in SUBMITTER_ROLE_CHOICES}
        if role not in allowed:
            raise ValidationError(
                "Only External Submitter or Employee submitter accounts can be created here."
            )
        return role

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = False
        user.is_superuser = False
        user.has_admin_access = False
        user.has_accounts_access = False
        user.account_status = AccountStatus.ACTIVE if user.is_active else AccountStatus.DEACTIVATED
        if commit:
            user.save()
        return user


class ExternalUserEditForm(forms.ModelForm):
    """Admin form for editing external/employee user details, status, and submitter permissions."""
    class Meta:
        model = User
        fields = (
            'first_name', 'last_name', 'email', 'phone_number',
            'department', 'account_status', 'can_upload_invoices',
            'can_resubmit_invoices', 'must_change_password',
        )
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'department': forms.Select(attrs={'class': 'form-select'}),
            'account_status': forms.Select(attrs={'class': 'form-select'}),
            'can_upload_invoices': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'can_resubmit_invoices': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'must_change_password': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_active = (user.account_status == AccountStatus.ACTIVE)
        if user.account_status != AccountStatus.SUSPENDED:
            user.suspended_at = None
            user.suspended_reason = ''
        if commit:
            user.save()
        return user


class AdminResetPasswordForm(forms.Form):
    new_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'new-password'}),
        label="New Password",
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'autocomplete': 'new-password'}),
        label="Confirm New Password",
    )
    must_change_password = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        label="Require user to change password on next login",
    )

    def __init__(self, target_user, *args, **kwargs):
        self.target_user = target_user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        pw1 = cleaned.get('new_password')
        pw2 = cleaned.get('confirm_password')
        if pw1 and pw2 and pw1 != pw2:
            self.add_error('confirm_password', "Passwords do not match.")
            return cleaned
        if pw1:
            validate_password(pw1, user=self.target_user)
        return cleaned


class ProfileUpdateForm(forms.ModelForm):
    """Permitted self-service profile fields for authenticated users."""
    class Meta:
        model = User
        fields = ('first_name', 'last_name', 'email', 'phone_number')
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
        }


class CustomUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = ('username', 'first_name', 'last_name', 'email', 'phone_number', 'role', 'department', 'is_active')
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'first_name': forms.TextInput(attrs={'class': 'form-control'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
            'phone_number': forms.TextInput(attrs={'class': 'form-control'}),
            'role': forms.Select(attrs={'class': 'form-select'}),
            'department': forms.Select(attrs={'class': 'form-select'}),
        }

