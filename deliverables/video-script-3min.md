# Resale Radar: 3-minute demo video script

Record the screen (OBS, or Windows + Alt + R with Xbox Game Bar) with a voice-over. Export as MP4.
Have the app open: https://01a1634d-3d18-7dfd-bd64-7fe0e4f1f7ea-8080.eur-1.aiven.app

## 0:00 to 0:20 — Hook (show /slide)
"Brands know their return rate. They don't know which products customers keep but don't want.
Those products end up on Vinted, new, often with tags still on.
Vinted is the world's largest panel of disappointed customers. Resale Radar reads it for brands."

## 0:20 to 0:50 — The real data (Radar tab, top KPI cards)
"We tested it today on Cabaïa, a French bag brand. Our agent read the 315 latest Cabaïa listings on Vinted:
57.8% are new, almost a third still have their tags.
Every listing is classified into a product family, and matched to the exact catalog reference when the agent is sure."

## 0:50 to 1:40 — The agent at work (click "Lancer l'agent", show the live log)
"This is not one ChatGPT call. The agent decides which Vinted pages and countries to read through Keenable,
and stops when new listings dry up.
For each listing it compares the photo with 2,038 catalog products, escalates hard cases to a stronger model,
and says 'unknown' instead of guessing.
It writes what it learns into memory, aliases and confirmed photos, so the next run is better."

## 1:40 to 2:10 — The insight (scroll to "Par catégorie de produit")
"Here is what a product team gets: per family, how many listings, the share resold new, and the resale discount.
The 'Poche' family: 87 listings, 69% new. Bags: 61% new. Beanies lose 60% of their value on resale.
Plus written insights for the product team."

## 2:10 to 2:35 — Proof (show bench results / slide table)
"We measured it against a simple keyword workflow on listings labeled by humans:
[read the 3 lines of the benchmark table: family accuracy, exact product accuracy, cost per listing].
A full run of 315 listings took under 9 minutes for 29 cents."

## 2:35 to 3:00 — Data layer and go-to-market (Labeling tab, then /slide footer)
"Everything lives in Aiven PostgreSQL: catalog, listings, matches, agent memory and runs, served by Aiven Runtime.
Human corrections in this Labeling tab feed the agent's memory.
Our go-to-market: a free resale audit for ten pilot fashion brands,
a free resale audit, then a monthly radar per brand. Thank you."
