# Away Mode: Complete Skill & Scenario Catalog

An exhaustive reference of everything a coding agent can do autonomously while the developer is away. Organized by category. Each category maps to one or more skills/tools the agent can execute.

---

## 1. Live QA & Runtime Testing

Actually run the project, interact with it like a real user, and find real problems.

- Launch the app/site and crawl every route/page, recording HTTP status codes, console errors, unhandled exceptions, and failed network requests
- Screenshot every page at multiple viewport sizes (mobile, tablet, desktop) and flag visual breakage — overlapping elements, text overflow, cut-off content, horizontal scroll
- Test every form submission with valid data, invalid data, empty data, and extremely long input — log which ones crash, show poor error messages, or silently fail
- Click every button and link on every page, verify they do something or go somewhere (no dead buttons, no `href="#"` that does nothing)
- Test authentication flows end-to-end — signup, login, logout, password reset, session expiry, "remember me"
- Test authorization — can a logged-out user hit authenticated API endpoints directly? Can a regular user access admin routes?
- Navigate the full user journey for each core feature (e.g., add to cart → checkout → payment → confirmation), verifying each step works and data persists correctly
- Test file upload functionality with various file types, oversized files, zero-byte files, files with special characters in names, and no file at all when one is expected
- Test what happens when the user rapidly double-clicks submit buttons — does it create duplicate entries?
- Test browser back/forward button behavior — does state break? Do forms re-submit? Do modals get stuck open?
- Test deep linking — paste any URL directly into a fresh browser session, verify it loads correctly without needing prior navigation state
- Test with cookies disabled, JavaScript disabled, and ad blockers active — does the site degrade gracefully or break completely?
- Run the project's own test suite (unit, integration, e2e) that may not have been run recently and report any failures
- If the project has seed data or fixtures, verify they still work and produce a usable app state
- Test WebSocket connections if applicable — do they connect, stay alive, reconnect after drops?
- Test API responses for consistency — are error formats uniform? Do all endpoints return proper status codes or do some return 200 with an error body?
- Monitor memory usage over time during interaction — detect memory leaks from navigation, repeated actions, or never-closed event listeners
- Test concurrent user simulation — open multiple sessions doing different things simultaneously, check for race conditions
- Test what happens when the backend is slow or unreachable — does the frontend hang forever, show a spinner indefinitely, or handle it gracefully?
- Test copy/paste behavior in text fields — does pasting rich text cause formatting issues? Does pasting into number fields allow non-numeric input?
- Test drag-and-drop features with edge cases — dropping outside the target area, dropping the same item twice, dragging many items at once
- Test search functionality — empty queries, special characters, SQL-like syntax, extremely long queries, queries that match nothing
- Test pagination — first page, last page, page beyond the last, page zero, page negative one
- Test sorting and filtering — does applying a filter and then sorting work? Does clearing a filter restore the original state?
- Test undo/redo if supported — does it actually reverse the action completely? Can you undo past a save point?
- Test notification systems — do notifications actually appear? Can they be dismissed? Do they stack properly when many fire at once?
- Open every modal/dialog/drawer in the app and verify they can be closed (X button, escape key, clicking outside), and that focus returns to the trigger element
- Test idle/timeout behavior — leave the app open for an extended period, then try to use it. Does the session handle expiry gracefully?
- Test what happens when localStorage/sessionStorage is full or unavailable
- Test international characters in every text input — Chinese, Arabic, emoji, zero-width characters, right-to-left text

---

## 2. Performance & Speed

Measure and improve how fast things load and run.

- Run Lighthouse (or equivalent) on every page and log performance scores, flagging anything below threshold
- Measure and record Time to First Byte, First Contentful Paint, Largest Contentful Paint, Cumulative Layout Shift, Interaction to Next Paint for every route
- Find oversized images — any image served at 2000px when displayed at 200px, any PNG that should be WebP/AVIF, any uncompressed asset
- Check if images have explicit width/height attributes set (prevents layout shift)
- Detect render-blocking CSS and JavaScript in the critical path
- Check if the project uses lazy loading for below-the-fold images and heavy components
- Verify static assets have proper cache headers set and add them if missing
- Analyze JavaScript bundle size — are we shipping moment.js when we use one date function? Find tree-shaking opportunities
- Profile server-side response times for each API endpoint under normal load, flag slow ones (>500ms)
- Run a basic load test — simulate 50, 100, 500 concurrent users and find where throughput degrades or errors begin
- Detect N+1 database query patterns by instrumenting ORM calls during typical request flows
- Check if database queries use indexes by running EXPLAIN on the most common queries
- Find synchronous operations that block the event loop (Node.js) or main thread
- Check if gzip/brotli compression is enabled for text-based responses
- Verify CDN configuration if applicable — are static assets served from the CDN or falling back to origin?
- Check if DNS prefetch, preconnect, and preload hints are used for critical third-party resources
- Detect if the app re-fetches data that hasn't changed (missing ETags, no client-side cache, polling when nothing changed)
- Find React/Vue/Svelte components that re-render unnecessarily on every state change
- Check if CSS loaded on the current page includes large amounts of unused rules
- Measure and optimize cold start time for serverless deployments
- Check if fonts are optimized — using `font-display: swap`, subsetting to only needed characters, preloading critical fonts
- Detect JavaScript long tasks (>50ms) that block interactivity
- Time every user-facing action — click a button, how long until the result appears? Submit a form, how long until confirmation? Flag anything over 1 second that doesn't show a loading indicator
- Check if the app prefetches data for likely next pages (e.g., hovering over a link could prefetch that route)
- Identify API calls that could be parallelized but are sequential (fetching user profile, then fetching settings, when both could happen simultaneously)
- Check if the app loads all data upfront on page load when it could load on demand (e.g., loading all tabs' content when only the first tab is visible)
- Profile CSS selector performance — deeply nested selectors, universal selectors in large DOMs
- Check for expensive CSS properties causing layout thrashing (e.g., reading `offsetHeight` then writing styles in a loop)
- Replace `O(n²)` patterns with `O(n)` alternatives where the transform is well-known (nested array lookups → Map/Set, repeated `.find()` in a loop → index first)
- Add memoization/caching to pure functions called repeatedly with the same arguments
- Replace synchronous file I/O with async equivalents
- Replace string concatenation in hot loops with buffer/builder patterns
- Lazy-load heavy imports that aren't needed at startup

---

## 3. SEO & Web Standards

Ensure the project is discoverable, well-structured, and standards-compliant.

- Verify every page has a unique, descriptive `<title>` tag and meta description of appropriate length
- Check for missing or duplicate `<h1>` tags — every page should have exactly one
- Verify heading hierarchy (h1 → h2 → h3) is logical and doesn't skip levels
- Check that all images have meaningful alt text (not empty, not "image", not the filename)
- Verify Open Graph and Twitter Card meta tags are present and correct on shareable pages
- Check for a valid sitemap.xml and robots.txt
- Verify canonical URLs are set correctly, especially for pages accessible via multiple URLs
- Check that the site returns proper 404 pages (not a blank page, not a redirect to home, not a generic error)
- Test structured data / JSON-LD markup if present — validate against schema.org
- Verify all internal links use consistent URL format (trailing slash or not, www or not)
- Check that pagination uses `rel="next"` / `rel="prev"` or is otherwise crawlable
- Verify the site is accessible over HTTPS and HTTP redirects to HTTPS properly
- Check for mixed content warnings (HTTPS page loading HTTP resources)
- Verify hreflang tags if the site has multiple languages
- Check that dynamic/JS-rendered content is visible to search engine crawlers (test with rendering disabled)
- Validate HTML markup against W3C standards — unclosed tags, invalid nesting, deprecated elements
- Check that the site has a proper favicon in all required sizes and formats (ICO, PNG, apple-touch-icon, webmanifest)
- Verify that page URLs are human-readable and descriptive (not `/page?id=4829`)
- Check that 301 redirects exist for any old URLs that have changed
- Verify that social sharing previews look correct by fetching and rendering OG metadata
- Check that the site loads and renders meaningful content without JavaScript (for SEO crawlers)
- Verify that meta robots tags aren't accidentally blocking indexing on pages that should be indexed

---

## 4. Accessibility

Make the project usable by everyone.

- Run axe-core or similar on every page and catalog all violations by severity
- Check every interactive element is keyboard-navigable — tab to it, activate with Enter/Space, see a visible focus indicator
- Verify color contrast ratios meet WCAG AA (4.5:1 for normal text, 3:1 for large text)
- Check that all form inputs have associated `<label>` elements (not just placeholder text as labels)
- Verify ARIA roles and attributes are used correctly and not randomly
- Test with a screen reader (or simulation) — does content make sense read linearly?
- Check that dynamic content changes (modals, alerts, live updates) are announced to assistive technology via live regions
- Verify all functionality available by mouse is also available by keyboard
- Check that focus is trapped correctly inside modals and returned to trigger element on close
- Verify error messages are associated with their form fields programmatically, not just visually by proximity
- Check that media (video/audio) has captions or transcripts
- Verify skip navigation links exist for keyboard and screen reader users
- Check that touch targets on mobile are at least 44x44px
- Test with browser zoom at 200% — does the layout still work without horizontal scrolling?
- Check that status messages, loading indicators, and progress updates are communicated to screen readers
- Verify that decorative images use `alt=""` and informative images have descriptive alt text
- Check that data tables use proper `<th>`, `scope`, and `<caption>` elements
- Verify that autoplaying media can be paused and doesn't play audio automatically
- Check that time-limited interactions offer extensions or can be disabled
- Test that content doesn't rely solely on color to convey meaning (e.g., red/green for error/success without icons or text)

---

## 5. Security Auditing & Hardening

Find and fix vulnerabilities.

- Run dependency vulnerability scanners (npm audit, pip-audit, cargo audit, Snyk) and report/fix known CVEs
- Scan for hardcoded secrets — API keys, passwords, tokens, connection strings — in code, config files, and git history
- Check that environment variables are used for all secrets and `.env` files are gitignored
- Verify all API endpoints validate and sanitize input, not just trusting client data
- Check for SQL injection surfaces — raw string concatenation in database queries
- Check for XSS vectors — user input rendered without escaping in HTML/templates
- Verify CSRF protection is enabled on all state-changing endpoints
- Check that authentication tokens have reasonable expiry times
- Verify password hashing uses a strong algorithm (bcrypt, argon2) not MD5/SHA1
- Check that sensitive data isn't logged (passwords, tokens, PII in server logs)
- Verify HTTP security headers: Content-Security-Policy, X-Frame-Options, Strict-Transport-Security, X-Content-Type-Options, Referrer-Policy, Permissions-Policy
- Check that cookies have Secure, HttpOnly, and SameSite flags set appropriately
- Verify rate limiting exists on authentication endpoints, password reset, and abuse-prone routes
- Check that error messages don't leak internal details (stack traces, DB schemas, file paths) to end users
- Verify file upload endpoints restrict file types, sizes, and prevent path traversal
- Check that CORS is configured restrictively (not `Access-Control-Allow-Origin: *` on authenticated endpoints)
- Scan for exposed debugging endpoints or admin panels with weak or no authentication
- Check if source maps are exposed in production (leaking source code to anyone who opens devtools)
- Verify that lock files don't contain known vulnerable dependency resolutions
- Check for open redirect vulnerabilities in login/redirect flows
- Verify admin/internal APIs are not accessible from public networks
- Check that session tokens are regenerated after login (prevent session fixation)
- Verify that account enumeration isn't possible via login, registration, or password reset error messages
- Check that file downloads are served with correct Content-Disposition and Content-Type headers (prevent content sniffing attacks)
- Scan for prototype pollution vulnerabilities in JavaScript code
- Verify that subresource integrity (SRI) hashes are used for CDN-loaded scripts

---

## 6. Content & Data Integrity

Verify the actual content of the project is correct and consistent.

- Crawl every link (internal and external) and flag broken links (404s, 5xx, timeouts, DNS failures)
- Find references to outdated dates — "Copyright 2023", "Updated January 2024", "Upcoming event: March 2025"
- Detect contradictions across pages — one page says "14-day free trial" while another says "30-day free trial"
- Find placeholder content that was never replaced — "Lorem ipsum", "[COMPANY NAME]", "TODO: write this", "example@email.com", "John Doe"
- Check for orphaned pages — pages that exist but aren't linked from anywhere in the navigation or content
- Validate email addresses and phone numbers in content are properly formatted
- Check that pricing information is consistent across all pages
- Verify team/about pages have current information
- Check that legal pages exist and have real content — privacy policy, terms of service, cookie policy
- Scan for typos and grammatical errors in user-facing content
- Verify all downloadable assets (PDFs, documents) are actually accessible and not corrupted or zero-byte
- Check that image thumbnails and previews match their full-size versions
- Verify timezone handling — are dates displayed correctly for different user timezones?
- Check that number formatting (currency, decimals, thousands separators) is consistent and locale-appropriate
- Detect duplicate content — same paragraph appearing verbatim on multiple pages
- Verify RSS feeds, API documentation, and machine-readable content is valid and current
- Check that footer information matches header information (same nav links, same contact info)
- Find images with broken src paths or images that return 404
- Check that social media links point to active, correct profiles
- Verify that "last updated" or "published" dates on content pages are accurate relative to git history
- Check for hardcoded URLs that should be relative or environment-based (e.g., `http://localhost:3000` in production content)

---

## 7. Dependency & Ecosystem Management

Keep the project's dependencies healthy and current.

- Check every dependency for newer versions — separate into patch (safe), minor (usually safe), and major (needs review)
- Auto-apply patch version updates, run full test suite, commit only those that pass
- Attempt minor version updates one at a time, test each, commit successes, revert and log failures with error details
- Identify dependencies that are unmaintained (no commits in 1+ year, deprecated notices)
- Find dependencies that could be replaced by smaller, better-maintained alternatives
- Check for dependencies that duplicate functionality already in the standard library
- Detect multiple versions of the same package in the dependency tree
- Verify lock files are consistent with package manifests (no drift)
- Check for dependencies with restrictive licenses that conflict with the project's license
- Identify vendored/copied code that has upstream updates available
- Check if build tools themselves are outdated (webpack 4 → 5, Create React App → Vite, etc.)
- Look for polyfills no longer needed given the project's browser/runtime support targets
- Audit transitive dependencies for known issues
- Check if dependency install size is reasonable — flag dependencies that pull in hundreds of MB for a small feature
- Verify that optional/peer dependencies are documented and handled
- Check if the project would benefit from dependency pinning (exact versions vs ranges)

---

## 8. Testing & Coverage

Expand and strengthen the test suite.

- Measure current test coverage and map exactly which files, functions, and branches have zero tests
- Generate unit tests for untested utility functions by analyzing their signature, implementation, and call sites
- Generate edge case tests — null, undefined, empty string, empty array, negative numbers, very large inputs, unicode, special characters
- Generate integration tests for API endpoints by reading route handlers and producing request/response pairs
- Create snapshot tests for UI components that have no visual regression testing
- Generate property-based tests where function contracts can be inferred
- Run mutation testing — modify code slightly and verify tests catch the mutations (find tests that pass no matter what)
- Find flaky tests — run the suite multiple times and flag tests that intermittently fail
- Check for tests that don't actually assert anything (test runs but has no expectations)
- Check for tests that mock so heavily they don't test real behavior
- Generate database integration tests using test fixtures
- Create smoke tests for critical user paths if none exist
- Write regression tests for bugs fixed in recent git history that lack corresponding tests
- Test error paths — verify that error handling code is actually exercised by the test suite
- Check for tests that depend on execution order (will fail if run individually)
- Find tests that depend on the current date/time and will break in the future
- Generate API contract tests that verify request/response schemas match documentation
- Check test isolation — does each test clean up after itself or do tests leak state?

---

## 9. Code Quality & Refactoring

Improve readability, maintainability, and structure.

- Extract duplicated code blocks into shared utility functions (with tests written first)
- Break files over 500 lines into smaller modules along natural seams
- Rename unclear variables and functions to descriptive names using scope-safe renaming
- Simplify deeply nested conditionals into early returns or guard clauses
- Replace magic numbers and strings with named constants
- Convert callback-based code to async/await
- Replace hand-rolled utilities with standard library or well-known library equivalents
- Remove dead code — functions never called, variables never read, feature flags permanently on/off
- Remove commented-out code blocks (preserved in git history)
- Simplify overly complex expressions — long ternary chains, deeply nested logical operators
- Normalize inconsistent patterns across the codebase
- Add missing TypeScript types, replacing `any` with proper types where inferrable
- Convert loose object shapes to defined interfaces/types based on actual usage patterns
- Group related functions into appropriate modules (reduce "junk drawer" utility files)
- Detect god classes/modules that do too many things and decompose them
- Standardize naming conventions across the codebase (camelCase vs snake_case consistency)
- Replace `var` with `const`/`let` where safe
- Convert `require` to `import` (or vice versa) for consistency
- Replace old-style class components with functional components + hooks (React)
- Replace hand-rolled `deepClone` with `structuredClone`, custom debounce with lodash debounce, etc.
- Sort and organize imports according to project conventions
- Apply the project's formatter (prettier, black, rustfmt) to any unformatted files
- Add missing type annotations where they can be inferred with certainty

---

## 10. Error Handling & Resilience

Make the project handle failure gracefully.

- Find all empty catch blocks and add proper error logging
- Find all unhandled promise rejections and add error handlers
- Add input validation at every public API boundary
- Add null/undefined checks before deep property access chains (or convert to optional chaining)
- Add timeout handling to all external HTTP/API calls that currently wait forever
- Add retry logic with exponential backoff to transient failure points
- Add circuit breaker patterns around external service dependencies
- Verify database connection errors are handled and connections returned to the pool
- Add graceful shutdown handling — SIGTERM → stop accepting requests → finish in-flight work → close connections → exit
- Check that file operations handle permission errors, disk-full, and missing-directory scenarios
- Verify background jobs/workers handle failures without silently dropping tasks
- Add dead letter queues or failure logs for async operations that can fail
- Check that WebSocket disconnections are handled with automatic reconnection logic
- Verify long-running operations have progress tracking and cancellation support
- Add health check endpoints that verify all critical dependencies (database, cache, external services)
- Check that retried operations are idempotent (retrying a payment doesn't charge twice)
- Verify that error boundaries exist in the UI (one component crashing doesn't take down the whole page)
- Add fallback UI for when JavaScript fails to load or execute
- Check that network failure during form submission doesn't lose the user's input
- Verify that the app recovers gracefully after regaining network connectivity

---

## 11. Cross-Browser & Cross-Platform Compatibility

Verify the project works everywhere it should.

- Test rendering in Chrome, Firefox, Safari, and Edge — compare screenshots for layout differences
- Test on iOS Safari specifically (most CSS/JS quirks live here)
- Check for CSS features used without fallbacks that aren't supported in target browsers
- Verify JavaScript APIs used are available in all target environments or polyfilled
- Test with different OS-level font rendering (Windows ClearType vs macOS antialiasing)
- Check that responsive breakpoints transition smoothly through all widths, not just specific breakpoints
- Test print stylesheets — does content print readably without nav bars, ads, and background colors?
- Check dark mode support — does the site respect `prefers-color-scheme`?
- Verify email templates (if any) render in Outlook, Gmail, and Apple Mail
- Test right-to-left text handling if the project supports or should support RTL languages
- Verify touch interactions work properly on mobile (hover states, drag-and-drop, gesture handling)
- Check that media queries and responsive images serve appropriate assets for device pixel ratios
- Test with browser text size set to "Large" and "Very Large" — does the layout accommodate it?
- Verify that CSS custom properties have fallback values for older browsers if needed
- Test on slow/throttled network connections (3G simulation) — does the app remain usable?

---

## 12. DevOps, CI/CD & Infrastructure

Improve the build, deploy, and operations pipeline.

- Verify CI pipeline runs and passes (CI configs can drift silently)
- Check that CI runs all tests that can be run locally
- Add missing CI steps — linting, type checking, security scanning, coverage reporting
- Verify the production build actually works (build it locally and test the output)
- Check that environment-specific configs (dev, staging, prod) are consistent and not missing variables
- Add or improve Docker configuration — multi-stage builds, minimal base images, proper layer caching
- Check that docker-compose includes all required services and works from a fresh clone
- Verify deployment scripts are idempotent
- Add pre-commit hooks for linting, formatting, and secret scanning if none exist
- Generate or update a Makefile/justfile consolidating all common commands
- Verify backup and restore procedures work if applicable
- Check that logging is structured (JSON) and persisted, not just stdout that disappears
- Verify alerts/monitoring exist for critical failure modes
- Check that the README setup instructions actually work from scratch on a clean environment
- Verify that the build output is deterministic (same input → same output)
- Check that CI caching is configured properly to speed up builds
- Verify that staging/preview environments match production configuration

---

## 13. Documentation & Developer Experience

Make the project easier to understand and contribute to.

- Generate docstrings/JSDoc for all undocumented public functions
- Generate or update API documentation from code (endpoint signatures, request/response schemas)
- Verify README installation instructions actually work from scratch on a clean machine
- Generate architecture documentation — system components, data flow, key design decisions
- Create CONTRIBUTING.md if none exists
- Generate CHANGELOG from git history if none is maintained
- Add code comments to complex algorithms explaining the "why"
- Generate environment variable documentation — what each var does, required vs optional, example values
- Check that error messages in the codebase are helpful and actionable
- Generate database schema documentation with entity relationship diagrams
- Create runbook documentation for common operational tasks
- Verify all configuration options are documented somewhere
- Generate a "getting started" guide for new developers beyond the README
- Document all available scripts/commands (npm scripts, make targets) with descriptions
- Add inline documentation for complex regex patterns, bitwise operations, or math
- Generate a glossary of domain-specific terms used in the codebase

---

## 14. Styling, UI & Visual Polish

Improve the look and feel.

- Check for inconsistent spacing, font sizes, and colors across pages (design system drift)
- Verify favicon exists in all required sizes and formats (ICO, PNG, apple-touch-icon, webmanifest)
- Check that loading states exist for all async operations (not blank screen while data loads)
- Verify empty states exist and are helpful (not a blank page when there's no data, but a message or call-to-action)
- Check that error states are designed (not raw error text or a white screen of death)
- Verify animations/transitions are smooth (no janky, low-framerate transitions)
- Check for z-index issues — overlapping elements, modals behind other content, tooltips clipped by containers
- Verify scrollbar styling is consistent and doesn't break across operating systems
- Check that text is readable — sufficient line height, appropriate max-width for readability (~65-75 chars), not too small on mobile
- Verify interactive elements have clear hover, active, focus, and disabled states
- Check for layout shifts when content loads dynamically
- Verify long text handles edge cases — very long words, very long URLs, user-generated content that might break layouts
- Check that notifications/toasts don't overlap with navigation or other critical UI elements
- Verify consistent border radius, shadow depth, and spacing scale usage across the app
- Check that truncated text has tooltips or expand affordances so the full content is accessible
- Verify that skeleton loaders or shimmer placeholders are used instead of just spinners for content-heavy pages
- Check that clickable elements look clickable (buttons look like buttons, links are visually distinct from plain text)
- Verify that disabled states are visually clear and don't look like regular enabled elements

---

## 15. Data, Database & API Quality

Ensure the data layer is solid.

- Run EXPLAIN/ANALYZE on common queries and flag full table scans or missing indexes
- Check for database migrations that are pending or out of sync
- Verify API responses follow a consistent format (envelope structure, error format, pagination style)
- Check for API endpoints that return too much data (no pagination, no field selection)
- Verify API versioning is implemented if the project has external consumers
- Check for race conditions in concurrent data modifications
- Verify database constraints match application-level validation
- Check for orphaned records — foreign key references to deleted rows
- Verify database connections are pooled properly and released after use
- Test API rate limiting if implemented — does it actually work?
- Check that sensitive fields are excluded from API responses
- Verify batch operations have reasonable limits
- Check that database migrations are reversible (have both up and down)
- Verify that the database has appropriate backup schedules
- Check for queries that select all columns when only a few are needed
- Verify that database timeouts are configured reasonably

---

## 16. Logging, Monitoring & Observability

Make the project debuggable and operable.

- Check that all error paths log enough context to diagnose problems (not just "error occurred")
- Verify logs are structured and parseable (JSON, not random string concatenation)
- Add request/response logging for API endpoints with appropriate redaction of sensitive fields
- Add timing/duration logging for slow operations
- Check that log levels are used appropriately (not everything as console.log/INFO)
- Verify correlation/request IDs are propagated through the request lifecycle
- Add startup logging — configuration loaded, services connected, ready to serve
- Check that log rotation or size limits are configured
- Add metric collection points for key business events
- Verify existing monitoring/alerting thresholds are reasonable
- Check that unhandled exceptions are reported to an error tracking service (or add one)
- Add performance timing markers for critical code paths
- Verify that async operations log their start, completion, and failure states

---

## 17. Internationalization & Localization

Prepare for or verify multi-language support.

- Scan for hardcoded user-facing strings that should be in translation files
- Verify all existing translation keys have translations in all supported languages
- Check for string concatenation that would break in languages with different word order
- Verify date, time, number, and currency formatting uses locale-aware functions
- Check that the UI accommodates text expansion (German is ~30% longer than English)
- Verify character encoding is UTF-8 everywhere
- Check right-to-left layout support if applicable
- Find user-facing text in code that bypasses the i18n system
- Check that pluralization is handled correctly (not just appending "s")
- Verify that locale is not hardcoded and can be changed by the user or detected from browser settings

---

## 18. Compliance & Legal

Verify the project meets regulatory and legal requirements.

- Check that cookie consent mechanisms are implemented and functional
- Verify data deletion/export functionality works (GDPR)
- Check that privacy policy and terms of service links are present and not broken
- Verify analytics and tracking only fire after consent is given
- Check that user data retention policies are implemented
- Verify accessibility requirements are met (ADA, WCAG)
- Check that third-party scripts and services are listed in the privacy policy
- Verify audit logging exists for sensitive operations
- Check that the project handles "Do Not Track" browser settings
- Verify that children's privacy protections are in place if the audience could include minors (COPPA)

---

## 19. UX Micro-Improvements & Small Feature Additions

Small, safe, verifiable additions that make the product noticeably better for users.

### Help & Discoverability

- Find all settings, toggles, and non-obvious options — add `(?)` info icons with tooltip/hover explanations describing what each option does
- Add `title` attributes or tooltips to icon-only buttons that have no visible label
- Add descriptive placeholder text to empty form fields showing expected format (e.g., "e.g., john@example.com" or "YYYY-MM-DD")
- Add helper text below complex form fields explaining constraints (max length, allowed characters, required format)
- Add contextual "Learn more" links next to features that reference documentation or help pages
- Add keyboard shortcut hints next to actions that have shortcuts (e.g., "Save (Ctrl+S)")
- Add a "What's this?" or info panel to complex dashboards or data-heavy screens
- Add instructional empty states — when a list is empty, show a helpful message and a call-to-action ("No projects yet. Create your first project →")

### Navigation & Information Architecture

- Find large menus with many items — reorganize into grouped sections with headers, or convert to a dropdown/popup with categories
- Find long settings pages with dozens of options — group into tabbed sections or collapsible categories
- Add breadcrumb navigation to deeply nested pages that lack it
- Add a "Back to [parent]" link on detail pages where the only way back is the browser button
- Add anchor links and a floating table of contents to long scrollable pages
- Detect pages where the user is likely to want to search (long lists, many items) and add a filter/search bar if missing
- Add "recently visited" or "quick access" shortcuts for frequently used features
- Add a command palette / quick-search (Ctrl+K style) for apps with many routes or actions
- Check that the main navigation highlights the currently active page/section
- Add a sticky header/nav that stays visible when scrolling long pages
- Add a "scroll to top" button on long pages

### Feedback & Confirmation

- Find all destructive actions (delete, remove, clear, reset) and verify they have confirmation dialogs — add them where missing
- Add success confirmation after form submissions (not just silently saving)
- Add undo capability for destructive actions where possible ("Item deleted. Undo?")
- Add progress indicators for multi-step processes (step 1 of 4, progress bar)
- Show character count and remaining characters for text fields with length limits
- Add visual feedback when items are added to lists, carts, or collections (animation, highlight, count update)
- Show "Saved" or "Up to date" indicators for auto-saving forms
- Add loading indicators to buttons that trigger async actions (button text changes to "Saving..." or shows a spinner)
- Ensure every user action has visible feedback — no clicking a button and wondering if anything happened

### Forms & Input Enhancement

- Add autofocus to the first input field on pages that are primarily forms
- Add autocomplete attributes to common form fields (name, email, address, phone, credit card) so browsers can fill them
- Convert date inputs from plain text fields to date pickers where the user is expected to enter a date
- Add input masks for phone numbers, credit card numbers, and other formatted fields
- Add "Show password" toggles to password fields
- Preserve form data when validation fails (don't clear the form and make the user re-enter everything)
- Add inline validation that checks as the user types, not just on submit
- Add smart defaults to form fields where a reasonable default exists
- Convert radio button groups with many options (5+) into searchable dropdowns
- Add "Select all" / "Deselect all" to checkbox groups with many options
- Support paste-and-split for fields that accept multiple values (paste comma-separated emails into a multi-email field)

### Data Display & Tables

- Add sorting to table columns that aren't currently sortable
- Add column resizing to wide tables
- Add row highlighting on hover for dense tables
- Add a "Copy" button next to IDs, API keys, code snippets, or other values users frequently copy
- Add export functionality (CSV, JSON) to data tables that don't have it
- Add pagination or virtual scrolling to long lists that currently render all items
- Make long table cells truncate with a tooltip showing the full value on hover
- Add a row count / total items indicator to tables and lists
- Add expandable rows for tables where each row has additional detail
- Sticky table headers that stay visible when scrolling long tables

### Quality of Life

- Add keyboard shortcuts for common actions (save, create new, search, navigate)
- Add "Copy to clipboard" buttons for shareable URLs, reference codes, or generated content
- Add relative time displays alongside absolute dates ("January 15, 2026 · 2 months ago")
- Add dark mode toggle if the app doesn't have one but has the infrastructure to support it (CSS custom properties)
- Add a print-friendly stylesheet for pages users are likely to print (invoices, reports, receipts)
- Add "Open in new tab" support for items in lists (middle-click, Ctrl+click)
- Add drag-and-drop reordering to lists that have manual ordering but only use up/down buttons
- Add batch actions to lists (select multiple → delete/archive/move)
- Add persistent user preferences for view modes (list vs grid), sort order, and filters
- Auto-save drafts for long-form text input so users don't lose work

---

## 20. Smart Feature Generation

More substantial features that can be generated, tested, and offered on a branch for review.

### API & Integration

- If the project has an API but no OpenAPI/Swagger spec, generate one from the code
- Generate a Postman/Insomnia collection from API routes
- Add webhook support for key events if the project is an API-first product
- Generate a basic CLI interface for a library that only has a programmatic API
- Add RSS/Atom feeds for content that updates regularly
- Generate API client SDKs (TypeScript types, Python client) from the API definition
- Add a public API status/health endpoint

### Admin & Internal Tools

- If there's no admin dashboard, scaffold a basic one showing key metrics, recent activity, and system health
- Add a user management interface if there are users but no way to manage them outside the database
- Generate a simple audit log viewer for admin users
- Add feature flags infrastructure if none exists (with a simple toggle UI)
- Generate a database seed script with realistic test data

### Content & Documentation Features

- Add a search feature to documentation or content-heavy sites
- Generate a sitemap page (human-readable) linking to all public pages
- Add "last updated" timestamps to content pages derived from git
- Add a changelog page generated from git history
- Generate a glossary page from terms defined throughout the content
- Add "Edit this page" links pointing to the source file in the git repo

### Developer Features

- Generate TypeScript declaration files for untyped JS modules in the project
- Add i18n scaffolding — extract strings, create translation files, set up the translation function
- Generate database entity relationship diagrams from schema/models
- Create a style guide / component library page showcasing all UI components in the project
- Add Storybook stories for React/Vue components that don't have them
- Generate mock API server from route definitions for frontend development without backend

---

## 21. Proactive Codebase Auditing & Reporting

Generate reports and insights without changing anything.

- Run static analysis tools (eslint, mypy, clippy) and compile a prioritized report of all warnings/errors
- Detect dead code — unused functions, unreachable branches, unused imports, unused dependencies
- Find all TODO/FIXME/HACK comments and catalog them with surrounding context and file locations
- Map dependency trees and flag circular dependencies
- Identify code duplication (near-identical functions or blocks across files)
- License audit — scan all dependencies for license compatibility issues
- Generate a "codebase health report" — file sizes, complexity metrics, test coverage gaps, documentation gaps
- Generate a dependency graph visualization
- Map the API surface — all endpoints, methods, params, and whether they have tests
- Identify "bus factor" files — complex, critical, and poorly documented
- Detect architectural drift — files violating expected dependency direction (e.g., utils importing from features)
- Measure and trend code complexity over recent commits — is the codebase getting more or less complex?
- Generate a security posture report summarizing all findings across categories
- Produce a "quick wins" list — the lowest effort, highest impact improvements sorted by category
- Generate an estimated effort report for each finding (minutes vs hours vs days)

---

## 22. Build & Config Hardening

Tighten the project's build and configuration without changing application code.

- Add or tighten compiler strictness flags (e.g., enable `strict: true` in tsconfig if all checks already pass)
- Enable additional linter rules that the codebase already complies with (free strictness)
- Add `.editorconfig` or normalize existing config files
- Ensure CI config is consistent with local config (same runtime versions, same flags)
- Pin floating dependency versions to their currently resolved versions
- Remove deprecated configuration options that no longer have any effect
- Normalize and consolidate duplicate configuration (multiple places defining the same port number, URL, etc.)
- Add configuration schema validation so invalid config is caught at startup, not at runtime
- Verify that `.gitignore` covers all generated files, build artifacts, OS files (.DS_Store, Thumbs.db), and IDE configs
- Check that npm/yarn/pnpm is used consistently (not mixing package managers)
- Verify that the `engines` field in package.json matches the version used in CI and production

---

## Execution Framework

Every task above runs inside a common execution wrapper:

### Pre-flight
- Snapshot the current state (git stash or branch from current HEAD)
- Run existing test suite, record baseline results and metrics
- Verify clean working directory

### Execute
- Make the change (on a working branch if Tier 4+ risk)
- Log every modification with rationale

### Verify
- Run all existing tests
- Run any new tests written by the agent
- Compile/build check
- Lint check
- For runtime changes: launch and smoke-test the app

### Decide
- All green → commit with a detailed, descriptive message
- Any red → full revert, log what was tried and the exact failure
- High-risk changes → keep on a feature branch, never auto-merge to main

### Report
When the user returns, present a structured summary:
- **Done**: changes committed to main, with diffs and rationale
- **Attempted**: changes tried but reverted, with failure details
- **Discovered**: issues found but not acted on, prioritized by severity
- **For Review**: feature branches awaiting human review, with descriptions
- **Metrics**: before/after comparisons (test count, coverage, Lighthouse scores, bundle size, etc.)

---

## Skill Decomposition

Each category above maps to one or more agent skills. A meta-orchestrator skill selects which to run based on:

- **Project type**: web app, API, CLI tool, library, mobile app, static site
- **Stack detection**: framework, language, database, deployment platform
- **Available tooling**: what's already configured (linters, test runners, CI)
- **Time budget**: how long the user will be away
- **Priority**: security and correctness first, polish and features last
- **Risk tolerance**: configurable by the user before entering away mode
