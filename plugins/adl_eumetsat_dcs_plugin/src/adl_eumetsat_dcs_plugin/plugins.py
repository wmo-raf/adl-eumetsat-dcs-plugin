import logging
import zoneinfo
from datetime import datetime, timedelta, timezone

from adl.core.registries import Plugin

from .utils import parse_transmission_time

logger = logging.getLogger(__name__)

# Messages are filtered on their transmission time (header date/time),
# which lags the observation times inside the body. The back-margin
# keeps observations transmitted shortly after start_date from being
# missed; the core's upsert dedupes, so over-inclusion is harmless.
TRANSMISSION_LAG_MARGIN = timedelta(hours=1)


class EumetsatDCSPlugin(Plugin):
    type = "adl_eumetsat_dcs_plugin"
    label = "ADL EUMETSAT DCS Plugin"

    def get_default_start_date(self, station_link):
        # First run without an explicit start_date shouldn't ingest the
        # whole month-long backlog by default.
        end_date = self.get_default_end_date(station_link)
        return end_date - timedelta(days=1)

    def get_station_data(self, station_link, start_date=None, end_date=None):
        client = station_link.network_connection.get_api_client()

        # TTL-cached full backlog: ACTION_DOWNLOAD ignores dcp_date
        # server-side, so every uncached call pulls everything. The
        # cache is what stops repeated full pulls inside one cycle.
        messages = client.get_latest_observations(station_link.dcp_id)

        records = {}
        for message in messages:
            tx_time = self._parse_transmission_time(message)
            if tx_time is None:
                continue
            if start_date is not None and tx_time < start_date - TRANSMISSION_LAG_MARGIN:
                continue
            if end_date is not None and tx_time > end_date:
                continue

            parsed = message.data
            if parsed is None:
                # Colon-tag / pseudo-binary encodings — a future Format C
                # parser slots into parse_xml_body without touching this.
                logger.info("DCP %s seq %s: unparsed body encoding, skipped",
                            message.dcp_id, message.sequence)
                continue

            for channel in parsed["channels"]:
                obs_time = self._resolve_channel_time(
                    channel, parsed.get("timezone"), station_link, message
                )
                if obs_time is None:
                    continue

                value = self._cast_channel_value(channel, message)
                if value is None:
                    continue

                record = records.setdefault(obs_time, {"observation_time": obs_time})
                record[channel["channel_id"]] = value

        return sorted(records.values(), key=lambda r: r["observation_time"])

    def _parse_transmission_time(self, message):
        """
        Parses the message header's transmission date/time (DD/MM/YY +
        HH:MM:SS, UTC) into an aware UTC datetime. Shared with the
        variable-mapping page's channel extraction.
        """
        return parse_transmission_time(message)

    def _resolve_channel_time(self, channel, body_timezone, station_link, message):
        """
        Resolves a channel entry's naive ISO time to an aware UTC
        datetime. A Format-A body's declared offset (e.g. +03:00) takes
        precedence; otherwise the station link's configured observation
        timezone applies.
        """
        time_str = channel["time"]

        if body_timezone:
            # Format A: the body declares its own fixed offset.
            try:
                obs_time = datetime.fromisoformat(f"{time_str}{body_timezone}")
            except ValueError:
                logger.warning("DCP %s seq %s: unparseable channel time %r with offset %r, skipped",
                               message.dcp_id, message.sequence, time_str, body_timezone)
                return None
        else:
            # Format B: no offset in the body — localize with the
            # configured station timezone. Non-YYYYMMDD dateformat
            # fallbacks ("YYYYMMDD;HHMMSS" raw strings) fail this parse
            # and are skipped rather than guessed at.
            try:
                naive = datetime.fromisoformat(time_str)
            except ValueError:
                logger.warning("DCP %s seq %s: unparseable channel time %r, skipped",
                               message.dcp_id, message.sequence, time_str)
                return None
            if naive.tzinfo is None:
                zone = zoneinfo.ZoneInfo(station_link.observation_timezone)
                obs_time = naive.replace(tzinfo=zone)
            else:
                obs_time = naive

        return obs_time.astimezone(timezone.utc)

    def _cast_channel_value(self, channel, message):
        """
        Casts a channel value to float. Values are kept as strings by the
        parser so a truncated message yields a clean per-channel skip
        here instead of killing the station's whole cycle.
        """
        if channel.get("errorcode"):
            logger.warning("DCP %s seq %s: channel %s has errorcode %s, skipped",
                           message.dcp_id, message.sequence,
                           channel["channel_id"], channel["errorcode"])
            return None

        try:
            return float(channel["value"])
        except (TypeError, ValueError):
            logger.warning("DCP %s seq %s: channel %s has non-numeric value %r, skipped",
                           message.dcp_id, message.sequence,
                           channel["channel_id"], channel["value"])
            return None
