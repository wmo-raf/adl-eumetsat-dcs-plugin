from django.forms import Widget
from django.urls import reverse


class DCPSelectWidget(Widget):
    template_name = "adl_eumetsat_dcs_plugin/widgets/dcp_select_widget.html"

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context.update({
            "dcps_url": reverse("eumetsat_dcs_dcps_for_connection"),
        })
        return context
