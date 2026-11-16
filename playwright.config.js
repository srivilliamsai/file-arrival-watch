import { defineConfig } from "@playwright/test";

const externalUrl = process.env.FILE_ARRIVAL_TEST_URL;

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.spec.js",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: externalUrl || "http://127.0.0.1:4174",
    launchOptions: process.env.FILE_ARRIVAL_CHROME_CHANNEL ? { channel: process.env.FILE_ARRIVAL_CHROME_CHANNEL } : {},
    trace: "retain-on-failure",
  },
  webServer: externalUrl ? undefined : {
    command: "python3 scripts/preview.py --port 4174 --demo-after-cutoff",
    url: "http://127.0.0.1:4174/",
    reuseExistingServer: false,
  },
});
