from decimal import Decimal, InvalidOperation
from django import forms
from .models import Tracking
from .engine import valid_address, LAMPORTS_PER_SOL


class TrackingForm(forms.ModelForm):
    minimum_sol = forms.DecimalField(
        min_value=Decimal("0.000000001"),
        initial=Decimal("1"),
        decimal_places=9,
        label="Seuil par transfert en SOL",
    )
    start_slot = forms.IntegerField(min_value=0, label="Premier slot inclus")

    class Meta:
        model = Tracking
        fields = ["name", "root_address", "schema", "tampon_count", "minimum_sol", "start_slot"]
        labels = {
            "name": "Nom du ruger / analyse",
            "root_address": "Adresse initiale",
            "schema": "Schéma",
            "tampon_count": "Nombre de tampons entre deux DEV",
        }

    def clean_root_address(self):
        address = self.cleaned_data["root_address"].strip()
        if not valid_address(address):
            raise forms.ValidationError("Adresse Solana invalide.")
        return address

    def clean(self):
        cleaned = super().clean()
        schema = cleaned.get("schema")
        tampon_count = cleaned.get("tampon_count", 0)
        if schema == 2 and tampon_count < 0:
            self.add_error("tampon_count", "Le nombre de tampons doit être positif ou nul.")
        return cleaned

    def get_minimum_lamports(self):
        value = self.cleaned_data["minimum_sol"]
        return int((value * LAMPORTS_PER_SOL).to_integral_value(rounding="ROUND_CEILING"))


class AddAddressForm(forms.Form):
    address = forms.CharField(max_length=64, label="Adresse Solana à suivre")

    def clean_address(self):
        address = self.cleaned_data["address"].strip()
        if not valid_address(address):
            raise forms.ValidationError("Adresse Solana invalide.")
        return address