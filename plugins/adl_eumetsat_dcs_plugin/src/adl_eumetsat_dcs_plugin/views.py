import logging
from datetime import timedelta

import requests
from adl.core.utils import get_object_or_none
from django.contrib import messages
from django.forms import inlineformset_factory
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _

from .client import DCSWebServiceConnectionError
from .forms import EumetsatDCSVariableMappingForm
from .models import (
    EumetsatDCSConnection,
    EumetsatDCSStationLink,
    EumetsatDCSStationLinkVariableMapping,
)
from .utils import extract_observed_channels, get_registered_dcp_options

logger = logging.getLogger(__name__)

# Ladder for the mapping page's "Observed Channels" window selector,
# filtered on message transmission time. Escalates in roughly-x3 steps
# so an operator hunting a quiet DCP reaches "everything" in three
# clicks. A None delta means no filtering at all.
#
# Widening is free: ACTION_DOWNLOAD ignores date filters and always
# returns the full backlog (see client.build_download_url), so `since`
# is a pure post-filter over messages already in memory. Nothing above
# ~30d is offered because the service's own retention has been observed
# at ~28 days — a "30d" rung would render identically to "All time".
OBSERVED_CHANNELS_WINDOWS = (
    ("6h", _("Last 6 hours"), timedelta(hours=6)),
    ("24h", _("Last 24 hours"), timedelta(hours=24)),
    ("3d", _("Last 3 days"), timedelta(days=3)),
    ("7d", _("Last 7 days"), timedelta(days=7)),
    ("all", _("All time"), None),
)

DEFAULT_OBSERVED_CHANNELS_WINDOW = "24h"


def resolve_observed_channels_window(value):
    """
    Maps a ``?window=`` token to its ``(token, label, delta)`` rung.

    Unrecognised tokens — hand-edited URLs, stale bookmarks — fall
    back to the default silently rather than erroring: this is a
    display filter, and an unusable page is a worse answer than a
    24-hour one.
    """
    for rung in OBSERVED_CHANNELS_WINDOWS:
        if rung[0] == value:
            return rung
    for rung in OBSERVED_CHANNELS_WINDOWS:
        if rung[0] == DEFAULT_OBSERVED_CHANNELS_WINDOW:
            return rung


VariableMappingFormSet = inlineformset_factory(
    EumetsatDCSStationLink,
    EumetsatDCSStationLinkVariableMapping,
    form=EumetsatDCSVariableMappingForm,
    extra=1,
    can_delete=True,
)


def _breadcrumbs(connection, leaf_label):
    return [
        {"url": reverse("wagtailadmin_home"), "label": _("Home")},
        {"url": reverse("eumetsat_dcs_browse_messages", args=[connection.id]),
         "label": _("DCP Messages — %s") % connection.name},
        {"url": None, "label": leaf_label},
    ]


def get_dcps_for_connection(request):
    """
    AJAX endpoint feeding the DCP select widget: the connection's
    registered DCPs as {label, value} options (24h-cached roster).
    """
    network_connection_id = request.GET.get("connection_id")

    if not network_connection_id:
        return JsonResponse({"error": _("Network connection ID is required.")}, status=400)

    connection = get_object_or_none(EumetsatDCSConnection, pk=network_connection_id)
    if not connection:
        return JsonResponse(
            {"error": _("The selected connection is not a EUMETSAT DCS Connection.")},
            status=400,
        )

    try:
        options = get_registered_dcp_options(connection)
    except (DCSWebServiceConnectionError, requests.RequestException) as e:
        logger.error("Registered-DCP fetch failed for connection %s: %s",
                     network_connection_id, e)
        return JsonResponse({"error": str(e)}, status=502)

    return JsonResponse(options, safe=False)


def refresh_dcp_list(request, connection_id):
    """
    Clears the cached registered-DCP roster so the next widget load or
    browse-page render re-scrapes the DCP_ADMIN page.
    """
    connection = get_object_or_404(EumetsatDCSConnection, pk=connection_id)

    connection.get_api_client().clear_registered_dcps_cache()
    messages.success(request, _("DCP list cache cleared — it will be refetched "
                                "from the DCS Web Service on next use."))

    # The Referer header is client-supplied — only follow it back to our
    # own host, else land on the browse page.
    referer = request.META.get("HTTP_REFERER")
    if referer and url_has_allowed_host_and_scheme(
            referer, allowed_hosts={request.get_host()},
            require_https=request.is_secure()):
        return redirect(referer)
    return redirect(reverse("eumetsat_dcs_browse_messages", args=[connection.id]))


def browse_dcp_messages(request, connection_id):
    """
    Lists the messages the DCS Web Service holds for one of the
    connection's DCPs, via the session-backed ACTION_LIST page.
    """
    connection = get_object_or_404(EumetsatDCSConnection, pk=connection_id)

    # The picker prefers the registered-DCP roster (so unlinked DCPs can
    # be inspected before creating their station links); if the roster
    # fetch fails, fall back to the DCP IDs of linked stations. The
    # polymorphic station_links manager yields EumetsatDCSStationLink
    # instances for this connection subclass.
    try:
        dcp_options = get_registered_dcp_options(connection)
    except (DCSWebServiceConnectionError, requests.RequestException) as e:
        logger.warning("Registered-DCP roster unavailable for connection %s, "
                       "falling back to linked stations: %s", connection_id, e)
        dcp_options = [{"label": sl.dcp_id, "value": sl.dcp_id}
                       for sl in connection.station_links.all()]

    dcp_ids = [o["value"] for o in dcp_options]
    dcp_id = request.GET.get("dcp_id") or (dcp_ids[0] if dcp_ids else None)

    listing = None
    error = None
    if dcp_id:
        client = connection.get_api_client()
        try:
            listing = client.list_available(dcp_id)
        # requests errors surface raw from the client (a dead/slow JSP
        # server raises before any status-code check converts them) —
        # render in-page rather than 500ing the admin.
        except (DCSWebServiceConnectionError, requests.RequestException) as e:
            logger.error("DCS browse failed for DCP %s: %s", dcp_id, e)
            error = str(e)
    else:
        error = _("No registered DCPs found and no station links with a DCP ID "
                  "exist on this connection yet. Add one, or pass ?dcp_id= in "
                  "the URL.")

    context = {
        "connection": connection,
        "dcp_id": dcp_id,
        "dcp_ids": dcp_ids,
        "dcp_options": dcp_options,
        "listing": listing,
        "error": error,
        "breadcrumbs_items": [
            {"url": reverse("wagtailadmin_home"), "label": _("Home")},
            {"url": None, "label": _("DCP Messages — %s") % connection.name},
        ],
        "header_title": _("DCP Messages — %s") % connection.name,
        "header_icon": "list-ul",
    }
    return render(request, "adl_eumetsat_dcs_plugin/dcp_messages.html", context)


def edit_variable_mappings(request, station_link_id):
    """
    Per-station variable-mapping page: an "observed channels" table
    extracted live from the DCP's recent messages, above an editable
    mapping formset. The download is always the full backlog (the
    server ignores date filters) — the window is an extraction filter
    so the table shows the DCP's CURRENT sensor vocabulary rather than
    burying it under weeks of dead codes.

    ``?window=`` widens that filter on demand (see
    OBSERVED_CHANNELS_WINDOWS) for hunting a DCP that has been quiet
    longer than the 24h default. It survives a save, so mapping a
    channel found at a wide window does not snap the table back to
    empty. If the DCS fetch fails, the formset stays fully usable via
    free-text channel entry.
    """
    station_link = get_object_or_404(EumetsatDCSStationLink, pk=station_link_id)
    connection = station_link.network_connection

    window_token, window_label, window_delta = resolve_observed_channels_window(
        request.GET.get("window")
    )
    since = timezone.now() - window_delta if window_delta else None

    channels = []
    channels_error = None
    client = connection.get_api_client()
    try:
        dcp_messages = client.get_latest_observations(station_link.dcp_id)
        channels = extract_observed_channels(dcp_messages, since=since)
    except (DCSWebServiceConnectionError, requests.RequestException) as e:
        logger.error("Channel extraction failed for DCP %s: %s",
                     station_link.dcp_id, e)
        channels_error = str(e)

    if request.method == "POST":
        formset = VariableMappingFormSet(request.POST, instance=station_link)
        if formset.is_valid():
            formset.save()
            messages.success(request, _("Variable mappings saved."))
            # Full path, not request.path: keeps ?window= across the save
            # so the table the operator was working from stays on screen.
            return redirect(request.get_full_path())
    else:
        formset = VariableMappingFormSet(instance=station_link)

    mapped_codes = set(
        station_link.variable_mappings.values_list("channel_id", flat=True)
    )
    for channel in channels:
        channel["mapped"] = channel["channel_id"] in mapped_codes

    title = _("Variable Mappings — %s") % station_link
    context = {
        "station_link": station_link,
        "connection": connection,
        "channels": channels,
        "channels_error": channels_error,
        "window_token": window_token,
        "window_label": window_label,
        "window_options": [{"value": t, "label": lbl}
                           for t, lbl, _delta in OBSERVED_CHANNELS_WINDOWS],
        "formset": formset,
        "breadcrumbs_items": [
            {"url": reverse("wagtailadmin_home"), "label": _("Home")},
            {"url": None, "label": title},
        ],
        "header_title": title,
        "header_icon": "list-ul",
    }
    return render(request, "adl_eumetsat_dcs_plugin/variable_mappings.html", context)


def dcp_message_detail(request, connection_id):
    """
    Renders one message via the stateful "Show" page flow. The date must
    be passed EXACTLY as returned by list_available — the JSP flow
    requires an exact string match.
    """
    connection = get_object_or_404(EumetsatDCSConnection, pk=connection_id)
    dcp_id = request.GET.get("dcp_id")
    date = request.GET.get("date")

    detail = None
    raw_body_repr = None
    error = None
    if dcp_id and date:
        client = connection.get_api_client()
        try:
            detail = client.get_message_detail(dcp_id, date)
            raw_body_repr = repr(detail["raw_body"])
        except (DCSWebServiceConnectionError, requests.RequestException) as e:
            logger.error("DCS message detail failed for DCP %s at %s: %s", dcp_id, date, e)
            error = str(e)
    else:
        error = _("Both dcp_id and date query parameters are required.")

    context = {
        "connection": connection,
        "dcp_id": dcp_id,
        "date": date,
        "detail": detail,
        "raw_body_repr": raw_body_repr,
        "error": error,
        "breadcrumbs_items": _breadcrumbs(connection, _("Message %s") % (date or "")),
        "header_title": _("DCP Message — %s") % (date or ""),
        "header_icon": "list-ul",
    }
    return render(request, "adl_eumetsat_dcs_plugin/dcp_message_detail.html", context)
