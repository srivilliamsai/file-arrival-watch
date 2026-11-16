import { accessToken, beginSignIn, finishSignIn, signOut } from "./auth.js";

const $ = (id) => document.getElementById(id);
const MAX_BYTES = 256 * 1024;
const formatDate = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const displayPreferenceKey = "file-arrival-time-display";
const states = {
  awaiting_file: ["Awaiting file", "", "muted"],
  checking_deadline: ["Checking deadline", "", "muted"],
  missing: ["Missing stock file", "!", "attention"],
  invalid_file: ["Invalid stock file", "!", "attention"],
  ready_on_time: ["Ready · on time", "✓", "normal"],
  ready_late: ["Ready · received late", "✓", "attention"],
  ready_no_cutoff: ["Ready", "✓", "normal"],
  cutoff_skipped: ["No cutoff today", "–", "muted"],
  check_delayed: ["Deadline check delayed", "!", "attention"],
};

let config;
let currentDate;
let lastStatus;
let lastUploads;
let timeFormats;
let busy = false;
let uploadAttempt;
let activityRequest = 0;
let statusRequest = 0;

function displayTimeZone(warehouseZone) {
  return $("time-local").getAttribute("aria-selected") === "true" ? new Intl.DateTimeFormat().resolvedOptions().timeZone : warehouseZone;
}

function setDisplayTimeZone(warehouseZone) {
  const timeZone = displayTimeZone(warehouseZone);
  $("time-display").disabled = false;
  const note = "Warehouse dates follow " + warehouseZone;
  if ($("warehouse-day-note").textContent !== note) $("warehouse-day-note").textContent = note;
  if (timeFormats?.timeZone === timeZone) return;
  const options = { hour: "numeric", minute: "2-digit", timeZone, timeZoneName: "short", hour12: true };
  timeFormats = {
    timeZone,
    cutoff: new Intl.DateTimeFormat("en-IN", options),
    receipt: new Intl.DateTimeFormat("en-IN", { ...options, second: "2-digit" }),
    day: new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }),
  };
  $("display-time-zone").textContent = timeZone;
  if (lastUploads && $("upload-activity").open) renderActivity(lastUploads);
}

function timeLabel(value, warehouseDay, withSeconds = false) {
  if (!value) return "—";
  const instant = new Date(value);
  const format = withSeconds ? timeFormats.receipt : timeFormats.cutoff;
  const time = format.format(instant).replace(/\s+/g, " ").toUpperCase();
  const dateParts = Object.fromEntries(timeFormats.day.formatToParts(instant).map(({ type, value }) => [type, value]));
  const displayDay = `${dateParts.year}-${dateParts.month}-${dateParts.day}`;
  return displayDay === warehouseDay ? time : dateLabel(displayDay) + " · " + time;
}

function dateLabel(value) {
  return formatDate.format(new Date(value + "T12:00:00Z"));
}

async function request(path, options = {}, authorized = false) {
  const headers = new Headers(options.headers);
  if (authorized) {
    const token = accessToken();
    if (!token) throw new Error("Your session expired. Sign in again to continue.");
    headers.set("Authorization", "Bearer " + token);
  }
  const response = await fetch(config.apiUrl + path, { ...options, headers });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    if (response.status === 401) {
      sessionStorage.removeItem("file-arrival-operator");
      updateSession();
    }
    throw new Error(result.message || (response.status === 401 ? "Your session expired. Sign in again." : "The delivery service is unavailable. Try again."));
  }
  return response.json();
}

function resultLabel(item) {
  if (item.status === "missing") return "Missing";
  if (item.status === "ready_late") {
    if (item.deadlineOutcome === "missing") return "Received late · missing at cutoff";
    if (item.deadlineOutcome === "invalid_file") return "Received late · invalid at cutoff";
    return "Received late";
  }
  if (item.status === "ready_on_time") return "On time";
  if (item.status === "invalid_file") {
    return item.deadlineOutcome === "missing" ? "Invalid file · missing at cutoff" : "Invalid file";
  }
  return states[item.status][0];
}

function makeResult(item) {
  const [, symbol, tone] = states[item.status];
  const result = document.createElement("span");
  result.className = "result";
  result.dataset.tone = tone;
  if (!symbol) result.dataset.icon = "clock";
  result.textContent = resultLabel(item);
  return result;
}

function renderHistory(items) {
  const table = document.createDocumentFragment();
  const mobile = document.createDocumentFragment();
  for (const item of items) {
    const row = document.createElement("tr");
    const due = timeLabel(item.cutoffAt, item.date);
    const received = timeLabel(item.receivedAt, item.date, true);
    const latest = timeLabel(item.latestUploadAt, item.date, true);
    for (const value of [dateLabel(item.date), due, received, latest, makeResult(item)]) {
      const cell = row.insertCell();
      cell.append(value);
    }
    table.append(row);
    const entry = document.createElement("li");
    const heading = document.createElement("div");
    heading.className = "row";
    heading.append(dateLabel(item.date), makeResult(item));
    const details = document.createElement("p");
    details.className = "details";
    const times = [item.cutoffAt ? "Due " + due : "No cutoff"];
    if (item.receivedAt) times.push("First valid " + received);
    if (item.latestUploadAt) times.push("Latest upload " + latest);
    details.textContent = times.join(" · ");
    entry.append(heading, details);
    mobile.append(entry);
  }
  $("history-rows").replaceChildren(table);
  $("history-mobile").replaceChildren(mobile);
  $("history-table").hidden = items.length === 0;
  $("history-mobile").hidden = items.length === 0;
  $("history-empty").hidden = items.length > 0;
  $("history-empty").textContent = "No delivery history yet.";
}

function renderStatus(payload) {
  const state = payload.today;
  const [title, icon, tone] = states[state.status];
  setDisplayTimeZone(payload.timeZone);
  $("warehouse").textContent = payload.warehouse;
  $("delivery-date").dateTime = state.date;
  $("delivery-date").textContent = dateLabel(state.date);
  $("state-title").textContent = title;
  let message = state.reason;
  if (state.status === "ready_late" && state.deadlineOutcome === "missing") {
    message += " The file was missing at the cutoff.";
  } else if (state.status === "ready_late" && state.deadlineOutcome === "invalid_file") {
    message += " The file available at the cutoff was invalid.";
  } else if (state.status === "invalid_file" && state.deadlineOutcome === "missing") {
    message += " This file arrived after the cutoff.";
  }
  $("state-message").textContent = message;
  $("state-mark").textContent = icon;
  $("delivery-state").dataset.tone = tone;
  $("cutoff").textContent = timeLabel(state.cutoffAt, state.date);
  $("received").textContent = timeLabel(state.receivedAt, state.date, true);
  $("latest-upload").textContent = timeLabel(state.latestUploadAt, state.date, true);
  $("retry-status").hidden = true;
  const recordedDays = payload.history.filter(({ status }) => status !== "not_monitored" && status !== "cutoff_skipped");
  renderHistory([state, ...recordedDays]);
}

async function refresh() {
  const requestNumber = ++statusRequest;
  try {
    const payload = await request("/api/status");
    if (requestNumber !== statusRequest) return;
    if (timeFormats?.timeZone === displayTimeZone(payload.timeZone) && JSON.stringify(lastStatus) === JSON.stringify(payload)) return;
    renderStatus(payload);
    lastStatus = payload;
    currentDate = payload.today.date;
  } catch {
    if (requestNumber !== statusRequest) return;
    if (lastStatus === null && !$("retry-status").hidden) return;
    lastStatus = null;
    currentDate = null;
    $("time-display").disabled = true;
    $("state-title").textContent = "Status unavailable";
    $("state-message").textContent = "The latest delivery status could not be loaded. Retry in a moment.";
    $("state-mark").textContent = "!";
    $("delivery-state").dataset.tone = "attention";
    $("delivery-date").textContent = "";
    $("cutoff").textContent = "—";
    $("received").textContent = "—";
    $("latest-upload").textContent = "—";
    renderHistory([]);
    $("history-empty").textContent = "History is currently unavailable.";
    $("retry-status").hidden = false;
  }
}

function updateSession() {
  const signedIn = Boolean(accessToken());
  $("sign-in").hidden = signedIn;
  $("sign-in").disabled = !config;
  $("sign-out").hidden = !signedIn;
  $("upload-form").hidden = !signedIn;
  $("upload-activity").hidden = !signedIn;
  if (!signedIn) {
    lastUploads = undefined;
    $("upload-activity").open = false;
    $("activity-message").textContent = "";
    $("activity-results").replaceChildren();
    $("upload-errors").replaceChildren();
    $("upload-errors").hidden = true;
  }
  $("operator-note").textContent = signedIn
    ? "Send today’s warehouse inventory. You’ll see any rows that need fixing."
    : "Sign in as the warehouse operator to send today’s file or replace a rejected upload.";
}

function showErrors(list, errors) {
  for (const problem of errors) {
    const entry = document.createElement("li");
    entry.textContent = (problem.row ? "Row " + problem.row + " · " : "") + problem.message;
    list.append(entry);
  }
}

function renderActivity(uploads) {
  const results = $("activity-results");
  results.replaceChildren();
  for (const upload of uploads) {
    const entry = document.createElement("li");
    const label = document.createElement("strong");
    const rejected = upload.status === "rejected";
    const day = upload.uploadId.slice(0, 10);
    label.textContent = (rejected ? "Rejected" : "Accepted") + " · Uploaded " + timeLabel(upload.receivedAt, day, true);
    const summary = document.createElement("p");
    summary.className = "small";
    const count = upload.rowCount.toLocaleString("en-IN");
    summary.textContent = count + (upload.rowCount === 1 ? " stock row" : " stock rows") + (rejected ? " read" : " validated");
    if (upload.validatedAt) summary.textContent += " · Checked " + timeLabel(upload.validatedAt, day, true);
    entry.append(label, summary);
    if (rejected) {
      const issues = document.createElement("ul");
      issues.className = "errors";
      showErrors(issues, upload.errors);
      entry.append(issues);
    }
    results.append(entry);
  }
  $("activity-message").textContent = uploads.length ? "" : "No files have finished validation today.";
}

async function refreshActivity() {
  const button = $("refresh-activity");
  const message = $("activity-message");
  const token = accessToken();
  if (!$("upload-activity").open || !token) return;
  const requestNumber = ++activityRequest;
  button.disabled = true;
  message.textContent = "Loading upload activity…";
  try {
    const { uploads } = await request("/api/uploads", {}, true);
    if (requestNumber !== activityRequest || accessToken() !== token) return;
    lastUploads = uploads;
    if (timeFormats) renderActivity(uploads);
  } catch (error) {
    if (requestNumber === activityRequest && accessToken() === token) message.textContent = error.message;
  } finally {
    if (requestNumber === activityRequest) button.disabled = false;
  }
}

async function showUpload(event) {
  event.preventDefault();
  if (busy) return;
  const input = $("csv-file");
  const feedback = $("upload-feedback");
  const errors = $("upload-errors");
  const file = input.files[0];
  errors.hidden = true;
  errors.replaceChildren();
  if (!file || !file.name.toLowerCase().endsWith(".csv")) {
    feedback.textContent = "Choose a CSV file to upload.";
    input.focus();
    return;
  }
  if (file.size > MAX_BYTES) {
    feedback.textContent = "The CSV exceeds 256 KiB.";
    return;
  }
  if (!currentDate) {
    feedback.textContent = "Refresh the delivery status before uploading.";
    return;
  }
  busy = true;
  $("submit-upload").disabled = true;
  input.disabled = true;
  feedback.textContent = "Uploading " + file.name + "…";
  try {
    if (uploadAttempt?.file !== file || !uploadAttempt.id.startsWith(currentDate + "_")) {
      uploadAttempt = { file, id: currentDate + "_" + crypto.randomUUID() };
    }
    let csv;
    try {
      csv = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    } catch {
      throw new Error("Save the CSV with UTF-8 encoding and try again.");
    }
    const id = uploadAttempt.id;
    await request("/api/uploads", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uploadId: id, csv }),
    }, true);
    feedback.textContent = "Checking the CSV…";
    for (let attempt = 0; attempt < 30; attempt += 1) {
      const result = await request("/api/uploads/" + id, {}, true);
      if (result.status !== "processing") {
        if (result.status === "accepted") {
          feedback.textContent = "File accepted · " + result.rowCount + (result.rowCount === 1 ? " row." : " rows.");
          input.value = "";
          uploadAttempt = null;
        } else {
          feedback.textContent = "File rejected. Correct the issues below and choose the updated CSV.";
          showErrors(errors, result.errors);
          errors.hidden = false;
        }
        await Promise.all([refresh(), refreshActivity()]);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    feedback.textContent = "Validation is still running. Select Upload CSV again to check this same attempt.";
  } catch (error) {
    feedback.textContent = error.message;
    await refresh();
  } finally {
    busy = false;
    $("submit-upload").disabled = false;
    input.disabled = false;
  }
}

function setupTimeDisplay() {
  const trigger = $("time-display");
  const menu = $("time-options");
  const options = [...menu.children];
  let selected = 0;
  let active = 0;

  function showSelection(index) {
    options[selected].setAttribute("aria-selected", "false");
    selected = index;
    options[selected].setAttribute("aria-selected", "true");
    trigger.textContent = options[selected].textContent;
  }

  function setActive(index) {
    options[active].classList.remove("is-active");
    active = index;
    options[active].classList.add("is-active");
    trigger.setAttribute("aria-activedescendant", options[active].id);
  }

  function setOpen(open) {
    menu.hidden = !open;
    trigger.setAttribute("aria-expanded", String(open));
    if (open) setActive(selected);
    else trigger.removeAttribute("aria-activedescendant");
  }

  function choose(index) {
    showSelection(index);
    setOpen(false);
    try {
      localStorage.setItem(displayPreferenceKey, options[selected].dataset.value);
    } catch {
      // The current view can still use the selection when storage is unavailable.
    }
    if (lastStatus) renderStatus(lastStatus);
  }

  try {
    if (localStorage.getItem(displayPreferenceKey) === "local") showSelection(1);
  } catch {
    // The default warehouse time is available when storage is unavailable.
  }

  trigger.addEventListener("click", () => {
    trigger.focus({ preventScroll: true });
    setOpen(menu.hidden);
  });
  trigger.addEventListener("blur", () => setOpen(false));
  document.addEventListener("pointerdown", (event) => {
    if (!menu.hidden && !trigger.parentElement.contains(event.target)) setOpen(false);
  });
  trigger.addEventListener("keydown", (event) => {
    const { key } = event;
    if (!menu.hidden && (key === "Escape" || key === "Tab")) {
      if (key === "Escape") event.preventDefault();
      setOpen(false);
      return;
    }
    if (!menu.hidden && (key === "Enter" || key === " ")) {
      event.preventDefault();
      choose(active);
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(key)) return;
    event.preventDefault();
    if (menu.hidden) setOpen(true);
    if (key === "Home") setActive(0);
    if (key === "End") setActive(options.length - 1);
    if (key === "ArrowDown") setActive(Math.min(active + 1, options.length - 1));
    if (key === "ArrowUp") setActive(Math.max(active - 1, 0));
  });
  options.forEach((option, index) => {
    option.addEventListener("pointerdown", (event) => event.preventDefault());
    option.addEventListener("pointermove", () => setActive(index));
    option.addEventListener("click", () => choose(index));
  });
}

async function start() {
  try {
    const response = await fetch("./config.json");
    if (!response.ok) throw new Error("Missing app configuration");
    config = await response.json();
  } catch {
    $("state-title").textContent = "Dashboard setup required";
    $("state-message").textContent = "Publish the application configuration and reload this page.";
    $("history-empty").textContent = "History is currently unavailable.";
    $("sign-in-feedback").textContent = "Sign-in is currently unavailable. Reload this page to retry.";
    return;
  }
  try {
    await finishSignIn(config);
  } catch (error) {
    $("upload-feedback").textContent = error.message;
  }
  updateSession();
  renderView();
  setupTimeDisplay();
  $("sign-in").addEventListener("click", () => {
    history.pushState(null, "", "#operator-sign-in");
    renderView();
  });
  const continueSignIn = $("continue-sign-in");
  const signInFeedback = $("sign-in-feedback");
  if (config.localPreview) {
    continueSignIn.textContent = "Sign in to demo";
    $("sign-in-description").textContent = "Try uploading and validating a stock CSV in this local demo.";
  }
  continueSignIn.addEventListener("click", async () => {
    continueSignIn.disabled = true;
    signInFeedback.textContent = "";
    try {
      await beginSignIn(config);
      if (config.localPreview) {
        updateSession();
        renderView();
      }
    } catch {
      signInFeedback.textContent = "Couldn’t start sign-in. Please try again.";
    } finally {
      continueSignIn.disabled = false;
    }
  });
  continueSignIn.disabled = false;
  $("sign-out").addEventListener("click", () => signOut(config));
  $("upload-form").addEventListener("submit", showUpload);
  $("upload-activity").addEventListener("toggle", refreshActivity);
  $("refresh-activity").addEventListener("click", refreshActivity);
  $("retry-status").addEventListener("click", refresh);
  await refresh();
  setInterval(() => {
    if (document.hidden) return;
    if (!busy) updateSession();
    refresh();
    refreshActivity();
  }, 15000);
}

const viewLinks = document.querySelectorAll('nav[aria-label="Dashboard"] a');
const dashboardChrome = document.querySelectorAll(".site-header, .time-display, .footer");

function renderView() {
  const signedIn = Boolean(accessToken());
  if (location.hash === "#operator-sign-in" && signedIn) history.replaceState(null, "", "#delivery");
  const current = ["#history", "#operator-sign-in"].includes(location.hash) ? location.hash : "#delivery";
  const wasOperator = !$("operator-view").hidden;
  const showingOperator = current === "#operator-sign-in";
  $("operator-view").hidden = !showingOperator;
  $("delivery-view").hidden = current !== "#delivery";
  $("history-view").hidden = current !== "#history";
  $("main").classList.toggle("operator-layout", showingOperator);
  document.title = showingOperator ? "Operator sign in · File Arrival Watch" : "File Arrival Watch";
  for (const element of dashboardChrome) element.hidden = showingOperator;
  for (const link of viewLinks) {
    if (link.hash === current) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  }
  if (showingOperator && !wasOperator) {
    window.scrollTo(0, 0);
    $("operator-sign-in-heading").focus({ preventScroll: true });
  } else if (wasOperator && !showingOperator) {
    const focusTarget = current !== "#delivery" ? $("main") : $(signedIn ? "upload-heading" : "sign-in");
    focusTarget.focus();
  }
}

for (const link of [...viewLinks, $("back-to-dashboard")]) {
  link.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (location.hash !== link.hash) history.pushState(null, "", link.hash);
    renderView();
  });
}
window.addEventListener("hashchange", renderView);
document.querySelector(".skip").addEventListener("click", (event) => {
  event.preventDefault();
  $("main").focus();
});
renderView();
start();
