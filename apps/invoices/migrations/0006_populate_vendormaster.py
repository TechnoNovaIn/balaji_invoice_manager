from django.db import migrations


def populate_vendor_master(apps, schema_editor):
    VendorMaster = apps.get_model('invoices', 'VendorMaster')
    from apps.invoices.vendor_directory import VENDOR_DIRECTORY, STATE_CODE_MAP
    import re

    for gstin, info in VENDOR_DIRECTORY.items():
        name = info.get('name', '').strip()
        if not name:
            continue
        cleaned_gstin = gstin.strip().upper()
        norm_name = re.sub(r'[^a-z0-9]', '', name.lower())
        pan = cleaned_gstin[2:12] if len(cleaned_gstin) >= 12 else ''
        state_code = cleaned_gstin[:2] if len(cleaned_gstin) >= 2 and cleaned_gstin[:2].isdigit() else ''
        state = info.get('state') or STATE_CODE_MAP.get(state_code, '')

        # Avoid duplicates
        if not VendorMaster.objects.filter(gstin=cleaned_gstin).exists():
            VendorMaster.objects.create(
                name=name,
                normalized_name=norm_name,
                gstin=cleaned_gstin,
                pan=pan,
                state=state,
                state_code=state_code,
                default_expense_ledger=info.get('ledger', 'Purchase Spares & Consumables'),
                is_active=True,
                is_verified=True,
            )


def reverse_populate(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0005_vendormaster'),
    ]

    operations = [
        migrations.RunPython(populate_vendor_master, reverse_populate),
    ]

