from django.test import SimpleTestCase

from adl_eumetsat_dcs_plugin.client import (
    DCPMessage,
    DCSWebServiceConnectionError,
    parse_xml_body,
)

from .helpers import (
    COLON_TAG_BODY,
    FORMAT_A_BODY,
    FORMAT_A_BODY_WITH_ERRORCODE,
    FORMAT_B_BODY,
    build_bulk,
    build_raw_message,
)


class IterFromBulkTests(SimpleTestCase):

    def test_splits_back_to_back_messages(self):
        data = build_bulk(
            {"body": FORMAT_A_BODY, "sequence": 1, "date": b"08/07/26", "time_": b"15:55:00"},
            {"body": FORMAT_B_BODY, "sequence": 2, "date": b"05/08/26", "time_": b"12:25:47"},
            {"body": COLON_TAG_BODY, "sequence": 3, "date": b"05/08/26", "time_": b"13:25:47"},
        )

        messages = list(DCPMessage.iter_from_bulk(data))

        self.assertEqual(len(messages), 3)
        self.assertEqual([m.sequence for m in messages], [1, 2, 3])
        self.assertEqual(messages[0].dcp_id, "188990C0")
        self.assertEqual(messages[0].date, "08/07/26")
        self.assertEqual(messages[0].time, "15:55:00")
        self.assertEqual(messages[0].body, FORMAT_A_BODY)
        self.assertEqual(messages[1].body, FORMAT_B_BODY)
        self.assertEqual(messages[2].body, COLON_TAG_BODY)
        for message in messages:
            self.assertEqual(message.declared_size, len(message.body))

    def test_declared_size_past_end_of_buffer_raises(self):
        data = build_raw_message(FORMAT_A_BODY)
        truncated = data[:-10]

        with self.assertRaises(DCSWebServiceConnectionError):
            list(DCPMessage.iter_from_bulk(truncated))


class ParseXmlBodyTests(SimpleTestCase):

    def test_format_a(self):
        parsed = parse_xml_body(FORMAT_A_BODY)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["station_id"], "000NEGHELE")
        self.assertEqual(parsed["station_name"], "Neghele")
        self.assertEqual(parsed["timezone"], "+03:00")
        self.assertEqual(len(parsed["channels"]), 2)

        wsav = parsed["channels"][0]
        self.assertEqual(wsav["channel_id"], "WSAV")
        self.assertEqual(wsav["unit"], "m/s")
        self.assertEqual(wsav["time"], "2026-07-08T15:50:00")
        self.assertEqual(wsav["value"], "0.9")
        self.assertIsNone(wsav["errorcode"])

    def test_format_a_errorcode(self):
        parsed = parse_xml_body(FORMAT_A_BODY_WITH_ERRORCODE)

        self.assertEqual(len(parsed["channels"]), 1)
        self.assertEqual(parsed["channels"][0]["errorcode"], "E1")

    def test_format_a_truncated_keeps_complete_entries(self):
        # Cut inside the second ChannelData entry -- the lenient parser
        # should still return the first, complete entry.
        cut = FORMAT_A_BODY.find(b'<ChannelData channelId="WSMN"') + 20
        parsed = parse_xml_body(FORMAT_A_BODY[:cut])

        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["channels"]), 1)
        self.assertEqual(parsed["channels"][0]["channel_id"], "WSAV")

    def test_format_b(self):
        parsed = parse_xml_body(FORMAT_B_BODY)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["station_id"], "000NEGHELE")
        self.assertIsNone(parsed["station_name"])
        self.assertIsNone(parsed["timezone"])
        self.assertEqual(len(parsed["channels"]), 1)

        taav = parsed["channels"][0]
        self.assertEqual(taav["channel_id"], "TAAV")
        self.assertEqual(taav["time"], "2026-08-05T12:00:00")
        self.assertEqual(taav["value"], "23.5")
        self.assertEqual(taav["firmware"], "V8.81.4062")

    def test_colon_tag_returns_none(self):
        self.assertIsNone(parse_xml_body(COLON_TAG_BODY))

    def test_message_data_property(self):
        data = build_raw_message(COLON_TAG_BODY)
        (message,) = DCPMessage.iter_from_bulk(data)
        self.assertIsNone(message.data)

        data = build_raw_message(FORMAT_A_BODY)
        (message,) = DCPMessage.iter_from_bulk(data)
        self.assertEqual(message.data["timezone"], "+03:00")
