"""Write OsmChange (.osc) files for offline review in JOSM.

This module does not upload anything to OpenStreetMap. It only produces a
.osc file that you load in JOSM (File -> Open) to inspect each proposed
maxspeed change and upload manually after verifying the posted signs.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

from .analyzer import SpeedCandidate


def write_osc(candidates: list[SpeedCandidate], path: str, units: str = "mph") -> None:
    """Write an OsmChange (.osc) file for offline review in JOSM.

    JOSM can load .osc with File -> Open and inspect every change before upload.
    """
    osc = ET.Element("osmChange", attrib={"version": "0.6", "generator": "comma-osm-speed/0.1"})
    modify = ET.SubElement(osc, "modify")
    for cand in candidates:
        # We don't have the way geometry here — for review purposes a stub
        # way element with just the new tag is enough; JOSM will pull the
        # rest from the server when uploading and do its conflict resolution.
        way = ET.SubElement(
            modify,
            "way",
            attrib={
                "id": str(cand.way_id),
                "version": str(cand.osm_version),
                "changeset": "-1",
            },
        )
        ET.SubElement(way, "tag", attrib={"k": "maxspeed", "v": cand.proposed_maxspeed_tag(units=units)})
        if cand.name_tag:
            ET.SubElement(way, "tag", attrib={"k": "name", "v": cand.name_tag})
        if cand.highway_tag:
            ET.SubElement(way, "tag", attrib={"k": "highway", "v": cand.highway_tag})
    tree = ET.ElementTree(osc)
    tree.write(path, encoding="utf-8", xml_declaration=True)
