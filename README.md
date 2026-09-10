# ADL EUMETSAT DCS Plugin

Collects the messages that **Data Collection Platforms (DCPs)** transmit
through Meteosat into an [ADL](https://github.com/wmo-raf/adl) instance, by
logging in to the **EUMETSAT [Data Collection Service (DCS) Web
Service](https://user.eumetsat.int/resources/user-guides/dcs-web-service-user-guide)**
with an operator account and downloading each platform's message backlog.
One connection holds one set of DCS credentials; one station link binds an
ADL station to one DCP id and maps that platform's channel codes (`TAAV`,
`WSAV`, …) to ADL parameters — codes vary by platform and firmware, so the
mappings are per station.

The plugin also adds a **Browse DCP Messages** admin view for reading the
service's messages directly, which is a debugging convenience rather than
part of ingestion.

**Operator guide:** [docs/guide.md](docs/guide.md) — prerequisites, the
account and DCP registration, every connection and station-link field, the
DCP picker, the message browser, variable mappings, collection behaviour,
diagnostics and troubleshooting. The guide is also published on the central
ADL documentation site.

## Development setup

The plugin runs inside the ADL core image. Build the `adl:latest` image from
the [ADL core repository](https://github.com/wmo-raf/adl) first, then:

```bash
git clone https://github.com/wmo-raf/adl-eumetsat-dcs-plugin.git
cd adl-eumetsat-dcs-plugin
cp .env.sample .env        # set PLUGIN_BUILD_UID=$(id -u), PLUGIN_BUILD_GID=$(id -g), ADL_DB_PASSWORD
docker compose build
docker compose up
docker compose exec adl adl createsuperuser
```

The admin is served on `PORT` (default 8080). The plugin source is
bind-mounted, so code changes reload the dev server. If the build fails with
`pull access denied` for `adl:latest`, prefix the build with
`DOCKER_BUILDKIT=0`.

Tests are Django-runner tests under
`plugins/adl_eumetsat_dcs_plugin/src/adl_eumetsat_dcs_plugin/tests/`.
`docs/screenshots/mock-dcs/` is a stub of the DCS Web Service used by the
documentation capture harness, and is a useful way to exercise the client
without an account. Lint and format from `plugins/adl_eumetsat_dcs_plugin/`
with `make lint` and `make format`. See [CONTRIBUTING.md](CONTRIBUTING.md) —
a change to any connection or station-link field must update the guide in the
same PR, and a change to the client's parsing must update the mock.
