import logging

import requests
from adl.core.utils import get_object_or_none
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _

from .client import DCSWebServiceConnectionError
from .models import EumetsatDCSConnection
from .utils import get_registered_dcp_options

logger = logging.getLogger(__name__)


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
