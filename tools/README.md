# Responsive audit harness

Captures screenshots and runs automated layout checks at the viewport sizes
BioTrack supports. Requires Node and a Chromium binary.

```bash
npm i puppeteer-core
BASE=http://127.0.0.1:8000 OUT=/tmp/shots CHROME=/path/to/chrome node tools/responsive-audit.js
```

The script logs in with the demo administrator account, then for each
viewport (1440x900, 1366x768, 1280x720, 1024x768, 768x1024, 390x844) it visits
each page and asserts:

- no horizontal page scrolling
- no text clipped by its own container
- no element extending past the viewport outside an intended scroll container
- metric cards in the same row are the same height
- the donut and its legend are vertically centred when side by side
- no donut legend label is visually truncated
- every sidebar navigation item is reachable on screen

It exits with a per-issue report and writes `report.json` alongside the images.
Pass `FULL=1` for full-page screenshots.
