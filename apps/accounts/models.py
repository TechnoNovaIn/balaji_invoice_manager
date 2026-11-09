from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import PermissionDenied


class Department(models.Model):
    """
    Departments within Balaji Transports (e.g., Fleet Maintenance, Fuel, Operations).
    """
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, unique=True, help_text="Short department code, e.g. FLEET, FUEL")
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'balaji_departments'
        ordering = ['name']
        verbose_name = 'Department'
        verbose_name_plural = 'Departments'

    def __str__(self):
        return f"{self.name} ({self.code})"


class UserRole(models.TextChoices):
    EMPLOYEE = 'EMPLOYEE', _('Employee (Invoice Collector)')
    NAVEEN = 'NAVEEN', _('Naveen (Accounts Operator)')
    ADMIN = 'ADMIN', _('System Administrator')
    MANAGEMENT = 'MANAGEMENT', _('Management (Read-Only Executive)')


class User(AbstractUser):
    """
    Custom User Model for Balaji InvoiceHub with strict Role-Based Access Control (RBAC).
    """
    role = models.CharField(
        max_length=20,
        choices=UserRole.choices,
        default=UserRole.EMPLOYEE,
        db_index=True,
        help_text="Designated role defining authorization level across InvoiceHub."
    )
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text="Phone number for identity verification and notifications."
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='members',
        help_text="Associated business department in Balaji Transports."
    )
    must_change_password = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Flag requiring the employee to set their own password upon first login."
    )

    class Meta:
        db_table = 'balaji_users'
        ordering = ['username']
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self):
        full = self.get_full_name()
        display = full if full else self.username
        return f"{display} [{self.get_role_display()}]"

    # Role Convenience Properties
    @property
    def is_employee(self) -> bool:
        return self.role == UserRole.EMPLOYEE

    @property
    def is_naveen(self) -> bool:
        return self.role == UserRole.NAVEEN

    @property
    def is_admin_role(self) -> bool:
        return self.role == UserRole.ADMIN or self.is_superuser

    @property
    def is_management(self) -> bool:
        return self.role == UserRole.MANAGEMENT

    # Fine-Grained Permission Capabilities
    def can_submit_invoices(self) -> bool:
        return self.is_active and self.is_employee

    def can_verify_invoices(self) -> bool:
        return self.is_active and self.is_naveen

    def can_record_advances(self) -> bool:
        return self.is_active and self.is_naveen

    def can_manage_reminders(self) -> bool:
        return self.is_active and self.is_naveen

    def can_convert_tally(self) -> bool:
        return self.is_active and self.is_naveen

    def can_view_financial_reports(self) -> bool:
        return self.is_active and (self.is_naveen or self.is_management or self.is_admin_role)

    def can_manage_users(self) -> bool:
        return self.is_active and self.is_admin_role

    def can_trigger_backups(self) -> bool:
        return self.is_active and self.is_admin_role


class AuditLog(models.Model):
    """
    Immutable audit trail for all security, authentication, and state change events.
    Edits and deletions are strictly prevented at the model and admin layer.
    """
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_entries'
    )
    action = models.CharField(max_length=64, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    details = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'balaji_audit_logs'
        ordering = ['-timestamp']
        verbose_name = 'Audit Log'
        verbose_name_plural = 'Audit Logs'

    def __str__(self):
        user_display = self.user.username if self.user else "System/Anonymous"
        return f"[{self.timestamp:%Y-%m-%d %H:%M:%S}] {user_display}: {self.action}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionDenied("AuditLog entries are immutable and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("AuditLog entries cannot be deleted under any circumstances.")
