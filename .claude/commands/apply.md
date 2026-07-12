---
description: Tailor a resume for a job posting URL, compile the PDF, and log it to applications.md
---

The user gives a job-posting URL (or pasted JD text) as: $ARGUMENTS

Run this pipeline:

1. **Fetch the JD.** WebFetch the URL (fall back to `curl -sL` if blocked).
   Extract: company, exact role title, location, cycle/season, required
   qualifications, preferred qualifications, any short-answer/essay questions.

2. **Load context.** Read `applications.md` (integrity issues at top are
   binding) and skim the relevant parts of `career-history.md`. Style rules
   (from memory, non-negotiable): no em-dashes, no "this, not that"
   constructions, every bullet exactly 1 or exactly 2 rendered lines, human
   voice. NEVER fabricate metrics or claims; if a preferred qualification is
   almost-true, tell Alex the small task that would make it true instead of
   writing it. Never mention SPY-6 classified details or IWS briefings —
   "naval radars" + generic radar theory only.

3. **Pick the base resume** from `resumes/` (closest target: quant dev /
   quant research / SWE-defense / FDSE / cyber). Ask Alex only if genuinely
   ambiguous. Confirm the local .tex is current if the role is high-priority
   (his Overleaf may be ahead).

4. **Tailor with minimal targeted edits** — reorder/bold/swap bullets rather
   than rewrite. Cover every preferred qualification that is truthfully
   claimable and **bold** those phrases. Save as
   `resumes/out/Epstein_Alexander_<Company>_<Role>.tex` (never overwrite the
   base version).

5. **Compile:** `tectonic <file>.tex` in `resumes/out/`. Fix LaTeX errors
   (common: bare `&` must be `\&`). Verify it stays ONE page
   (`pdfinfo` or page count via python). Open the PDF for review with
   `open <file>.pdf`.

6. **Report a coverage table:** each JD requirement/preferred qual → where the
   resume now addresses it, or "gap (honest)" / "gap (30-min task would fix)".

7. **Draft any short-answer/essay questions** in Alex's voice (see
   career-history.md §4 and §7.3 for his voice samples). Show as drafts for
   his edit — never auto-submit prose.

8. **Log it:** add/update the entry in `applications.md` with status
   `prepared`, the resume filename, the date, and any deadline found in the
   JD. When Alex later says "sent"/"applied", flip the status to `applied`.

9. **Hand off:** tell Alex the PDF path. If he wants wording changes he edits
   the .tex directly (VS Code) and says "recompile", or tells you the change.
