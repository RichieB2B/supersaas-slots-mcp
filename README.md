# SuperSaaS slots MCP server

<!-- mcp-name: io.github.RichieB2B/supersaas-slots-mcp -->

A read-only MCP server that reports which slots are free on a public SuperSaaS schedule. It covers both intraday appointment slots and date-only schedules such as nightly rentals. Give it a SuperSaaS schedule URL, or a business page that links to one, such as `https://www.down-the-hatch.nl/reserveren/`. No account or API key is needed.

Licensed under the [MIT License](LICENSE). How the schedule data is read is documented in [INTERNALS.md](INTERNALS.md).

## Install

After publication, use Python 3.10+:

```sh
pip install supersaas-slots-mcp
```

For local development from the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Dependencies are declared in `pyproject.toml`; FastMCP is pinned to version 4.0.10. Both `supersaas-slots` and `supersaas-slots-mcp` start the server.

## Connect

Configure a stdio MCP server in your MCP client:

```json
{
  "mcpServers": {
    "supersaas-slots": {
      "command": "/absolute/path/to/supersaas-mcp/.venv/bin/supersaas-slots"
    }
  }
}
```

Replace the command path with the absolute path to your project directory. The client must allow this local process to make HTTPS requests to `www.supersaas.nl` (or `www.supersaas.com`).

Or run it from PyPI with `uvx`:

```json
{
  "mcpServers": {
    "supersaas-slots": {
      "command": "uvx",
      "args": ["supersaas-slots-mcp"]
    }
  }
}
```

## Tool

### `find_available_slots`

```json
{
  "schedule_url": "https://www.supersaas.nl/schedule/downthehatch/SLEEP",
  "from_date": "2026-10-19",
  "through_date": "2026-10-25"
}
```

| Parameter | Default | Notes |
| --- | --- | --- |
| `schedule_url` | required | A SuperSaaS `/schedule/` URL, or a third-party page linking to exactly one. |
| `from_date` | required | `YYYY-MM-DD`, inclusive. |
| `through_date` | required | `YYYY-MM-DD`, inclusive. Up to 366 days. |
| `max_results` | `500` | Caps the returned `slots` list; `count` is always the full number. |
| `respect_booking_window` | `true` | Apply the schedule's minimum and maximum advance-booking limits. Set `false` to examine historical data. |

`schedule_url` may be a business page instead. The server scans it for a link to a SuperSaaS schedule, follows it, and reports the origin as `linked_from`.

### Response

```json
{
  "schedule_url": "https://www.supersaas.nl/schedule/downthehatch/SLEEP",
  "from_date": "2026-10-19",
  "through_date": "2026-10-25",
  "time_basis": "schedule wall time; epoch values interpreted as UTC",
  "unit": "slot",
  "duration_minutes": 180,
  "count": 1,
  "truncated": false,
  "slots": [{ "start": "2026-10-22 09:30", "end": "2026-10-22 12:30" }]
}
```

Times are schedule wall-clock strings, `YYYY-MM-DD HH:MM`. `truncated` describes `slots` only, so compare it against `count` before treating a list as complete. Data is re-fetched on every call, so results change as other people book.

`unit` is either `slot` or `night`. Appointment schedules return `slot` with `duration_minutes` set to the appointment length. Date-only schedules such as rentals return `night` and add three fields:

```json
{
  "unit": "night",
  "duration_minutes": 1440,
  "check_in": "14:00",
  "check_out": "12:00",
  "slots": [{ "start": "2026-10-05 14:00", "end": "2026-10-06 12:00" }],
  "stays": [{ "start": "2026-10-05", "end": "2026-10-10", "nights": 5 }]
}
```

Each `slots` entry is one night, from check-in on a date to check-out on the next; `duration_minutes` is the nominal 1440-minute night. `stays` merges consecutive free nights into the ranges you can actually book, so four adjacent nights become one four-night stay. Use `slots` when pricing per night and `stays` when offering a date range to the customer.

### Errors

Every error names a cause, and most have a remedy:

| Error | What to do |
| --- | --- |
| Page asks the visitor to pick a resource | Pass one resource URL, such as `.../Meeting_Rooms/Room_1` or `.../Rental_Homes/House_1`. |
| Page links to several schedules | The message lists them; pass the intended one directly. |
| Schedule is kept behind a login | Nothing to do — the schedule is not public. |
| Schedule publishes opening hours but no start times | Nothing to do — slots are generated per request, not from a fixed grid. |
| URL is not a public SuperSaaS HTTPS page | Use `https://` with no credentials or custom port. |

## Scope

Supported: public resource schedules with a single resource, fixed appointment duration, weekly opening hours with per-day exceptions and blocked ranges, booked appointments, buffer time, advance-booking limits, and both slot and night units.

Not modeled: scheduling plans other than the resource type, recurring rules, per-user limits, minimum-stay rules, and payment-dependent availability. An available slot is a calculated candidate, not a booking guarantee — the booking page remains authoritative at reservation time.

## Development

```sh
.venv/bin/python -m unittest -v test_supersaas_mcp.py
.venv/bin/python check_release.py
```

The tests run offline against saved copies of three real schedules, so they do not drift as customers book. See [INTERNALS.md](INTERNALS.md#fixtures) for what each fixture pins.

## Release

Releases use [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/) and [MCP Registry GitHub OIDC](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/github-actions.mdx). Before the first release:

1. Create a `pypi` environment in this GitHub repository and allow deployment from version tags. In PyPI, register a pending trusted publisher for owner `RichieB2B`, repository `supersaas-slots-mcp`, workflow `release.yml`, and environment `pypi`. The PyPI project does not need to exist yet.
2. Create an `mcp-registry` GitHub environment and allow deployment from version tags. The Registry uses GitHub OIDC, so it needs no registry token.
3. Keep the version in `pyproject.toml`, `server.json` (both version fields), and the FastMCP server constructor in sync. `check_release.py` verifies this and accepts the candidate tag with `--tag v0.2.0`. Commit the release before tagging it.
4. Push a matching tag, for example `git tag v0.2.0 && git push origin v0.2.0`.

The [release workflow](.github/workflows/release.yml) tests and builds the distribution, publishes it to PyPI, then submits `server.json` to the MCP Registry. The [CI workflow](.github/workflows/ci.yml) runs tests and package checks on pushes and pull requests. A pushed release tag publishes externally; review its commit and environment settings first.
