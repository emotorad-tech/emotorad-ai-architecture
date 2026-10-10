# Recent weather for the rider's area: design

9 October 2026 · Sagnik Mukherjee · approved in chat on 9 October 2026 · status: spec, awaiting review

## Why

Four battery records turn on the temperature where the rider charges or keeps the bike:

| Record | What temperature decides |
|---|---|
| `knowledge/battery/wont-charge.yaml` | The BMS blocks charging outside 10 °C to 40 °C, "the single most common cause of it just will not charge in summer". |
| `knowledge/battery/charging-slowly.yaml` | The BMS slows charging when the pack is cold. The escalation rule assumes room temperature. |
| `knowledge/battery/range-dropped.yaml` | Cold weather cuts usable capacity for a while. Escalate only if range halved "with no change in terrain, load or weather". |
| `knowledge/battery/storage.yaml` | Store indoors, out of direct sun, ideally between 10 °C and 25 °C. |

No motor record depends on temperature. Today the agent has to ask the rider how hot or cold it has been, and riders rarely know. EMotorad has bought Open-Meteo's Standard plan, so the bot can look up the last two weeks at the rider's area, say what it found, and ask the rider once whether that matches where they charge.

## Decisions taken (9 October 2026)

| Question | Decision |
|---|---|
| How the weather is fetched | A tool the agent calls when a record needs it, not fetched for every chat |
| Window | Current temperature and the last 14 days' daily highs and lows |
| Confirmation | State it with the area and ask once. The rider's answer wins. |
| Where | The centre of the rider's pin code (the chat's area), never the rider's exact position |
| Endpoint | Open-Meteo's forecast endpoint with `past_days=14` (current up to today). Not the archive endpoint, which runs 2 to 5 days behind. |

## 1. Settings

- **`EMOTORAD_OPEN_METEO_API_KEY`**: the paid plan's key, in the config store (`/emotorad/stage/ai/app`). Set by a person, never read, printed or written to a file by a Claude session.
- **`EMOTORAD_OPEN_METEO_BASE_URL`**: optional, default `https://customer-api.open-meteo.com`. Set only for a test server.
- **Without the key** the tool is not registered, so no agent is offered it, and `/health` says `"weather": "not configured"`. **With it**, `"weather": "open-meteo"`.
- **Rollback:** remove the key and redeploy. The agents go back to asking the rider.

## 2. The client (`src/emotorad_ai/weather.py`)

One request per lookup:

```
GET {base}/v1/forecast?latitude=18.56&longitude=73.91
    &current=temperature_2m
    &daily=temperature_2m_max,temperature_2m_min
    &past_days=14&forecast_days=1&timezone=Asia/Kolkata
    &apikey=<key>
```

- **The point** is the centre of the rider's pin code from `geo.PincodeCentres`, rounded to 2 decimal places (about 1 km). The rider's own position is never sent, and nothing else about them is sent: no phone, name or id.
- **Limits:** the request times out after 3 seconds. The response is read up to 256 KB.
- **The cache** is one entry per pin code for 1 hour, in memory. After a failure, lookups fail at once for 60 seconds (the breaker pattern of `tools/oms_db.py`).
- **Errors** carry the exception class or the HTTP status only. The URL holds the key, so it is never logged or put into an error message or an event. A test asserts this.
- **What it returns:** a `WeatherReport`.
  - `current_c`: the current temperature.
  - `days`: the daily rows, `[{"days_ago": 0..14, "max_c", "min_c"}]`, today first. A row with a missing value is skipped.
  - `fetched_at`.

## 3. What the model is given (`weather.summary`)

A short summary, temperatures rounded to whole degrees:

| Field | Meaning |
|---|---|
| `area` | `{"pincode", "district", "state"}` the weather is for |
| `current_c` | Temperature now |
| `highest_c`, `lowest_c` | Highest daily high and lowest daily low over the 15 days (today and the 14 before) |
| `days_above_40c` | Days whose high went over 40 °C, the top of the charging band |
| `days_below_10c` | Days whose low went under 10 °C, the bottom of the charging band |
| `last_7_days_avg_high_c`, `previous_7_days_avg_high_c` | Mean daily high for days 0 to 6 and days 7 to 13, for "it has turned colder or hotter lately" |
| `days` | The rows above, by `days_ago`, never by calendar date |

**No calendar dates are given.** The bot says "this week" or "a few days ago", and the date post-check (`date_check.py`) never has a weather date to judge.

## 4. The tool: `get_recent_weather`

- **Who has it:** the customer persona's battery and narrow agents, read-only. It is registered only when the key is set. The dealer persona never has it, and a test asserts that.
- **Arguments:** an optional `pincode`. It is accepted only if the rider typed it in this chat, under the same rule as `find_nearest_dealers` (`tools/mocks._TYPED_PINCODE` over `customer_messages`, any script's digits). An unknown key is rejected by the registry.
- **Injected:** `area` (the chat's area from a location or a typed pin code, spec 2026-10-09 nearest dealers) and `customer_messages`.
- **Outcomes:**

| Outcome | When | What the agent does |
|---|---|---|
| `ok` | an area and a report | states the figures with the area and asks once if that matches where the bike is charged or kept |
| `no_area` | no area and no typed pin code; returned as an `ok` result carrying the `request_location` action, as the dealer tool does | asks for the pin code, or the rider taps "Share my location" |
| `bad_pincode` | the typed pin code is not one India Post knows, has no centre, or the rider never typed it | asks for the pin code |
| `weather_unavailable` | the request failed, timed out, or the breaker is open | asks the rider about the temperature, as before |

A typed pin code also becomes the chat's area (`source: typed`), as the dealer tool's does. The runtime's `_dealer_cards` already reads `area` from a successful result; the weather tool's result is read the same way.

## 5. The prompt rule (`agents/base.WEATHER_RULE`)

Appended for an agent that has the tool and whose registry holds it, as `DEALER_RULE` is:

- When a record needs the temperature (the battery will not charge, charges slowly, range dropped, storage), call `get_recent_weather` instead of asking the rider.
- Say what it found, with the area, in one short sentence: for example, "It has been up to 41 °C around Pune this week." Then ask once: "Is it about that warm where you charge or keep the bike, or cooler indoors?"
- Never say "very hot" or "extremely hot", and never ask whether it is hot. The battery safety gate stops a chat on those words, whoever writes them (the final review, 9 October 2026).
- The rider's answer wins. If they charge indoors, in an air-conditioned room or in a basement, go by what they say.
- Never blame a fault on the weather alone, and never use the weather to refuse or hold up a ticket.
- If the tool says `weather_unavailable`, ask the rider about the temperature as before.
- Never give a calendar date for the weather.

The knowledge records are frozen and do not change. The rule is the only place this behaviour lives.

## 6. Privacy

- Only a pin-code centre, rounded to about 1 km, leaves for Open-Meteo, a company outside AWS (in Switzerland). No phone, name, id or exact position.
- The weather summary is logged in the `tool_call` event like any tool result. It holds the area's pin code, which `redact_pii` already masks, and temperatures.
- Worth a line to Sachin, as with the other outside services.

## 7. Health and wiring

- `api.py` builds the client from the environment (`weather.client_from_env`) and passes it to `build_registry(weather=...)`.
- `/health` gains `"weather"`.
- The CLI and the playground keep it off.

## 8. Tests

- **Client, with a fake transport:**
  - the exact query parameters (2-decimal point, the 14 days, the time zone);
  - the key is never in an error message or an event;
  - a non-200 answer and a timeout are unavailable;
  - the 256 KB cap;
  - the 1-hour cache per pin code;
  - the 60-second breaker.
- **Summary:**
  - highest and lowest;
  - the 40 °C and 10 °C day counts;
  - the two 7-day means;
  - skipped rows with missing values;
  - no calendar date anywhere in the summary.
- **Tool, through the registry:**
  - absent without a client;
  - not in the dealer persona's slice;
  - each outcome;
  - a pin code the rider never typed is refused;
  - an extra argument is rejected.
- **Runtime:**
  - a conversation through `runtime.handle()` with a fake client, where the agent states the weather;
  - a typed pin code becomes the chat's area;
  - the rule is in the system prompt only when the tool is.
- **`/health`:** `weather`.

## Out of scope

- Fetching the weather for every chat.
- Forecasts beyond today.
- Humidity, rain and wind.
- The archive endpoint.
- The EU region.
- Changing the knowledge records (frozen).
