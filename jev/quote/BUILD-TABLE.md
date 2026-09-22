# Building a real price table

The shipped `tables/phoenix-hvac.json` is a **sample** (see its `note`). Hand this to Claude Code to
replace it with a table built from public contractor pages. Same for a new trade or city: copy the
file, change `trade` and `city`, and run the prompt.

```
build the price table for [trade] in [city].

- find public price pages from contractors in this city and this trade. at least eight sources
- for every common service, pull the low and high price actually printed on the page
- build a table with the service, the size band, the urgency band, and the low and high
- the bands must not overlap. if two sources disagree, keep the wider range
- cite the url for every row
- if a service has fewer than three sources, leave it out and tell me which ones you dropped
- write it into jev/quote/tables/[city]-[trade].json in the format of phoenix-hvac.json,
  set "status": "live", put the URLs in each service's "sources",
  then run `node jev/quote/validate-table.js jev/quote/tables/[city]-[trade].json`
  and show me the output
```

Rules the validator enforces (`validate-table.js`):

- `sizes[].maxSqft` strictly increasing (size bands can't overlap)
- every row `low <= high`
- a service with fewer than `minSources` (3) sources is reported as a **drop**
- `status` must be `"live"` before the widget stops showing the SAMPLE banner

One table per trade per city. Every business in that city uses the same table; the business config
(`configs/*.json`) maps its own service names onto the table's rows with `services[].row`.
