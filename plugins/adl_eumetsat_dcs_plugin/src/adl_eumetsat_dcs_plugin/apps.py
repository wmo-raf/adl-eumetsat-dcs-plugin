from django.apps import AppConfig

from adl.core.registries import plugin_registry


class EumetsatDcsPluginConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = "adl_eumetsat_dcs_plugin"

    def ready(self):
        from .plugins import EumetsatDCSPlugin

        plugin_registry.register(EumetsatDCSPlugin())
