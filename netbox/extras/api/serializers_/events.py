from rest_framework import serializers

from core.models import ObjectType
from extras.choices import *
from extras.models import EventRule, Webhook, WebhookDelivery
from netbox.api.fields import ChoiceField, ContentTypeField
from netbox.api.gfk_fields import GFKSerializerField
from netbox.api.serializers import NetBoxModelSerializer
from users.api.serializers_.mixins import OwnerMixin

__all__ = (
    'EventRuleSerializer',
    'WebhookDeliverySerializer',
    'WebhookSerializer',
)


#
# Event Rules
#

class EventRuleSerializer(OwnerMixin, NetBoxModelSerializer):
    object_types = ContentTypeField(
        queryset=ObjectType.objects.with_feature('event_rules'),
        many=True
    )
    action_type = ChoiceField(choices=EventRuleActionChoices)
    action_object_type = ContentTypeField(
        queryset=ObjectType.objects.with_feature('event_rules'),
    )
    action_object = GFKSerializerField(read_only=True)

    class Meta:
        model = EventRule
        fields = [
            'id', 'url', 'display_url', 'display', 'object_types', 'name', 'enabled', 'event_types', 'conditions',
            'action_type', 'action_object_type', 'action_object_id', 'action_object', 'description', 'custom_fields',
            'owner', 'tags', 'created', 'last_updated',
        ]
        brief_fields = ('id', 'url', 'display', 'name', 'description')


#
# Webhooks
#

class WebhookSerializer(OwnerMixin, NetBoxModelSerializer):

    class Meta:
        model = Webhook
        fields = [
            'id', 'url', 'display_url', 'display', 'name', 'description', 'payload_url', 'http_method',
            'http_content_type', 'additional_headers', 'body_template', 'secret', 'ssl_verification', 'ca_file_path',
            'custom_fields', 'owner', 'tags', 'created', 'last_updated',
        ]
        brief_fields = ('id', 'url', 'display', 'name', 'description')


#
# Webhook deliveries (dead-letter queue records for failed deliveries)
#

class WebhookDeliverySerializer(serializers.ModelSerializer):
    """
    Serializer for WebhookDelivery — a persisted record of a failed webhook delivery.

    Uses plain ModelSerializer (rather than NetBoxModelSerializer) because WebhookDelivery is
    a lightweight log row with no tags, no custom fields, and no UI view. The related Webhook
    is rendered using WebhookSerializer in nested/brief mode.
    """
    url = serializers.HyperlinkedIdentityField(
        view_name='extras-api:webhookdelivery-detail',
    )
    display = serializers.SerializerMethodField(read_only=True)
    webhook = WebhookSerializer(nested=True, read_only=True, allow_null=True)
    # The model has a `url` field for the destination URL; the top-level `url` key is
    # reserved for the hyperlinked identity field, so expose the destination as `url_called`.
    url_called = serializers.CharField(source='url', read_only=True)

    class Meta:
        model = WebhookDelivery
        fields = [
            'id', 'url', 'display', 'webhook', 'url_called', 'request_body', 'request_headers',
            'status_code', 'response_body', 'success', 'attempt_count', 'error_message',
            'created', 'last_updated',
        ]
        brief_fields = ('id', 'url', 'display', 'success', 'status_code', 'attempt_count')

    def get_display(self, obj):
        return str(obj)
