from django.urls import path
from wagtail import hooks

from .views import (
    browse_dcp_messages,
    dcp_message_detail,
    edit_variable_mappings,
    get_dcps_for_connection,
    refresh_dcp_list,
)


@hooks.register("register_admin_urls")
def urlconf_eumetsat_dcs_plugin():
    return [
        path(
            "adl-eumetsat-dcs-plugin/conn-dcps/",
            get_dcps_for_connection,
            name="eumetsat_dcs_dcps_for_connection",
        ),
        path(
            "adl-eumetsat-dcs-plugin/refresh-dcps/<int:connection_id>/",
            refresh_dcp_list,
            name="eumetsat_dcs_refresh_dcps",
        ),
        path(
            "adl-eumetsat-dcs-plugin/messages/<int:connection_id>/",
            browse_dcp_messages,
            name="eumetsat_dcs_browse_messages",
        ),
        path(
            "adl-eumetsat-dcs-plugin/message-detail/<int:connection_id>/",
            dcp_message_detail,
            name="eumetsat_dcs_message_detail",
        ),
        path(
            "adl-eumetsat-dcs-plugin/variable-mappings/<int:station_link_id>/",
            edit_variable_mappings,
            name="eumetsat_dcs_edit_variable_mappings",
        ),
    ]
