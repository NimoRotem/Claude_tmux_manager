# tmux Dashboard Technical Specification

## Purpose

The dashboard manages owner-scoped coding conversations running in tmux. It preserves exact Codex rollout roots, restores durable tabs after process or host restarts, and exposes chat, terminal, project, voice, account, and browser controls through a FastAPI application. Chat and Terminal are views of the same coding session, not separate conversations.

## Session identity and close

- Every managed tab is bound to an explicit owner, lifecycle generation, project working directory, and, once available, an exact user-root Codex rollout UUID.
- Create, restore, park, resume, and delete operations revalidate owner and generation under per-session and tmux-server mutation fences.
- Interactive Close warns that running work will stop, then directly requests the owner-scoped session DELETE operation. It does not launch an asynchronous archival or summarization job, wait for work to become idle, or write a project specification.
- The controller durably records deletion intent before terminating the exact tmux session and its child processes. It confirms that the intended tmux identity is absent before reporting completion. Ambiguous tmux state or reuse of the same session name fails closed; retained deletion intent prevents automatic resurrection after a partial failure.
- The browser removes a closed tab only after a successful response explicitly reports `ok: true` and `killed` equal to the requested session name. A transport failure or unconfirmed result leaves the UI in an error state with a refresh action, not an assumed success.
- Closing a coding session does not delete its project files or ordinary retained conversation history. Operational account cleanup is a separate controller path.

## Chat, Terminal, and mobile controls

- Chat is the default view for members and administrators. User messages appear on the right and brief assistant replies on the left. Assistant previews use a few plain-language sentences, with code, command output, reasoning, and tool logs excluded. Longer prose expands through Read more; Terminal remains available for technical output.
- A completed turn produces a final reply. During a long-running turn, useful new assistant prose can produce a separate progress message about every 20 minutes. Unchanged output does not generate repeated updates, and progress is not treated as completion. Background collection and retained messages preserve updates while the browser is closed. Summarizer failures fall back to concise assistant prose without inventing results.
- Chat and Terminal share one persistent composer, attachment tray, and status strip per session. Switching views preserves unsent text, selection, attachments, pending sends, and recording state. The strip reports measured activity and elapsed time, last-turn duration, cumulative session tokens, and context consumption. Missing measurements remain unavailable rather than fabricated as zero.
- Dictation uses the microphone icon when the composer is empty. Typed text or attachments change that button to Send. An adjacent waveform icon opens Voice Mode without replacing or submitting the draft.
- On mobile, a pinned green New session button stays outside the scrolling session tabs. Composer action buttons remain touch-sized and the mobile input is capped at four visible lines with internal scrolling.
- Terminal clean view suppresses tool plumbing, traceback output, transcript-navigation notices, and file-change previews, including wrapped edit headers and clipped diff continuations. It preserves genuine user messages and intentional conversational examples. Turning clean view off retains the unfiltered technical transcript.
- Saved terminal history is paged from the exact owner-bound root and is distinct from live tmux output. My last message, Freeze, and Latest are reading controls; they do not send keys or instructions to the coding worker.
- Paste delivery recognizes collapsed and wrapped live composer content, uses the coding client's queue when available, and verifies that the draft left the composer. Historical echoes, pickers, or another draft do not justify retrying Enter. A draft that remains stranded is not reported as successfully delivered.

## Navigation, names, and account preferences

- Desktop navigation exposes CPU, memory, and available Codex plan usage measurements. Detailed status remains reachable on mobile without crowding out session creation or browser controls.
- The browser badge distinguishes disconnected, connected, and currently working states using the signed-in account's browser and its active leases. Members reach their own browser settings; administrative browser management remains restricted to administrators.
- Rename changes only the visible session label. The stable session route, tmux name, project directory, owner, generation, and rollout root do not change. Manual labels are persisted under owner and generation checks and are not overwritten by delayed automatic naming.
- Account Preferences controls automatic session naming. The default preserves automatic naming; disabling it prevents subsequent automatic renaming without clearing manual labels. Preferences are private to the signed-in account.
- Saving a model or reasoning choice is distinct from restarting a running worker. Settings restarts briefly wait for a busy session before touching the coding worker; persistent contention returns an explicit busy response with the choice still saved. Waiting never retries terminal writes, and owner, generation, working directory, and conversation-root checks remain mandatory.

## Saved project details

- Saved for this project exposes useful URLs, credentials already present in the conversation, and project or uploaded file references, with copy and open controls. It is available in the shared composer drawer for members and administrators.
- Collection is bounded and incremental. It reads authorized user and assistant prose, declared file-change paths, current session notes, and regular uploads; it does not scan unrelated projects, private reasoning, arbitrary tool output, or credential stores. Exact captured values are retained without LLM rewriting.
- Collection, persistence, and response delivery are fenced by the signed-in owner, lifecycle generation, and exact conversation root. A changed identity or invalid known root fails closed rather than falling back to a sibling transcript. A new rootless session may expose its own verified uploads and current messages.
- Saved state is private account data, not a repository artifact. Reads and atomic writes reject symlinked or unsafe state files. Responses are private and not cached; credential values must not be logged or included in public documentation.
- Displayed links do not grant additional access. File opening remains subject to the dashboard's session and file authorization rules. Browser polling applies only to the selected visible session, and stale responses must not populate a replacement tab.

## Administrative usage

- The administrator Users view can report prompt and token counts for All time, Today, and the preceding 2, 5, or 7 days. Today begins at midnight UTC; the numbered periods are rolling day windows. All time remains the default.
- Period selection is validated server-side and does not weaken the administrator-only boundary. Counts come from retained account prompt history and coding usage events. Input and output contribute to the total; cached input and reasoning are subsets, not additional tokens to count twice.

## Browser routing and account isolation

- New browser identities use a direct connection by default. Residential routing is an explicit exception after a direct attempt fails, not an automatic fallback. Enabling it requires a strict affirmative direct-failure acknowledgement.
- Routing changes preserve per-browser opt-in flags, sticky identities, profiles, and unrelated accounts' timezone caches. Changing the default browser's route does not silently enable or disable another account's route. Existing explicit routing choices are preserved.
- Proxy configuration and route mutation remain administrator-only. Provider credentials stay in private server-side configuration; status responses expose whether a password is configured, never the password. Browser access, leases, and live viewing remain bound to the signed-in account.

## Voice conversation

- Voice connects only after an explicit user action and microphone permission. Dictation and Voice Mode cannot hold the microphone simultaneously, including while a permission request is pending. Closing voice, removing its session, or leaving the page releases media and connections. Switching Chat and Terminal preserves the shared composer.
- Browser audio travels over WebRTC. Session creation and delegated application controls use authenticated, same-origin server routes. The OpenAI credential remains server-side. Audio and voice transcripts are not persisted by the dashboard; approved instructions become ordinary messages in the coding conversation. Diagnostic storage contains bounded transport metadata only.
- Voice history and actions are bound to the signed-in owner and the exact session generation and conversation root. Mutations revalidate that binding under the existing session operation lock. Stop is additionally bound to its own connection identity, so a stale panel cannot end a newer connection. Passive progress information does not grant permission to send instructions or interrupt work.
- Ordinary Voice Mode is the safe default. Connecting or ending it does not restart the coding worker, send setup or continuation prompts, change autonomous modes, or create testing or deployment permissions. Explicit voice directions follow the same delivery and approval rules as typed directions. Without separately enabled supervision, interruption is reported from the server's observed result and is not a durable hold on future work.
- Voice supports continuous listening and push-to-talk, mute, and opt-in updates from other sessions belonging to the same owner. Disconnection stops voice transport, not the coding task. A fresh connection must revalidate the session before resuming voice.

### Explicit advanced voice controls

- Managed supervision and the three-minute pre-test check-in are separate, explicit options. Neither is enabled by opening or connecting ordinary Voice Mode. Their controls require a fresh browser action bound to the connection, owner, generation, and root; a delegated model tool or background update cannot enable them.
- Enabling managed supervision discloses that the coding worker may restart and requires explicit confirmation. It is available only where the managed tool hook can be installed and verified against the same owner, generation, and conversation root. A failed handshake cannot be reported as an active hold.
- An explicitly supervised tool hold survives End, network loss, and reconnection to the same conversation. Reconnecting does not automatically enable advanced options or release that hold. The user can select Release existing tool hold or disable managed controls without sending work. An explicit revised voice instruction to the attached session may also release its hold, but only after observed delivery with the same prior hold and revision. Ordinary typing after voice has ended is not a release action; releasing another session's hold requires its own explicit resume confirmation.
- Unpaused supervision from an ended connection does not permanently suspend automatic dashboard prompts and is not silently re-enabled by ordinary reconnection. A failed restart leaves supervision unready, never an invented successful hold. Explicit holds remain protected until deliberately released.
- Cross-session supervision requires the attached connection's explicit supervision opt-in, the user's complete current spoken request naming the target and supervision or restart, and a second affirmative answer to an exact confirmation disclosing the target restart. It never transfers the microphone or changes the attached session. Existing target voice controls cannot be replaced by this operation.
- Cross-session actions are single-use and bind both source and target owner, generation, conversation root, control revision, and connection identity. Changed work, changed history, expired confirmation, opt-out, or replacement invalidates the authorization. Durable state changes compare both guards under ordered session operation locks. Restart readiness is confirmed by the target hook, not by a sent restart request; no setup or resume prompt is automatically injected.
- The pre-test check-in is connection-local and attached-session-only. It recognizes the exact question: "Any other feature tweaks before I test?" After 180 seconds without input, it may send one scoped continuation only if that question and task revision are still current, the coding worker is idle, voice is connected, and no hold or pending action intervenes. Fresh input resets the interval; a hold, cancellation, changed work, or disconnect invalidates it.
- A check-in timeout permits only already-requested testing and the normal workflow under existing project rules. It is not deployment consent, does not grant sensitive-action permissions, and cannot override an explicit restriction or required approval. Another session's update cannot start or satisfy this check-in.
