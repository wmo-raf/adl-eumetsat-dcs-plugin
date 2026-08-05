from django.utils.translation import gettext as _


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
