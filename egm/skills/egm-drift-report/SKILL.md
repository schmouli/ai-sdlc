---
name: egm-drift-report
license: Apache-2.0
disable-model-invocation: true
description: >-
  Runs drift detection on all design documents to check for document and
  code consistency. Produces an actionable report with differences. Use when the
  user asks to detect drift or check for consistency across all design documents
  or READMEs in a directory.
---
> **Copilot adapter — read this path map before following the steps below.**
> This skill is invoked as `/egm-drift-report` in Copilot Chat (JetBrains, VS Code, CLI).
> Paths in this skill are written relative to the EGM plugin root. Resolve them:
>
> - Plugin root: `C:/Users/schmo/.copilot/egm`
> - `agents/<x>.md` -> `C:/Users/schmo/.copilot/egm/agents/<x>.md`
> - `resources/...` -> `C:/Users/schmo/.copilot/egm/resources/...`
> - `docs/...` -> `C:/Users/schmo/.copilot/egm/docs/...`
> - `references/...` -> relative to this skill folder (already copied beside this file)
> - Harness guide: `C:/Users/schmo/.copilot/egm/docs/copilot.md`
>
> Dispatching a Goldfish: enable the `runSubagent` tool, then name the custom
> agent in the prompt (for example "Use the egm-zoom-out subagent for this
> review") and pass absolute paths. If the named agent is not offered, call
> `runSubagent` with a general-purpose prompt that starts by reading the persona
> path yourself. Either way never summarise this chat into the prompt: a reviewer
> with conversation history is not a Goldfish review. Never run a review in the
> main context.
>
> Copilot subagents cannot spawn further subagents, so dispatch reviewers in
> parallel from the main chat and compile their findings there instead of
> relying on `egm-synthesizer`.
>
> Gates: Copilot has no structured question tool. Ask exactly one question per
> turn as plain text, number the options (1., 2., ...), and end the turn. Map
> the gate payload below as `question` -> your question, each option string ->
> one numbered option, `is_multi_select: false` -> the user picks exactly one.
> Wherever the steps below name a structured question tool (`ask_question`, a
> question payload, `len(questions) == 1`), use this numbered plain-text gate.
> No `(Recommended)` prefixes.


# Detect Drift In Designs and Code

When the user asks to detect drift across all designs or a set of READMEs, the
goal is to identify all instances where the codebase has diverged or failed to
implement the specifications described by those designs or READMEs.

1.  **Check Input**: Verify a directory containing design documents or READMEs
    is provided. If not provided, ask the user to specify the target directory.
2.  **List Documents**: List all files in the design directory.
3.  **Analyze**: For EACH design document:

    *   **Contextualize**: Read the design document. Identify the intent,
        proposed changes, and specific filenames/symbols mentioned.
    *   **Locate Source**: Find the corresponding source code files in the
        source tree. If paths aren't explicit, use code search or grep to
        locate the relevant classes, functions, or API endpoints mentioned in
        the doc. Prompt the user to add missing links to the document to make
        future detection cheaper.
    *   **Verify**: Read the current state of those source files.
    *   **Detect Drift**: Compare the design vs. reality. Look specifically for:
        *   Missing Features: Logic described in the doc but absent in the code.
        *   Incomplete Models: Dataclasses/Classes/Structs/Protos missing fields
            specified in the design.
        *   Logic Shortcuts: Hardcoded values where dynamic logic was planned.
        *   Architectural Divergence: Implementation using different
            patterns/libraries than proposed.
    *   **Filter**: Report ONLY the differences. Do not describe what is working
        or what matches. If the code matches the design perfectly, output "No
        drift detected."

4.  **Report**: Produce a single report listing all discrepancies found.

    *   Append the analysis for each file to `{YYYY-MM-DD}_DRIFT_REPORT.md`
        using your file-writing tool.
    *   Strict Format:

        ```markdown
        ## <DESIGN_DOC_NAME>

        <Bulleted list of specific divergences found>
        <Or "No drift detected">
        ```

5.  **Guidance**

    -   Be rigorous. If a design doc mentions a specific object, field or file
        name and the code doesn't have it, that is a reportable drift. Double
        check for coverage. If there is some part of the logic that is not
        referenced in a design or README, this is a reportable drift.
    -   Read files liberally to ensure you are looking at the current state
        of the code, not relying on memory.
    -   Process the files sequentially to ensure the tool output buffer doesn't
        overflow.
