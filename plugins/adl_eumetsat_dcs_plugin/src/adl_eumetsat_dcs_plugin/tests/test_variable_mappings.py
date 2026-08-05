from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from django.utils import timezone as dj_timezone

from adl.core.tests.factories import (
    DataParameterFactory,
    NetworkFactory,
    StationFactory,
    UnitFactory,
)
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from adl_eumetsat_dcs_plugin.client import DCPMessage, DCSWebServiceConnectionError
from adl_eumetsat_dcs_plugin.models import (
    EumetsatDCSConnection,
    EumetsatDCSStationLink,
    EumetsatDCSStationLinkVariableMapping,
)
from adl_eumetsat_dcs_plugin.utils import extract_observed_channels

from .helpers import (
    BODY_PREAMBLE,
    COLON_TAG_BODY,
    FORMAT_A_BODY_WITH_ERRORCODE,
    FORMAT_B_BODY,
    build_bulk,
)


def format_a_wind_body(wsav_value):
    """FORMAT_A_BODY with a parameterized WSAV value, for last-seen tests."""
    return BODY_PREAMBLE + (
        b'<?xml version="1.0"?><StationDataList>'
        b'<StationData stationId="000NEGHELE" name="Neghele" timezone="+03:00">'
        b'<ChannelData channelId="WSAV" name="WSAV_S" unit="m/s">'
        b'<Values><VT t="2026-07-08T15:50:00">' + wsav_value + b'</VT></Values></ChannelData>'
        b'</StationData></StationDataList>'
    )


def messages_from_specs(*specs):
    return list(DCPMessage.iter_from_bulk(build_bulk(*specs)))


def tx_spec(dt):
    """Header date/time spec kwargs for a transmission datetime (UTC)."""
    return {"date": dt.strftime("%d/%m/%y").encode(),
            "time_": dt.strftime("%H:%M:%S").encode()}


class ExtractObservedChannelsTests(SimpleTestCase):
    def test_unions_channels_across_backlog_with_last_seen(self):
        msgs = messages_from_specs(
            {"body": format_a_wind_body(b"0.9"), "sequence": 1,
             "date": b"04/08/26", "time_": b"10:00:00"},
            {"body": format_a_wind_body(b"1.7"), "sequence": 2,
             "date": b"05/08/26", "time_": b"10:00:00"},
        )

        channels = extract_observed_channels(msgs)

        self.assertEqual(len(channels), 1)
        wsav = channels[0]
        self.assertEqual(wsav["channel_id"], "WSAV")
        self.assertEqual(wsav["name"], "WSAV_S")
        self.assertEqual(wsav["unit"], "m/s")
        self.assertEqual(wsav["sample_value"], "1.7")
        self.assertEqual(wsav["last_seen"],
                         datetime(2026, 8, 5, 10, 0, 0, tzinfo=timezone.utc))

    def test_older_message_does_not_overwrite_newer_sample(self):
        # File order newest-first must not matter.
        msgs = messages_from_specs(
            {"body": format_a_wind_body(b"1.7"), "sequence": 2,
             "date": b"05/08/26", "time_": b"10:00:00"},
            {"body": format_a_wind_body(b"0.9"), "sequence": 1,
             "date": b"04/08/26", "time_": b"10:00:00"},
        )

        channels = extract_observed_channels(msgs)

        self.assertEqual(channels[0]["sample_value"], "1.7")

    def test_format_b_sighting_keeps_format_a_name_and_unit(self):
        msgs = messages_from_specs(
            {"body": FORMAT_A_BODY_WITH_ERRORCODE, "sequence": 1,
             "date": b"04/08/26", "time_": b"10:00:00"},
            {"body": FORMAT_B_BODY, "sequence": 2,
             "date": b"05/08/26", "time_": b"12:00:00"},
        )

        channels = extract_observed_channels(msgs)

        taav = next(c for c in channels if c["channel_id"] == "TAAV")
        self.assertEqual(taav["name"], "TAAV_S")
        self.assertEqual(taav["unit"], "degC")
        # ...but last_seen/sample still advance to the newer Format B message.
        self.assertEqual(taav["sample_value"], "23.5")
        self.assertEqual(taav["last_seen"],
                         datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc))

    def test_since_drops_messages_transmitted_before_it(self):
        msgs = messages_from_specs(
            {"body": format_a_wind_body(b"0.9"), "sequence": 1,
             "date": b"04/08/26", "time_": b"10:00:00"},
            {"body": FORMAT_B_BODY, "sequence": 2,
             "date": b"05/08/26", "time_": b"12:00:00"},
        )
        since = datetime(2026, 8, 5, 0, 0, 0, tzinfo=timezone.utc)

        codes = [c["channel_id"] for c in extract_observed_channels(msgs, since=since)]

        self.assertEqual(codes, ["TAAV"])

    def test_unparsed_body_encodings_contribute_nothing(self):
        msgs = messages_from_specs({"body": COLON_TAG_BODY})

        self.assertEqual(extract_observed_channels(msgs), [])

    def test_sorted_by_channel_id(self):
        msgs = messages_from_specs(
            {"body": FORMAT_B_BODY, "sequence": 1},
            {"body": format_a_wind_body(b"0.9"), "sequence": 2},
        )

        codes = [c["channel_id"] for c in extract_observed_channels(msgs)]
        self.assertEqual(codes, ["TAAV", "WSAV"])


class EditVariableMappingsViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        network = NetworkFactory()
        cls.connection = EumetsatDCSConnection.objects.create(
            name="DCS Test Connection",
            network=network,
            plugin="adl_eumetsat_dcs_plugin",
            dcs_user="user",
            dcs_password="pass",
        )
        cls.station_link = EumetsatDCSStationLink.objects.create(
            network_connection=cls.connection,
            station=StationFactory(network=network),
            dcp_id="188990C0",
        )
        cls.url = reverse("eumetsat_dcs_edit_variable_mappings",
                          args=[cls.station_link.id])

    def setUp(self):
        user = get_user_model().objects.create_superuser(
            username="admin", email="admin@example.com", password="pw"
        )
        self.client.force_login(user)

    def _mock_client(self, messages=None, error=None):
        client = MagicMock()
        if error is not None:
            client.get_latest_observations.side_effect = error
        else:
            client.get_latest_observations.return_value = messages or []
        return patch.object(EumetsatDCSConnection, "get_api_client",
                            return_value=client)

    def test_renders_observed_channels_with_mapped_indicator(self):
        # Create via the model manager, NOT station_link.variable_mappings
        # .create() — modelcluster stages child rows in memory until the
        # parent is saved, so they'd never reach the DB the view reads.
        EumetsatDCSStationLinkVariableMapping.objects.create(
            station_link=self.station_link,
            adl_parameter=DataParameterFactory(),
            channel_id="WSAV",
            channel_unit=UnitFactory(),
        )
        # The view only shows channels from the last 24h of messages, so
        # transmission times must be relative to the test run -- fixed
        # dates would age out of the window.
        now = dj_timezone.now()
        msgs = messages_from_specs(
            {"body": format_a_wind_body(b"0.9"), "sequence": 1,
             **tx_spec(now - timedelta(hours=2))},
            {"body": FORMAT_B_BODY, "sequence": 2,
             **tx_spec(now - timedelta(hours=1))},
            # Outside the 24h window: must NOT appear in the table.
            {"body": FORMAT_A_BODY_WITH_ERRORCODE, "sequence": 3,
             **tx_spec(now - timedelta(days=20))},
        )

        with self._mock_client(messages=msgs):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        channels = {c["channel_id"]: c for c in response.context["channels"]}
        self.assertTrue(channels["WSAV"]["mapped"])
        self.assertFalse(channels["TAAV"]["mapped"])
        self.assertContains(response, "WSAV_S")
        # TAAV's name/unit only exist in the 20-day-old Format A message;
        # None here proves the out-of-window message was excluded.
        self.assertIsNone(channels["TAAV"]["name"])

    def test_fetch_failure_renders_error_banner_with_editable_formset(self):
        with self._mock_client(error=DCSWebServiceConnectionError("dcs down")):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["channels"], [])
        self.assertIn("dcs down", response.context["channels_error"])
        self.assertIsNotNone(response.context["formset"])

    def test_post_saves_mapping_and_normalizes_channel_code(self):
        parameter = DataParameterFactory()
        unit = UnitFactory()
        data = {
            "variable_mappings-TOTAL_FORMS": "1",
            "variable_mappings-INITIAL_FORMS": "0",
            "variable_mappings-MIN_NUM_FORMS": "0",
            "variable_mappings-MAX_NUM_FORMS": "1000",
            "variable_mappings-0-id": "",
            "variable_mappings-0-adl_parameter": str(parameter.id),
            "variable_mappings-0-channel_id": "  WSAV  ",
            "variable_mappings-0-channel_unit": str(unit.id),
        }

        with self._mock_client(messages=[]):
            response = self.client.post(self.url, data)
            # assertRedirects re-fetches the target page, so it must stay
            # inside the mock -- outside it, that GET would hit the real
            # DCS service.
            self.assertRedirects(response, self.url)
        mapping = self.station_link.variable_mappings.get()
        self.assertEqual(mapping.channel_id, "WSAV")
        self.assertEqual(mapping.adl_parameter, parameter)
        self.assertEqual(mapping.channel_unit, unit)

    def test_post_invalid_rerenders_with_errors(self):
        data = {
            "variable_mappings-TOTAL_FORMS": "1",
            "variable_mappings-INITIAL_FORMS": "0",
            "variable_mappings-MIN_NUM_FORMS": "0",
            "variable_mappings-MAX_NUM_FORMS": "1000",
            "variable_mappings-0-id": "",
            "variable_mappings-0-adl_parameter": str(DataParameterFactory().id),
            "variable_mappings-0-channel_id": "   ",
            "variable_mappings-0-channel_unit": str(UnitFactory().id),
        }

        with self._mock_client(messages=[]):
            response = self.client.post(self.url, data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["formset"].errors[0])
        self.assertEqual(self.station_link.variable_mappings.count(), 0)
