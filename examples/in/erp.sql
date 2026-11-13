CREATE TABLE orders (
    order_id BIGINT PRIMARY KEY,
    customer_email TEXT,
    created_at TIMESTAMP
);

CREATE TABLE "顧客マスタ" (
    "顧客ID" BIGINT PRIMARY KEY,
    "電話番号" VARCHAR(32),
    "住所" TEXT
);

CREATE TABLE `주문_내역` (
    `주문번호` BIGINT,
    `주민등록번호` VARCHAR(16)
);

CREATE TABLE "Société" (
    "téléphone" VARCHAR(32),
    "adresse_ligne_1" TEXT
);
