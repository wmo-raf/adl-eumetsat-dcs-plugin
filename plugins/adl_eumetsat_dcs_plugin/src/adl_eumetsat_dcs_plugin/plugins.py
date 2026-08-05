from adl.core.registries import Plugin


class PluginNamePlugin(Plugin):
    type = "adl_eumetsat_dcs_plugin"
    label = "ADL EUMETSAT DCS Plugin"
    
    def get_urls(self):
        return []
    
    def get_station_data(self, station_link, start_date=None, end_date=None):
        return []
