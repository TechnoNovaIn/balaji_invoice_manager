# Administrator Account Recovery Procedure

Balaji InvoiceHub stores no hardcoded passwords, recovery tokens, or backdoors in source code.

If the sole Administrator & Accounts Operator account (`naveen`) loses access due to a forgotten password or accidental deactivation, an operator with SSH / local server console access can restore access safely using the Django management command.

## Command

```bash
python manage.py recover_admin_account \
    --username naveen \
    --operator "<operator_name>" \
    --reason "Ticket / Incident explanation" \
    --grant-accounts-access
```

### Interactive Password Entry (Recommended)

Omit `--generate-temp-password` so the command prompts cleanly with masked console input (`getpass`) and runs Django password validators.

### Generating a Random Temporary Password

```bash
python manage.py recover_admin_account \
    --username naveen \
    --operator "ops-admin" \
    --reason "Locked out after password rotation" \
    --generate-temp-password \
    --grant-accounts-access
```

When invoked:
1. Reactivates the target user (`is_active=True`, `account_status='ACTIVE'`), unlocks `has_admin_access=True`, and forces a password change on next login (`must_change_password=True`).
2. Invalidates all existing sessions for that account.
3. Records an immutable `ADMIN_RECOVERY_EXECUTED` event in `AuditLog` containing operator, target user, reason, and timestamp.

