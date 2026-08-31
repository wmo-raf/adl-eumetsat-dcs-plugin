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

# Real Format C body captured from DCP 18CAD718 (ET/METEHARA), seq 394,
# 31/08/26 13:25:17 UTC -- including its real 12-byte preamble and the
# trailing binary CRC/checksum block, byte-for-byte.
FORMAT_C_BODY = (
    b'\x18\xca\xd7\x18\x00\xa3\x00y\x00\x00\x00\x00'
    b':WISI 25 #60 1.32 :WIDI 25 #60 178.3 :WSMA 25 #60 2.17 '
    b':WDMA 25 #60 125.7 :TMPI 25 #60 19.0 :RHUI 25 #60 67.7 '
    b':PREI 25 #60 772.9 :PRCI 25 #60 0.0 :00BV 25 #60 13.6'
    b'<\xde*\xcf \xbbS\xc6'
)

# Format C with the ":TAG value" trailer seen on the seq-197 sample
# (":BL 13.03") plus a ":UUID"-style binary tail, which must NOT be
# captured as an extra field (its value isn't clean numeric ASCII).
FORMAT_C_BODY_WITH_EXTRAS = BODY_PREAMBLE + (
    b':WSMN 25 #60 0.4 :TAAV 25 #60 23.5 :BL 13.03 :UUID 23n,7\x8f\x02'
)

# A body matching none of the three known formats (pseudo-binary).
UNPARSED_BODY = BODY_PREAMBLE + b'\xf0\xf1\xf2\xf3\xf4\xf5\xf6\xf7'


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
