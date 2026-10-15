# Glossary

Domain vocabulary for AGOS. Code names that differ are noted; keep UI/docs on the domain word.

## Site

A monitored location: one river/waterway post with its sensors, cameras, responders, and alert stream. Schema name: `locations` / `location_id`. The system runs many sites; single-site assumptions are legacy (see map #1, ticket #2 audit).

## Responder

Field worker (a cleaner) who receives and acts on blockage alerts. Schema: `responders`. Has a **home site** (`responders.location_id`) and may hold **deployments** to other sites.

## Deployment

A responder's temporary posting to a site — site + active window. Schema: `responder_deployments`. Membership (who may deploy where) is admin-set; presence (where they are today) is responder-marked, admin-overridable.

## Active site / presence

The site a responder is currently on — the only site whose alerts reach them. Responder-app site picker sets this. Distinct from home site and from admin's view scope.

## View scope

The site an admin is looking at in agos-admin — dropdown filters sensors/readings/cameras/alerts. Same control as the responder picker but means "looking at", not "being at".

## Dispatch

The shared alert entity (`notification_dispatches`) fanned out to responders as deliveries. Gains `location_id` (nullable; NULL = global announcement) per #3.

## Claim

A responder's "On it" on a blockage dispatch — first-wins, broadcasts `alert_claimed` to the dispatch's site. Distinct from **acknowledgement** (the closing/handling log).

## Site wall / multi-view

Proposed (pending #5): admin grid view showing all sites at once — CCTV-wall style tiles. Contrasts with the site picker (single-site focus).
