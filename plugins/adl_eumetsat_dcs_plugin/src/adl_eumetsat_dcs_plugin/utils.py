import logging
from datetime import datetime, timezone

from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


def get_registered_dcp_options(connection):
    """
    Returns the connection's registered DCPs as {label, value} choices
    for select widgets, sorted by name. All DCPs are listed -- ones that
    have never transmitted are still valid link targets -- with the
    message count annotated so the operator knows what to expect.
    """
    client = connection.get_api_client()
    roster = client.list_registered_dcps()

    options = []
    for dcp in sorted(roster["dcps"], key=lambda d: (d["name"], d["dcp_id"])):
        if dcp["num_messages"]:
            annotation = _("(%d msgs)") % dcp["num_messages"]
        else:
            annotation = _("(no messages)")
        label = f"{dcp['dcp_id']} — {dcp['name']} {annotation}"
        options.append({"label": label, "value": dcp["dcp_id"]})

    return options


def parse_transmission_time(message):
    """
    Parses a DCPMessage header's transmission date/time (DD/MM/YY +
    HH:MM:SS, UTC) into an aware UTC datetime. Returns None (and logs)
    on an unparseable header.
    """
    try:
        tx_time = datetime.strptime(
            f"{message.date} {message.time}", "%d/%m/%y %H:%M:%S"
        )
    except ValueError:
        logger.warning("DCP %s seq %s: unparseable transmission time %r %r, skipped",
                       message.dcp_id, message.sequence, message.date, message.time)
        return None

    return tx_time.replace(tzinfo=timezone.utc)


def extract_observed_channels(messages, since=None):
    """
    Unions the channels observed across a DCP's fetched messages into a
    list of dicts for the variable-mapping page:

        {"channel_id": "WSAV", "name": "WSAV_S" or None,
         "unit": "m/s" or None, "last_seen": aware datetime,
         "sample_value": "0.9"}

    ``since`` (aware datetime) drops messages transmitted before it.
    The mapping page passes now-24h: platforms have been observed to
    change their whole sensor vocabulary over firmware updates, so a
    full-backlog union buries the codes the DCP transmits NOW under
    weeks of dead ones (Neghele: 30 codes all-time vs 9 current).

    "Last seen" is the newest message's *transmission* time (header
    date/time) — chosen over body observation times because it parses
    uniformly across body formats. name/unit come from Format A bodies
    only; a Format B sighting never blanks out a name/unit already
    learned from a Format A message of the same channel. Messages with
    unparsed body encodings contribute nothing, same as ingestion.

    Sorted by channel_id for stable display.
    """
    channels = {}
    for message in messages:
        tx_time = parse_transmission_time(message)
        if tx_time is None:
            continue
        if since is not None and tx_time < since:
            continue
        parsed = message.data
        if parsed is None:
            continue

        for channel in parsed["channels"]:
            entry = channels.setdefault(channel["channel_id"], {
                "channel_id": channel["channel_id"],
                "name": None,
                "unit": None,
                "last_seen": None,
                "sample_value": None,
            })
            if channel.get("name"):
                entry["name"] = channel["name"]
            if channel.get("unit"):
                entry["unit"] = channel["unit"]
            if entry["last_seen"] is None or tx_time >= entry["last_seen"]:
                entry["last_seen"] = tx_time
                entry["sample_value"] = channel.get("value")

    return sorted(channels.values(), key=lambda c: c["channel_id"])
