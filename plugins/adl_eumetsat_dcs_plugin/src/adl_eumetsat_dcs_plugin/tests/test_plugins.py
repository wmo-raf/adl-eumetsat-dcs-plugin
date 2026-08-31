from datetime import datetime, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from adl_eumetsat_dcs_plugin.client import DCPMessage
from adl_eumetsat_dcs_plugin.plugins import EumetsatDCSPlugin

from .helpers import (
    BODY_PREAMBLE,
    FORMAT_A_BODY,
    FORMAT_A_BODY_WITH_ERRORCODE,
    FORMAT_B_BODY,
    FORMAT_C_BODY,
    FORMAT_C_BODY_WITH_EXTRAS,
    UNPARSED_BODY,
    build_raw_message,
)


class StubClient:
    def __init__(self, messages):
        self._messages = messages

    def get_latest_observations(self, dcp_id, cache_ttl=300):
        return self._messages


def make_station_link(messages, observation_timezone="Africa/Addis_Ababa"):
    client = StubClient(messages)
    return SimpleNamespace(
        dcp_id="188990C0",
        observation_timezone=observation_timezone,
        network_connection=SimpleNamespace(get_api_client=lambda: client),
    )


def make_message(body, **kwargs):
    (message,) = DCPMessage.iter_from_bulk(build_raw_message(body, **kwargs))
    return message


class GetStationDataTests(SimpleTestCase):

    def setUp(self):
        self.plugin = EumetsatDCSPlugin()

    def get_records(self, messages, start_date=None, end_date=None, **link_kwargs):
        station_link = make_station_link(messages, **link_kwargs)
        return self.plugin.get_station_data(station_link, start_date=start_date,
                                            end_date=end_date)

    def test_format_a_uses_declared_offset(self):
        # Body declares +03:00; channels observed at 15:50 local.
        message = make_message(FORMAT_A_BODY, date=b"08/07/26", time_=b"15:55:00")

        records = self.get_records([message])

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["observation_time"],
                         datetime(2026, 7, 8, 12, 50, tzinfo=timezone.utc))
        # Both channels at the same timestamp collapse into one record.
        self.assertEqual(record["WSAV"], 0.9)
        self.assertEqual(record["WSMN"], 0.4)

    def test_format_b_uses_station_timezone(self):
        # No offset in the body -- Africa/Addis_Ababa (UTC+3) applies.
        message = make_message(FORMAT_B_BODY, date=b"05/08/26", time_=b"12:25:47")

        records = self.get_records([message])

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["observation_time"],
                         datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(record["TAAV"], 23.5)

    def test_format_c_uses_transmission_time(self):
        # Format C bodies carry no observation time -- the header's
        # transmission time (UTC) is used verbatim, regardless of the
        # station link's configured observation timezone.
        message = make_message(FORMAT_C_BODY, dcp_id=b"18CAD718",
                               sequence=394, date=b"31/08/26",
                               time_=b"13:25:17")

        records = self.get_records([message])

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["observation_time"],
                         datetime(2026, 8, 31, 13, 25, 17, tzinfo=timezone.utc))
        self.assertEqual(record["WISI"], 1.32)
        self.assertEqual(record["TMPI"], 19.0)
        self.assertEqual(record["PRCI"], 0.0)
        self.assertEqual(record["00BV"], 13.6)
        # observation_time + the 9 channels, nothing else.
        self.assertEqual(len(record), 10)

    def test_format_c_extra_fields_not_ingested(self):
        # unknown_1/unknown_2 and trailing ":TAG value" extras (":BL")
        # are parser metadata, not observations.
        message = make_message(FORMAT_C_BODY_WITH_EXTRAS,
                               date=b"31/08/26", time_=b"13:25:17")

        records = self.get_records([message])

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(sorted(record), ["TAAV", "WSMN", "observation_time"])

    def test_none_channel_time_skipped_not_crashed(self):
        # A channel time DCPMessage.data couldn't fill must be skipped
        # with a warning, never reach fromisoformat(None) (TypeError).
        message = SimpleNamespace(dcp_id="18CAD718", sequence=1)
        channel = {"channel_id": "WISI", "time": None}
        station_link = make_station_link([])

        result = self.plugin._resolve_channel_time(
            channel, None, station_link, message
        )

        self.assertIsNone(result)

    def test_unparsed_body_encoding_skipped(self):
        message = make_message(UNPARSED_BODY, date=b"05/08/26", time_=b"13:25:47")

        self.assertEqual(self.get_records([message]), [])

    def test_errorcode_channel_skipped(self):
        message = make_message(FORMAT_A_BODY_WITH_ERRORCODE,
                               date=b"08/07/26", time_=b"15:55:00")

        self.assertEqual(self.get_records([message]), [])

    def test_non_numeric_value_skipped(self):
        body = BODY_PREAMBLE + (
            b'<?xml version="1.0"?><StationDataList>'
            b'<StationData stationId="000NEGHELE" name="Neghele" timezone="+03:00">'
            b'<ChannelData channelId="WSAV" name="WSAV_S" unit="m/s">'
            b'<Values><VT t="2026-07-08T15:50:00">no</VT></Values></ChannelData>'
            b'</StationData></StationDataList>'
        )
        message = make_message(body, date=b"08/07/26", time_=b"15:55:00")

        self.assertEqual(self.get_records([message]), [])

    def test_format_b_fallback_datetime_string_skipped(self):
        # Non-YYYYMMDD dateformat -- the parser falls back to a raw
        # "date;time" string, which must be skipped, not guessed at.
        body = BODY_PREAMBLE + (
            b'<STATION>000NEGHELE</STATION><SENSOR>TAAV</SENSOR>'
            b'<DATEFORMAT>DDMMYYYY</DATEFORMAT><FIRMWARE>V8.81</FIRMWARE>\r\n'
            b'05082026;120000;23.5'
        )
        message = make_message(body, date=b"05/08/26", time_=b"12:25:47")

        self.assertEqual(self.get_records([message]), [])

    def test_transmission_time_filter_with_margin(self):
        early = make_message(FORMAT_A_BODY, sequence=1,
                             date=b"01/07/26", time_=b"00:00:00")
        in_range = make_message(FORMAT_B_BODY, sequence=2,
                                date=b"05/08/26", time_=b"12:25:47")
        late = make_message(FORMAT_A_BODY, sequence=3,
                            date=b"06/08/26", time_=b"00:00:00")
        # Transmitted 30 minutes before start_date -- kept by the
        # 1-hour back-margin for transmission lag.
        margin = make_message(
            BODY_PREAMBLE +
            b'<STATION>000NEGHELE</STATION><SENSOR>WSAV</SENSOR>'
            b'<DATEFORMAT>YYYYMMDD</DATEFORMAT><FIRMWARE>V8.81</FIRMWARE>\r\n'
            b'20260805;103000;1.5',
            sequence=4, date=b"05/08/26", time_=b"11:30:00",
        )

        start_date = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        end_date = datetime(2026, 8, 5, 23, 59, tzinfo=timezone.utc)

        records = self.get_records([early, in_range, late, margin],
                                   start_date=start_date, end_date=end_date)

        self.assertEqual(len(records), 2)
        # Sorted by observation_time.
        self.assertEqual([r["observation_time"] for r in records],
                         sorted(r["observation_time"] for r in records))
        self.assertEqual(records[0]["WSAV"], 1.5)
        self.assertEqual(records[1]["TAAV"], 23.5)

    def test_grouping_across_messages(self):
        # Two Format-B messages (one channel each) at the same
        # observation time merge into a single record.
        first = make_message(FORMAT_B_BODY, sequence=1,
                             date=b"05/08/26", time_=b"12:25:47")
        second = make_message(
            BODY_PREAMBLE +
            b'<STATION>000NEGHELE</STATION><SENSOR>WSAV</SENSOR>'
            b'<DATEFORMAT>YYYYMMDD</DATEFORMAT><FIRMWARE>V8.81</FIRMWARE>\r\n'
            b'20260805;120000;0.7',
            sequence=2, date=b"05/08/26", time_=b"12:26:47",
        )

        records = self.get_records([first, second])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["TAAV"], 23.5)
        self.assertEqual(records[0]["WSAV"], 0.7)
