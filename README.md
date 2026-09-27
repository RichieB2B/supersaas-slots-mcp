# SuperSaaS slots MCP server

<!-- mcp-name: io.github.RichieB2B/supersaas-slots-mcp -->

A read-only FastMCP server for public **resource** schedules with one resource and declared start times. Give it either a SuperSaaS schedule URL or a business page that links to one, such as `https://www.down-the-hatch.nl/reserveren/`. It downloads the page, extracts `rp_id`, `token`, `bit_prefs`, `open_times`, appointment duration, buffer, and start-time constraints, then calls `/ajax/resource/<rp_id>` in 28-day windows. Each call explicitly requests the exception list with `efrom`, `eto`, and `ed=r`. No account or API key is needed for the tested public page.

Licensed under the [MIT License](LICENSE).

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

Replace the command path with the absolute path to your project directory. The client must allow this local process to make HTTPS requests to `www.supersaas.nl` (or `www.supersaas.com`). FastMCP handles the stdio protocol; the availability calculation remains in `supersaas_mcp.py`.

## Tool

`find_available_slots` accepts:

```json
{
  "schedule_url": "https://www.supersaas.nl/schedule/downthehatch/SLEEP",
  "from_date": "2026-10-19",
  "through_date": "2026-10-25"
}
```

`through_date` is inclusive. Optional `max_results` defaults to 500; the response includes the full `count` and `truncated` flag. Optional `respect_booking_window` defaults to `true` and applies the page's minimum and maximum advance-booking limits. A limit of `0` means unlimited, matching the truthiness guards in SuperSaaS's own page script, so a schedule that sets neither limit still returns future slots. Set `respect_booking_window` to `false` when examining historical schedule data.

`schedule_url` may also be a third-party page. The server scans it for links to `supersaas.nl/schedule/...` or `supersaas.com/schedule/...`, including protocol-relative ones and ones inside embedded JSON, then continues from the schedule it finds and reports the origin as `linked_from`. A page that links to no schedule, or to several, is an error naming the candidates so you can pass the intended one directly. Only HTTPS URLs without credentials or custom ports are fetched, and the schedule itself must still resolve to a SuperSaaS host.

Times are returned as schedule wall-clock strings (`YYYY-MM-DD HH:MM`). The schedule's numeric appointment and exception epochs are interpreted as UTC, matching the tested page. The server refreshes the page and AJAX data on each call, so results can change as bookings are made.

For the saved October fixture, the week of October 19 has one free slot: **October 22, 09:30–12:30**. Monday is closed by the low seven bits of `bit_prefs` (`0b1111001`, Sunday first). The October 13 Tuesday exception opens 09:30–12:30.

SuperSaaS selects exception rows by their **start date**. To catch a blocked range that began before the requested window, the AJAX query sets `efrom=1970-01-01` while keeping `eto` at the window's end. Exception type `0` blocks all overlapping dates; type `1` adds the listed opening interval. For example, the live response contains a type `0` block from February 19 through February 28, 2027, so the week of February 22 has **no available slots**.

## Start times

SuperSaaS writes its start times into the page as `start=precalc_constraints('<list>')`, and the server expands that string the same way SuperSaaS's own page script does:

- A **positive** number is a minute-of-day offset: `570` is 09:30.
- **`0`** is the hourly grid, `00:00` through `23:00`. It is a common default and does *not* mean "midnight only".
- A **negative** `-N` is a repeating grid every `N` minutes, so `' -30 0'` is every half hour and `' -5 0'` is every five minutes.
- A `:<mask>=<list>` suffix replaces the days selected by the weekday bitmask, which is numbered from Sunday, like `bit_prefs`. `'540 720:1=60 120'` opens Sunday at 01:00 and 02:00 and every other day at 09:00 and 12:00.
- When the page sets `rounding` to `86400`, SuperSaaS keeps a single start time for the day and ignores the per-weekday suffixes; the server does the same.

Expanded start times are then filtered by that weekday's opening periods, so a 24-hour grid on an office that opens 09:00–17:00 yields eight hourly slots rather than a midnight-only answer.

## Scope

This server handles the tested resource-schedule shape: one resource, fixed duration under 24 hours, up to two daily opening periods, explicit, repeating-grid, and per-weekday start times, weekday enable bits, additive opening exceptions, blocked ranges, booked appointments, and buffer time. It rejects schedules advertising clustering, synchronization, or complex linked rules. Other SuperSaaS schedule types, recurring rule patterns, per-user limits, and payment-dependent availability are not modeled. An available slot is a calculated candidate, not a booking guarantee; the booking page remains authoritative at reservation time.

Rejections name the cause and, where possible, the remedy:

- A page with `filter=0` asks the visitor to choose a resource, so pass one resource URL such as `.../Meeting_Rooms/Room_1`.
- A schedule publishing no opening hours is behind a login; SuperSaaS returns HTTP 200 with a login stub rather than a 4xx, so this presents as missing data.
- A schedule publishing opening hours but no start times generates its slots from the request time, which the server cannot replay.
- A `default_length` of 86400 or more is a date-only schedule, typically a nightly rental priced per `price_unit=86400`. This guard is load-bearing rather than merely conservative: with a 24-hour unit, a 12:00 start ends at minute 2160 and can never fit inside a single day's opening period, so the containment test would silently return zero slots.

## Test

```sh
.venv/bin/python -m unittest -v test_supersaas_mcp.py
```

The tests use the included copies of your example files, plus a saved copy of the SuperSaaS `Meeting_Rooms/Room_1` demo schedule and its AJAX response. That pair covers the hourly-grid start time, which the October fixture does not exercise: Room_1 declares `precalc_constraints('0')` inside 09:00–17:00 opening hours, and the correct week is 23 slots where a literal reading of `0` returns none.

Live calls drift as customers book, so the saved fixtures are the stable assertion. On 2026-09-27 a live read-only call returned the expected October 22 slot for `downthehatch/SLEEP`; by 2026-09-28 that whole week was booked out and the same call correctly returns none, while the October 13 exception day still resolves.

## Release

Releases use [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/) and [MCP Registry GitHub OIDC](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/github-actions.mdx). Before the first release:

1. Create a `pypi` environment in this GitHub repository and allow deployment from version tags. In PyPI, register a pending trusted publisher for owner `RichieB2B`, repository `supersaas-slots-mcp`, workflow `release.yml`, and environment `pypi`. The PyPI project does not need to exist yet.
2. Create an `mcp-registry` GitHub environment and allow deployment from version tags. The Registry uses GitHub OIDC, so it needs no registry token.
3. Keep the version in `pyproject.toml`, `server.json` (both version fields), and the FastMCP server constructor in sync. Commit the release before tagging it.
4. Push a matching tag, for example `git tag v0.1.0 && git push origin v0.1.0`.

The [release workflow](.github/workflows/release.yml) tests and builds the distribution, publishes it to PyPI, then submits `server.json` to the MCP Registry. The [CI workflow](.github/workflows/ci.yml) runs tests and package checks on pushes and pull requests. A pushed release tag publishes externally; review its commit and environment settings first.
