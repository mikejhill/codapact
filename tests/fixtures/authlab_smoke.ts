// Every TypeScript auth path against AuthLab: node authlab_smoke.ts <index.ts> <baseUrl> <signingSecret>
import { createHmac } from "node:crypto";

const { Client, AuthError, ApiError, clientCredentials } = await import(process.argv[2]);
const baseUrl = process.argv[3];
const [, , , , secret] = process.argv;

const signer = {
  authenticate(request: { method: string; url: string; headers: Record<string, string>; body?: string }) {
    const path = new URL(request.url).pathname;
    const digest = createHmac("sha256", secret).update(`${request.method}\n${path}\n${request.body ?? ""}`).digest("hex");
    return { ...request, headers: { ...request.headers, "X-Key-Id": "key-1", "X-Signature": digest } };
  },
};
let calls = 0;
const sso = clientCredentials({ tokenUrl: `${baseUrl}/oauth/token`, clientId: "authlab-service", clientSecret: "s3cret", scopes: ["read"] });
const client = new Client({
  baseUrl,
  maxRetries: 0,
  auth: {
    header_key: () => {
      calls++;
      return "key-alice";
    },
    query_key: "key-alice",
    cookie_key: "key-alice",
    basic: { username: "alice", password: "wonderland" },
    bearer: async () => "static-bearer-token",
    sso,
    signed: signer,
  },
});
const results = {
  header: (await client.lab.headerKey()).scheme,
  query: (await client.lab.queryKey()).scheme,
  cookie: (await client.lab.cookieKey()).scheme,
  basic: (await client.lab.basic()).scheme,
  bearer: (await client.lab.bearer()).scheme,
  sso: (await client.lab.sso()).principal,
  ssoAgain: (await client.lab.sso()).principal,
  afterRevoke: await fetch(`${baseUrl}/admin/revoke`, { method: "POST" }).then(async () => (await client.lab.sso()).principal),
  signed: (await client.lab.signed({ message: "hi" })).principal,
  providerCalls: calls,
  missing: "",
  custom: "",
  rejected: 0,
};
try {
  await new Client({ baseUrl, maxRetries: 0 }).lab.headerKey();
} catch (error) {
  if (error instanceof AuthError) results.missing = "AuthError";
}
try {
  await new Client({ baseUrl, maxRetries: 0, auth: { signed: "plain" } }).lab.signed({ message: "x" });
} catch (error) {
  if (error instanceof AuthError) results.custom = String(error.message);
}
try {
  await new Client({ baseUrl, maxRetries: 0, auth: { header_key: "wrong" } }).lab.headerKey();
} catch (error) {
  if (error instanceof ApiError) results.rejected = error.status;
}
console.log(JSON.stringify(results));
