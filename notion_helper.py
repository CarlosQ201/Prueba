import os
import logging
from notion_client import Client

logger = logging.getLogger(__name__)

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_PAGE_ID = os.environ["NOTION_PAGE_ID"]

notion = Client(auth=NOTION_TOKEN)
DATABASE_NAME = "Bot Notas"
_database_id: str | None = None


def _init_database() -> str:
    global _database_id
    if _database_id:
        return _database_id

    results = notion.search(
        query=DATABASE_NAME,
        filter={"property": "object", "value": "database"},
    )
    for obj in results["results"]:
        title_parts = obj.get("title", [])
        if title_parts and title_parts[0]["plain_text"] == DATABASE_NAME:
            _database_id = obj["id"]
            logger.info("Notion DB encontrada: %s", _database_id)
            return _database_id

    db = notion.databases.create(
        parent={"type": "page_id", "page_id": NOTION_PAGE_ID},
        title=[{"type": "text", "text": {"content": DATABASE_NAME}}],
        properties={
            "Nota": {"title": {}},
            "Categoría": {"select": {}},
            "Tipo": {
                "select": {
                    "options": [
                        {"name": "texto", "color": "blue"},
                        {"name": "link", "color": "green"},
                        {"name": "foto", "color": "orange"},
                    ]
                }
            },
            "URL": {"url": {}},
        },
    )
    _database_id = db["id"]
    logger.info("Notion DB creada: %s", _database_id)
    return _database_id


def get_categories() -> list[str]:
    db_id = _init_database()
    db = notion.databases.retrieve(db_id)
    options = db["properties"]["Categoría"]["select"].get("options", [])
    return [opt["name"] for opt in options]


def add_note(content: str, category: str, note_type: str, url: str | None = None) -> None:
    db_id = _init_database()
    properties: dict = {
        "Nota": {"title": [{"text": {"content": content[:2000]}}]},
        "Categoría": {"select": {"name": category}},
        "Tipo": {"select": {"name": note_type}},
    }
    if url:
        properties["URL"] = {"url": url}
    notion.pages.create(parent={"database_id": db_id}, properties=properties)


def get_notes_by_category(category: str) -> list[dict]:
    db_id = _init_database()
    result = notion.databases.query(
        database_id=db_id,
        filter={"property": "Categoría", "select": {"equals": category}},
    )
    return _parse_pages(result["results"])


def get_all_notes() -> list[dict]:
    db_id = _init_database()
    result = notion.databases.query(database_id=db_id)
    return _parse_pages(result["results"])


def _parse_pages(pages: list) -> list[dict]:
    notes = []
    for page in pages:
        props = page["properties"]
        title_parts = props.get("Nota", {}).get("title", [])
        cat = props.get("Categoría", {}).get("select")
        tipo = props.get("Tipo", {}).get("select")
        url_prop = props.get("URL", {})
        notes.append({
            "content": title_parts[0]["plain_text"] if title_parts else "",
            "category": cat["name"] if cat else "",
            "type": tipo["name"] if tipo else "",
            "url": url_prop.get("url") if url_prop else None,
        })
    return notes
