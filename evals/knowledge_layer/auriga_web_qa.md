# Knowledge-layer eval — auriga-web Q&A

**Purpose:** Ground-truth questions for the Unified Knowledge Layer A/B/C experiment ([design](../docs/workspace-index-design.md)).  
**Corpus:** [auriga-web](https://github.com/human-plus-machine/auriga-web) (clone into the agent workspace).  
**Scoring:** Agent answers questions from the repo only; parse the transcript and compare to **Answer** / **Accept** fields here. Do **not** give this file (or the Answer blocks) to the agent under test.

## How to run

1. Give the agent a **questions-only** prompt (copy from each `### Qxx` → **Question** line, or use the [Agent prompt pack](#agent-prompt-pack) at the bottom).
2. Instruct it to clone auriga-web into the workspace and answer **every** question.
3. Require a fixed answer format in the transcript, e.g.:

```text
Q01: <answer>
Evidence: <repo-relative-path>[:lines]
```

4. Score: mark correct if the transcript answer matches **Answer** (case-insensitive where noted) **or** contains all **Accept** tokens, **and** preferably cites a path under **Evidence**.

## Legend

| Field | Meaning |
|-------|---------|
| **Bucket** | auth · api · ui · domain · config · architecture · media |
| **Kind** | `exact` (needle) · `conceptual` (reason across files) · `path` (locate asset/file) |
| **Evidence** | Primary ground-truth file (+ lines when stable) |
| **Accept** | Substrings/keywords that also count as correct |

Paths are **repo-relative** to auriga-web.

---

## Questions and answers

### Q01

- **Bucket:** auth · **Kind:** exact
- **Question:** In production, how many minutes of idle time before the AuthProvider signs the user out? Why that duration?
- **Answer:** 15 minutes; documented as a FERPA requirement (60 minutes in development).
- **Evidence:** `src/providers/AuthProvider.tsx` L45–47
- **Accept:** `15`, `FERPA`

### Q02

- **Bucket:** auth · **Kind:** exact
- **Question:** What Firebase auth persistence mode does the app set by default?
- **Answer:** `browserLocalPersistence`
- **Evidence:** `src/lib/firebase.ts`
- **Accept:** `browserLocalPersistence`, `local`

### Q03

- **Bucket:** auth · **Kind:** exact
- **Question:** What storage key is used for the backend Context Engine user id, and where is it stored?
- **Answer:** Key `backend_user_id` in `sessionStorage` (preferred) and `localStorage` (fallback).
- **Evidence:** `src/lib/backendUserId.ts` L1–12
- **Accept:** `backend_user_id`

### Q04

- **Bucket:** auth · **Kind:** conceptual
- **Question:** Besides Firebase auth and email verification, what else can `ProtectedRoute` use to redirect a signed-in user who has not finished setup?
- **Answer:** Incomplete onboarding — via `getIncompleteOnboardingHref` (and related onboarding checks).
- **Evidence:** `src/components/auth/ProtectedRoute.tsx`
- **Accept:** `onboarding`, `getIncompleteOnboardingHref`

### Q05

- **Bucket:** api · **Kind:** exact
- **Question:** Which three HTTP headers does auriga-web attach on Auriga Connect / Context Engine API calls for auth and tenancy?
- **Answer:** `Authorization: Bearer <firebase_token>`, `X-Tenant-Id`, and `X-Identity-Provider-Type: firebase`.
- **Evidence:** `src/lib/api/contextengine.ts` ~L180–182
- **Accept:** `Authorization`, `X-Tenant-Id`, `X-Identity-Provider-Type`

### Q06

- **Bucket:** api · **Kind:** conceptual
- **Question:** How does `getIdToken()` avoid races when multiple callers need a token while Firebase auth is still initializing?
- **Answer:** It deduplicates concurrent waits with a shared `onAuthStateChanged` / `authReadyPromise` pattern.
- **Evidence:** `src/lib/api/contextengine.ts` ~L127–163
- **Accept:** `authReadyPromise` OR `onAuthStateChanged` OR `deduplicat`

### Q07

- **Bucket:** api · **Kind:** conceptual
- **Question:** Does SAT prep chat/session traffic go through LangGraph streaming? If not, what does it use instead?
- **Answer:** No — SAT uses REST against auriga-connect `/v1/sat/sessions` (see `src/lib/api/sat.ts`), and `assistantSkipsLangGraphStream` treats `sat_prep` / `act_prep` as non-LangGraph stream.
- **Evidence:** `src/lib/api/sat.ts` L1–4; `src/config/flags.ts` L188–197
- **Accept:** `REST` OR `/v1/sat/sessions`; `sat_prep`

### Q08

- **Bucket:** api · **Kind:** exact
- **Question:** What is the maximum LangGraph API passthrough response size enforced by `/api/[..._path]`?
- **Answer:** 50 MB (`50 * 1024 * 1024` bytes).
- **Evidence:** `src/app/api/[..._path]/route.ts` L5–6
- **Accept:** `50`

### Q09

- **Bucket:** api · **Kind:** conceptual
- **Question:** How does Agent Gateway auth relate to Context Engine auth headers?
- **Answer:** It reuses the same Firebase bearer + tenant header pattern via `getAgentGatewayAuthHeaders()`.
- **Evidence:** `src/lib/agent-gateway/authHeaders.ts`
- **Accept:** `getAgentGatewayAuthHeaders` OR (`Bearer` AND `X-Tenant-Id`)

### Q10

- **Bucket:** ui · **Kind:** exact
- **Question:** What is the primary brand accent hex color in the Auriga “Ivy League SaaS” design system?
- **Answer:** `#EA580C` (burnished orange).
- **Evidence:** `DESIGN.md` L5–6
- **Accept:** `#EA580C` OR `EA580C`

### Q11

- **Bucket:** ui · **Kind:** exact
- **Question:** Which fonts does DESIGN.md assign to headlines vs body/UI?
- **Answer:** Merriweather (serif) for headlines; Inter (sans) for body/UI.
- **Evidence:** `DESIGN.md` typography section
- **Accept:** `Merriweather`, `Inter`

### Q12

- **Bucket:** ui · **Kind:** exact
- **Question:** What CSS variable holds the light-mode paper surface color, and what hex is it?
- **Answer:** `--surface-paper` is `#fbfbf9`.
- **Evidence:** `src/app/globals.css` ~L43
- **Accept:** `--surface-paper`, `fbfbf9`

### Q13

- **Bucket:** ui · **Kind:** conceptual
- **Question:** What component library style does shadcn use here, and what underlying library powers the Button?
- **Answer:** shadcn **new-york** style; Button uses **react-aria-components** (with CVA variants).
- **Evidence:** `components.json`; `src/components/ui/button.tsx`
- **Accept:** `new-york`, `react-aria`

### Q14

- **Bucket:** ui · **Kind:** exact
- **Question:** Why does `tailwind.config.cjs` include paths under `../auriga-agents/agents/*/ui/**`?
- **Answer:** So Tailwind can style generative UI components loaded via `LoadExternalComponent` from the sibling auriga-agents repo.
- **Evidence:** `tailwind.config.cjs` L8–10
- **Accept:** `LoadExternalComponent` OR `auriga-agents` OR `generative`

### Q15

- **Bucket:** domain · **Kind:** exact
- **Question:** Which URL query parameter selects the agent chat layout, and what layout is used when it is unset?
- **Answer:** `assistantId`; default is EssayCoachLayout / essay coach.
- **Evidence:** `src/components/thread/index.tsx` L21–32
- **Accept:** `assistantId`, `EssayCoach` OR `essay_coach`

### Q16

- **Bucket:** domain · **Kind:** exact
- **Question:** List at least five `assistantId` values that the Thread router documents.
- **Answer:** Among: `essay_coach`, `financial_advisor`, `sat_prep`, `interview_prep`, `application_architect`, `college_recommendation` (also ACT / proactive tutor exist in the layouts import set).
- **Evidence:** `src/components/thread/index.tsx` L24–31
- **Accept:** any 5 of those ids

### Q17

- **Bucket:** domain · **Kind:** exact
- **Question:** Which grade levels does student onboarding support as `GradeLevel`?
- **Answer:** `9th`, `10th`, `11th`, `12th`.
- **Evidence:** `src/components/onboarding/questions.ts` L4
- **Accept:** `9th`, `10th`, `11th`, `12th`

### Q18

- **Bucket:** domain · **Kind:** exact
- **Question:** After onboarding/login, where does each persona land for their dashboard home?
- **Answer:** Counselor → `/counselor/today`; parent → `/parent/dashboard`; student → `/dashboard`.
- **Evidence:** `src/lib/routing.ts` L34–48
- **Accept:** `/counselor/today`, `/parent/dashboard`, `/dashboard`

### Q19

- **Bucket:** domain · **Kind:** exact
- **Question:** Which assistants does `assistantSkipsLangGraphStream` always treat as skipping LangGraph stream (by id equality), before the Agent Engine check?
- **Answer:** `sat_prep` and `act_prep`.
- **Evidence:** `src/config/flags.ts` L188–197
- **Accept:** `sat_prep`, `act_prep`

### Q20

- **Bucket:** domain · **Kind:** path
- **Question:** Where does static college reference JSON live in the repo?
- **Answer:** `data/colleges.json`
- **Evidence:** `data/colleges.json`
- **Accept:** `data/colleges.json` OR `colleges.json`

### Q21

- **Bucket:** config · **Kind:** exact
- **Question:** What is `NEXT_PUBLIC_SITE_URL` set to in production App Hosting config?
- **Answer:** `https://auriga-os.com`
- **Evidence:** `apphosting.yaml` L25–26
- **Accept:** `auriga-os.com`

### Q22

- **Bucket:** config · **Kind:** exact
- **Question:** What site URL does the QA App Hosting overlay use?
- **Answer:** `https://beta.auriga-os.com`
- **Evidence:** `apphosting.qa.yaml`
- **Accept:** `beta.auriga-os.com`

### Q23

- **Bucket:** config · **Kind:** exact
- **Question:** What Firebase App Hosting backend id is configured for this app, and is classic Firebase Hosting enabled?
- **Answer:** Backend id `auriga-web-app`; classic hosting is `null` (disabled).
- **Evidence:** `firebase.json` L1–5
- **Accept:** `auriga-web-app`, `null`

### Q24

- **Bucket:** architecture · **Kind:** conceptual
- **Question:** How does the app keep `threadId` / `assistantId` available across navigations in the agent chat UX?
- **Answer:** Via **nuqs** `useQueryState` — conversation/agent ids live in the URL query string (Stream/Thread providers).
- **Evidence:** `src/providers/Stream.tsx`; `src/components/thread/index.tsx`
- **Accept:** `nuqs` OR `useQueryState`

### Q25

- **Bucket:** architecture · **Kind:** exact
- **Question:** Which LangGraph SDK React UI component loads generative UI cards from auriga-agents?
- **Answer:** `LoadExternalComponent`
- **Evidence:** `src/components/thread/messages/ai.tsx` L4, ~L80
- **Accept:** `LoadExternalComponent`

### Q26

- **Bucket:** architecture · **Kind:** exact
- **Question:** For Agent Engine / MonkeyBot SSE streaming, what are the max retry count and delay used in `useAgentStream`?
- **Answer:** Max 5 retries; 250 ms delay between retries.
- **Evidence:** `src/hooks/useAgentStream.ts` L39–40
- **Accept:** `5`, `250`

### Q27

- **Bucket:** architecture · **Kind:** exact
- **Question:** What does the root `/` route do?
- **Answer:** Redirects to `/home`.
- **Evidence:** `src/app/page.tsx` L3–4
- **Accept:** `/home`

### Q28

- **Bucket:** architecture · **Kind:** conceptual
- **Question:** Does this Next.js app use a traditional `middleware.ts` for edge redirects? Where does www→apex (and related) edge behavior live?
- **Answer:** No `middleware.ts` — edge behavior is in `src/proxy.ts`.
- **Evidence:** `src/proxy.ts`
- **Accept:** `proxy.ts`

### Q29

- **Bucket:** architecture · **Kind:** conceptual
- **Question:** Which doc is the better source of Auriga-specific architecture than the top-level README, and why?
- **Answer:** `REPO_SUMMARY.md` — README still describes upstream LangGraph Agent Chat UI setup; REPO_SUMMARY covers Auriga stack/contracts.
- **Evidence:** `REPO_SUMMARY.md`; `README.md`
- **Accept:** `REPO_SUMMARY`

### Q30

- **Bucket:** architecture · **Kind:** conceptual
- **Question:** Name the main external backends the front end talks to for auth, CRUD/dashboard data, LangGraph agents, and MonkeyBot/Agent Engine.
- **Answer:** Firebase Auth; Auriga Connect / Context Engine; LangGraph; Agent Gateway (MonkeyBot/Agent Engine).
- **Evidence:** `.env.example`; `REPO_SUMMARY.md`; `src/lib/api/contextengine.ts`
- **Accept:** `Firebase`, `Context Engine` OR `Connect`, `LangGraph`, `Gateway` OR `MonkeyBot` OR `Agent Engine`

### Q31

- **Bucket:** domain · **Kind:** conceptual
- **Question:** How does onboarding question content vary for different students?
- **Answer:** Flows branch by grade level (`GradeLevel` 9th–12th) with shared questions plus grade-specific questions.
- **Evidence:** `src/components/onboarding/questions.ts`
- **Accept:** `grade` OR `GradeLevel`

### Q32

- **Bucket:** api · **Kind:** exact
- **Question:** What env var holds the Context Engine / Connect base URL used by CE and SAT clients?
- **Answer:** `NEXT_PUBLIC_CONTEXTENGINE_URL`
- **Evidence:** `src/lib/api/sat.ts`; `.env.example`
- **Accept:** `NEXT_PUBLIC_CONTEXTENGINE_URL`

### Q33

- **Bucket:** ui · **Kind:** exact
- **Question:** What is the dark-mode background hex documented alongside the design system / globals?
- **Answer:** `#1c1c1e` (charcoal).
- **Evidence:** `DESIGN.md` (`dark-surface`); `src/app/globals.css` `.dark` `--background`
- **Accept:** `1c1c1e`

### Q34

- **Bucket:** config · **Kind:** path
- **Question:** Where are the MDX SEO college guides stored (not under `src/`)?
- **Answer:** `content/guides/`
- **Evidence:** `content/guides/README.md`
- **Accept:** `content/guides`

### Q35

- **Bucket:** config · **Kind:** path
- **Question:** Where does the blog MDX content live?
- **Answer:** `content/blog/`
- **Evidence:** `content/blog/README.md`
- **Accept:** `content/blog`

### Q36

- **Bucket:** media · **Kind:** path
- **Question:** Where is the Data Processing Agreement PDF template in the repo?
- **Answer:** `public/legal/auriga-dpa-template.pdf`
- **Evidence:** `public/legal/auriga-dpa-template.pdf`
- **Accept:** `auriga-dpa-template.pdf` OR `public/legal`

### Q37

- **Bucket:** media · **Kind:** path
- **Question:** Name the role-specific auth hero image files under `public/images/` (student/default, parent, counselor).
- **Answer:** `auth-hero-auriga.png`, `auth-hero-auriga-parent.png`, `auth-hero-auriga-counselor.png`
- **Evidence:** `public/images/`
- **Accept:** `auth-hero-auriga`, `parent`, `counselor`

### Q38

- **Bucket:** media · **Kind:** path
- **Question:** Which public image is the SAT coach UI screenshot?
- **Answer:** `public/images/sat-coach-ui.png`
- **Evidence:** `public/images/sat-coach-ui.png`
- **Accept:** `sat-coach-ui.png`

### Q39

- **Bucket:** media · **Kind:** path
- **Question:** Which public image is used for Open Graph / social preview (og image)?
- **Answer:** `public/og-image.png` (referenced from layout metadata).
- **Evidence:** `public/og-image.png`
- **Accept:** `og-image`

### Q40

- **Bucket:** architecture · **Kind:** conceptual
- **Question:** Why might `essay_coach` also skip LangGraph streaming in tests/flags even though SAT/ACT are the explicit id checks?
- **Answer:** Other assistants (including essay coach) can be routed to Agent Engine via `assistantUsesAgentEngineRuntime`; `assistantSkipsLangGraphStream` returns true for those as well after the sat/act checks.
- **Evidence:** `src/config/flags.ts`; `src/config/__tests__/flags.test.ts`
- **Accept:** `Agent Engine` OR `assistantUsesAgentEngineRuntime`

### Q41

- **Bucket:** domain · **Kind:** exact
- **Question:** What URL path is the student agent chat page?
- **Answer:** `/agent` (with `assistantId` query param).
- **Evidence:** `src/app/agent/page.tsx`; sidebar links
- **Accept:** `/agent`

### Q42

- **Bucket:** api · **Kind:** exact
- **Question:** What package initializes the LangGraph production API passthrough on `/api/[..._path]`?
- **Answer:** `@langchain/langgraph-api-passthrough` / `initApiPassthrough` (as used in the route module).
- **Evidence:** `src/app/api/[..._path]/route.ts`
- **Accept:** `initApiPassthrough` OR `langgraph-api-passthrough`

### Q43

- **Bucket:** ui · **Kind:** conceptual
- **Question:** In one sentence, what aesthetic does DESIGN.md say Auriga OS is going for?
- **Answer:** “Ivy League SaaS” — premium/academic/trustworthy (“Digital Acceptance Letter”); paper warmth light mode, charcoal dark mode, burnished orange accent.
- **Evidence:** `DESIGN.md` L1–4
- **Accept:** `Ivy League` OR `Digital Acceptance Letter`

### Q44

- **Bucket:** architecture · **Kind:** path
- **Question:** Where do Playwright e2e / component tests live?
- **Answer:** `tests/`
- **Evidence:** `tests/`
- **Accept:** `tests/`

### Q45

- **Bucket:** config · **Kind:** path
- **Question:** Where is privacy policy markdown content for the app?
- **Answer:** `src/content/privacy-policy.md`
- **Evidence:** `src/content/privacy-policy.md`
- **Accept:** `privacy-policy.md`

### Q46

- **Bucket:** media · **Kind:** path
- **Question:** What audio worklet file under `public/` supports voice interview processing?
- **Answer:** `public/audio-processor-worklet.js`
- **Evidence:** `public/audio-processor-worklet.js`
- **Accept:** `audio-processor-worklet`

### Q47

- **Bucket:** domain · **Kind:** conceptual
- **Question:** How does `normalizeRoutingPersona` treat `high_school_student`?
- **Answer:** Maps to the student routing bucket (same as `student`).
- **Evidence:** `src/lib/routing.ts` L12–30
- **Accept:** `student`

### Q48

- **Bucket:** architecture · **Kind:** conceptual
- **Question:** What is the dual-runtime idea for agents in this frontend?
- **Answer:** Some assistants use LangGraph `useStream` / passthrough; others use Agent Engine / MonkeyBot via `useAgentStream` (SSE) and gateway URLs — selected by feature flags / assistant id.
- **Evidence:** `src/config/flags.ts`; `src/hooks/useAgentStream.ts`; `src/providers/Stream.tsx`
- **Accept:** `LangGraph` AND (`Agent Engine` OR `useAgentStream` OR `MonkeyBot`)

---

## Scoring cheat-sheet

| IDs | Focus |
|-----|--------|
| Q01–Q04 | Auth / session |
| Q05–Q09, Q32, Q42 | API / headers / transports |
| Q10–Q14, Q33, Q43 | Design / UI |
| Q15–Q20, Q31, Q41, Q47 | Domain / agents / onboarding |
| Q21–Q23, Q34–Q35, Q45 | Config / content paths |
| Q24–Q30, Q40, Q44, Q48 | Architecture |
| Q36–Q39, Q46 | Media / public assets |

**Suggested pass bar (Config A baseline):** ≥ 60% exact/path; conceptual may be lower.  
**Config B (FTS + links):** expect gains on conceptual + cross-file items (Q07, Q14, Q29, Q30, Q48).  
**Config C (embeddings):** expect gains on paraphrased conceptual wording when identifiers are absent from the query.

---

## Agent prompt pack

Paste to the agent under test (**questions only** — no answers):

```text
Clone the auriga-web git repository into your workspace (if not already present).
Use **search first** to locate candidates in the workspace index, then verify with
read_file (grep only for exact identifiers). Do not skip search because answers
are in source — that is what the index covers. Answer EVERY question below.
Do not skip any. For each question, reply in exactly this format:

Qxx: <concise answer>
Evidence: <repo-relative-path>

Questions:
Q01: In production, how many minutes of idle time before the AuthProvider signs the user out? Why that duration?
Q02: What Firebase auth persistence mode does the app set by default?
Q03: What storage key is used for the backend Context Engine user id, and where is it stored?
Q04: Besides Firebase auth and email verification, what else can ProtectedRoute use to redirect a signed-in user who has not finished setup?
Q05: Which three HTTP headers does auriga-web attach on Auriga Connect / Context Engine API calls for auth and tenancy?
Q06: How does getIdToken() avoid races when multiple callers need a token while Firebase auth is still initializing?
Q07: Does SAT prep chat/session traffic go through LangGraph streaming? If not, what does it use instead?
Q08: What is the maximum LangGraph API passthrough response size enforced by /api/[..._path]?
Q09: How does Agent Gateway auth relate to Context Engine auth headers?
Q10: What is the primary brand accent hex color in the Auriga “Ivy League SaaS” design system?
Q11: Which fonts does DESIGN.md assign to headlines vs body/UI?
Q12: What CSS variable holds the light-mode paper surface color, and what hex is it?
Q13: What component library style does shadcn use here, and what underlying library powers the Button?
Q14: Why does tailwind.config.cjs include paths under ../auriga-agents/agents/*/ui/**?
Q15: Which URL query parameter selects the agent chat layout, and what layout is used when it is unset?
Q16: List at least five assistantId values that the Thread router documents.
Q17: Which grade levels does student onboarding support as GradeLevel?
Q18: After onboarding/login, where does each persona land for their dashboard home?
Q19: Which assistants does assistantSkipsLangGraphStream always treat as skipping LangGraph stream (by id equality), before the Agent Engine check?
Q20: Where does static college reference JSON live in the repo?
Q21: What is NEXT_PUBLIC_SITE_URL set to in production App Hosting config?
Q22: What site URL does the QA App Hosting overlay use?
Q23: What Firebase App Hosting backend id is configured for this app, and is classic Firebase Hosting enabled?
Q24: How does the app keep threadId / assistantId available across navigations in the agent chat UX?
Q25: Which LangGraph SDK React UI component loads generative UI cards from auriga-agents?
Q26: For Agent Engine / MonkeyBot SSE streaming, what are the max retry count and delay used in useAgentStream?
Q27: What does the root / route do?
Q28: Does this Next.js app use a traditional middleware.ts for edge redirects? Where does www→apex (and related) edge behavior live?
Q29: Which doc is the better source of Auriga-specific architecture than the top-level README, and why?
Q30: Name the main external backends the front end talks to for auth, CRUD/dashboard data, LangGraph agents, and MonkeyBot/Agent Engine.
Q31: How does onboarding question content vary for different students?
Q32: What env var holds the Context Engine / Connect base URL used by CE and SAT clients?
Q33: What is the dark-mode background hex documented alongside the design system / globals?
Q34: Where are the MDX SEO college guides stored (not under src/)?
Q35: Where does the blog MDX content live?
Q36: Where is the Data Processing Agreement PDF template in the repo?
Q37: Name the role-specific auth hero image files under public/images/ (student/default, parent, counselor).
Q38: Which public image is the SAT coach UI screenshot?
Q39: Which public image is used for Open Graph / social preview (og image)?
Q40: Why might essay_coach also skip LangGraph streaming in tests/flags even though SAT/ACT are the explicit id checks?
Q41: What URL path is the student agent chat page?
Q42: What package or helper initializes the LangGraph production API passthrough on /api/[..._path]?
Q43: In one sentence, what aesthetic does DESIGN.md say Auriga OS is going for?
Q44: Where do Playwright e2e / component tests live?
Q45: Where is privacy policy markdown content for the app?
Q46: What audio worklet file under public/ supports voice interview processing?
Q47: How does normalizeRoutingPersona treat high_school_student?
Q48: What is the dual-runtime idea for agents in this frontend?
```
