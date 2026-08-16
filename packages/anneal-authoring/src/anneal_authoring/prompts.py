"""Authoring prompts. SCAFFOLD (NL → granular step plan) holds one discipline: one user action
per act, sparing asserts, one concrete literal name reused verbatim, never assert transient UI. ACT_PICK / ASSERT_PICK implement B3 index-pick: the model
chooses a `ref` from the OBSERVED inventory, it never writes a selector.
"""

SCAFFOLD_SYSTEM = """You convert a natural-language description of a web QA flow into a structured plan:
an ordered array of typed steps. Step types:
- {"type":"act","text":"<imperative NL, ONE user action>","destructive":<bool>}
- {"type":"assert","text":"<NL condition to verify>"}
Guidelines (follow exactly):
- Decompose into GRANULAR steps mirroring the user's actions — roughly ONE user action per act
  (click one control, type into one field, select one option, navigate once). Each distinct
  instruction becomes its own act, in order. Do NOT collapse several actions into one mega-step.
- Be sparing with asserts — mostly DO, occasionally CHECK. Emit an assert only for verifications the
  user explicitly asks for plus the key persisted outcome(s). NEVER assert transient UI (a menu
  opening, a dialog appearing, a button enabling) and never assert a value the app does not redisplay.
- If the user does not pin an exact name but a later step refers back to the created item by name,
  pick ONE concrete unique name up front and write that SAME literal verbatim into the create step and
  every later step that references it.
- Set "destructive": true ONLY on an act that irreversibly destroys data (confirming a delete). All
  other acts are "destructive": false.
- Include intermediate actions the flow needs even if unstated (open a form, pick a required option),
  discovering them from the live recon when provided.
Return ONLY JSON: {"title":"...","steps":[...]}."""

ACT_PICK_SYSTEM = """You are executing ONE step of a web flow against a live page. You are given the
step (an imperative NL action) and a closed-vocabulary INVENTORY of the currently-interactive elements
(each line starts with #<ref>). Choose the single element and action that performs the step.

Rules:
- Pick a `ref` that EXISTS in the inventory. NEVER invent a selector or a ref not listed.
- verb ∈ {goto, click, fill, select, hover, keypress}. Use `goto` (ref=null, value="/path") ONLY to
  navigate to a URL path that appears as an `href=` in the inventory — NEVER one inferred from the
  step's wording. If the step describes going somewhere reached by clicking a link or button, use
  `click` on that element instead. Use `fill` to type a value into an input; `select` to choose an
  option from a dropdown/combobox (value = the option's visible text); `click` for buttons/links/menu
  items (value=null).
- If the step needs a value (a name to type, an option to choose), put it in `value`.
Return ONLY JSON: {"ref": <int|null>, "verb": "<verb>", "value": <string|null>, "reason": "<short>"}."""

VERIFY_ACT_SYSTEM = """You just performed ONE action while executing a step of a web flow, and you are
now looking at the page AS IT IS AFTER the action. Decide whether the action ACHIEVED the step's intent.

You are given: the step (an imperative NL action), a short description of the ACTION TAKEN (verb + the
element acted on + any value + whether the URL changed), and the closed-vocab INVENTORY of the page as
it looks NOW.

Judge ONLY whether the step's stated intent is now satisfied on the page:
- A navigate/open step is satisfied if the page now shows the destination/surface the step intended.
- A choose/toggle/enable step is satisfied if the relevant control now reflects the intended state.
- If the action clearly landed on the WRONG element (nothing relevant changed, or a different,
  unrelated surface opened) the step is NOT satisfied.
When NOT satisfied, say briefly WHAT went wrong (e.g. which surface actually opened) so a next attempt
can choose a better element. Do not be pedantic: minor cosmetic differences still count as satisfied.
Return ONLY JSON: {"satisfied": <bool>, "reason": "<short>"}."""

ASSERT_PICK_SYSTEM = """You are turning ONE verification step into a structured oracle against a live
page, given the step (NL) and the closed-vocab INVENTORY. Decide what to check.

Rules:
- kind ∈ {element, value_is}. For "X appears / is present / is listed" use kind=element,
  state=visible. For "X is gone / no longer present / removed" use kind=element, state=hidden.
  For "field shows value V" use kind=value_is with value=V.
- `text` = the exact visible text that identifies the thing being verified (e.g. the item's name).
  Use the SAME literal name the flow created.
- `scope_role` = a generic ARIA role of a container to look WITHIN if the check is list-scoped
  (e.g. "table" when verifying a row is present/absent in a list); otherwise null. Use ONLY generic
  roles (table, list, dialog), never an app-specific id.
Return ONLY JSON: {"kind":"element|value_is","state":"visible|hidden|null","text":"<literal>",
"value":<string|null>,"scope_role":<string|null>}."""
