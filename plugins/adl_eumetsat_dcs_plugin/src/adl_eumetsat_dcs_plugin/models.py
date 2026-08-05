import re
import zoneinfo

from adl.core.models import DataParameter, NetworkConnection, StationLink, Unit
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from modelcluster.fields import ParentalKey
from wagtail.admin.panels import FieldPanel, MultiFieldPanel, Panel
from wagtail.models import Orderable

from .client import DCSWebServiceClient
from .validators import validate_start_date
from .widgets import DCPSelectWidget

TIMEZONE_CHOICES = [(tz, tz) for tz in sorted(zoneinfo.available_timezones())]


class EumetsatDCSConnection(NetworkConnection):
    """
    Model representing a connection to the EUMETSAT Meteosat Data
    Collection Service (DCS) Web Service.
    """
    station_link_model_string_label = "adl_eumetsat_dcs_plugin.EumetsatDCSStationLink"

    base_url = models.URLField(
        default="https://service.eumetsat.int/dcswebservice",
        verbose_name=_("DCS Web Service Base URL"),
        help_text=_("Note the path is 'dcswebservice' (with an s) — distinct from "
                    "the 'dcpwebservice' public viewer path."),
    )
    dcs_user = models.CharField(max_length=255, verbose_name=_("DCS Username"))
    dcs_password = models.CharField(max_length=255, verbose_name=_("DCS Password"))

    panels = NetworkConnection.panels + [
        MultiFieldPanel([
            FieldPanel("base_url"),
            FieldPanel("dcs_user"),
            FieldPanel("dcs_password"),
        ], heading=_("EUMETSAT DCS Web Service Credentials")),
    ]

    class Meta:
        verbose_name = _("EUMETSAT DCS Connection")
        verbose_name_plural = _("EUMETSAT DCS Connections")

    def get_api_client(self):
        """
        Returns a DCS Web Service client instance.

        A NEW client per call means the client's cached login session
        only lives for one request/task — acceptable: the browse views
        construct one client per view call and the client already
        retries once on a stale session.
        """
        return DCSWebServiceClient(
            baseurl=self.base_url,
            user=self.dcs_user,
            password=self.dcs_password,
        )

    def get_extra_model_admin_links(self):
        return [
            {
                "label": _("Browse DCP Messages"),
                "url": reverse("eumetsat_dcs_browse_messages", args=[self.id]),
                "icon_name": "list-ul",
                "kwargs": {"attrs": {"target": "_blank"}},
            },
            {
                "label": _("Refresh DCP List"),
                "url": reverse("eumetsat_dcs_refresh_dcps", args=[self.id]),
                "icon_name": "rotate",
            },
        ]


class VariableMappingsLinkPanel(Panel):
    """
    Renders a link to the station link's dedicated variable-mapping page
    on the edit form (mappings are managed there, not inline, so the
    channel options can be extracted from the DCP's actual messages).
    Renders nothing on the create form — the page needs a saved pk.
    """

    class BoundPanel(Panel.BoundPanel):
        template_name = "adl_eumetsat_dcs_plugin/panels/variable_mappings_link_panel.html"

        def get_context_data(self, parent_context=None):
            context = super().get_context_data(parent_context)
            context["station_link"] = self.instance
            return context


class EumetsatDCSStationLink(StationLink):
    """
    Links an ADL station to one DCP (Data Collection Platform) ID on a
    DCS Web Service connection.
    """
    dcp_id = models.CharField(
        max_length=32,
        verbose_name=_("DCP ID"),
        help_text=_("Hex DCP address, e.g. 188990C0"),
    )
    observation_timezone = models.CharField(
        max_length=64,
        default="UTC",
        choices=TIMEZONE_CHOICES,
        verbose_name=_("Observation Timezone"),
        help_text=_("Timezone of timestamps inside message bodies, used when "
                    "the body itself does not declare an offset. Format-A XML "
                    "bodies declare their own offset, which takes precedence."),
    )
    start_date = models.DateTimeField(
        blank=True,
        null=True,
        validators=[validate_start_date],
        verbose_name=_("Initial Collection Start Date"),
        help_text=_("The date to start collecting data on the first run. "
                    "Ignored if data has already been collected for this station."),
    )

    # Variable mappings are deliberately NOT edited inline here — they
    # live on a dedicated page (see edit_variable_mappings) where the
    # channel options are extracted from the DCP's actual messages.
    panels = StationLink.panels + [
        FieldPanel("dcp_id", widget=DCPSelectWidget),
        FieldPanel("observation_timezone"),
        FieldPanel("start_date"),
        VariableMappingsLinkPanel(),
    ]

    class Meta:
        verbose_name = _("EUMETSAT DCS Station Link")
        verbose_name_plural = _("EUMETSAT DCS Station Links")

    def __str__(self):
        return f"DCP {self.dcp_id} → {self.station}"

    def clean(self):
        super().clean()
        # Pure-field check only — the core re-runs full_clean() on stored
        # rows outside the request cycle, so no I/O is allowed here.
        # Observed DCP IDs are 8 hex chars (e.g. 188990C0), but that is
        # verified against one platform's addressing only, so length is
        # deliberately not enforced — only that the ID is hex.
        if self.dcp_id:
            self.dcp_id = self.dcp_id.strip().upper()
            if not re.fullmatch(r"[0-9A-F]+", self.dcp_id):
                raise ValidationError({
                    "dcp_id": _("DCP ID must contain only hexadecimal "
                                "characters (0-9, A-F).")
                })

    def get_extra_model_admin_links(self):
        return [
            {
                "label": _("Variable Mappings"),
                "url": reverse("eumetsat_dcs_edit_variable_mappings", args=[self.id]),
                "icon_name": "list-ul",
            },
        ]

    def get_variable_mappings(self):
        """
        Returns the variable mappings for this station link.
        """
        return self.variable_mappings.all()

    def get_first_collection_date(self):
        """
        Returns the first collection date for this station link.
        Returns None if no start date is set.
        """
        return self.start_date


class EumetsatDCSStationLinkVariableMapping(Orderable):
    """
    Maps a DCP message channel/sensor code to an ADL DataParameter.

    Per-station (Pattern A) because channel codes and body encodings vary
    by platform/firmware — the verified Neghele channels (WSAV, WSMN, …)
    are not guaranteed universal.
    """
    station_link = ParentalKey(EumetsatDCSStationLink, on_delete=models.CASCADE,
                               related_name="variable_mappings",
                               verbose_name=_("EUMETSAT DCS Station Link"))
    adl_parameter = models.ForeignKey(DataParameter, on_delete=models.CASCADE,
                                      verbose_name=_("ADL Parameter"))
    channel_id = models.CharField(max_length=64,
                                  verbose_name=_("DCP Channel/Sensor Code"),
                                  help_text=_("As found in message bodies, "
                                              "e.g. WSAV, WSMN, TAAV"))
    channel_unit = models.ForeignKey(Unit, on_delete=models.CASCADE,
                                     verbose_name=_("DCP Channel Unit"))

    panels = [
        FieldPanel("adl_parameter"),
        FieldPanel("channel_id"),
        FieldPanel("channel_unit"),
    ]

    class Meta:
        verbose_name = _("EUMETSAT DCS Station Link Variable Mapping")
        verbose_name_plural = _("EUMETSAT DCS Station Link Variable Mappings")

    @property
    def source_parameter_name(self):
        """
        Returns the DCP channel/sensor code — the key under which this
        variable's value appears in get_station_data records.
        """
        return self.channel_id

    @property
    def source_parameter_unit(self):
        """
        Returns the unit of the DCP channel.
        """
        return self.channel_unit
