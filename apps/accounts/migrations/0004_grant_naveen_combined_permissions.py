from django.db import migrations


def grant_naveen_combined_permissions(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    # Grant both Admin and Accounts capabilities to existing Accounts Operator account (Naveen)
    User.objects.filter(role__in=['NAVEEN', 'ACCOUNTS']).update(
        has_admin_access=True,
        has_accounts_access=True,
        is_staff=True,
    )


def revert_naveen_combined_permissions(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    User.objects.filter(role__in=['NAVEEN', 'ACCOUNTS']).update(
        has_admin_access=False,
        has_accounts_access=False,
    )


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0003_user_account_status_user_can_resubmit_invoices_and_more'),
    ]

    operations = [
        migrations.RunPython(
            grant_naveen_combined_permissions,
            reverse_code=revert_naveen_combined_permissions,
        ),
    ]

