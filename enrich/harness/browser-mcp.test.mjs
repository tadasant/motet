// The navigation lock, tested without a browser (`node --test`, run by bin/ci).
//
// This is the piece of the harness with the most security riding on it and the least
// reachable through a real Playwright run: proving "the browser refused to go to
// attacker.example" end to end means a browser, a network and a hostile page. The rule
// itself is a pure function of a request, so it is tested as one, against the four shapes
// that matter — allowed host, subdomain, off-site, and a redirect continuation.

import assert from "node:assert/strict";
import { test } from "node:test";

import { hostAllowed, navigationAllowed } from "./browser-mcp.mjs";

const ALLOWED = ["example.com", "url3396.example.com", "links.sender.test"];

const request = ({
  url = "https://example.com/articles/x",
  navigation = true,
  type = navigation ? "document" : "image",
  parentFrame = null,
  redirectedFrom = null,
} = {}) => ({
  url: () => url,
  resourceType: () => type,
  isNavigationRequest: () => navigation,
  frame: () => ({ parentFrame: () => parentFrame }),
  redirectedFrom: () => redirectedFrom,
});

test("a host is itself or a subdomain of an allowed one", () => {
  assert.equal(hostAllowed("example.com", ALLOWED), true);
  assert.equal(hostAllowed("EXAMPLE.COM.", ALLOWED), true);
  assert.equal(hostAllowed("url3396.example.com", ALLOWED), true);
  assert.equal(hostAllowed("deep.sub.example.com", ALLOWED), true);
});

test("a host that merely ends in the same letters is not a subdomain", () => {
  assert.equal(hostAllowed("notexample.com", ALLOWED), false);
  assert.equal(hostAllowed("example.com.attacker.test", ALLOWED), false);
  assert.equal(hostAllowed("", ALLOWED), false);
  assert.equal(hostAllowed(undefined, ALLOWED), false);
});

test("a top-level navigation off-site is refused", () => {
  const off = request({ url: "https://attacker.test/steal" });
  assert.equal(navigationAllowed(off, ALLOWED), false);
});

test("a top-level navigation to the site, or a subdomain of it, is allowed", () => {
  assert.equal(navigationAllowed(request(), ALLOWED), true);
  assert.equal(
    navigationAllowed(request({ url: "https://url3396.example.com/ls/click?upn=abc" }), ALLOWED),
    true,
  );
});

test("a passive sub-resource is not filtered, which is a stated limit", () => {
  // A publisher's page loads a dozen CDN hosts; blocking those breaks the page outright.
  // The cost is that an `<img>` beacon still gets out — see `navigationAllowed`.
  const image = request({ url: "https://cdn.elsewhere.test/hero.jpg", navigation: false });
  assert.equal(navigationAllowed(image, ALLOWED), true);
  const script = request({ url: "https://cdn.elsewhere.test/a.js", navigation: false, type: "script" });
  assert.equal(navigationAllowed(script, ALLOWED), true);
});

test("an off-site fetch or XHR is refused — that is the exfiltration shape", () => {
  for (const type of ["xhr", "fetch", "websocket", "eventsource"]) {
    const call = request({ url: "https://attacker.test/?d=secret", navigation: false, type });
    assert.equal(navigationAllowed(call, ALLOWED), false, type);
  }
});

test("an on-site fetch is allowed, because the page needs its own API", () => {
  const call = request({ url: "https://example.com/api/article", navigation: false, type: "fetch" });
  assert.equal(navigationAllowed(call, ALLOWED), true);
});

test("an injected off-site sub-frame is refused", () => {
  const iframe = request({
    url: "https://ads.elsewhere.test/frame",
    parentFrame: { parentFrame: () => null },
  });
  assert.equal(navigationAllowed(iframe, ALLOWED), false);
});

test("an empty allowlist refuses everything rather than disabling the lock", () => {
  // Fail-open here would be fail-open in the one place this file calls itself a control.
  assert.equal(navigationAllowed(request(), []), false);
  assert.equal(navigationAllowed(request({ navigation: false, type: "fetch" }), []), false);
});

test("a redirect continuation is followed, because that is what a tracking link is", () => {
  const redirected = request({
    url: "https://tracking.elsewhere.test/hop",
    redirectedFrom: request({ url: "https://links.sender.test/ls/click" }),
  });
  assert.equal(navigationAllowed(redirected, ALLOWED), true);
});

test("a URL that will not parse is refused rather than allowed", () => {
  assert.equal(navigationAllowed(request({ url: "not a url" }), ALLOWED), false);
});
