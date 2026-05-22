import hashlib
import hmac
import logging
import time

import requests
from django_rq import job
from jinja2.exceptions import TemplateError

from netbox.registry import registry
from utilities.proxy import resolve_proxies

from .constants import WEBHOOK_EVENT_TYPES

__all__ = (
    'generate_signature',
    'register_webhook_callback',
    'send_webhook',
)

logger = logging.getLogger('netbox.webhooks')

# Retry configuration: 5 total attempts with exponential backoff delays between them.
# Delays are 1s, 2s, 4s, 8s, 16s after attempts 1..5. The delay after the final attempt
# is never used (loop exits first), but we keep the list length aligned to attempt count
# so an off-by-one in indexing is impossible.
WEBHOOK_MAX_ATTEMPTS = 5
WEBHOOK_RETRY_DELAYS = (1, 2, 4, 8, 16)


def register_webhook_callback(func):
    """
    Register a function as a webhook callback.
    """
    registry['webhook_callbacks'].append(func)
    logger.debug(f'Registered webhook callback {func.__module__}.{func.__name__}')
    return func


def generate_signature(request_body, secret):
    """
    Return a cryptographic signature that can be used to verify the authenticity of webhook data.
    """
    hmac_prep = hmac.new(
        key=secret.encode('utf8'),
        msg=request_body,
        digestmod=hashlib.sha512
    )
    return hmac_prep.hexdigest()


def _sleep(seconds):
    """
    Indirection point so tests can patch time.sleep at the webhooks module level
    without interfering with other code paths that also use time.sleep().
    """
    time.sleep(seconds)


def _attempt_send(session, prepared_request, proxies):
    """
    Send a single prepared request and return the response. Raised exceptions propagate.
    """
    return session.send(prepared_request, proxies=proxies)


def _record_failed_delivery(webhook, url, request_body, request_headers, status_code, response_body,
                            attempt_count, error_message):
    """
    Persist a WebhookDelivery row for a failed delivery. Imported lazily to avoid module-load
    ordering issues (this module is imported via the RQ worker before Django app loading
    completes in some environments).
    """
    from extras.models import WebhookDelivery

    truncated_response = response_body[:1000] if response_body is not None else None
    truncated_error = error_message[:1000] if error_message else None

    WebhookDelivery.objects.create(
        webhook=webhook,
        url=url,
        request_body=request_body,
        request_headers=request_headers,
        status_code=status_code,
        response_body=truncated_response,
        success=False,
        attempt_count=attempt_count,
        error_message=truncated_error,
    )


@job('default')
def send_webhook(event_rule=None, object_type=None, event_type=None, data=None, timestamp=None,
                 username=None, request=None, snapshots=None, webhook=None, url=None,
                 request_body=None, request_headers=None, http_method=None, ssl_verification=True,
                 ca_file_path=None, secret=None):
    """
    Make a POST request to the defined Webhook with exponential-backoff retry.

    Two invocation modes are supported:

    1. Normal event-driven dispatch (the original path): caller supplies event_rule, object_type,
       event_type, data, timestamp, username, and optionally request/snapshots. The destination
       URL, headers, and body are rendered from the Webhook's templates.

    2. Replay dispatch: caller supplies webhook plus a pre-rendered url, request_body,
       request_headers, http_method, and TLS settings. Used by the WebhookDelivery replay
       action to re-send a previously captured request without re-rendering templates.
    """
    if event_rule is not None:
        webhook = event_rule.action_object

        # Prepare context data for headers & body templates
        context = {
            'event': WEBHOOK_EVENT_TYPES.get(event_type, event_type),
            'timestamp': timestamp,
            'object_type': '.'.join(object_type.natural_key()),
            'username': username,
            'request_id': request.id if request else None,
            'data': data,
        }
        if request:
            context['request'] = {
                'id': str(request.id) if request.id else None,
                'method': request.method,
                'path': request.path,
                'user': str(request.user),
            }
        if snapshots:
            context.update({
                'snapshots': snapshots
            })

        # Add any additional context from plugins
        callback_data = {}
        for callback in registry['webhook_callbacks']:
            try:
                if ret := callback(object_type, event_type, data, request):
                    callback_data.update(**ret)
            except Exception as e:
                logger.warning(f"Caught exception when processing callback {callback}: {e}")
                pass
        if callback_data:
            context['context'] = callback_data

        # Build the headers for the HTTP request
        headers = {
            'Content-Type': webhook.http_content_type,
        }
        try:
            headers.update(webhook.render_headers(context))
        except (TemplateError, ValueError) as e:
            logger.error(f"Error parsing HTTP headers for webhook {webhook}: {e}")
            raise e

        # Render the request body
        try:
            body = webhook.render_body(context)
        except TemplateError as e:
            logger.error(f"Error rendering request body for webhook {webhook}: {e}")
            raise e

        url = webhook.render_payload_url(context)
        request_body = body.encode('utf8')
        request_headers = headers
        http_method = webhook.http_method
        ssl_verification = webhook.ssl_verification
        ca_file_path = webhook.ca_file_path
        secret = webhook.secret
    else:
        # Replay path: caller supplied the already-prepared payload.
        if webhook is None or url is None or http_method is None:
            raise ValueError("send_webhook requires either event_rule or (webhook, url, http_method) arguments")
        if isinstance(request_body, str):
            request_body = request_body.encode('utf8')

    # Prepare the HTTP request
    params = {
        'method': http_method,
        'url': url,
        'headers': request_headers or {},
        'data': request_body,
    }
    logger.info(f"Sending {params['method']} request to {params['url']}")
    logger.debug(params)
    try:
        prepared_request = requests.Request(**params).prepare()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error forming HTTP request: {e}")
        raise e

    # If a secret key is defined, sign the request with a hash of the key and its content
    if secret:
        prepared_request.headers['X-Hook-Signature'] = generate_signature(prepared_request.body, secret)

    # Pre-compute proxies once; they do not change between attempts.
    proxies = resolve_proxies(url=url, context={'client': webhook}) if webhook else None

    # Retry loop with exponential backoff. Stop early on any 2xx response.
    last_status = None
    last_response_body = None
    last_error = None
    attempt = 0
    success = False

    for attempt in range(1, WEBHOOK_MAX_ATTEMPTS + 1):
        attempt_started = time.time()
        try:
            with requests.Session() as session:
                session.verify = ssl_verification
                if ca_file_path:
                    session.verify = ca_file_path
                response = _attempt_send(session, prepared_request, proxies)
            last_status = response.status_code
            try:
                last_response_body = response.text
            except Exception:  # noqa: BLE001
                last_response_body = None

            if 200 <= response.status_code <= 299:
                logger.info(
                    f"Webhook attempt {attempt} succeeded; status {response.status_code} "
                    f"at {time.time():.0f}"
                )
                success = True
                break

            last_error = None
            logger.warning(
                f"Webhook attempt {attempt} failed with status {response.status_code} "
                f"at {time.time():.0f} (after {time.time() - attempt_started:.2f}s)"
            )
        except requests.exceptions.RequestException as e:
            last_status = None
            last_response_body = None
            last_error = str(e) or e.__class__.__name__
            logger.warning(
                f"Webhook attempt {attempt} raised {e.__class__.__name__}: {last_error} "
                f"at {time.time():.0f}"
            )

        if attempt < WEBHOOK_MAX_ATTEMPTS:
            _sleep(WEBHOOK_RETRY_DELAYS[attempt - 1])

    if success:
        return f"Status {last_status} returned, webhook successfully processed."

    # All attempts exhausted. Persist a dead-letter record for inspection/replay.
    try:
        # request_headers may be bytes-ish from an httplib.HTTPMessage-like obj in tests;
        # coerce to plain dict for JSONField.
        stored_headers = dict(request_headers) if request_headers else {}
        # Strip the signature header — it depends on body+secret and can be re-derived;
        # storing it offers no value and could leak rotation history.
        stored_headers.pop('X-Hook-Signature', None)

        stored_body = request_body.decode('utf8') if isinstance(request_body, bytes) else (request_body or '')

        _record_failed_delivery(
            webhook=webhook,
            url=url,
            request_body=stored_body,
            request_headers=stored_headers,
            status_code=last_status,
            response_body=last_response_body,
            attempt_count=attempt,
            error_message=last_error,
        )
    except Exception as e:  # noqa: BLE001
        # Never let a DLQ write error mask the original failure.
        logger.error(f"Failed to persist WebhookDelivery record: {e}")

    if last_status is not None:
        raise requests.exceptions.RequestException(
            f"Status {last_status} returned after {attempt} attempts, webhook FAILED to process."
        )
    raise requests.exceptions.RequestException(
        f"Webhook FAILED to process after {attempt} attempts: {last_error}"
    )
