from adl.core.models import DataParameter, Unit
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import EumetsatDCSStationLinkVariableMapping


class EumetsatDCSVariableMappingForm(forms.ModelForm):
    """
    One row of the per-station variable-mapping page. channel_id is a
    combobox: a text input backed by the page-level <datalist> of
    channels extracted from the DCP's recent messages, so codes not
    present in the backlog (dead sensor, DCS outage) can still be typed
    in free-text.
    """

    class Meta:
        model = EumetsatDCSStationLinkVariableMapping
        fields = ["adl_parameter", "channel_id", "channel_unit"]
        widgets = {
            "channel_id": forms.TextInput(attrs={
                "list": "observed-channel-options",
                "autocomplete": "off",
                "placeholder": _("e.g. WSAV"),
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["adl_parameter"].queryset = DataParameter.objects.all().order_by("name")
        self.fields["channel_unit"].queryset = Unit.objects.all().order_by("name")

    def clean_channel_id(self):
        # Same normalization the ingestion side expects: codes are
        # matched verbatim against message bodies, so only trim
        # whitespace — do NOT case-fold (channel codes are
        # case-sensitive in the bodies).
        value = self.cleaned_data.get("channel_id", "")
        value = value.strip()
        if not value:
            raise forms.ValidationError(_("Channel code is required."))
        return value
