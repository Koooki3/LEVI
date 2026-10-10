import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import { NextRequest } from "next/server";
import {
  allowedHosts,
  backendProxy,
  forwardedCookie,
  hostAllowed,
  parseHost,
  writeAllowed,
} from "@/utils/backendProxy";
import { forgetUiToken } from "@/utils/serviceToken";

const SETTINGS = [
  "PORT",
  "LEVI_FRONTEND_URL",
  "LEVI_UI_ALLOWED_HOSTS",
  "SPACE_HOST",
  "LEVI_CORE_DIR",
  "LEVI_UI_TOKEN",
  "LEVI_BACKEND_URL",
] as const;
const saved: Record<string, string | undefined> = {};
const realFetch = globalThis.fetch;
let forwarded: { url: string; init: RequestInit }[] = [];

beforeEach(() => {
  for (const key of SETTINGS) {
    saved[key] = process.env[key];
    delete process.env[key];
  }
  process.env.LEVI_UI_TOKEN = "test-ui-token";
  process.env.LEVI_BACKEND_URL = "http://127.0.0.1:65530";
  forgetUiToken();
  forwarded = [];
  globalThis.fetch = mock((url: string | URL | Request, init?: RequestInit) => {
    forwarded.push({ url: String(url), init: init ?? {} });
    return Promise.resolve(Response.json({ ok: true }));
  }) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  for (const key of SETTINGS) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  forgetUiToken();
});

function call(
  method: string,
  headers: Record<string, string>,
  path: string[] = ["catalog"],
  annotation = false,
) {
  const request = new NextRequest(
    `http://127.0.0.1:7860/api/levi/${path.join("/")}`,
    {
      method,
      headers,
      body: ["GET", "HEAD"].includes(method) ? undefined : "{}",
    },
  );
  return backendProxy(
    request,
    { params: Promise.resolve({ path }) },
    annotation,
  );
}

/** What a browser sends with a write from the LEVI page itself. */
const PAGE_WRITE = {
  host: "127.0.0.1:7860",
  origin: "http://127.0.0.1:7860",
  "sec-fetch-site": "same-origin",
  "content-type": "application/json",
};

function sentHeaders(): Headers {
  expect(forwarded).toHaveLength(1);
  return new Headers(forwarded[0].init.headers);
}

describe("parseHost", () => {
  test("canonicalises names, IPv6 literals and ports", () => {
    expect(parseHost("LocalHost:7860")).toEqual({
      name: "localhost",
      port: "7860",
    });
    expect(parseHost("[0:0:0:0:0:0:0:1]:7860")).toEqual({
      name: "[::1]",
      port: "7860",
    });
    expect(parseHost("127.0.0.1:07860")).toEqual({
      name: "127.0.0.1",
      port: "7860",
    });
    expect(parseHost("127.0.0.1")).toEqual({ name: "127.0.0.1", port: "" });
  });

  test("refuses what is not a bare host name", () => {
    for (const value of [
      "",
      "evil.example@127.0.0.1:7860",
      "127.0.0.1:7860/path",
      "localhost.:7860",
      "127.0.0.1:0",
      "127.0.0.1:70000",
      "127.0.0.1 :7860",
      "[::1%25eth0]:7860",
      "exämple.com",
    ])
      expect(parseHost(value)).toBeNull();
    expect(parseHost(null)).toBeNull();
  });
});

describe("allowedHosts / hostAllowed", () => {
  test("loopback on 7860 by default", () => {
    const allowed = allowedHosts({});
    for (const host of [
      "127.0.0.1:7860",
      "localhost:7860",
      "LOCALHOST:7860",
      "[::1]:7860",
      "[0:0:0:0:0:0:0:1]:7860",
    ])
      expect(hostAllowed(host, allowed)).toBe(true);
    for (const host of [
      "evil.example:7860",
      "127.0.0.1.evil.example:7860",
      "0.0.0.0:7860",
      "192.168.1.20:7860",
      "127.0.0.1:7861",
      "localhost:9000",
      "127.0.0.1",
      "",
    ])
      expect(hostAllowed(host, allowed)).toBe(false);
    expect(hostAllowed(null, allowed)).toBe(false);
  });

  test("follows the page's own port and the launcher's address", () => {
    const allowed = allowedHosts({
      PORT: "7870",
      LEVI_FRONTEND_URL: "http://192.168.1.20:7870",
    });
    expect(hostAllowed("localhost:7870", allowed)).toBe(true);
    expect(hostAllowed("192.168.1.20:7870", allowed)).toBe(true);
    expect(hostAllowed("localhost:7860", allowed)).toBe(false);
    expect(hostAllowed("192.168.1.20:7860", allowed)).toBe(false);
  });

  test("LEVI_UI_ALLOWED_HOSTS adds names, with or without a port", () => {
    const allowed = allowedHosts({
      LEVI_UI_ALLOWED_HOSTS: "levi.lab, localhost:9000  bad@host,,",
    });
    expect(hostAllowed("levi.lab", allowed)).toBe(true);
    expect(hostAllowed("LEVI.LAB:8443", allowed)).toBe(true);
    expect(hostAllowed("localhost:9000", allowed)).toBe(true);
    expect(hostAllowed("localhost:9001", allowed)).toBe(false);
    expect(hostAllowed("host", allowed)).toBe(false);
  });

  test("a Hugging Face Space answers to its own host", () => {
    const allowed = allowedHosts({ SPACE_HOST: "owner-levi.hf.space" });
    expect(hostAllowed("owner-levi.hf.space", allowed)).toBe(true);
    expect(hostAllowed("other.hf.space", allowed)).toBe(false);
  });
});

describe("writeAllowed", () => {
  const allowed = allowedHosts({});
  const check = (headers: Record<string, string>) =>
    writeAllowed(new Headers(headers), allowed);

  test("the page's own writes pass", () => {
    expect(
      check({
        origin: "http://127.0.0.1:7860",
        "sec-fetch-site": "same-origin",
      }),
    ).toBe(true);
    // Older browsers without Fetch Metadata still send Origin on a write.
    expect(check({ origin: "http://localhost:7860" })).toBe(true);
    expect(check({ origin: "http://[::1]:7860" })).toBe(true);
    // A browser that sends Fetch Metadata but omits Origin.
    expect(check({ "sec-fetch-site": "same-origin" })).toBe(true);
  });

  test("a script with neither header is refused", () => {
    expect(check({})).toBe(false);
  });

  test("other fetch sites are refused, whatever the Origin", () => {
    for (const site of ["cross-site", "same-site", "none", "bogus"])
      expect(
        check({ origin: "http://127.0.0.1:7860", "sec-fetch-site": site }),
      ).toBe(false);
  });

  test("a foreign or opaque Origin is refused", () => {
    for (const origin of [
      "http://evil.example:7860",
      "http://127.0.0.1.evil.example:7860",
      "http://localhost:3000",
      "http://127.0.0.1:7880",
      "null",
      "file://",
      "not a url",
    ]) {
      expect(check({ origin })).toBe(false);
      expect(check({ origin, "sec-fetch-site": "same-origin" })).toBe(false);
    }
  });
});

describe("forwardedCookie", () => {
  test("keeps only the Hugging Face session", () => {
    expect(forwardedCookie("a=1; hf_access_token=abc; b=2")).toBe(
      "hf_access_token=abc",
    );
    expect(forwardedCookie("x_hf_access_token=abc; session=1")).toBeNull();
    expect(forwardedCookie(null)).toBeNull();
  });
});

describe("backendProxy", () => {
  test("a read from the page is forwarded with the UI token", async () => {
    const response = await call("GET", { host: "localhost:7860" });
    expect(response.status).toBe(200);
    expect(forwarded[0].url).toBe("http://127.0.0.1:65530/api/levi/catalog");
    expect(sentHeaders().get("x-levi-ui-token")).toBe("test-ui-token");
  });

  test("a forged Host is answered 421, reads included, and nothing is forwarded", async () => {
    for (const host of [
      "evil.example:7860",
      "127.0.0.1.evil.example:7860",
      "0.0.0.0:7860",
    ])
      for (const method of ["GET", "HEAD", "POST", "DELETE"]) {
        const response = await call(method, { ...PAGE_WRITE, host });
        expect(response.status).toBe(421);
      }
    expect(forwarded).toHaveLength(0);
  });

  test("a DNS-rebound page writing to its own name is refused", async () => {
    const response = await call("POST", {
      host: "evil.example:7860",
      origin: "http://evil.example:7860",
      "sec-fetch-site": "same-origin",
    });
    expect(response.status).toBe(421);
    expect(forwarded).toHaveLength(0);
  });

  test("a write without Origin or Fetch Metadata is refused", async () => {
    for (const method of ["POST", "PUT", "DELETE"]) {
      const response = await call(method, { host: "127.0.0.1:7860" });
      expect(response.status).toBe(403);
    }
    expect(forwarded).toHaveLength(0);
  });

  test("cross-site, same-site and user-initiated writes are refused", async () => {
    for (const site of ["cross-site", "same-site", "none"]) {
      const response = await call("POST", {
        ...PAGE_WRITE,
        "sec-fetch-site": site,
      });
      expect(response.status).toBe(403);
    }
    const foreign = await call("POST", {
      host: "127.0.0.1:7860",
      origin: "http://localhost:8888",
    });
    expect(foreign.status).toBe(403);
    expect(forwarded).toHaveLength(0);
  });

  test("the page's own writes reach the core, on every loopback name", async () => {
    for (const [host, origin] of [
      ["127.0.0.1:7860", "http://127.0.0.1:7860"],
      ["localhost:7860", "http://localhost:7860"],
      ["[::1]:7860", "http://[::1]:7860"],
    ]) {
      const response = await call("POST", { ...PAGE_WRITE, host, origin }, [
        "jobs",
        "plan",
      ]);
      expect(response.status).toBe(200);
    }
    const annotation = await call(
      "DELETE",
      PAGE_WRITE,
      ["datasets", "x"],
      true,
    );
    expect(annotation.status).toBe(200);
    expect(forwarded.map((f) => f.init.method)).toEqual([
      "POST",
      "POST",
      "POST",
      "DELETE",
    ]);
    expect(forwarded[3].url).toBe(
      "http://127.0.0.1:65530/annotations/api/datasets/x",
    );
  });

  test("only the proxy's own token reaches the core", async () => {
    const response = await call("POST", {
      ...PAGE_WRITE,
      authorization: "Bearer someone-elses",
      cookie: "theme=dark; hf_access_token=hub-session; other=1",
      "x-forwarded-for": "10.0.0.1",
      "x-forwarded-host": "127.0.0.1:7861",
      "x-levi-ui-token": "forged",
      "x-levi-annotation-revision": "r1",
    });
    expect(response.status).toBe(200);
    const sent = sentHeaders();
    expect(sent.get("authorization")).toBeNull();
    expect(sent.get("cookie")).toBe("hf_access_token=hub-session");
    expect(sent.get("x-forwarded-for")).toBeNull();
    expect(sent.get("x-forwarded-host")).toBeNull();
    expect(sent.get("origin")).toBeNull();
    expect(sent.get("x-levi-ui-token")).toBe("test-ui-token");
    expect(sent.get("x-levi-annotation-revision")).toBe("r1");
    expect(sent.get("content-type")).toBe("application/json");
  });

  test("a frontend fetch through ssh -L on another local port needs the setting", async () => {
    const headers = {
      host: "localhost:9000",
      origin: "http://localhost:9000",
      "sec-fetch-site": "same-origin",
    };
    expect((await call("POST", headers)).status).toBe(421);
    process.env.LEVI_UI_ALLOWED_HOSTS = "localhost:9000";
    expect((await call("POST", headers)).status).toBe(200);
  });

  test("path traversal is still refused after the checks", async () => {
    const response = await call("GET", { host: "127.0.0.1:7860" }, [
      "..",
      "secret",
    ]);
    expect(response.status).toBe(403);
    expect(forwarded).toHaveLength(0);
  });
});
