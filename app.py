from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
from io import BytesIO
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import qrcode
from qrcode.constants import ERROR_CORRECT_M

import click
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    get_flashed_messages,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    from pymongo import ASCENDING, MongoClient
    from pymongo.errors import PyMongoError
except ImportError:
    ASCENDING = None
    MongoClient = None
    PyMongoError = Exception

load_dotenv(Path(__file__).resolve().parent / ".env")


ROOT = Path(__file__).resolve().parent
INSTANCE_DIRECTORY = ROOT / "instance"
PRODUCTS_FILE = INSTANCE_DIRECTORY / "products.json"
PRODUCT_IMAGES_DIRECTORY = INSTANCE_DIRECTORY / "product_images"
MAX_IMAGE_SIZE = 5 * 1024 * 1024
RESERVATION_MINUTES = 5
PRODUCT_CATEGORIES = {
    "pasteles": "Pasteles",
    "panes": "Panes",
    "postres": "Postres",
    "bebidas": "Bebidas",
}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
DEFAULT_PRODUCTS = [
    {
        "name": "Panqueque",
        "category": "pasteles",
        "category_label": "Pasteles",
        "description": "Un antojo delicioso para disfrutar a cualquier hora.",
        "image": "images (1).jfif",
        "tag": "Favorito",
        "price_cents": None,
        "uploaded": False,
    },
    {
        "name": "Delicia de la casa",
        "category": "postres",
        "category_label": "Postres",
        "description": "Una dulce sorpresa para consentirte.",
        "image": "images.jfif",
        "tag": "Recién hecho",
        "price_cents": None,
        "uploaded": False,
    },
    {
        "name": "Capricho artesanal",
        "category": "panes",
        "category_label": "Panes",
        "description": "Un toque de felicidad: el acompañamiento perfecto para tu pausa favorita.",
        "image": "images (2).jfif",
        "tag": "Para compartir",
        "price_cents": None,
        "uploaded": False,
    },
]


def _load_secret_key() -> str:
    configured_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if configured_key:
        return configured_key

    key_file = INSTANCE_DIRECTORY / "secret_key"
    try:
        stored_key = key_file.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        stored_key = ""
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError("No se pudo leer instance/secret_key.") from error
    if re.fullmatch(r"[0-9a-fA-F]{64}", stored_key):
        return stored_key

    key = secrets.token_hex(32)
    temporary_file = key_file.with_name(f"{key_file.name}.{uuid.uuid4().hex}.tmp")
    try:
        INSTANCE_DIRECTORY.mkdir(parents=True, exist_ok=True)
        temporary_file.write_text(key, encoding="ascii")
        temporary_file.replace(key_file)
    except OSError as error:
        temporary_file.unlink(missing_ok=True)
        raise RuntimeError("No se pudo guardar instance/secret_key.") from error
    return key


app = Flask(__name__, instance_path=str(INSTANCE_DIRECTORY))
app.config.update(
    SECRET_KEY=_load_secret_key(),
    MAX_CONTENT_LENGTH=MAX_IMAGE_SIZE + 256 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

DATABASE_BACKEND = os.environ.get("DATABASE_BACKEND", "json").strip().casefold()
MONGODB_URI = (
    os.environ.get("MONGODB_URI", "")
    or os.environ.get("MONGO_URI", "")
).strip()
MONGODB_DATABASE = os.environ.get("MONGODB_DATABASE", "dulce_encanto").strip()
try:
    MONGODB_TIMEOUT_MS = int(os.environ.get("MONGODB_TIMEOUT_MS", "5000"))
except ValueError as error:
    raise RuntimeError("MONGODB_TIMEOUT_MS debe ser un número entero positivo.") from error

if MONGODB_TIMEOUT_MS < 1:
    raise RuntimeError("MONGODB_TIMEOUT_MS debe ser un número entero positivo.")
if DATABASE_BACKEND not in {"json", "mongodb"}:
    raise RuntimeError("DATABASE_BACKEND debe ser 'json' o 'mongodb'.")
if DATABASE_BACKEND == "mongodb":
    if not MONGODB_URI:
        raise RuntimeError("Configura MONGODB_URI para usar DATABASE_BACKEND=mongodb.")
    if not MONGODB_DATABASE:
        raise RuntimeError("MONGODB_DATABASE no puede estar vacío.")
    if MongoClient is None:
        raise RuntimeError("Instala pymongo para usar DATABASE_BACKEND=mongodb.")

_data_lock = threading.RLock()
_mongo_client: Any = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {key: _parse_datetime(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_parse_datetime(item) for item in value]
    if isinstance(value, str) and value.endswith(("Z", "+00:00")):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


def _mongo_collection(name: str) -> Any:
    global _mongo_client
    if MongoClient is None or not MONGODB_URI:
        raise RuntimeError("La base de datos MongoDB no está habilitada.")
    if _mongo_client is None:
        _mongo_client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=MONGODB_TIMEOUT_MS,
            connectTimeoutMS=MONGODB_TIMEOUT_MS,
            socketTimeoutMS=MONGODB_TIMEOUT_MS,
        )
        _mongo_client.admin.command("ping")
    return _mongo_client[MONGODB_DATABASE][name]


def _records_file(name: str) -> Path:
    if name == "products":
        return PRODUCTS_FILE
    return INSTANCE_DIRECTORY / f"{name}.json"


def _read_records(name: str) -> list[dict[str, Any]]:
    if DATABASE_BACKEND == "mongodb":
        records = []
        for document in _mongo_collection(name).find():
            document["id"] = str(document.pop("_id"))
            records.append(_parse_datetime(document))
        return records

    path = _records_file(name)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as file:
            records = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"No se pudo leer el archivo de datos {path.name}.") from error
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise RuntimeError(f"El archivo de datos {path.name} tiene un formato no válido.")
    return [_parse_datetime(record) for record in records]


def _write_records(name: str, records: list[dict[str, Any]]) -> None:
    if DATABASE_BACKEND == "mongodb":
        collection = _mongo_collection(name)
        ids = [str(record["id"]) for record in records]
        if ids:
            collection.delete_many({"_id": {"$nin": ids}})
        else:
            collection.delete_many({})
        for record in records:
            document = dict(record)
            document["_id"] = str(document.pop("id"))
            collection.replace_one({"_id": document["_id"]}, document, upsert=True)
        return

    path = _records_file(name)
    temporary_file = path.with_suffix(path.suffix + ".tmp")
    INSTANCE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(records, file, ensure_ascii=False, indent=2, default=lambda value: value.isoformat())
            file.flush()
            os.fsync(file.fileno())
        temporary_file.replace(path)
    except OSError as error:
        temporary_file.unlink(missing_ok=True)
        raise RuntimeError(f"No se pudieron guardar los datos en {path.name}.") from error


def _all_records(name: str) -> list[dict[str, Any]]:
    with _data_lock:
        return _read_records(name)


def _save_records(name: str, records: list[dict[str, Any]]) -> None:
    with _data_lock:
        _write_records(name, records)


def obtener_coleccion_productos() -> Any:
    return _mongo_collection("products")


def cargar_productos() -> list[dict[str, Any]]:
    return _all_records("products")


def guardar_productos(products: list[dict[str, Any]]) -> None:
    _save_records("products", products)


def guardar_producto(producto: dict[str, Any]) -> None:
    products = cargar_productos()
    product_id = str(producto.get("id") or uuid.uuid4().hex)
    producto["id"] = product_id
    if DATABASE_BACKEND == "mongodb" and producto.get("uploaded"):
        image_path = PRODUCT_IMAGES_DIRECTORY / Path(str(producto.get("image", ""))).name
        if image_path.is_file():
            photo_data = image_path.read_bytes()
            producto["photo"] = photo_data
            producto["photo_mimetype"] = _image_mimetype(photo_data) or "application/octet-stream"
    for index, existing in enumerate(products):
        if str(existing.get("id")) == product_id:
            products[index] = producto
            break
    else:
        products.append(producto)
    guardar_productos(products)


def _seed_products() -> list[dict[str, Any]]:
    products = cargar_productos()
    if products:
        return products
    products = []
    for item in DEFAULT_PRODUCTS:
        product = dict(item)
        product.update(
            id=uuid.uuid4().hex,
            stock_quantity=0,
            reserved_quantity=0,
            created_at=_now(),
        )
        products.append(product)
    guardar_productos(products)
    return products


def _product_availability(product: dict[str, Any]) -> dict[str, Any]:
    result = dict(product)
    stock = max(0, int(result.get("stock_quantity", result.get("quantity", 0)) or 0))
    reserved = max(0, int(result.get("reserved_quantity", 0) or 0))
    result["stock_quantity"] = stock
    result["reserved_quantity"] = min(reserved, stock)
    result["quantity"] = max(0, stock - result["reserved_quantity"])
    result.setdefault("id", str(result.get("_id") or result.get("image", "")))
    result.setdefault("category_label", PRODUCT_CATEGORIES.get(result.get("category"), "Otros"))
    result.setdefault("description", "")
    result.setdefault("tag", "Preparado con cariño en Dulce Encanto")
    result.setdefault("price_cents", 0)
    result.setdefault("uploaded", False)
    return result


def obtener_catalogo() -> list[dict[str, Any]]:
    products = [_product_availability(product) for product in _seed_products()]
    return sorted(products, key=lambda product: product.get("name", "").casefold())


def _catalog_with_reservations() -> list[dict[str, Any]]:
    expirations: dict[str, list[datetime]] = {}
    for order in _all_records("orders"):
        expires_at = order.get("expires_at")
        if order.get("status") != "reserved" or not isinstance(expires_at, datetime):
            continue
        for item in order.get("items", []):
            expirations.setdefault(str(item.get("product_id")), []).append(expires_at)

    products = obtener_catalogo()
    for product in products:
        product_expirations = expirations.get(str(product["id"]), [])
        product["reserved"] = product["reserved_quantity"] > 0
        if product["reserved"]:
            product["reservation_expires_at"] = min(product_expirations) if product_expirations else (
                _now() + timedelta(minutes=RESERVATION_MINUTES)
            )
    return products


def _get_product(product_id: str) -> dict[str, Any] | None:
    return next((product for product in obtener_catalogo() if str(product["id"]) == product_id), None)


def _update_product(product: dict[str, Any]) -> None:
    product["updated_at"] = _now()
    guardar_producto(product)


def _get_user(email: str) -> dict[str, Any] | None:
    normalized = email.strip().casefold()
    return next(
        (user for user in _all_records("users") if user.get("email", "").casefold() == normalized),
        None,
    )


def _ensure_admin_account() -> None:
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        return
    email = os.environ.get("ADMIN_EMAIL", "admin").strip().casefold()
    if not email:
        return
    if _get_user(email):
        return
    users = _all_records("users")
    users.append(
        {
            "id": uuid.uuid4().hex,
            "email": email,
            "password_hash": generate_password_hash(password),
            "role": "admin",
            "created_at": _now(),
        }
    )
    _save_records("users", users)


def _current_user() -> dict[str, Any] | None:
    email = session.get("user_email")
    return _get_user(email) if isinstance(email, str) else None


def _is_admin() -> bool:
    user = _current_user()
    return bool(user and user.get("role") == "admin")


def _require_admin() -> Any:
    if not _is_admin():
        flash("Inicia sesión como administrador para continuar.", "error")
        return redirect(url_for("inicio"))
    return None


def _parse_quantity(value: str, *, minimum: int = 1, maximum: int = 100000) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Escribe una cantidad válida.") from error
    if quantity < minimum or quantity > maximum:
        raise ValueError("La cantidad está fuera del rango permitido.")
    return quantity


def _price_to_cents(value: str) -> int:
    try:
        price = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Escribe un precio válido.") from error
    if not price.is_finite() or price <= 0 or price >= Decimal("1000000"):
        raise ValueError("Escribe un precio mayor que $0 y menor que $1,000,000.")
    if price.as_tuple().exponent < -2:
        raise ValueError("El precio puede tener como máximo dos decimales.")
    return int(price * 100)


def _email_is_valid(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)) and len(email) <= 254


def _order_item(product: dict[str, Any], quantity: int) -> dict[str, Any]:
    return {
        "product_id": str(product["id"]),
        "product_name": product["name"],
        "quantity": quantity,
        "unit_price_cents": int(product.get("price_cents", 0) or 0),
    }


def _order_total(items: list[dict[str, Any]]) -> int:
    return sum(int(item["unit_price_cents"]) * int(item["quantity"]) for item in items)


def _new_order(
    items: list[dict[str, Any]],
    customer_name: str,
    customer_email: str,
    note: str,
    status: str,
) -> dict[str, Any]:
    now = _now()
    return {
        "id": uuid.uuid4().hex,
        "items": items,
        "customer_name": customer_name[:100],
        "customer_email": customer_email.strip().casefold()[:254],
        "note": note[:500],
        "status": status,
        "total_cents": _order_total(items),
        "created_at": now,
        "expires_at": now + timedelta(minutes=RESERVATION_MINUTES) if status == "reserved" else None,
    }


def _find_order(order_id: str, orders: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((order for order in orders if str(order.get("id")) == order_id), None)


def _change_reserved_stock(items: list[dict[str, Any]], amount: int) -> None:
    products = cargar_productos()
    by_id = {str(product.get("id")): product for product in products}
    for item in items:
        product = by_id.get(str(item.get("product_id")))
        if product is None:
            continue
        product["reserved_quantity"] = max(
            0, int(product.get("reserved_quantity", 0) or 0) + amount * int(item["quantity"])
        )
    guardar_productos(products)


def _expire_reservations() -> None:
    orders = _all_records("orders")
    now = _now()
    expired = [
        order
        for order in orders
        if order.get("status") == "reserved"
        and isinstance(order.get("expires_at"), datetime)
        and order["expires_at"] <= now
    ]
    if not expired:
        return
    for order in expired:
        _change_reserved_stock(order.get("items", []), -1)
        order["status"] = "expired"
        order["updated_at"] = now
    _save_records("orders", orders)


def _active_reservations(email: str | None) -> list[dict[str, Any]]:
    _expire_reservations()
    reservations = []
    now = _now()
    for order in _all_records("orders"):
        if order.get("status") != "reserved":
            continue
        if email and order.get("customer_email") != email.casefold():
            continue
        if not email and order.get("id") not in session.get("reservation_ids", []):
            continue
        expires_at = order.get("expires_at")
        if not isinstance(expires_at, datetime):
            continue
        order["remaining_seconds"] = max(0, int((expires_at - now).total_seconds()))
        reservations.append(order)
    return sorted(reservations, key=lambda order: order.get("created_at", now), reverse=True)


def _public_base_url() -> str:
    configured = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return request.url_root.rstrip("/")


def _reservation_is_visible(order: dict[str, Any]) -> bool:
    return _owns_reservation(order)


def _reservation_status_label(status: str) -> str:
    return {
        "reserved": "Reservado temporalmente",
        "confirmed": "Confirmado",
        "cancelled": "Cancelado",
        "expired": "Vencido",
    }.get(status, status)


def _cart_summary() -> dict[str, Any]:
    products = {str(product["id"]): product for product in obtener_catalogo()}
    cart = session.get("cart", {})
    items = []
    for product_id, raw_quantity in cart.items():
        product = products.get(str(product_id))
        if product is None:
            continue
        quantity = max(0, int(raw_quantity))
        if quantity == 0:
            continue
        items.append(
            {
                "product_id": product_id,
                "name": product["name"],
                "quantity": quantity,
                "line_total_cents": int(product.get("price_cents", 0) or 0) * quantity,
            }
        )
    return {
        "items": items,
        "count": sum(item["quantity"] for item in items),
        "total_cents": sum(item["line_total_cents"] for item in items),
    }


def _make_sale(order: dict[str, Any]) -> None:
    products = {str(product["id"]): product for product in cargar_productos()}
    for item in order.get("items", []):
        product = products.get(str(item.get("product_id")))
        quantity = int(item.get("quantity", 0))
        if product is None or quantity < 1:
            raise ValueError("El producto del pedido ya no está disponible.")
        stock = int(product.get("stock_quantity", product.get("quantity", 0)) or 0)
        if stock < quantity:
            raise ValueError(f"No hay suficiente inventario de {product.get('name', 'este producto')}.")
        product["stock_quantity"] = stock - quantity
        product["reserved_quantity"] = max(
            0, int(product.get("reserved_quantity", 0) or 0) - (quantity if order.get("status") == "reserved" else 0)
        )
    guardar_productos(list(products.values()))
    sales = _all_records("sales")
    sales.append(
        {
            "id": uuid.uuid4().hex,
            "items": order["items"],
            "customer_email": order.get("customer_email", ""),
            "total_cents": int(order.get("total_cents", 0) or 0),
            "created_at": _now(),
        }
    )
    _save_records("sales", sales)


def _owns_reservation(order: dict[str, Any]) -> bool:
    user = _current_user()
    if user:
        return user.get("email") == order.get("customer_email")
    return order.get("id") in session.get("reservation_ids", [])


def _image_mimetype(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _save_uploaded_image(upload: Any) -> str:
    extension = Path(secure_filename(upload.filename or "")).suffix.casefold()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("La foto debe ser JPG, PNG, GIF o WEBP.")
    data = upload.stream.read(MAX_IMAGE_SIZE + 1)
    if not data:
        raise ValueError("Selecciona una foto para el producto.")
    if len(data) > MAX_IMAGE_SIZE:
        raise ValueError("La imagen no puede superar 5 MB.")
    mimetype = _image_mimetype(data)
    if mimetype is None:
        raise ValueError("El archivo seleccionado no parece ser una imagen válida.")
    actual_extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }[mimetype]
    filename = f"{uuid.uuid4().hex}{actual_extension}"
    PRODUCT_IMAGES_DIRECTORY.mkdir(parents=True, exist_ok=True)
    image_path = PRODUCT_IMAGES_DIRECTORY / filename
    try:
        image_path.write_bytes(data)
    except OSError as error:
        raise RuntimeError("No se pudo guardar la foto del producto.") from error
    return filename


def _admin_dashboard_data() -> dict[str, Any]:
    products = obtener_catalogo()
    inventory = [
        {
            "product_name": product["name"],
            "quantity": product["stock_quantity"],
            "reserved": product["reserved_quantity"],
            "available": product["quantity"],
        }
        for product in products
    ]
    orders = [
        order for order in _all_records("orders")
        if order.get("status") == "pending"
    ]
    users = sorted(_all_records("users"), key=lambda user: user.get("email", "").casefold())
    return {
        "productos": products,
        "inventory": inventory,
        "pedidos": sorted(orders, key=lambda order: order.get("created_at", _now())),
        "ventas": sorted(_all_records("sales"), key=lambda item: item.get("created_at", _now()), reverse=True)[:20],
        "compras": sorted(_all_records("purchases"), key=lambda item: item.get("created_at", _now()), reverse=True)[:20],
        "users": users,
        "categorias": PRODUCT_CATEGORIES,
    }


@app.get("/")
def index() -> str:
    _seed_products()
    _expire_reservations()
    user = _current_user()
    email = user.get("email") if user else None
    return render_template(
        "index.html",
        productos=_catalog_with_reservations(),
        usuario_email=email,
        es_admin=bool(user and user.get("role") == "admin"),
        reservas_activas=_active_reservations(email),
        carrito=_cart_summary(),
        flashes=get_flashed_messages(with_categories=True),
    )


@app.route("/inicio", methods=["GET", "POST"])
def inicio() -> Any:
    _ensure_admin_account()
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        user = _get_user(email)
        if user and check_password_hash(user.get("password_hash", ""), password):
            session.clear()
            session["user_email"] = user["email"]
            flash("Sesión iniciada correctamente.", "success")
            return redirect(url_for("administracion" if user.get("role") == "admin" else "index"))
        error = "El correo o la contraseña no son correctos."
    else:
        messages = get_flashed_messages()
        if messages:
            error = messages[-1]
    return render_template("inicio.html", error=error)


@app.post("/registro")
def registro() -> Any:
    email = request.form.get("email", "").strip().casefold()
    password = request.form.get("password", "")
    if not _email_is_valid(email):
        flash("Escribe un correo electrónico válido.", "error")
    elif len(password) < 8:
        flash("La contraseña debe tener al menos 8 caracteres.", "error")
    elif _get_user(email):
        flash("Ya existe una cuenta con ese correo.", "error")
    else:
        users = _all_records("users")
        users.append(
            {
                "id": uuid.uuid4().hex,
                "email": email,
                "password_hash": generate_password_hash(password),
                "role": "customer",
                "created_at": _now(),
            }
        )
        _save_records("users", users)
        flash("Tu cuenta fue creada. Ya puedes iniciar sesión.", "success")
    return redirect(url_for("inicio"))


@app.route("/administracion")
def administracion() -> Any:
    response = _require_admin()
    if response is not None:
        return response
    _ensure_admin_account()
    _expire_reservations()
    user = _current_user()
    return render_template(
        "administracion.html",
        usuario_email=user["email"] if user else "",
        **_admin_dashboard_data(),
    )


@app.post("/administracion/productos")
def agregar_producto() -> Any:
    response = _require_admin()
    if response is not None:
        return response
    try:
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        if not name or len(name) > 100:
            raise ValueError("Escribe un nombre de hasta 100 caracteres.")
        if category not in PRODUCT_CATEGORIES:
            raise ValueError("Selecciona una categoría válida.")
        price_cents = _price_to_cents(request.form.get("price", ""))
        quantity = _parse_quantity(request.form.get("quantity", "0"), minimum=0)
        photo = request.files.get("photo")
        if photo is None or not photo.filename:
            raise ValueError("Selecciona una foto para el producto.")
        image_name = _save_uploaded_image(photo)
        product = {
            "id": uuid.uuid4().hex,
            "name": name,
            "category": category,
            "category_label": PRODUCT_CATEGORIES[category],
            "description": "Preparado con cariño en Dulce Encanto.",
            "image": image_name,
            "tag": "Nuevo",
            "price_cents": price_cents,
            "stock_quantity": quantity,
            "reserved_quantity": 0,
            "uploaded": True,
            "created_at": _now(),
        }
        guardar_producto(product)
        flash("Se agregó al catálogo.", "success")
    except (ValueError, RuntimeError) as error:
        flash(str(error), "error")
    return redirect(url_for("administracion"))


@app.post("/cerrar-sesion")
def cerrar_sesion() -> Any:
    session.clear()
    flash("Se cerró la sesión.", "success")
    return redirect(url_for("index"))


@app.get("/img/<path:filename>")
def servir_imagen(filename: str) -> Any:
    safe_name = Path(filename).name
    if safe_name != filename:
        abort(404)
    return send_from_directory(ROOT / "img", safe_name)


@app.get("/product-images/<path:filename>")
def product_image(filename: str) -> Any:
    safe_name = Path(filename).name
    if safe_name != filename:
        abort(404)
    image_path = PRODUCT_IMAGES_DIRECTORY / safe_name
    if not image_path.is_file() and DATABASE_BACKEND == "mongodb":
        product = next(
            (item for item in cargar_productos() if item.get("image") == safe_name and item.get("photo")),
            None,
        )
        if product is not None:
            return send_file(
                BytesIO(product["photo"]),
                mimetype=product.get("photo_mimetype", "application/octet-stream"),
                download_name=safe_name,
            )
    return send_from_directory(PRODUCT_IMAGES_DIRECTORY, safe_name)


@app.get("/ficha/<product_id>")
def ficha_producto(product_id: str) -> Any:
    _expire_reservations()
    product = _get_product(product_id)
    if product is None:
        return render_template(
            "ficha.html",
            encontrado=False,
            tipo="producto",
            titulo="Producto no encontrado",
            mensaje="El identificador no corresponde a ningún producto.",
        ), 404
    return render_template(
        "ficha.html",
        encontrado=True,
        tipo="producto",
        producto=product,
        titulo=product["name"],
        qr_url=_public_base_url() + url_for("ficha_producto", product_id=product_id),
    )


@app.get("/ficha/<product_id>/qr")
def ficha_producto_qr(product_id: str) -> Any:
    if _get_product(product_id) is None:
        abort(404)
    public_url = _public_base_url() + url_for("ficha_producto", product_id=product_id)
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(public_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#39251f", back_color="white")
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", download_name=f"producto-{product_id}.png")


@app.get("/reserva/<order_id>")
def ficha_reserva(order_id: str) -> Any:
    _expire_reservations()
    order = _find_order(order_id, _all_records("orders"))
    if order is None:
        return render_template(
            "ficha.html",
            encontrado=False,
            tipo="reserva",
            titulo="Reserva no encontrada",
            mensaje="El folio no existe o ya no está registrado.",
        ), 404
    expires_at = order.get("expires_at")
    remaining_seconds = 0
    if order.get("status") == "reserved" and isinstance(expires_at, datetime):
        remaining_seconds = max(0, int((expires_at - _now()).total_seconds()))
    return render_template(
        "ficha.html",
        encontrado=True,
        tipo="reserva",
        reserva=order,
        estado=_reservation_status_label(order.get("status", "")),
        remaining_seconds=remaining_seconds,
        qr_url=_public_base_url() + url_for("ficha_reserva", order_id=order_id),
    )


@app.get("/reserva/<order_id>/qr")
def reserva_qr(order_id: str) -> Any:
    order = _find_order(order_id, _all_records("orders"))
    if order is None:
        abort(404)
    public_url = _public_base_url() + url_for("ficha_reserva", order_id=order_id)
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(public_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#39251f", back_color="white")
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", download_name=f"reserva-{order_id}.png")


@app.post("/agregar-al-carrito")
def agregar_al_carrito() -> Any:
    product_id = request.form.get("product_id", "")
    try:
        quantity = _parse_quantity(request.form.get("quantity", "1"), maximum=100)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index"))
    product = _get_product(product_id)
    if product is None:
        flash("El producto ya no está disponible.", "error")
        return redirect(url_for("index"))
    cart = dict(session.get("cart", {}))
    new_quantity = int(cart.get(product_id, 0)) + quantity
    if new_quantity > product["quantity"]:
        flash("No hay suficientes piezas disponibles.", "error")
    else:
        cart[product_id] = new_quantity
        session["cart"] = cart
        flash("Se agregó al carrito.", "success")
    return redirect(url_for("index"))


@app.post("/vaciar-carrito")
def vaciar_carrito_route() -> Any:
    session.pop("cart", None)
    flash("Se vació el carrito.", "success")
    return redirect(url_for("index"))


@app.post("/reservar-carrito")
def reservar_carrito() -> Any:
    with _data_lock:
        _expire_reservations()
        summary = _cart_summary()
        if not summary["items"]:
            flash("Tu carrito está vacío.", "error")
            return redirect(url_for("index"))
        name = request.form.get("customer_name", "").strip()
        email = request.form.get("customer_email", "").strip().casefold()
        note = request.form.get("note", "").strip()
        if not name or len(name) > 100 or not _email_is_valid(email):
            flash("Escribe tu nombre y un correo válido.", "error")
            return redirect(url_for("index"))
        products = {str(product["id"]): product for product in cargar_productos()}
        items = []
        for cart_item in summary["items"]:
            product = products.get(str(cart_item["product_id"]))
            if product is None:
                flash("Un producto del carrito ya no está disponible.", "error")
                return redirect(url_for("index"))
            quantity = int(cart_item["quantity"])
            stock = int(product.get("stock_quantity", product.get("quantity", 0)) or 0)
            reserved = int(product.get("reserved_quantity", 0) or 0)
            if stock - reserved < quantity:
                flash(f"No hay suficientes piezas de {product['name']}.", "error")
                return redirect(url_for("index"))
            items.append(_order_item(product, quantity))

        order = _new_order(items, name, email, note, "reserved")
        if DATABASE_BACKEND == "mongodb":
            collection = obtener_coleccion_productos()
            reserved_item_ids = []
            for item in items:
                result = collection.update_one(
                    {
                        "_id": str(item["product_id"]),
                        "$expr": {
                            "$gte": [
                                {"$subtract": [
                                    {"$ifNull": ["$stock_quantity", "$quantity"]},
                                    {"$ifNull": ["$reserved_quantity", 0]},
                                ]},
                                int(item["quantity"]),
                            ]
                        },
                    },
                    {"$inc": {"reserved_quantity": int(item["quantity"])}},
                )
                if result.modified_count != 1:
                    for reserved_item in reserved_item_ids:
                        collection.update_one(
                            {"_id": str(reserved_item["product_id"])},
                            {"$inc": {"reserved_quantity": -int(reserved_item["quantity"])}},
                        )
                    flash("Otro usuario tomó la última cantidad disponible.", "error")
                    return redirect(url_for("index"))
                reserved_item_ids.append(item)
        else:
            for item in items:
                product = products[str(item["product_id"])]
                product["reserved_quantity"] = int(product.get("reserved_quantity", 0) or 0) + int(item["quantity"])
            guardar_productos(list(products.values()))

        orders = _all_records("orders")
        orders.append(order)
        _save_records("orders", orders)
        session.pop("cart", None)
        reservation_ids = list(session.get("reservation_ids", []))
        reservation_ids.append(order["id"])
        session["reservation_ids"] = reservation_ids
    flash("Tu carrito quedó reservado durante 5 minutos.", "success")
    return redirect(url_for("index"))


@app.post("/crear-pedido")
def crear_pedido() -> Any:
    product = _get_product(request.form.get("product_id", ""))
    if product is None:
        flash("El producto ya no está disponible.", "error")
        return redirect(url_for("index"))
    try:
        quantity = _parse_quantity(request.form.get("quantity", "1"), maximum=100)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index"))
    if quantity > product["quantity"]:
        flash("No hay suficientes piezas disponibles.", "error")
        return redirect(url_for("index"))
    name = request.form.get("customer_name", "").strip()
    email = request.form.get("customer_email", "").strip().casefold()
    note = request.form.get("note", "").strip()
    if not name or len(name) > 100 or not _email_is_valid(email):
        flash("Escribe tu nombre y un correo válido.", "error")
        return redirect(url_for("index"))
    order = _new_order([_order_item(product, quantity)], name, email, note, "pending")
    orders = _all_records("orders")
    orders.append(order)
    _save_records("orders", orders)
    flash("Tu solicitud fue enviada a la pastelería.", "success")
    return redirect(url_for("index"))


@app.post("/reservas/<order_id>/confirmar")
def confirmar_reserva_route(order_id: str) -> Any:
    _expire_reservations()
    orders = _all_records("orders")
    order = _find_order(order_id, orders)
    if order is None or order.get("status") != "reserved" or not _owns_reservation(order):
        abort(404)
    try:
        _make_sale(order)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("index"))
    order["status"] = "confirmed"
    order["updated_at"] = _now()
    _save_records("orders", orders)
    session["reservation_ids"] = [
        reservation_id for reservation_id in session.get("reservation_ids", []) if reservation_id != order_id
    ]
    flash("Tu pedido fue confirmado. ¡Gracias por tu compra!", "success")
    return redirect(url_for("index"))


@app.post("/reservas/<order_id>/cancelar")
def cancelar_reserva_route(order_id: str) -> Any:
    orders = _all_records("orders")
    order = _find_order(order_id, orders)
    if order is None or order.get("status") != "reserved" or not _owns_reservation(order):
        abort(404)
    _change_reserved_stock(order.get("items", []), -1)
    order["status"] = "cancelled"
    order["updated_at"] = _now()
    _save_records("orders", orders)
    session["reservation_ids"] = [
        reservation_id for reservation_id in session.get("reservation_ids", []) if reservation_id != order_id
    ]
    flash("Se canceló tu reserva.", "success")
    return redirect(url_for("index"))


@app.post("/administracion/pedidos/<order_id>/confirmar")
def confirmar_pedido(order_id: str) -> Any:
    response = _require_admin()
    if response is not None:
        return response
    orders = _all_records("orders")
    order = _find_order(order_id, orders)
    if order is None or order.get("status") != "pending":
        abort(404)
    try:
        _make_sale(order)
    except ValueError as error:
        flash(str(error), "error")
    else:
        order["status"] = "confirmed"
        order["updated_at"] = _now()
        _save_records("orders", orders)
        flash("Se confirmó el pedido y se registró la venta.", "success")
    return redirect(url_for("administracion"))


@app.post("/administracion/pedidos/<order_id>/cancelar")
def cancelar_pedido(order_id: str) -> Any:
    response = _require_admin()
    if response is not None:
        return response
    orders = _all_records("orders")
    order = _find_order(order_id, orders)
    if order is None or order.get("status") != "pending":
        abort(404)
    order["status"] = "cancelled"
    order["updated_at"] = _now()
    _save_records("orders", orders)
    flash("Se canceló el pedido.", "success")
    return redirect(url_for("administracion"))


@app.post("/administracion/compras")
def crear_compra() -> Any:
    response = _require_admin()
    if response is not None:
        return response
    try:
        product_id = request.form.get("product_id", "")
        quantity = _parse_quantity(request.form.get("quantity", ""))
        unit_cost_cents = _price_to_cents(request.form.get("unit_cost", ""))
        supplier = request.form.get("supplier", "").strip()
        if not supplier or len(supplier) > 120:
            raise ValueError("Escribe el nombre del proveedor.")
        products = cargar_productos()
        product = next((item for item in products if str(item.get("id")) == product_id), None)
        if product is None:
            raise ValueError("Selecciona un producto válido.")
        product["stock_quantity"] = int(product.get("stock_quantity", product.get("quantity", 0)) or 0) + quantity
        guardar_productos(products)
        purchases = _all_records("purchases")
        purchases.append(
            {
                "id": uuid.uuid4().hex,
                "items": [{
                    "product_id": product_id,
                    "product_name": product["name"],
                    "quantity": quantity,
                    "unit_cost_cents": unit_cost_cents,
                }],
                "supplier": supplier,
                "total_cents": unit_cost_cents * quantity,
                "created_at": _now(),
            }
        )
        _save_records("purchases", purchases)
        flash("Se guardó la compra y se actualizó el inventario.", "success")
    except (ValueError, RuntimeError) as error:
        flash(str(error), "error")
    return redirect(url_for("administracion"))


@app.post("/administracion/ventas")
def crear_venta() -> Any:
    response = _require_admin()
    if response is not None:
        return response
    try:
        product_id = request.form.get("product_id", "")
        quantity = _parse_quantity(request.form.get("quantity", ""))
        customer_email = request.form.get("customer_email", "").strip().casefold()
        product = next((item for item in cargar_productos() if str(item.get("id")) == product_id), None)
        if product is None or int(product.get("price_cents", 0) or 0) <= 0:
            raise ValueError("Selecciona un producto con precio asignado.")
        order = _new_order(
            [_order_item(product, quantity)],
            "",
            customer_email,
            "",
            "pending",
        )
        _make_sale(order)
        flash("Se registró la venta y se actualizó el inventario.", "success")
    except (ValueError, RuntimeError) as error:
        flash(str(error), "error")
    return redirect(url_for("administracion"))


@app.post("/administracion/usuarios")
def crear_usuario() -> Any:
    response = _require_admin()
    if response is not None:
        return response
    email = request.form.get("email", "").strip().casefold()
    password = request.form.get("password", "")
    role = request.form.get("role", "customer")
    if not _email_is_valid(email):
        flash("Escribe un correo electrónico válido.", "error")
    elif len(password) < 12:
        flash("La contraseña debe tener al menos 12 caracteres.", "error")
    elif role not in {"customer", "admin"}:
        flash("Selecciona un rol válido.", "error")
    elif _get_user(email):
        flash("Ya existe una cuenta con ese correo.", "error")
    else:
        users = _all_records("users")
        users.append(
            {
                "id": uuid.uuid4().hex,
                "email": email,
                "password_hash": generate_password_hash(password),
                "role": role,
                "created_at": _now(),
            }
        )
        _save_records("users", users)
        flash("Se creó la cuenta.", "success")
    return redirect(url_for("administracion"))


@app.cli.command("migrate-products-to-mongodb")
def migrate_products_to_mongodb() -> None:
    """Move locally saved products and their photos into MongoDB."""
    if DATABASE_BACKEND != "mongodb":
        raise click.ClickException(
            "Activa MongoDB con DATABASE_BACKEND=mongodb y configura MONGODB_URI."
        )
    local_file = PRODUCTS_FILE
    if not local_file.exists():
        raise click.ClickException("No hay productos locales para migrar.")
    try:
        with local_file.open("r", encoding="utf-8") as file:
            products = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise click.ClickException(f"No se pudo leer el catálogo local: {error}") from error
    if not isinstance(products, list) or not all(isinstance(item, dict) for item in products):
        raise click.ClickException("El archivo de productos local tiene un formato no válido.")
    migrated = 0
    for product in products:
        product_id = str(product.get("id") or uuid.uuid4().hex)
        document = dict(product, id=product_id)
        if document.get("uploaded") and document.get("image"):
            image_path = PRODUCT_IMAGES_DIRECTORY / Path(document["image"]).name
            if image_path.is_file():
                document["photo"] = image_path.read_bytes()
                document["photo_mimetype"] = _image_mimetype(document["photo"]) or "application/octet-stream"
        collection = obtener_coleccion_productos()
        mongo_document = dict(document)
        mongo_document["_id"] = str(mongo_document.pop("id"))
        collection.replace_one({"_id": mongo_document["_id"]}, mongo_document, upsert=True)
        migrated += 1
    click.echo(f"Migración completada: {migrated} productos procesados.")


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "").strip().casefold() in {"1", "true", "yes"})
