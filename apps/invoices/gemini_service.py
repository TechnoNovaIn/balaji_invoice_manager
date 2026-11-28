import os
import re
import time
import logging
import threading

# Clean up any invalid GOOGLE_GENAI_HTTP_CLIENT configuration
_client_backend = os.environ.get('GOOGLE_GENAI_HTTP_CLIENT', '').strip()
if _client_backend:
    try:
        __import__(_client_backend)
    except ImportError:
        os.environ.pop('GOOGLE_GENAI_HTTP_CLIENT', None)
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
from django.conf import settings

logger = logging.getLogger(__name__)

# Thread-safe in-memory key health state & atomic .env write lock
_KEY_HEALTH_LOCK = threading.Lock()
_ENV_WRITE_LOCK = threading.Lock()
_KEY_EXHAUSTED_UNTIL: Dict[str, float] = {}  # key -> epoch timestamp when cooldown expires
_KEY_FAIL_COUNT: Dict[str, int] = {}         # key -> consecutive fail count


def mask_api_key(key: Optional[str]) -> str:
    """Masks an API key for safe UI display (e.g. '••••••••••••••••••••eyQ')."""
    if not key:
        return ""
    clean = key.strip()
    if len(clean) <= 4:
        return "••••••••"
    return f"{'•' * 20}{clean[-4:]}"


def parse_gemini_keys(raw_input: Union[str, List[str], None]) -> List[str]:
    """
    Parses comma-separated, newline-separated, or list of Gemini API keys.
    Deduplicates keys while preserving ordering.
    """
    if not raw_input:
        return []

    if isinstance(raw_input, (list, tuple, set)):
        items = list(raw_input)
    else:
        # Split on commas, newlines, carriage returns, or semicolons
        items = re.split(r'[\r\n,;]+', str(raw_input))

    result: List[str] = []
    seen = set()
    for item in items:
        clean = item.strip()
        # Basic validation: ignore empty strings or placeholders
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def get_configured_gemini_keys() -> List[str]:
    """
    Retrieves all configured Gemini API keys from settings / environment.
    Combines GEMINI_API_KEYS and GEMINI_API_KEY with deduplication.
    """
    raw_keys = getattr(settings, 'GEMINI_API_KEYS', None) or os.getenv('GEMINI_API_KEYS', '')
    parsed = parse_gemini_keys(raw_keys)

    single_key = (getattr(settings, 'GEMINI_API_KEY', None) or os.getenv('GEMINI_API_KEY', '')).strip()
    if single_key and single_key not in parsed:
        parsed.insert(0, single_key)

    return parsed


def mark_key_exhausted(key: str, cooldown_seconds: int = 3600) -> None:
    """
    Marks an API key as exhausted (HTTP 429 / 403 quota) with a cooldown period (default 1 hour).
    Subsequent extractions will skip this key and prioritize healthy keys.
    """
    if not key:
        return
    clean_key = key.strip()
    with _KEY_HEALTH_LOCK:
        expires_at = time.time() + cooldown_seconds
        _KEY_EXHAUSTED_UNTIL[clean_key] = expires_at
        _KEY_FAIL_COUNT[clean_key] = _KEY_FAIL_COUNT.get(clean_key, 0) + 1
        logger.warning(
            "Gemini API key (...%s) marked EXHAUSTED for %ds cooldown (fail count: %d)",
            clean_key[-4:] if len(clean_key) >= 4 else clean_key,
            cooldown_seconds,
            _KEY_FAIL_COUNT[clean_key]
        )


def mark_key_healthy(key: str) -> None:
    """Marks an API key as healthy and resets its cooldown."""
    if not key:
        return
    clean_key = key.strip()
    with _KEY_HEALTH_LOCK:
        if clean_key in _KEY_EXHAUSTED_UNTIL:
            del _KEY_EXHAUSTED_UNTIL[clean_key]
        _KEY_FAIL_COUNT[clean_key] = 0


def get_key_health(key: str) -> Dict[str, Any]:
    """Returns the current health status and remaining cooldown for a key."""
    if not key:
        return {'is_exhausted': False, 'cooldown_remaining_seconds': 0, 'status': 'UNCONFIGURED', 'masked': ''}

    clean_key = key.strip()
    now = time.time()
    with _KEY_HEALTH_LOCK:
        expires_at = _KEY_EXHAUSTED_UNTIL.get(clean_key, 0)
        remaining = int(expires_at - now) if expires_at > now else 0
        is_exhausted = remaining > 0

    return {
        'is_exhausted': is_exhausted,
        'cooldown_remaining_seconds': remaining,
        'status': 'EXHAUSTED' if is_exhausted else 'HEALTHY',
        'masked': mask_api_key(clean_key),
    }


def get_available_gemini_keys() -> List[str]:
    """
    Returns configured keys ordered so healthy keys come first.
    If all keys are exhausted, returns all keys sorted by shortest remaining cooldown
    so processing can attempt recovery.
    """
    keys = get_configured_gemini_keys()
    if not keys:
        return []

    now = time.time()
    healthy: List[str] = []
    exhausted: List[tuple[float, str]] = []

    with _KEY_HEALTH_LOCK:
        for k in keys:
            expires_at = _KEY_EXHAUSTED_UNTIL.get(k, 0)
            if expires_at > now:
                exhausted.append((expires_at, k))
            else:
                healthy.append(k)

    if healthy:
        # Healthy keys first, then exhausted keys ordered by earliest expiry
        exhausted.sort(key=lambda x: x[0])
        return healthy + [k for _, k in exhausted]

    # All keys currently exhausted: sort by earliest expiry to try the closest-to-recovered key
    exhausted.sort(key=lambda x: x[0])
    return [k for _, k in exhausted]


def test_gemini_connection(api_key: str, model_name: str = "gemini-3.5-flash-lite") -> Dict[str, Any]:
    """
    Performs a lightweight 1-token test against Google Gemini API.
    Returns latency, status, and troubleshooting hints.
    Automatically updates in-memory key health status.
    """
    if not api_key or not api_key.strip():
        return {
            'success': False,
            'error': 'API key cannot be empty.',
            'latency_ms': 0,
            'model': model_name,
            'masked_key': '',
        }

    clean_key = api_key.strip()
    target_model = (model_name or getattr(settings, 'GEMINI_MODEL', 'gemini-3.5-flash-lite') or 'gemini-3.5-flash-lite').strip()

    from google import genai
    from google.genai import types

    t0 = time.perf_counter()
    try:
        timeout_ms = 12000
        client = genai.Client(
            api_key=clean_key,
            http_options=types.HttpOptions(timeout=timeout_ms)
        )
        resp = client.models.generate_content(
            model=target_model,
            contents="ping"
        )
        latency = round((time.perf_counter() - t0) * 1000)
        mark_key_healthy(clean_key)
        return {
            'success': True,
            'message': f'Google Gemini API is active & responding (HTTP 200 OK in {latency}ms)',
            'latency_ms': latency,
            'model': target_model,
            'snippet': (resp.text or '').strip()[:60],
            'masked_key': mask_api_key(clean_key),
        }
    except Exception as e:
        latency = round((time.perf_counter() - t0) * 1000)
        err_str = str(e)
        err_code = getattr(e, 'code', None)
        if not isinstance(err_code, int):
            err_code = getattr(e, 'status_code', None)
        logger.warning("Gemini API connection test failed: %s", err_str)

        is_timeout = isinstance(e, TimeoutError) or 'Timeout' in type(e).__name__
        is_connect_err = isinstance(e, ConnectionError) or 'Connect' in type(e).__name__ or 'Transport' in type(e).__name__

        if 'httpx' in err_str and 'not installed' in err_str:
            hint = 'Python package "httpx" is missing on this server environment. Run "pip install httpx" to activate Gemini Vision AI.'
        elif is_timeout:
            hint = f'Google Gemini API timed out after {latency}ms. Check internet connectivity or firewall access.'
        elif is_connect_err:
            hint = f'Network transport error reaching Google Gemini API: {err_str[:160]}'
        elif err_code == 403 or '403' in err_str or 'PERMISSION_DENIED' in err_str:
            hint = 'Google 403 Forbidden: Free tier unavailable or project disabled. Check billing or try a fresh project key.'
            mark_key_exhausted(clean_key, cooldown_seconds=7200)
        elif err_code == 429 or '429' in err_str or 'RESOURCE_EXHAUSTED' in err_str:
            hint = 'Google 429 Quota Exceeded: Daily or per-minute rate limit reached for this key. Cooldown activated.'
            mark_key_exhausted(clean_key, cooldown_seconds=3600)
        elif err_code == 404 or '404' in err_str or 'NOT_FOUND' in err_str:
            hint = f'Google 404 Not Found: Model "{target_model}" is unavailable for this key. We recommend "gemini-3.5-flash-lite".'
        elif err_code == 400 or '400' in err_str or 'INVALID_ARGUMENT' in err_str:
            hint = 'Google 400 Invalid Argument: API key appears invalid or malformed.'
        else:
            hint = f'Connection error: {err_str[:180]}'

        return {
            'success': False,
            'error': hint,
            'raw_error': err_str[:300],
            'latency_ms': latency,
            'model': target_model,
            'masked_key': mask_api_key(clean_key),
        }


def test_all_gemini_keys(keys: Optional[List[str]] = None, model_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Tests all configured or provided Gemini API keys concurrently in parallel.
    Returns status, latency, and health state for each key.
    """
    target_keys = keys if keys is not None else get_configured_gemini_keys()
    if not target_keys:
        return []

    target_model = (model_name or getattr(settings, 'GEMINI_MODEL', 'gemini-3.5-flash-lite') or 'gemini-3.5-flash-lite').strip()
    results: List[Dict[str, Any]] = [{} for _ in target_keys]

    def _test_single(idx: int, k: str):
        res = test_gemini_connection(api_key=k, model_name=target_model)
        res['key_index'] = idx + 1
        res['health'] = get_key_health(k)
        return idx, res

    workers = min(len(target_keys), 6)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(_test_single, i, key) for i, key in enumerate(target_keys)]
        for f in as_completed(futures):
            try:
                idx, res = f.result()
                results[idx] = res
            except Exception as e:
                logger.error("Error testing key index: %s", e)

    return results


def save_gemini_key_pool(keys: List[str], model_name: Optional[str] = None) -> bool:
    """
    Safely persists the multi-key pool (GEMINI_API_KEYS) and primary key (GEMINI_API_KEY)
    into the server-side .env file, hot-reloads runtime settings, and clears client caches.
    """
    clean_keys = parse_gemini_keys(keys)
    env_path = Path(settings.BASE_DIR) / '.env'
    if not env_path.exists():
        logger.error(".env file not found at %s", env_path)
        return False

    keys_str = ','.join(clean_keys)
    primary_key = clean_keys[0] if clean_keys else ''

    key_values: Dict[str, str] = {
        'GEMINI_API_KEYS': keys_str,
        'GEMINI_API_KEY': primary_key,
    }
    if model_name and model_name.strip():
        safe_model = re.sub(r'[\r\n]+', '', model_name.strip())
        if safe_model:
            key_values['GEMINI_MODEL'] = safe_model

    with _ENV_WRITE_LOCK:
        lines = env_path.read_text(encoding='utf-8').splitlines(keepends=True)
        updated_keys = set()
        new_lines = []

        for line in lines:
            stripped = line.strip()
            matched = False
            for k, v in key_values.items():
                if stripped.startswith(f'{k}=') or stripped == k:
                    new_lines.append(f'{k}={v}\n')
                    updated_keys.add(k)
                    matched = True
                    break
            if not matched:
                new_lines.append(line)

        for k, v in key_values.items():
            if k not in updated_keys:
                new_lines.append(f'{k}={v}\n')

        # Atomic write to .env
        temp_path = env_path.with_suffix('.tmp')
        temp_path.write_text(''.join(new_lines), encoding='utf-8')
        temp_path.replace(env_path)

    # Hot update in-memory runtime
    os.environ['GEMINI_API_KEYS'] = keys_str
    setattr(settings, 'GEMINI_API_KEYS', keys_str)

    os.environ['GEMINI_API_KEY'] = primary_key
    setattr(settings, 'GEMINI_API_KEY', primary_key)

    if model_name and model_name.strip():
        clean_model = model_name.strip()
        os.environ['GEMINI_MODEL'] = clean_model
        setattr(settings, 'GEMINI_MODEL', clean_model)

    # Flush client cache so new keys take effect immediately
    try:
        from apps.invoices.gemini_extractor import _CLIENT_CACHE, _CLIENT_LOCK
        with _CLIENT_LOCK:
            _CLIENT_CACHE.clear()
    except Exception:
        pass

    logger.info("Saved Gemini Key Pool with %d keys to .env and updated runtime.", len(clean_keys))
    return True


def update_gemini_env_and_runtime(new_key: str, model_name: Optional[str] = None) -> bool:
    """
    Backwards-compatible helper for single key updates.
    Delegates to save_gemini_key_pool.
    """
    clean_key = new_key.strip()
    if not clean_key:
        return False
    return save_gemini_key_pool([clean_key], model_name=model_name)
