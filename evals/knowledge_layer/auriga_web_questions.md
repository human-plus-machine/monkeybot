
clone [auriga-web](https://github.com/human-plus-machine/auriga-web) into the workspace.  

## Instructions

Use **`search` first** to locate candidates in the workspace index, then verify with
`read_file` (and `grep` only for exact identifiers). Do not skip `search` because
answers are in source — that is what the index covers. Answer **every** question.

> F21 unprompted variant: run the same pack with this Instructions block removed
> (questions only) and measure unprompted `search` usage via `recall_rank_report.py`.

For each question, reply in exactly this format:

```text
Qxx: <concise answer>
Evidence: <repo-relative-path>
```

---

## Questions

**Q01:** In production, how many minutes of idle time before the AuthProvider signs the user out? Why that duration?

**Q02:** What Firebase auth persistence mode does the app set by default?

**Q03:** What storage key is used for the backend Context Engine user id, and where is it stored?

**Q04:** Besides Firebase auth and email verification, what else can ProtectedRoute use to redirect a signed-in user who has not finished setup?

**Q05:** Which three HTTP headers does auriga-web attach on Auriga Connect / Context Engine API calls for auth and tenancy?

**Q06:** How does getIdToken() avoid races when multiple callers need a token while Firebase auth is still initializing?

**Q07:** Does SAT prep chat/session traffic go through LangGraph streaming? If not, what does it use instead?

**Q08:** What is the maximum LangGraph API passthrough response size enforced by /api/[..._path]?

**Q09:** How does Agent Gateway auth relate to Context Engine auth headers?

**Q10:** What is the primary brand accent hex color in the Auriga “Ivy League SaaS” design system?

**Q11:** Which fonts does DESIGN.md assign to headlines vs body/UI?

**Q12:** What CSS variable holds the light-mode paper surface color, and what hex is it?

**Q13:** What component library style does shadcn use here, and what underlying library powers the Button?

**Q14:** Why does tailwind.config.cjs include paths under ../auriga-agents/agents/*/ui/**?

**Q15:** Which URL query parameter selects the agent chat layout, and what layout is used when it is unset?

**Q16:** List at least five assistantId values that the Thread router documents.

**Q17:** Which grade levels does student onboarding support as GradeLevel?

**Q18:** After onboarding/login, where does each persona land for their dashboard home?

**Q19:** Which assistants does assistantSkipsLangGraphStream always treat as skipping LangGraph stream (by id equality), before the Agent Engine check?

**Q20:** Where does static college reference JSON live in the repo?

**Q21:** What is NEXT_PUBLIC_SITE_URL set to in production App Hosting config?

**Q22:** What site URL does the QA App Hosting overlay use?

**Q23:** What Firebase App Hosting backend id is configured for this app, and is classic Firebase Hosting enabled?

**Q24:** How does the app keep threadId / assistantId available across navigations in the agent chat UX?

**Q25:** Which LangGraph SDK React UI component loads generative UI cards from auriga-agents?

**Q26:** For Agent Engine / MonkeyBot SSE streaming, what are the max retry count and delay used in useAgentStream?

**Q27:** What does the root / route do?

**Q28:** Does this Next.js app use a traditional middleware.ts for edge redirects? Where does www→apex (and related) edge behavior live?

**Q29:** Which doc is the better source of Auriga-specific architecture than the top-level README, and why?

**Q30:** Name the main external backends the front end talks to for auth, CRUD/dashboard data, LangGraph agents, and MonkeyBot/Agent Engine.

**Q31:** How does onboarding question content vary for different students?

**Q32:** What env var holds the Context Engine / Connect base URL used by CE and SAT clients?

**Q33:** What is the dark-mode background hex documented alongside the design system / globals?

**Q34:** Where are the MDX SEO college guides stored (not under src/)?

**Q35:** Where does the blog MDX content live?

**Q36:** Where is the Data Processing Agreement PDF template in the repo?

**Q37:** Name the role-specific auth hero image files under public/images/ (student/default, parent, counselor).

**Q38:** Which public image is the SAT coach UI screenshot?

**Q39:** Which public image is used for Open Graph / social preview (og image)?

**Q40:** Why might essay_coach also skip LangGraph streaming in tests/flags even though SAT/ACT are the explicit id checks?

**Q41:** What URL path is the student agent chat page?

**Q42:** What package or helper initializes the LangGraph production API passthrough on /api/[..._path]?

**Q43:** In one sentence, what aesthetic does DESIGN.md say Auriga OS is going for?

**Q44:** Where do Playwright e2e / component tests live?

**Q45:** Where is privacy policy markdown content for the app?

**Q46:** What audio worklet file under public/ supports voice interview processing?

**Q47:** How does normalizeRoutingPersona treat high_school_student?

**Q48:** What is the dual-runtime idea for agents in this frontend?
