# Realeak: evidence it works (before / after)

Measured on 22 Oct 2026 on real public data: the Cabaïa catalog (2,038 products, public Shopify feed) and the latest Cabaïa listings on Vinted (FR + DE), fetched live through Keenable. Every number is traceable in the Aiven PostgreSQL database behind the live app.

## 1. No-data baseline vs Realeak

Question: "What share of our items listed second-hand on Vinted are brand new?"

| | Answer | Basis |
|---|---|---|
| GPT-6.1 without data | About 30% (range 20 to 45%) | Its own words: low confidence, judgment-based estimate |
| **Realeak (agent + data)** | **55.5% new** (611 of 1,101 listings; 57.8% on the first 315) | Measured, listing by listing |

The measured value is outside the no-data range: guessing underestimates the real share by almost half.

## 2. Keyword workflow (no AI) vs agent, on the 315 listings of the first agent run

| | Keyword workflow | Realeak agent |
|---|---|---|
| Listings classified into a product family | 281 / 315 (89%) | 315 / 315 (100%) |
| Exact product | 145 text guesses; agreeing with the photo check on 6 of 28 | 47 photo-verified, the rest flagged "unknown" |

Vinted titles are vague ("sac cabaia bleu"), so keywords "find" a product for half the listings but disagree with the photo check 22 times out of 28. The agent names a product only when the photo supports it.

## 3. Time and cost

| | Result |
|---|---|
| 315 listings read and classified | **8.7 minutes**, fully automatic |
| Cost via the OpenAI API | **$0.29** (about $0.001 per listing) |
| Live refresh | Found **67 new listings**; after a timeout on the French site, the agent switched to the German site on its own |

Limits: accuracy against human labels is not measured yet (no labels entered at submission time).
