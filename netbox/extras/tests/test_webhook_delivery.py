"""
Tests for the WebhookDelivery dead-letter queue: retry/backoff behavior in send_webhook,
the REST API for listing failed deliveries, and the replay action.
"""
from unittest.mock import MagicMock, patch

import django_rq
import requests
from django.urls import reverse
from rest_framework import status

from extras.models import Webhook, WebhookDelivery
from extras.webhooks import send_webhook
from utilities.testing import APITestCase


def _make_response(status_code, body=b'ok'):
    """Build a minimal requests.Response-like mock with the given status and body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body.decode('utf8') if isinstance(body, bytes) else body
    resp.content = body if isinstance(body, bytes) else body.encode('utf8')
    return resp


class SendWebhookRetryTestCase(APITestCase):
    """
    Direct tests of send_webhook's retry loop. The HTTP send is mocked; time.sleep is patched
    out (via the webhooks._sleep indirection) so the tests run in a few ms instead of 30s.
    """

    @classmethod
    def setUpTestData(cls):
        cls.webhook = Webhook.objects.create(
            name='Test Webhook',
            payload_url='http://example.invalid/hook',
            secret='shh',
        )

    def setUp(self):
        super().setUp()
        self.queue = django_rq.get_queue('default')
        self.queue.empty()

    def _build_kwargs(self):
        """Build the keyword args send_webhook expects for the replay/direct path."""
        return {
            'webhook': self.webhook,
            'url': self.webhook.payload_url,
            'request_body': b'{"foo": "bar"}',
            'request_headers': {'Content-Type': 'application/json'},
            'http_method': self.webhook.http_method,
            'ssl_verification': self.webhook.ssl_verification,
            'ca_file_path': self.webhook.ca_file_path,
            'secret': self.webhook.secret,
        }

    @patch('extras.webhooks._sleep', lambda _seconds: None)
    @patch('extras.webhooks._attempt_send')
    def test_success_first_attempt_creates_no_record(self, mock_send):
        """A 200 on the first attempt should not write a WebhookDelivery row."""
        mock_send.return_value = _make_response(200)

        send_webhook(**self._build_kwargs())

        self.assertEqual(WebhookDelivery.objects.count(), 0)
        self.assertEqual(mock_send.call_count, 1)

    @patch('extras.webhooks._sleep', lambda _seconds: None)
    @patch('extras.webhooks._attempt_send')
    def test_success_third_attempt_creates_no_record(self, mock_send):
        """
        If the first two attempts raise ConnectionError but the third returns 200, the call
        succeeds and no WebhookDelivery record is written.
        """
        mock_send.side_effect = [
            requests.ConnectionError('boom 1'),
            requests.ConnectionError('boom 2'),
            _make_response(200),
        ]

        send_webhook(**self._build_kwargs())

        self.assertEqual(WebhookDelivery.objects.count(), 0)
        self.assertEqual(mock_send.call_count, 3)

    @patch('extras.webhooks._sleep', lambda _seconds: None)
    @patch('extras.webhooks._attempt_send')
    def test_all_attempts_fail_writes_dlq_record(self, mock_send):
        """
        If every attempt raises ConnectionError, send_webhook should write exactly one
        WebhookDelivery row with success=False, attempt_count=5, and a non-empty error_message.
        """
        mock_send.side_effect = requests.ConnectionError('unreachable')

        with self.assertRaises(requests.exceptions.RequestException):
            send_webhook(**self._build_kwargs())

        self.assertEqual(mock_send.call_count, 5)
        self.assertEqual(WebhookDelivery.objects.count(), 1)

        delivery = WebhookDelivery.objects.first()
        self.assertFalse(delivery.success)
        self.assertEqual(delivery.attempt_count, 5)
        self.assertTrue(delivery.error_message)
        self.assertIn('unreachable', delivery.error_message)
        self.assertEqual(delivery.webhook, self.webhook)
        self.assertEqual(delivery.url, self.webhook.payload_url)
        self.assertIsNone(delivery.status_code)

    @patch('extras.webhooks._sleep', lambda _seconds: None)
    @patch('extras.webhooks._attempt_send')
    def test_all_attempts_return_5xx_writes_dlq_record(self, mock_send):
        """
        If every attempt returns a 500, a WebhookDelivery row is written capturing the
        final status_code and (truncated) response body.
        """
        long_body = 'x' * 2000
        mock_send.return_value = _make_response(500, long_body)

        with self.assertRaises(requests.exceptions.RequestException):
            send_webhook(**self._build_kwargs())

        self.assertEqual(mock_send.call_count, 5)
        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.attempt_count, 5)
        self.assertEqual(delivery.status_code, 500)
        self.assertFalse(delivery.success)
        # response_body must be truncated to 1000 chars
        self.assertEqual(len(delivery.response_body), 1000)


class WebhookDeliveryAPITestCase(APITestCase):
    """
    Tests for the REST endpoints: list, retrieve, and the replay action.
    """

    @classmethod
    def setUpTestData(cls):
        cls.webhook = Webhook.objects.create(
            name='Test Webhook',
            payload_url='http://example.invalid/hook',
        )
        cls.delivery = WebhookDelivery.objects.create(
            webhook=cls.webhook,
            url='http://example.invalid/hook',
            request_body='{"foo": "bar"}',
            request_headers={'Content-Type': 'application/json'},
            status_code=500,
            response_body='internal server error',
            success=False,
            attempt_count=5,
            error_message='Status 500 returned',
        )

    def setUp(self):
        super().setUp()
        self.queue = django_rq.get_queue('default')
        self.queue.empty()

    def test_list_returns_403_without_permission(self):
        url = reverse('extras-api:webhookdelivery-list')
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_returns_200_with_view_permission(self):
        self.add_permissions('extras.view_webhookdelivery')
        url = reverse('extras-api:webhookdelivery-list')
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], self.delivery.pk)
        # Brief mode is not active — full fields should be present.
        self.assertEqual(response.data['results'][0]['attempt_count'], 5)
        self.assertEqual(response.data['results'][0]['success'], False)

    def test_list_filterable_by_webhook_id(self):
        self.add_permissions('extras.view_webhookdelivery')
        other_webhook = Webhook.objects.create(name='Other', payload_url='http://other.invalid/')
        WebhookDelivery.objects.create(
            webhook=other_webhook,
            url='http://other.invalid/',
            success=False,
            attempt_count=5,
        )
        url = reverse('extras-api:webhookdelivery-list')
        response = self.client.get(f'{url}?webhook_id={self.webhook.pk}', **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], self.delivery.pk)

    def test_detail_returns_200_with_view_permission(self):
        self.add_permissions('extras.view_webhookdelivery')
        url = reverse('extras-api:webhookdelivery-detail', kwargs={'pk': self.delivery.pk})
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], self.delivery.pk)
        self.assertEqual(response.data['attempt_count'], 5)

    def test_replay_returns_202_and_enqueues(self):
        self.add_permissions('extras.view_webhookdelivery', 'extras.change_webhookdelivery')
        url = reverse('extras-api:webhookdelivery-replay', kwargs={'pk': self.delivery.pk})

        # enqueue() just serializes the job and stores it in Redis; the worker is not started
        # in the test, so the actual send_webhook function is never invoked here.
        response = self.client.post(url, **self.header)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data, {'queued': True, 'delivery_id': self.delivery.pk})

        # The original record must still be present.
        self.assertEqual(WebhookDelivery.objects.filter(pk=self.delivery.pk).count(), 1)

        # An RQ job must have been enqueued targeting send_webhook.
        self.assertEqual(self.queue.count, 1)
        job = self.queue.jobs[0]
        self.assertEqual(job.func_name, 'extras.webhooks.send_webhook')
        self.assertEqual(job.kwargs['url'], self.delivery.url)
        self.assertEqual(job.kwargs['webhook'], self.webhook)

    def test_replay_returns_403_without_change_permission(self):
        # View permission only — replay (change) should be denied.
        self.add_permissions('extras.view_webhookdelivery')
        url = reverse('extras-api:webhookdelivery-replay', kwargs={'pk': self.delivery.pk})
        response = self.client.post(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
