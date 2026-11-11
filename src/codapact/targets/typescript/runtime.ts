// Codapact TypeScript runtime. Generated alongside every client. Do not edit.
// Uses only the standard fetch API so it runs in browsers, Node 18+, Deno, and Bun.

export type Credential = string | { username: string; password: string };

export interface ClientOptions {
  /** Base URL; defaults to the contract's first server. */
  baseUrl?: string;
  /** Credentials keyed by security scheme name. */
  auth?: Record<string, Credential>;
  /** Replace the fetch implementation (testing, proxies, instrumentation). */
  fetch?: typeof fetch;
  /** Extra headers sent with every request. */
  headers?: Record<string, string>;
  /** Retries for idempotent operations on 408/429/5xx (default 2). */
  maxRetries?: number;
}

export interface SchemeSpec {
  kind: "bearer" | "basic" | "api_key" | "oauth2";
  location?: "header" | "query" | "cookie";
  wire?: string;
}

export interface Paging {
  style: "cursor" | "offset" | "page";
  param: string;
  items: string[];
  next?: string[];
  start?: number;
}

interface BaseOperation {
  name: string;
  auth: string[][];
  idempotent: boolean;
  paging?: Paging;
}

export interface HttpOperation extends BaseOperation {
  kind: "http";
  method: string;
  path: string;
  params: [param: string, location: string, wire: string, explode: boolean][];
  responses: [status: string, outcome: string, media: string | null][];
  requestMediaType: string | null;
}

export interface GraphQLOperation extends BaseOperation {
  kind: "graphql";
  document: string;
  operationName: string;
  variables: [param: string, variable: string][];
  unwrap: string | null;
}

export type OperationSpec = HttpOperation | GraphQLOperation;

export class ApiError extends Error {
  readonly operation: string;
  readonly status: number;
  readonly error: string | null;
  readonly payload: unknown;

  constructor(operation: string, status: number, error: string | null, payload: unknown) {
    super(`${operation} failed with HTTP ${status}${error ? ` (${error})` : ""}`);
    this.operation = operation;
    this.status = status;
    this.error = error;
    this.payload = payload;
  }
}

export class GraphQLResponseError extends Error {
  readonly operation: string;
  readonly errors: unknown[];
  readonly data: unknown;

  constructor(operation: string, errors: unknown[], data: unknown) {
    super(`${operation} returned GraphQL errors`);
    this.operation = operation;
    this.errors = errors;
    this.data = data;
  }
}

export class AuthError extends Error {}

const RETRY_STATUSES = new Set([408, 429, 500, 502, 503, 504]);

export class Runtime {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly options: ClientOptions;
  private readonly schemes: Record<string, SchemeSpec>;

  constructor(options: ClientOptions, schemes: Record<string, SchemeSpec>, defaultBaseUrl: string | null) {
    this.options = options;
    this.schemes = schemes;
    const url = options.baseUrl ?? defaultBaseUrl;
    if (!url) throw new Error("baseUrl is required: the contract declares no server");
    this.baseUrl = url.replace(/\/+$/, "");
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  async call(op: OperationSpec, args: Record<string, unknown>): Promise<unknown> {
    const auth = this.resolveAuth(op);
    return op.kind === "http" ? this.callHttp(op, args, auth) : this.callGraphQL(op, args, auth);
  }

  async *paginate(op: OperationSpec, args: Record<string, unknown>, maxItems?: number): AsyncGenerator<unknown> {
    const paging = op.paging;
    if (!paging) throw new Error(`${op.name} is not paginated`);
    const current: Record<string, unknown> = { ...args };
    if (paging.style === "page" && current[paging.param] == null) current[paging.param] = paging.start ?? 1;
    let produced = 0;
    for (let page = 0; page < 10_000; page++) {
      const result = await this.call(op, current);
      const items = (dig(result, paging.items) as unknown[] | undefined) ?? [];
      for (const item of items) {
        if (maxItems !== undefined && produced >= maxItems) return;
        produced++;
        yield item;
      }
      if (items.length === 0) return;
      if (paging.style === "cursor") {
        const cursor = dig(result, paging.next ?? []);
        if (!cursor) return;
        current[paging.param] = cursor;
      } else if (paging.style === "page") {
        current[paging.param] = Number(current[paging.param]) + 1;
      } else {
        current[paging.param] = Number(current[paging.param] ?? 0) + items.length;
      }
    }
  }

  private resolveAuth(op: OperationSpec): { headers: Record<string, string>; query: [string, string][]; cookies: string[] } {
    for (const alternative of op.auth) {
      const applied = { headers: {} as Record<string, string>, query: [] as [string, string][], cookies: [] as string[] };
      let complete = true;
      for (const name of alternative) {
        const credential = this.options.auth?.[name];
        const scheme = this.schemes[name];
        if (credential === undefined || scheme === undefined) {
          complete = false;
          break;
        }
        if (scheme.kind === "basic") {
          if (typeof credential === "string") throw new AuthError(`scheme ${name} needs username and password`);
          applied.headers["Authorization"] = "Basic " + btoa(`${credential.username}:${credential.password}`);
        } else if (typeof credential !== "string") {
          throw new AuthError(`scheme ${name} needs a string credential`);
        } else if (scheme.kind === "api_key") {
          const wire = scheme.wire ?? name;
          if (scheme.location === "query") applied.query.push([wire, credential]);
          else if (scheme.location === "cookie") applied.cookies.push(`${wire}=${credential}`);
          else applied.headers[wire] = credential;
        } else {
          applied.headers["Authorization"] = `Bearer ${credential}`;
        }
      }
      if (complete) return applied;
    }
    throw new AuthError(`no credentials configured for ${op.name}`);
  }

  private async send(op: OperationSpec, url: string, init: RequestInit): Promise<Response> {
    const retries = op.idempotent ? (this.options.maxRetries ?? 2) : 0;
    for (let attempt = 0; ; attempt++) {
      const response = await this.fetchImpl(url, { ...init, redirect: "error" });
      if (!RETRY_STATUSES.has(response.status) || attempt >= retries) return response;
      await new Promise((resolve) => setTimeout(resolve, Math.min(8000, 500 * 2 ** attempt)));
    }
  }

  private async callHttp(
    op: HttpOperation,
    args: Record<string, unknown>,
    auth: { headers: Record<string, string>; query: [string, string][]; cookies: string[] },
  ): Promise<unknown> {
    let path = op.path;
    const query = new URLSearchParams();
    const headers: Record<string, string> = { Accept: "application/json", ...this.options.headers, ...auth.headers };
    const cookies = [...auth.cookies];
    let body: string | undefined;
    for (const [param, location, wire, explode] of op.params) {
      const value = args[param];
      if (value === undefined) continue;
      if (location === "path") path = path.replace(`{${wire}}`, encodeURIComponent(text(value)));
      else if (location === "query" && value !== null) {
        if (Array.isArray(value)) {
          if (explode) value.forEach((v) => query.append(wire, text(v)));
          else query.append(wire, value.map(text).join(","));
        } else query.append(wire, text(value));
      } else if (location === "header" && value !== null) headers[wire] = text(value);
      else if (location === "cookie" && value !== null) cookies.push(`${wire}=${text(value)}`);
      else if (location === "body") {
        body = JSON.stringify(value);
        headers["Content-Type"] = op.requestMediaType ?? "application/json";
      }
    }
    for (const [key, value] of auth.query) query.append(key, value);
    if (cookies.length) headers["Cookie"] = cookies.join("; ");
    const qs = query.toString();
    const response = await this.send(op, this.baseUrl + path + (qs ? `?${qs}` : ""), { method: op.method, headers, body });
    const bound = match(op.responses, response.status);
    const ok = response.status >= 200 && response.status < 300;
    const payload = await decode(response, bound?.[2] ?? "application/json");
    if (ok && (!bound || bound[1] === "result")) return payload;
    throw new ApiError(op.name, response.status, bound && bound[1] !== "result" ? bound[1] : null, payload);
  }

  private async callGraphQL(
    op: GraphQLOperation,
    args: Record<string, unknown>,
    auth: { headers: Record<string, string>; query: [string, string][]; cookies: string[] },
  ): Promise<unknown> {
    const variables: Record<string, unknown> = {};
    for (const [param, variable] of op.variables) if (args[param] !== undefined) variables[variable] = args[param];
    const headers: Record<string, string> = {
      Accept: "application/graphql-response+json, application/json",
      "Content-Type": "application/json",
      ...this.options.headers,
      ...auth.headers,
    };
    if (auth.cookies.length) headers["Cookie"] = auth.cookies.join("; ");
    const qs = new URLSearchParams(auth.query).toString();
    const response = await this.send(op, this.baseUrl + (qs ? `?${qs}` : ""), {
      method: "POST",
      headers,
      body: JSON.stringify({ query: op.document, operationName: op.operationName, variables }),
    });
    const document = (await decode(response, "application/json")) as { data?: Record<string, unknown> | null; errors?: unknown[] };
    if (document?.errors?.length) throw new GraphQLResponseError(op.name, document.errors, document.data);
    if (!response.ok || !document?.data) throw new ApiError(op.name, response.status, null, document);
    return op.unwrap === null ? document.data : document.data[op.unwrap];
  }
}

function text(value: unknown): string {
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  return String(value);
}

function dig(value: unknown, path: string[]): unknown {
  let current = value;
  for (const part of path) {
    if (typeof current !== "object" || current === null) return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function match(responses: HttpOperation["responses"], status: number) {
  const candidates = [String(status), `${Math.floor(status / 100)}XX`, `${Math.floor(status / 100)}xx`];
  for (const candidate of candidates) {
    const found = responses.find((r) => r[0] === candidate);
    if (found) return found;
  }
  return responses.find((r) => r[0] === "default");
}

async function decode(response: Response, media: string | null): Promise<unknown> {
  const raw = await response.text();
  if (!raw) return undefined;
  if (media && media.includes("json")) {
    try {
      return JSON.parse(raw);
    } catch {
      return raw;
    }
  }
  return raw;
}
