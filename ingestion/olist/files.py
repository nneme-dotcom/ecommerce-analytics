"""The nine CSVs of the Olist dataset and the raw tables they load into (DATA_CONTRACT §2).

Headers were verified against the official Kaggle archive on 2026-11-03. The loader refuses a file
whose header differs, printing the header it found. Column names are kept exactly as published,
typos included (``product_name_lenght``); a BOM before the first header is stripped.
"""

from __future__ import annotations

from dataclasses import dataclass

KAGGLE_DATASET = "olistbr/brazilian-ecommerce"
RAW_SCHEMA = "raw_olist"
AUDIT_TABLE = "_load_audit"
MANIFEST_NAME = "manifest.json"


@dataclass(frozen=True)
class OlistFile:
    table: str
    filename: str
    columns: tuple[str, ...]


OLIST_FILES: tuple[OlistFile, ...] = (
    OlistFile(
        "customers",
        "olist_customers_dataset.csv",
        (
            "customer_id",
            "customer_unique_id",
            "customer_zip_code_prefix",
            "customer_city",
            "customer_state",
        ),
    ),
    OlistFile(
        "geolocation",
        "olist_geolocation_dataset.csv",
        (
            "geolocation_zip_code_prefix",
            "geolocation_lat",
            "geolocation_lng",
            "geolocation_city",
            "geolocation_state",
        ),
    ),
    OlistFile(
        "order_items",
        "olist_order_items_dataset.csv",
        (
            "order_id",
            "order_item_id",
            "product_id",
            "seller_id",
            "shipping_limit_date",
            "price",
            "freight_value",
        ),
    ),
    OlistFile(
        "order_payments",
        "olist_order_payments_dataset.csv",
        (
            "order_id",
            "payment_sequential",
            "payment_type",
            "payment_installments",
            "payment_value",
        ),
    ),
    OlistFile(
        "order_reviews",
        "olist_order_reviews_dataset.csv",
        (
            "review_id",
            "order_id",
            "review_score",
            "review_comment_title",
            "review_comment_message",
            "review_creation_date",
            "review_answer_timestamp",
        ),
    ),
    OlistFile(
        "orders",
        "olist_orders_dataset.csv",
        (
            "order_id",
            "customer_id",
            "order_status",
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ),
    ),
    OlistFile(
        "products",
        "olist_products_dataset.csv",
        (
            "product_id",
            "product_category_name",
            "product_name_lenght",
            "product_description_lenght",
            "product_photos_qty",
            "product_weight_g",
            "product_length_cm",
            "product_height_cm",
            "product_width_cm",
        ),
    ),
    OlistFile(
        "sellers",
        "olist_sellers_dataset.csv",
        ("seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"),
    ),
    OlistFile(
        "product_category_name_translation",
        "product_category_name_translation.csv",
        ("product_category_name", "product_category_name_english"),
    ),
)

EXPECTED_FILENAMES: frozenset[str] = frozenset(f.filename for f in OLIST_FILES)
