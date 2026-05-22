import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('extras', '0138_customfieldchoiceset_choice_colors'),
    ]

    operations = [
        migrations.CreateModel(
            name='WebhookDelivery',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('last_updated', models.DateTimeField(auto_now=True)),
                ('url', models.CharField(max_length=500)),
                ('request_body', models.TextField(blank=True)),
                ('request_headers', models.JSONField(blank=True, default=dict)),
                ('status_code', models.IntegerField(blank=True, null=True)),
                ('response_body', models.TextField(blank=True, null=True)),
                ('success', models.BooleanField(default=False)),
                ('attempt_count', models.PositiveSmallIntegerField(default=0)),
                ('error_message', models.CharField(blank=True, max_length=1000, null=True)),
                (
                    'webhook',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='deliveries',
                        to='extras.webhook',
                    ),
                ),
            ],
            options={
                'verbose_name': 'webhook delivery',
                'verbose_name_plural': 'webhook deliveries',
                'ordering': ('-last_updated',),
            },
        ),
    ]
