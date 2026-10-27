// Mirrors backend/models/*.py and the response models in backend/api/*.py.
// Dates arrive as ISO strings ("2026-10-19").

export interface Case {
  id: string;
  title: string;
  case_number: string;
  court: string;
  court_hall: string | null;
  judge: string | null;
  case_type: string;
  stage: string;
  our_side: string;
  client_name: string;
  client_phone: string | null;
  client_language: string;
  opposing_party: string;
  advocate: string;
  next_hearing: string | null;
  next_hearing_purpose: string | null;
  cause_list_item: number | null;
  summary: string;
}

export interface CaseDocument {
  id: string;
  case_id: string;
  title: string;
  kind: string;
  doc_date: string | null;
  text: string;
  trusted: boolean;
}

export interface HearingEvent {
  id: number | null;
  case_id: string;
  event_date: string;
  kind: string;
  outcome: string;
  next_date: string | null;
  source_doc_id: string | null;
  source_para: string | null;
}

export interface Task {
  id: number | null;
  case_id: string;
  title: string;
  owner: string | null;
  due: string | null;
  status: string;
  source_doc_id: string | null;
}

export interface Citation {
  doc_id: string;
  doc_title: string;
  para: string | null;
  doc_date: string | null;
}

export interface BriefLine {
  text: string;
  citation: Citation | null;
}

export interface CaseBrief {
  case_id: string;
  title: string;
  case_number: string;
  court: string;
  court_hall: string | null;
  cause_list_item: number | null;
  next_hearing: string | null;
  next_hearing_purpose: string | null;
  stage: string;
  lines: BriefLine[];
  open_tasks: string[];
  spoken_summary: string;
}

export interface DailyBrief {
  date: string;
  advocate: string | null;
  matters: CaseBrief[];
  spoken_summary: string;
}

export interface AnswerCitation extends Citation {
  quote: string;
  trusted: boolean;
}

export type AnswerMode = "llm" | "extractive" | "refused" | "not_found";

export interface CaseAnswer {
  case_id: string;
  question: string;
  answer: string;
  answerable: boolean;
  mode: AnswerMode | string;
  citations: AnswerCitation[];
}

export interface SearchHit {
  doc_id: string;
  doc_title: string;
  para: string | null;
  doc_date: string | null;
  text: string;
  trusted: boolean;
  score: number;
}

export interface UploadResult {
  doc_id: string;
  chunks: number;
  flagged_paragraphs: string[];
}

export type DocumentKind = "order_sheet" | "pleading" | "evidence" | "note" | "correspondence";

export interface DocumentUpload {
  title: string;
  kind: DocumentKind;
  doc_date?: string | null;
  text: string;
}

export interface CallResponse {
  status: string;
  message: string;
  call_id: string | null;
}

export interface CallStatus {
  call_id: string;
  status: string;
  is_terminal: boolean;
  duration_seconds: number | null;
  transcript: string | null;
  summary: string | null;
  recording_url: string | null;
  error_message: string | null;
}

// Bolna execution payload as received on /bolna/webhook. Only a few fields
// are typed by the backend; everything else Bolna sends is passed through.
export interface ExecutionEvent {
  id?: string | null;
  agent_id?: string | null;
  status?: string | null;
  transcript?: string | null;
  summary?: string | null;
  extracted_data?: Record<string, unknown> | null;
  context_details?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface Health {
  status: string;
  service: string;
}

// Post-hearing dictation drafts (one human approval before anything is saved or sent)
export interface DraftTask {
  title: string;
  owner?: string | null;
  due?: string | null;
}

export type UpdateStatus = "pending" | "approved" | "rejected";

export interface HearingUpdate {
  id: number;
  case_id: string;
  case_title: string;
  case_number: string;
  source: "console" | "voice" | string;
  execution_id: string | null;
  dictation: string;
  hearing_date: string;
  outcome: string;
  next_date: string | null;
  next_purpose: string | null;
  tasks: DraftTask[];
  client_message: string;
  client_phone: string | null;
  send_link: string | null;
  extractor: "gemini" | "rules" | string;
  status: UpdateStatus;
  created_at: string;
  decided_at: string | null;
  /** Signed, expiring, single-use approval token (pending drafts only). */
  approval_token?: string | null;
  approve_url?: string | null;
  reject_url?: string | null;
}

export interface UpdateEdits {
  outcome?: string;
  next_date?: string | null;
  next_purpose?: string | null;
  tasks?: DraftTask[];
  client_message?: string;
}

export interface UpdateMetrics {
  pending: number;
  approved: number;
  rejected: number;
  approved_unedited: number;
  approved_unedited_share: number | null;
}
