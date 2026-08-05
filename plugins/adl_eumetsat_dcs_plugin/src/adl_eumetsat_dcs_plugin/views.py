import logging

import requests
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .client import DCSWebServiceConnectionError
from .models import EumetsatDCSConnection

logger = logging.getLogger(__name__)


def _breadcrumbs(connection, leaf_label):
    return [
        {"url": reverse("wagtailadmin_home"), "label": _("Home")},
        {"url": reverse("eumetsat_dcs_browse_messages", args=[connection.id]),
         "label": _("DCP Messages — %s") % connection.name},
        {"url": None, "label": leaf_label},
    ]


def browse_dcp_messages(request, connection_id):
    """
    Lists the messages the DCS Web Service holds for one of the
    connection's DCPs, via the session-backed ACTION_LIST page.
    """
    connection = get_object_or_404(EumetsatDCSConnection, pk=connection_id)

    # The polymorphic station_links manager yields EumetsatDCSStationLink
    # instances for this connection subclass.
    dcp_ids = [sl.dcp_id for sl in connection.station_links.all()]
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
        error = _("No station links with a DCP ID exist on this connection yet. "
                  "Add one, or pass ?dcp_id= in the URL.")

    context = {
        "connection": connection,
        "dcp_id": dcp_id,
        "dcp_ids": dcp_ids,
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
