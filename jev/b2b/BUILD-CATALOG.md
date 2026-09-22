# Building a real catalog

`catalogs/valley-sign-co.json` is a **sample**. Hand this to Claude Code for a real shop:

```
build the b2b version of the widget for [business].

- pull their product catalog and their listed prices from their site
- cut the prices into bands by quantity or size. the bands must not overlap
- one jev call asks: which item from the catalog, how many on a scale, rush true or false,
  installation true or false, design needed true or false, needs a call true or false
- anything that needs a call shows a booking link instead of a range
- anything over [their top band] always goes to a call
- write the catalog into jev/b2b/catalogs/[business].json in the format of valley-sign-co.json,
  set "status": "live", put the price page URLs in each item's "sources"
- show me the bands before you build the widget:
  run `node jev/quote/validate-table.js jev/b2b/catalogs/[business].json` and print the bands table
```

Rules the validator enforces (`../quote/validate-table.js`):

- `quantityBands` are contiguous (`min` = previous `max` + 1) so they cannot overlap; the last one may have `max: null`
- every band `low <= high`
- an item with fewer than `minSources` sources is reported as a **drop**
- a quantity above the last band's `max` goes to a call (`engine.js`, `bandByQuantity` returns null)
