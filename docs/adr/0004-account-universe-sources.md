# ADR 0004 - Account universe from YC directory mirror + Wikidata (via QLever)

Status: accepted (2026-11-20)

## Context
The outbound niche is small/medium digital agencies and B2B SaaS in US/UK/CA/AU. Paid
databases (Apollo, Crunchbase Pro, BuiltWith) are out of budget. Clutch.co returned 403.

## Decision
* **B2B SaaS**: `yc-oss.github.io/api/companies/all.json` (community mirror of the public YC
  directory). Filter: status Active, industry B2B, team size 5-500, location in a target country.
* **Agencies**: Wikidata entities that are an advertising/design/marketing agency (P31) or whose
  industry (P452) is marketing, advertising, web design/development, digital/online/internet/
  content/social-media marketing, digital advertising or graphic design, with an official
  website (P856), a target country (P17), not dissolved (P576), and <=1,000 employees where
  P1128 is known. Queried through the **QLever** Wikidata endpoint because WDQS was rate
  limiting to 1 request/minute during the build; WDQS remains the fallback.
* `account_id` = website host without `www.`; agencies are kept first when capping at 1,000.
* Company-level data only (name, domain, country, segment, headcount band). No people.

## Consequences
- Wikidata's "marketing" industry also catches some marketing-software and media companies;
  the segment label is therefore "agency-like", and the backtest reports by signal, not segment.
- YC companies skew to venture-backed SaaS; this is the B2B SaaS half of the ICP, not all of it.
- The outbound-engine prospect list should be joined on domain; prospects outside the universe
  can be appended to `data/universe.csv` (same columns) and will be collected from the next run.
