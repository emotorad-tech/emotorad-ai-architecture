# Emotorad: awareness-to-aftersales journey mapped to AI use cases

This maps the full customer journey — from first hearing about Emotorad to long-term retention — against the phygital reality of the business: a hybrid of a D2C website/app (~30% of domestic revenue), marketplaces (Amazon, Flipkart), and a large offline network (350–600+ dealers, 12 experience stores), supported by EMI/BNPL financing, test-ride events, and a growing subscription-based service model.

Each use case is tagged by AI type, because the type determines how hard it is to build:

- **Agentic AI** — takes multi-step action using tools (looks up stock, books a slot, verifies a document, escalates to a human). This is what "agentic AI implementation" actually refers to.
- **Generative AI** — produces content in one shot (copy, summaries, video scripts). Simplest to stand up.
- **Predictive ML** — scores, forecasts, or classifies (who will churn, what will break, who to call first). Needs clean historical data.
- **Rule-based / deterministic** — plain logic and lookups (nearest dealer, slot availability). No model involved, and often the cheaper, more reliable choice. Only earns an "agentic" label if it's exposed as a tool a conversational agent calls mid-chat, rather than a fixed form or menu.

A note on voice bots specifically: a voice bot isn't a separate AI type — it's an agentic bot delivered over the phone instead of chat. Same build considerations as any agentic use case, plus a telephony/speech-to-text/text-to-speech layer on top. Given a large share of Emotorad's buyers come through offline dealers and may be less digitally engaged, voice is worth calling out wherever it adds real reach beyond text.

---

## 1. Awareness
*Touchpoints: Meta/Instagram/YouTube ads, TV spots (e.g. cricket sponsorships), SEO content, marketplace listings, dealer signage, community events like "E-Bike Mornings."*

| Use case | Type | What it does | Description | Priority |
|---|---|---|---|---|
| Localized ad copy & creative generation | Generative | Produces regional-language variants (Hindi, Marathi, Tamil, etc.) of ad copy and short video scripts at scale | Helps ads resonate in regional markets beyond metro, English-first creative — relevant given ~85% of e-cycle sales are offline/dealer-driven across India | Low |
| SEO content engine | Generative | Drafts comparison articles, city-specific landing pages ("best e-cycle in Pune") | Builds organic discovery to complement paid social and lower acquisition cost over time | Low |
| Lookalike/propensity audience modeling | Predictive | Scores which ad audiences are likely to convert, reallocates spend | Improves paid ad efficiency, but depends on clean conversion data flowing back from CRM | Low |
| Dynamic creative optimization | Predictive | Tests multiple ad creatives per audience segment and reallocates spend to top performers automatically | Squeezes more out of the same ad budget once enough creative variants and conversion data exist | Medium |
| WhatsApp/Instagram DM first-response agent | Agentic | Answers FAQs, qualifies interest, captures the lead into CRM without a human touching it | Captures and qualifies leads instantly on the channel where most Indian D2C conversations already happen — a high-leverage, foundational entry point for agentic AI | High |
| Website chatbot | Agentic | Answers FAQs and qualifies interest for visitors landing directly on emotorad.com, mirroring the WhatsApp/Instagram bot | Same high-leverage entry point as the DM bot, but for direct/organic/paid-search traffic instead of social | High |
| Voice bot for inbound query calls | Agentic | Handles common pre-sales phone questions (availability, pricing, nearest dealer) on the customer care line, escalating to a human when needed | Captures prospects — often less digitally-savvy or in a hurry — who still prefer to call rather than chat | Medium |
| Social comment auto-response | Agentic | Replies to product questions in Instagram/Facebook comments in the right tone and language, escalating anything sensitive | Keeps response time low on public comments, where a slow or missing reply is visible to every other prospect browsing that post | Medium |
| Marketing mix/attribution modeling | Predictive | Attributes sales to channel (TV vs social vs dealer footfall) to guide budget shifts | Clarifies which channel is actually driving sales to inform budget allocation across the phygital mix | Low |

## 2. Consideration
*Touchpoints: website/app browsing, marketplace reviews, WhatsApp inquiry, dealer showroom visit, test ride.*

| Use case | Type | What it does | Description | Priority |
|---|---|---|---|---|
| Model configurator / advisor bot | Agentic | Asks commute distance, terrain, budget; recommends model + accessories; can check live dealer stock | Turns a website or WhatsApp browse session into a guided recommendation, cutting the back-and-forth a dealer would otherwise handle manually | Medium |
| RAG-based FAQ/policy assistant | Agentic | Answers spec, warranty, and policy questions by retrieving from Emotorad's actual documents instead of a hard-coded script | The clearest RAG use case in this journey — grounds answers in real, frequently-updated documents instead of a model's guess | High |
| Personalized website merchandising | Predictive | Reorders homepage and product recommendations based on browsing behavior and past purchases | Standard e-commerce personalization; useful but not urgent compared to fixing the core conversion funnel | Low |
| Dealer-match + test-ride booking | Rule-based (agentic only if chat-driven) | Finds nearest of the 350–600+ dealers with stock, books a test-ride slot end-to-end | The matching and booking logic is just geolocation + inventory + calendar lookups — no model needed. It only becomes agentic if it's wired in as a tool the WhatsApp/website bot calls mid-conversation, rather than a plain find-a-store-and-book form | Medium |
| Review & rating summarizer | Generative | Aggregates Amazon/Flipkart/Google reviews into a plain-language pros/cons digest per model | Surfaces trustworthy proof points right when a prospect is comparing models | High |
| EMI/BNPL eligibility assistant | Agentic | Walks the buyer through the 25%-down + 3/6/9-month EMI plan and pre-checks eligibility | Removes friction from the EMI decision, which is central to Emotorad's affordability pitch | Medium |
| Lead scoring | Predictive | Ranks inbound inquiries by likelihood to convert, so dealer staff call the hottest leads first | Directs the highest-intent inquiries to follow-up first instead of treating every lead equally | Medium |
| Outbound voice bot for lead follow-up | Agentic | Calls leads flagged by the scoring model who've gone quiet, answers financing/product questions verbally, and books a test ride or hands off to a dealer | Many buyers still expect a phone call before a big purchase — reaches leads a chat nudge alone won't move | Medium |
| AR/virtual bike preview | Generative (multimodal) | Shows the bike in the customer's space before a showroom visit | A nice differentiator for the D2C channel, but lower near-term ROI than fixing the core funnel first | Low |

## 3. Purchase / conversion
*Touchpoints: website checkout, marketplace order, in-store billing, EMI paperwork.*

| Use case | Type | What it does | Description | Priority |
|---|---|---|---|---|
| Abandoned-cart recovery agent | Agentic | Sends a personalized WhatsApp/email nudge with context-aware incentive | Recovers revenue on the website channel (~30% of domestic revenue), where cart abandonment is highest | High |
| Checkout upsell prompt | Generative | Suggests a helmet, lock, or accessory bundle at checkout based on the model being purchased | Cheap, proven e-commerce pattern that lifts average order value with minimal engineering | High |
| Dealer sales copilot | Agentic | Live-suggests objection handling and accessory upsells to dealer floor staff | Standardizes pitch quality across a large, variable-skill dealer network of 350–600+ outlets | Medium |
| KYC/document verification agent | Agentic | OCR + validation of ID/income documents for EMI approval | Speeds up EMI approval without adding headcount at the point of sale | Medium |
| Voice bot for EMI verbal verification | Agentic | Calls the applicant to confirm income/employment details verbally, a secondary check lenders often require for higher-ticket EMI | Complements the OCR agent above with the verbal confirmation step financing partners typically ask for | Medium |
| Fraud/credit risk scoring | Predictive | Flags suspicious online payments or EMI applications before approval | Protects margin as EMI/BNPL volume grows | Medium |
| Next-best-offer/discount optimization | Predictive | Determines the most effective personalized incentive to close a hesitant buyer | Handle carefully — inconsistent pricing across customers can damage trust if it becomes visible; test narrowly before scaling | Low |
| Order-to-dealer inventory allocation | Predictive/Agentic | Routes each order to the nearest dealer or warehouse with stock, feeding logistics | Reduces delivery delays, but needs real-time stock visibility across dealers first — a data problem before an AI problem | Low |

## 4. Onboarding & delivery
*Touchpoints: D2C shipping or in-store handover, first setup, pre-delivery inspection.*

| Use case | Type | What it does | Description | Priority |
|---|---|---|---|---|
| Delivery ETA & route optimization | Predictive | Improves last-mile shipping accuracy (builds on Emotorad's existing smart-shipping logistics work) | Builds on an existing capability rather than starting from zero | Medium |
| Outbound delivery-confirmation voice bot | Agentic | Calls to confirm the delivery slot before dispatch, and again after handover to check the bike arrived safely and assembly went fine | A proven, low-risk pattern already common in Indian logistics — cuts failed/missed deliveries and catches assembly problems early | High |
| Delivery/setup WhatsApp agent | Agentic | Tracks shipment, answers assembly/charging questions, can reschedule delivery | Cuts inbound "where's my order" and "how do I assemble this" volume on the channel customers already use | High |
| Auto-generated welcome content | Generative | Produces model-specific welcome video/PDF — battery care, first-ride tips | Low-effort content generation that improves the first-ride experience and reduces early support tickets | High |
| Vision-based pre-delivery inspection | Generative/Predictive (vision) | Flags cosmetic or assembly defects from dealer-submitted photos before handover | Valuable for consistency across a large dealer network, but needs a photo-capture workflow to exist first | Low |
| Automated warranty/serial registration | Agentic | Scans the bike's serial number/QR at handover and auto-registers the warranty instead of a manual form | Removes a manual paperwork step at every one of the 350–600+ dealer handovers | Medium |

## 5. Usage
*Touchpoints: daily riding, in-app experience (if bikes are connected/IoT-enabled).*

| Use case | Type | What it does | Description | Priority |
|---|---|---|---|---|
| Battery health & usage monitoring | Predictive | Flags early battery degradation from usage patterns, if telematics exist | Only possible once bikes carry telematics/connectivity — a hardware and data dependency, not just a model | Low |
| Crash/anomaly detection | Predictive | Flags sudden deceleration or fall patterns from IoT sensors and can trigger an emergency check-in call | Genuinely valuable for rider safety and brand trust, but — like battery monitoring — depends on telematics hardware that may not exist yet | Low |
| Personalized riding tips & nudges | Generative | Seasonal maintenance reminders, model-specific care tips | Cheap to produce and keeps the brand present between purchase and next service visit | High |
| Weekly ride summary generator | Generative | Auto-generates a shareable weekly ride/CO2-saved summary, Strava-style | Cheap engagement and word-of-mouth play once basic ride data exists | Medium |
| Send-time/channel optimization | Predictive | Learns the best time/channel to reach each rider with notifications | Improves engagement once there's enough interaction data to learn from | Medium |
| In-app troubleshooting assistant | Agentic | Resolves simple issues before the rider ever calls support | Assumes a companion app exists; otherwise this folds into the WhatsApp support bot instead | Medium |

## 6. Service & support
*Touchpoints: physical service centers, subscription service plans, call center, dealer service bays.*

| Use case | Type | What it does | Description | Priority |
|---|---|---|---|---|
| Tier-1 support bot / voice bot | Agentic | Handles battery/brake/motor troubleshooting; can book a service slot or order a spare part directly | Directly reduces call center load — the highest-volume, highest-cost support interaction | High |
| Predictive maintenance outreach | Predictive | Proactively contacts riders for service based on mileage/battery cycles, before something breaks | Turns the existing subscription service plans into a proactive relationship instead of a reactive one — the trigger for the voice bot below | Medium |
| Outbound voice bot for service scheduling | Agentic | Calls the rider flagged by the predictive-maintenance model, explains why service is due, offers slots, and books the appointment on the call | Executes the prediction above as a booked appointment — voice converts better than an SMS/app ping for scheduling | Medium |
| Spare-parts demand forecasting | Predictive | Forecasts part demand across the service network to cut stockouts | Needs consistent parts-usage data across service centers and dealers before it's reliable | Low |
| Technician capacity/scheduling optimization | Predictive | Balances technician workload across service centers based on incoming demand forecasts | Same data dependency as spare-parts forecasting — worth doing once that pipeline exists, not before | Low |
| Voice bot for complaint/warranty intake | Agentic | Captures a spoken complaint or warranty issue, opens a ticket, requests photo follow-up via WhatsApp, and escalates complex cases to a technician | Gives callers who report a problem by phone the same structured intake as the chat-based flows, instead of a manual call-center note | Medium |
| Vision-based warranty claim triage | Agentic (vision) | Assesses damage/misuse from submitted photos, auto-approves low-risk claims, routes edge cases to a human | Reduces manual review time but carries real risk if the model misjudges a claim — needs a human-review fallback | Low |
| Technician copilot (RAG over manuals/tickets) | Agentic | Surfaces the right fix from service manuals and past tickets while the technician is on the job | High long-term value, but only worth building once there's a real corpus of manuals and tickets to draw from | Low |
| Service-ticket sentiment/escalation flagging | Predictive | Flags at-risk tickets before they become a bad review | An early-warning system for complaints that would otherwise surface publicly first | Medium |

## 7. Retention, loyalty & advocacy
*Touchpoints: repeat purchase, referrals, community rides, reviews.*

| Use case | Type | What it does | Description | Priority |
|---|---|---|---|---|
| Churn/attrition prediction | Predictive | Flags riders unlikely to re-engage or renew a service plan; triggers win-back offers | Needs at least a year or two of purchase/service history to be statistically meaningful | Low |
| Outbound voice bot for win-back calls | Agentic | Calls riders flagged by the churn model with a renewal or win-back offer | Executes the churn model's trigger as a live conversation, but inherits the same dependency — needs trustworthy history first | Low |
| Upsell/cross-sell agent | Agentic | Recommends and executes accessory, upgrade, or extended-warranty offers per rider profile | Turns service visits and app usage into a natural moment for accessory or upgrade offers | Medium |
| CLV modeling | Predictive | Prioritizes retention spend toward the highest lifetime-value riders | Best used to prioritize retention spend once churn prediction is already in place | Low |
| Loyalty-tier personalization | Predictive | Personalizes rewards and perks based on predicted lifetime value and engagement | A natural extension of CLV modeling, not a separate build | Low |
| Review/UGC request generation | Generative | Drafts a personalized review ask right after a high-NPS service interaction | Cheapest way to compound the review base that already drives online/marketplace consideration | High |
| Outbound review-collection voice bot | Agentic | Calls customers after a purchase or service visit, asks for a verbal rating, transcribes it, and routes it to review platforms or flags detractors for recovery | Reaches riders who won't respond to a WhatsApp/email ask — useful given a large share of buyers come through offline dealers and may be less digitally engaged | Medium |
| Voice-of-customer synthesis | Generative | Rolls up marketplace/social feedback into themes for product and marketing | Feeds product and marketing decisions from real rider language instead of assumptions | Medium |
| Community/event-matching agent | Agentic | Invites riders to relevant local community rides and demo events | Extends the proven "E-Bike Mornings" community-ride model without manual coordination | Medium |
| Referral fraud detection | Predictive | Flags referral-program abuse — self-referrals, fake accounts — before payout | Only matters once there's an active referral program with real payout volume to protect | Low |

---

## Suggested phased rollout

**Phase 1 (0–3 months) — quick wins, low risk, existing tools**
WhatsApp/website lead-capture and FAQ bot · review summarizer · abandoned-cart recovery · basic order-status/service-booking bot. These need minimal data integration and prove value fast.

**Phase 2 (3–6 months) — needs CRM/data integration**
Lead scoring · EMI eligibility agent · dealer-match + test-ride booking · predictive service reminders · dealer sales copilot.

**Phase 3 (6–12+ months) — needs a solid data foundation**
Telematics-based predictive maintenance · dynamic dealer inventory allocation · vision-based warranty automation · churn/CLV models · full technician copilot.

## Implementation architecture: what "building the agentic layer" actually means

**Foundational prerequisites (apply across all stages)**
- One unified customer data view across website, marketplaces, dealer POS, and service records — Emotorad already has "advanced CRM" per public sources; the open question is whether dealer and service data actually feed into it.
- WhatsApp Business API as the primary agentic-AI channel — it's already the dominant engagement layer for Indian D2C brands.
- Clear human-escalation paths for every agent (warranty, refunds, safety complaints should never be fully autonomous).
- Data governance/guardrails before any customer-facing agent goes live.

**One agent core, not five bots**
Don't build a separate bot for WhatsApp, the website, and voice. Build one agent core — a system prompt, a fixed set of tools (CRM lookup, inventory check, calendar booking, order status, escalation), and guardrails — then put a thin channel adapter in front of it per surface. WhatsApp and the website share almost everything; voice just adds a speech-to-text/text-to-speech layer on top of the same core. That reusable core is the actual "agentic backend" — an orchestration layer deciding which tool to call, not a chatbot per channel.

Build vs buy: buy the channel/telephony plumbing (WhatsApp Business API access, IVR/telephony, speech-to-text/text-to-speech are commodity infrastructure — Gupshup, Exotel, Ozonetel, Yellow.ai, Verloop and similar are common in India; building this from scratch rarely pays off). Build, or closely own, the reasoning layer — the prompts, tool definitions, and business logic that make the agent behave like Emotorad, not a generic vendor bot. That's the part that's actually your IP.

**When you actually need RAG**
RAG (retrieval-augmented generation) solves one specific problem: the agent needs to answer from a body of unstructured text too large for a prompt and that changes over time — service manuals, warranty policy, spec sheets across models, past support tickets. That's the technician copilot, the RAG-based FAQ/policy assistant, and voice-of-customer synthesis.

It is not needed for anything that's really a lookup against structured, live data — dealer stock, calendar slots, order status, EMI rules. Those are tool calls to a database or API, not RAG. Conflating the two is the most common over-engineering mistake teams make — building a retrieval pipeline for something that's actually just "call the inventory API."

**Cost**
Three real cost drivers, roughly in order of how often they're underestimated:
1. Telephony for voice bots — speech-to-text/text-to-speech and call minutes usually cost more than the LLM call itself. Budget voice separately from the "AI" line item.
2. Conversation volume × model choice — text bots are cheap per interaction; cost scales with how many conversations you run and how verbose the model is, not with how "smart" the use case is.
3. One-time data/integration engineering — connecting CRM, dealer POS, and service records is usually the bigger line item, and it's the same investment whether you build one bot or ten.

I don't have your actual message/call volumes, so I won't invent a number — the estimating exercise is (conversations/month) × (average tokens or call-minutes per conversation) × (per-unit cost of your chosen model/telephony vendor). Run a small, real Phase 1 pilot and measure actual usage before committing to Phase 2/3 spend.

**Quality**
- Evals before launch — build a test set of 50-100 realistic queries per use case, including edge cases and things the agent should refuse, and score accuracy/policy-adherence before it goes live.
- Guardrails, not trust — write down explicitly what the agent may never do autonomously (refunds above a threshold, warranty approvals, anything safety-related) and hard-code the escalation; don't rely on the model "knowing better."
- Human sampling in production — review a sample of real conversations weekly, especially in the first months; track escalation rate and resolution rate, not just "conversations handled."
- Version and re-test prompts — treat prompts and tool definitions like code: version them, and re-run the eval set before any change ships.

---

*Sources: [EMotorad marketing strategy](https://businessmodelcanvastemplate.com/blogs/marketing-strategy/emotorad-marketing-strategy), [EMotorad customer service](https://www.emotorad.com/service), [EMotorad Buy Now Pay Later](https://www.emotorad.com/buy-now-pay-later), [Smart shipping case study](https://www.eshipz.com/case-study-emotorad/), [EVreporter conversation with EMotorad](https://evreporter.com/exploring-emotorads-role-in-indias-e-cycle-market/)*
