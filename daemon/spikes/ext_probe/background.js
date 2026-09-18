// Spike4 / Issue #4 finding #2 verification extension.
//
// Goal: test whether chrome.debugger, from an installed extension, can read an
// already-authenticated tab's content in a NORMAL Chrome window that was never
// started with --remote-debugging-port. This is the one thing CDP attach (path a)
// structurally cannot do (it requires a cold start with a debug port), so it is
// the only remaining candidate for literally reusing "the browser window the user
// already has open and logged into" per FR09's original wording.
//
// No external CDP driver is used here on purpose. The extension logs itself in
// via chrome.scripting.executeScript (an ordinary extension capability, not
// chrome.debugger) to reach an authenticated state, then — separately — uses
// chrome.debugger to attach to that same tab and read protected content. That
// second step is the thing under test.
//
// Results are POSTed to a localhost report server (started by the harness) and
// the extension does not require any manual click: it runs from
// chrome.runtime.onInstalled, which fires once when the extension loads into a
// fresh profile.

const REPORT_URL = "http://127.0.0.1:8765/report";
const LOGIN_URL = "https://the-internet.herokuapp.com/login";
const SECURE_URL = "https://the-internet.herokuapp.com/secure";

async function report(obj) {
  try {
    await fetch(REPORT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(obj),
    });
  } catch (e) {
    // best effort; also log so it's visible in chrome://extensions service worker console
    console.error("report failed", e);
  }
}

function waitForTabComplete(tabId) {
  return new Promise((resolve) => {
    function listener(id, info) {
      if (id === tabId && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function run() {
  const result = {
    step: "extension_debugger",
    login_ok: false,
    debugger_attach_ok: false,
    debugger_read_ok: false,
    secure_h2_text: null,
    error: null,
  };

  try {
    const tab = await chrome.tabs.create({ url: LOGIN_URL, active: true });
    await waitForTabComplete(tab.id);

    // Log in using an ordinary content-script injection (no chrome.debugger
    // involved) -- this stands in for "the user is already logged in";
    // the point under test is the read step below, not how login happened.
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        document.querySelector("#username").value = "tomsmith";
        document.querySelector("#password").value = "SuperSecretPassword!";
        document.querySelector("button[type=submit]").click();
      },
    });
    await sleep(1500);

    const flashText = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => document.querySelector("#flash")?.innerText || null,
    });
    result.login_ok = !!(flashText[0]?.result && flashText[0].result.includes("logged"));

    // Navigate the SAME tab (same authenticated session, no restart, no debug
    // port) to the protected page.
    await chrome.tabs.update(tab.id, { url: SECURE_URL });
    await waitForTabComplete(tab.id);

    // Now the part under test: attach chrome.debugger to this normal tab
    // (the window/profile was never launched with --remote-debugging-port)
    // and read the page via the Runtime domain, exactly like a CDP client
    // would, but through the extension's debugger binding instead of a
    // TCP debug port.
    await new Promise((resolve, reject) => {
      chrome.debugger.attach({ tabId: tab.id }, "1.3", () => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else {
          resolve();
        }
      });
    });
    result.debugger_attach_ok = true;

    const evalResult = await new Promise((resolve, reject) => {
      chrome.debugger.sendCommand(
        { tabId: tab.id },
        "Runtime.evaluate",
        { expression: "document.querySelector('h2') && document.querySelector('h2').innerText" },
        (res) => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else {
            resolve(res);
          }
        }
      );
    });
    result.secure_h2_text = evalResult && evalResult.result ? evalResult.result.value : null;
    result.debugger_read_ok = result.secure_h2_text === "Secure Area";

    await new Promise((resolve) => chrome.debugger.detach({ tabId: tab.id }, resolve));
  } catch (e) {
    result.error = String(e && e.message ? e.message : e);
  }

  await report(result);
}

chrome.runtime.onInstalled.addListener(() => {
  run();
});
