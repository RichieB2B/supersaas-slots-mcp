# SuperSaaS schedule internals

How `supersaas_mcp.py` turns a public SuperSaaS page into a slot list. Read [README.md](README.md) for installation and the tool contract.

The server reads the public page configuration and calls the same AJAX endpoint as the browser. Resource schedules require availability arithmetic; capacity schedules publish explicit classes and counts. The fixtures in [`fixtures/`](#fixtures) pin the observed resource and Jazzercise capacity response shapes.

## Page model

A schedule page is HTML containing an inline script with bare assignments. The server reads them with `\b<name>\s*=\s*(\d+)\b` (via `_number`) or a JSON parse for arrays:

| Variable | Unit | Meaning |
| --- | --- | --- |
| `filter` | — | Selected resource id. `0` means the page is a resource picker. |
| `rp_id` | — | Numeric schedule id, used to build the AJAX path. |
| `token` | — | Public read token embedded in the page. Sent as `v=12&token=`. |
| `bit_prefs` | bitmask | Which weekdays are open, and other preference bits. |
| `open_times` | minutes | Opening and closing minute-of-day per weekday. |
| `default_length` | seconds | Appointment duration. `86400` marks a date-only schedule. |
| `buffer` | **minutes** | Free gap required before and after a booking. |
| `add_limit` | **seconds** | Minimum advance notice. `0` = unlimited. |
| `early_limit` | **seconds** | Maximum booking horizon. `0` = unlimited. |
| `rounding` | seconds | Time-step of the grid. `86400` = daily rounding. |
| `cluster`, `complex`, `sync` | flags | Features the server refuses. |
| `local` | bool | Observed `false` on every page tested; epochs are read as UTC. |

Note the unit asymmetry: `buffer` is minutes while `default_length`, `add_limit` and `early_limit` are seconds. `buffer` is converted once, at parse time (`buffer_seconds`).

`start` is not a number — it is `precalc_constraints('<list>')`, see [start times](#start-times).

### Weekday indexing

SuperSaaS numbers weekdays from **Sunday = 0**, so `_weekday()` returns `(day.weekday() + 1) % 7`. The same ordering applies to the low seven bits of `bit_prefs` and to `open_times`. Getting this off by one silently shifts every opening hour by a day.

### open_times

A 14- or 28-element array; the server pads to 28. For weekday `w`, the first period is `(open_times[w], open_times[w + 7])` and the second is `(open_times[w + 14], open_times[w + 21])`, so a day can have at most two regular periods. `null` means unset. Values are minutes of day, `0..1440`, and `1440` means midnight at the end of the day.

`SLEEP` illustrates the sparse layout: `[570, 570, ..., 1320, 1320, 750, 1320, ...]` with `null`s in the second half except for two extra periods.

### bit_prefs

`bit_prefs & (1 << weekday)` decides whether a day is open at all. The upper bits carry unrelated preferences, so only the low seven are meaningful here — `SLEEP` is `0b1111001`, i.e. Monday **and** Tuesday closed, with a type `1` exception later reopening one Tuesday.

## Fetching

`_get()` enforces, on both the request and the response after redirects:

- scheme `https`, no userinfo, port `None` or `443`;
- final host in `SCHEDULE_HOSTS` (`supersaas.nl` / `supersaas.com`, bare and under `www.`, `m.` and `d.`) when loading a schedule;
- at most `MAX_BYTES` (5 MB), `TIMEOUT` 20 s.

`resolve_schedule_url()` accepts either a SuperSaaS URL or a third-party page. For a third-party page it scans the HTML with `SCHEDULE_LINK` for links to `/schedule/...`, handling protocol-relative URLs and entities via `unescape()`. Zero or several matches is an error, and the several case lists the candidates so the caller can pick.

### Links kept in JS bundles

Single-page apps render an empty shell and keep the booking link in a bundled script, so the HTML scan finds nothing. `guistcreek.com/booking` is this shape: its link lives in `/assets/index-CPjZSA-J.js`. When the HTML yields no link, `links_from_scripts()` fetches the page's own scripts and scans them with the same regex, which already matches a URL inside a JS string literal.

Three deliberate limits:

- **Same origin only.** `same_origin_scripts()` resolves each `<script src>` against the page URL and keeps only those sharing the page's scheme and host. A customer's configuration cannot live in a third-party bundle, and this keeps the extra fetches inside the trust boundary of the page the caller named. SuperSaaS's own `cdn.supersaas.net/widget.js` is therefore skipped.
- **At most `MAX_SCRIPTS` (10)**, deduplicated, each bounded by the existing `MAX_BYTES`.
- **HTML wins.** Scripts are fetched only when the HTML yields nothing, so a conventional page costs what it always did. An unreachable script is skipped rather than aborting discovery, so one dead bundle cannot hide the link in the next.

The widget endpoint itself is deliberately not supported. `https://www.supersaas.com/widget/index/633382/842100` does return a parseable resource page — the two ids are account and `rp_id` — but for GuistCreek it reports `filter=0` across 42 pitches, so it can only ever produce the "pick a resource" error, and the widget URL carries no resource names to recover. The bundle link resolves to the named schedule, which is the useful path.

### Host canonicalisation

`www`, `m` (mobile) and `d` are mirrors: `www` and `d` return byte-identical bodies, and `m` returns a lighter page carrying the same configuration variables and the same parsed values. Every schedule URL therefore passes through `_canonical_schedule_url()`, which rewrites the host to `www.` and drops the query string and trailing slash, so the rest of the pipeline sees one shape and `schedule_url` in the response is stable.

Dropping the query matters beyond tidiness: view parameters such as `?view=week` and `?m=1` are client state, and a page linking both `/SLEEP` and `/SLEEP?view=week` would otherwise look ambiguous. The same applies to `/SLEEP` versus `/SLEEP/`, which is a real pattern in the wild — SuperSaaS's own documentation prints schedule URLs both ways.

`SCHEDULE_LINK` excludes `?`, `#` and `[` from the path class so the query never reaches the dedup step, and strips sentence punctuation with `TRAILING_PUNCTUATION` for bare URLs in prose.

### Schedule type detection

`SCHEDULE_ASSET` matches `/assets/(resource|capacity|service)-<hex>.js`, the script SuperSaaS loads per schedule type. It is the clean discriminator: capacity pages carry `overbooking`, `first_hour` and `rp_name` instead of `filter` and `resource[]`, and no `precalc_constraints` at all. The asset name is not entity-encoded, so it matches identically before and after `unescape()`.

Each page references the script twice — the CDN form plus an inline `document.write` local fallback — which is why the check collects a `set`. Capacity pages go through `parse_capacity_schedule()` before the resource start-time parser. Service pages still raise a type-specific error. Six of fourteen customer schedules sampled were capacity type.

### Capacity schedules

Jazzercise publishes `rp_id`, `token`, `add_limit`, `from_utc`, and an inline `app` array. The page script fetches further classes from `/ajax/capacity/<rp_id>` with `token`, `afrom`, and `ato`. The server uses that endpoint for each 28-day chunk. Its `app` rows use `[start_epoch, end_epoch, slot_id, capacity, booked, ..., title, ..., waiting, location, ...]`. Epochs represent schedule wall time, as on resource pages. `from_utc` converts the current UTC time to schedule wall time for booking-window checks. `capacity -1` means unlimited; other non-positive capacities, negative booked values, and classes without a normal seat are excluded. SuperSaaS includes waitlisted places in `booked`, so remaining ordinary seats are `capacity - booked + waiting`.

Capacity entries preserve the class ID, title, location, capacity, booked and waiting counts, and remaining seats. The page's `add_limit` and optional `early_limit` are applied when booking-window filtering is enabled. Class length is per row, so the top-level response has no fixed duration.

Availability is fetched from `/ajax/resource/<rp_id>` with `v=12`, `token`, `afrom`, `ato`, `ad=r`, `efrom=1970-01-01`, `eto`, `ed=r`. Requests are chunked to `CHUNK_DAYS` = 28 days, capped at `MAX_DAYS` = 366.

The `efrom=1970-01-01` is deliberate: SuperSaaS selects exception rows by their **start** date, so a blocked range that began before the requested window would be omitted by a naive query. Widening `efrom` while keeping `eto` at the window end returns it, and `_periods()` then tests overlap against each day.

### AJAX arrays

- `app` — bookings as `[start_epoch, end_epoch, resource_id, ...]`. Rows are filtered by `resource_id` so a sibling resource cannot block this one.
- `exc` — opening modifications as `[start_epoch, end_epoch, type, ...]`. Type `0` closes every overlapping date; type `1` **adds** an interval and carries closing/opening minutes at indices 3 and 4. Type `1` is additive, not a replacement, which is why an extra opening on an already-open day widens it rather than shrinking it.

Rows shorter than three fields, or unknown exception types, raise rather than being skipped.

## Start times

`precalc_constraints()` is a small language, ported as `parse_constraints()`. SuperSaaS's own definition lives in `assets/resource-*.js`.

- A **positive** token is a minute-of-day offset: `570` is 09:30.
- **`0`** is the hourly grid, `00:00`–`23:00`. It does *not* mean "midnight only" — this was the source of a silent-zero bug.
- A **negative** `-N` is a grid every `N` minutes, so `' -30 0'` is every half hour and `' -5 0'` every five minutes.
- `:<mask>=<list>` replaces the days in the weekday bitmask (Sunday first). `'540 720:1=60 120'` gives Sunday 01:00/02:00 and every other day 09:00/12:00.
- With `rounding=86400`, SuperSaaS keeps one start time for the day and ignores per-weekday suffixes; `parse_constraints()` returns `base[1:2] or base[:1]` to match.

Expansion is deduplicated and sorted. Grids are then filtered by that weekday's `open_times` periods, so a 24-hour grid on an office open 09:00–17:00 yields eight slots, not a midnight-only answer.

## Date-only schedules

`default_length == 86400` means the product is whole days: nightly rentals, marina pitches, court hires. The server switches units and reports `unit: "night"`. **`rounding` is not part of the test.** It was originally `rounding=86400 and default_length >= 86400`, which worked for `Rental_Homes/House_1` but wrongly rejected GuistCreek Marina, whose page sets `rounding=60` with the same 24-hour unit; all 36 of its live bookings are exact whole days starting at 15:00, confirming the unit is a night. `rounding` only sets the UI time-step and is irrelevant once the duration is a full day.

A larger unit is rejected outright rather than approximated: `default_length` of 172800 is a two-day product, and the single-night arithmetic below would silently mis-report it.

SuperSaaS hides both clock times in the same start-time constraints. `'720 840'` decodes to check-out 12:00 and check-in 14:00 — the **later** value is arrival, the earlier one is next-morning departure. `_night_times()` returns that pair; a single value makes a full 24-hour night; a repeat grid expands to more than two values and is rejected, since it carries no arrival/departure pair.

In `calculate_slots()` a night is:

```python
begin  = midnight + checkin_minute * 60
finish = midnight + SECONDS_PER_DAY + checkout_minute * 60
```

Two consequences worth keeping when editing:

- **Real times, not calendar days.** A guest leaving at 12:00 frees the same afternoon's 14:00 arrival. Measured on `Rental_Homes/House_2` for Sep 28 – Oct 4: the correct model returns four nights (Sep 29 – Oct 2) because the Sep 28 booking costs only its arrival night, while treating a booking as occupying its check-out date as well throws away a fifth night that is genuinely free.
- **Containment is replaced by an open-day test.** A 24-hour span can never fit inside a single day's `(open, close)` window, so night mode requires only that `_periods()` be non-empty for the check-in date. Opening rules therefore apply per arrival: `House_1` has the Sunday bit clear in `bit_prefs`, so no night starts on Sunday and free nights collapse into Monday-to-Saturday stays.

`merge_stays()` then joins nights whose check-out date equals the next check-in date, producing `stays`.

## Guards

Each rejection exists because the alternative is a wrong answer rather than an error:

| Condition | Error | Why guard instead |
| --- | --- | --- |
| `cluster`, `complex`, `sync` set | Unsupported feature | Availability depends on rules not present in the page. |
| `filter=0` | Pick a resource | The page has no single resource to compute for. |
| Capacity page without public `app` | No public slots | The page does not expose class data. |
| Service asset | Schedule type unsupported | Shared-staff services come from a different endpoint. |
| `precalc_constraints('')` | No start-time grid | Visitors pick their own times, so there is no fixed grid to replay. |
| No `open_times` at all | Behind a login | SuperSaaS returns HTTP **200** with a login stub, not a 4xx, so this presents as missing data. |
| `open_times` but no `start` | No start times | Slots are generated from the request time; the server cannot replay them. |
| `default_length` above 86400 | Multi-day unit | The night arithmetic covers one day, so a two-day product would be mis-reported. |
| `open_times` length outside 14–28 | Unsupported layout | The two-period-per-day assumption breaks. |

`add_limit` and `early_limit` of `0` mean *unlimited*, not *now* — matching the truthiness guards in SuperSaaS's own script. Treating `0` as a real limit would silently discard every slot on schedules that set neither, which is most public ones.

## Fixtures

Tests run offline against saved pages so they cannot drift as customers book:

| Fixture | Shape it pins |
| --- | --- |
| `SLEEP.html`, `ajax-response.json` | Baseline resource schedule: three explicit start times, 28-entry `open_times`, Mon/Tue closed, additive and blocking exceptions, buffer of 30 minutes, real advance limits. |
| `RESERVEREN _ Down the hatch.html` | Business page carrying exactly one schedule link. Protocol-relative and entity-encoded forms are covered by synthetic cases. |
| `meeting-room.html`, `meeting-room-ajax.json` | The hourly-grid start time `'0'` inside 09:00–17:00 hours. Correct week is 23 slots; a literal reading of `0` returns none. |
| `rental-home.html`, `rental-home-ajax.json` | Date-only unit: check-in 14:00, check-out 12:00, Sunday closed, one booking that occupies a night while leaving its check-out night free, and the merge into Monday-to-Saturday stays. |
| `jazzercise-capacity-ajax.json` | Live public capacity response captured on 2026-09-27 for Sep 27–Oct 4: 13 classes, including simultaneous classes, varied lengths, capacities and remaining seats. |

`CapacityScheduleTests` uses a compact page sample based on Jazzercise's public page, the saved response, and synthetic edge cases for full and unlimited classes, response truncation, and booking limits.

Live behaviour is spot-checked separately, not asserted: `downthehatch/SLEEP` returned the documented October 22 slot on 2026-09-27 and correctly returned none on 2026-09-28 once that week booked out.
