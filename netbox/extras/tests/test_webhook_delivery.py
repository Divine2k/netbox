from unittest.mock import MagicMock, patch

import django_rq
import requests
from django.urls import reverse
from rest_framework import status

from core.events import OBJECT_CREATED
from core.models import ObjectType
from extras.choices import EventRuleActionChoices
from extras.models import EventRule, Webhook, WebhookDelivery
from extras.webhooks import send_webhook
from utilities.testing import APITestCase


def _build_event_rule():
    webhook = Webhook.objects.create(
        name='Test Webhook',
        payload_url='http://example.invalid/hook',
        secret='',
    )
    site_type = ObjectType.objects.get(app_label='dcim', model='site')
    webhook_type = ObjectType.objects.get(app_label='extras', model='webhook')
    event_rule = EventRule.objects.create(
        name='Test Rule',
        event_types=[OBJECT_CREATED],
        action_type=EventRuleActionChoices.WEBHOOK,
        action_object_type=webhook_type,
        action_object_id=webhook.id,
    )
    event_rule.object_types.set([site_type])
    return event_rule, webhook, site_type


class WebhookDeliveryRetryTestCase(APITestCase):
    """
    Tests for retry-with-backoff behavior in send_webhook(), and for the dead-letter
    WebhookDelivery record that gets written when retries are exhausted.
    """

    def setUp(self):
        super().setUp()
        self.event_rule, self.webhook, self.site_type = _build_event_rule()

    def _send(self):
        # Avoid actually sleeping during retry backoff
        with patch('extras.webhooks.time.sleep'):
            send_webhook(
                event_rule=self.event_rule,
                object_type=self.site_type,
                event_type=OBJECT_CREATED,
                data={'id': 1, 'name': 'Site 1'},
                timestamp='2026-01-01T00:00:00Z',
                username='testuser',
            )

    def test_successful_first_attempt_creates_no_record(self):
        """Success on the very first attempt must not create a WebhookDelivery record."""
        mock_response = MagicMock(status_code=200, text='ok', content=b'ok')

        with patch('extras.webhooks.requests.Session') as session_cls:
            session = session_cls.return_value.__enter__.return_value
            session.send.return_value = mock_response

            self._send()

            self.assertEqual(session.send.call_count, 1)

        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_all_attempts_fail_records_delivery(self):
        """
        When every attempt raises ConnectionError, a WebhookDelivery record must be
        persisted with success=False, attempt_count=5, and a populated error_message.
        send_webhook must re-raise after recording.
        """
        with patch('extras.webhooks.requests.Session') as session_cls:
            session = session_cls.return_value.__enter__.return_value
            session.send.side_effect = requests.ConnectionError('connection refused')

            with self.assertRaises(requests.exceptions.RequestException):
                self._send()

            self.assertEqual(session.send.call_count, 5)

        self.assertEqual(WebhookDelivery.objects.count(), 1)
        delivery = WebhookDelivery.objects.first()
        self.assertFalse(delivery.success)
        self.assertEqual(delivery.attempt_count, 5)
        self.assertTrue(delivery.error_message)
        self.assertIn('connection refused', delivery.error_message)
        self.assertEqual(delivery.webhook, self.webhook)
        self.assertEqual(delivery.url, self.webhook.payload_url)

    def test_recovery_on_third_attempt_creates_no_record(self):
        """
        When the first two attempts fail and the third succeeds, no WebhookDelivery
        record should be written and no further attempts should be made.
        """
        fail = MagicMock(status_code=503, text='unavailable', content=b'unavailable')
        ok = MagicMock(status_code=200, text='ok', content=b'ok')

        with patch('extras.webhooks.requests.Session') as session_cls:
            session = session_cls.return_value.__enter__.return_value
            session.send.side_effect = [fail, fail, ok]

            self._send()

            self.assertEqual(session.send.call_count, 3)

        self.assertEqual(WebhookDelivery.objects.count(), 0)


class WebhookDeliveryAPITestCase(APITestCase):
    """
    Tests for the /api/extras/webhook-deliveries/ REST API endpoints.
    """

    def setUp(self):
        super().setUp()
        self.event_rule, self.webhook, self.site_type = _build_event_rule()
        self.delivery = WebhookDelivery.objects.create(
            webhook=self.webhook,
            url=self.webhook.payload_url,
            request_body='{"hello":"world"}',
            request_headers={'Content-Type': 'application/json'},
            status_code=None,
            response_body=None,
            success=False,
            attempt_count=5,
            error_message='connection refused',
        )

    def test_list_requires_view_permission(self):
        """GET without permission must return 403, with permission must return 200."""
        url = reverse('extras-api:webhookdelivery-list')

        # Without permission: forbidden
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        # With permission: ok and our delivery is included
        self.add_permissions('extras.view_webhookdelivery')
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], self.delivery.pk)

    def test_replay_enqueues_and_returns_202(self):
        """
        POST replay must return 202 with the queued payload and must enqueue a
        background job. The original delivery record must not be deleted.
        """
        url = reverse('extras-api:webhookdelivery-replay', args=[self.delivery.pk])
        self.add_permissions('extras.view_webhookdelivery', 'extras.change_webhookdelivery')

        queue = django_rq.get_queue('default')
        queue.empty()

        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data, {'queued': True, 'delivery_id': self.delivery.pk})
        self.assertEqual(queue.count, 1)

        # The original record must still exist
        self.assertTrue(WebhookDelivery.objects.filter(pk=self.delivery.pk).exists())
