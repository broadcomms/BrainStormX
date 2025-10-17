import React, { useMemo, useRef, useState, useEffect } from "react";
import { MessageCircle, X, Mic, Send, Sparkles, Clock, Users, ShieldCheck, BookOpen, PlusCircle, Play, Pause, ThumbsUp, ThumbsDown, ChevronDown, ChevronUp, Bot, NotebookPen, Gavel, AlertTriangle, Filter, Paperclip, Share2, CheckCircle2, SquarePen } from "lucide-react";

// ------------------------------------------------------------
// BrainStormX Autonomous Workshop Assistant – Visual Mockup
// Single-file React component (Tailwind CSS). No backend wiring.
// ------------------------------------------------------------

const PERSONA_META = {
  guide: { label: "Guide", color: "bg-indigo-600", soft: "bg-indigo-50 text-indigo-700", icon: Sparkles },
  scribe: { label: "Scribe", color: "bg-emerald-600", soft: "bg-emerald-50 text-emerald-700", icon: NotebookPen },
  mediator: { label: "Mediator", color: "bg-amber-600", soft: "bg-amber-50 text-amber-800", icon: Users },
  devil: { label: "Devil", color: "bg-rose-600", soft: "bg-rose-50 text-rose-700", icon: AlertTriangle },
};

const SAMPLE_MESSAGES = [
  {
    id: 1,
    role: "assistant",
    persona: "guide",
    text: "Welcome! I’m tracking our **Feasibility** phase. We have 12 minutes left. Want me to summarize the top-voted clusters or draft next steps?",
    citations: [],
    toolfeed: [{ tool: "fetch_phase_snapshot", status: "ok", millis: 212 }],
  },
  {
    id: 2,
    role: "user",
    persona: "participant",
    author: "Patrick",
    text: "Summarize Cluster Gamma and flag any blockers.",
  },
  {
    id: 3,
    role: "assistant",
    persona: "scribe",
    text:
      "**Cluster Gamma – ‘Virtual Collaboration Toolkit’**\n• Technical: Feasible with current stack; minor auth work.\n• Operational: Needs onboarding playbook.\n• Risk: Vendor lock-in.\n**Blockers:** SSO scope creep; unclear admin roles.",
    citations: [
      { label: "Feasibility Report §2", type: "doc" },
      { label: "Voting Results", type: "phase" },
    ],
    toolfeed: [
      { tool: "search_documents", status: "ok", millis: 438 },
      { tool: "fetch_phase_snapshot", status: "ok", millis: 175 },
    ],
  },
  {
    id: 4,
    role: "assistant",
    persona: "devil",
    text:
      "Counterpoint: If adoption lags, ROI slips. Consider **open standards** first and timebox a pilot to 2 weeks.",
    citations: [],
    toolfeed: [],
  },
];

function PersonaBadge({ persona }: { persona: keyof typeof PERSONA_META | "participant" }) {
  if (persona === "participant") return (
    <div className="inline-flex items-center gap-1 rounded-full bg-slate-200 px-2 py-0.5 text-[11px] font-medium text-slate-700">
      <Users className="h-3 w-3" /> Participant
    </div>
  );
  const meta = PERSONA_META[persona];
  const Icon = meta.icon;
  return (
    <div className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium text-white ${meta.color}`}>
      <Icon className="h-3 w-3" /> {meta.label}
    </div>
  );
}

function ToolFeed({ items = [] as { tool: string; status: string; millis: number }[] }) {
  if (!items.length) return null;
  return (
    <div className="mt-2 rounded-md bg-slate-50 p-2 text-[11px] text-slate-600 ring-1 ring-slate-200">
      <div className="mb-1 flex items-center gap-2 font-semibold text-slate-700">
        <Bot className="h-3.5 w-3.5" /> Tool activity
      </div>
      <div className="grid grid-cols-1 gap-1">
        {items.map((it, i) => (
          <div key={i} className="flex items-center justify-between">
            <span className="font-mono">{it.tool}</span>
            <span className={it.status === "ok" ? "text-emerald-600" : "text-rose-600"}>
              {it.status} <span className="text-slate-400">· {it.millis}ms</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Citations({ items = [] as { label: string; type: string }[] }) {
  if (!items.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {items.map((c, i) => (
        <span key={i} className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600 ring-1 ring-slate-200">
          {c.label}
        </span>
      ))}
    </div>
  );
}

function MessageCard({ msg, onFeedback }: { msg: any; onFeedback: (id: number, v: "up" | "down") => void }) {
  const isAssistant = msg.role === "assistant";
  const bubbleColor = isAssistant ? PERSONA_META[msg.persona]?.soft ?? "bg-slate-50" : "bg-white";
  return (
    <div className="group w-full">
      <div className={`rounded-2xl ${bubbleColor} p-3 ring-1 ring-slate-200`}> 
        <div className="mb-1 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <PersonaBadge persona={msg.persona || "participant"} />
            {msg.author && <span className="text-xs font-medium text-slate-600">{msg.author}</span>}
          </div>
          {isAssistant && (
            <div className="invisible flex items-center gap-1 text-slate-400 group-hover:visible">
              <button onClick={() => onFeedback(msg.id, "up")} className="rounded p-1 hover:bg-white">
                <ThumbsUp className="h-4 w-4" />
              </button>
              <button onClick={() => onFeedback(msg.id, "down")} className="rounded p-1 hover:bg-white">
                <ThumbsDown className="h-4 w-4" />
              </button>
            </div>
          )}
        </div>
        <div className="prose prose-sm max-w-none text-slate-800">
          {/* eslint-disable-next-line react/no-danger */}
          <div dangerouslySetInnerHTML={{ __html: msg.text.replace(/\n/g, "<br/>") }} />
        </div>
        <Citations items={msg.citations} />
        <ToolFeed items={msg.toolfeed} />
        {isAssistant && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button className="inline-flex items-center gap-1 rounded-full border border-slate-300 px-2 py-1 text-[12px] text-slate-700 hover:bg-white">
              <PlusCircle className="h-3.5 w-3.5" /> Add as Action Item
            </button>
            <button className="inline-flex items-center gap-1 rounded-full border border-slate-300 px-2 py-1 text-[12px] text-slate-700 hover:bg-white">
              <Gavel className="h-3.5 w-3.5" /> Capture Decision
            </button>
            <button className="inline-flex items-center gap-1 rounded-full border border-slate-300 px-2 py-1 text-[12px] text-slate-700 hover:bg-white">
              <Share2 className="h-3.5 w-3.5" /> Broadcast
            </button>
            <button className="ml-auto inline-flex items-center gap-1 rounded-full border border-slate-300 px-2 py-1 text-[12px] text-slate-700 hover:bg-white">
              <Play className="h-3.5 w-3.5" /> TTS
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function HeaderBar({ onClose }: { onClose: () => void }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-200 p-3">
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-full bg-indigo-600 text-white shadow-sm">
          <MessageCircle className="h-5 w-5" />
        </div>
        <div>
          <div className="text-sm font-semibold text-slate-900">BrainStormX Assistant</div>
          <div className="text-xs text-slate-500">Workshop · Feasibility · 12m left</div>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <div className="hidden items-center gap-1 rounded-full bg-slate-100 px-2 py-1 text-xs text-slate-700 md:flex">
          <Clock className="h-3.5 w-3.5" /> Timebox Active
        </div>
        <div className="hidden items-center gap-1 rounded-full bg-slate-100 px-2 py-1 text-xs text-slate-700 md:flex">
          <ShieldCheck className="h-3.5 w-3.5" /> RBAC: Organizer
        </div>
        <button onClick={onClose} className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100">
          <X className="h-5 w-5" />
        </button>
      </div>
    </div>
  );
}

function Chips({ items = [], onClick }: { items: string[]; onClick: (s: string) => void }) {
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((c, i) => (
        <button key={i} onClick={() => onClick(c)} className="rounded-full bg-slate-100 px-3 py-1 text-xs text-slate-700 hover:bg-white hover:text-slate-900 hover:ring-1 hover:ring-slate-300">
          {c}
        </button>
      ))}
    </div>
  );
}

function Collapsible({ title, children, defaultOpen = true }: any) {
  const [open, setOpen] = useState(!!defaultOpen);
  return (
    <div className="rounded-xl border border-slate-200">
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium">
        <span>{title}</span>
        {open ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
      </button>
      {open && <div className="border-t border-slate-100 p-3 text-sm text-slate-700">{children}</div>}
    </div>
  );
}

function Sidebar({ onChipClick }: { onChipClick: (s: string) => void }) {
  return (
    <div className="flex h-full flex-col gap-3 p-3">
      <div className="rounded-xl border border-slate-200 p-3">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Context</div>
        <Chips
          items={["Explain shortlist", "Summarize decisions", "Draft status email", "Generate devil's advocate", "Show action items"]}
          onClick={onChipClick}
        />
      </div>
      <Collapsible title="Phase Snapshot">
        <ul className="space-y-1 text-[13px]">
          <li className="flex items-center justify-between"><span>Phase</span><span className="font-medium">Feasibility</span></li>
          <li className="flex items-center justify-between"><span>Timer</span><span className="font-medium">12:00</span></li>
          <li className="flex items-center justify-between"><span>Top Cluster</span><span className="font-medium">Gamma</span></li>
        </ul>
      </Collapsible>
      <Collapsible title="Proposed Actions">
        <div className="space-y-2">
          <div className="rounded-lg border border-slate-200 p-2">
            <div className="text-[13px] font-semibold">Action Item</div>
            <div className="text-[12px] text-slate-600">Draft onboarding playbook · Owner: Alex · Due: Nov 4</div>
            <div className="mt-2 flex gap-2">
              <button className="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-[12px] hover:bg-white"><CheckCircle2 className="h-3.5 w-3.5"/>Accept</button>
              <button className="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-[12px] hover:bg-white"><X className="h-3.5 w-3.5"/>Dismiss</button>
            </div>
          </div>
          <div className="rounded-lg border border-slate-200 p-2">
            <div className="text-[13px] font-semibold">Decision</div>
            <div className="text-[12px] text-slate-600">Proceed with 2-week pilot on open-standard stack</div>
            <div className="mt-2 flex gap-2">
              <button className="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-[12px] hover:bg-white"><SquarePen className="h-3.5 w-3.5"/>Edit</button>
              <button className="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-[12px] hover:bg-white"><CheckCircle2 className="h-3.5 w-3.5"/>Capture</button>
            </div>
          </div>
        </div>
      </Collapsible>
      <Collapsible title="Threads">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <button className="rounded-lg border border-indigo-300 bg-indigo-50 px-2 py-1 font-medium text-indigo-700">Room</button>
          <button className="rounded-lg border border-slate-200 px-2 py-1">Patrick</button>
          <button className="rounded-lg border border-slate-200 px-2 py-1">Feasibility Qs</button>
          <button className="rounded-lg border border-slate-200 px-2 py-1">Ideas</button>
        </div>
      </Collapsible>
    </div>
  );
}

function InputBar({ onSend, persona, setPersona }: any) {
  const [text, setText] = useState("");
  const textRef = useRef<HTMLTextAreaElement | null>(null);
  const personas = ["guide", "scribe", "mediator", "devil"] as const;

  return (
    <div className="border-t border-slate-200 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span>Persona:</span>
          <div className="flex gap-1">
            {personas.map((p) => (
              <button
                key={p}
                onClick={() => setPersona(p)}
                className={`rounded-full px-2 py-1 text-xs ${
                  persona === p ? PERSONA_META[p].soft : "bg-slate-100 text-slate-700"
                }`}
              >
                {PERSONA_META[p].label}
              </button>
            ))}
          </div>
        </div>
        <div className="hidden items-center gap-2 text-xs text-slate-500 md:flex">
          <span className="inline-flex items-center gap-1"><Paperclip className="h-3.5 w-3.5"/>Attach</span>
          <span className="inline-flex items-center gap-1"><Filter className="h-3.5 w-3.5"/>Citations on</span>
        </div>
      </div>
      <div className="flex items-end gap-2">
        <textarea
          ref={textRef}
          className="min-h-[44px] w-full flex-1 resize-y rounded-xl border border-slate-300 p-3 text-sm outline-none placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-200"
          placeholder="Ask the assistant…"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button
          onClick={() => {
            if (!text.trim()) return;
            onSend(text.trim());
            setText("");
            textRef.current?.focus();
          }}
          className="inline-flex h-11 items-center justify-center gap-2 rounded-xl bg-indigo-600 px-4 text-sm font-semibold text-white shadow-sm hover:bg-indigo-700"
        >
          <Send className="h-4 w-4" /> Send
        </button>
      </div>
    </div>
  );
}

function ChatPane({ messages, setMessages }: any) {
  const [persona, setPersona] = useState<keyof typeof PERSONA_META>("guide");
  const viewportRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    viewportRef.current?.scrollTo({ top: viewportRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length]);

  const handleSend = (text: string) => {
    const id = Date.now();
    setMessages((prev: any[]) => [
      ...prev,
      { id: id - 1, role: "user", persona: "participant", author: "You", text },
      {
        id,
        role: "assistant",
        persona,
        text: `▌`, // streaming cursor
        citations: [],
        toolfeed: [{ tool: "fetch_phase_snapshot", status: "ok", millis: 120 }],
      },
    ]);
    // Fake stream
    const chunks = [
      "Thinking…",
      " Here’s a concise take:",
      " 1) Align SSO scope. 2) Timebox pilot. 3) Track adoption KPIs.",
    ];
    let i = 0;
    const timer = setInterval(() => {
      setMessages((prev: any[]) =>
        prev.map((m: any) => (m.id === id ? { ...m, text: (m.text + " " + chunks[i]).trim() } : m))
      );
      i++;
      if (i >= chunks.length) {
        clearInterval(timer);
        setMessages((prev: any[]) => prev.map((m: any) => (m.id === id ? { ...m, text: m.text.replace(/^▌/, "").trim() } : m)));
      }
    }, 450);
  };

  const onFeedback = (id: number, v: "up" | "down") => {
    setMessages((prev: any[]) => prev.map((m: any) => (m.id === id ? { ...m, feedback: v } : m)));
  };

  return (
    <div className="flex h-full flex-col">
      <HeaderBar onClose={() => { /* no-op in mock */ }} />
      <div ref={viewportRef} className="flex-1 space-y-3 overflow-y-auto bg-gradient-to-b from-white to-slate-50 p-3">
        {messages.map((m: any) => (
          <MessageCard key={m.id} msg={m} onFeedback={onFeedback} />
        ))}
      </div>
      <InputBar onSend={handleSend} persona={persona} setPersona={setPersona} />
    </div>
  );
}

export default function BrainStormXAssistantMock() {
  const [open, setOpen] = useState(true);
  const [messages, setMessages] = useState(SAMPLE_MESSAGES);

  return (
    <div className="relative min-h-[100vh] w-full bg-white p-6">
      {/* North Star & Page Header */}
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">BrainStormX Autonomous Workshop Assistant</h1>
        <p className="mt-2 rounded-xl border border-indigo-100 bg-indigo-50 p-3 text-sm leading-6 text-indigo-900">
          <strong>North Star:</strong> Deliver the first workshop assistant that blends multimodal intelligence, proactive facilitation, and collaborative AI personas — trustworthy, governable, delightful.
        </p>
      </div>

      {/* Floating bubble */}
      {!open && (
        <button
          onClick={() => setOpen(true)}
          className="fixed bottom-6 right-6 inline-flex h-14 w-14 items-center justify-center rounded-full bg-indigo-600 text-white shadow-xl ring-4 ring-indigo-200 transition hover:bg-indigo-700"
          aria-label="Open Assistant"
        >
          <Sparkles className="h-6 w-6" />
        </button>
      )}

      {/* Assistant Panel */}
      {open && (
        <div className="fixed bottom-6 right-6 z-50 w-[min(1100px,100vw-1.5rem)] overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
          <div className="grid h-[72vh] grid-cols-1 md:grid-cols-12">
            <div className="md:col-span-8">
              <ChatPane messages={messages} setMessages={setMessages} />
            </div>
            <div className="hidden border-l border-slate-200 md:col-span-4 md:block">
              <Sidebar onChipClick={(s) => {
                setMessages((prev: any[]) => [
                  ...prev,
                  { id: Date.now() - 1, role: "user", persona: "participant", author: "You", text: s },
                  { id: Date.now(), role: "assistant", persona: "guide", text: "On it — here’s a quick pass…", toolfeed: [], citations: [] },
                ]);
              }} />
            </div>
          </div>
        </div>
      )}

      {/* Trust & Governance footer callouts (static) */}
      <div className="pointer-events-none fixed inset-x-6 bottom-3 mx-auto flex max-w-5xl items-center justify-center gap-2 opacity-70">
        <div className="pointer-events-auto inline-flex items-center gap-2 rounded-full bg-slate-900/90 px-3 py-1 text-[11px] text-slate-100 shadow">
          <ShieldCheck className="h-3.5 w-3.5"/> Governance-first
        </div>
        <div className="pointer-events-auto inline-flex items-center gap-2 rounded-full bg-slate-900/90 px-3 py-1 text-[11px] text-slate-100 shadow">
          <BookOpen className="h-3.5 w-3.5"/> Citations required
        </div>
      </div>
    </div>
  );
}
