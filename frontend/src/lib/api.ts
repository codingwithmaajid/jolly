import type {
  CallResponse,
  CallStatus,
  Case,
  CaseAnswer,
  CaseBrief,
  CaseDocument,
  DailyBrief,
  DocumentUpload,
  ExecutionEvent,
  Health,
  HearingEvent,
  HearingUpdate,
  UpdateEdits,
  UpdateMetrics,
  UpdateStatus,
  SearchHit,
  Task,
  UploadResult,
} from "./types";

function approvalHeaders(update: Pick<HearingUpdate, "approval_token">): Record<string, string> {
  return update.approval_token ? { "X-Approval-Token": update.approval_token } : {};
}

export const API_URL: string = (
  (import.meta.env.VITE_API_URL as string | undefined) || "http://localhost:8000"
).replace(/\/+$/, "");

/** `network` = backend unreachable (offline, asleep, CORS); `http` = backend answered with an error. */
export class ApiError extends Error {
  kind: "network" | "http" | "timeout";
  status?: number;
  constructor(kind: ApiError["kind"], message: string, status?: number) {
    super(message);
    this.kind = kind;
    this.status = status;
  }
}

function messageFrom(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const err = b.error as Record<string, unknown> | undefined;
    if (err && typeof err === "object") {
      const msg = err.message;
      if (typeof msg === "string") return msg;
      if (msg) return JSON.stringify(msg);
    }
    if (typeof b.detail === "string") return b.detail;
    if (typeof b.message === "string") return b.message;
  }
  return fallback;
}

async function request<T>(
  path: string,
  init: RequestInit & { timeoutMs?: number } = {},
): Promise<T> {
  const { timeoutMs = 20000, ...rest } = init;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...rest,
      signal: ctrl.signal,
      headers: { Accept: "application/json", ...(rest.body ? { "Content-Type": "application/json" } : {}), ...rest.headers },
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") {
      throw new ApiError("timeout", `The backend did not answer within ${Math.round(timeoutMs / 1000)}s.`);
    }
    throw new ApiError("network", `Can't reach the backend at ${API_URL}.`);
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    throw new ApiError("http", messageFrom(body, `${res.status} ${res.statusText}`), res.status);
  }
  return body as T;
}

const enc = encodeURIComponent;
const post = (body: unknown) => ({ method: "POST", body: JSON.stringify(body) });

export const api = {
  health: (timeoutMs = 10000) => request<Health>("/health", { timeoutMs }),
  dailyBrief: (date?: string) => request<DailyBrief>(`/briefs/daily${date ? `?date=${enc(date)}` : ""}`),
  cases: () => request<Case[]>("/cases"),
  case: (id: string) => request<Case>(`/cases/${enc(id)}`),
  brief: (id: string) => request<CaseBrief>(`/cases/${enc(id)}/brief`),
  timeline: (id: string) => request<HearingEvent[]>(`/cases/${enc(id)}/timeline`),
  tasks: (id: string) => request<Task[]>(`/cases/${enc(id)}/tasks`),
  documents: (id: string) => request<CaseDocument[]>(`/cases/${enc(id)}/documents`),
  ask: (id: string, question: string) =>
    request<CaseAnswer>(`/cases/${enc(id)}/ask`, { ...post({ question }), timeoutMs: 60000 }),
  search: (id: string, q: string, k = 5) =>
    request<SearchHit[]>(`/cases/${enc(id)}/search?q=${enc(q)}&k=${k}`),
  upload: (id: string, doc: DocumentUpload) =>
    request<UploadResult>(`/cases/${enc(id)}/documents`, { ...post(doc), timeoutMs: 60000 }),
  call: (phone_number: string, user_data?: Record<string, unknown>) =>
    request<CallResponse>("/call", { ...post({ phone_number, ...(user_data ? { user_data } : {}) }), timeoutMs: 40000 }),
  callStatus: (callId: string) => request<CallStatus>(`/call/${enc(callId)}`),
  recentExecutions: () => request<ExecutionEvent[]>("/bolna/executions/recent"),
  dictate: (id: string, text: string, hearing_date?: string) =>
    request<HearingUpdate>(`/cases/${enc(id)}/dictations`, {
      ...post({ text, ...(hearing_date ? { hearing_date } : {}) }),
      timeoutMs: 60000,
    }),
  updates: (status?: UpdateStatus) => request<HearingUpdate[]>(`/updates${status ? `?status=${status}` : ""}`),
  caseUpdates: (id: string) => request<HearingUpdate[]>(`/cases/${enc(id)}/updates`),
  /** Approve/reject need the signed, single-use token the backend puts on each pending update. */
  approveUpdate: (update: Pick<HearingUpdate, "id" | "approval_token">, edits?: UpdateEdits) =>
    request<HearingUpdate>(`/updates/${update.id}/approve`, {
      ...(edits ? post(edits) : { method: "POST" }),
      headers: approvalHeaders(update),
    }),
  rejectUpdate: (update: Pick<HearingUpdate, "id" | "approval_token">) =>
    request<HearingUpdate>(`/updates/${update.id}/reject`, { method: "POST", headers: approvalHeaders(update) }),
  updateMetrics: () => request<UpdateMetrics>("/updates/metrics"),
};
