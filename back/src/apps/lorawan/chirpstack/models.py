"""ORM model for raw LoRaWAN uplinks.

One row per ChirpStack uplink — stores *everything* we receive (battery,
pH, signal, counters, the full codec-decoded object, and the raw payload)
so no device information is lost, even fields we don't graph yet.

Like the analytics sensor models, this table is NOT migrated by agri-api
(schema is created out-of-band — see the deploy step that runs
``schema_editor().create_model(LoraUplink)`` against the prod DB). The
per-metric dashboard write (e.g. PhSoil) still happens separately in the
router; this table is the complete, append-only record.
"""

from __future__ import annotations

from django.db import models


class LoraUplink(models.Model):
    dev_eui = models.CharField(max_length=16, db_index=True)
    device_name = models.CharField(max_length=128, blank=True, default="")
    received_at = models.DateTimeField(db_index=True)

    f_cnt = models.IntegerField(null=True, blank=True)
    f_port = models.IntegerField(null=True, blank=True)
    rssi = models.FloatField(null=True, blank=True, help_text="dBm")
    snr = models.FloatField(null=True, blank=True, help_text="dB")
    frequency = models.BigIntegerField(null=True, blank=True, help_text="Hz")

    # Critical/commonly-queried metrics promoted to columns.
    battery_v = models.FloatField(null=True, blank=True, help_text="Battery voltage")
    ph = models.FloatField(null=True, blank=True)

    # Everything the codec decoded, plus the raw bytes — nothing dropped.
    decoded = models.JSONField(default=dict, blank=True)
    raw_b64 = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        app_label = "chirpstack"
        db_table = "lora_uplink"
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["dev_eui", "-received_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"LoraUplink({self.dev_eui} @ {self.received_at} "
            f"bat={self.battery_v}V ph={self.ph})"
        )
