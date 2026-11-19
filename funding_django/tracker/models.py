from django.db import models


class Tracking(models.Model):
    SCHEMA_CHOICES = [
        (1, "Classique"),
        (2, "DEV → N tampons → DEV"),
    ]
    STATUS_CHOICES = [
        ("stopped", "Arrêté"),
        ("running", "En cours"),
        ("error", "Erreur"),
    ]

    name = models.CharField(max_length=120, verbose_name="Nom du ruger / analyse")
    root_address = models.CharField(max_length=64)
    schema = models.IntegerField(choices=SCHEMA_CHOICES, default=2)
    tampon_count = models.PositiveIntegerField(default=2)
    minimum_lamports = models.PositiveBigIntegerField(default=1_000_000_000)
    start_slot = models.PositiveBigIntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="stopped")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class WatchedAddress(models.Model):
    tracking = models.ForeignKey(
        Tracking,
        on_delete=models.CASCADE,
        related_name="addresses",
    )
    address = models.CharField(max_length=64)
    role = models.CharField(max_length=20, default="non_classe")
    depth = models.PositiveIntegerField(default=0)
    transactions_examined = models.PositiveIntegerField(default=0)
    outgoing_count = models.PositiveIntegerField(default=0)
    outgoing_lamports = models.PositiveBigIntegerField(default=0)
    incoming_count = models.PositiveIntegerField(default=0)
    incoming_lamports = models.PositiveBigIntegerField(default=0)
    backfill_complete = models.BooleanField(default=False)
    added_manually = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("tracking", "address")]
        ordering = ["depth", "address"]

    def __str__(self):
        return self.address


class Transfer(models.Model):
    tracking = models.ForeignKey(
        Tracking,
        on_delete=models.CASCADE,
        related_name="transfers",
    )
    transfer_id = models.CharField(max_length=200, unique=True)
    source = models.CharField(max_length=64)
    destination = models.CharField(max_length=64)
    lamports = models.PositiveBigIntegerField()
    slot = models.PositiveBigIntegerField()
    signature = models.CharField(max_length=120)
    instruction_position = models.CharField(max_length=50)
    timestamp = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["slot", "transfer_id"]

    def __str__(self):
        return self.transfer_id