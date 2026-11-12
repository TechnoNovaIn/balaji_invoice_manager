from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from apps.accounts.models import User, Department, AuditLog


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'code')


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'first_name', 'last_name', 'role', 'account_status', 'has_admin_access', 'has_accounts_access', 'is_active')
    list_filter = ('role', 'account_status', 'has_admin_access', 'has_accounts_access', 'is_active', 'department')
    search_fields = ('username', 'first_name', 'last_name', 'email', 'phone_number')
    ordering = ('role', 'username')

    fieldsets = UserAdmin.fieldsets + (
        ('Balaji InvoiceHub Role & Profile', {
            'fields': (
                'role', 'has_admin_access', 'has_accounts_access',
                'can_upload_invoices', 'can_resubmit_invoices',
                'account_status', 'suspended_at', 'suspended_reason',
                'phone_number', 'department', 'must_change_password',
            )
        }),
    )

    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Balaji InvoiceHub Role & Profile', {
            'fields': (
                'role', 'has_admin_access', 'has_accounts_access',
                'can_upload_invoices', 'can_resubmit_invoices',
                'phone_number', 'department', 'must_change_password',
            )
        }),
    )


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'user', 'action', 'ip_address')
    list_filter = ('action', 'timestamp')
    search_fields = ('action', 'user__username', 'ip_address')
    readonly_fields = ('user', 'action', 'ip_address', 'user_agent', 'details', 'timestamp')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

