# Live-eval and end-to-end test photos

None of these shows a customer, a customer's bike or a real place.

- `charger-light-off.jpg`, `display-e07.jpg`: drawn with Pillow for the live evaluation. A scenario sends one as `fixture:<file>` (tests/data/live_scenarios.yaml); the loader inlines it as a data URL.
- `smoke-battery.jpg`: an image-model picture of an EMX with smoke coming from the battery, supplied by the person for the end-to-end test on 2026-09-29, saved at 1280 px as the chat page sends it. The test console (`/dev/e2e`) loads it as its sample photo.
