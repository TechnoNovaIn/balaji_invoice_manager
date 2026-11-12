"""
Security, reverse-proxy IP resolution, login throttling, session lifecycle,
and centralized audit helpers for Balaji InvoiceHub.
"""
import hashlib
import ipaddress
import time
from typing import Any, Dict, Optional, Tuple

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import AuditLog


def _parse_valid_ip(raw_ip: Optional[str]) -> Optional[str]:
    if not raw_ip:
        return None
    candidate = str(raw_ip).strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _is_remote_trusted_proxy(remote_addr: Optional[str]) -> Tuple[bool, bool]:
    """
    Validate whether REMOTE_ADDR is a trusted reverse proxy.
    Returns (is_trusted_for_forwarded, is_trusted_for_cloudflare):
    - If TRUSTED_PROXY_IPS is explicitly configured, REMOTE_ADDR must match one of its IPs/CIDRs.
    - Otherwise, X-Forwarded-For falls back to hop-count rules while CF-Connecting-IP strictly requires loopback REMOTE_ADDR.
    """
    if not remote_addr:
        return (False, False)

    try:
        addr_obj = ipaddress.ip_address(remote_addr)
    except ValueError:
        return (False, False)

    configured = getattr(settings, 'TRUSTED_PROXY_IPS', None) or []
    if configured:
        for entry in configured:
            try:
                if '/' in entry:
                    if addr_obj in ipaddress.ip_network(entry, strict=False):
                        return (True, True)
                elif addr_obj == ipaddress.ip_address(entry):
                    return (True, True)
            except ValueError:
                continue
        return (False, False)

    return (True, addr_obj.is_loopback)


def get_client_ip(request) -> Optional[str]:
    """
    Resolve client IP safely without blindly trusting client-supplied forwarding headers.

    When DJANGO_TRUSTED_PROXY_COUNT <= 0 (default), REMOTE_ADDR is used directly.
    When behind Cloudflare Tunnel (TRUSTED_PROXY_COUNT >= 1), inspects CF-Connecting-IP ONLY if
    REMOTE_ADDR is verified as a trusted proxy (or loopback when TRUSTED_PROXY_IPS is unset)
    and TRUST_CF_CONNECTING_IP is enabled; otherwise selects the Nth hop from the right of X-Forwarded-For.
    """
    if request is None:
        return None

    remote_addr = _parse_valid_ip(request.META.get('REMOTE_ADDR'))
    trusted_proxies = int(getattr(settings, 'TRUSTED_PROXY_COUNT', 0) or 0)
    if trusted_proxies <= 0:
        return remote_addr

    trusted_forwarded, trusted_cf = _is_remote_trusted_proxy(remote_addr)
    if not trusted_forwarded:
        return remote_addr

    if getattr(settings, 'TRUST_CF_CONNECTING_IP', False) and trusted_cf:
        cf_ip = _parse_valid_ip(request.META.get('HTTP_CF_CONNECTING_IP'))
        if cf_ip:
            return cf_ip

    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if not forwarded_for:
        return remote_addr

    hops = [ip for ip in (_parse_valid_ip(part) for part in forwarded_for.split(',')) if ip]
    if not hops:
        return remote_addr

    idx = max(0, len(hops) - trusted_proxies)
    return hops[idx]


def record_audit_log(
    user,
    action: str,
    request=None,
    details: Optional[Dict[str, Any]] = None,
) -> AuditLog:
    """Create an immutable AuditLog entry cleanly."""
    ip_addr = get_client_ip(request) if request is not None else None
    user_agent = ''
    if request is not None:
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]
    return AuditLog.objects.create(
        user=user if getattr(user, 'pk', None) else None,
        action=action[:64],
        ip_address=ip_addr,
        user_agent=user_agent,
        details=details or {},
    )


def _hash_key_part(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]


def _rate_limit_keys(ip_address: Optional[str], username: str) -> Tuple[str, str, str, str]:
    norm_ip = (ip_address or 'unknown').strip()
    norm_user = (username or '').strip().lower() or 'anonymous'
    ip_tag = _hash_key_part(norm_ip)
    acct_tag = _hash_key_part(norm_user)
    return (
        f"auth:fail:ip:{ip_tag}",
        f"auth:lock:ip:{ip_tag}",
        f"auth:fail:acct:{acct_tag}",
        f"auth:lock:acct:{acct_tag}",
    )


def check_login_rate_limit(request, username: str) -> Tuple[bool, int]:
    """
    Check if either client IP or target account is currently in a temporary cooldown.
    Returns (is_blocked, retry_after_seconds).
    Never enforces permanent lockouts (prevents credential-stuffing DoS).
    """
    now = int(time.time())
    ip_addr = get_client_ip(request)
    _, ip_lock_key, _, acct_lock_key = _rate_limit_keys(ip_addr, username)

    retry_after = 0
    for lock_key in (ip_lock_key, acct_lock_key):
        locked_until = cache.get(lock_key)
        if locked_until and int(locked_until) > now:
            retry_after = max(retry_after, int(locked_until) - now)

    return (retry_after > 0, retry_after)


def record_login_failure(request, username: str) -> Tuple[bool, int]:
    """
    Increment failure counters for client IP and target account.
    Triggers temporary cooldown when thresholds are exceeded.
    Returns (cooldown_triggered, retry_after_seconds).
    """
    max_attempts = int(getattr(settings, 'LOGIN_MAX_ATTEMPTS', 5) or 5)
    window_secs = int(getattr(settings, 'LOGIN_ATTEMPT_WINDOW_SECONDS', 300) or 300)
    cooldown_secs = int(getattr(settings, 'LOGIN_COOLDOWN_SECONDS', 300) or 300)
    ip_max_attempts = max(max_attempts, max_attempts * 2)

    ip_addr = get_client_ip(request)
    ip_fail_key, ip_lock_key, acct_fail_key, acct_lock_key = _rate_limit_keys(ip_addr, username)

    ip_fails = int(cache.get(ip_fail_key) or 0) + 1
    cache.set(ip_fail_key, ip_fails, timeout=window_secs)

    acct_fails = 0
    norm_user = (username or '').strip().lower()
    if norm_user:
        acct_fails = int(cache.get(acct_fail_key) or 0) + 1
        cache.set(acct_fail_key, acct_fails, timeout=window_secs)

    now = int(time.time())
    triggered = False
    if acct_fails >= max_attempts and norm_user:
        cache.set(acct_lock_key, now + cooldown_secs, timeout=cooldown_secs)
        triggered = True
    if ip_fails >= ip_max_attempts:
        cache.set(ip_lock_key, now + cooldown_secs, timeout=cooldown_secs)
        triggered = True

    return (triggered, cooldown_secs if triggered else 0)


def clear_login_rate_limit(request, username: str) -> None:
    """Reset per-account/IP throttling counters upon valid authentication or admin recovery."""
    ip_addr = get_client_ip(request) if request is not None else None
    ip_fail_key, ip_lock_key, acct_fail_key, acct_lock_key = _rate_limit_keys(ip_addr, username)
    keys = [acct_fail_key, acct_lock_key]
    if request is not None:
        keys.extend([ip_fail_key, ip_lock_key])
    cache.delete_many(keys)


def get_session_expiry_for_user(user, remember_me: bool) -> int:
    """
    Enforce role-based session lifetimes:
    - If remember_me is False -> 0 (expire at browser close).
    - Privileged roles (Admin / Naveen / Accounts / Management) -> shorter privileged window (default 8h).
    - External submitters -> bounded convenience window (default 14 days).
    - Field employees -> long-lived mobile convenience window (default 365 days).
    """
    if not remember_me:
        return 0
    if getattr(user, 'is_privileged_user', False):
        return int(getattr(settings, 'PRIVILEGED_SESSION_AGE', 28800))
    if getattr(user, 'is_external_submitter', False):
        return int(getattr(settings, 'EXTERNAL_SUBMITTER_SESSION_AGE', 1209600))
    return int(getattr(settings, 'EMPLOYEE_SESSION_AGE', 31536000))


def invalidate_user_sessions(user) -> int:
    """Delete all active Django sessions belonging to the target user."""
    if not getattr(user, 'pk', None):
        return 0
    target_id = str(user.pk)
    removed = 0
    for session in Session.objects.filter(expire_date__gte=timezone.now()):
        try:
            data = session.get_decoded()
        except Exception:
            continue
        if str(data.get('_auth_user_id', '')) == target_id:
            session.delete()
            removed += 1
    return removed

