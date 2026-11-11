import pytest
from django.test import override_settings


@pytest.fixture(autouse=True)
def _test_http_client_compatibility(request):
    """
    Allows Django's test.Client (which defaults to http://testserver) to run
    cleanly even when production .env enables DEBUG=False and SECURE_SSL_REDIRECT=True.
    Tests decorated with @pytest.mark.no_http_test_overrides inspect raw settings.
    """
    if request.node.get_closest_marker('no_http_test_overrides'):
        yield
        return

    with override_settings(
        ALLOWED_HOSTS=['testserver', 'localhost', '127.0.0.1'],
        SECURE_SSL_REDIRECT=False,
        SESSION_COOKIE_SECURE=False,
        CSRF_COOKIE_SECURE=False,
    ):
        yield

