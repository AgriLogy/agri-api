"""KML / KMZ → GeoJSON polygons for sector import (agri-api#459 phase 3).

Only what a farm outline needs: every ``Placemark`` with a ``Polygon`` (or a
``MultiGeometry`` of polygons) becomes one named shape; points and lines are
reported as skipped. KML coordinates are ``lon,lat[,alt]`` — altitude dropped.

Safety: the upload is size-capped by the caller, a KMZ is read for its first
``.kml`` entry only (size-capped again after decompression), and any document
carrying a DTD is refused before parsing, which rules out entity-expansion
("billion laughs") and external-entity tricks without an extra dependency.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

MAX_KML_BYTES = 2 * 1024 * 1024
MAX_KML_UNZIPPED_BYTES = 10 * 1024 * 1024


class KmlError(ValueError):
    """The file cannot be read as KML; the message is shown to the farmer."""


@dataclass
class ParsedShape:
    name: str
    geometry: dict[str, Any] | None
    reason: str | None = None  # set when skipped


@dataclass
class ParsedKml:
    shapes: list[ParsedShape] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _find_all(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el.iter() if _local(c.tag) == name]


def _ring(el: ET.Element | None) -> list[list[float]] | None:
    if el is None:
        return None
    coords = next((c for c in el.iter() if _local(c.tag) == "coordinates"), None)
    if coords is None or not (coords.text or "").strip():
        return None
    ring: list[list[float]] = []
    for token in coords.text.split():
        parts = token.split(",")
        if len(parts) < 2:
            return None
        try:
            ring.append([round(float(parts[0]), 7), round(float(parts[1]), 7)])
        except ValueError:
            return None
    if len(ring) >= 3 and ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring if len(ring) >= 4 else None


def _polygon(el: ET.Element) -> list[list[list[float]]] | None:
    outer = next(iter(_find_all(el, "outerBoundaryIs")), None)
    shell = _ring(outer)
    if shell is None:
        return None
    holes = [r for r in (_ring(h) for h in _find_all(el, "innerBoundaryIs")) if r]
    return [shell, *holes]


def read_kml_bytes(data: bytes, filename: str = "") -> bytes:
    """Return the KML document bytes (unwrapping a KMZ)."""
    if len(data) > MAX_KML_BYTES:
        raise KmlError("Fichier trop volumineux (2 Mo maximum).")
    if filename.lower().endswith(".kmz") or data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as kmz:
                entry = next(
                    (i for i in kmz.infolist() if i.filename.lower().endswith(".kml")),
                    None,
                )
                if entry is None:
                    raise KmlError("Archive KMZ sans fichier .kml.")
                if entry.file_size > MAX_KML_UNZIPPED_BYTES:
                    raise KmlError("Fichier KML décompressé trop volumineux.")
                return kmz.read(entry)[: MAX_KML_UNZIPPED_BYTES + 1]
        except zipfile.BadZipFile as exc:
            raise KmlError("Archive KMZ illisible.") from exc
    return data


def parse_kml(data: bytes, filename: str = "") -> ParsedKml:
    doc = read_kml_bytes(data, filename)
    head = doc[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in doc.lower():
        raise KmlError("Fichier KML refusé (déclaration DTD non autorisée).")
    try:
        root = ET.fromstring(doc)
    except ET.ParseError as exc:
        raise KmlError("Fichier KML illisible.") from exc
    if _local(root.tag) != "kml":
        raise KmlError("Ce fichier n'est pas un document KML.")

    out = ParsedKml()
    for i, placemark in enumerate(_find_all(root, "Placemark"), start=1):
        name_el = next(iter(_children(placemark, "name")), None)
        name = ((name_el.text if name_el is not None else "") or "").strip()[:100]
        name = name or f"Secteur importé {i}"
        polygons = [
            p for p in (_polygon(el) for el in _find_all(placemark, "Polygon")) if p
        ]
        has_polygon_tag = bool(_find_all(placemark, "Polygon"))
        if not polygons:
            out.shapes.append(
                ParsedShape(
                    name,
                    None,
                    "invalid_geometry" if has_polygon_tag else "not_a_polygon",
                )
            )
            continue
        geometry = (
            {"type": "Polygon", "coordinates": polygons[0]}
            if len(polygons) == 1
            else {"type": "MultiPolygon", "coordinates": polygons}
        )
        out.shapes.append(ParsedShape(name, geometry))
    return out
