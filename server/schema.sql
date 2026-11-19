-- Схема базы ZENTO.
--
-- Написана на стандартном SQL, чтобы её можно было перенести на PostgreSQL:
--   * время хранится строкой ISO 8601 в UTC, например 2026-11-19T07:52:00.000001Z;
--   * цены и суммы — целые гривны;
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
    address     TEXT NOT NULL DEFAULT '',
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

-- Покупатели (аккаунты). Вход по нику или почте. Ник показываем как ввёл, сравниваем по nick_lower.
-- Почта необязательна, в нижнем регистре. Пароль — только в виде scrypt-хеша.
-- phone — старые данные до договора 2 (новые аккаунты его не получают, NULL допускается).
-- Если таблица создана по старой схеме, её пересобирает store._migrate_users().
CREATE TABLE IF NOT EXISTS users (
    id                INTEGER PRIMARY KEY,
    nick              TEXT NOT NULL,
    nick_lower        TEXT NOT NULL UNIQUE,
    email             TEXT UNIQUE,
    password_hash     TEXT NOT NULL,
    phone             TEXT UNIQUE,
    created_at        TEXT NOT NULL,
    rules_accepted_at TEXT NOT NULL
);

-- Коды восстановления пароля: один код на пользователя (новый заменяет старый). Только хеш.
CREATE TABLE IF NOT EXISTS reset_codes (
    user_id    INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    code_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    used_at    TEXT
);

-- Сессии: в базе только SHA-256 от токена из cookie.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);

-- Задания капчи. expected — JSON {"<токен кусочка>": номер части 0..3}.
CREATE TABLE IF NOT EXISTS captcha_challenges (
    id         TEXT PRIMARY KEY,
    expected   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

-- Билеты после правильной капчи: только хеш, одноразовые.
CREATE TABLE IF NOT EXISTS captcha_tickets (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
