"""
A stand-in for the EUMETSAT DCS Web Service, so this plugin's guide can be
captured against a healthy instance without a live operator account.

It answers the four things the client actually calls:

  POST /dcswebservice/logon.do                       -> sets a session cookie
  GET  /dcswebservice/dcpAdmin.do?action=DCP_ADMIN   -> the registered-DCP roster
  GET  /dcswebservice/dcpAdmin.do?action=ACTION_LIST -> one DCP's message list
  GET  /dcswebservice/dcpAdmin.do?action=ACTION_DOWNLOAD -> gzipped bulk messages

Two things a canned response file could not do, and the reason this is a
server (see adl/docs/screenshots/capture/README.md, "Mocking the source"):

* **Messages are synthesised at request time**, so the newest one is always
  "now" and the diagnostic's freshness layer is honest rather than showing a
  demo that went stale the day it was recorded.
* **The credential is checked**, so the guide's failure diagnostics -- a wrong
  password, an unregistered DCP id -- can be captured deliberately instead of
  described from memory.

The wire format is not invented. The framing (an 88-byte two-line header, a
33-byte quality record, then a body of the declared size) and the XML body
shape are taken from the plugin's own tests/helpers.py, which were built from
recorded downloads. Station names, ids and coordinates here are demo values.
"""

import gzip
import http.server
import math
import os
import random
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

USER = os.environ.get("DCS_USER", "demo")
PASSWORD = os.environ.get("DCS_PASSWORD", "demo")
OPERATOR = os.environ.get("DCS_OPERATOR", "EMI")

HEADER_LENGTH = 88
QUALITY_RECORD_LENGTH = 33
BODY_PREAMBLE = bytes(range(1, 13))

# Registered DCPs. The first two transmit; the third never has, which is the
# state the guide describes for a platform that is registered but silent.
DCPS = [
    {"id": "188990C0", "name": "MZ/BEIRA", "station": "000BEIRA", "transmits": True},
    {"id": "1889A1E2", "name": "MZ/QUELIMANE", "station": "000QUELIM", "transmits": True},
    {"id": "1889B4F6", "name": "MZ/PEMBA", "station": "000PEMBA", "transmits": False},
]

CHANNELS = [
    ("TAAV", "TAAV_S", "degC"),
    ("RHUM", "RHUM_S", "%"),
    ("PRES", "PRES_S", "hPa"),
    ("WSAV", "WSAV_S", "m/s"),
    ("WDAV", "WDAV_S", "deg"),
]


def readings(station, moment):
    """A smooth diurnal cycle, so charts look like weather."""
    rng = random.Random(f"{station}-{moment:%Y%m%d%H%M}")
    hour = moment.hour + moment.minute / 60
    diurnal = math.sin((hour - 9) / 24 * 2 * math.pi)
    return {
        "TAAV": 24 + 5 * diurnal + rng.uniform(-0.3, 0.3),
        "RHUM": 74 - 18 * diurnal + rng.uniform(-2, 2),
        "PRES": 1011 + 1.5 * math.sin(hour / 12 * math.pi) + rng.uniform(-0.2, 0.2),
        "WSAV": max(0.0, 2.6 + 1.8 * diurnal + rng.uniform(-0.6, 0.6)),
        "WDAV": (135 + 30 * diurnal + rng.uniform(-12, 12)) % 360,
    }


def xml_body(station, moment):
    values = readings(station, moment)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%S")
    parts = [
        b'<?xml version="1.0"?><StationDataList>',
        f'<StationData stationId="{station}" name="{station}" timezone="+00:00">'.encode(),
    ]
    for channel, name, unit in CHANNELS:
        parts.append(
            f'<ChannelData channelId="{channel}" name="{name}" unit="{unit}">'
            f'<Values><VT t="{stamp}">{values[channel]:.1f}</VT></Values>'
            f'</ChannelData>'.encode()
        )
    parts.append(b"</StationData></StationDataList>")
    return BODY_PREAMBLE + b"".join(parts)


def raw_message(dcp_id, station, moment, sequence):
    """One [header][quality][body] block, framed as the real download is."""
    body = xml_body(station, moment)
    line2 = b"%s-ALL %d at %s %s UTC %05dBT %s\r\n" % (
        dcp_id.encode(), sequence,
        moment.strftime("%d/%m/%y").encode(),
        moment.strftime("%H:%M:%S").encode(),
        len(body), b"G",
    )
    line1 = b"36430082 " + station.encode()
    line1 = line1.ljust(HEADER_LENGTH - len(line2) - 2) + b"\r\n"
    header = line1 + line2
    assert len(header) == HEADER_LENGTH, len(header)
    return header + b"\x00" * QUALITY_RECORD_LENGTH + body


def message_moments(count=24, step_minutes=60):
    """Newest is always the current hour, so the demo never goes stale."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [now - timedelta(minutes=step_minutes * i) for i in range(count - 1, -1, -1)]


def bulk_for(dcp):
    moments = message_moments()
    return b"".join(
        raw_message(dcp["id"], dcp["station"], moment, i + 1)
        for i, moment in enumerate(moments)
    )


PAGE = """<html><head><title>DCS Web Service</title></head><body>
<h3 class="contentMiddleHead">Operator: {operator}</h3>
{content}
</body></html>"""

SPACER = '<td class="separator">&nbsp;</td>'


def admin_page():
    """
    The DCP Administration table, in the shape parse_dcp_admin_page expects:
    data rows carry 8 content cells interleaved with separator spacers, and
    are recognised by the num-messages cell being numeric.
    """
    rows = [
        "<tr>" + SPACER.join([
            "<td></td>", "<td>DCP ID</td>", "<td>Name</td>",
            "<td>Downloaded</td>", "<td>Num Messages</td>",
            "<td>Most Recent Message</td>", "<td></td>", "<td></td>",
        ]) + "</tr>"
    ]
    now = datetime.now(timezone.utc)
    for dcp in DCPS:
        if dcp["transmits"]:
            downloaded = now.strftime("%d/%m/%Y %H:%M:%S")
            recent = now.replace(minute=0, second=0).strftime("%d/%m/%Y %H:%M:%S")
            num, links = "24", "<td>List</td>"
        else:
            downloaded, recent, num, links = "", "", "0", "<td></td>"
        rows.append("<tr>" + SPACER.join([
            '<td><input type="checkbox"></td>',
            f'<td>{dcp["id"]}</td>', f'<td>{dcp["name"]}</td>',
            f"<td>{downloaded}</td>", f"<td>{num}</td>", f"<td>{recent}</td>",
            links, "<td></td>",
        ]) + "</tr>")
    table = "<table>" + "".join(rows) + "</table>"
    return PAGE.format(operator=OPERATOR, content=table)


def list_page(dcp_id):
    """
    One DCP's message list (ACTION_LIST on dcpAdmin.do).

    Two tables, in the shape list_available parses:
      * a summary table whose text contains "DCP ID:", with
        <th class="headline">Label:</th><td>value</td> pairs;
      * the message table, recognised by containing both "Date" and
        "Size of Data", whose data rows carry exactly 3 content cells
        (date, numeric size, a link) between separator spacers.

    The date text is what the client sends back as dcp_date for a detail
    view, so the two must agree exactly.
    """
    dcp = {d["id"]: d for d in DCPS}[dcp_id]
    now = datetime.now(timezone.utc)
    summary = (
        '<table>'
        '<tr><th class="headline">DCP ID:</th>'
        f'<td>{dcp_id}</td></tr>'
        '<tr><th class="headline">Name:</th>'
        f'<td>{dcp["name"]}</td></tr>'
        '<tr><th class="headline">Downloaded Date:</th>'
        f'<td>{now.strftime("%d/%m/%Y %H:%M:%S")}</td></tr>'
        '</table>'
    )

    rows = ["<tr>" + SPACER.join([
        "<td>Date</td>", "<td>Size of Data</td>", "<td></td>",
    ]) + "</tr>"]
    for moment in reversed(message_moments()):
        stamp = moment.strftime("%d/%m/%Y %H:%M:%S")
        quoted = urllib.parse.quote(stamp)
        rows.append("<tr>" + SPACER.join([
            f"<td>{stamp}</td>",
            f"<td>{len(xml_body(dcp['station'], moment))}</td>",
            f'<td><a href="dcp.do?action=ACTION_LIST&id={dcp_id}'
            f'&dcp_date={quoted}">Show</a></td>',
        ]) + "</tr>")

    return PAGE.format(
        operator=OPERATOR,
        content=summary + '<table>' + "".join(rows) + '</table>')


def hex_dump(data):
    """The HEX: block a detail page shows, in the offset: bytes layout the
    client reads back with a `[0-9a-f]{4}:` strip."""
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        lines.append(f"{offset:04x}: " + " ".join(f"{b:02x}" for b in chunk))
    return "\n".join(lines)


def detail_page(dcp_id, date_text):
    """
    One message (dcp.do?action=ACTION_LIST&dcp_date=...).

    get_message_detail reads <th>Label:</th><td>value</td> pairs and takes
    the message body out of the <pre> under the "HEX:" heading, so that is
    what this renders. The date must match a row from list_page exactly --
    the client says as much in its error text.
    """
    dcp = {d["id"]: d for d in DCPS}[dcp_id]
    match = None
    for moment in message_moments():
        if moment.strftime("%d/%m/%Y %H:%M:%S") == date_text:
            match = moment
            break
    if match is None:
        return None

    body = xml_body(dcp["station"], match)
    rows = [
        ("DCP ID:", dcp_id),
        ("Name:", dcp["name"]),
        ("Date:", date_text),
        ("Size of Data:", str(len(body))),
        ("Flag:", "G"),
    ]
    table = "<table>" + "".join(
        f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows
    ) + (
        f"<tr><th>HEX:</th><td><pre>{hex_dump(body)}</pre></td></tr>"
    ) + "</table>"
    return PAGE.format(operator=OPERATOR, content=table)


LOGIN_PAGE = PAGE.format(operator="", content=(
    '<form action="logon.do" method="post">'
    '<input name="user"><input name="password" type="password">'
    '<input type="submit" value="Logon"></form>'
    "<p>Please log on to continue.</p>"))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MockDCS/1.0"

    def log_message(self, fmt, *args):
        print("[mock-dcs] " + fmt % args, flush=True)

    # -- helpers ----------------------------------------------------------

    def _send(self, body, content_type="text/html; charset=utf-8", status=200,
              extra_headers=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    @property
    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _authenticated(self):
        """A session cookie from logon.do, or user/pass as query params."""
        cookie = self.headers.get("Cookie", "")
        if "JSESSIONID=" in cookie:
            return True
        query = self._query
        return (query.get("user", [None])[0] == USER
                and query.get("pass", [None])[0] == PASSWORD)

    # -- routes -----------------------------------------------------------

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        # The client posts username/password/submit (confirmed against the real
        # service via DevTools, see DCSWebServiceClient._login_session); accept
        # "user" too so a hand-driven curl against this mock also works.
        user = form.get("username", form.get("user", [""]))[0]
        password = form.get("password", [""])[0]

        if user == USER and password == PASSWORD:
            self._send(admin_page(), extra_headers=[
                ("Set-Cookie", "JSESSIONID=mock-session; Path=/")])
        else:
            # What the real service does on a bad credential: the login form
            # again, which is what the client detects as a failed logon.
            self._send(LOGIN_PAGE, status=200)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        query = self._query
        action = query.get("action", [""])[0]

        if path.endswith("/logon.do"):
            return self._send(LOGIN_PAGE)

        if not self._authenticated():
            return self._send(LOGIN_PAGE)

        if action == "DCP_ADMIN":
            return self._send(admin_page())

        dcp_id = query.get("id", [""])[0]
        known = {d["id"]: d for d in DCPS}

        if action == "ACTION_DOWNLOAD":
            dcp = known.get(dcp_id)
            if dcp is None or not dcp["transmits"]:
                # An unregistered or silent DCP answers with a page, not gzip
                # -- the case the guide's feedback catalogue calls out.
                return self._send(PAGE.format(
                    operator=OPERATOR,
                    content=f"<p>No messages available for {dcp_id}.</p>"))
            payload = gzip.compress(bulk_for(dcp))
            return self._send(payload, content_type="application/x-gzip")

        if action == "ACTION_LIST":
            if dcp_id not in known:
                return self._send(PAGE.format(
                    operator=OPERATOR, content="<p>Unknown DCP.</p>"))
            # dcp.do is the single-message view; dcpAdmin.do is the list.
            if path.endswith("/dcp.do"):
                date_text = query.get("dcp_date", [""])[0]
                page = detail_page(dcp_id, date_text)
                if page is None:
                    return self._send(PAGE.format(
                        operator=OPERATOR,
                        content=f"<p>No message at {date_text}.</p>"))
                return self._send(page)
            return self._send(list_page(dcp_id))

        return self._send(admin_page())


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 80), Handler)
    print(f"[mock-dcs] serving on :80 as {USER}, operator {OPERATOR}, "
          f"{len(DCPS)} registered DCP(s)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
