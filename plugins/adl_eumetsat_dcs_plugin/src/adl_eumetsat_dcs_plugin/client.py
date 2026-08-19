import gzip
import logging
import re
import requests
from bs4 import BeautifulSoup
from django.core.cache import cache
from urllib.parse import quote, urlencode, urljoin

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 60


class DCSWebServiceConnectionError(Exception):
    pass


# Lenient regex-based parsers for the THREE distinct body formats
# observed on this station's messages -- they are NOT the same format at
# different verbosity, they're structurally different:
#
#   Format A (seen on earlier/July messages), verbose XML:
#     <StationDataList><StationData stationId="..." name="..." timezone="...">
#       <ChannelData channelId="..." name="..." unit="...">
#         <Values><VT t="...">value</VT></Values>
#       </ChannelData>...
#
#   Format B (seen on later/August messages, e.g. via get_message_detail),
#   compact per-sensor tags followed by a semicolon-delimited data line:
#     <STATION>id</STATION><SENSOR>code</SENSOR><DATEFORMAT>YYYYMMDD</DATEFORMAT>
#     <FIRMWARE>version</FIRMWARE>
#     YYYYMMDD;HHMMSS;value
#
#   Format C (colon-tag compact ASCII, e.g. DCP 18CAD718), one entry per
#   channel reading on a single line:
#     :WISI 25 #60 2.3 :WIDI 25 #60 223.8 :WSMA 25 #60 4.6 ...
#   See the comment above _FORMAT_C_ENTRY_RE for what's confirmed vs.
#   guessed about this format -- it's only been seen in ONE full message
#   so far, unlike A and B.
#
# All three are parsed leniently (not with a strict XML/text parser)
# because real bodies from this platform have been observed truncated
# mid-document -- a strict parser would just raise on those. Each
# format's regex greedily extracts whatever complete entries it can
# find, tolerating a cut-off tail entry.
_FORMAT_A_STATION_RE = re.compile(rb'<StationData stationId="([^"]*)" name="([^"]*)" timezone="([^"]*)"')
_FORMAT_A_CHANNEL_RE = re.compile(
    rb'<ChannelData channelId="([^"]*)" name="([^"]*)" unit="([^"]*)">\s*'
    rb'<Values>\s*<VT t="([^"]*)"(?:\s+errorcode="([^"]*)")?>([^<]*)'
)

_FORMAT_B_RE = re.compile(
    rb'<STATION>([^<]*)</STATION><SENSOR>([^<]*)</SENSOR><DATEFORMAT>([^<]*)</DATEFORMAT>'
    rb'<FIRMWARE>([^<]*)</FIRMWARE>\r?\n([0-9]{8});([0-9]{6});([^\r\n]*)'
)

# Format C: repeating ":CODE NUM1 #NUM2 VALUE" channel entries, e.g.
# ":WISI 25 #60 2.3" or ":WSMN 25 #60 0.4" (the latter is the example
# noted in parse_xml_body's docstring before this format was handled).
#
# Confirmed against exactly ONE real message body (DCP 18CAD718, seq
# 197, 19/08/26 11:25:17) -- treat the structure as a working
# hypothesis, not confirmed universal the way Format A/B's shapes are:
#
#   - CODE: channel code, same shape as the channel IDs in Formats A/B.
#   - NUM1 and the digits after '#' (NUM2): constant (25 and 60) across
#     every entry in the one message seen. Possibly a fixed element/
#     table id and a reporting interval in minutes -- that's a guess,
#     not confirmed, so these are kept as opaque "unknown_1"/"unknown_2"
#     fields in _parse_format_c rather than named/typed. Re-evaluate
#     once messages where these vary turn up.
#   - VALUE: the reading, kept as a string for the same reason values
#     are kept as strings elsewhere in this module (see parse_xml_body).
#
# The one sample seen also has a trailer after the channel entries:
#   :BL 13.03 :UUID 23n,7<raw binary bytes>
# ":BL" and any other trailing ":TAG value" pair that lacks the '#'
# marker is captured generically via _FORMAT_C_EXTRA_RE into an
# "extra_fields" dict -- NOT assumed to mean "battery level" or
# anything else; that would be reading meaning into a tag name from a
# single sample. ":UUID" is followed by what looks like raw binary (a
# CRC/checksum?), not clean ASCII text, so it deliberately doesn't
# match this pattern and is left unparsed rather than guessed at.
#
# No station id, name, or timezone appear anywhere in this body
# encoding (unlike Format A), so _parse_format_c always returns None
# for those. One loose observation, not acted on below: in the single
# sample seen, the message's 12-byte body preamble (see DCPMessage's
# docstring) opens with 4 bytes (\x18\xca\xd7\x18) identical to the
# DCP ID (18CAD718) read as raw hex -- possibly the preamble encodes
# the station id, but that's one data point and isn't relied on here.
_FORMAT_C_ENTRY_RE = re.compile(
    rb':(\S+)\s+(\d+)\s+#(\d+)\s+(-?\d+(?:\.\d+)?)'
)
_FORMAT_C_EXTRA_RE = re.compile(
    rb':(\S+)\s+(-?\d+(?:\.\d+)?)(?=\s|$)'
)


def _parse_format_a(body):
    station_match = _FORMAT_A_STATION_RE.search(body)
    if station_match is None:
        return None
    
    station_id, station_name, timezone = (
        g.decode("ascii", errors="replace") for g in station_match.groups()
    )
    
    channels = []
    for channel_id, name, unit, time_, errorcode, value in _FORMAT_A_CHANNEL_RE.findall(body):
        channels.append({
            "channel_id": channel_id.decode("ascii", errors="replace"),
            "name": name.decode("ascii", errors="replace"),
            "unit": unit.decode("ascii", errors="replace"),
            "time": time_.decode("ascii", errors="replace"),
            "value": value.decode("ascii", errors="replace"),
            "errorcode": errorcode.decode("ascii", errors="replace") if errorcode else None,
        })
    
    return {
        "station_id": station_id,
        "station_name": station_name,
        "timezone": timezone,
        "channels": channels,
    }


def _parse_format_b(body):
    matches = _FORMAT_B_RE.findall(body)
    if not matches:
        return None
    
    station_id = None
    channels = []
    for station, sensor, dateformat, firmware, date_str, time_str, value in matches:
        station_id = station.decode("ascii", errors="replace")
        dateformat = dateformat.decode("ascii", errors="replace")
        date_str = date_str.decode("ascii", errors="replace")
        time_str = time_str.decode("ascii", errors="replace")
        
        # only YYYYMMDD observed so far; fall back to raw strings for
        # anything else rather than guessing a different layout
        if dateformat == "YYYYMMDD" and len(date_str) == 8 and len(time_str) == 6:
            time_iso = (
                f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}T"
                f"{time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]}"
            )
        else:
            time_iso = f"{date_str};{time_str}"
        
        channels.append({
            "channel_id": sensor.decode("ascii", errors="replace"),
            "name": None,  # not present in this compact format
            "unit": None,  # not present in this compact format
            "time": time_iso,
            "value": value.decode("ascii", errors="replace"),
            "errorcode": None,
            "firmware": firmware.decode("ascii", errors="replace"),
        })
    
    return {
        "station_id": station_id,
        "station_name": None,
        "timezone": None,
        "channels": channels,
    }


def _parse_format_c(body):
    entry_matches = list(_FORMAT_C_ENTRY_RE.finditer(body))
    if not entry_matches:
        return None
    
    channels = []
    matched_spans = []
    for m in entry_matches:
        code, num1, num2, value = m.groups()
        matched_spans.append(m.span())
        channels.append({
            "channel_id": code.decode("ascii", errors="replace"),
            "name": None,  # not present in this compact format
            "unit": None,  # not present in this compact format
            "time": None,  # not present in this body -- use the
            # containing DCPMessage's .date/.time instead
            "value": value.decode("ascii", errors="replace"),
            "errorcode": None,
            "unknown_1": num1.decode("ascii", errors="replace"),
            "unknown_2": num2.decode("ascii", errors="replace"),
        })
    
    # Strip out everything already claimed by the channel-entry regex,
    # then look for remaining simple ":TAG value" pairs (e.g. ":BL
    # 13.03") in what's left over. Doing it in this order -- rather than
    # one combined regex -- avoids _FORMAT_C_EXTRA_RE accidentally
    # matching just the first two tokens of a channel entry (":WISI 25"
    # looks exactly like a valid ":TAG value" pair on its own).
    remainder = body
    for start, end in sorted(matched_spans, reverse=True):
        remainder = remainder[:start] + remainder[end:]
    
    extra_fields = {}
    for m in _FORMAT_C_EXTRA_RE.finditer(remainder):
        tag, value = m.groups()
        extra_fields[tag.decode("ascii", errors="replace")] = value.decode("ascii", errors="replace")
    
    return {
        "station_id": None,
        "station_name": None,
        "timezone": None,
        "channels": channels,
        "extra_fields": extra_fields,
    }


def parse_xml_body(body):
    """
    Parses any of the three DCP body formats seen on this station (see
    comments above _FORMAT_A_STATION_RE / _FORMAT_B_RE / _FORMAT_C_ENTRY_RE
    for what each looks like) into one normalized JSON-serializable dict:

        {
          "station_id": "000NEGHELE",
          "station_name": "Neghele" or None (Formats B/C don't carry this),
          "timezone": "+03:00" or None (Formats B/C don't carry this),
          "channels": [
            {"channel_id": "WSAV", "name": "WSAV_S" or None, "unit": "m/s" or None,
             "time": "2026-07-08T15:50:00" or None (Format C has no per-channel
             time -- use the containing DCPMessage's .date/.time), "value": "0.9",
             "errorcode": None,
             "firmware": "V8.81.4062",  # only present for Format B entries
             "unknown_1": "25", "unknown_2": "60"},  # only present for Format C
            ...
          ],
          "extra_fields": {"BL": "13.03"},  # only present for Format C, and
                                             # only when trailing tags beyond
                                             # the channel entries were found
        }

    Tries Format A, then Format B, then Format C. Returns None if none
    match -- callers should treat None as "this body uses some other
    encoding entirely" (e.g. the ":UUID"-prefixed binary trailer seen
    tacked onto Format C messages isn't itself a format this function
    handles), not as an error.

    Values are kept as strings, not cast to float/int, since malformed
    or truncated messages could contain partial/invalid numeric text --
    casting here would trade a clean failure for a silent bad value.
    Cast downstream once you've decided how to handle that per field.
    """
    return _parse_format_a(body) or _parse_format_b(body) or _parse_format_c(body)


class DCPMessage:
    """
    A single decoded DCP message: the fixed-size Service Header, the
    fixed-size Quality record, and the variable-length message body.

    Structure below is EMPIRICALLY VERIFIED against a real 669-message,
    909,971-byte download for DCP 188990C0 (ET/NEGHELE), with zero
    mismatches across every message:

        [88-byte header][33-byte quality record][SIZE-byte body]

    repeated back-to-back with no gap or trailer between one message's
    body and the next message's header. The header itself is plain
    ASCII text (not opaque binary as earlier assumed), two lines:

        <8-digit code> <station name, padded>\\r\\n
        <DCP-ID>-ALL <sequence> at <DD/MM/YY> <HH:MM:SS> UTC
        <5-digit SIZE>BT <flag>\\r\\n   (single line in the data)

    The declared SIZE field (bytes 2-3 of line 2) exactly equals the
    body's byte length in every one of the 669 verified messages.

    NOT yet decoded/confirmed:
      - the leading 8-digit code on line 1 (constant per DCP in the
        sample data -- meaning unknown, not decoded here)
      - internal byte-level fields *within* the 33-byte quality record
        (this is shorter than the 31-byte DCP_QUALITY record EUMETSAT's
        docs describe elsewhere, plus some additional bytes -- possibly
        HRDCP fields bringing it to 33, but not confirmed field-by-field)
      - the body itself has a further 12-byte binary preamble before
        readable text starts, consistent across XML/pseudo-binary/
        colon-tag body encodings seen so far, but not yet decoded

    This was all derived from ONE DCP's real download. Byte lengths
    here (esp. the 33-byte quality record and 12-byte body preamble)
    should be re-verified against other DCP IDs/platforms before being
    treated as universal -- they may be EUMETSAT-format constants that
    hold everywhere, or may vary by platform/firmware.
    """
    
    HEADER_LENGTH = 88
    QUALITY_RECORD_LENGTH = 33
    
    # matches line 2 of the header only, ending exactly at the header's
    # last byte -- this lets header_start be derived as match.end() -
    # HEADER_LENGTH without needing to know line 1's station-name
    # padding, so it generalizes across DCP IDs rather than being tied
    # to one station's name/prefix.
    _HEADER_RE = re.compile(
        rb'(\S+)-ALL\s+(\d+) at (\d{2}/\d{2}/\d{2}) (\d{2}:\d{2}:\d{2}) UTC (\d{5})BT (\S)\r\n'
    )
    
    def __init__(self, header, quality_record, body, dcp_id, sequence, date, time_, flag, declared_size):
        self.header = header
        self.quality_record = quality_record
        self.body = body
        self.dcp_id = dcp_id
        self.sequence = sequence
        self.date = date  # string, DD/MM/YY as transmitted
        self.time = time_  # string, HH:MM:SS as transmitted
        self.flag = flag
        self.declared_size = declared_size
    
    @classmethod
    def iter_from_bulk(cls, data):
        """
        Parses a bulk download (the decompressed contents of an
        ACTION_DOWNLOAD response) into individual DCPMessage objects,
        using the verified [header][quality][body] structure above.

        Yields messages in file order. Raises DCSWebServiceConnectionError
        if a match's computed body would run past the end of the buffer
        (a sign the fixed lengths above don't hold for this data, rather
        than silently returning truncated/wrong messages).
        """
        for m in cls._HEADER_RE.finditer(data):
            dcp_id, seq, date, time_, size_str, flag = m.groups()
            size = int(size_str)
            
            header_start = m.end() - cls.HEADER_LENGTH
            header = data[header_start:m.end()]
            
            quality_start = m.end()
            quality_end = quality_start + cls.QUALITY_RECORD_LENGTH
            quality_record = data[quality_start:quality_end]
            
            body_start = quality_end
            body_end = body_start + size
            
            if body_end > len(data):
                raise DCSWebServiceConnectionError(
                    f"Message at offset {header_start} declares a "
                    f"{size}-byte body that runs past the end of the "
                    "download -- the fixed header/quality lengths may "
                    "not hold for this data"
                )
            
            body = data[body_start:body_end]
            
            yield cls(
                header=header,
                quality_record=quality_record,
                body=body,
                dcp_id=dcp_id.decode("ascii", errors="replace"),
                sequence=int(seq),
                date=date.decode("ascii"),
                time_=time_.decode("ascii"),
                flag=flag.decode("ascii", errors="replace"),
                declared_size=size,
            )
    
    @property
    def body_text(self):
        """
        Best-effort ASCII decode of the message body.

        Note: in every body observed so far (XML, pseudo-binary, and
        colon-tag encodings alike), the body opens with a ~12-byte
        binary preamble before readable text begins -- this property
        decodes the WHOLE body including that preamble, so expect a
        few garbled leading characters. Splitting that preamble out
        cleanly is not yet implemented (see class docstring).
        """
        try:
            return self.body.decode("ascii")
        except UnicodeDecodeError:
            return self.body.decode("ascii", errors="replace")
    
    @property
    def data(self):
        """
        Parsed JSON-serializable form of the body, via parse_xml_body().
        None if this message's body isn't the XML-tagged format (the
        colon-tag and pseudo-binary encodings seen on this same station
        aren't handled by parse_xml_body() -- see its docstring).
        """
        return parse_xml_body(self.body)
    
    def __repr__(self):
        return (
            f"<DCPMessage {self.dcp_id} seq={self.sequence} "
            f"{self.date} {self.time} body_len={len(self.body)}>"
        )


def parse_dcp_admin_page(html):
    """
    Parses the "DCP Administration" page (the ACTION=DCP_ADMIN main-menu
    view) into the operator plus the list of registered DCPs.

    Table parsing mirrors list_available's: data rows interleave spacer
    cells (class containing "separator", holding only &nbsp;) with real
    content cells; divider rows are a single colspan separator cell.
    Content cells are identified by filtering out spacers rather than by
    fixed position. A data row has 8 content cells: checkbox, DCP ID,
    name, downloaded date, num messages, most recent message, and two
    link cells (List/Download -- empty for never-transmitting DCPs).
    Rows are validated by the num-messages cell being numeric, so
    header/summary/button rows fall through harmlessly.
    """
    soup = BeautifulSoup(html, "html.parser")
    
    operator = None
    heading = soup.find("h3", class_="contentMiddleHead")
    if heading and "Operator" in heading.get_text():
        operator = heading.get_text(strip=True).replace("Operator:", "").strip()
    
    target_table = None
    for table in soup.find_all("table"):
        header_text = table.get_text()
        if "DCP ID" in header_text and "Num Messages" in header_text:
            target_table = table
            break
    
    if target_table is None:
        raise DCSWebServiceConnectionError(
            "Could not find the registered-DCP table on the DCP_ADMIN "
            "page -- page structure may differ from what this client "
            "expects (or the session bounced to a login page)"
        )
    
    def is_spacer_cell(td):
        classes = td.get("class") or []
        return any("separator" in c for c in classes)
    
    dcps = []
    for row in target_table.find_all("tr"):
        content_cells = [
            td for td in row.find_all("td") if not is_spacer_cell(td)
        ]
        
        if len(content_cells) != 8:
            continue  # header row, divider row, or unrelated row
        
        num_messages_text = content_cells[4].get_text(strip=True)
        if not num_messages_text.isdigit():
            continue
        
        dcps.append({
            "dcp_id": content_cells[1].get_text(strip=True),
            "name": content_cells[2].get_text(strip=True),
            "downloaded_date": content_cells[3].get_text(strip=True) or None,
            "num_messages": int(num_messages_text),
            "most_recent_message": content_cells[5].get_text(strip=True) or None,
        })
    
    return {"operator": operator, "dcps": dcps}


class DCSWebServiceClient:
    """
    Client for the EUMETSAT Meteosat Data Collection Service (DCS) Web Service.

    Docs: https://user.eumetsat.int/resources/user-guides/dcs-web-service-user-guide

    Note the base URL uses "dcswebservice" (with an s), which is distinct
    from the "dcpwebservice" public viewer path seen in some browser
    sessions -- confirm which one your account credentials are valid on.
    """
    
    def __init__(self, baseurl, user, password):
        self.baseurl = baseurl.rstrip("/")
        self.user = user
        self.password = password
        self._cached_session = None
    
    def get(self, action, params=None):
        if params is None:
            params = {}
        
        params = {
            "action": action,
            "user": self.user,
            "pass": self.password,
            **params,
        }
        
        url = f"{self.baseurl}/dcpAdmin.do"
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        
        if response.status_code != 200:
            raise DCSWebServiceConnectionError(
                f"DCS Web Service returned {response.status_code} "
                f"for action={action}"
            )
        
        return response
    
    def build_download_url(self, dcp_id, date=None):
        """
        Builds the stateless ACTION_DOWNLOAD URL for a DCP ID:

            dcpAdmin.do?action=ACTION_DOWNLOAD&id=<id>&user=<user>&pass=<pass>

        CONFIRMED (tested against a real request): passing `dcp_date`
        does NOT scope the download to a single message. A request with
        dcp_date=08/07/2026 13:25:47 returned the full 669-message,
        909,971-byte backlog spanning 08/07/2026 through 05/08/2026,
        identical in shape to an undated download -- the parameter is
        silently ignored by this endpoint. `date` is kept as an argument
        here (and still sent, in case behavior differs by account/DCP)
        but should NOT be relied on to reduce response size.

        There is currently no known way to download a single message
        directly. To get one message: call get_messages() for the full
        backlog and filter the returned DCPMessage list by .date/.time,
        or use ACTION_LIST's "Show" link (dcp.do, see list_available's
        show_url) to view one message's HTML rendering -- not a raw
        download, but usable for a specific single-message lookup.
        """
        params = {
            "action": "ACTION_DOWNLOAD",
            "id": dcp_id,
            "user": self.user,
            "pass": self.password,
        }
        if date:
            params["dcp_date"] = date
        
        return f"{self.baseurl}/dcpAdmin.do?{urlencode(params, quote_via=quote)}"
    
    def download_raw(self, dcp_id):
        """
        Calls the stateless automatic download endpoint (ACTION_DOWNLOAD)
        and returns the raw payload bytes for the given DCP ID (gzip, or
        already-decompressed bulk data).

        The payload is sniffed rather than trusting Content-Type: the
        server labels even successful gzip downloads as
        "text/html; charset=UTF-8", so the header cannot distinguish
        data from an HTML login/error page.
        """
        response = self.get("ACTION_DOWNLOAD", {"id": dcp_id})
        content = response.content
        
        if content[:2] == b"\x1f\x8b":
            # gzip magic bytes -- the documented payload
            return content
        
        # An HTML login/error page opens with a tag; non-gzipped bulk
        # data opens with the ASCII message header (8-digit code +
        # station name), never '<'.
        if content.lstrip()[:1] == b"<":
            # almost always a credentials or DCP ID problem rather than
            # a network failure
            raise DCSWebServiceConnectionError(
                f"Expected gzip data for DCP {dcp_id}, got an HTML response "
                "back (check credentials and DCP ID)"
            )
        
        return content
    
    def get_messages(self, dcp_id):
        """
        Downloads and splits the response for a DCP ID into individual
        DCPMessage objects, using the verified [header][quality][body]
        structure (see DCPMessage class docstring).

        Handles both possibilities for the response body:
          - true gzip bytes (per EUMETSAT's documented behavior)
          - already-decompressed plain bytes (observed in a real saved
            response -- likely `requests` or a browser transparently
            decompressed a Content-Encoding: gzip response before we
            ever saw it, or the endpoint doesn't actually gzip in
            practice for this call)
        by trying gzip.decompress() first and falling back to the raw
        bytes if that fails, rather than assuming one or the other.
        """
        raw = self.download_raw(dcp_id)
        
        try:
            data = gzip.decompress(raw)
        except OSError:
            # not actually gzip -- assume it's already plain/decompressed
            data = raw
        
        messages = list(DCPMessage.iter_from_bulk(data))
        
        if not messages:
            raise DCSWebServiceConnectionError(
                f"No messages could be parsed from the response for DCP "
                f"{dcp_id} ({len(data)} bytes) -- header pattern may not "
                "match this data"
            )
        
        return messages
    
    def _login_session(self):
        """
        Logs in to the DCS Web Service and returns a NEW authenticated
        requests.Session carrying the resulting session cookies
        (JSESSIONID / SRVNAME).

        Confirmed against a real login via browser DevTools:

            POST https://service.eumetsat.int/dcswebservice/logon.do
            form fields: username, password, submit=Submit

        On success the response sets a JSESSIONID cookie (HttpOnly,
        Secure, scoped to /dcswebservice) plus an SRVNAME cookie for
        server affinity; requests.Session carries both automatically
        on subsequent calls against the same base path.

        Prefer _get_session() over calling this directly -- this always
        performs a fresh login, whereas _get_session() reuses one across
        calls. Logging in repeatedly in quick succession (e.g. once per
        list_available()/get_message_detail() call) is wasteful and may
        trigger different server behavior (rate limiting, session
        invalidation) than a single session reused normally would.
        """
        session = requests.Session()
        login_url = f"{self.baseurl}/logon.do"
        
        payload = {
            "username": self.user,
            "password": self.password,
            "submit": "Submit",
        }
        
        response = session.post(login_url, data=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise DCSWebServiceConnectionError(
                f"Login POST failed ({response.status_code})"
            )
        
        if "JSESSIONID" not in session.cookies.get_dict():
            raise DCSWebServiceConnectionError(
                "Login did not return a JSESSIONID cookie -- check credentials"
            )
        
        # Heuristic success check: if the response page still contains a
        # password field, treat that as a failed login rather than
        # assuming the POST worked (a wrong password may still return
        # 200 with the login form re-rendered).
        soup = BeautifulSoup(response.text, "html.parser")
        if soup.find("input", {"type": "password"}):
            raise DCSWebServiceConnectionError(
                "Login appears to have failed (a password field is still "
                "present on the response page) -- check credentials"
            )
        
        return session
    
    def _get_session(self, force_relogin=False):
        """
        Returns a cached, logged-in session, reused across calls instead
        of logging in fresh every time. Pass force_relogin=True to
        discard the cached session and log in again (e.g. as a retry
        after a request that looks like it hit a stale/expired session).
        """
        if self._cached_session is None or force_relogin:
            self._cached_session = self._login_session()
        return self._cached_session
    
    def list_available(self, dcp_id):
        """
        Returns metadata plus the list of available messages for a DCP
        ID, matching the interactive "View the DCP Information" page
        (ACTION_LIST), e.g.:

            {
              "operator": "EMI",
              "dcp_id": "188990C0",
              "name": "ET/NEGHELE",
              "downloaded_date": "05/08/2026 13:11:10",
              "messages": [
                {"date": "05/08/2026 12:25:47", "size": 1262,
                 "show_url": "https://.../dcp.do?action=ACTION_LIST
                              &id=188990C0&dcp_date=05/08/2026 12:25:47",
                 "download_url": "https://.../dcpAdmin.do?action=ACTION_DOWNLOAD
                                  &id=188990C0&user=...&pass=...
                                  &dcp_date=05%2F08%2F2026+12%3A25%3A47"},
                ...
              ]
            }

        Requires a session login (see _login_session), since ACTION_LIST
        on dcpAdmin.do doesn't accept user/pass as stateless query params
        the way ACTION_DOWNLOAD does.

        Table parsing below matches real page markup (confirmed against
        a live response): each data row interleaves spacer cells
        (class="separatorV"/"separatorVRight", holding only &nbsp;) with
        three real content cells (plain <td align="center" nowrap>, no
        class) for date, size, and a "Show" link. Divider rows between
        entries are a single <td colspan="7" class="separator">. Content
        cells are identified by filtering out anything classed as a
        spacer, rather than by fixed position, since spacer count is a
        layout detail that could shift.

        The "Show" link itself points at dcp.do (not dcpAdmin.do) with
        action=ACTION_LIST&id=...&dcp_date=..., i.e. it's how you view
        one specific message -- captured here as show_url so callers can
        follow it directly without reconstructing the dcp_date format.

        Each entry also carries a download_url built by build_download_url()
        -- see that method's docstring for why this is an unverified
        hypothesis (dcp_date scoping ACTION_DOWNLOAD to one message) rather
        than confirmed documented behavior. Test it before relying on it.

        The metadata fields (operator, dcp_id, name, downloaded_date)
        come from a small summary table above the message list. If that
        table isn't present in the response (structure may vary), these
        fields come back as None rather than raising -- the message list
        is the important part and shouldn't fail just because a summary
        block is missing.
        """
        session = self._get_session()
        
        url = f"{self.baseurl}/dcpAdmin.do"
        params = {"action": "ACTION_LIST", "id": dcp_id}
        
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise DCSWebServiceConnectionError(
                f"ACTION_LIST returned {response.status_code} for DCP {dcp_id}"
            )
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        operator = None
        heading = soup.find("h3", class_="contentMiddleHead")
        if heading and "Operator" in heading.get_text():
            operator = heading.get_text(strip=True).replace("Operator:", "").strip()
        
        summary = {}
        for table in soup.find_all("table"):
            if "DCP ID:" not in table.get_text():
                continue
            for th in table.find_all("th", class_="headline"):
                label = th.get_text(strip=True).rstrip(":")
                td = th.find_next_sibling("td")
                if td:
                    summary[label] = td.get_text(strip=True)
            break
        
        target_table = None
        for table in soup.find_all("table"):
            header_text = table.get_text()
            if "Date" in header_text and "Size of Data" in header_text:
                target_table = table
                break
        
        if target_table is None:
            raise DCSWebServiceConnectionError(
                f"Could not find the message list table for DCP {dcp_id} "
                "-- page structure may differ from what this client expects"
            )
        
        def is_spacer_cell(td):
            classes = td.get("class") or []
            return any("separator" in c for c in classes)
        
        entries = []
        for row in target_table.find_all("tr"):
            content_cells = [
                td for td in row.find_all("td") if not is_spacer_cell(td)
            ]
            
            if len(content_cells) != 3:
                continue  # header row, divider row, or unrelated row
            
            date_text = content_cells[0].get_text(strip=True)
            size_text = content_cells[1].get_text(strip=True)
            
            if not size_text.isdigit():
                continue
            
            link_tag = content_cells[2].find("a")
            show_url = (
                requests.utils.requote_uri(urljoin(response.url, link_tag["href"]))
                if link_tag and link_tag.get("href")
                else None
            )
            download_url = self.build_download_url(dcp_id, date=date_text)
            
            entries.append({
                "date": date_text,
                "size": int(size_text),
                "show_url": show_url,
                "download_url": download_url,
            })
        
        return {
            "operator": operator,
            "dcp_id": summary.get("DCP ID"),
            "name": summary.get("Name"),
            "downloaded_date": summary.get("Downloaded Date"),
            "messages": entries,
        }
    
    def get_message_detail(self, dcp_id, date):
        """
        Fetches and parses a single message via the "Show" page (the
        interactive single-message view, dcp.do?action=ACTION_LIST),
        as an alternative to pulling the full backlog via download_raw()
        just to get one message.

        Returns a dict, e.g.:

            {
              "fields": {"DCP ID": "188990C0", "Date": "05/08/2026 12:25:47",
                         "Size of Data": "1262", "SubSystemId": "1",
                         "ChannelFreq": "402383500", "CarrierLevel": "-46.45",
                         "HRDCP PlatformLength": "1242", ... },
              "data": {"station_id": "000NEGHELE", "station_name": "Neghele",
                        "timezone": "+03:00",
                        "channels": [{"channel_id": "WSMN", "name": "...",
                                      "unit": "m/s", "time": "2026-08-05T15:00:00",
                                      "value": "1.5", "errorcode": None}, ...]},
              "raw_body": b"<?xml version...",   # exact bytes, decoded from HEX table
            }

        "data" is the parsed JSON-serializable form (see parse_xml_body())
        -- only implemented for the XML-tagged body encoding this station
        uses. If a message uses one of the other encodings seen on this
        same DCP (colon-tag compact ASCII, or a pseudo-binary format),
        "data" comes back None rather than garbage; "raw_body" is always
        populated regardless of encoding, so nothing is lost, it's just
        not yet structured for those formats.

        IMPORTANT, verified by cross-checking a real message against the
        bulk download for the same date/DCP: this page's body is NOT
        byte-identical to what you'd slice out of a bulk ACTION_DOWNLOAD
        response for the same message. This page's body length matches
        the "HRDCP PlatformLength" field, while the bulk download's body
        length matches "Size of Data" -- the latter is ~20 bytes longer,
        that extra tail being a binary CRC/checksum block this page
        excludes from its display. If you need the exact bytes as they
        appear in a bulk download (e.g. to keep a decoder consistent
        across both code paths), don't mix sources for the same message.

        Uses the shared cached session (_get_session()), not a fresh
        login every call. Before requesting the single-message page,
        this first visits the DCP's list page (dcpAdmin.do?action=
        ACTION_LIST&id=...) with the same session -- CONFIRMED necessary
        empirically: hitting dcp.do?dcp_date=... directly, even with a
        genuinely valid logged-in session, returned a page with working
        nav/login state but a completely empty content area (no DCP ID,
        no HEX/ASCII data, just an empty template placeholder). This
        looks like a stateful multi-step JSP flow where the server needs
        the list page hit first to establish which DCP is "selected"
        before dcp_date can resolve to a specific message.

        If the response looks like a login page (a password field
        present) -- meaning the cached session had gone stale -- this
        retries ONCE with a forced fresh login (re-running both the list
        visit and the detail request) before giving up. If the page
        loads fine but simply has no HEX table after that, it does NOT
        retry further, since that isn't a session problem -- double
        check the exact date string against what list_available()
        returned for that DCP.

        The HEX table is used to reconstruct the body (not the ASCII
        table) since hex is unambiguous -- 2 hex digits always decode
        to exactly 1 byte, whereas the ASCII table's rendering of
        control/non-printable bytes wasn't verified byte-for-byte here.
        """
        list_url = f"{self.baseurl}/dcpAdmin.do"
        list_params = {"action": "ACTION_LIST", "id": dcp_id}
        
        detail_url = f"{self.baseurl}/dcp.do"
        detail_params = {"action": "ACTION_LIST", "id": dcp_id, "dcp_date": date}
        
        def is_spacer(tag):
            classes = tag.get("class") or []
            return any("separator" in c for c in classes)
        
        def next_real_td(th):
            sib = th.find_next_sibling("td")
            while sib is not None and is_spacer(sib):
                sib = sib.find_next_sibling("td")
            return sib
        
        last_response = None
        
        for attempt in range(2):
            session = self._get_session(force_relogin=(attempt == 1))
            
            # Visit the DCP's list page first with the same session, to
            # establish whatever server-side "currently selected DCP"
            # state this app tracks -- observed empirically: hitting
            # dcp.do?dcp_date=... directly (skipping this step) returned
            # a page with a valid logged-in nav but a completely empty
            # content area, no DCP ID/HEX/ASCII data at all, even though
            # login itself had clearly succeeded. Matches a stateful
            # multi-step JSP flow rather than a login/session problem.
            list_response = session.get(list_url, params=list_params, timeout=REQUEST_TIMEOUT)
            if list_response.status_code != 200:
                last_response = list_response
                continue
            
            response = session.get(detail_url, params=detail_params, timeout=REQUEST_TIMEOUT)
            last_response = response
            
            if response.status_code != 200:
                continue  # try again with a fresh login before giving up
            
            soup = BeautifulSoup(response.text, "html.parser")
            
            if soup.find("input", {"type": "password"}):
                # bounced back to a login form -- cached session was
                # stale; force a fresh login and retry once
                continue
            
            fields = {}
            hex_pre = None
            for th in soup.find_all("th"):
                label = th.get_text(strip=True)
                if not label:
                    continue
                td = next_real_td(th)
                if td is None:
                    continue
                if label == "HEX:":
                    hex_pre = td.find("pre")
                elif label == "ASCII:":
                    continue  # redundant with HEX, skipped -- see docstring
                else:
                    fields[label] = td.get_text(strip=True)
            
            if hex_pre is not None:
                hex_text = hex_pre.get_text()
                hex_bytes_str = re.sub(r'[0-9a-f]{4}:', '', hex_text)
                hex_tokens = re.findall(r'[0-9a-f]{2}', hex_bytes_str)
                body = bytes.fromhex("".join(hex_tokens))
                return {
                    "fields": fields,
                    "data": parse_xml_body(body),
                    "raw_body": body,
                }
            
            # page loaded, wasn't a login page, but has no HEX table --
            # not a session problem, so don't burn the retry on it
            break
        
        status = last_response.status_code if last_response is not None else "no response"
        snippet = last_response.text[:300].strip() if last_response is not None else ""
        raise DCSWebServiceConnectionError(
            f"Could not find message data for DCP {dcp_id} at {date} "
            f"(status={status}). Likely causes: no message exists for "
            "that exact date/time string (double-check it against what "
            "list_available() returned -- formatting must match exactly), "
            "or the page structure differs from what this client expects. "
            f"Response snippet: {snippet!r}"
        )
    
    @property
    def registered_dcps_cache_key(self):
        # The registered-DCP roster is per account (unlike per-DCP message
        # caches), so the key namespaces by user + base URL.
        return f"dcs_web_service_registered_dcps_{self.user}_{self.baseurl}"
    
    def list_registered_dcps(self, cache_ttl=86400):
        """
        Returns the DCPs registered to this account, from the "DCP
        Administration" page (mainMenuAction.do?action=DCP_ADMIN), e.g.:

            {
              "operator": "EMI",
              "dcps": [
                {"dcp_id": "188990C0", "name": "ET/NEGHELE",
                 "downloaded_date": "05/08/2026 14:36:35",
                 "num_messages": 670,
                 "most_recent_message": "05/08/2026 15:25:47"},
                ...
              ]
            }

        DCPs that have never transmitted (or aren't retained) appear with
        num_messages == 0 and empty date fields.

        The roster changes rarely (registration is an occasional admin
        act), so the result is cached for a long TTL by default; callers
        that need a fresh scrape can clear_registered_dcps_cache() first.

        Requires a session login like list_available -- the page is
        login-scoped (it shows the operator and a logoff link).
        """
        cached = cache.get(self.registered_dcps_cache_key)
        if cached is not None:
            return cached
        
        session = self._get_session()
        
        url = f"{self.baseurl}/mainMenuAction.do"
        params = {"action": "DCP_ADMIN"}
        
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            raise DCSWebServiceConnectionError(
                f"DCP_ADMIN returned {response.status_code}"
            )
        
        result = parse_dcp_admin_page(response.text)
        cache.set(self.registered_dcps_cache_key, result, cache_ttl)
        return result
    
    def clear_registered_dcps_cache(self):
        cache.delete(self.registered_dcps_cache_key)
    
    def get_latest_observations(self, dcp_id, cache_ttl=300):
        """
        Convenience wrapper: fetches messages for a DCP and caches the
        result briefly, to avoid hammering the endpoint if called
        repeatedly within the same polling cycle. This matters more here
        than in most clients: ACTION_DOWNLOAD ignores dcp_date (see
        build_download_url) and always returns the full backlog, so
        every cache miss is a full ~1 MB pull.

        The key namespaces by DCP ID only. Two connections with
        different credentials pointing at the same DCP would share a
        cache entry -- a non-scenario in practice, noted rather than
        engineered for.
        """
        cache_key = f"dcs_web_service_{dcp_id}"
        cached = cache.get(cache_key)
        
        if cached is not None:
            return cached
        
        messages = self.get_messages(dcp_id)
        cache.set(cache_key, messages, cache_ttl)
        return messages
