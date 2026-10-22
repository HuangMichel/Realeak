# Resale Radar: evidence it works (before / after)

All numbers below were measured on 22 Oct 2026 on real public data: the Cabaïa catalog (2,038 products, public Shopify feed) and the latest Cabaïa listings on Vinted (FR + DE), collected live through Keenable. Raw data is in the Aiven PostgreSQL database behind the live app.

## 1. Against a no-data baseline (same question, with and without data)

Question from a Cabaïa product manager: "What share of our items listed second-hand on Vinted are brand new, and which product families are resold new most often?"

| | Answer | Basis |
|---|---|---|
| **Before: GPT-6.1-Sol, no data** (same model family as our agent, asked the same question without data) | "About 30%", range 20 to 45%. Guessed families: Adventurer backpacks, interchangeable pockets, toiletry bags, beanies. | Its own words: "Low: judgment-based estimate, without a current Vinted sample." |
| **After: Resale Radar (agent + data)** | **57.8% new** (182 of 315 listings), of which **30.5% still have their tags** (96). Families with ≥15 listings ranked by share resold new: Poche 69.0% (87 listings), Sac à dos 66.7% (18), Sac 61.1% (36), Bonnet 61.1% (18). | Measured, every listing traceable in the database. |

Result: the no-data answer underestimates the real share by about half, and the measured value lies outside its own 20 to 45% range. Only the "pockets" guess survives contact with the data.

## 2. Against a naive keyword workflow (no AI), on all 315 listings

| | Keyword workflow (no AI) | Resale Radar agent |
|---|---|---|
| Listings classified into a product family | 281 / 315 (89%) | 315 / 315 (100%) |
| Exact catalog product proposed | 145 listings, from the title text only | 47 listings, confirmed on the photo; the other 268 flagged "unknown" instead of guessed |
| Agreement on exact product, where both propose one | 6 of 28 | 6 of 28 |
| Accuracy against human labels (Teach the agent screen) | not measured yet | not measured yet |

What this shows: titles on Vinted are vague ("sac cabaia bleu"), so a keyword rule happily "finds" a product for half the listings, but it disagrees with the photo check 22 times out of 28. The agent only names a product when the photo supports it. Accuracy against human labels is the next measurement: answers given in the "Teach the agent" screen are stored in the `labels` table and `python -m app.bench --brand cabaia` scores keywords vs agent vs agent + memory on them. No human labels had been entered at submission time, so we do not report an accuracy figure.

## 3. Time and cost

| | Before | After (measured) |
|---|---|---|
| Read and classify 315 listings | Manual: not measured (a person has to open each listing and compare it with a 2,038-product catalog) | **8.7 minutes**, fully automatic |
| Cost | Staff time | **$0.29** for 315 listings with the OpenAI API (≈ $0.001 per listing); **$0 marginal** now that the agent runs on a ChatGPT subscription through the Codex CLI |
| Freshness | Occasional manual checks | Live: one click on "Refresh from Vinted" at 13:58 found **67 new Cabaïa listings** (382 in total). The French page timed out (Keenable 504) and the agent switched to the German site on its own. |

## 4. The agent improves with use

After one run the agent stored **77 memories** (33 title aliases, 44 confirmed listing photos) in PostgreSQL. Human answers in "Teach the agent" are added to the same memory and reused as reference images in the next run.

## Method notes (honest limits)

- Listings are the most recent ones visible on Vinted brand pages (FR, DE), not the full history.
- "New" = Vinted condition "New with tags" or "New without tags", as declared by the seller.
- Product families come from the brand's own catalog (French names).
- The exact-product step is deliberately conservative: precision over coverage.
