# Dulce Encanto

Aplicación web de catálogo, inventario y reservas para una tienda de postres desarrollada con Flask y MongoDB.

## Integrantes

- Noh Caamal Lizbet Vanessa
- Ruperto Zetina Fernanda
- Jocabeth Vidal Valeria

## Proyecto original

Dulce Encanto conserva su catálogo de pasteles, panes, postres y bebidas; permite administrar productos, compras, ventas, usuarios, imágenes e inventario.

## Reserva temporal

El elemento reservado es la cantidad disponible de cada producto del catálogo.

- Duración: 5 minutos.
- Antes de reservar, el servidor vuelve a consultar el inventario.
- Dos usuarios no pueden apartar más piezas que las disponibles.
- La reserva conserva `recurso_id`, visitante o correo, cantidad, estado, `creada_en` y `expira_en`.
- Confirmar reduce definitivamente el inventario y registra una venta.
- Cancelar devuelve las cantidades reservadas al inventario.
- Al vencer, el servidor cambia el estado a `expired` y libera las cantidades.
- El contador se genera con la fecha de vencimiento guardada en la base de datos.

Estados: `reserved`, `confirmed`, `cancelled` y `expired`.

## Código QR

- Ficha pública de producto: `/ficha/<product_id>`
- Comprobante público de reserva: `/reserva/<order_id>`
- QR de producto: `/ficha/<product_id>/qr`
- QR de reserva: `/reserva/<order_id>/qr`

El QR contiene la URL pública configurada con `APP_BASE_URL`. El mismo QR sigue consultando el registro actual y muestra su estado actualizado.

## Ejecución local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
flask --app app run --debug
```

Sin MongoDB, el proyecto usa archivos JSON locales mediante `DATABASE_BACKEND=json`. No uses este modo como persistencia de producción.

## Variables de entorno

| Variable | Uso |
|---|---|
| `DATABASE_BACKEND` | `mongodb` para Atlas o `json` para archivos locales |
| `MONGO_URI` o `MONGODB_URI` | Cadena completa de MongoDB Atlas |
| `MONGODB_DATABASE` | Base de datos |
| `SECRET_KEY` o `FLASK_SECRET_KEY` | Llave privada de Flask |
| `APP_BASE_URL` | URL pública de Render, sin barra final |
| `ADMIN_EMAIL` | Correo del administrador inicial |
| `ADMIN_PASSWORD` | Contraseña privada del administrador |

## MongoDB Atlas

Configura Network Access para permitir Render y crea las variables de entorno desde el panel de Render. No subas `.env` ni credenciales al repositorio.

Colecciones principales: `products`, `orders`, `sales`, `purchases`, `users`.

## Render

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Lenguaje: Python 3
- Variables: `DATABASE_BACKEND=mongodb`, `MONGODB_URI`, `MONGODB_DATABASE`, `SECRET_KEY`, `APP_BASE_URL`

## Pruebas esenciales

1. Reservar una cantidad disponible y comprobar el contador.
2. Intentar reservar la última unidad desde otra ventana o incógnito.
3. Cancelar y comprobar que la cantidad vuelve a estar disponible.
4. Dejar vencer una reserva y comprobar el estado `expired`.
5. Confirmar antes de vencer.
6. Recargar y comprobar la persistencia.
7. Escanear el QR con un celular usando la URL de Render.
8. Cambiar el estado y volver a escanear el mismo QR.
9. Abrir un folio o identificador inexistente.
10. Revisar la aplicación desde otro equipo sin usar localhost.
