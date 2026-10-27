import { useState } from "react";
import { Link } from "react-router-dom";
import { Check, ExternalLink, Loader2, Mic, PhoneCall, Plus, Trash2, X } from "lucide-react";
import { BackendGate, Empty, ErrorNote, LoadingRows, PageHeader, Panel } from "@/components/jolly/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api";
import { useApi } from "@/lib/backend";
import { fmtDate } from "@/lib/format";
import type { DraftTask, HearingUpdate } from "@/lib/types";

const field =
  "w-full rounded-lg border border-input bg-ivory p-2.5 text-sm leading-relaxed text-navy-deep outline-none focus-visible:ring-3 focus-visible:ring-ring/40";

const SAMPLE =
  "Accused counsel sought time to file the reply, granted. Adjourned to 14 November for cross-examination of PW1. " +
  "Prepare cross questions on the dishonour memo. Collect the bank return memo original from the client.";

export default function ApprovalsPage() {
  const pending = useApi(() => api.updates("pending"), [], { intervalMs: 8000 });
  const decided = useApi(() => api.updates(), []);
  const metrics = useApi(() => api.updateMetrics(), []);
  const reloadAll = () => {
    pending.reload();
    decided.reload();
    metrics.reload();
  };
  const recent = (decided.data ?? []).filter((u) => u.status !== "pending").slice(0, 8);
  const share = metrics.data?.approved_unedited_share;

  return (
    <BackendGate>
      <PageHeader eyebrow="After the hearing" title="Approvals">
        <div className="text-sm text-muted-foreground">
          {metrics.data && (
            <span>
              Approved unedited:{" "}
              <strong className="text-navy">{share == null ? "n/a" : `${Math.round(share * 100)}%`}</strong> of{" "}
              {metrics.data.approved}
            </span>
          )}
        </div>
      </PageHeader>

      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <DictateForm onCreated={reloadAll} />

        <div className="space-y-4">
          <h2 className="font-serif text-2xl text-navy">Waiting for you</h2>
          {pending.error && <ErrorNote error={pending.error} onRetry={pending.reload} />}
          {!pending.data && pending.loading && <LoadingRows rows={2} />}
          {pending.data?.length === 0 && (
            <Empty>
              Nothing to approve. Dictate an update here, or tell Jolly on a call. It shows up here before anything
              is saved or sent.
            </Empty>
          )}
          {pending.data?.map((u) => <DraftCard key={u.id} update={u} onDone={reloadAll} />)}
        </div>
      </div>

      {recent.length > 0 && (
        <Panel eyebrow="History" title="Recently decided" className="mt-10">
          <ul className="divide-y divide-navy/10">
            {recent.map((u) => (
              <li key={u.id} className="flex flex-wrap items-center gap-3 py-3 text-sm">
                <Badge variant={u.status === "approved" ? "default" : "outline"}>{u.status}</Badge>
                <Link to={`/matters/${u.case_id}`} className="font-medium text-navy hover:underline">
                  {u.case_number}
                </Link>
                <span className="min-w-0 flex-1 truncate text-muted-foreground">{u.outcome}</span>
                {u.send_link && (
                  <a href={u.send_link} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-brass hover:underline">
                    Send to client <ExternalLink className="size-3.5" />
                  </a>
                )}
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </BackendGate>
  );
}

function DictateForm({ onCreated }: { onCreated: () => void }) {
  const cases = useApi(() => api.cases(), []);
  const [caseId, setCaseId] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const selected = caseId || cases.data?.[0]?.id || "";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.dictate(selected, text.trim());
      setText("");
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err : new ApiError("network", String(err)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      eyebrow="Corridor dictation"
      title={
        <span className="flex items-center gap-2">
          <Mic className="size-5 text-brass" /> What happened in court?
        </span>
      }
      className="border-l-4 border-l-brass self-start"
    >
      <form onSubmit={submit} className="space-y-3">
        <select value={selected} onChange={(e) => setCaseId(e.target.value)} className={field} aria-label="Matter">
          {cases.data?.map((c) => (
            <option key={c.id} value={c.id}>
              {c.id} · {c.title}
            </option>
          ))}
        </select>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          placeholder="Say it like you would to your junior: what happened, the next date, what needs doing."
          className={field}
        />
        {error && <ErrorNote error={error} />}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" disabled={busy || !selected || text.trim().length < 5}>
            {busy ? <Loader2 className="animate-spin" /> : <Plus />} Draft the update
          </Button>
          <Button type="button" variant="ghost" onClick={() => setText(SAMPLE)}>
            Use a sample note
          </Button>
          <Link to="/call" className="ml-auto inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-navy">
            <PhoneCall className="size-3.5" /> or dictate on a call
          </Link>
        </div>
      </form>
    </Panel>
  );
}

function DraftCard({ update, onDone }: { update: HearingUpdate; onDone: () => void }) {
  const [outcome, setOutcome] = useState(update.outcome);
  const [nextDate, setNextDate] = useState(update.next_date ?? "");
  const [nextPurpose, setNextPurpose] = useState(update.next_purpose ?? "");
  const [tasks, setTasks] = useState<DraftTask[]>(update.tasks);
  const [message, setMessage] = useState(update.client_message);
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<Error | null>(null);

  const edited =
    outcome !== update.outcome ||
    nextDate !== (update.next_date ?? "") ||
    nextPurpose !== (update.next_purpose ?? "") ||
    message !== update.client_message ||
    JSON.stringify(tasks) !== JSON.stringify(update.tasks);

  async function act(kind: "approve" | "reject") {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "approve") {
        const res = await api.approveUpdate(
          update,
          edited
            ? { outcome, next_date: nextDate || null, next_purpose: nextPurpose || null, tasks: tasks.filter((t) => t.title.trim()), client_message: message }
            : undefined,
        );
        if (res.send_link) window.open(res.send_link, "_blank", "noopener");
      } else {
        await api.rejectUpdate(update);
      }
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err : new ApiError("network", String(err)));
      setBusy(null);
    }
  }

  return (
    <Panel
      eyebrow={`${update.source === "voice" ? "Dictated on a call" : "Dictated in console"} · ${fmtDate(update.hearing_date)}`}
      title={
        <Link to={`/matters/${update.case_id}`} className="hover:underline">
          {update.case_number} · {update.case_title}
        </Link>
      }
    >
      <div className="space-y-4 text-sm">
        <blockquote className="rounded-lg bg-navy/5 p-3 italic text-navy-deep/80">“{update.dictation}”</blockquote>

        <Labeled label="Timeline entry">
          <textarea value={outcome} onChange={(e) => setOutcome(e.target.value)} rows={2} className={field} />
        </Labeled>

        <div className="grid gap-3 sm:grid-cols-[11rem_1fr]">
          <Labeled label="Next hearing">
            <input type="date" value={nextDate} onChange={(e) => setNextDate(e.target.value)} className={field} />
          </Labeled>
          <Labeled label="Purpose">
            <Input value={nextPurpose} onChange={(e) => setNextPurpose(e.target.value)} className="h-10 bg-ivory" />
          </Labeled>
        </div>

        <Labeled label={`Tasks (${tasks.length})`}>
          <div className="space-y-2">
            {tasks.map((t, i) => (
              <div key={i} className="flex gap-2">
                <Input
                  value={t.title}
                  onChange={(e) => setTasks(tasks.map((x, j) => (j === i ? { ...x, title: e.target.value } : x)))}
                  className="h-9 bg-ivory"
                />
                <Button type="button" variant="ghost" size="icon" aria-label="Remove task" onClick={() => setTasks(tasks.filter((_, j) => j !== i))}>
                  <Trash2 />
                </Button>
              </div>
            ))}
            <Button type="button" variant="outline" size="sm" onClick={() => setTasks([...tasks, { title: "" }])}>
              <Plus /> Add task
            </Button>
          </div>
        </Labeled>

        <Labeled label={`Message to client${update.client_phone ? ` (${update.client_phone})` : ""}`}>
          <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={4} className={field} />
        </Labeled>

        {error && <ErrorNote error={error} />}

        <div className="flex flex-wrap items-center gap-2 border-t border-navy/10 pt-4">
          <Button onClick={() => void act("approve")} disabled={busy !== null || outcome.trim().length < 2}>
            {busy === "approve" ? <Loader2 className="animate-spin" /> : <Check />} {edited ? "Approve with edits" : "Approve"}
          </Button>
          <Button variant="outline" onClick={() => void act("reject")} disabled={busy !== null}>
            {busy === "reject" ? <Loader2 className="animate-spin" /> : <X />} Discard
          </Button>
          <span className="ml-auto text-xs text-muted-foreground">
            Approving saves to the case file and opens WhatsApp with the message. Nothing is sent automatically.
          </span>
        </div>
      </div>
    </Panel>
  );
}

function Labeled({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}
