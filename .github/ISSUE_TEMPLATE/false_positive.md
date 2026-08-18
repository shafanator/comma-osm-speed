---
name: False-positive report
about: A way was flagged as a maxspeed candidate but the existing OSM tag is actually correct
title: "[false-positive] way <id> — "
labels: false-positive
---

**OSM way**
Link: https://www.openstreetmap.org/way/<id>

**What the tool proposed**
- Observed (85th pct): e.g. 47 mph
- Proposed: e.g. 45 mph
- Current OSM `maxspeed`: e.g. 35 mph

**Why it's a false positive**
<!-- e.g. posted sign verified at 35 on Mapillary; implicit residential default; map-matcher snapped to the wrong parallel way; school zone / conditional limit; etc. -->

**Evidence (if any)**
<!-- Mapillary / Street View link, photo, local knowledge -->

**Environment**
- OS:
- comma-osm-speed version (`comma-osm-speed --version` or commit):
- Region / country:
