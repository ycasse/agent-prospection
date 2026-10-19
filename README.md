# Outbound agents: one task, one agent, the cheapest model that works

A multi-agent system that runs my own B2B outbound (cold email for mobile apps and AI agents) every day, unattended, on a Mac with launchd. It reads three mailboxes, triages replies, researches the prospect, answers in the same thread, sends the daily sequence, prospects new leads and reports weekly. It has been running on real prospects and real replies since October 2026.

Prompts and rules are in French (my market). Code comments are in French too.

## Architecture

| Task | Who does it | Model |
|---|---|---|
| Read the mailboxes (IMAP, read-only, headers first) | `agents.py`, plain code | none |
| Triage a reply into 9 categories | agent `tri.md` | Haiku |
| Research the prospect before answering (site, App Store, jobs, press) | agent `besoins.md`, web tools | Sonnet |
| Answer in the thread, or decide not to, or hand over as a draft | agent `traitement.md` + shared `regles.md` | Sonnet, Opus when the lead is hot |
| Send the sequence (quota per mailbox, warm-up steps, follow-ups in thread, stop on reply or bounce) | `send.py`, plain code | none |
| Weekly prospecting batch, human-validated | agent `prospection.md`, web tools | Sonnet |
| Weekly analysis and 2 or 3 proposed decisions | agent `analyse.md` | Sonnet |
| Orchestration: schedule, lock, journal, notifications, daily report | `agents.py` + `planning.py` (launchd) | none |

Every agent runs as a headless `claude -p` call with a JSON schema for structured output, so the orchestrator only acts on validated fields (`action`, `statut`, `corps`, `rdv`, `apprentissage`).

## Guardrails

- The answering agent never gives a price by email, never invents a client or a number (an explicit list of allowed facts), and always steers towards a 20-minute call using free slots computed from a booking log.
- Legal, contractual, aggressive or unclear replies go to the human as a Gmail draft instead of being sent.
- A clear "no" gets no answer; an unsubscribe request stops everything for good; a refusal with a reason gets a short answer only if the reason can honestly be addressed.
- Every refusal produces a written lesson (what it says about targeting, the scraper or the copy), appended to a decision log that the weekly analysis reads.

## What was hard

- **Non-replies that look like replies.** Exchange bounce notices come from addresses that are not "mailer-daemon" and quote the original email, so they match the thread. Newsletters from prospects you subscribed to match the sender. Both are now filtered on headers before any model sees them, and bounces are logged as bounces.
- **Replies from another address.** People answer from a personal address; the thread is matched through `In-Reply-To` / `References` against the Message-IDs of every email sent.
- **Unattended runs.** A Gmail IMAP connection that hangs without a timeout froze the morning run; the CLI inherited the desktop app's environment and billed a credit-less API key instead of the subscription. Fixed with timeouts on every socket and a clean environment per agent call.
- **Cost.** Triage on Haiku, research and answers on Sonnet, Opus only for hot leads; headers are fetched first so bodies (and model calls) are only spent on actual prospect replies.
- **Evaluation.** Each prompt was tested on real past replies plus synthetic edge cases (price question, refusal with a reason, accepted slot, auto-replies) before going live.

## Run

```
python3 agents.py --selfcheck
python3 agents.py veille --dry      # read and decide, send nothing
python3 planning.py installer       # launchd: 10:00 send, 16:00 and 17:30 replies, Monday 9:00 prospecting
```
