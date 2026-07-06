"""FastAPI server for the Products R&D Viewer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

try:
    from .catalog import find_rnd_root, list_runs_for_product, pick_representative_asset, product_assets, product_card
except ImportError:  # pragma: no cover
    from catalog import find_rnd_root, list_runs_for_product, pick_representative_asset, product_assets, product_card


TOOL_ROOT = Path(__file__).resolve().parent
try:
    RND_ROOT = find_rnd_root(TOOL_ROOT)
except FileNotFoundError:
    # Web-server deployments can run as a standalone Supabase viewer package
    # without the full RND_SKILL repository and 20_Data folder.
    RND_ROOT = TOOL_ROOT


def load_viewer_env() -> None:
    env_paths = [
        TOOL_ROOT / ".env",
        TOOL_ROOT.parent / "scripts" / ".env.rnd",
    ]
    for env_path in env_paths:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_vscode_pg_settings() -> None:
    settings_path = RND_ROOT / ".vscode" / "settings.json"
    if not settings_path.exists():
        return
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return
    for conn in settings.get("sqltools.connections", []):
        if "postgres" not in str(conn.get("driver", "")).lower():
            continue
        mapping = {
            "PG_HOST": conn.get("server"),
            "PG_PORT": conn.get("port"),
            "PG_DATABASE": conn.get("database"),
            "PG_USER": conn.get("username"),
            "PG_PASSWORD": conn.get("password"),
        }
        for key, value in mapping.items():
            if value not in (None, ""):
                os.environ.setdefault(key, str(value))
        break


load_viewer_env()
load_vscode_pg_settings()

DATA_DIR = Path(os.environ.get("RND_DATA_DIR", str(RND_ROOT / "20_Data")))
PRODUCTS_DIR = DATA_DIR / "products"
STATIC_DIR = TOOL_ROOT / "static"
HIDDEN_PRODUCTS_PATH = DATA_DIR / "index" / "products_rnd_viewer_hidden.json"
HIDE_DATA_ONLY_PRODUCTS = os.environ.get("PRODUCTS_RND_HIDE_DATA_ONLY", "1").strip().lower() not in {"0", "false", "no", "off"}
try:
    RECENT_PRODUCT_LIMIT = max(1, int(os.environ.get("PRODUCTS_RND_RECENT_LIMIT", "20")))
except ValueError:
    RECENT_PRODUCT_LIMIT = 20

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
PG_SCHEMA = os.environ.get("PG_SCHEMA", "ax_dev")
PG_LAST_ERROR = ""

app = FastAPI(title="Products R&D Viewer")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
if DATA_DIR.exists():
    app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")
dashboard_assets = RND_ROOT / "40_Dashboard" / "assets"
if dashboard_assets.exists():
    app.mount("/dashboard-assets", StaticFiles(directory=str(dashboard_assets)), name="dashboard-assets")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge() -> str:
    return (STATIC_DIR / "knowledge.html").read_text(encoding="utf-8")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    mode = "postgres" if postgres_enabled() else "supabase" if supabase_enabled() else "local"
    return {
        "ok": True,
        "mode": mode,
        "can_delete": False if mode == "postgres" else bool(SUPABASE_SERVICE_ROLE_KEY) if mode == "supabase" else True,
        "rnd_root": str(RND_ROOT),
        "products_dir": str(PRODUCTS_DIR),
        "postgres_schema": PG_SCHEMA if postgres_enabled() else "",
        "postgres_error": PG_LAST_ERROR,
        "hide_data_only_products": HIDE_DATA_ONLY_PRODUCTS,
        "recent_product_limit": RECENT_PRODUCT_LIMIT,
    }


@app.get("/api/products")
async def products(
    request: FastAPIRequest,
    q: str = "",
    brand: str = "",
    ownership: str = "",
    status: str = "",
    updated_after: str = "",
    recent: str = "",
    x_department_id: str = Header("", alias="X-Department-Id"),
) -> dict[str, Any]:
    if postgres_enabled():
        return postgres_products(q, brand, ownership, status, updated_after, recent)
    if supabase_enabled():
        return supabase_products(auth_header(request), q, brand, ownership, status, updated_after, recent)
    items = local_products(q=q, brand=brand, ownership=ownership, status=status, updated_after=updated_after, recent=recent, department_id=x_department_id)
    return {"products": items, "mode": "local"}


@app.get("/api/products/{product_id}/runs")
async def product_runs(product_id: str, request: FastAPIRequest) -> dict[str, Any]:
    if postgres_enabled():
        return postgres_product_runs(product_id)
    if supabase_enabled():
        return supabase_product_runs(auth_header(request), product_id)
    product_dir = safe_product_dir(product_id)
    runs = [
        {
            "run_id": run["run_id"],
            "product_id": run["product_id"],
            "run_type": run["run_type"],
            "analysis_status": run["analysis_status"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "json_path": run["json_path"],
        }
        for run in list_runs_for_product(product_dir, RND_ROOT, DATA_DIR)
    ]
    return {"runs": runs, "mode": "local"}


@app.get("/api/knowledge-index")
async def knowledge_index(
    request: FastAPIRequest,
    q: str = "",
    brand: str = "",
    ownership: str = "",
    status: str = "",
    x_department_id: str = Header("", alias="X-Department-Id"),
) -> dict[str, Any]:
    if postgres_enabled():
        return postgres_knowledge_index(q=q, brand=brand, ownership=ownership, status=status)
    if supabase_enabled():
        return supabase_knowledge_index(auth_header(request), q=q, brand=brand, ownership=ownership, status=status)
    cards = local_products(q="", brand=brand, ownership=ownership, status=status, updated_after="", recent="", department_id=x_department_id)
    runs_by_product: dict[str, dict[str, Any] | None] = {}
    for card in cards:
        product_dir = safe_product_dir(card["product_id"])
        runs = list_runs_for_product(product_dir, RND_ROOT, DATA_DIR)
        runs_by_product[card["product_id"]] = runs[0] if runs else None
    return filter_knowledge_index(build_knowledge_index(cards, runs_by_product, "local"), q)


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str, request: FastAPIRequest) -> dict[str, Any]:
    if postgres_enabled():
        return postgres_run_detail(run_id)
    if supabase_enabled():
        return supabase_run_detail(auth_header(request), run_id)
    for product_dir in product_dirs():
        for run in list_runs_for_product(product_dir, RND_ROOT, DATA_DIR):
            if run["run_id"] == run_id:
                return {"run": run, "mode": "local"}
    raise HTTPException(status_code=404, detail="run not found")


@app.delete("/api/products/{product_id}")
async def hide_product(product_id: str, request: FastAPIRequest) -> dict[str, Any]:
    if postgres_enabled():
        raise HTTPException(status_code=403, detail="PostgreSQL viewer mode is read-only")
    if supabase_enabled():
        if not SUPABASE_SERVICE_ROLE_KEY:
            raise HTTPException(status_code=403, detail="SUPABASE_SERVICE_ROLE_KEY is required to hide products in Supabase mode")
        supabase_hide_product(product_id)
        return {"ok": True, "product_id": product_id, "mode": "supabase", "action": "hidden"}
    safe_product_dir(product_id)
    hidden = local_hidden_products()
    hidden.add(product_id)
    write_local_hidden_products(hidden)
    return {"ok": True, "product_id": product_id, "mode": "local", "action": "hidden"}


def postgres_enabled() -> bool:
    requested = os.environ.get("PRODUCTS_RND_VIEWER_DB", os.environ.get("VIEWER_DATA_MODE", "postgres")).strip().lower()
    if requested in {"local", "supabase", "off", "false", "0"}:
        return False
    return all(os.environ.get(key) for key in ["PG_HOST", "PG_DATABASE", "PG_USER", "PG_PASSWORD"])


def pg_schema() -> str:
    schema = os.environ.get("PG_SCHEMA", PG_SCHEMA).strip() or "ax_dev"
    if not schema.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid PostgreSQL schema")
    return schema


def postgres_conn():
    global PG_LAST_ERROR
    try:
        import psycopg2
        import psycopg2.extras
    except Exception as exc:
        PG_LAST_ERROR = f"psycopg2 import failed: {exc}"
        raise HTTPException(status_code=500, detail="psycopg2-binary is required for PostgreSQL mode")
    try:
        conn = psycopg2.connect(
            host=os.environ.get("PG_HOST"),
            port=int(os.environ.get("PG_PORT", "5432")),
            dbname=os.environ.get("PG_DATABASE"),
            user=os.environ.get("PG_USER"),
            password=os.environ.get("PG_PASSWORD"),
            connect_timeout=8,
            options=f"-c search_path={pg_schema()},public",
        )
        PG_LAST_ERROR = ""
        return conn
    except Exception as exc:
        PG_LAST_ERROR = str(exc)
        raise HTTPException(status_code=503, detail=f"PostgreSQL 연결 실패: {exc}")


def postgres_fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    import psycopg2.extras
    with postgres_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def postgres_fetchone(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    import psycopg2.extras
    with postgres_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def postgres_products(q: str, brand: str, ownership: str, status: str, updated_after: str, recent: str = "") -> dict[str, Any]:
    rows = postgres_product_rows()
    cards = [postgres_product_card(row) for row in rows]
    hidden = local_hidden_products()
    cards = [card for card in cards if card.get("product_id") not in hidden]
    cards = filter_displayable_products(cards)
    if q:
        needle = q.lower()
        cards = [
            card for card in cards
            if needle in " ".join(str(card.get(k, "")) for k in ["brand", "product_name", "display_name", "color", "sku", "ownership_label", "category", "season", "project_code"]).lower()
        ]
    if brand:
        cards = [card for card in cards if card.get("brand") == brand]
    if ownership and not truthy(recent):
        cards = [card for card in cards if card.get("ownership") == ownership]
    if status:
        cards = [card for card in cards if card.get("analysis_status") == status]
    if updated_after:
        cards = [card for card in cards if str(card.get("latest_run_updated_at") or "") >= updated_after]
    if truthy(recent):
        cards = recent_product_cards(cards)
    else:
        cards = sorted(cards, key=lambda item: str(item.get("latest_run_updated_at") or ""), reverse=True)
    return {"products": cards, "mode": "postgres"}


def postgres_product_rows(product_id: str = "") -> list[dict[str, Any]]:
    schema = pg_schema()
    where = "WHERE p.product_id = %s" if product_id else ""
    params: tuple[Any, ...] = (product_id,) if product_id else ()
    return postgres_fetchall(
        f"""
        WITH ranked AS (
            SELECT
                a.*,
                COUNT(*) OVER (PARTITION BY a.product_id) AS analysis_count,
                ROW_NUMBER() OVER (
                    PARTITION BY a.product_id
                    ORDER BY COALESCE(a.created_at, a.uploaded_at) DESC NULLS LAST,
                             a.agent_count DESC NULLS LAST,
                             a.analysis_id DESC
                ) AS rn
            FROM {schema}.rnd_analyses a
        )
        SELECT
            p.product_id, p.brand, p.product_name, p.display_name, p.color, p.sku,
            p.price, p.currency, p.source_url, p.final_url, p.site, p.description,
            p.captured_at, p.uploaded_at AS product_uploaded_at,
            p.representative_image_url AS db_representative_image_url,
            p.image_urls AS db_image_urls,
            r.analysis_id, r.schema_version, r.analysis_mode, r.agent_count,
            r.created_at AS analysis_created_at, r.uploaded_at AS analysis_uploaded_at,
            r.sections_json, r.ai_labels_json, r.qc_json, r.summary_text,
            COALESCE(r.analysis_count, 0) AS analysis_count
        FROM {schema}.rnd_products p
        LEFT JOIN ranked r ON r.product_id = p.product_id AND r.rn = 1
        {where}
        ORDER BY COALESCE(r.uploaded_at, r.created_at, p.uploaded_at) DESC NULLS LAST,
                 p.product_name ASC NULLS LAST
        """,
        params,
    )


def postgres_product_card(row: dict[str, Any]) -> dict[str, Any]:
    product_id = str(row.get("product_id") or "")
    local_card = local_product_card(product_id)
    final = postgres_final_from_row(row)
    assets = merged_product_assets(product_id, row)
    representative = pick_representative_asset(assets)
    ownership = local_card.get("ownership") if local_card else ""
    ownership = ownership or normalize_ownership_value(final.get("ownership") if isinstance(final, dict) else "") or normalize_ownership_value(row.get("site")) or "competitor"
    brand = row.get("brand") or local_card.get("brand", "") if local_card else row.get("brand") or ""
    product_name = row.get("product_name") or row.get("display_name") or local_card.get("product_name", "") if local_card else row.get("product_name") or row.get("display_name") or product_id
    color = row.get("color") or local_card.get("color", "") if local_card else row.get("color") or ""
    sku = row.get("sku") or local_card.get("sku", "") if local_card else row.get("sku") or ""
    source_url = row.get("final_url") or row.get("source_url") or local_card.get("source_url", "") if local_card else row.get("final_url") or row.get("source_url") or ""
    # 이미지 URL 우선순위: DB(S3) > 로컬 파일시스템 > local_card
    db_rep_url = row.get("db_representative_image_url") or ""
    db_image_urls = row.get("db_image_urls")
    asset_urls = display_image_urls([asset.get("public_url") or asset.get("url") for asset in assets])
    representative_image_url = (
        first_display_image_url(db_rep_url)
        or first_display_image_url(db_image_urls)
        or first_display_image_url(asset_urls)
        or first_display_image_url(representative.get("url") if representative else "")
        or first_display_image_url(local_card.get("representative_image_url", "") if local_card else "")
    )
    representative_asset_id = representative.get("asset_id") if representative else local_card.get("representative_asset_id", "") if local_card else ""
    return {
        "product_id": product_id,
        "style": product_id,
        "brand": brand,
        "product_name": product_name,
        "display_name": row.get("display_name") or row.get("product_name") or product_id,
        "color": color,
        "sku": sku,
        "price": row.get("price"),
        "currency": row.get("currency") or "KRW",
        "source_url": source_url,
        "analysis_count": int(row.get("analysis_count") or 0),
        "latest_run_id": row.get("analysis_id") or "",
        "latest_run_updated_at": row.get("analysis_uploaded_at") or row.get("analysis_created_at") or row.get("product_uploaded_at") or "",
        "registered_at": row.get("product_uploaded_at") or row.get("captured_at") or row.get("analysis_uploaded_at") or row.get("analysis_created_at") or "",
        "analysis_status": "complete" if row.get("analysis_id") else "not_analyzed",
        "representative_image_url": representative_image_url,
        "image_urls": display_image_urls(db_image_urls, asset_urls),
        "representative_asset_id": representative_asset_id,
        "ownership": ownership,
        "ownership_label": ownership_label(ownership),
        "category": local_card.get("category", "") if local_card else "",
        "season": local_card.get("season", "") if local_card else "",
        "project_code": local_card.get("project_code", "") if local_card else "",
        "last_name": local_card.get("last_name", "") if local_card else postgres_last_name(final),
        "tags": postgres_tags(row, ownership, final),
    }


def local_product_card(product_id: str) -> dict[str, Any]:
    if not product_id:
        return {}
    product_dir = PRODUCTS_DIR / product_id
    if not product_dir.exists() or not product_dir.is_dir():
        return {}
    try:
        return product_card(product_dir, RND_ROOT, DATA_DIR)
    except Exception:
        return {}


def product_assets_for_id(product_id: str) -> list[dict[str, Any]]:
    product_dir = PRODUCTS_DIR / product_id
    if not product_dir.exists() or not product_dir.is_dir():
        return []
    try:
        return product_assets(product_dir, RND_ROOT, DATA_DIR)
    except Exception:
        return []


def db_image_assets(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = ensure_json_object(row.get("db_image_urls"))
    assets: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for index, (view, url) in enumerate(raw.items()):
            if not url:
                continue
            view_name = str(view or f"image_{index + 1}")
            assets.append(
                {
                    "asset_id": f"db_{view_name}",
                    "asset_kind": "db",
                    "view_name": view_name,
                    "view": view_name,
                    "file_name": view_name,
                    "name": view_name,
                    "url": str(url),
                    "public_url": str(url),
                }
            )
    rep_url = row.get("db_representative_image_url") or ""
    if rep_url and all((asset.get("public_url") or asset.get("url")) != rep_url for asset in assets):
        assets.insert(
            0,
            {
                "asset_id": "db_representative",
                "asset_kind": "db",
                "view_name": "representative",
                "view": "representative",
                "file_name": "representative",
                "name": "representative",
                "url": str(rep_url),
                "public_url": str(rep_url),
            },
        )
    return assets


def merged_product_assets(product_id: str, row: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in [*(db_image_assets(row or {}) if row else []), *product_assets_for_id(product_id)]:
        url = str(asset.get("public_url") or asset.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(asset)
    return merged


def postgres_tags(row: dict[str, Any], ownership: str, final: dict[str, Any]) -> list[str]:
    tags = [ownership_label(ownership)]
    for value in [postgres_last_name(final), row.get("site"), row.get("brand"), row.get("sku")]:
        if value:
            tags.append(str(value))
    labels = final.get("ai_labels") if isinstance(final, dict) else {}
    if isinstance(labels, dict):
        for key, value in labels.items():
            score = value.get("score") if isinstance(value, dict) else value
            if score is not None:
                tags.append(f"{key} {score}")
    return tags[:8]


def postgres_last_name(final: dict[str, Any]) -> str:
    real_data = final.get("real_data") if isinstance(final, dict) else {}
    if isinstance(real_data, dict) and real_data.get("last_name_display"):
        return str(real_data["last_name_display"])
    sections = final.get("sections") if isinstance(final, dict) else {}
    last_field = sections.get("last_spec", {}).get("last_name", {}) if isinstance(sections, dict) else {}
    if isinstance(last_field, dict) and last_field.get("value"):
        return str(last_field["value"])
    return ""


def postgres_product_runs(product_id: str) -> dict[str, Any]:
    schema = pg_schema()
    rows = postgres_fetchall(
        f"""
        SELECT a.*, p.brand, p.product_name, p.display_name
        FROM {schema}.rnd_analyses a
        LEFT JOIN {schema}.rnd_products p USING(product_id)
        WHERE a.product_id = %s
        ORDER BY COALESCE(a.created_at, a.uploaded_at) DESC NULLS LAST,
                 a.agent_count DESC NULLS LAST
        """,
        (product_id,),
    )
    return {"runs": [postgres_run_payload(row) for row in rows], "mode": "postgres"}


def postgres_run_detail(run_id: str) -> dict[str, Any]:
    schema = pg_schema()
    row = postgres_fetchone(
        f"""
        SELECT
            a.*,
            p.brand,
            p.product_name,
            p.display_name,
            p.representative_image_url AS db_representative_image_url,
            p.image_urls AS db_image_urls
        FROM {schema}.rnd_analyses a
        LEFT JOIN {schema}.rnd_products p USING(product_id)
        WHERE a.analysis_id = %s
        LIMIT 1
        """,
        (run_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": postgres_run_payload(row), "mode": "postgres"}


def postgres_knowledge_index(q: str, brand: str, ownership: str, status: str) -> dict[str, Any]:
    cards = postgres_products(q="", brand=brand, ownership=ownership, status=status, updated_after="")["products"]
    card_ids = {card.get("product_id") for card in cards}
    runs_by_product: dict[str, dict[str, Any] | None] = {}
    for row in postgres_product_rows():
        product_id = str(row.get("product_id") or "")
        if product_id and product_id in card_ids:
            runs_by_product[product_id] = postgres_run_payload(row) if row.get("analysis_id") else None
    return filter_knowledge_index(build_knowledge_index(cards, runs_by_product, "postgres"), q)


def postgres_run_payload(row: dict[str, Any]) -> dict[str, Any]:
    product_id = str(row.get("product_id") or "")
    final = postgres_final_from_row(row)
    assets = merged_product_assets(product_id, row)
    return {
        "run_id": row.get("analysis_id") or "",
        "product_id": product_id,
        "run_type": "postgres_analysis",
        "analysis_status": "complete" if row.get("analysis_id") else "not_analyzed",
        "created_at": row.get("created_at") or row.get("analysis_created_at") or "",
        "updated_at": row.get("uploaded_at") or row.get("analysis_uploaded_at") or row.get("created_at") or "",
        "json_path": "",
        "final_analysis": final,
        "coverage": {},
        "assets": assets,
    }


def postgres_final_from_row(row: dict[str, Any]) -> dict[str, Any]:
    sections = visible_sections(ensure_json_object(row.get("sections_json")))
    ai_labels = ensure_json_object(row.get("ai_labels_json"))
    qc = ensure_json_object(row.get("qc_json"))
    return {
        "style": row.get("product_id") or "",
        "brand": row.get("brand") or "",
        "product_name": row.get("product_name") or row.get("display_name") or "",
        "schema_version": row.get("schema_version") or "",
        "analysis_mode": row.get("analysis_mode") or "",
        "created_at": row.get("analysis_created_at") or row.get("created_at") or "",
        "summary": row.get("summary_text") or "",
        "sections": sections,
        "ai_labels": ai_labels,
        "qc": qc,
    }


def visible_sections(sections: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in sections.items() if key != "quality_checklist"}


def ensure_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def auth_header(request: FastAPIRequest | None = None) -> str:
    provided = request.headers.get("authorization", "") if request else ""
    return provided or f"Bearer {SUPABASE_ANON_KEY}"


def supabase_request(path: str, auth_header: str, query: dict[str, str] | None = None) -> Any:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if query:
        url += "?" + urlencode(query)
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": auth_header,
        "Accept": "application/json",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def supabase_products(auth_header: str, q: str, brand: str, ownership: str, status: str, updated_after: str, recent: str = "") -> dict[str, Any]:
    base_select = "product_id,style,brand,product_name,display_name,color,sku,price,currency,source_url,analysis_count,latest_run_id,latest_run_updated_at,analysis_status,representative_image_url,tags"
    filters = {
        "select": f"{base_select},metadata",
        "order": "latest_run_updated_at.desc.nullslast,product_name.asc",
        "published": "eq.true",
    }
    if brand:
        filters["brand"] = f"eq.{brand}"
    if status:
        filters["analysis_status"] = f"eq.{status}"
    if updated_after:
        filters["latest_run_updated_at"] = f"gte.{updated_after}"
    try:
        rows = supabase_request("product_cards", auth_header, filters)
    except Exception:
        fallback_filters = {**filters, "select": base_select}
        rows = supabase_request("product_cards", auth_header, fallback_filters)
    rows = [enrich_supabase_product(row) for row in rows]
    rows = filter_displayable_products(rows)
    if ownership and not truthy(recent):
        rows = [row for row in rows if row.get("ownership") == ownership]
    if q:
        needle = q.lower()
        rows = [
            row for row in rows
            if needle in " ".join(str(row.get(k, "")) for k in ["brand", "product_name", "display_name", "color", "sku", "ownership_label", "category", "season", "project_code", "last_name"]).lower()
        ]
    if truthy(recent):
        rows = recent_product_cards(rows)
    return {"products": rows, "mode": "supabase"}


def enrich_supabase_product(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    ownership = normalize_ownership_value(
        metadata.get("ownership")
        or metadata.get("owner_type")
        or metadata.get("source_scope")
        or metadata.get("product_scope")
        or metadata.get("company_scope")
    )
    if not ownership and any(metadata.get(key) is True for key in ["is_internal", "internal", "is_own_brand"]):
        ownership = "internal"
    if not ownership and any(metadata.get(key) is True for key in ["is_competitor", "competitor"]):
        ownership = "competitor"
    ownership = ownership or "competitor"
    return {
        **row,
        "ownership": ownership,
        "ownership_label": ownership_label(ownership),
        "category": metadata.get("category") or metadata.get("product_type") or "",
        "season": metadata.get("season") or "",
        "project_code": metadata.get("project_code") or metadata.get("style_number") or "",
        "image_urls": row.get("image_urls") or metadata.get("image_urls") or metadata.get("images") or [],
        "registered_at": row.get("registered_at") or row.get("created_at") or row.get("latest_run_updated_at") or "",
    }


def filter_displayable_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not HIDE_DATA_ONLY_PRODUCTS:
        return products
    return [product for product in products if product_has_display_image(product)]


def product_has_display_image(product: dict[str, Any]) -> bool:
    return bool(first_display_image_url(product.get("representative_image_url"), product.get("image_urls")))


def first_display_image_url(*values: Any) -> str:
    urls = display_image_urls(*values)
    return urls[0] if urls else ""


def display_image_urls(*values: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for value in values:
        for url in iter_image_urls(value):
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
    return urls


def iter_image_urls(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw[:1] in "[{":
            try:
                return iter_image_urls(json.loads(raw))
            except Exception:
                pass
        if "," in raw and not raw.startswith(("http://", "https://", "/")):
            return [url for part in raw.split(",") for url in iter_image_urls(part)]
        return [raw] if is_display_image_url(raw) else []
    if isinstance(value, (list, tuple, set)):
        return [url for item in value for url in iter_image_urls(item)]
    if isinstance(value, dict):
        preferred_keys = [
            "representative_image_url",
            "image_url",
            "public_url",
            "url",
            "src",
            "source_url",
            "thumbnail_url",
            "image_urls",
            "images",
            "assets",
        ]
        urls: list[str] = []
        for key in preferred_keys:
            if key in value:
                urls.extend(iter_image_urls(value.get(key)))
        if urls:
            return urls
        return [url for item in value.values() for url in iter_image_urls(item)]
    return []


def is_display_image_url(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lower = text.lower()
    if lower in {"null", "none", "undefined", "no image", "n/a", "na", "-"}:
        return False
    if lower.startswith(("http://", "https://", "/data/", "data:image/")):
        return True
    return lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"))


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "recent"}


def recent_product_cards(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(products, key=recent_product_sort_key, reverse=True)[:RECENT_PRODUCT_LIMIT]


def recent_product_sort_key(product: dict[str, Any]) -> str:
    return str(product.get("registered_at") or product.get("latest_run_updated_at") or product.get("updated_at") or "")


def normalize_ownership_value(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in {"internal", "own", "owned", "inhouse", "in-house", "our", "ours", "company", "fnf", "f&f", "self", "자사", "내부", "당사", "자체"}:
        return "internal"
    if raw in {"competitor", "external", "market", "benchmark", "third_party", "third-party", "other", "rival", "타사", "외부", "경쟁사", "벤치마크"}:
        return "competitor"
    if any(token in raw for token in ["자사", "내부", "당사", "own", "internal", "inhouse", "in-house"]):
        return "internal"
    if any(token in raw for token in ["타사", "외부", "경쟁", "competitor", "external", "benchmark"]):
        return "competitor"
    return ""


def ownership_label(value: str) -> str:
    return {"internal": "자사", "competitor": "타사"}.get(value, "미분류")


def supabase_hide_product(product_id: str) -> None:
    if "/" in product_id or "\\" in product_id or product_id in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="invalid product id")
    url = f"{SUPABASE_URL}/rest/v1/products?{urlencode({'product_id': f'eq.{product_id}'})}"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    req = Request(url, data=json.dumps({"published": False}).encode("utf-8"), headers=headers, method="PATCH")
    with urlopen(req, timeout=20) as response:
        response.read()


def supabase_product_runs(auth_header: str, product_id: str) -> dict[str, Any]:
    rows = supabase_request(
        "analysis_runs",
        auth_header,
        {
            "select": "run_id,product_id,run_type,analysis_status,created_at,updated_at,json_storage_path",
            "product_id": f"eq.{product_id}",
            "order": "updated_at.desc",
        },
    )
    return {"runs": rows, "mode": "supabase"}


def supabase_run_detail(auth_header: str, run_id: str) -> dict[str, Any]:
    rows = supabase_request(
        "analysis_runs",
        auth_header,
        {
            "select": "run_id,product_id,run_type,analysis_status,created_at,updated_at,final_analysis,coverage,analysis_assets(*)",
            "run_id": f"eq.{run_id}",
            "limit": "1",
        },
    )
    if not rows:
        raise HTTPException(status_code=404, detail="run not found")
    row = rows[0]
    return {
        "run": {
            **row,
            "assets": row.get("analysis_assets") or [],
        },
        "mode": "supabase",
    }


def supabase_knowledge_index(auth_header: str, q: str, brand: str, ownership: str, status: str) -> dict[str, Any]:
    product_rows = supabase_products(auth_header, q="", brand=brand, ownership=ownership, status=status, updated_after="")["products"]
    runs = supabase_request(
        "analysis_runs",
        auth_header,
        {
            "select": "run_id,product_id,run_type,analysis_status,created_at,updated_at,final_analysis,coverage",
            "order": "updated_at.desc",
        },
    )
    product_ids = {row.get("product_id") for row in product_rows}
    runs_by_product: dict[str, dict[str, Any] | None] = {str(product_id): None for product_id in product_ids if product_id}
    for run in runs:
        product_id = str(run.get("product_id") or "")
        if product_id in runs_by_product and runs_by_product[product_id] is None:
            runs_by_product[product_id] = {
                "run_id": run.get("run_id") or "",
                "product_id": product_id,
                "run_type": run.get("run_type") or "agent_run",
                "analysis_status": run.get("analysis_status") or "complete",
                "created_at": run.get("created_at") or "",
                "updated_at": run.get("updated_at") or "",
                "final_analysis": run.get("final_analysis") or {},
                "coverage": run.get("coverage") or {},
                "assets": [],
            }
    return filter_knowledge_index(build_knowledge_index(product_rows, runs_by_product, "supabase"), q)


def filter_knowledge_index(index: dict[str, Any], query: str) -> dict[str, Any]:
    needle = query.strip().lower()
    if not needle:
        return index
    matched_ids = {
        item["product_id"]
        for item in index.get("search_items", [])
        if needle in str(item.get("haystack") or "")
    }
    for product in index.get("products", []):
        haystack = " ".join(str(product.get(key) or "") for key in ["product_id", "product_name", "display_name", "brand", "sku", "color", "ownership_label", "category", "season", "project_code"]).lower()
        if needle in haystack:
            matched_ids.add(product["product_id"])
    filtered_products = [product for product in index.get("products", []) if product.get("product_id") in matched_ids]
    index = {**index, "products": filtered_products}
    index["search_items"] = [
        item for item in index.get("search_items", [])
        if item.get("product_id") in matched_ids and (needle in str(item.get("haystack") or "") or item.get("type") == "product")
    ]
    index["tree"] = filter_tree_for_products(index.get("tree", []), matched_ids)
    return index


def filter_tree_for_products(tree: list[dict[str, Any]], product_ids: set[str]) -> list[dict[str, Any]]:
    filtered_tree = []
    for owner in tree:
        brands = []
        for brand in owner.get("brands", []):
            products = [product for product in brand.get("products", []) if product.get("product_id") in product_ids]
            if products:
                brands.append({**brand, "products": products})
        if brands:
            filtered_tree.append({**owner, "brands": brands})
    return filtered_tree


def build_knowledge_index(cards: list[dict[str, Any]], runs_by_product: dict[str, dict[str, Any] | None], mode: str) -> dict[str, Any]:
    products = []
    search_items = []
    graph_nodes: dict[str, dict[str, Any]] = {}
    graph_links: dict[tuple[str, str, str], dict[str, Any]] = {}
    tree: dict[str, Any] = {}
    brands: set[str] = set()
    ownerships: set[str] = set()
    sections_seen: set[str] = set()

    for card in cards:
        product_id = str(card.get("product_id") or "")
        if not product_id:
            continue
        ownership = str(card.get("ownership") or "competitor")
        ownership_name = ownership_label(ownership)
        brand = str(card.get("brand") or "브랜드 미상")
        product_name = str(card.get("product_name") or card.get("display_name") or product_id)
        run = runs_by_product.get(product_id) or {}
        final = run.get("final_analysis") if isinstance(run, dict) else {}
        final = final if isinstance(final, dict) else {}
        sections = final.get("sections") if isinstance(final.get("sections"), dict) else {}
        section_payloads = []
        field_count = 0

        ownerships.add(ownership)
        brands.add(brand)
        tree.setdefault(ownership, {"id": ownership, "label": ownership_name, "brands": {}})
        tree[ownership]["brands"].setdefault(brand, {"id": f"{ownership}:{brand}", "label": brand, "products": []})

        add_graph_node(graph_nodes, f"ownership:{ownership}", ownership_name, "ownership", {"ownership": ownership})
        add_graph_node(graph_nodes, f"brand:{ownership}:{brand}", brand, "brand", {"ownership": ownership, "brand": brand})
        add_graph_link(graph_links, f"ownership:{ownership}", f"brand:{ownership}:{brand}", "contains")
        add_graph_node(graph_nodes, f"product:{product_id}", product_name, "product", {"product_id": product_id, "brand": brand, "ownership": ownership})
        add_graph_link(graph_links, f"brand:{ownership}:{brand}", f"product:{product_id}", "contains")

        for tag in (card.get("tags") or [])[:8]:
            tag_text = str(tag)
            tag_id = f"tag:{tag_text}"
            add_graph_node(graph_nodes, tag_id, tag_text, "tag", {})
            add_graph_link(graph_links, f"product:{product_id}", tag_id, "tagged")

        for section_name, payload in ordered_section_payloads(sections):
            fields = collect_spec_fields(payload, section_name=section_name)
            sections_seen.add(section_name)
            field_count += len(fields)
            section_id = f"section:{product_id}:{section_name}"
            add_graph_node(graph_nodes, section_id, section_label(section_name), "section", {"product_id": product_id, "section": section_name})
            add_graph_link(graph_links, f"product:{product_id}", section_id, "has_section")
            section_payloads.append(
                {
                    "name": section_name,
                    "label": section_label(section_name),
                    "field_count": len(fields),
                    "fields": fields,
                }
            )
            for field in fields[:18]:
                attr_id = f"attribute:{section_name}:{field['key']}"
                add_graph_node(graph_nodes, attr_id, field["label"], "attribute", {"section": section_name, "key": field["key"]})
                add_graph_link(graph_links, section_id, attr_id, "has_attribute")
                add_search_item(search_items, "attribute", product_id, product_name, brand, ownership, section_name, field["key"], field["label"], field.get("value", ""), field.get("evidence", ""), field.get("notes", ""))
                if field.get("evidence"):
                    add_search_item(search_items, "evidence", product_id, product_name, brand, ownership, section_name, field["key"], field["label"], field.get("value", ""), field.get("evidence", ""), field.get("notes", ""))

        product_payload = {
            **card,
            "latest_run_id": run.get("run_id") if isinstance(run, dict) else card.get("latest_run_id", ""),
            "latest_run_updated_at": run.get("updated_at") if isinstance(run, dict) else card.get("latest_run_updated_at", ""),
            "sections": section_payloads,
            "field_count": field_count,
            "summary": final.get("summary") or "",
            "manager_brief": final.get("manager_brief") or {},
            "ai_labels": final.get("ai_labels") or {},
        }
        products.append(product_payload)
        tree[ownership]["brands"][brand]["products"].append(
            {
                "product_id": product_id,
                "product_name": product_name,
                "field_count": field_count,
                "analysis_status": card.get("analysis_status") or "not_analyzed",
            }
        )
        add_search_item(search_items, "product", product_id, product_name, brand, ownership, "", "", product_name, " ".join(str(card.get(key) or "") for key in ["sku", "color", "category", "season", "project_code", "last_name"]), "", "")

    return {
        "mode": mode,
        "facets": {
            "ownerships": [{"value": item, "label": ownership_label(item)} for item in sorted(ownerships)],
            "brands": sorted(brands),
            "sections": [{"value": item, "label": section_label(item)} for item in sorted(sections_seen)],
        },
        "products": products,
        "tree": normalize_tree(tree),
        "search_items": search_items[:4000],
        "graph": {"nodes": list(graph_nodes.values()), "links": list(graph_links.values())},
    }


def add_search_item(
    search_items: list[dict[str, Any]],
    item_type: str,
    product_id: str,
    product_name: str,
    brand: str,
    ownership: str,
    section_name: str,
    key: str,
    title: str,
    value: Any,
    evidence: Any,
    notes: Any,
) -> None:
    haystack = " ".join(str(part or "") for part in [product_id, product_name, brand, ownership_label(ownership), section_label(section_name), key, title, value, evidence, notes])
    search_items.append(
        {
            "type": item_type,
            "product_id": product_id,
            "product_name": product_name,
            "brand": brand,
            "ownership": ownership,
            "ownership_label": ownership_label(ownership),
            "section": section_name,
            "section_label": section_label(section_name) if section_name else "",
            "key": key,
            "label": title,
            "value": stringify_compact(value, 240),
            "evidence": stringify_compact(evidence, 420),
            "notes": stringify_compact(notes, 260),
            "haystack": haystack.lower(),
        }
    )


def collect_spec_fields(data: Any, section_name: str, path: list[str] | None = None) -> list[dict[str, Any]]:
    path = path or []
    if is_spec_field(data):
        key = path[-1] if path else section_name
        return [
            {
                "section": section_name,
                "key": key,
                "path": ".".join(path),
                "label": field_label(key),
                "value": data.get("value"),
                "status": data.get("status"),
                "confidence": data.get("confidence"),
                "source": data.get("source"),
                "evidence": data.get("evidence"),
                "notes": data.get("notes"),
            }
        ]
    if isinstance(data, dict):
        fields: list[dict[str, Any]] = []
        for key, value in data.items():
            fields.extend(collect_spec_fields(value, section_name, [*path, str(key)]))
        return fields
    if isinstance(data, list):
        fields = []
        for index, value in enumerate(data, start=1):
            fields.extend(collect_spec_fields(value, section_name, [*path, str(index)]))
        return fields
    return []


def is_spec_field(data: Any) -> bool:
    return isinstance(data, dict) and any(key in data for key in ["value", "status", "confidence", "source", "evidence", "notes", "owner"])


def ordered_section_payloads(sections: dict[str, Any]) -> list[tuple[str, Any]]:
    order = {name: index for index, name in enumerate(["internal_real_data", "last_spec", "mold_spec", "outsole_spec", "material_spec", "arch_waist_spec", "midsole_material_spec", "accessory_spec", "pattern_analysis", "biomechanics_analysis", "color_spec", "design_review"])}
    return sorted(visible_sections(sections).items(), key=lambda item: (order.get(item[0], 999), section_label(item[0])))


def normalize_tree(tree: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for ownership_key in sorted(tree, key=lambda item: 0 if item == "internal" else 1):
        ownership = tree[ownership_key]
        brands = []
        for brand_key in sorted(ownership["brands"], key=lambda item: item.lower()):
            brand = ownership["brands"][brand_key]
            brand["products"] = sorted(brand["products"], key=lambda item: str(item.get("product_name") or "").lower())
            brands.append(brand)
        result.append({"id": ownership["id"], "label": ownership["label"], "brands": brands})
    return result


def add_graph_node(nodes: dict[str, dict[str, Any]], node_id: str, label: str, node_type: str, meta: dict[str, Any]) -> None:
    if node_id not in nodes:
        nodes[node_id] = {"id": node_id, "label": label, "type": node_type, **meta}


def add_graph_link(links: dict[tuple[str, str, str], dict[str, Any]], source: str, target: str, relation: str) -> None:
    links[(source, target, relation)] = {"source": source, "target": target, "relation": relation}


def stringify_compact(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        raw = json.dumps(value, ensure_ascii=False)
    else:
        raw = str(value)
    raw = " ".join(raw.split())
    return raw[:limit] + ("..." if len(raw) > limit else "")


def section_label(value: str) -> str:
    return {
        "last_spec": "라스트",
        "arch_waist_spec": "아치/허리",
        "mold_spec": "몰드/금형",
        "midsole_material_spec": "미드솔 물성",
        "outsole_spec": "아웃솔 상세",
        "material_spec": "소재",
        "pattern_analysis": "패턴",
        "accessory_spec": "부자재",
        "biomechanics_analysis": "생체역학",
        "color_spec": "컬러",
        "design_review": "디자인",
        "internal_real_data": "자사 실데이터",
        "quality_checklist": "품질",
    }.get(value, value)


def field_label(value: str) -> str:
    return {
        "toe_spring_mm": "토 스프링",
        "visual_estimated_toe_spring_mm": "토 스프링(시각 추정 백업)",
        "real_last_toe_spring_mm": "토 스프링(실측)",
        "instep_girth": "인스텝 둘레",
        "visual_estimated_instep_girth": "인스텝 둘레(시각 추정 백업)",
        "real_last_instep_girth_mm": "인스텝 둘레(실측)",
        "ball_girth": "볼 거스",
        "visual_estimated_ball_girth": "볼 거스(시각 추정 백업)",
        "real_last_ball_girth_mm": "볼 거스(실측)",
        "last_name": "라스트 NO",
        "mold_no": "몰드 NO",
        "pattern_no": "패턴 NO",
        "matched_last_data_no": "실측 연결 라스트",
        "real_last_bottom_length_mm": "저부장(실측)",
        "real_last_bottom_width_mm": "저부폭(실측)",
        "real_last_toe_thickness_mm": "토두께(실측)",
        "real_last_stick_length_mm": "스틱장(실측)",
    }.get(value, str(value or "").replace("_", " "))


def product_dirs() -> list[Path]:
    if not PRODUCTS_DIR.exists():
        return []
    hidden = local_hidden_products()
    return sorted((p for p in PRODUCTS_DIR.iterdir() if p.is_dir() and p.name not in hidden), key=lambda path: path.name.lower())


def local_hidden_products() -> set[str]:
    if not HIDDEN_PRODUCTS_PATH.exists():
        return set()
    try:
        data = json.loads(HIDDEN_PRODUCTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if isinstance(data, dict) and isinstance(data.get("hidden_product_ids"), list):
        return {str(item) for item in data["hidden_product_ids"]}
    if isinstance(data, list):
        return {str(item) for item in data}
    return set()


def write_local_hidden_products(hidden: set[str]) -> None:
    HIDDEN_PRODUCTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    HIDDEN_PRODUCTS_PATH.write_text(
        json.dumps({"hidden_product_ids": sorted(hidden)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def local_products(q: str, brand: str, ownership: str, status: str, updated_after: str, recent: str, department_id: str) -> list[dict[str, Any]]:
    cards = [product_card(product_dir, RND_ROOT, DATA_DIR) for product_dir in product_dirs()]
    cards = filter_displayable_products(cards)
    if q:
        needle = q.lower()
        cards = [
            card for card in cards
            if needle in " ".join(str(card.get(k, "")) for k in ["brand", "product_name", "display_name", "color", "sku", "ownership_label", "category", "season", "project_code", "last_name"]).lower()
        ]
    if brand:
        cards = [card for card in cards if card.get("brand") == brand]
    if ownership and not truthy(recent):
        cards = [card for card in cards if card.get("ownership") == ownership]
    if status:
        cards = [card for card in cards if card.get("analysis_status") == status]
    if updated_after:
        cards = [card for card in cards if str(card.get("latest_run_updated_at") or "") >= updated_after]
    if department_id:
        for card in cards:
            card["department_scope"] = department_id
    if truthy(recent):
        return recent_product_cards(cards)
    return sorted(cards, key=lambda item: item.get("latest_run_updated_at") or "", reverse=True)


def safe_product_dir(product_id: str) -> Path:
    if "/" in product_id or "\\" in product_id or product_id in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="invalid product id")
    product_dir = (PRODUCTS_DIR / product_id).resolve()
    products_root = PRODUCTS_DIR.resolve()
    if product_dir != products_root and products_root not in product_dir.parents:
        raise HTTPException(status_code=400, detail="invalid product path")
    if not product_dir.exists():
        raise HTTPException(status_code=404, detail="product not found")
    return product_dir


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8791)
