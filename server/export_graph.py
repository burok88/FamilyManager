"""Exportiert alle Knoten und Kanten aus Kùzu als JSON, für die Visualisierungs-Seite."""

import json
from pathlib import Path

import kuzu

BASE = Path(__file__).resolve().parent.parent  # Projektordner
DB_PATH = BASE / "kuzu_db"
EXPORT_PATH = BASE / "export" / "graph.json"

NODE_TABLES = {
    "Person": "name",
    "Struktur": "name",
    "Aufgabe": "name",
    "Prozess": "name",
    "Termin": "datum",
    "Ressource": "name",
    "Schulstunde": "fach",
    "Dokument": "titel",
}

REL_QUERIES = [
    ("GEHOERT_ZU", "MATCH (a:Person)-[:GEHOERT_ZU]->(b:Struktur) RETURN a.id, b.id"),
    (
        "VERANTWORTLICH_FUER",
        """MATCH (a:Person)-[r:VERANTWORTLICH_FUER]->(b:Aufgabe)
           RETURN a.id, b.id, r.vorlaufzeit_tage""",
    ),
    ("AUFGABE_AN_ORT", "MATCH (a:Aufgabe)-[:AUFGABE_AN_ORT]->(b:Struktur) RETURN a.id, b.id"),
    (
        "TEIL_VON",
        "MATCH (a:Aufgabe)-[r:TEIL_VON]->(b:Prozess) RETURN a.id, b.id, r.reihenfolge",
    ),
    ("BEZIEHT_SICH_AUF", "MATCH (a:Aufgabe)-[:BEZIEHT_SICH_AUF]->(b:Ressource) RETURN a.id, b.id"),
    (
        "RESSOURCE_HAT_TERMIN",
        "MATCH (a:Ressource)-[:RESSOURCE_HAT_TERMIN]->(b:Termin) RETURN a.id, b.id",
    ),
    ("AUFGABE_HAT_TERMIN", "MATCH (a:Aufgabe)-[:AUFGABE_HAT_TERMIN]->(b:Termin) RETURN a.id, b.id"),
    ("BETRIFFT", "MATCH (a:Aufgabe)-[:BETRIFFT]->(b:Person) RETURN a.id, b.id"),
    ("HAT_STUNDE", "MATCH (a:Person)-[:HAT_STUNDE]->(b:Schulstunde) RETURN a.id, b.id"),
    ("DOKUMENT_BETRIFFT", "MATCH (a:Dokument)-[:DOKUMENT_BETRIFFT]->(b:Person) RETURN a.id, b.id"),
]


def export_nodes(conn: kuzu.Connection) -> list[dict]:
    nodes = []
    for label, name_field in NODE_TABLES.items():
        result = conn.execute(f"MATCH (n:{label}) RETURN n.*")
        col_names = result.get_column_names()
        while result.has_next():
            row = result.get_next()
            props = dict(zip(col_names, row))
            node_id = props["n.id"]
            label_text = str(props.get(f"n.{name_field}", node_id))
            props_clean = {k.replace("n.", ""): _jsonable(v) for k, v in props.items()}
            nodes.append({"id": node_id, "label": label_text, "type": label, "props": props_clean})
    return nodes


def export_edges(conn: kuzu.Connection) -> list[dict]:
    edges = []
    for rel_type, query in REL_QUERIES:
        result = conn.execute(query)
        while result.has_next():
            row = result.get_next()
            from_id, to_id = row[0], row[1]
            extra = row[2] if len(row) > 2 else None
            edges.append({"from": from_id, "to": to_id, "type": rel_type, "extra": _jsonable(extra)})
    return edges


def _jsonable(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def main() -> None:
    db = kuzu.Database(str(DB_PATH))
    conn = kuzu.Connection(db)

    graph = {"nodes": export_nodes(conn), "edges": export_edges(conn)}

    EXPORT_PATH.parent.mkdir(exist_ok=True)
    EXPORT_PATH.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Graph exportiert: {len(graph['nodes'])} Knoten, {len(graph['edges'])} Kanten -> {EXPORT_PATH}")


if __name__ == "__main__":
    main()
