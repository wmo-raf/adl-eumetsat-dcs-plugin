"""
Synthetic DCP message builders matching the empirically-verified bulk
download structure: [88-byte header][33-byte quality record][SIZE-byte
body], header line 2 declaring the body size.
"""

HEADER_LENGTH = 88
QUALITY_RECORD_LENGTH = 33

# All observed bodies open with a ~12-byte binary preamble before
# readable text starts; the parsers search anywhere in the body, so the
# tests include one to mirror real data.
BODY_PREAMBLE = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c"

FORMAT_A_BODY = BODY_PREAMBLE + (
    b'<?xml version="1.0"?><StationDataList>'
    b'<StationData stationId="000NEGHELE" name="Neghele" timezone="+03:00">'
    b'<ChannelData channelId="WSAV" name="WSAV_S" unit="m/s">'
    b'<Values><VT t="2026-07-08T15:50:00">0.9</VT></Values></ChannelData>'
    b'<ChannelData channelId="WSMN" name="WSMN_S" unit="m/s">'
    b'<Values><VT t="2026-07-08T15:50:00">0.4</VT></Values></ChannelData>'
    b'</StationData></StationDataList>'
)

FORMAT_A_BODY_WITH_ERRORCODE = BODY_PREAMBLE + (
    b'<?xml version="1.0"?><StationDataList>'
    b'<StationData stationId="000NEGHELE" name="Neghele" timezone="+03:00">'
    b'<ChannelData channelId="TAAV" name="TAAV_S" unit="degC">'
    b'<Values><VT t="2026-07-08T15:50:00" errorcode="E1">23.5</VT></Values></ChannelData>'
    b'</StationData></StationDataList>'
)

FORMAT_B_BODY = BODY_PREAMBLE + (
    b'<STATION>000NEGHELE</STATION><SENSOR>TAAV</SENSOR>'
    b'<DATEFORMAT>YYYYMMDD</DATEFORMAT><FIRMWARE>V8.81.4062</FIRMWARE>\r\n'
    b'20260805;120000;23.5'
)

COLON_TAG_BODY = BODY_PREAMBLE + b':WSMN 25 #60 0.4 :TAAV 25 #60 23.5'


def build_raw_message(body, dcp_id=b"188990C0", sequence=1,
                      date=b"05/08/26", time_=b"12:25:47", flag=b"G"):
    """
    Builds one raw [header][quality][body] block. Line 1's padding is
    computed so the two header lines total exactly HEADER_LENGTH bytes,
    like the real downloads.
    """
    line2 = b"%s-ALL %d at %s %s UTC %05dBT %s\r\n" % (
        dcp_id, sequence, date, time_, len(body), flag
    )
    line1_content = b"36430082 ET/NEGHELE"
    line1 = line1_content.ljust(HEADER_LENGTH - len(line2) - 2) + b"\r\n"
    header = line1 + line2
    assert len(header) == HEADER_LENGTH

    quality_record = b"\x00" * QUALITY_RECORD_LENGTH
    return header + quality_record + body


def build_bulk(*message_specs):
    """
    Concatenates raw messages back-to-back (no gap or trailer), the way
    a decompressed ACTION_DOWNLOAD response lays them out. Each spec is
    a kwargs dict for build_raw_message.
    """
    return b"".join(build_raw_message(**spec) for spec in message_specs)
