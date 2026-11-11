from django.db import migrations


def fix_duplicate_vendor_gstin(apps, schema_editor):
    VendorMaster = apps.get_model('invoices', 'VendorMaster')

    # Release colliding GSTIN from BHEESHMA METALS (PAN AVZPJ3961B belongs to BASANNA JANARDHAN)
    bheeshma = VendorMaster.objects.filter(name='BHEESHMA METALS', gstin='29AVZPJ3961B1Z4').first()
    if bheeshma is not None:
        bheeshma.gstin = ''
        bheeshma.pan = ''
        bheeshma.state_code = ''
        bheeshma.is_verified = False
        bheeshma.save(update_fields=['gstin', 'pan', 'state_code', 'is_verified', 'updated_at'])

    if not VendorMaster.objects.filter(gstin='29AVZPJ3961B1Z4').exists():
        VendorMaster.objects.create(
            name='BASANNA JANARDHAN',
            normalized_name='basannajanardhan',
            gstin='29AVZPJ3961B1Z4',
            pan='AVZPJ3961B',
            state='Karnataka',
            state_code='29',
            default_expense_ledger='Building Repairs, Hardware & Paints',
            is_active=True,
            is_verified=True,
        )


def reverse_fix(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0007_uploadeddocument_current_stage_and_more'),
    ]

    operations = [
        migrations.RunPython(fix_duplicate_vendor_gstin, reverse_fix),
    ]

