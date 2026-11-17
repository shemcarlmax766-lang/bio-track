// Screenshot harness: logs in, then captures BioTrack at the required viewports
// and reports any overflow / clipping / overlap issues it can detect.
const puppeteer = require("puppeteer-core");
const fs = require("fs");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const OUT = process.env.OUT || "/home/user/bio-track/.shots";
const EMAIL = process.env.EMAIL || "admin@biotrack.ug";
const PASSWORD = process.env.PASSWORD || "BioTrack2026!";

const VIEWPORTS = [
  { name: "1440x900", width: 1440, height: 900 },
  { name: "1366x768", width: 1366, height: 768 },
  { name: "1280x720", width: 1280, height: 720 },
  { name: "1024x768", width: 1024, height: 768 },
  { name: "768x1024", width: 768, height: 1024 },
  { name: "390x844", width: 390, height: 844 },
];

const PAGES = (process.env.PAGES || "dashboard:/,equipment:/equipment,fault:/faults/1,form:/equipment/new,maintenance:/maintenance")
  .split(",")
  .map((pair) => {
    const idx = pair.indexOf(":");
    return { name: pair.slice(0, idx), path: pair.slice(idx + 1) };
  });

const AUDIT = () => {
  const issues = [];
  const docW = document.documentElement.scrollWidth;
  const winW = window.innerWidth;
  if (docW > winW + 1) {
    issues.push(`horizontal scroll: page ${docW}px vs viewport ${winW}px`);
  }
  const vh = window.innerHeight;
  // clipped / overflowing text blocks
  document.querySelectorAll("h1,h2,h3,.metric-label,.metric-value,.metric-foot,.donut-legend .row,.side-link span,.pill").forEach((el) => {
    const style = getComputedStyle(el);
    if (el.scrollWidth > el.clientWidth + 2 && style.overflow === "visible") {
      const txt = (el.textContent || "").trim().slice(0, 32);
      if (txt) issues.push(`text overflow in <${el.tagName.toLowerCase()} class="${el.className}">: "${txt}"`);
    }
  });
  // elements wider than the viewport
  const insideScroller = (el) => {
    let node = el.parentElement;
    while (node && node !== document.body) {
      const ox = getComputedStyle(node).overflowX;
      if (ox === "auto" || ox === "scroll") return true;
      node = node.parentElement;
    }
    return false;
  };
  document.querySelectorAll("main *, .sidebar *").forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > winW + 2 && !insideScroller(el)) {
      issues.push(`element extends past viewport: <${el.tagName.toLowerCase()} class="${el.className}"> right=${Math.round(r.right)}`);
    }
  });
  // inconsistent metric card heights
  const byRow = {};
  [...document.querySelectorAll(".metric")].forEach((m) => {
    const r = m.getBoundingClientRect();
    const key = Math.round(r.top);
    (byRow[key] = byRow[key] || []).push(Math.round(r.height));
  });
  Object.entries(byRow).forEach(([top, heights]) => {
    if (heights.length > 1 && Math.max(...heights) - Math.min(...heights) > 2) {
      issues.push(`metric card heights differ in row y=${top}: ${heights.join(", ")}`);
    }
  });
  // donut + legend alignment
  const donut = document.querySelector(".donut");
  const legend = document.querySelector(".donut-legend");
  if (donut && legend) {
    const a = donut.getBoundingClientRect();
    const b = legend.getBoundingClientRect();
    const sideBySide = b.left >= a.right - 1;
    if (sideBySide) {
      const dCy = a.top + a.height / 2;
      const lCy = b.top + b.height / 2;
      if (Math.abs(dCy - lCy) > 26) {
        issues.push(`donut/legend vertical centres differ by ${Math.round(Math.abs(dCy - lCy))}px`);
      }
    } else if (b.top < a.bottom - 1) {
      issues.push("donut legend overlaps the chart when stacked");
    }
    // legend labels must not be visually truncated, and must have room to read
    document.querySelectorAll(".donut-legend .lbl").forEach((el) => {
      if (el.scrollWidth > el.clientWidth + 1) {
        issues.push(`legend label truncated: "${(el.textContent || "").trim().slice(0, 24)}"`);
      }
      if (el.getBoundingClientRect().width < 84) {
        issues.push(`legend label column too narrow: ${Math.round(el.getBoundingClientRect().width)}px ` +
                    `for "${(el.textContent || "").trim().slice(0, 20)}"`);
      }
    });
  }
  // sidebar identity tile: whichever variant is showing must fit on screen
  const photoTile = document.querySelector(".side-photo");
  const compactTile = document.querySelector(".side-identity");
  [["photo", photoTile], ["compact", compactTile]].forEach(([name, el]) => {
    if (!el) return;
    const style = getComputedStyle(el);
    if (style.display === "none" || el.getBoundingClientRect().height === 0) return;
    const r = el.getBoundingClientRect();
    if (r.bottom > vh + 1) {
      issues.push(`sidebar ${name} identity tile extends below the fold: bottom=${Math.round(r.bottom)} vh=${vh}`);
    }
  });
  // at least one identity variant should be visible on desktop widths
  if (winW > 1080) {
    const visible = [photoTile, compactTile].some((el) => {
      if (!el) return false;
      return getComputedStyle(el).display !== "none" && el.getBoundingClientRect().height > 0;
    });
    if (!visible) issues.push("no hospital identity tile visible in the sidebar");
  }

  // sidebar: can every nav item be reached?
  const nav = document.querySelector(".side-nav");
  if (nav) {
    const navRect = nav.getBoundingClientRect();
    [...nav.querySelectorAll(".side-link")].forEach((link) => {
      const r = link.getBoundingClientRect();
      if (r.bottom > vh + 1) {
        issues.push(`nav item below fold: "${(link.textContent || "").trim()}" bottom=${Math.round(r.bottom)} vh=${vh}`);
      }
    });
    if (nav.scrollHeight > nav.clientHeight + 1) {
      issues.push(`nav scrolls (scrollHeight ${nav.scrollHeight} > ${nav.clientHeight})`);
    }
  }
  return issues;
};

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME || "/tmp/chromium",
    headless: "shell",
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--single-process",
      "--no-zygote",
      "--font-render-hinting=none",
    ],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });

  await page.goto(`${BASE}/login`, { waitUntil: "networkidle2" });
  await page.type("#email", EMAIL);
  await page.type("#password", PASSWORD);
  await Promise.all([
    page.waitForNavigation({ waitUntil: "networkidle2" }),
    page.click("button[type=submit]"),
  ]);

  const report = {};
  for (const vp of VIEWPORTS) {
    await page.setViewport({ width: vp.width, height: vp.height });
    for (const target of PAGES) {
      await page.goto(`${BASE}${target.path}`, { waitUntil: "networkidle2" });
      await new Promise((r) => setTimeout(r, 140));
      const issues = await page.evaluate(AUDIT);
      const key = `${vp.name} ${target.name}`;
      report[key] = issues;
      const full = process.env.FULL === "1";
      await page.screenshot({
        path: `${OUT}/${vp.name}-${target.name}.png`,
        fullPage: full,
      });
    }
  }
  await browser.close();

  let total = 0;
  for (const [key, issues] of Object.entries(report)) {
    total += issues.length;
    if (issues.length) {
      console.log(`\n### ${key}`);
      issues.forEach((i) => console.log("  - " + i));
    }
  }
  console.log(`\n${total} issue(s) across ${Object.keys(report).length} page/viewport combinations`);
  fs.writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2));
})();
