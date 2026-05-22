import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime

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

# Retry policy: up to 5 attempts total, with exponential backoff between attempts.
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


def _record_failed_delivery(webhook, url, body, headers, status_code, response_body, attempt_count, error_message):
    """
    Persist a WebhookDelivery record when all retry attempts have been exhausted.
    Imported lazily to avoid circular imports at module load time.
    """
    from extras.models import WebhookDelivery

    WebhookDelivery.objects.create(
        webhook=webhook,
        url=url,
        request_body=body if isinstance(body, str) else body.decode('utf8', errors='replace'),
        request_headers=dict(headers) if headers else {},
        status_code=status_code,
        response_body=response_body[:1000] if response_body else None,
        success=False,
        attempt_count=attempt_count,
        error_message=error_message[:500] if error_message else None,
    )


@job('default')
def send_webhook(event_rule, object_type, event_type, data, timestamp, username, request=None, snapshots=None):
    """
    Make a POST request to the defined Webhook
    """
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

    # Prepare the HTTP request
    url = webhook.render_payload_url(context)
    params = {
        'method': webhook.http_method,
        'url': url,
        'headers': headers,
        'data': body.encode('utf8'),
    }
    logger.info(
        f"Sending {params['method']} request to {params['url']} ({context['object_type']} {context['event']})"
    )
    logger.debug(params)
    try:
        prepared_request = requests.Request(**params).prepare()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error forming HTTP request: {e}")
        raise e

    # If a secret key is defined, sign the request with a hash of the key and its content
    if webhook.secret != '':
        prepared_request.headers['X-Hook-Signature'] = generate_signature(prepared_request.body, webhook.secret)

    # Attempt delivery with exponential backoff. A 2xx response at any attempt is a success
    # and we stop immediately. If every attempt fails, a WebhookDelivery record is persisted
    # so an administrator can inspect and replay it.
    last_status_code = None
    last_response_body = None
    last_error_message = None
    attempts_made = 0

    for attempt in range(1, WEBHOOK_MAX_ATTEMPTS + 1):
        attempts_made = attempt
        attempt_ts = datetime.now(UTC).isoformat()
        try:
            with requests.Session() as session:
                session.verify = webhook.ssl_verification
                if webhook.ca_file_path:
                    session.verify = webhook.ca_file_path
                proxies = resolve_proxies(url=url, context={'client': webhook})
                response = session.send(prepared_request, proxies=proxies)

            last_status_code = response.status_code
            try:
                last_response_body = response.text
            except Exception:
                last_response_body = None
            last_error_message = None

            if 200 <= response.status_code <= 299:
                logger.info(
                    f"Attempt {attempt}/{WEBHOOK_MAX_ATTEMPTS} succeeded at {attempt_ts}; "
                    f"response status {response.status_code}"
                )
                return f"Status {response.status_code} returned, webhook successfully processed."

            logger.warning(
                f"Attempt {attempt}/{WEBHOOK_MAX_ATTEMPTS} failed at {attempt_ts}; "
                f"response status {response.status_code}: {response.content!r}"
            )
        except requests.exceptions.RequestException as e:
            last_status_code = None
            last_response_body = None
            last_error_message = str(e) or e.__class__.__name__
            logger.warning(
                f"Attempt {attempt}/{WEBHOOK_MAX_ATTEMPTS} raised at {attempt_ts}: "
                f"{e.__class__.__name__}: {last_error_message}"
            )

        # If more attempts remain, sleep before retrying.
        if attempt < WEBHOOK_MAX_ATTEMPTS:
            delay = WEBHOOK_RETRY_DELAYS[attempt - 1]
            logger.info(f"Retrying webhook delivery in {delay}s (attempt {attempt + 1}/{WEBHOOK_MAX_ATTEMPTS})")
            time.sleep(delay)

    # All attempts exhausted without success — persist a dead-letter record.
    _record_failed_delivery(
        webhook=webhook,
        url=url,
        body=params['data'],
        headers=dict(prepared_request.headers),
        status_code=last_status_code,
        response_body=last_response_body,
        attempt_count=attempts_made,
        error_message=last_error_message,
    )

    if last_status_code is not None:
        raise requests.exceptions.RequestException(
            f"Webhook delivery failed after {attempts_made} attempts; last status {last_status_code}."
        )
    raise requests.exceptions.RequestException(
        f"Webhook delivery failed after {attempts_made} attempts; last error: {last_error_message}"
    )


@job('default')
def _replay_webhook_delivery(delivery_id):
    """
    Replay a previously-failed WebhookDelivery using its stored request body and headers.
    Re-uses the same retry loop semantics as send_webhook: up to 5 attempts with exponential
    backoff, persisting a new WebhookDelivery on full failure. The original record is left
    untouched.
    """
    from extras.models import WebhookDelivery

    delivery = WebhookDelivery.objects.select_related('webhook').get(pk=delivery_id)
    webhook = delivery.webhook
    url = delivery.url
    headers = dict(delivery.request_headers or {})
    body = (delivery.request_body or '').encode('utf8')

    logger.info(f"Replaying webhook delivery #{delivery.pk} to {url}")

    last_status_code = None
    last_response_body = None
    last_error_message = None
    attempts_made = 0

    for attempt in range(1, WEBHOOK_MAX_ATTEMPTS + 1):
        attempts_made = attempt
        attempt_ts = datetime.now(UTC).isoformat()
        try:
            with requests.Session() as session:
                if webhook is not None:
                    session.verify = webhook.ssl_verification
                    if webhook.ca_file_path:
                        session.verify = webhook.ca_file_path
                    proxies = resolve_proxies(url=url, context={'client': webhook})
                else:
                    proxies = resolve_proxies(url=url)
                method = webhook.http_method if webhook is not None else 'POST'
                response = session.request(method=method, url=url, headers=headers, data=body, proxies=proxies)

            last_status_code = response.status_code
            try:
                last_response_body = response.text
            except Exception:
                last_response_body = None
            last_error_message = None

            if 200 <= response.status_code <= 299:
                logger.info(
                    f"Replay attempt {attempt}/{WEBHOOK_MAX_ATTEMPTS} succeeded at {attempt_ts}; "
                    f"response status {response.status_code}"
                )
                return f"Replay succeeded with status {response.status_code}."

            logger.warning(
                f"Replay attempt {attempt}/{WEBHOOK_MAX_ATTEMPTS} failed at {attempt_ts}; "
                f"response status {response.status_code}"
            )
        except requests.exceptions.RequestException as e:
            last_status_code = None
            last_response_body = None
            last_error_message = str(e) or e.__class__.__name__
            logger.warning(
                f"Replay attempt {attempt}/{WEBHOOK_MAX_ATTEMPTS} raised at {attempt_ts}: "
                f"{e.__class__.__name__}: {last_error_message}"
            )

        if attempt < WEBHOOK_MAX_ATTEMPTS:
            delay = WEBHOOK_RETRY_DELAYS[attempt - 1]
            logger.info(f"Retrying replay in {delay}s (attempt {attempt + 1}/{WEBHOOK_MAX_ATTEMPTS})")
            time.sleep(delay)

    _record_failed_delivery(
        webhook=webhook,
        url=url,
        body=body,
        headers=headers,
        status_code=last_status_code,
        response_body=last_response_body,
        attempt_count=attempts_made,
        error_message=last_error_message,
    )

    if last_status_code is not None:
        raise requests.exceptions.RequestException(
            f"Replay failed after {attempts_made} attempts; last status {last_status_code}."
        )
    raise requests.exceptions.RequestException(
        f"Replay failed after {attempts_made} attempts; last error: {last_error_message}"
    )
