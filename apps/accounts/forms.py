from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm, UserChangeForm
from apps.accounts.models import User, Department, UserRole


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

