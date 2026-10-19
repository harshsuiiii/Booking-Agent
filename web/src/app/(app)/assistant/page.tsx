"use client";

import { useEffect, useRef, useState, FormEvent, KeyboardEvent, ReactNode } from "react";
import type { AgentOffer } from "@/types/agent";
import { streamAgentChat } from "@/lib/agentStream";
import AgentOfferCard from "@/components/AgentOfferCard";
import BookingDialog from "@/components/BookingDialog";

interface Message {
  id: number;
  role: "user" | "assistant" | "error";
  text: string;
  offers?: AgentOffer[];
}

const EXAMPLES = [
  "Delhi to Mumbai next Friday, 1 adult, economy",
  "BLR to DXB on 2026-12-20 for 2 adults, refundable only",
];

/** Renders **bold** and line breaks; everything else stays plain text. */
function renderText(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? (
      <strong key={i}>{part.slice(2, -2)}</strong>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

export default function AgentPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [booking, setBooking] = useState<AgentOffer | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  const nextId = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, sending]);

  function add(msg: Omit<Message, "id">) {
    setMessages((prev) => [...prev, { ...msg, id: nextId.current++ }]);
  }

  async function send(text: string) {
    const message = text.trim();
    if (!message || sending) return;
    add({ role: "user", text: message });
    setInput("");
    setSending(true);
    setProgress("Thinking…");
    // Filled from inside the stream callback.
    const result: { reply: string | null; offers: AgentOffer[] } = { reply: null, offers: [] };
    try {
      await streamAgentChat({ message, session_id: sessionId }, (e) => {
        switch (e.type) {
          case "session":
            setSessionId(e.session_id);
            break;
          case "status":
          case "tool_done":
            setProgress(e.text);
            break;
          case "reply":
            result.reply = e.text;
            break;
          case "offers":
            result.offers = e.offers;
            break;
          case "error":
            add({ role: "error", text: e.message });
            break;
        }
      });
      if (result.reply !== null) {
        add({ role: "assistant", text: result.reply, offers: result.offers });
      }
    } catch (err) {
      add({
        role: "error",
        text:
          err instanceof Error && err.message
            ? err.message
            : "Could not reach the server. Check that the API is running.",
      });
    } finally {
      setSending(false);
      setProgress(null);
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    send(input);
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  }

  function newChat() {
    setMessages([]);
    setSessionId(null);
  }

  return (
    <div className="flex min-h-[calc(100vh-7rem)] flex-col">
      <div className="max-w-3xl w-full mx-auto flex-1 flex flex-col gap-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-zinc-900">Travel Agent Assistant</h1>
            <p className="text-sm text-zinc-500 mt-0.5">
              Describe the trip. The assistant searches and compares; you book.
            </p>
          </div>
          {messages.length > 0 && (
            <button
              type="button"
              onClick={newChat}
              disabled={sending}
              className="text-sm px-3 py-1.5 rounded-lg border border-zinc-300 text-zinc-600 hover:bg-white disabled:opacity-50"
            >
              New chat
            </button>
          )}
        </div>

        <div className="flex-1 flex flex-col gap-4" aria-live="polite" data-testid="chat-log">
          {messages.length === 0 && (
            <div className="flex flex-col gap-2 py-8">
              <p className="text-sm text-zinc-500">Try:</p>
              {EXAMPLES.map((ex) => (
                <button
                  key={ex}
                  type="button"
                  onClick={() => send(ex)}
                  className="text-left text-sm bg-white border border-zinc-200 rounded-lg px-4 py-2 text-zinc-700 hover:border-blue-300"
                >
                  {ex}
                </button>
              ))}
            </div>
          )}

          {messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="self-end max-w-[85%] bg-blue-600 text-white rounded-2xl rounded-br-sm px-4 py-2 text-sm whitespace-pre-wrap">
                {m.text}
              </div>
            ) : m.role === "error" ? (
              <div
                key={m.id}
                role="alert"
                className="self-start max-w-[85%] bg-red-50 border border-red-200 text-red-700 rounded-2xl rounded-bl-sm px-4 py-2 text-sm"
              >
                {m.text}
              </div>
            ) : (
              <div key={m.id} className="self-stretch flex flex-col gap-3">
                <div
                  className="self-start max-w-[85%] bg-white border border-zinc-200 rounded-2xl rounded-bl-sm px-4 py-2 text-sm text-zinc-800 whitespace-pre-wrap"
                  data-testid="assistant-message"
                >
                  {renderText(m.text)}
                </div>
                {m.offers && m.offers.length > 0 && (
                  <div className="flex flex-col gap-2">
                    {m.offers.map((o) => (
                      <AgentOfferCard key={o.offer_id} offer={o} onBook={setBooking} />
                    ))}
                  </div>
                )}
              </div>
            ),
          )}

          {sending && (
            <div
              className="self-start text-sm text-zinc-500 animate-pulse"
              aria-label="Assistant is replying"
              data-testid="agent-progress"
            >
              {progress ?? "Thinking…"}
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        <form
          onSubmit={handleSubmit}
          className="sticky bottom-4 bg-white border border-zinc-200 rounded-xl shadow-sm p-2 flex gap-2 items-end"
        >
          <label htmlFor="chat-input" className="sr-only">
            Message
          </label>
          <textarea
            id="chat-input"
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="e.g. Cheapest DEL → BOM on 20 Nov, 2 adults"
            className="flex-1 resize-none px-3 py-2 text-sm focus:outline-none max-h-40"
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            className="bg-blue-600 text-white text-sm font-medium rounded-lg px-4 py-2 hover:bg-blue-700 disabled:opacity-50"
          >
            Send
          </button>
        </form>
      </div>

      {booking && <BookingDialog offer={booking} onClose={() => setBooking(null)} />}
    </div>
  );
}
