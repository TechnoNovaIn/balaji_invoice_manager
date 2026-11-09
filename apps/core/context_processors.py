def app_info(request):
    """
    Context processor injecting global application metadata and role state.
    """
    context = {
        'APP_NAME': 'Balaji InvoiceHub',
        'COMPANY_NAME': 'Balaji Transports',
        'APP_VERSION': '1.0.0 (Phase 1)',
        'SANDBOX_MODE': True,
    }
    return context

