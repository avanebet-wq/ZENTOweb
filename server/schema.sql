-- Схема базы ZENTO.
--
-- Написана на стандартном SQL, чтобы её можно было перенести на PostgreSQL:
--   * время хранится строкой ISO 8601 в UTC, например 2026-11-19T07:52:00.000001Z;
--   * цены и суммы — целые рубли;
--   * статусы заказа проверяются CHECK-ограничением;
--   * идентификаторы товаров — текст (можно взять из products.json как есть).
-- При переходе на PostgreSQL заменить INTEGER PRIMARY KEY на BIGSERIAL/IDENTITY.

CREATE TABLE IF NOT EXISTS products (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    price       INTEGER NOT NULL CHECK (price >= 0),
    description TEXT NOT NULL DEFAULT '',
    color       TEXT NOT NULL DEFAULT '',
    stock       INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);

CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL UNIQUE,
    email      TEXT NOT NULL DEFAULT '',
    address    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (id),
    status      TEXT NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'paid', 'shipped', 'done', 'cancelled')),
    total       INTEGER NOT NULL CHECK (total >= 0),
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (created_at);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders (customer_id);

CREATE TABLE IF NOT EXISTS order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders (id),
    product_id TEXT NOT NULL REFERENCES products (id),
    qty        INTEGER NOT NULL CHECK (qty >= 1),
    price      INTEGER NOT NULL CHECK (price >= 0)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (order_id);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,
    path       TEXT NOT NULL DEFAULT '',
    product_id TEXT REFERENCES products (id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events (created_at);
