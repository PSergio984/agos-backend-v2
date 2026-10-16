# 0001 — Dormant `admin_site_assignments` join table

`admin_site_assignments(admin_user_id, location_id)` exists as provision for
future site-scoped admin assignment (decision record #20, ticket #23). It is
deliberately dormant: no service, schema, endpoint, or enforcement reads or
writes it — the site-manager authorization matrix is undecided, but the join
shape (many admins ↔ many sites, unique pair) is settled, so the table lands
now rather than in the same change as the feature. When the feature ships:
assigning is a superuser power, and the frontend presents single-select first.
